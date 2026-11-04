"""A native profile action remains visible to chat with its original receipt and sources."""

import asyncio
import json
import pickle
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from companion import account_action_context, owner_authority
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop, HistoryPolicy
from companion.retrieval import SemanticIndex
from companion.social_autonomy import _sha
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.sdk_reader_fixture import answer
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.test_native_account_host import configure_host
from tests.test_onebot_account_actions import (
    model_response,
    native_fixture,
    plan_action,
    pump_account,
)
from tests.test_onebot_capture import real_core_http
from tests.test_owner_authority import activate
from tests.test_social_owner_authority import authorize, close_provider_clients  # noqa: F401


@asynccontextmanager
async def action_then_chat(core, tmp_path, monkeypatch, *, outcome="SUCCEEDED", claim=True,
                           history=True, history_bytes=16000):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    ref, conversation, options, proposal = native_fixture(core, "nickname")
    proposal = {"kind": "nickname", "value": "星语"}
    core.settings.social_autonomy = options["core_options"]["social_autonomy"]
    core.settings.content_enabled = False
    core.settings.context_soft_tokens = 12000
    policy, _ = authorize(core, tmp_path, additional_purposes=("memory",))
    if history:
        policy = policy.model_copy(update={"purposes": [*policy.purposes, "history"],
            "history": HistoryPolicy(scope="owner_private",
            not_before=core.clock.now - timedelta(days=1), max_turns=15,
            max_utf8_bytes=history_bytes)})
    cfg = configure_host(core, tmp_path, policy)
    native = []

    def protocol_reply(request):
        if request.url.path == "/set_qq_profile":
            native.append(request)
            if outcome == "UNKNOWN":
                raise httpx.ReadTimeout("synthetic missing nickname acknowledgment",
                                        request=request)
            return httpx.Response(200, json={"status": "ok" if outcome == "SUCCEEDED" else "failed",
                "retcode": 0 if outcome == "SUCCEEDED" else 1400, "data": None})
        return provider(request, [])

    async with real_core_http(core) as port, httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: model_response(proposal))) as model:
        cfg = replace(cfg, core_url=f"http://127.0.0.1:{port}")
        core.model = ModelClient(core.settings, model)
        with Spool(cfg) as spool:
            runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
            async with private_http_client(cfg.core_url, cfg.bridge_token) as client, (
                private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                                    transport=httpx.MockTransport(protocol_reply))
            ) as protocol:
                frozen = await plan_action(core, ref, conversation, proposal)
                from types import SimpleNamespace

                await pump_account(SimpleNamespace(runtime=runtime, client=client,
                                                    protocol=protocol, spool=spool))
                assert len(native) == 1
                expected = {"SUCCEEDED": "SENT", "REJECTED": "DELIVERY_UNCERTAIN",
                            "UNKNOWN": "DELIVERY_UNCERTAIN"}[outcome]
                assert core.store.one("SELECT state FROM outbox WHERE message_id=?",
                                      (frozen.message_id,))["state"] == expected
                if outcome != "SUCCEEDED":
                    yield frozen, None, cfg, native
                    return
                core.clock.advance(2)
                raw = json.dumps(raw_event(9300, text="你把昵称改成星语了吗？",
                                           time=int(core.clock.now.timestamp())))
                event = normalize(raw, cfg, now=core.clock.now.timestamp())
                runtime.persist_capture(event, raw, received_at=core.clock.now,
                                        peer=("127.0.0.1", 3001))
                assert await runtime.flush_one(client)
                job = core.claim_job("profile-result-chat") if claim else core.store.one(
                    "SELECT * FROM jobs WHERE event_id=? AND kind='chat'", (event["event_id"],))
                assert job["event_id"] == event["event_id"]
                yield frozen, job, cfg, native


def test_chat_receives_actual_profile_result_even_while_background_work_yields(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with action_then_chat(core, tmp_path, monkeypatch) as (_, job, _, _):
            before = (core.store.db.total_changes,
                      len(core.store.rows("SELECT * FROM model_usage")))
            reference = account_action_context.reference(core, job)
            assert reference is not None
            assert reference["data"]["records"][0]["result"] == "SUCCEEDED"
            assert reference["data"]["records"][0]["value"] == "星语"
            assert (core.store.db.total_changes, len(core.store.rows("SELECT * FROM model_usage"))
                    ) == before
            snapshots = []
            messages = core.context(job, memories=[], snapshots=snapshots)
            text = json.dumps(messages, ensure_ascii=False)
            assert "BACKGROUND_YIELD_TO_CHAT" in text
            assert account_action_context.HEADING in text and "SUCCEEDED" in text
            assert any(s["kind"] == "account_actions" for s in snapshots)
            assert {"evt_test_800", "evt_test_801"} <= {r["event_id"] for r in core.store.rows(
                "SELECT event_id FROM job_sources WHERE job_id=?", (job["job_id"],))}
    run(scenario())


@pytest.mark.parametrize("outcome", ["UNKNOWN", "REJECTED"])
def test_uncertain_profile_ack_remains_unknown_without_reopening_delivery(
    core, tmp_path, monkeypatch, outcome,
):
    async def scenario():
        async with action_then_chat(core, tmp_path, monkeypatch, outcome=outcome) as values:
            frozen, _, _, native = values
            view = owner_authority.current(core.store, core.settings, core.clock)
            recorded = account_action_context._record(core, frozen.message_id, core.clock(), view)
            assert recorded is not None and recorded[0]["result"] == "UNKNOWN"
            assert len(native) == 1
    run(scenario())


@pytest.mark.parametrize("history,history_bytes", [(False, 16000), (True, 32)])
def test_history_permission_and_byte_limit_bound_profile_results(
    core, tmp_path, monkeypatch, history, history_bytes,
):
    async def scenario():
        async with action_then_chat(core, tmp_path, monkeypatch,
            history=history, history_bytes=history_bytes) as (_, job, _, _):
            assert account_action_context.reference(core, job) is None
    run(scenario())


@pytest.mark.parametrize("change", ["receipt", "lease", "payload", "source", "erasure"])
def test_invalidated_receipt_or_source_cannot_survive_model_await(
    core, tmp_path, monkeypatch, change,
):
    async def scenario():
        async with action_then_chat(core, tmp_path, monkeypatch) as (frozen, job, _, _):
            snapshots = []
            core.context(job, memories=[], snapshots=snapshots)
            assert any(s["kind"] == "account_actions" for s in snapshots)
            if change in {"receipt", "lease"}:
                row = core.store.one("SELECT receipt_json FROM social_autonomy_reservations")
                receipt = json.loads(row["receipt_json"])
                receipt["native_retcode" if change == "receipt" else "lease_token"] = (
                    1400 if change == "receipt" else "lease_another")
                core.store.execute("UPDATE social_autonomy_reservations SET receipt_json=?,"
                    "receipt_sha256=?", (json.dumps(receipt), _sha(receipt)))
            elif change == "payload":
                core.store.execute("UPDATE outbox SET content_json='{}' WHERE message_id=?",
                                   (frozen.message_id,))
            elif change == "source":
                core.store.execute("UPDATE events SET body_json=? WHERE event_id='evt_test_800'",
                                   (json.dumps({"text": "changed evidence"}),))
            else:
                with core.store.transaction():
                    core.ledger.expire_events(core.store, ["evt_test_800"], core.clock())
            with pytest.raises(CoreError):
                core._check_context_snapshots(job, snapshots)
            assert account_action_context.reference(core, job) is None
    run(scenario())


def test_original_receipt_survives_native_authority_successor(core, tmp_path, monkeypatch):
    async def scenario():
        async with action_then_chat(core, tmp_path, monkeypatch) as (frozen, job, _, _):
            view = owner_authority.current(core.store, core.settings, core.clock)
            core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (job["job_id"],))
            stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="profile-history-stop", authority=view.authority.as_dict()))
            activate(core, view.policy, revision=stopped["authority"]["revision"],
                     operation="profile-history-successor")
            successor = owner_authority.current(core.store, core.settings, core.clock)
            recorded = account_action_context._record(
                core, frozen.message_id, core.clock(), successor)
            assert recorded is not None and recorded[0]["result"] == "SUCCEEDED"
            # Historical evidence never re-authorizes an old chat or action.
            with pytest.raises(CoreError):
                account_action_context.reference(core, job)
    run(scenario())


def test_independent_sdk_worker_receives_original_profile_receipt(core, tmp_path, monkeypatch):
    async def scenario():
        async with action_then_chat(core, tmp_path, monkeypatch, claim=False) as values:
            _, job, _, native = values
            config, output = tmp_path / "worker.pickle", tmp_path / "requests.json"
            config.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                [answer("我把昵称改成星语了。")], "SUCCEEDED", str(output))))
            child = await asyncio.create_subprocess_exec(sys.executable, "-B",
                str(Path(__file__).with_name("reader_sdk_worker.py")), str(config),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(45):
                    _, error = await child.communicate()
                assert child.returncode == 0, error.decode()
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            requests = json.loads(output.read_text("utf-8"))
            assert len(requests) == 1 and len(native) == 1
            text = json.dumps(requests[0]["messages"], ensure_ascii=False)
            assert account_action_context.HEADING in text
            assert '"value":"星语"' in text.replace('\\"', '"')
            assert "SUCCEEDED" in text and "BACKGROUND_YIELD_TO_CHAT" in text
            assert core.store.one("SELECT count(*) n FROM outbox WHERE action_type="
                                  "'qq.profile.nickname'")["n"] == 1
    run(scenario())
