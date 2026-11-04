"""Signed HTTP sources, native Reader receipts and an independent original Life worker."""

import asyncio
import json
import os
import sys
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from companion import owner_authority, paid_budget
from companion.conversation_policy import ConversationPolicyConfig
from companion.life_protocol import (
    LifeLoopConfig,
    LifeQZoneDraft,
    LifeQZoneSourceGrant,
    LifeShadowApproval,
)
from companion.life_topics import _source
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion_onebot_bridge.config import BridgeConfig
from tests.conftest import run
from tests.life_shadow_process import run_native
from tests.test_exploration import URL, configure
from tests.test_onebot_capture import real_core_http
from tests.test_owner_authority import activate, prepare
from tests.test_reading_followup import admit as admit_owner
from tests.test_reading_followup import select_read
from tests.test_reading_protocol import envelope


async def native_worker(core, provider, monkeypatch):
    """Generic compatibility export: observe real selector units, not Core.run_once stubs."""
    await run_native(core.settings, core.clock, provider)


async def process_worker(core, tmp_path, scenario, source_id, *, interrupt=None, restart=False):
    label = "restart" if restart else "worker"
    config, result = tmp_path / f"life-{label}.json", tmp_path / f"life-{label}-result.json"
    value = {"settings": core.settings.model_dump(mode="python"), "now": core.clock(),
             "scenario": scenario, "source_id": source_id, "result_path": str(result),
             "restart": restart}
    encoded = json.dumps(value, default=lambda item: item.get_secret_value()
                         if isinstance(item, SecretStr) else str(item))
    await asyncio.to_thread(config.write_text, encoded, encoding="utf-8")
    child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.life_shadow_process", str(config), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(60):
            if not restart:
                signal = await child.stdout.readline()
                while signal and signal.strip() != b"LIFE_PROVIDER_STARTED":
                    signal = await child.stdout.readline()
                if signal.strip() != b"LIFE_PROVIDER_STARTED":
                    _, error = await child.communicate()
                    state = json.loads(result.read_text("utf-8")) if result.exists() else {}
                    fields = ("state", "job_state", "last_error_code")
                    failures = [{key: row.get(key) for key in fields}
                                for row in state.get("results", [])]
                    raise AssertionError((signal, error.decode(errors="replace"), failures))
                if interrupt:
                    await interrupt()
            output, error = await child.communicate(None if restart else b"continue\n")
            assert child.returncode == 0, (output, error.decode(errors="replace"))
        observed = json.loads(await asyncio.to_thread(result.read_text, encoding="utf-8"))
        assert observed["pid"] != os.getpid()
        return observed
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.parametrize("scenario", [
    "bug", "reading", "qzone", "qzone_revoked", "preempt", "pause", "reading_preempt", "unknown",
])
def test_real_http_source_to_independent_worker_shadow(core, tmp_path, monkeypatch, scenario):
    reading = scenario in {"reading", "reading_preempt"}
    preempt = scenario in {"preempt", "reading_preempt"}
    interrupted = preempt or scenario == "pause"
    core.settings.conversation_policy = ConversationPolicyConfig()
    core.settings.conversation_guidance = "先说一个具体细节，再聊自己的看法。"
    core.settings.control_token = SecretStr("synthetic-life-control-" * 3)
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW")
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    if reading:
        configure(core)
        core.settings.content_enabled = True
        core.settings.rich_input_scope = "references_v1"
    if preempt:
        core.settings.proactive_idle_seconds = 0
    base = prepare(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id, bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
        protocol_epoch="life-native-shadow")
    policy = OwnerGrantPolicy.model_validate({**base.model_dump(),
        "purposes": ["chat", *(["content", "exploration"] if reading else [])],
        "capture": {"public_key": key.public_key().public_bytes_raw().hex(),
            "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
            "protocol_epoch": config.protocol_epoch}})
    grant = SimpleNamespace(policy=policy, key=key,
                            authority=activate(core, policy)[0]["authority"])

    async def exercise():
        next_owner = None
        control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as http:
            inbound, _ = await admit_owner(http, core, config, grant, 1,
                "请阅读 " + URL if reading else "导出Bug还没解决，明天接着看")
            source_id = inbound["event_id"]
            if reading:
                selected = await select_read(core)
                assert selected.requests
                headers = {"Authorization": "Bearer "
                           + core.settings.observation_adapters["bili"].token.get_secret_value()}
                leased = await http.post("/internal/v3/readers/bili/lease", headers=headers)
                assert leased.status_code == 200, leased.text
                item = leased.json()["item"]
                assert item and item["context"]["source_event_ids"] == [source_id]
                checked = await http.post(
                    f"/internal/v3/readers/bili/{item['request_id']}/validate", headers=headers,
                    json={"lease_token": item["lease_token"]})
                assert checked.status_code == 200, checked.text
                body = envelope("bilibili", level="L1")
                body["lease_token"] = item["lease_token"]
                body["observations"][0]["provenance"] = item["context"]
                body["observations"][0]["fetched_at"] = core.clock()
                body["observations"][0]["evidence"]["fetched_at"] = core.clock()
                completed = await http.post(
                    f"/internal/v3/readers/bili/{item['request_id']}/result",
                    headers=headers, json=body)
                assert completed.status_code == 200, completed.text
                source_id = completed.json()["event_ids"][0]
                assert core.store.one("SELECT state FROM reading_attempts")["state"] == "DONE"

            # Retire original foreground custody through the actual control contract.
            stopped = await http.post("/internal/v1/owner-authority/stop", headers=control,
                json={"authority": grant.authority, "operation_id": "life-source-phase-complete"})
            assert stopped.status_code == 200, stopped.text
            conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                          (inbound["event_id"],))["conversation_id"]
            scope = LifeShadowApproval(scope="owner_private_life_shadow_v1",
                authorization_sha256="e" * 64, conversation_id=conversation,
                source_event_ids=[source_id], purposes=["PRIVATE_MESSAGE_DRAFT"],
                expires_at=core.clock.now + timedelta(days=2), max_items=1)
            grant.policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
                "purposes": [*policy.purposes, "life"], "life_shadow": scope})
            grant.authority = activate(core, grant.policy,
                revision=stopped.json()["authority"]["revision"],
                operation="life-source-explicit-scope")[0]["authority"]
            if scenario.startswith("qzone"):
                binding = _source(core.store, core.settings, source_id, core.clock())
                scope = scope.model_copy(update={"qzone_draft_sources": [LifeQZoneSourceGrant(
                    grant_id="synthetic-qzone-purpose-one", revision=1, purpose="qzone_draft",
                    event_id=source_id, source_sha256=binding["source_sha256"],
                    expires_at=scope.expires_at, audience="owner_only",
                    owner_qq_id=core.settings.owner_qq_id)]})
                grant.policy = OwnerGrantPolicy.model_validate({**grant.policy.model_dump(),
                                                               "life_shadow": scope})
                grant.authority = activate(core, grant.policy, revision=grant.authority["revision"],
                    operation="life-qzone-explicit-source")[0]["authority"]
            core.clock.advance(core.settings.proactive_idle_seconds + 1 if reading else 86400)
            baseline_outbox = core.store.rows("SELECT * FROM outbox")
            baseline_ids = {row["message_id"] for row in baseline_outbox}

            async def interrupt():
                nonlocal next_owner
                if preempt:
                    next_owner, _ = await admit_owner(http, core, config, grant, 2,
                                                      "先处理我的新问题")
                elif scenario == "pause":
                    changed = await http.post("/internal/v2/life/runtime-mode", headers=control,
                                              json={"enabled": False})
                    assert changed.status_code == 200, changed.text
                elif scenario == "qzone_revoked":
                    changed = await http.post("/internal/v1/owner-authority/stop", headers=control,
                        json={"authority": grant.authority,
                              "operation_id": "revoke-qzone-inflight"})
                    assert changed.status_code == 200, changed.text

            observed = await process_worker(core, tmp_path, scenario, source_id,
                                            interrupt=interrupt)
            result = core.store.one("SELECT * FROM life_activity_results")
            assert observed["provider_calls"] == 1
            assert observed["materials"][0]["sources"][0]["event_id"] == source_id
            system = observed["prompts"][0][0]["content"]
            assert (core.settings.conversation_guidance in system) == (not scenario.startswith(
                "qzone"))
            assert any(unit["lane"] == "life_topic" and unit["progressed"]
                       for unit in observed["units"])
            assert [(u["lane"], u["admissions"]) for u in observed["units"] if u["admissions"]] == [
                ("activity", 1)]
            if interrupted or scenario in {"qzone_revoked", "unknown"}:
                assert result["text"] == "" and result["state"] != "SHADOW"
            else:
                assert result["state"] == "SHADOW"
            usage = core.store.one("SELECT * FROM model_usage WHERE job_id=?", (result["job_id"],))
            assert usage["state"] == ("UNKNOWN" if scenario == "unknown" else "SETTLED")
            assert usage["billed_micro_usd"] > 0
            assert core.store.one("SELECT io_started_at FROM model_admissions WHERE request_id=?",
                                  (usage["request_id"],))["io_started_at"]
            preview = await http.get("/internal/v2/life/topics", headers=control)
            assert preview.status_code == 200 and preview.headers["Cache-Control"] == "no-store"
            visible = [r for topic in preview.json()["items"] for r in topic["results"]]
            if scenario == "qzone_revoked":
                assert not visible
            elif interrupted or scenario == "unknown":
                assert not any(row["state"] == "SHADOW" for row in visible)
            else:
                assert visible
            if scenario == "qzone":
                draft = LifeQZoneDraft.model_validate(visible[0]["qzone_draft"])
                assert draft.purpose == "qzone_draft" and draft.audience == "owner_only"
                assert draft.sources[0].event_id == source_id and draft.sources[0].revision == 1
            assert core.store.rows("SELECT * FROM outbox") == baseline_outbox
            assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
            if scenario == "unknown":
                repeated = await process_worker(core, tmp_path, scenario, source_id, restart=True)
                assert repeated["provider_calls"] == 0
                assert core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                                      (usage["request_id"],)) == usage
            if interrupted:
                assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                      (result["job_id"],))["state"] == "CANCELLED"
                status = owner_authority.status(core.store, core.settings, core.clock)
                assert status["state"] == "ACTIVE"
                if next_owner is None:
                    next_owner, _ = await admit_owner(http, core, config, grant, 3, "我们接着聊")
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
                    200, json={"choices": [{"message": {"content": "收到，我们接着聊。"}}],
                               "usage": {"prompt_tokens": 180, "completion_tokens": 60}},
                ))) as foreground:
                    core.model = ModelClient(core.settings, foreground)
                    assert await core.run_once("next-foreground")
                assert core.store.one("SELECT state FROM jobs WHERE event_id=?",
                                      (next_owner["event_id"],))["state"] == "SUCCEEDED"
                new_outbox = [row for row in core.store.rows("SELECT * FROM outbox")
                              if row["message_id"] not in baseline_ids]
                assert new_outbox and all(row["origin_event_id"] == next_owner["event_id"]
                                          for row in new_outbox)
                assert core.store.one("SELECT * FROM model_usage WHERE request_id=?",
                                      (usage["request_id"],)) == usage
    run(exercise())
