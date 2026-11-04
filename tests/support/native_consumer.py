"""Consumer fixtures reuse signed Bridge capture, native grants and SDK selection."""

from contextlib import asynccontextmanager

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.server import serve

from companion import owner_authority, paid_budget
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.native_context import Capture
from tests.support.onebot import onebot_http
from tests.test_onebot_capture import real_core_http
from tests.test_owner_authority import activate
from tests.test_paid_model_gate import approved_live


@asynccontextmanager
async def source_window(core, tmp_path, source, *, scope_options, model=None):
    for name, value in scope_options.get("core_options", {}).items():
        setattr(core.settings, name, value)
    approved_live(core, tmp_path, **scope_options.get("paid_options", {}))
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.rich_input_scope = "references_v1"
    key = Ed25519PrivateKey.generate()
    capabilities = []
    if core.settings.paid_model.image_input is not None:
        capabilities.append("image")
    if core.settings.bili_video.enabled:
        capabilities.append("bili_video")
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", "content", "history", *capabilities],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={name: owner_authority.capability_hashes(core.settings)[name]
                             for name in capabilities},
        expires_at=scope_options.get("window_options", {}).get("ends_at"),
        history={"scope": "owner_private", "not_before": after(core.clock(), -3600),
                 "max_turns": 16, "max_utf8_bytes": 64000},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
            "identity_sha256": paid_budget.sha([core.settings.owner_qq_id,
                                                core.settings.bot_qq_id]),
            "protocol_epoch": "native-consumer-test"})
    authority = activate(core, policy)[0]["authority"]
    handshakes, sends = [], []

    async def authenticate(connection, request):
        handshakes.append(request.headers.get("Authorization"))
        assert handshakes[-1] == "Bearer " + "s" * 32

    with onebot_http(sends, "s" * 32) as onebot_port, Core(
        core.settings, model=model, clock=core.clock,
    ) as api_core:
        async with serve(source, "127.0.0.1", 0, process_request=authenticate) as server:
            async with real_core_http(api_core) as port:
                config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
                    bot_qq_id=core.settings.bot_qq_id,
                    bridge_token=core.settings.bridge_token.get_secret_value(),
                    onebot_token="s" * 32, core_url=f"http://127.0.0.1:{port}",
                    onebot_http_url=f"http://127.0.0.1:{onebot_port}",
                    onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    protocol_epoch=policy.capture.protocol_epoch, owner_authority=authority,
                    capture_signing_key=key.private_bytes_raw().hex(),
                    rich_input_scope="references_v1", data_dir=str(tmp_path / "consumer-bridge"))
                with Spool(config) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    entry = Capture(core, spool, runtime)
                    entry.api_core, entry.policy = api_core, policy
                    async with private_http_client(config.core_url, config.bridge_token) as client:
                        async with private_http_client(
                            config.onebot_http_url, config.onebot_token,
                        ) as protocol:
                            entry.onebot = OneBotClient(config, protocol)
                            yield entry, client, protocol, handshakes, sends


async def select_tools(core, name, urls):
    from tests.sdk_reader_fixture import Provider, answer, call, worker_for

    provider = Provider(answer(calls=[call(f"{name}-{index}", name, {"url": url})
                                      for index, url in enumerate(urls)]))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("consumer-tool-selection")
    assert not provider.responses
    return provider


async def admit(client, core, entry, number, text, *, reference=None):
    from companion.store import encode
    from companion_onebot_bridge.capture import prepare_owner_input
    from tests.support.onebot_events import raw_event

    raw = raw_event(number, time=int(core.clock.now.timestamp()), text=text)
    if reference is not None:
        raw["message"].append({"type": "reply", "data": {"id": str(reference)}})
    frame = encode(raw)
    prepared = await prepare_owner_input(entry.runtime, client, entry.onebot, frame, core.clock.now,
                                          ("127.0.0.1", 3001))
    entry.runtime.persist_capture(prepared["event"], frame, received_at=core.clock.now,
        peer=("127.0.0.1", 3001), media=prepared["media"])
    assert await entry.runtime.flush_one(client)
    event = prepared["event"]
    return event, core.store.one("SELECT * FROM jobs WHERE event_id=?", (event["event_id"],))
