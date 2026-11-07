from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
from pydantic import SecretStr

from companion.config import LiveTextWindowApproval
from companion.model import ModelClient
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import WindowPlan
from tests.support.onebot import provider
from tests.support.onebot_delivery import core_client
from tests.support.onebot_events import raw_event
from tests.support.onebot_window import plan_fixture
from tests.support.paid_model import approved_live


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
