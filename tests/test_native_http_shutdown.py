"""Real owned HTTP transport cleanup on the fixture's Windows Proactor scope."""

import asyncio
import ssl
from contextlib import AsyncExitStack

import httpx
import pytest
from uvicorn.protocols.http.auto import AutoHTTPProtocol

from tests.native_continuous_support import core_http
from tests.support.native_http import native_http_proactor_scope
from tests.support.voice_node_api import tls_core


@pytest.mark.skipif(not native_http_proactor_scope(),
                    reason="Windows CPython 3.12 Proactor cleanup compatibility")
@pytest.mark.parametrize("tls,independent", [(False, False), (False, True), (True, False)],
                         ids=["shared", "independent", "tls"])
@pytest.mark.parametrize("failure", [None, "reset", "other"])
def test_native_http_closes_real_socket_and_preserves_callback_error(
    core, tmp_path, monkeypatch, failure, tls, independent,
):
    transports, servers, sockets, errors, lost = [], [], [], [], []
    made, connection_lost = AutoHTTPProtocol.connection_made, AutoHTTPProtocol.connection_lost

    def observe(self, transport):
        native = transport._ssl_protocol._transport if tls else transport
        transports.append(native)
        servers.append(native._server)
        sockets.append(native._sock)
        return made(self, transport)

    def observe_lost(self, error):
        lost.append(self)
        return connection_lost(self, error)

    monkeypatch.setattr(AutoHTTPProtocol, "connection_made", observe)
    monkeypatch.setattr(AutoHTTPProtocol, "connection_lost", observe_lost)
    expected = None
    if failure == "reset":
        expected = ConnectionResetError("synthetic shutdown reset")
        expected.winerror = 10054
    elif failure == "other":
        expected = OSError("synthetic shutdown failure")

    class FailedShutdown:
        def __init__(self, sock):
            self.sock = sock

        def shutdown(self, how):
            raise expected

        def __getattr__(self, name):
            return getattr(self.sock, name)

    async def scenario():
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()

        def handler(loop, context):
            errors.append(context.get("exception"))
            if previous is None:
                loop.default_exception_handler(context)
            else:
                previous(loop, context)

        loop.set_exception_handler(handler)
        try:
            async with AsyncExitStack() as stack:
                if tls:
                    url, certificate = await stack.enter_async_context(tls_core(core, tmp_path))
                    served_core, verify = core, ssl.create_default_context(cafile=str(certificate))
                else:
                    port, served_core = await stack.enter_async_context(
                        core_http(core, independent=independent))
                    url, verify = f"http://127.0.0.1:{port}", True
                assert (served_core is not core) if independent else (served_core is core)
                async with httpx.AsyncClient(trust_env=False, verify=verify) as client:
                    assert client._transport._pool._keepalive_expiry == 5
                    response = await client.get(
                        url + "/internal/v1/health/live")
                    assert response.status_code == 200 and len(transports) == 1
                    assert native_http_proactor_scope(transports[0], servers)
                    assert not native_http_proactor_scope(transports[0], ())
                    assert sockets[0].fileno() >= 0
                    if expected is not None:
                        transports[0]._sock = FailedShutdown(sockets[0])
            assert len(lost) == 1
            assert errors == ([] if expected is None else [expected])
            assert sockets[0].fileno() == -1
            assert transports[0]._sock is None and transports[0]._server is None
            assert transports[0]._called_connection_lost
            assert servers[0]._active_count == 0
        finally:
            for sock in sockets:
                sock.close()
            loop.set_exception_handler(previous)

    asyncio.run(scenario())
