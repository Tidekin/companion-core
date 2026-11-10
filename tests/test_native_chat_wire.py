"""The shipped client over TLS sockets, followed by a fresh worker process; no QQ service."""

import asyncio
import ipaddress
import json
import os
import pickle
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from companion.api import create_app
from companion.api_store import ApiStore
from tests.conftest import run
from tests.support.owner_console import PASSWORD
from tests.support.owner_input import captured


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


@pytest.mark.skipif(os.environ.get('COMPANION_FLUTTER_WIRE') != '1',
    reason='explicit Flutter/Core cross-runtime gate; run scripts/check-flutter.ps1 -Wire')
def test_flutter_tls_entry_worker_and_restart_without_qq(core, tmp_path):
    captured(core, tmp_path)
    directory = tmp_path / 'flutter-client'
    directory.mkdir()
    root = Path(os.environ['COMPANION_FLUTTER_WORKSPACE']) if os.environ.get(
        'COMPANION_FLUTTER_WORKSPACE') else Path(__file__).resolve().parents[1]
    project = root / 'clients/chat'

    async def scenario():
        async with serve(core, tmp_path) as (origin, certificate):
            config = directory / 'wire.private.json'

            async def client(phase):
                config.write_text(json.dumps(dict(phase=phase, origin=origin,
                    certificate=str(certificate), password=PASSWORD, directory=str(directory),
                    now=core.clock.now.isoformat())), encoding='utf-8')
                env = {**os.environ, 'COMPANION_WIRE_CONFIG': str(config)}
                process = await asyncio.create_subprocess_exec('pwsh', '-NoProfile', '-File',
                    str(root / 'scripts/flutter.ps1'), '-Tool', 'flutter',
                    'test', 'test/wire_entry.dart',
                    cwd=project, env=env, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                try:
                    logs, _ = await asyncio.wait_for(process.communicate(), 180)
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                assert process.returncode == 0, logs.decode(errors='replace')

            await client('initial')
            receipt = json.loads((directory / 'receipt.json').read_text(encoding='utf-8'))
            client_task = asyncio.create_task(client('stream'))
            async with asyncio.timeout(90):
                while not (directory / 'subscription-ready').exists():
                    if client_task.done():
                        await client_task
                        raise AssertionError('client exited without subscribing')
                    await asyncio.sleep(0.05)
            job_id = core.store.one('SELECT job_id FROM jobs WHERE event_id=%s',
                                    (receipt['event_id'],))['job_id']
            worker_config = directory / 'worker.pickle'
            worker_config.write_bytes(pickle.dumps((core.settings, core.clock.now,
                                                   job_id, str(directory))))
            worker = await asyncio.create_subprocess_exec(sys.executable, '-B',
                str(root / 'tests/native_stream_worker.py'), str(worker_config),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                await client_task
                logs, _ = await asyncio.wait_for(worker.communicate(), 90)
                assert worker.returncode == 0, logs.decode(errors='replace')
            finally:
                if worker.returncode is None:
                    worker.kill()
                    await worker.wait()
            assert (directory / 'client-saw-preview').exists()
            await client('restart')
            assert len(core.store.rows('SELECT 1 FROM model_usage')) == 1

    run(scenario())
