import asyncio
import hashlib
import socket
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn

from companion import live_text, paid_budget
from companion.memory_control import RESTORED, MemoryControlGrant, stopped_state, target_digest
from companion.privacy import epoch
from companion.store import uid
from companion_onebot_bridge.rich_input import input_digest
from companion_onebot_bridge.window import bridge_code_sha256
from tests.support.native_http import owned_http_protocol


def grant(core, cfg, text, *, operation="remember", target=None, **changes):
    restored = core.store.one("SELECT value FROM runtime_meta WHERE key=?", (RESTORED,))
    values = dict(
        execution="local_synthetic", grant_id=uid("mc"), operation=operation,
        permission={"remember": "memory.write", "confirm": "memory.write",
                    "correct": "memory.correct", "forget": "memory.forget"}[operation],
        max_commands=1, starts_at=core.clock.now - timedelta(seconds=1),
        ends_at=core.clock.now + timedelta(minutes=10), privacy_epoch=epoch(core.store),
        restore_generation=restored["value"] if restored else None,
        owner_approval_sha256="a" * 64, stopped_services_sha256="b" * 64,
        ledger_sha256=hashlib.sha256(Path(core.settings.erasure_ledger).read_bytes()).hexdigest(),
        stopped_state_sha256=stopped_state(core.store),
        identity_sha256=paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id]),
        core_config_sha256=live_text.config_sha256(core.settings),
        core_code_sha256=live_text.code_sha256(),
        bridge_config_sha256=paid_budget.sha(cfg.snapshot()),
        bridge_code_sha256=bridge_code_sha256(), capture_public_key=cfg.capture_public_key,
        protocol_epoch=cfg.protocol_epoch, command_sha256=input_digest({"text": text}),
        target_memory_id=target,
        target_sha256=target_digest(core.store, target) if target else None,
        fact_key="synthetic_project_name", subject="project",
    )
    return MemoryControlGrant(**(values | changes))


@asynccontextmanager
async def real_http(app, listener=None):
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    if listener is None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
    server = Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False,
                                   http=owned_http_protocol(lambda: server.servers)))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
