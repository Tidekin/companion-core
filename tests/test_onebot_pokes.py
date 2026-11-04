"""Native owner poke intake and delivery; only provider HTTP is synthetic."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from companion import cumulative, paid_budget
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.poke_protocol import PokePolicy, canonical_notice, canonical_source
from companion_onebot_bridge import continuous, owner_pokes, pokes
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.native_actions import content_hash
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_send import OwnerSendGuard
from tests.conftest import run
from tests.native_continuous_support import Host
from tests.poke_authority_fixture import environment as native_environment
from tests.support.core_http import real_core_http
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.test_cumulative_budget import policy as quota_policy
from tests.test_cumulative_budget import view
from tests.test_onebot_capture import source_window
from tests.test_poke import native_frame
from tests.test_private_companion import companion_options
from tests.test_social_owner_authority import stop


@asynccontextmanager
async def poke_host(core, tmp_path, *, decision="poke", protocol_reply=None, quota=None):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    env = native_environment(core, tmp_path)
    core.settings.poke_policy = PokePolicy(enabled=True, allow_native=True, coalesce_seconds=0)
    if quota is not None:
        quota_policy(core, account=quota)
        with core.store.transaction():
            cumulative.activate(core.store, core.settings, core.clock())
    grant = OwnerGrantPolicy.model_validate({**env.approval.policy.model_dump(),
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        "capability_policies": {"poke": paid_budget.sha(core.settings.poke_policy.model_dump())}})
    authority = activate(core, grant, revision=env.approval.authority.revision,
                         operation="native-poke-fixture")[0]["authority"]
    cfg = replace(env.config, owner_authority=authority,
        capture_signing_key=env.key.private_bytes_raw().hex(), data_dir=str(tmp_path / "bridge"),
        onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001")
    sends, calls = [], []

    async def native(request):
        result = protocol_reply(request) if protocol_reply else None
        if hasattr(result, "__await__"):
            result = await result
        return result if result is not None else provider(request, sends)

    def model(request):
        calls.append(request)
        choice = {"kind": decision, **({"text": "怎么啦"} if decision == "text" else {})}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(choice)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async with real_core_http(core) as port:
        cfg = replace(cfg, core_url=f"http://127.0.0.1:{port}")
        async with private_http_client(cfg.core_url, cfg.bridge_token) as client, (
            private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                                transport=httpx.MockTransport(native))
        ) as protocol, httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
            original = core.model
            core.model = ModelClient(core.settings, model_http)
            active = Host(core, cfg, client, protocol)
            try:
                yield active, sends, calls
            finally:
                active.spool.close()
                core.model = original


async def capture_poke(active, *, flush=True):
    raw = json.dumps(native_frame(int(active.core.clock.now.timestamp())))
    assert await owner_pokes.put(active.runtime, active.client, raw, active.core.clock.now,
                                 ("127.0.0.1", 9000))
    receipt = json.loads(active.db.execute(
        "SELECT capture_json FROM window_inputs ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    if flush:
        assert await active.runtime.flush_one(active.client)
    return receipt


def capture_owner(active):
    raw = json.dumps(raw_event(2, text="等等，先说这件事",
                              time=int(active.core.clock.now.timestamp())))
    event = normalize(raw, active.config, now=active.core.clock.now.timestamp())
    active.runtime.persist_capture(event, raw, received_at=active.core.clock.now,
                                   peer=("127.0.0.1", 9000))
    return event["event_id"]



def options(core):
    value = companion_options(core)
    policy = PokePolicy(enabled=True, allow_native=True, coalesce_seconds=0)
    value.update(poke_scope="owner_poke_v1",
                 poke_policy_sha256=paid_budget.sha(policy.model_dump()))
    value["core_options"]["poke_policy"] = policy
    return value


async def admit(window, client):
    source, observed = queue(window)
    assert isinstance(observed, pokes.ObservedPoke)
    await continuous.capture_observed(window, client, None, observed)
    source.consume(observed)
    return observed



async def silent(ws):
    await ws.wait_closed()


def queue(window):
    source = continuous.OwnerSource(window, None, asyncio.Event())
    frame = native_frame(int(window.clock().timestamp()))
    pokes.put(source, json.dumps(frame), window.clock(), ("127.0.0.1", 9000))
    return source, source.first()


def test_native_notice_canonicalization_agrees_with_http_contract():
    from tests.support.onebot_events import config

    frame = native_frame()
    value = pokes.notice(frame, config())
    assert value == canonical_notice(frame, owner_qq_id="10001", bot_qq_id="10002")
    expected = canonical_source(value)
    assert pokes.source(value, config()) == {key: expected[key] for key in (
        "source_event_id", "event_id")}
    assert pokes.notice(frame | {"action_img_url": "http://127.0.0.1/private"}, config()) == value


def test_poke_intake_is_closed_without_explicit_window_scope(core, tmp_path):
    async def scenario():
        async with source_window(core, tmp_path, silent, live=True, approved=True,
                                 scope_options=companion_options(core)) as (window, _, _, _, _):
            source, observed = queue(window)
            assert observed is None and source.first() is None
            assert not window.db.execute("SELECT 1 FROM automatic_received").fetchone()
            assert not core.store.one("SELECT 1 FROM events")

    run(scenario())


@pytest.mark.parametrize("change", [
    {"group_id": 90001}, {"sender_id": 10003}, {"user_id": 10003}, {"target_id": 10001},
    {"self_id": 10003}, {"sender_id": True}, {"time": True}, {"action": "x" * 81},
    {"suffix": "private\nline"}, {"target_id": None},
])
def test_unsupported_notices_cannot_become_owner_pokes(change):
    from tests.support.onebot_events import config

    with pytest.raises(ValueError, match="PRIVATE_OWNER_POKE_REQUIRED"):
        pokes.notice(native_frame() | change, config())


def test_signed_poke_tampering_cannot_be_recovered_as_an_authenticated_notice(core, tmp_path):
    async def scenario():
        async with poke_host(core, tmp_path) as (active, _, _):
            receipt = await capture_poke(active, flush=False)
            for value in ({"signature": "0" * 128},
                          {"authority": receipt["authority"] | {"revision": 99}},
                          {"source_event_id": receipt["source_event_id"] + "-changed"},
                          {"notice": receipt["notice"] | {"suffix": "changed"}}):
                response = await active.client.post("/internal/v1/pokes", json=receipt | value)
                assert response.status_code in {403, 409}, response.text
                assert not core.store.rows("SELECT * FROM events")
            active.runtime.verify()
    run(scenario())


def test_poke_uses_existing_cumulative_input_ledger_and_duplicate_does_not_spend_again(
    core, tmp_path,
):
    async def scenario():
        async with poke_host(core, tmp_path, quota={"owner_inputs": 1, "send_attempts": 1}) as (
            active, sends, _,
        ):
            receipt = await capture_poke(active)
            assert len(view(core)["operations"]) == 1
            assert await owner_pokes.put(active.runtime, active.client,
                json.dumps(native_frame(int(core.clock.now.timestamp()))), core.clock.now,
                ("127.0.0.1", 9000))
            assert not await active.runtime.flush_one(active.client)
            repeated = await active.client.post("/internal/v1/pokes", json=receipt)
            assert repeated.status_code == 200 and repeated.json()["duplicate"]
            assert len(view(core)["operations"]) == 1
            core.clock.advance(1)
            await capture_poke(active, flush=False)

            async def read_error(response):
                if response.status_code == 429:
                    await response.aread()

            active.client.event_hooks["response"].append(read_error)
            with pytest.raises(httpx.HTTPStatusError) as error:
                await active.runtime.flush_one(active.client)
            assert error.value.response.json()["error"] == "CUMULATIVE_LIMIT_EXHAUSTED"
            assert len(view(core)["operations"]) == 1 and sends == []
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 1
            assert active.db.execute(
                "SELECT COUNT(*) FROM inbox WHERE state='accepted'").fetchone()[0] == 1
    run(scenario())


def test_poke_buffer_deduplicates_without_interrupting_owner_turn(core, tmp_path):
    async def scenario():
        async with poke_host(core, tmp_path) as (active, _, _):
            receipt = await capture_poke(active, flush=False)
            assert await capture_poke(active, flush=False) == receipt
            assert active.captures() == 1
            guard = OwnerSendGuard(active.runtime, active.client)
            guard.current_item = {"delivery_key": "poke-response:synthetic",
                                  "authority": active.runtime.authority,
                                  "origin_event_id": receipt["event_id"]}
            assert guard.pending_input() is None
            owner_id = capture_owner(active)
            assert guard.pending_input() == owner_id
            assert await active.runtime.flush_one(active.client)
            assert active.db.execute("SELECT state FROM inbox WHERE key=?",
                                     (receipt["event_id"],)).fetchone()[0] == "accepted"
            assert core.store.one("SELECT event_type FROM events")["event_type"] == "notice.poke"
            assert core.store.one("SELECT reply_revision FROM conversations")["reply_revision"] == 0
            active.runtime.verify()
    run(scenario())


def test_lost_poke_ack_recovers_after_stop_without_new_job(core, tmp_path):
    async def scenario():
        async with poke_host(core, tmp_path) as (active, _, _):
            receipt = await capture_poke(active, flush=False)

            async def lost(response):
                if response.request.url.path == "/internal/v1/pokes":
                    raise httpx.ReadError("lost synthetic ACK")

            active.client.event_hooks["response"].append(lost)
            with pytest.raises(httpx.ReadError):
                await active.runtime.flush_one(active.client)
            assert active.db.execute("SELECT state FROM inbox").fetchone()[0] == "pending"
            active.client.event_hooks["response"].remove(lost)
            stop(core, active.runtime.authority)
            active.runtime.stop()
            jobs = core.store.rows("SELECT * FROM jobs")
            # Recovery admits no work and uses the original receipt after STOP.
            await owner_pokes.flush(active.runtime, active.client, receipt)
            assert core.store.rows("SELECT * FROM jobs") == jobs
            assert active.db.execute("SELECT state FROM inbox").fetchone()[0] == "accepted"
            active.runtime.verify(active=False)
    run(scenario())


@pytest.mark.parametrize("decision", ["poke", "text", "none"])
def test_poke_model_result_dispatch_uses_real_receipt(core, tmp_path, decision):
    native = []

    def protocol_reply(request):
        if request.url.path == "/friend_poke":
            native.append(request)
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})

    async def scenario():
        async with poke_host(core, tmp_path, decision=decision, protocol_reply=protocol_reply) as (
            active, sends, requests,
        ):
            receipt = await capture_poke(active)
            assert await core.run_once("poke-synthetic")
            assert len(requests) == 1
            usage = core.store.rows("SELECT * FROM model_usage")
            assert await active.pump() == (decision != "none")
            assert len(native) == int(decision == "poke")
            assert len(sends) == int(decision == "text")
            if decision == "poke":
                result = core.store.one("SELECT * FROM outbox")
                assert result["state"] == "SENT" and result["provider_message_id"] is None
                assert json.loads(result["native_ack_json"])["response_sha256"] == content_hash(
                    {"status": "ok", "retcode": 0, "data": None})
            assert active.db.execute("SELECT state FROM inbox").fetchone()[0] == "accepted"
            assert core.store.one("SELECT reply_revision FROM events WHERE event_id=?",
                                  (receipt["event_id"],))["reply_revision"] == 0
            assert core.store.rows("SELECT * FROM model_usage") == usage
            active.runtime.verify()
    run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "bad_ack"])
def test_dispatched_poke_without_positive_ack_stays_unknown_and_is_not_retried(
    core, tmp_path, failure,
):
    native = []

    def protocol_reply(request):
        if request.url.path == "/friend_poke":
            native.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic lost native ack")
            return httpx.Response(200, json={"status": "failed", "retcode": 1, "data": None})

    async def scenario():
        async with poke_host(core, tmp_path, protocol_reply=protocol_reply) as (active, sends, _):
            await capture_poke(active)
            assert await core.run_once("poke-synthetic")
            usage = core.store.rows("SELECT * FROM model_usage")
            assert await active.pump()
            assert len(native) == 1 and sends == []
            assert active.runtime.row()["state"] == "STOPPED"
            assert active.attempts() == {"UNKNOWN": 1}
            assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
            active.restart()
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                await active.pump()
            assert len(native) == 1
            result = json.loads(active.db.execute("SELECT body FROM results").fetchone()[0])
            assert "native_ack" not in result and "provider_message_id" not in result
            assert core.store.rows("SELECT * FROM model_usage") == usage
            active.runtime.verify(active=False)
    run(scenario())


@pytest.mark.parametrize("decision", ["poke", "text"])
@pytest.mark.parametrize("phase", ["request", "core_approved"])
def test_new_owner_input_at_final_native_boundary_has_core_not_sent_proof(
    core, tmp_path, decision, phase,
):
    attempted = []

    def protocol_reply(request):
        if request.url.path == "/friend_poke":
            attempted.append(request)
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})

    async def scenario():
        async with poke_host(core, tmp_path, decision=decision, protocol_reply=protocol_reply) as (
            active, sends, _,
        ):
            await capture_poke(active)
            assert await core.run_once("poke-synthetic")
            usage = core.store.rows("SELECT * FROM model_usage")

            async def before(request):
                if phase == "request" and request.url.path in {"/friend_poke", "/send_private_msg"}:
                    capture_owner(active)

            async def approved(response):
                if phase == "core_approved" and response.request.url.path.endswith("/send-check"):
                    capture_owner(active)

            active.protocol.event_hooks["request"].append(before)
            active.client.event_hooks["response"].append(approved)
            assert await active.pump()
            assert attempted == sends == []
            assert active.runtime.row()["state"] == "ACTIVE"
            assert active.attempts() == {"NOT_SENT": 1}
            assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
            result = json.loads(active.db.execute("SELECT body FROM results").fetchone()[0])
            assert result["result"] == "NOT_SENT" and result["core_receipt"]["cancelled"] is True
            assert core.store.rows("SELECT * FROM model_usage") == usage
            active.runtime.verify()
    run(scenario())
