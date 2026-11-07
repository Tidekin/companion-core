"""Node boundary tests and real TLS/WS/Core entrypoint with independent local processes."""

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
import pytest
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
from companion.voice_config import VoiceASRPolicy
from companion.voice_jobs import VoiceJobs
from companion.voice_node_api import register_voice_node_routes
from companion.voice_protocol import (
    MAX_RESULT_BODY_BYTES,
    ROUTE_PREFIX,
    VoiceClaimResponse,
    VoiceLeaseRef,
    canonical_bytes,
)
from companion_voice_node.worker import VoiceNodeConfig, VoiceNodeWorker
from tests.conftest import run
from tests.support.native_http import owned_http_protocol
from tests.test_voice_jobs import TOKEN, create_tts, prepared, result
from tests.test_voice_node_worker import factory
from tests.test_voice_node_worker import node as node  # noqa: F401 -- pytest fixture


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


def test_api_auth_idempotency_path_and_result_receipt(core, monkeypatch):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    job = create_tts(core, jobs, message_id)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core)), base_url="http://test"
        ) as client:
            body = claim_body(core)
            headers = {
                "Authorization": "Bearer " + TOKEN,
                "Idempotency-Key": f"voice-claim:{body['incarnation']}:{body['sequence']}",
            }
            for token in (
                "wrong-credential-" + "x" * 32,
                core.settings.bridge_token.get_secret_value(),
            ):
                response = await client.post(
                    ROUTE_PREFIX + "/claim",
                    json=body,
                    headers=headers | {"Authorization": "Bearer " + token},
                )
                assert response.status_code == 401
            assert core.store.rows("SELECT * FROM voice_node_state") == []
            assert (
                await client.post(
                    ROUTE_PREFIX + "/claim", json=body, headers={"Authorization": "Bearer " + TOKEN}
                )
            ).status_code == 400
            claimed = await client.post(ROUTE_PREFIX + "/claim", json=body, headers=headers)
            assert claimed.status_code == 200 and claimed.headers["cache-control"] == "no-store"
            # Normalized DTO hashing makes key replay independent of wire whitespace/key order.
            replayed = await client.post(
                ROUTE_PREFIX + "/claim",
                content=json.dumps(dict(reversed(list(body.items()))), indent=2),
                headers=headers | {"Content-Type": "application/json"},
            )
            assert replayed.json() == claimed.json()
            ref = VoiceLeaseRef.from_lease(VoiceClaimResponse.model_validate(claimed.json()).lease)
            path = ROUTE_PREFIX + "/jobs/" + job["job_id"]
            bad = await client.post(
                ROUTE_PREFIX + "/jobs/voice_" + "f" * 32 + "/input",
                json=ref.model_dump(),
                headers=headers,
            )
            assert bad.status_code == 409
            assert jobs.status(job["job_id"])["state"] == "LEASED"
            source = await client.post(path + "/input", json=ref.model_dump(), headers=headers)
            assert source.status_code == 200 and source.headers["cache-control"] == "no-store"
            value = result(ref)
            ready = await client.post(
                path + "/control",
                json={
                    "ref": ref.model_dump(),
                    "phase": "RESULT_READY",
                    "resource_state": "READY",
                    "result_sha256": value.result_sha256,
                },
                headers=headers,
            )
            assert ready.json()["action"] == "CONTINUE"
            headers["Idempotency-Key"] = f"voice-result:{ref.job_id}:{ref.generation}"
            accepted = await client.put(
                path + "/result",
                content=canonical_bytes(value),
                headers=headers | {"Content-Type": "application/json"},
            )
            assert accepted.status_code == 200 and accepted.json()["status"] == "COMMITTED"
            again = await client.put(
                path + "/result", json=value.model_dump(mode="json"), headers=headers
            )
            assert again.json() == accepted.json()
            assert jobs.result(job["job_id"]) == value.payload

    run(scenario())


@pytest.mark.parametrize("reserved", ["bridge_token", "control_token", "diary_admin_token"])
def test_voice_credential_cannot_alias_owner_bridge_or_admin(core, monkeypatch, reserved):
    prepared(core, monkeypatch)
    from pydantic import SecretStr

    setattr(core.settings, reserved, SecretStr(TOKEN))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core)), base_url="http://test"
        ) as client:
            body = claim_body(core)
            response = await client.post(
                ROUTE_PREFIX + "/claim",
                json=body,
                headers={
                    "Authorization": "Bearer " + TOKEN,
                    "Idempotency-Key": f"voice-claim:{body['incarnation']}:{body['sequence']}",
                },
            )
            assert response.status_code == 401
            assert core.store.rows("SELECT * FROM voice_node_state") == []

    run(scenario())


def test_boundary_auth_precedes_body_read_and_stream_limit_is_enforced(core, monkeypatch):
    prepared(core, monkeypatch)

    async def scenario():
        reads = []

        async def oversized():
            reads.append(True)
            yield b"x" * (MAX_RESULT_BODY_BYTES + 1)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core, boundary_only=True)),
            base_url="http://test",
        ) as client:
            path = ROUTE_PREFIX + "/jobs/voice_" + "a" * 32 + "/result"
            denied = await client.put(
                path, content=oversized(), headers={"Content-Type": "application/json"}
            )
            assert denied.status_code == 401 and not reads
            response = await client.put(
                path,
                content=oversized(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
            )
            assert response.status_code == 413 and len(reads) == 1
            duplicate = await client.post(
                ROUTE_PREFIX + "/claim",
                content=b'{"node_id":1,"node_id":2}',
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
            )
            assert duplicate.status_code == 400 and "node_id" not in duplicate.text

    run(scenario())


@pytest.mark.parametrize("change", ["cancelled", "revision", "source", "scope", "fenced_epoch"])
def test_disabled_asr_input_state_preserves_source_authority(core, monkeypatch, change):
    _, _, event_id, _ = prepared(core, monkeypatch, make_reply=False,
                                asr=VoiceASRPolicy(enabled=False))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(core)), base_url="http://test",
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            path = f"/internal/v1/voice-inputs/{event_id}/state"
            initial = await client.get(path)
            assert initial.status_code == 200, initial.text
            assert initial.json()["state"] == "FALLBACK"
            assert not initial.json()["records"][0]["upload_allowed"]
            if change == "cancelled":
                core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE event_id=?",
                                   (event_id,))
            elif change == "revision":
                core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
            elif change == "source":
                event = core.store.one("SELECT body_json FROM events WHERE event_id=?", (event_id,))
                body = json.loads(event["body_json"])
                body["text"] = "source changed after capture"
                core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                                   (encode(body), event_id))
            elif change == "scope":
                core.settings.voice = core.settings.voice.model_copy(
                    update={"job_ttl_seconds": 91})
            else:
                event = core.store.one("SELECT * FROM events WHERE event_id=?", (event_id,))
                value = core.voice._input_authority(event) | {"state": "FENCED"}
                core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                                   ("voice_input:" + event_id, encode(value)))
                core.store.execute(
                    "INSERT OR REPLACE INTO runtime_meta VALUES ('privacy_epoch','1')")
            before = core.store.db.total_changes
            denied = await client.get(path)
            assert denied.status_code in {403, 409}, denied.text
            assert core.store.db.total_changes == before
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())


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


@pytest.mark.parametrize("advancing", [False, True])
def test_node_wall_clock_preserves_parent_anchor(monkeypatch, advancing):
    clock = node_wall_clock(1000.0, 200.0 if advancing else None)
    # The first reading occurs after child startup; the anchor must not reset there.
    monkeypatch.setattr(time, "monotonic", lambda: 208.0)
    assert clock() == (1008.0 if advancing else 1000.0)
    monkeypatch.setattr(time, "monotonic", lambda: 210.0)
    assert clock() == (1010.0 if advancing else 1000.0)


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
        "tests.test_voice_node_api",
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
