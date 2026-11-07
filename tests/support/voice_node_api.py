import asyncio
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from companion.api import create_app
from companion.api_store import ApiStore
from companion.contracts import CoreError
from companion.store import encode
from companion.voice_jobs import VoiceJobs
from companion.voice_node_api import register_voice_node_routes
from companion.voice_protocol import ROUTE_PREFIX
from companion_voice_node.worker import VoiceNodeConfig, VoiceNodeWorker
from tests.support.native_http import owned_http_protocol
from tests.support.voice_node_worker import factory


def claim_body(core):
    session = VoiceJobs(core.store, core.settings, core.clock).session("local")
    return {
        "node_id": "local",
        "incarnation": session.incarnation,
        "sequence": session.last_sequence + 1,
        "manifest_sha256": "b" * 64,
        "resource_state": "READY",
    }


def app_for(core, *, boundary_only=False):
    if boundary_only:
        app = FastAPI()

        @app.exception_handler(CoreError)
        async def error(request, exception):
            return JSONResponse({"error": exception.code}, status_code=exception.status)

        register_voice_node_routes(app)
    else:
        app = create_app(core.settings)
        assert ROUTE_PREFIX + "/claim" in app.openapi()["paths"]
    app.state.core = core
    if boundary_only:
        app.state.api_store = ApiStore(core.settings.database)
    return app


def tls_material(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic voice core")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "core-cert.pem", tmp_path / "core-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@asynccontextmanager
async def tls_core(core, tmp_path):
    cert, key = tls_material(tmp_path)
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = Server(
        uvicorn.Config(
            app_for(core),
            lifespan="off",
            log_config=None,
            access_log=False,
            http=owned_http_protocol(lambda: server.servers),
            # Match real_core_http: server idle expiry outlasts the client's 5s pool expiry.
            timeout_keep_alive=60,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        yield f"https://127.0.0.1:{listener.getsockname()[1]}", cert
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


def node_wall_clock(timestamp, monotonic_start=None):
    """Share a fixed clock, or a parent's advancing clock including process startup."""
    if monotonic_start is None:
        return lambda: timestamp
    return lambda: timestamp + time.monotonic() - monotonic_start


async def independent_node(config, certificate, counter, tmp_path, timestamp, scenario="success",
                           *, monotonic_start=None):
    config_path = tmp_path / "node-fixture.json"
    config_path.write_text(
        encode(
            config.model_dump(mode="json") | {"credential": config.credential.get_secret_value()}
        ),
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.support.voice_node_api",
        "--node-fixture",
        str(config_path),
        str(certificate),
        str(counter),
        str(timestamp),
        scenario,
        *([str(monotonic_start)] if monotonic_start is not None else []),
        cwd=str(config.workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        receipt = json.loads(stdout)
        assert receipt["pid"] != os.getpid() and receipt["outcome"] == "COMPLETED"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        config_path.unlink(missing_ok=True)


if __name__ == "__main__" and sys.argv[1:2] == ["--node-fixture"]:
    node_config = VoiceNodeConfig.model_validate_json(Path(sys.argv[2]).read_text(encoding="utf-8"))
    trust = ssl.create_default_context(cafile=sys.argv[3])
    # Fixed Core clocks remain fixed; advancing callers supply their original anchor.
    clock = node_wall_clock(float(sys.argv[5]), float(sys.argv[7]) if len(sys.argv) > 7 else None)
    with patch("companion_voice_node.worker.time.time", side_effect=clock):
        outcome = asyncio.run(
            VoiceNodeWorker(
                node_config,
                transport=httpx.AsyncHTTPTransport(verify=trust),
                supervisor_factory=factory(Path(sys.argv[4]), scenario=sys.argv[6]),
            ).run(once=True)
        )
    print(json.dumps({"outcome": outcome, "pid": os.getpid()}))
