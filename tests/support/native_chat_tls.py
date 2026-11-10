"""Actual TLS socket server for native clients and SSE subscription acceptance."""

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from companion.api import create_app
from companion.api_store import ApiStore
from tests.support.owner_console import PASSWORD


@asynccontextmanager
async def serve(core, tmp_path):
    import socket

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'native-loopback-test')])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(key, hashes.SHA256()))
    certificate, private_key = tmp_path / 'test-cert.pem', tmp_path / 'test-key.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    origin = 'https://127.0.0.1:' + str(listener.getsockname()[1])
    core.settings.console.enabled = True
    core.settings.console.public_origin = origin
    core.settings.control_token = SecretStr(PASSWORD)
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = Server(uvicorn.Config(app, lifespan='off', log_config=None, access_log=False,
        ssl_keyfile=str(private_key), ssl_certfile=str(certificate), timeout_keep_alive=60))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield origin, certificate
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
