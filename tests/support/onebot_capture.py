import json
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from websockets.asyncio.server import serve

from companion.api import create_app
from companion.config import LiveTextWindowApproval
from companion_onebot_bridge import network
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow, WindowPlan, digest
from tests.support.live_text_window import live_fixture
from tests.support.onebot import provider
from tests.support.onebot_events import config, raw_event
from tests.support.onebot_window import plan_fixture


def capture_approval(window):
    row = window.db.execute("SELECT * FROM window_inputs ORDER BY rowid DESC LIMIT 1").fetchone()
    return {
        "window_sha256": window.plan.sha256,
        "event_id": row["event_id"],
        "input_sha256": row["input_hash"],
        "capture_sha256": digest(json.loads(row["capture_json"])),
        "evidence_sha256": "e" * 64,
    }


def bootstrap_frames():
    common = {"time": raw_event()["time"], "self_id": 10002, "post_type": "meta_event"}
    return [
        common | {"meta_event_type": "lifecycle", "sub_type": kind}
        for kind in ("connect", "enable")
    ] + [
        common
        | {
            "meta_event_type": "heartbeat",
            "status": {"online": True, "good": True},
            "interval": 15000,
        }
    ]


def pinned_plan(
    core, tmp_path, ws_port, *, http_port=3000, core_port=8765, live=False, approved=False,
    rich_input_scope="none",
    scope_options=None,
    bridge_options=None,
):
    options = {
        "onebot_ws_url": f"ws://127.0.0.1:{ws_port}",
        "onebot_http_url": f"http://127.0.0.1:{http_port}",
        "core_url": f"http://127.0.0.1:{core_port}",
    }
    if approved:
        options["capture_signing_key"] = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    options.update(bridge_options or {})
    if live:
        cfg, plan = live_fixture(core, tmp_path, bridge_options=options,
                                 rich_input_scope=rich_input_scope, **(scope_options or {}))
        value = dict(plan.value)
        inventory = {
            key: value[key]
            for key in (
                "identity_sha256",
                "protocol_epoch",
                "config_sha256",
                "config_version",
                "image_ids",
            )
        }
    else:
        cfg, value, inventory, now = plan_fixture(tmp_path)
        cfg = replace(cfg, **options)
        value["config_sha256"] = inventory["config_sha256"] = digest(cfg.snapshot())
        value["runtime_attestation_sha256"] = digest(inventory)
        core.clock.now = now
        core.settings.bridge_token = SecretStr(cfg.bridge_token)
    authorization = None
    if approved:
        value["execution"] = "approved_live"
        if live:
            value["live_text"] = {**value["live_text"], "execution": "approved_live"}
        authorization = {
            "kind": "owner_qq_window",
            "approved": True,
            "scope_sha256": digest(
                {key: val for key, val in value.items() if key != "authorization_sha256"}
            ),
            "evidence_sha256": "a" * 64,
        }
        value["authorization_sha256"] = digest(authorization)
    plan = WindowPlan(value, cfg, inventory, authorization)
    if live:
        core.settings.live_text_window = LiveTextWindowApproval(
            **value["live_text"], plan_sha256=plan.sha256
        )
    return cfg, plan


@asynccontextmanager
async def source_window(
    core, tmp_path, source, *, live=False, approved=False, reject_auth=False, protocol_reply=None,
    rich_input_scope="none",
    scope_options=None,
    bridge_options=None,
    independent_api=False,
):
    handshakes, sends = [], []

    async def authenticate(connection, request):
        handshakes.append(request.headers.get("Authorization"))
        if reject_auth or request.headers.get("Authorization") != "Bearer " + config().onebot_token:
            return connection.respond(401, "synthetic auth required")

    async with serve(source, "127.0.0.1", 0, process_request=authenticate) as server:
        cfg, plan = pinned_plan(
            core, tmp_path, server.sockets[0].getsockname()[1], live=live, approved=approved,
            rich_input_scope=rich_input_scope,
            scope_options=scope_options,
            bridge_options=bridge_options,
        )

        def respond(request):
            if protocol_reply:
                result = protocol_reply(request)
                if result is not None:
                    return result
            return provider(request, sends)

        api = create_app(core.settings)
        if not independent_api:
            api.state.core = core
        async with (
            api.router.lifespan_context(api) if independent_api else nullcontext(),
            network.private_http_client(
                cfg.core_url, cfg.bridge_token, transport=httpx.ASGITransport(app=api)
            ) as client,
            network.private_http_client(
                cfg.onebot_http_url, cfg.onebot_token, transport=httpx.MockTransport(respond)
            ) as protocol,
        ):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client, protocol if approved else None)
                yield window, client, protocol, handshakes, sends
