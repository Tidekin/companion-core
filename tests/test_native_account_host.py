"""Native account host uses real Core admission and provider-only synthetic transport."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime

import httpx
import pytest

from companion import paid_budget
from companion.api import create_app
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.support.onebot_events import config
from tests.support.owner_authority import activate
from tests.test_social_autonomy import background, queue
from tests.test_social_owner_authority import authorize, close_provider_clients, stop  # noqa: F401


def configure_host(core, tmp_path, policy):
    cfg = replace(config(), data_dir=str(tmp_path / "bridge"), capture_signing_key="c" * 64,
                  protocol_epoch="synthetic-epoch", core_url="http://127.0.0.1:8765",
                  onebot_http_url="http://127.0.0.1:3000",
                  onebot_ws_url="ws://127.0.0.1:3001",
                  bridge_token=core.settings.bridge_token.get_secret_value())
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "purposes": ["chat", *policy.purposes], "capture": {
            "public_key": cfg.capture_public_key, "protocol_epoch": cfg.protocol_epoch,
            "identity_sha256": paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id])}})
    result, _ = activate(core, policy, revision=1, operation="account-host-capture")
    return replace(cfg, owner_authority=result["authority"])


def prepared_host(core, tmp_path):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    module, source, conversation = background(core, max_output_tokens=512)
    policy, _ = authorize(core, tmp_path)
    cfg = configure_host(core, tmp_path, policy)

    async def plan():
        def respond(request):
            data = json.loads(json.loads(request.content)["messages"][-1]["content"])
            proposal = {"kind": "qzone_publish", "text": "Synthetic public observation",
                        "source_refs": [data["sources"][0]["ref"]]}
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(proposal)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            module.model = ModelClient(core.settings, client)
            return await module.plan("native-host-test", source_ids=[source])

    frozen = queue(module, asyncio.run(plan()), conversation)
    return cfg, frozen


@pytest.mark.parametrize("outcome", ["ack", "unknown", "stop", "mutate",
                                    "unknown_sent", "unknown_cancelled", "owner", "ack_handoff"])
def test_account_host_checked_native_delivery(core, tmp_path, outcome):
    cfg, frozen = prepared_host(core, tmp_path)
    app = create_app(core.settings)
    app.state.core = core
    sent = []

    def native(request):
        if request.url.path == "/send_qzone_msg":
            sent.append(json.loads(request.content))
            if outcome.startswith("unknown"):
                raise httpx.ReadTimeout("synthetic ambiguity", request=request)
            data = {"tid": "synthetic-account-ack"}
        else:
            data = {"/get_login_info": {"user_id": int(cfg.bot_qq_id)},
                    "/get_status": {"online": True, "good": True},
                    "/get_version_info": {"app_name": "SnowLuma",
                                          "app_version": "2.0.3-node"}}[request.url.path]
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def mutate(request):
        if request.url.path == "/send_qzone_msg":
            if outcome == "stop":
                stop(core, cfg.owner_authority)
            elif outcome == "mutate":
                request.url = request.url.copy_with(path="/set_self_longnick")
            elif outcome == "owner":
                from companion_onebot_bridge.event_normalizer import normalize
                from tests.support.onebot_events import raw_event

                frame = json.dumps(raw_event(2, text="先回应这里",
                    time=int(datetime.fromisoformat(core.clock()).timestamp())))
                runtime = active[0]
                runtime.persist_capture(normalize(frame, cfg), frame,
                    received_at=runtime.clock(), peer=("127.0.0.1", 3001))

    active = []
    async def scenario():
        with Spool(cfg) as spool:
            runtime = OwnerRuntime(spool, clock=lambda: datetime.fromisoformat(core.clock()))
            active.append(runtime)
            async with private_http_client(cfg.core_url, cfg.bridge_token,
                transport=httpx.ASGITransport(app)) as client, private_http_client(
                    cfg.onebot_http_url, cfg.onebot_token,
                    transport=httpx.MockTransport(native)) as protocol:
                protocol.event_hooks["request"].append(mutate)
                guard = OwnerSendGuard(runtime, client)
                assert await deliver_tick(client, OwnerOneBot(guard, protocol), spool,
                                          guard=guard, delivery_lane="qq_account")
                expected = ("UNKNOWN" if outcome.startswith("unknown") else
                            "ACK" if outcome.startswith("ack") else "NOT_SENT")
                row = spool.db.execute("SELECT state FROM window_attempts").fetchone()
                assert row[0] == expected
                runtime.verify(active=not outcome.startswith("unknown"))
                assert len(sent) == (1 if expected in {"ACK", "UNKNOWN"} else 0)
                if sent:
                    assert sent[0] == {"content": frozen.proposal.text, "ugc_right": 16,
                                       "target_uins": [int(cfg.owner_qq_id)]}
                assert core.store.one("SELECT attempt_count FROM outbox WHERE message_id=?",
                                      (frozen.message_id,))["attempt_count"] == 1
                if outcome == "ack_handoff":
                    runtime.stop()
        if outcome in {"unknown_sent", "unknown_cancelled", "ack_handoff"}:
            from companion import delivery_reconciliation, owner_authority
            from companion_onebot_bridge import owner_handoff, owner_native_resolution

            policy = owner_authority.current(core.store, core.settings, core.clock).policy
            stop(core, cfg.owner_authority)
            core.clock.advance(60)
            with Spool(cfg, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: datetime.fromisoformat(core.clock()))
                async with private_http_client(cfg.core_url, cfg.bridge_token,
                    transport=httpx.ASGITransport(app)) as client:
                    if outcome != "ack_handoff":
                        assert (await owner_native_resolution.prepare(
                            runtime, client, frozen.message_id))["state"] == "READY"
                        target = outcome.removeprefix("unknown_").upper()
                        resolved = delivery_reconciliation.resolve(core.store, core.settings,
                            frozen.message_id, target, core.clock(),
                            operation_id="account-host-resolve", operation_sha256="e" * 64)
                        assert resolved["receipt"]["quiescence"]["authority"] == cfg.owner_authority
                        assert (await owner_native_resolution.reconcile(
                            runtime, client, frozen.message_id))["outcome"] == target
                    runtime.verify(active=False)
                    assert len(sent) == 1
                    await owner_handoff.prepare(runtime, client)
                    approved, _ = activate(core, policy,
                        revision=cfg.owner_authority["revision"] + 1,
                        operation="native-account-successor")
                    successor = approved["authority"]
                    assert (await owner_handoff.handoff(runtime, client, successor))[
                        "state"] == "ACTIVE"
            with Spool(replace(cfg, owner_authority=successor)) as spool:
                restarted = OwnerRuntime(spool,
                    clock=lambda: datetime.fromisoformat(core.clock()))
                restarted.verify()
                assert spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == (
                    "ACK" if outcome == "ack_handoff" else "UNKNOWN")
                assert len(sent) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("fence", [False, True])
def test_production_delivery_loop_in_independent_process(core, tmp_path, fence):
    from tests.support.core_http import real_core_http

    cfg, frozen = prepared_host(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port:
            path = tmp_path / "native-account-host.json"
            path.write_text(json.dumps({"config": asdict(replace(cfg,
                core_url=f"http://127.0.0.1:{port}")), "now": core.clock()}), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.native_account_host_process", str(path), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(45):
                    signal = await child.stdout.readline()
                    while signal and signal.strip() != b"BEFORE_ACCOUNT_CHECK":
                        assert "stage" in json.loads(signal), signal
                        signal = await child.stdout.readline()
                    assert signal.strip() == b"BEFORE_ACCOUNT_CHECK", signal
                    if fence:
                        stop(core, cfg.owner_authority)
                    output, error = await child.communicate(b"continue\n")
                    assert child.returncode == 0, error.decode()
                    result = json.loads(output.splitlines()[-1])
                    assert result == {"sent": 0 if fence else 1,
                                      "attempt": "NOT_SENT" if fence else "ACK"}
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert core.store.one("SELECT state,attempt_count FROM outbox WHERE message_id=?",
                (frozen.message_id,)) == {"state": "CANCELLED" if fence else "SENT",
                                         "attempt_count": 1}

    asyncio.run(scenario())
