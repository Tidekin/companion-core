import asyncio
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from companion_onebot_bridge.network import private_http_client, resolve_private, websocket
from companion_onebot_bridge.onebot_client import OneBotClient
from tests.conftest import run
from tests.support.onebot import onebot_http
from tests.support.onebot_events import config


def test_real_loopback_http_probe_and_owner_send_are_bounded_and_authenticated():
    sends = []
    with onebot_http(sends, config().onebot_token) as port:
        cfg = config(
            onebot_http_url=f"http://127.0.0.1:{port}",
            onebot_ws_url="ws://127.0.0.1:3001",
            delivery_enabled=True,
        )

        async def scenario():
            async with private_http_client(cfg.onebot_http_url, cfg.onebot_token) as client:
                onebot = OneBotClient(cfg, client)
                assert (await onebot.probe())["connection_ready"]
                assert await onebot.send_text("literal [CQ:image,file=untrusted]") == "123456"

        run(scenario())
    assert len(sends) == 1
    assert sends[0]["message"] == [
        {"type": "text", "data": {"text": "literal [CQ:image,file=untrusted]"}},
    ]


def test_public_dns_answer_is_rejected_before_any_authenticated_io(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()

        async def public_answer(*args, **kwargs):
            return [(2, 1, 6, "", ("8.8.8.8", 3000))]

        monkeypatch.setattr(loop, "getaddrinfo", public_answer)
        with pytest.raises(ValueError, match="PUBLIC_DNS"):
            await resolve_private("snowluma", 3000)

    run(scenario())


def test_websocket_redirect_is_not_followed():
    calls = []

    async def scenario():
        async def redirect(connection, request):
            calls.append(request.path)
            response = connection.respond(302, "redirect")
            response.headers["Location"] = "ws://public.example:3001/"
            return response

        async def unused(ws):
            pytest.fail("redirect must not reach application")

        async with serve(unused, "127.0.0.1", 0, process_request=redirect) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = config(
                onebot_ws_url=f"ws://127.0.0.1:{port}", onebot_http_url="http://127.0.0.1:3000"
            )
            with pytest.raises(InvalidStatus):
                async with await websocket(cfg):
                    pytest.fail("redirect accepted")

    run(scenario())
    assert calls == ["/"]


def test_private_tcp_pinning_preserves_https_wss_hostname_sni_and_auth(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "snowluma")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("snowluma")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    sni, http_headers, ws_headers = [], [], []
    server_context.set_servername_callback(lambda conn, host, ctx: sni.append(host))
    trust = ssl.create_default_context(cafile=str(cert_path))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            http_headers.append((self.headers["Host"], self.headers["Authorization"]))
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    async def scenario():
        loop = asyncio.get_running_loop()

        async def dns(host, port, **kw):
            assert host == "snowluma"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        monkeypatch.setattr(loop, "getaddrinfo", dns)
        cfg = config(onebot_http_url=f"https://snowluma:{server.server_address[1]}",
                     onebot_ws_url="wss://snowluma:3001")
        async with private_http_client(cfg.onebot_http_url, cfg.onebot_token) as client:
            # Trust only our synthetic CA; certificate hostname checking stays enabled.
            client._transport._pool._ssl_context = trust
            assert (await client.post("/get_status", json={})).status_code == 200

        async def handler(ws):
            ws_headers.append((ws.request.headers["Host"], ws.request.headers["Authorization"]))
            await ws.send("fixture")

        async with serve(handler, "127.0.0.1", 0, ssl=server_context) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            ws_config = config(onebot_http_url=cfg.onebot_http_url,
                               onebot_ws_url=f"wss://snowluma:{port}")
            connector = await websocket(ws_config)
            connector.connection_kwargs["ssl"] = trust
            async with connector as ws:
                assert await ws.recv() == "fixture"
        assert http_headers == [(f"snowluma:{server.server_address[1]}",
                                 "Bearer " + cfg.onebot_token)]
        assert ws_headers == [(f"snowluma:{port}", "Bearer " + cfg.onebot_token)]
        assert sni == ["snowluma", "snowluma"]

    try:
        run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
