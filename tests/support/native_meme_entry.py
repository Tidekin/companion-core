"""Native signed WS, independent Core HTTP and original OneBot transport fixtures."""

from contextlib import asynccontextmanager

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.server import serve

from companion import owner_authority, paid_budget, private_life
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.core_http import real_core_http
from tests.support.native_context import Capture
from tests.support.onebot import onebot_http
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live


@asynccontextmanager
async def native_entry(core, tmp_path, source, *, options, rules, public, resolver):
    for name, value in options["core_options"].items():
        setattr(core.settings, name, value)
    approved_live(core, tmp_path, **options["paid_options"])
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate()
    voice = ["voice"] if core.settings.voice.enabled else []
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", "history", "meme", *private_life.FEATURES, *voice],
        memory_scope="owner_private", authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={name: owner_authority.capability_hashes(core.settings)[name]
                             for name in ("meme", *voice)},
        history={"scope": "owner_private", "not_before": after(core.clock(), -1),
                 "max_turns": 15, "max_utf8_bytes": 16000},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "native-meme-semantic"})
    authority = activate(core, policy)[0]["authority"]
    handshakes, sends = [], []

    async def authenticate(connection, request):
        handshakes.append(request.headers.get("Authorization"))
        assert handshakes[-1] == "Bearer " + "s" * 32

    with onebot_http(sends, "s" * 32) as onebot_port, Core(
        core.settings, clock=core.clock,
    ) as api_core:
        async with serve(source, "127.0.0.1", 0, process_request=authenticate) as server:
            async with real_core_http(api_core) as api_port:
                cfg = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
                    bot_qq_id=core.settings.bot_qq_id,
                    bridge_token=core.settings.bridge_token.get_secret_value(),
                    onebot_token="s" * 32, core_url=f"http://127.0.0.1:{api_port}",
                    onebot_http_url=f"http://127.0.0.1:{onebot_port}",
                    onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    protocol_epoch=policy.capture.protocol_epoch, owner_authority=authority,
                    capture_signing_key=key.private_bytes_raw().hex(),
                    data_dir=str(tmp_path / "native-meme-bridge"), meme_enabled=True,
                    meme_discovery_enabled=True, meme_semantics_enabled=True,
                    meme_trusted_sources=rules)
                with Spool(cfg) as spool:
                    owner = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    owner._memes = MemeRuntime(owner, transport=public, resolver=resolver)
                    entry = Capture(core, spool, owner)
                    entry.api_core = api_core
                    async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                        async with private_http_client(
                            cfg.onebot_http_url, cfg.onebot_token,
                        ) as protocol:
                            yield entry, client, protocol, handshakes, sends
