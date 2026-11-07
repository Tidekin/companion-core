"""Actual native capture, API and delivery boundaries for consumer integration tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from companion import owner_authority, paid_budget
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after
from companion_onebot_bridge import owner_notices
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.core_http import real_core_http
from tests.support.native_context import Capture
from tests.support.onebot import onebot_http
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live


class Entry(Capture):
    def __init__(self, core, spool, runtime, frames, client, protocol):
        super().__init__(core, spool, runtime)
        self.frames, self.client, self.protocol = frames, client, protocol
        self.accepted = {}

    async def capture(self, number, text="", *, message=None):
        frame = raw_event(number, time=int(self.core.clock.now.timestamp()),
                          **({"message": message} if message is not None else {"text": text}))
        self.frames.put_nowait(json.dumps(frame))
        async with connect(self.config.onebot_ws_url,
            additional_headers={"Authorization": "Bearer " + self.config.onebot_token},
            proxy=None) as ws:
            received = await ws.recv()
            prepared = await prepare_owner_input(self.runtime, self.client,
                OneBotClient(self.config, self.protocol), received, self.core.clock.now,
                ws.remote_address)
            assert prepared is not None
            self.runtime.persist_capture(prepared["event"], received,
                received_at=self.core.clock.now, peer=ws.remote_address, media=prepared["media"])
            return prepared

    async def admit(self, number, text="", *, message=None):
        prepared = await self.capture(number, text, message=message)
        event_id = prepared["event"]["event_id"]

        async def record(response):
            if (response.request.url.path == "/internal/v1/inbox/events"
                and json.loads(response.request.content)["event_id"] == event_id):
                await response.aread()
                assert response.status_code == 202, response.text
                self.accepted[event_id] = response.json()

        self.client.event_hooks["response"].append(record)
        try:
            assert await self.runtime.flush_one(self.client)
        finally:
            self.client.event_hooks["response"].remove(record)
        return prepared["event"]

    async def recall(self, number):
        self.frames.put_nowait(json.dumps({"post_type": "notice", "notice_type": "friend_recall",
            "time": int(self.core.clock.now.timestamp()), "self_id": int(self.config.bot_qq_id),
            "user_id": int(self.config.owner_qq_id), "message_id": number}))
        async with connect(self.config.onebot_ws_url,
            additional_headers={"Authorization": "Bearer " + self.config.onebot_token},
            proxy=None) as ws:
            assert owner_notices.put(self.runtime, await ws.recv(), self.core.clock.now,
                                     ws.remote_address)
        assert await owner_notices.flush_one(self.runtime, self.client)

    async def state(self, event):
        capture = json.loads(self.db.execute(
            "SELECT capture_json FROM window_inputs WHERE event_id=?", (event["event_id"],)
        ).fetchone()[0])
        result = await self.client.post("/internal/v1/owner-authority/inputs/state", json=capture)
        assert result.status_code == 200, result.text
        return result.json()

    async def stop(self):
        result = await self.client.post("/internal/v1/owner-authority/stop",
            headers={"Authorization": "Bearer "
                     + self.core.settings.control_token.get_secret_value()},
            json={"operation_id": "native-consumer-stop", "authority": self.runtime.authority})
        assert result.status_code == 200, result.text
        self.runtime.stop()


@asynccontextmanager
async def environment(core, tmp_path, *, purposes=("chat", "memory", "history"),
                      core_options=None, paid_options=None, expires_in=None,
                      history_bytes=16000, history_start_offset=-1):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    for name, value in (core_options or {}).items():
        setattr(core.settings, name, value)
    approved_live(core, tmp_path, **(paid_options or {}))
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.rich_input_scope = "references_v1"
    core.settings.memory_enabled = "memory" in purposes
    core.settings.control_token = SecretStr("native-consumer-control-token-12345")
    key = Ed25519PrivateKey.generate()
    hashes = owner_authority.capability_hashes(core.settings)
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id, purposes=list(purposes),
        memory_scope="owner_private" if "memory" in purposes else "none",
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={name: value for name, value in hashes.items() if name in purposes},
        history={"scope": "owner_private",
                 "not_before": after(core.clock(), history_start_offset),
                 "max_turns": 15, "max_utf8_bytes": history_bytes} if "history" in purposes else {},
        expires_at=core.clock.now + timedelta(seconds=expires_in) if expires_in else None,
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "native-consumer-test"})
    authority = activate(core, policy)[0]["authority"]
    frames, handshakes, sends = asyncio.Queue(), [], []

    async def source(ws):
        await ws.send(await frames.get())
        await ws.wait_closed()

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
                    rich_input_scope="references_v1", data_dir=str(tmp_path / "native-consumer"))
                with Spool(cfg) as spool:
                    owner = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                        async with private_http_client(
                            cfg.onebot_http_url, cfg.onebot_token,
                        ) as protocol:
                            yield Entry(core, spool, owner, frames, client, protocol), sends
