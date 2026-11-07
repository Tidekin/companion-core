import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from companion.store import after
from tests.support.gateway import body, gateway_harness, rotate  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PROCESS = ROOT / "tests/support/gateway_process.py"


@pytest.mark.parametrize("pause_record", [None, "reserve", "settle"])
def test_real_http_ingress_independent_worker_and_original_receipt(harness, tmp_path, pause_record):
    core = harness.core
    core.clock.now = datetime.now(UTC)
    rotate(harness, expires_at=after(core.clock(), 7200))
    harness.config_path.write_text(harness.config_path.read_text("utf-8").replace(
        "wait_seconds = 0", "wait_seconds = 15"), encoding="utf-8")
    settings_file = tmp_path / "settings.json"

    def serialize(value):
        return value.get_secret_value() if isinstance(value, SecretStr) else value.isoformat()

    settings_file.write_text(json.dumps(core.settings.model_dump(), default=serialize),
                              encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer " + harness.key, "Idempotency-Key": "http-request-123"}
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with (tmp_path / "api.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-B", str(PROCESS), "api", str(settings_file),
                                   str(port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   **flags)
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=20) as client:
                while True:
                    try:
                        ready = client.get("/v1/models")
                        assert ready.status_code == 200, ready.text
                        break
                    except httpx.TransportError:
                        assert process.poll() is None, (tmp_path / "api.log").read_text("utf-8")
                        assert time.monotonic() < deadline
                        time.sleep(0.05)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    observed = tmp_path / "provider.json"
                    arguments = [sys.executable, "-B", str(PROCESS),
                                 "worker-paused" if pause_record else "worker",
                                 str(settings_file), str(observed)]
                    if pause_record:
                        arguments.append(pause_record)
                    arguments.append("--wait-ready")
                    worker = subprocess.Popen(arguments, cwd=ROOT, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **flags)
                    try:
                        assert worker.stdout.readline().strip() == "GATEWAY_WORKER_READY"
                        pending = pool.submit(client.post, "/v1/chat/completions", json=body())
                        deadline = time.monotonic() + 5
                        while not core.store.one("SELECT 1 FROM gateway_requests"):
                            assert time.monotonic() < deadline
                            time.sleep(0.02)
                        worker.stdin.write("run\n")
                        worker.stdin.flush()
                        if pause_record:
                            assert worker.stdout.readline().strip() == "GATEWAY_JOURNAL_APPENDED"
                            # Six API poll intervals pass while the journal append is
                            # durable but the database transaction is uncommitted.
                            with pytest.raises(FutureTimeout):
                                pending.result(timeout=0.3)
                            worker.stdin.write("continue\n")
                            worker.stdin.flush()
                        _, error = worker.communicate(timeout=30)
                        assert worker.returncode == 0, error
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                            worker.communicate(timeout=10)
                    result = pending.result(timeout=20)
                assert result.status_code == 200, result.text
                completion = result.json()
                assert completion["choices"][0]["message"]["content"] == "independent worker reply"
                request_id = result.headers["X-Request-Id"]
                receipt = client.get("/v1/gateway/requests/" + request_id)
                assert receipt.json()["completion"] == completion
                replay = client.post("/v1/chat/completions", json=body())
                assert replay.json() == completion
                assert len(json.loads(observed.read_text("utf-8"))) == 1
                assert len(core.store.rows("SELECT * FROM model_usage")) == 1
                assert not core.store.rows("SELECT * FROM events")
                assert not core.store.rows("SELECT * FROM outbox")
                assert core.model.calls == []
                with Path(core.settings.paid_model.journal_path).open("ab") as journal:
                    journal.write(b"{broken-journal-record}\n")
                blocked = client.get("/v1/gateway/requests/" + request_id)
                assert blocked.status_code == 503
                assert blocked.json()["error"]["code"] == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
                assert len(json.loads(observed.read_text("utf-8"))) == 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_hard_worker_exit_leaves_native_reservation_unknown_without_replay(harness, tmp_path):
    from companion import gateway

    core = harness.core
    core.clock.now = datetime.now(UTC)
    rotate(harness, expires_at=after(core.clock(), 7200))
    request_id = harness.submit()
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps(core.settings.model_dump(), default=lambda value:
        value.get_secret_value() if isinstance(value, SecretStr) else value.isoformat()),
        encoding="utf-8")
    observed = tmp_path / "dispatch.json"
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    crashed = subprocess.run([sys.executable, "-B", str(PROCESS), "crash", str(settings_file),
        str(observed)], cwd=ROOT, capture_output=True, text=True, timeout=30, **flags)
    assert crashed.returncode == 23, crashed.stderr
    assert len(json.loads(observed.read_text("utf-8"))) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "RESERVED"
    core.clock.advance(121)
    assert not harness.process(lambda req: (_ for _ in ()).throw(AssertionError("replayed")))
    result = gateway.receipt(core.store, core.settings, "client-a", request_id, core.clock())
    assert result["status"] == "UNKNOWN"
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
