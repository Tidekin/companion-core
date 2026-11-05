"""Real authenticated WS/Core HTTP with native custody; only provider HTTP is synthetic.

Reuses DSH jobs-local exact-owner custody as reviewed against the original runtime,
https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/jobs/jobs-local.
WebSocket lifecycle: https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html
HTTP observation: https://www.python-httpx.org/advanced/event-hooks/
No fixture replaces approval, upload, lease, send-check, journal, or result handling.
"""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import uvicorn
from websockets.asyncio.server import serve

from companion import owner_authority, paid_budget
from companion.api import create_app
from companion.core import Core
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.worker_schedule import run_native_unit
from companion.worker_wakeup import Wakeup
from companion_onebot_bridge import app
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.support.core_http import real_core_http
from tests.support.native_http import owned_http_protocol
from tests.support.onebot import provider
from tests.support.onebot_events import config, raw_event
from tests.support.owner_authority import activate
from tests.test_paid_model_gate import approved_live


def frame(number=1, text="你好", **changes):
    return json.dumps(raw_event(number, text=text, time=int(datetime.now(UTC).timestamp()),
                               **changes), ensure_ascii=False)


def recall_frame(number=1, **changes):
    return json.dumps({"time": int(datetime.now(UTC).timestamp()), "self_id": 10002,
        "post_type": "notice", "user_id": 10001, "notice_type": "friend_recall",
        "message_id": number, **changes})


def answer(text="合成自动回复"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text},
                                                "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40}})


async def until(predicate, *, tasks=(), deadline_seconds=8):
    async with asyncio.timeout(deadline_seconds):
        while not predicate():
            for task in tasks:
                if task.done():
                    task.result()
                    raise AssertionError("native loop exited before expected evidence")
            await asyncio.sleep(0.01)


async def worker(core, stop):
    with Wakeup(core.store, core.settings, "worker", core.clock) as wake:
        while not stop.is_set():
            scan = wake.before_scan()
            result = await run_native_unit(core.run_once, "native-continuous-test")
            if not result.progressed:
                await wake.wait(scan, result)


@asynccontextmanager
async def core_http(core, *, independent=False, provider_reply=None):
    if not independent:
        async with real_core_http(core) as port:
            yield port, core
        return
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    api = create_app(core.settings)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = Server(uvicorn.Config(api, lifespan="on", log_config=None, access_log=False,
                                   http=owned_http_protocol(lambda: server.servers),
                                   timeout_keep_alive=60))
    # API vision may call the external model; only its HTTP transport is replaced.
    substitute = patch("companion.api.AsyncClient", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(provider_reply), **kwargs,
    )) if provider_reply is not None else nullcontext()
    with substitute:
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(), 20)
            yield listener.getsockname()[1], api.state.core
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            listener.close()


class Host:
    def __init__(self, core, cfg, client, protocol):
        self.core, self.config, self.client, self.protocol = core, cfg, client, protocol
        self.spool = Spool(cfg)
        self.runtime = OwnerRuntime(self.spool, clock=lambda: core.clock.now)
        self.onebot = OneBotClient(cfg, protocol)
        self.events, self.tasks = [], []
        self.metrics = Metrics("OFFLINE_FIXTURE", self.events.append)
        self.stop, self.wake = asyncio.Event(), asyncio.Event()

    @property
    def db(self):
        return self.spool.db

    def captures(self):
        return self.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0]

    def attempts(self):
        return dict(self.db.execute("SELECT state,COUNT(*) FROM window_attempts GROUP BY state"))

    def restart(self):
        assert not self.tasks
        self.spool.close()
        self.spool = Spool(self.config)
        self.runtime = OwnerRuntime(self.spool, clock=lambda: self.core.clock.now)

    def start(self, *, receive=True, upload=True, deliver=True, worker=True):
        assert not self.tasks
        self.stop, self.wake = asyncio.Event(), asyncio.Event()
        if receive:
            self.tasks.append(asyncio.create_task(app.receive(
                self.config, self.spool, self.wake, self.stop, self.metrics, self.onebot,
                runtime=self.runtime, core=self.client)))
        if upload:
            self.tasks.append(asyncio.create_task(app.upload(
                self.client, self.spool, self.wake, self.stop, self.metrics,
                runtime=self.runtime)))
        if deliver:
            self.tasks.append(asyncio.create_task(app.deliver(
                self.client, self.onebot, self.spool, self.stop, self.metrics,
                runtime=self.runtime, protocol=self.protocol)))
        if worker:
            self.tasks.append(asyncio.create_task(self.run_worker()))
        return self.tasks

    async def run_worker(self):
        with Core(self.core.settings, self.core.model, self.core.clock) as independent:
            await worker(independent, self.stop)

    async def pause(self):
        self.stop.set()
        for task in self.tasks:
            task.cancel()
        result = await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        return [item for item in result if isinstance(item, Exception)]

    async def capture(self, count=1):
        self.start(upload=False, deliver=False, worker=False)
        try:
            await until(lambda: self.captures() >= count, tasks=self.tasks)
        finally:
            await self.pause()

    async def flush(self):
        while await self.runtime.flush_one(self.client):
            pass

    async def pump(self):
        guard = OwnerSendGuard(self.runtime, self.client)
        return await deliver_tick(self.client, OwnerOneBot(guard, self.protocol),
                                  self.spool, guard=guard, metrics=self.metrics)


@asynccontextmanager
async def host(core, tmp_path, source, *, policy=None, protocol_reply=None, model_reply=None,
               reject_auth=False, ticking=True, expires=False, core_options=None,
               paid_options=None, purposes=("chat",), grant_options=None, bridge_options=None,
               independent_api=False):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    for name, value in (core_options or {}).items():
        setattr(core.settings, name, value)
    approved_live(core, tmp_path, **{"max_output_tokens": 4096,
        "per_attempt_micro_usd": 100000, "per_request_micro_usd": 200000, **(paid_options or {})})
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    if policy is not None:
        core.settings.segmented_reply = policy
    cfg = replace(config(), data_dir=str(tmp_path / "native-bridge"),
        bridge_token=core.settings.bridge_token.get_secret_value(), capture_signing_key="c" * 64,
        protocol_epoch="native-continuous", poll_seconds=0.05)
    cfg = replace(cfg, **(bridge_options or {}))
    hashes = owner_authority.capability_hashes(core.settings)
    grant = OwnerGrantPolicy.model_validate({"owner_qq_id": cfg.owner_qq_id,
        "purposes": list(purposes), "authorization_sha256": "d" * 64,
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        "expires_at": core.clock.now + timedelta(seconds=60) if expires else None,
        "source_retention_days": core.settings.raw_event_retention_days,
        "memory_scope": "owner_private" if "memory" in purposes else "none",
        "history": {"scope": "owner_private", "not_before": core.clock.now - timedelta(days=1),
                    "max_turns": 15, "max_utf8_bytes": 16000} if "history" in purposes else {},
        "capability_policies": {name: hashes[name] for name in purposes if name in hashes},
        "capture": {"public_key": cfg.capture_public_key, "protocol_epoch": cfg.protocol_epoch,
                    "identity_sha256": paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id])},
        **(grant_options or {})})
    reference = activate(core, grant)[0]["authority"]
    handshakes, sends, calls = [], [], []

    async def authenticate(connection, request):
        handshakes.append(request.headers.get("Authorization"))
        if reject_auth or handshakes[-1] != "Bearer " + cfg.onebot_token:
            return connection.respond(401, "synthetic auth required")

    async def native(request):
        if protocol_reply:
            result = protocol_reply(request)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                return result
        return provider(request, sends)

    async def model(request):
        assert request.url.host == "synthetic.invalid"
        calls.append(request)
        result = model_reply(request) if model_reply else answer()
        return await result if hasattr(result, "__await__") else result

    async def tick():
        previous = time.monotonic()
        probe = getattr(core.clock, "probe", None)
        if probe is not None:
            probe["tick_start"] = (previous, datetime.now(UTC).isoformat(), core.clock())
            probe["max_delta"] = 0
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            if probe is not None:
                probe["max_delta"] = max(probe["max_delta"], now - previous)
            core.clock.now += timedelta(seconds=now - previous)
            previous = now

    async with core_http(core, independent=independent_api, provider_reply=model) as (
        port, api_core,
    ), serve(
        source, "127.0.0.1", 0, process_request=authenticate,
    ) as server:
        cfg = replace(cfg, owner_authority=reference, core_url=f"http://127.0.0.1:{port}",
                      onebot_http_url="http://127.0.0.1:3000",
                      onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        async with private_http_client(cfg.core_url, cfg.bridge_token) as client, (
            private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                                transport=httpx.MockTransport(native))
        ) as protocol, httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_client:
            previous_model = core.model
            core.model = ModelClient(core.settings, model_client)
            active = Host(core, cfg, client, protocol)
            active.api_core = api_core
            # SDK/client startup can exceed the capture clock-drift bound.
            core.clock.now = datetime.now(UTC)
            core.clock.probe = {"anchor": (time.monotonic(), core.clock())}
            ticker = asyncio.create_task(tick()) if ticking else None
            try:
                yield active, handshakes, sends, calls
            finally:
                await active.pause()
                if ticker:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)
                active.spool.close()
                core.model = previous_model
