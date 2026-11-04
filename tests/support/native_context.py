"""Signed native context fixtures; real Core HTTP/WS and original Bridge custody."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from companion import owner_authority, paid_budget
from companion.file_evidence import PrivateFilesConfig
from companion.image_input import ImageInputPolicy
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.support.core_http import real_core_http
from tests.support.onebot import onebot_http
from tests.support.owner_authority import activate
from tests.test_paid_model_gate import approved_live


def configure(core, tmp_path, *, scope="references_v1", history_bytes=0, media=False,
              owner_policy=None, **limits):
    approved_live(core, tmp_path, **limits)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.rich_input_scope = scope
    if media:
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            "image_input": ImageInputPolicy(approval_sha256="b" * 64,
                estimated_tokens_per_image=1000, max_bytes_per_image=100000,
                max_pixels_per_image=10000, max_images_per_request=1)})
        core.settings.private_files = PrivateFilesConfig(
            enabled=True, parser_image="sha256:" + "f" * 64)
    key = Ed25519PrivateKey.generate()
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", *(["history"] if history_bytes else []),
                  *(["image", "file"] if media else [])],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "native-context-test"},
        history={"scope": "owner_private", "not_before": after(core.clock(), -1),
                 "max_turns": 1, "max_utf8_bytes": history_bytes} if history_bytes else {},
        capability_policies={kind: owner_authority.capability_hashes(core.settings)[kind]
                             for kind in ("image", "file")} if media else {},
    )
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(), **(owner_policy or {})})
    authority = activate(core, policy)[0]["authority"]
    core.context_test_capture = SimpleNamespace(key=key, policy=policy, authority=authority,
                                               directory=tmp_path)
    return core.context_test_capture


async def approve_value(core, client, value):
    plan = core.context_test_capture
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
        protocol_epoch=plan.policy.capture.protocol_epoch, rich_input_scope="references_v1",
        owner_authority=plan.authority, capture_signing_key=plan.key.private_bytes_raw().hex(),
        data_dir=str(plan.directory / "natural-capture"))
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
        runtime.persist_capture(value, core.context_test_frame,
                                received_at=core.clock.now, peer=("127.0.0.1", 3001))
        capture = json.loads(spool.db.execute(
            "SELECT capture_json FROM window_inputs WHERE event_id=?",
            (value["event_id"],)).fetchone()[0])
    response = await client.post("/internal/v1/owner-authority/inputs/approve", json=capture)
    assert response.status_code == 200, response.text


class Capture:
    def __init__(self, core, spool, runtime):
        self.core, self.spool, self.runtime = core, spool, runtime
        self.db, self.config = spool.db, spool.config

    async def capture_once(self, client, protocol):
        async with connect(self.config.onebot_ws_url,
            additional_headers={"Authorization": "Bearer " + self.config.onebot_token},
            proxy=None) as ws:
            while True:
                frame = await ws.recv()
                if json.loads(frame).get("post_type") == "message":
                    break
            prepared = await prepare_owner_input(self.runtime, client,
                OneBotClient(self.config, protocol), frame, self.core.clock.now, ws.remote_address)
            assert prepared is not None
            inserted = self.runtime.persist_capture(prepared["event"], frame,
                received_at=self.core.clock.now, peer=ws.remote_address, media=prepared["media"])
            return {"new_inputs": int(inserted)}

    async def send(self, client, protocol):
        guard = OwnerSendGuard(self.runtime, client)
        result = await deliver_tick(client, OwnerOneBot(guard, protocol), self.spool, guard=guard)
        states = {row[0]: row[1] for row in self.db.execute(
            "SELECT state,count(*) FROM window_attempts GROUP BY state")}
        return {"worked": result, "attempt_states": states}


@asynccontextmanager
async def source_window(core, tmp_path, source, *, rich_input_scope="none", scope_options=None,
                        media=False):
    options = scope_options or {}
    for name, value in options.get("core_options", {}).items():
        setattr(core.settings, name, value)
    plan = configure(core, tmp_path, scope=rich_input_scope,
                     history_bytes=options.get("max_history_utf8_bytes", 0), media=media,
                     owner_policy=options.get("owner_policy"))
    handshakes, sends = [], []

    async def authenticate(connection, request):
        handshakes.append(request.headers.get("Authorization"))
        assert handshakes[-1] == "Bearer " + "s" * 32

    with onebot_http(sends, "s" * 32) as http_port:
        async with serve(source, "127.0.0.1", 0, process_request=authenticate) as server:
            config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
                bot_qq_id=core.settings.bot_qq_id,
                bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
                onebot_http_url=f"http://127.0.0.1:{http_port}",
                onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                protocol_epoch=plan.policy.capture.protocol_epoch,
                rich_input_scope=rich_input_scope, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / "native-context-bridge"))
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer " + config.bridge_token},
            ) as client, private_http_client(
                config.onebot_http_url, config.onebot_token,
            ) as protocol:
                with Spool(replace(config, core_url=str(client.base_url).rstrip("/"))) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    yield Capture(core, spool, runtime), client, protocol, handshakes, sends
