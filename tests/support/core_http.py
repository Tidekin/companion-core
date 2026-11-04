"""Serve a supplied Core over a real loopback HTTP listener."""

import asyncio
import socket
from contextlib import asynccontextmanager

import uvicorn

from companion.api import create_app
from tests.support.native_http import owned_http_protocol


@asynccontextmanager
async def real_core_http(core):
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    api = create_app(core.settings)
    api.state.core = core
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # Native children have a 30s deadline. Keep server idle expiry beyond the
    # client's 5s pool expiry to avoid Windows timer/connection-close races.
    server = Server(uvicorn.Config(api, lifespan="off", log_config=None, access_log=False,
                                   http=owned_http_protocol(lambda: server.servers),
                                   timeout_keep_alive=60))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
