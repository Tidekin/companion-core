"""Native paid chat boundaries and retained historical window evidence."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from companion.config import LiveTextWindowApproval
from companion.model import ModelClient
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow, WindowPlan
from tests.conftest import run
from tests.support.failure_notice import assert_failure_notice
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.test_onebot_delivery import core_client
from tests.test_onebot_window import plan_fixture
from tests.test_paid_model_gate import approved_live


@asynccontextmanager
async def native_clients(core, tmp_path, *, respond=None, receipt=None, paid_options=None,
                         bridge_options=None):
    """Real signed native ingress/HTTP and an independent SDK worker connection.

    Only external provider and OneBot transports are synthetic. The existing
    OwnerRuntime, paid journal, SDK hooks and final send checks run unchanged.
    """
    from datetime import timedelta
    from types import SimpleNamespace

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from companion import paid_budget
    from companion.core import Core
    from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
    from companion_onebot_bridge.config import BridgeConfig
    from companion_onebot_bridge.network import private_http_client
    from companion_onebot_bridge.owner_runtime import OwnerRuntime
    from tests.sdk_reader_fixture import answer
    from tests.support.core_http import real_core_http
    from tests.support.owner_authority import activate

    approved_live(core, tmp_path, **(paid_options or {}))
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.control_token = SecretStr("native-window-test-owner-control")
    key = Ed25519PrivateKey.generate()
    cfg = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
        protocol_epoch="native-window-test", capture_signing_key=key.private_bytes_raw().hex(),
        data_dir=str(tmp_path / "native-bridge"), **{
            "onebot_http_url": "http://127.0.0.1:3000",
            "onebot_ws_url": "ws://127.0.0.1:3001", **(bridge_options or {})})
    policy = OwnerGrantPolicy(owner_qq_id=cfg.owner_qq_id, purposes=["chat"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        max_reply_chars=500, expires_at=core.clock.now + timedelta(seconds=600),
        capture={"public_key": cfg.capture_public_key, "protocol_epoch": cfg.protocol_epoch,
                 "identity_sha256": paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id])})
    authority = activate(core, policy)[0]["authority"]
    calls, sends = [], []

    def response(request):
        calls.append(request)
        return respond(request) if respond else httpx.Response(200, json=answer("合成文字回复"))

    async with real_core_http(core) as port, httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", trust_env=False,
        headers={"Authorization": "Bearer " + cfg.bridge_token},
    ) as client, httpx.AsyncClient(transport=httpx.MockTransport(response)) as model_http:
        cfg = replace(cfg, core_url=str(client.base_url).rstrip("/"), owner_authority=authority)
        async with private_http_client(cfg.onebot_http_url, cfg.onebot_token,
            transport=httpx.MockTransport(lambda request: provider(request, sends, receipt)),
        ) as protocol:
            with Core(core.settings, ModelClient(core.settings, model_http), core.clock) as worker:
                assert worker.store is not core.store
                with Spool(cfg) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    yield SimpleNamespace(core=core, worker=worker, runtime=runtime, config=cfg,
                        client=client, protocol=protocol, model_http=model_http, calls=calls,
                        sends=sends, policy=policy, authority=authority, spool=spool)


async def native_admit(ctx, number=1, text="合成主人输入"):
    from companion.store import encode
    from companion_onebot_bridge.event_normalizer import normalize

    raw = encode(raw_event(number, text=text, time=int(ctx.core.clock.now.timestamp())))
    value = normalize(raw, ctx.config, now=ctx.core.clock.now.timestamp())
    assert value is not None
    assert ctx.runtime.persist_capture(value, raw, received_at=ctx.core.clock.now,
                                       peer=("127.0.0.1", 3001))
    assert await ctx.runtime.flush_one(ctx.client)
    return value


async def native_send(ctx, *, protocol=None, runtime=None):
    from companion_onebot_bridge.outbound import deliver_tick
    from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard

    runtime = runtime or ctx.runtime
    guard = OwnerSendGuard(runtime, ctx.client)
    return await deliver_tick(ctx.client, OwnerOneBot(guard, protocol or ctx.protocol),
                              runtime.spool, guard=guard)


async def native_recall(ctx):
    from companion.store import encode
    from companion_onebot_bridge import owner_notices

    frame = encode({"time": int(ctx.core.clock.now.timestamp()),
        "self_id": int(ctx.config.bot_qq_id), "user_id": int(ctx.config.owner_qq_id),
        "post_type": "notice", "notice_type": "friend_recall", "message_id": 1})
    assert owner_notices.put(ctx.runtime, frame, ctx.core.clock.now, ("127.0.0.1", 3001))
    assert await owner_notices.flush_one(ctx.runtime, ctx.client)


async def native_stop(ctx):
    response = await ctx.client.post("/internal/v1/owner-authority/stop",
        headers={"Authorization": "Bearer native-window-test-owner-control"},
        json={"authority": ctx.authority, "operation_id": "stop-native-window-test"})
    assert response.status_code == 200, response.text
    return response.json()


def test_existing_demo_window_still_rejects_live_core(core, tmp_path):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.settings.mode = "live"
    core.clock.now = now

    async def scenario():
        from companion_onebot_bridge.spool import Spool

        with Spool(cfg) as spool:
            window = TestWindow(spool, WindowPlan(value, cfg, inventory), clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as client:
                with pytest.raises(ValueError, match="DEMO_CLOSED_FEATURES_REQUIRED"):
                    await window.open(client)

    run(scenario())


def test_live_text_approval_has_closed_default_and_public_status(core):
    assert core.settings.live_text_window is None
    assert core.status()["live_text_window"] == {"approved": False}


def live_fixture(
    core, tmp_path, *, core_options=None, bridge_options=None, paid_options=None,
    window_options=None, **changes,
):
    from companion import live_text, paid_budget
    from companion_onebot_bridge.window import bridge_code_sha256, digest

    cfg, value, inventory, now = plan_fixture(tmp_path, **(window_options or {}))
    if bridge_options:
        cfg = replace(cfg, **bridge_options)
        inventory["config_sha256"] = digest(cfg.snapshot())
        value.update(config_sha256=inventory["config_sha256"],
                     runtime_attestation_sha256=digest(inventory))
    core.clock.now = now
    core.settings.bridge_token = SecretStr(cfg.bridge_token)
    for key, val in {"max_recent_messages": 0, **(core_options or {})}.items():
        setattr(core.settings, key, val)
    approval = approved_live(core, tmp_path, **{
        "window_id": value["window_id"], "concurrency": 1, "per_request_calls": 1,
        "window_calls": 5, **(paid_options or {}),
    })
    with core.store.transaction():
        paid_budget.journal(core.store, approval)
    scope = LiveTextWindowApproval(
        live_approved=True, plan_sha256="0" * 64, window_id=value["window_id"],
        capture_public_key=cfg.capture_public_key,
        starts_at=value["starts_at"], ends_at=value["ends_at"],
        core_config_sha256=live_text.config_sha256(core.settings),
        core_code_sha256=live_text.code_sha256(), bridge_code_sha256=bridge_code_sha256(),
        candidate_sha="b" * 40,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        budget_journal_id=core.store.one(
            "SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'"
        )["value"], custodian_sha256="c" * 64, approval_sha256="d" * 64, **changes,
    ).model_dump(mode="json", exclude={"plan_sha256"})
    value.update(mode="live_text", live_text=scope)
    core.settings.live_text_window = LiveTextWindowApproval(**scope, plan_sha256=digest(value))
    return cfg, WindowPlan(value, cfg, inventory)


def test_native_five_paid_replies_keep_original_cost_and_send_receipts(core, tmp_path):
    async def scenario():
        async with native_clients(core, tmp_path, paid_options={"window_calls": 5}) as ctx:
            for number in range(1, 6):
                await native_admit(ctx, number)
                assert await ctx.worker.run_once()
                assert await native_send(ctx)
                assert ctx.spool.db.execute(
                    "SELECT count(*) FROM window_attempts WHERE state='ACK'"
                ).fetchone()[0] == number
            assert len(ctx.calls) == len(ctx.sends) == 5
            assert all(item["message"][0]["data"]["text"] == "合成文字回复" for item in ctx.sends)
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
            assert len(usage) == 5 and all(
                row["state"] == "SETTLED" and row["billed_micro_usd"] > 0 for row in usage)
    run(scenario())


def test_core_and_bridge_allow_fifteen_model_attempts_without_raising_input_or_reply_caps(
    core, tmp_path,
):
    assert LiveTextWindowApproval.model_fields["max_model_attempts"].default == 5
    cfg, plan = live_fixture(core, tmp_path, max_model_attempts=15,
                             paid_options={"window_calls": 15})
    assert core.settings.live_text_window.max_model_attempts == 15
    assert plan.value["live_text"]["max_model_attempts"] == 15
    assert core.settings.live_text_window.max_inputs == 5
    assert all(plan.value[key] == 5 for key in (
        "max_new_inputs", "max_attempts", "max_visible_replies",
    ))

    value = core.settings.live_text_window.model_dump()
    with pytest.raises(ValidationError):
        LiveTextWindowApproval.model_validate({**value, "max_model_attempts": 2**31})
    _, _, inventory, _ = plan_fixture(tmp_path)
    invalid = {**plan.value, "live_text": {
        **plan.value["live_text"], "max_model_attempts": 2**31,
    }}
    with pytest.raises(ValueError, match="LIVE_TEXT_SCOPE_MISMATCH"):
        WindowPlan(invalid, cfg, inventory)


@asynccontextmanager
async def live_clients(core, cfg, *, respond=None, receipt=None):
    model_calls, sends = [], []

    def model_reply(request):
        model_calls.append(request)
        if respond:
            return respond(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "合成文字回复"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async with (
        core_client(core) as client,
        httpx.AsyncClient(transport=httpx.MockTransport(model_reply)) as model_http,
        httpx.AsyncClient(
            base_url=cfg.onebot_http_url,
            headers={"Authorization": "Bearer " + cfg.onebot_token},
            transport=httpx.MockTransport(lambda r: provider(r, sends, receipt)),
        ) as protocol,
    ):
        client.base_url = cfg.core_url
        core.model = ModelClient(core.settings, model_http)
        yield client, protocol, model_calls, sends


def test_native_outbox_body_cannot_be_replaced_with_a_new_content_hash(core, tmp_path):
    from companion.store import encode
    from companion.transport import digest

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            body = {"text": "替换后的正文"}
            core.store.execute("UPDATE outbox SET content_json=?,content_sha256=?",
                               (encode(body), digest(body)))
            assert not await native_send(ctx)
            assert len(ctx.calls) == 1 and ctx.sends == []
            assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())


def test_live_text_cannot_send_historical_context_without_its_own_approval(core, tmp_path):
    cfg, plan = live_fixture(core, tmp_path, core_options={"max_recent_messages": 12})

    async def scenario():
        async with live_clients(core, cfg) as (client, _, model_calls, sends):
            with Spool(cfg) as spool:
                with pytest.raises(httpx.HTTPStatusError):
                    await TestWindow(spool, plan, clock=lambda: core.clock.now).open(client)
            assert model_calls == sends == []

    run(scenario())


def test_confirmed_core_stop_prevents_already_queued_model_attempt(core, tmp_path):
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            await native_stop(ctx)
            await ctx.worker.run_once()
            assert ctx.calls == ctx.sends == []
            assert not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM model_usage")
    run(scenario())


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.77.9"])
def test_real_dns_pin_hook_accepts_original_hostname_authority(
    core, tmp_path, monkeypatch, address,
):
    from companion_onebot_bridge import network
    destinations = []
    async def resolve(host, port):
        assert (host, port) == ("localhost", 3000)
        return address
    monkeypatch.setattr(network, "resolve_private", resolve)
    async def scenario():
        async with native_clients(core, tmp_path, bridge_options={
            "onebot_http_url": "http://localhost:3000",
            "onebot_ws_url": "ws://localhost:3001"}) as ctx:
            def response(request):
                destinations.append((request.url.host, request.headers["Host"],
                                     request.extensions["sni_hostname"]))
                assert network.private_request_matches(request, ctx.config.onebot_http_url,
                                                       request.url.path)
                assert network.private_request_matches(request, ctx.config.onebot_http_url,
                    request.url.path, synthetic=True) == (address == "127.0.0.1")
                return provider(request, ctx.sends)
            ctx.protocol._transport = httpx.MockTransport(response)
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            assert await native_send(ctx)
            assert ctx.spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == "ACK"
            assert len(ctx.calls) == len(ctx.sends) == 1
    run(scenario())
    assert destinations and all(value == (address, "localhost:3000", "localhost")
                                for value in destinations)


@pytest.mark.parametrize("change", ["body", "header", "deadline", "privacy", "key", "endpoint"])
def test_model_http_request_drift_is_rejected_after_async_hooks(core, tmp_path, change):
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            async def mutate(request):
                if change == "body":
                    request._content = b'{"messages":[],"model":"different"}'
                    request.stream = httpx.ByteStream(request.content)
                elif change == "header":
                    request.headers["Authorization"] = "Bearer different-synthetic-key"
                elif change == "deadline":
                    core.clock.now = ctx.policy.expires_at
                elif change == "privacy":
                    await native_recall(ctx)
                elif change == "key":
                    core.settings.model_key = SecretStr("rotated-synthetic-key")
                else:
                    request.url = request.url.copy_with(host="different.invalid")
            ctx.model_http.event_hooks["request"].append(mutate)
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            assert ctx.calls == ctx.sends == []
            usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
            # The checked transport proves that no provider request was dispatched.
            assert usage == {"state": "SETTLED", "billed_micro_usd": 0}
            if change in {"body", "header", "endpoint"}:
                assert_failure_notice(core)
            else:
                assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())


@pytest.mark.parametrize("raw_change", [
    {"text": "/当前交互状态"}, {"text": " \n/未知"},
    {"user_id": 10003, "sender": {"user_id": 10003}},
    {"self_id": 10003}, {"message_type": "group", "group_id": 10003},
    {"message": [{"type": "image", "data": {"file": "synthetic"}}]},
    {"message": [{"type": "record", "data": {"file": "synthetic"}}]},
])
def test_wrong_identity_nontext_and_slash_never_create_model_or_delivery(
    core, tmp_path, raw_change,
):
    cfg, plan = live_fixture(core, tmp_path)

    async def scenario():
        async with live_clients(core, cfg) as (client, _, model_calls, sends):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client)
                with pytest.raises(ValueError):
                    await window.ingest(client, raw_event(1, **raw_change), {})
                assert window.status()["new_inputs"] == 0
                assert window.status()["state"] == "STOPPED"
            assert model_calls == sends == []
            assert core.store.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0

    run(scenario())


@pytest.mark.parametrize("approval", [None, False])
def test_core_requires_its_own_explicit_live_approval(core, tmp_path, approval):
    cfg, plan = live_fixture(core, tmp_path)
    core.settings.live_text_window = (
        None if approval is None else core.settings.live_text_window.model_copy(
            update={"live_approved": False}
        )
    )

    async def scenario():
        async with live_clients(core, cfg) as (client, _, model_calls, sends):
            with Spool(cfg) as spool:
                with pytest.raises(httpx.HTTPStatusError):
                    await TestWindow(spool, plan, clock=lambda: core.clock.now).open(client)
            assert model_calls == sends == []

    run(scenario())


def test_live_input_cannot_bypass_individual_approval_through_generic_inbox(core, tmp_path):
    from tests.support.onebot_events import normalized

    cfg, plan = live_fixture(core, tmp_path)

    async def scenario():
        async with live_clients(core, cfg) as (client, _, model_calls, sends):
            with Spool(cfg) as spool:
                await TestWindow(spool, plan, clock=lambda: core.clock.now).open(client)
                event = normalized(raw_event(1), cfg)
                response = await client.post("/internal/v1/inbox/events", json=event,
                                             headers={"X-Idempotency-Key": event["event_id"]})
                assert response.status_code == 403
                assert response.json()["error"] == "OWNER_AUTHORITY_DISABLED"
                assert model_calls == sends == []
                assert core.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 0

    run(scenario())


@pytest.mark.parametrize("result,expected", [
    ("timeout", "UNKNOWN"), ("missing_usage", "UNKNOWN"), ("rate_change", "UNKNOWN"),
    ("deadline", "SETTLED"), ("privacy", "SETTLED"), ("revocation", "SETTLED"),
    ("oversize", "SETTLED"),
])
def test_model_late_or_unknown_results_retain_costs_and_never_enqueue(
    core, tmp_path, result, expected,
):
    from companion import owner_authority
    from companion.owner_authority_protocol import AuthorityStop
    from tests.sdk_reader_fixture import answer

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            async def respond(_request):
                ctx.calls.append(_request)
                if result == "timeout":
                    raise httpx.ReadTimeout("synthetic lost reply")
                if result == "deadline":
                    core.clock.now = ctx.policy.expires_at
                elif result == "privacy":
                    await native_recall(ctx)
                elif result == "revocation":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-during-native-model", authority=ctx.authority))
                elif result == "rate_change":
                    core.settings.input_rate += 1
                body = answer("X" * 501 if result == "oversize" else "合成迟到回复")
                if result == "missing_usage":
                    body.pop("usage")
                return httpx.Response(200, json=body)
            ctx.model_http._transport = httpx.MockTransport(respond)
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            usage = core.store.one("SELECT * FROM model_usage")
            assert usage["state"] == expected and usage["billed_micro_usd"] > 0
            if result == "oversize":
                assert_failure_notice(core)
            else:
                assert not core.store.rows("SELECT * FROM outbox")
            assert len(ctx.calls) == 1 and ctx.sends == []
            core.clock.advance(10)
            await ctx.worker.run_once()
            assert len(ctx.calls) == 1
            assert core.store.one("SELECT * FROM model_usage") == usage
    run(scenario())


@pytest.mark.parametrize("drift", ["deadline", "privacy", "revocation", "lease", "journal"])
def test_after_network_wait_send_revalidates_core_and_keeps_spent_reservation(
    core, tmp_path, drift,
):
    from companion import owner_authority
    from companion.owner_authority_protocol import AuthorityStop
    from companion_onebot_bridge.owner_runtime import OwnerRuntime

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            async def network_wait(request):
                if request.url.path != "/send_private_msg":
                    return
                await asyncio.sleep(0)
                if drift == "deadline":
                    core.clock.now = ctx.policy.expires_at
                elif drift == "privacy":
                    await native_recall(ctx)
                elif drift == "revocation":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        authority=ctx.authority, operation_id="stop-during-send-hook"))
                elif drift == "lease":
                    core.clock.advance(core.settings.lease_seconds + 1)
                else:
                    from pathlib import Path
                    await asyncio.to_thread(
                        Path(core.settings.paid_model.journal_path).write_text, "{}\n", "utf-8")
            ctx.protocol.event_hooks["request"].append(network_wait)
            assert await native_send(ctx)
            assert len(ctx.calls) == 1 and ctx.sends == []
            # Source recall supplies original pre-send cancellation proof. Expired
            # authority/lease or broken journal cannot confirm cancellation.
            expected = "NOT_SENT" if drift == "privacy" else "UNKNOWN"
            assert ctx.spool.db.execute(
                "SELECT state FROM window_attempts").fetchone()[0] == expected
            assert core.store.one("SELECT state FROM outbox")["state"] == (
                "CANCELLED" if drift == "privacy" else "DELIVERY_UNCERTAIN")
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            cfg = ctx.config
        with Spool(cfg) as spool:
            restarted = OwnerRuntime(spool, clock=lambda: core.clock.now)
            assert spool.db.execute("SELECT count(*) FROM window_attempts").fetchone()[0] == 1
            if drift != "privacy":
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    restarted.verify()
    run(scenario())


def test_onebot_ack_loss_is_one_external_attempt_across_restart(core, tmp_path):
    from companion_onebot_bridge.owner_runtime import OwnerRuntime

    async def scenario():
        async with native_clients(core, tmp_path,
                                  receipt=httpx.ReadTimeout("synthetic ACK lost")) as ctx:
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            assert await native_send(ctx)
            assert ctx.spool.db.execute(
                "SELECT state FROM window_attempts").fetchone()[0] == "UNKNOWN"
            assert len(ctx.calls) == len(ctx.sends) == 1
            assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
            cfg = ctx.config
        with Spool(cfg) as spool:
            restarted = OwnerRuntime(spool, clock=lambda: core.clock.now)
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                restarted.verify()
            assert spool.db.execute("SELECT count(*) FROM window_attempts").fetchone()[0] == 1
            assert spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == "UNKNOWN"
        assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())


def test_sqlite_lock_wait_cannot_authorize_send_after_deadline(core, tmp_path):
    import sqlite3
    import threading
    import time

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            locked = threading.Event()
            def hold():
                connection = sqlite3.connect(core.settings.database)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    locked.set()
                    time.sleep(0.1)
                    core.clock.now = ctx.policy.expires_at
                    connection.commit()
                finally:
                    connection.close()
            thread = threading.Thread(target=hold)
            async def lock_before_dispatch(request):
                if request.url.path == "/send_private_msg":
                    thread.start()
                    assert locked.wait(3)
            ctx.protocol.event_hooks["request"].append(lock_before_dispatch)
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            try:
                assert await native_send(ctx)
                assert ctx.sends == []
                assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
                assert ctx.spool.db.execute(
                    "SELECT state FROM window_attempts").fetchone()[0] == "UNKNOWN"
                assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
            finally:
                thread.join(3)
                assert not thread.is_alive()
    run(scenario())


@pytest.mark.parametrize("limits", [
    {"per_attempt_micro_usd": 1}, {"daily_micro_usd": 1}, {"monthly_micro_usd": 1},
    {"window_micro_usd": 1}, {"max_input_tokens": 1},
])
def test_existing_paid_approval_and_amount_limits_are_not_relaxed(core, tmp_path, limits):
    async def scenario():
        async with native_clients(core, tmp_path, paid_options=limits) as ctx:
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            assert ctx.calls == ctx.sends == []
            assert not core.store.rows("SELECT * FROM model_usage")
            assert_failure_notice(core)
            assert ctx.spool.db.execute("SELECT count(*) FROM window_attempts").fetchone()[0] == 0
    run(scenario())


def test_existing_global_reservation_blocks_native_model_without_new_io(core, tmp_path):
    from companion import budget
    async def scenario():
        async with native_clients(core, tmp_path, paid_options={"concurrency": 1}) as ctx:
            first = await native_admit(ctx)
            first_job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?",
                                       (first["event_id"],))
            job = core.claim_job("held-original-reservation", job_id=first_job["job_id"])
            request_id = budget.reserve(core.store, core.settings, job["job_id"], "chat",
                core.context(job), core.clock(), clock=core.clock)
            await native_admit(ctx, 2)
            await ctx.worker.run_once()
            assert ctx.calls == ctx.sends == []
            assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                                  (request_id,))["state"] == "RESERVED"
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1
            assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())


def test_cancel_after_native_journal_io_started_never_replays_after_restart(core, tmp_path):
    from companion_onebot_bridge.owner_runtime import OwnerRuntime

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            def cancelled(request):
                if request.url.path == "/send_private_msg":
                    raise asyncio.CancelledError()
                return provider(request, ctx.sends)
            # Cancellation happens at the external transport, after the real
            # send-check and durable IO_STARTED, without inventing a receipt.
            ctx.protocol._transport = httpx.MockTransport(cancelled)
            with pytest.raises(asyncio.CancelledError):
                await native_send(ctx)
            assert ctx.spool.db.execute(
                "SELECT state FROM window_attempts").fetchone()[0] == "IO_STARTED"
            assert len(ctx.calls) == 1 and ctx.sends == []
            assert core.store.one("SELECT state FROM outbox")["state"] == "SENDING"
            cfg = ctx.config
        with Spool(cfg) as spool:
            restarted = OwnerRuntime(spool, clock=lambda: core.clock.now)
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                restarted.verify()
            assert spool.db.execute("SELECT count(*) FROM window_attempts").fetchone()[0] == 1
        assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())


@pytest.mark.parametrize("stage_name", ["queued", "model_preflight"])
def test_owner_stop_entrypoint_fences_core_before_new_model_io(core, tmp_path, stage_name):
    confirmed = []
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            async def stop(_request=None):
                confirmed.append(await native_stop(ctx))
            if stage_name == "queued":
                await stop()
            else:
                ctx.model_http.event_hooks["request"].append(stop)
            await ctx.worker.run_once()
            assert ctx.calls == ctx.sends == [] and len(confirmed) == 1
            assert not core.store.rows("SELECT * FROM outbox")
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
            assert usage == ([{"state": "SETTLED", "billed_micro_usd": 0}]
                             if stage_name == "model_preflight" else [])
    run(scenario())


def test_core_stop_survives_old_db_without_its_native_projection(core, tmp_path):
    import sqlite3
    from contextlib import closing

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            with closing(sqlite3.connect(tmp_path / "before-stop.db")) as old_db:
                core.store.db.backup(old_db)
                await native_stop(ctx)
                old_db.backup(core.store.db)
            await ctx.worker.run_once()
            assert ctx.calls == ctx.sends == []
            assert not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM model_usage")
            response = await ctx.client.post("/internal/v1/owner-authority/check",
                json={"authority": ctx.authority, "purpose": "chat"})
            assert response.status_code != 200
    run(scenario())


def test_native_custom_transport_cannot_bypass_current_owner_authority(core, tmp_path):
    dispatched = []
    class UnprovedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            dispatched.append(request)
            raise AssertionError("unproved external transport was reached")
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            ctx.model_http._transport = UnprovedTransport()
            async def revoke_before_dispatch(_request):
                await native_stop(ctx)
            ctx.model_http.event_hooks["request"].append(revoke_before_dispatch)
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            assert dispatched == ctx.sends == []
            assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())


def test_cli_persists_explicit_unconfirmed_core_stop(core, tmp_path, monkeypatch, capsys):
    import json
    import sys

    from companion_onebot_bridge import app
    from companion_onebot_bridge.config import BridgeConfig
    from companion_onebot_bridge.owner_runtime import OwnerRuntime

    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            return ctx.config
    cfg = run(scenario())
    monkeypatch.setattr(BridgeConfig, "load", classmethod(lambda cls, path: cfg))
    monkeypatch.setattr(sys, "argv", ["bridge", "owner-stop"])
    app.main()
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["state"] == "STOPPED" and result["core_stop_confirmed"] is False
    assert result["authority"] == cfg.owner_authority
    with Spool(cfg) as spool:
        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
            OwnerRuntime(spool, clock=lambda: core.clock.now).verify()
    assert not core.store.rows("SELECT * FROM model_usage")


def test_synthetic_core_dns_pin_cannot_dispatch_to_nonloopback(core, tmp_path, monkeypatch):
    from companion_onebot_bridge import network
    from companion_onebot_bridge.onebot_client import http_client

    cfg, plan = live_fixture(core, tmp_path, bridge_options={"core_url": "http://localhost:8765"})
    dispatched = []

    async def resolve(_host, _port):
        return "192.168.77.9"

    def response(request):
        dispatched.append(request)
        return httpx.Response(200, json={"approved": False})

    monkeypatch.setattr(network, "resolve_private", resolve)
    monkeypatch.setattr(network, "http_client", lambda url, token: http_client(
        url, token, transport=httpx.MockTransport(response)
    ))

    async def scenario():
        async with network.private_http_client(cfg.core_url, cfg.bridge_token) as client:
            with Spool(cfg) as spool:
                with pytest.raises(ValueError):
                    await TestWindow(spool, plan, clock=lambda: core.clock.now).open(client)
                assert dispatched == []

    run(scenario())


def test_unfinished_native_send_keeps_custody_when_new_input_arrives(core, tmp_path):
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx)
            assert await ctx.worker.run_once()
            response = await ctx.client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "synthetic-interrupted-worker", "limit": 1,
                "authority": ctx.authority})
            assert response.status_code == 200, response.text
            item = response.json()["items"][0]
            response = await ctx.client.post(
                f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                json={"lease_token": item["lease_token"]})
            assert response.status_code == 200, response.text
            await native_admit(ctx, 2)
            # Native ingress may accept the newer owner turn; it cannot erase or
            # replay a prior send whose external outcome is still unresolved.
            assert core.store.one("SELECT state FROM outbox WHERE message_id=?",
                                  (item["message_id"],))["state"] == "SENDING"
            assert not await native_send(ctx)
            assert len(ctx.calls) == 1 and ctx.sends == []
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())


@pytest.mark.parametrize("boundary", ["window", "paid", "job_lease"])
def test_model_dispatch_rechecks_time_after_last_blocking_journal_read(
    core, tmp_path, monkeypatch, boundary,
):
    from datetime import datetime, timedelta

    from companion import paid_budget

    real_journal = paid_budget.journal
    preflight_started, advanced = [], []
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            def slow_journal(*args, **kwargs):
                value = real_journal(*args, **kwargs)
                record = args[2] if len(args) > 2 else kwargs.get("record")
                if preflight_started and not advanced and record is None:
                    if boundary == "window":
                        core.clock.now = ctx.policy.expires_at
                    elif boundary == "paid":
                        core.settings.paid_model = core.settings.paid_model.model_copy(
                            update={"expires_at": core.clock.now})
                    else:
                        job = core.store.one("SELECT lease_until FROM jobs WHERE state='RUNNING'")
                        core.clock.now = (datetime.fromisoformat(job["lease_until"])
                                          + timedelta(seconds=1))
                    advanced.append(True)
                return value
            async def start_final_preflight(_request):
                preflight_started.append(True)
            ctx.model_http.event_hooks["request"].append(start_final_preflight)
            await native_admit(ctx)
            monkeypatch.setattr(paid_budget, "journal", slow_journal)
            assert await ctx.worker.run_once()
            assert preflight_started and advanced
            assert ctx.calls == ctx.sends == []
            if boundary == "job_lease":
                assert_failure_notice(core)
            else:
                assert not core.store.rows("SELECT * FROM outbox")
            assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
                "state": "SETTLED", "billed_micro_usd": 0}
    run(scenario())
