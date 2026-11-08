"""Real serve entrypoint: forwarded sources affect limits only behind a named proxy."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from pydantic import ValidationError

from companion.operator_config import ConsoleConfig

ORIGIN = "https://console.example.test"
PASSWORD = "synthetic-console-owner-token-" * 2


def test_console_origin_has_no_private_deployment_default():
    assert ConsoleConfig().public_origin == ""
    with pytest.raises(ValidationError):
        ConsoleConfig(enabled=True)
    assert ConsoleConfig(enabled=True, public_origin=ORIGIN).enabled


@pytest.mark.parametrize("proxy", [None, "203.0.113.5", "127.0.0.1"])
def test_serve_proxy_source_limits_and_untrusted_spoofing(core, tmp_path, proxy):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPANION_")}
    env.update(
        COMPANION_CONFIG=str(tmp_path / "absent.toml"),
        COMPANION_DATABASE=core.settings.database.get_secret_value(),
        COMPANION_STATE_DIRECTORY=core.settings.state_directory,
        COMPANION_ERASURE_LEDGER=core.settings.erasure_ledger,
        COMPANION_OWNER_QQ_ID="10001", COMPANION_BOT_QQ_ID="10002",
        COMPANION_BRIDGE_TOKEN="synthetic-bridge-token-" * 2,
        COMPANION_CONTROL_TOKEN=PASSWORD, COMPANION_PROACTIVE_ENABLED="false",
        COMPANION_CONSOLE=json.dumps({"enabled": True, "public_origin": ORIGIN}),
        FORWARDED_ALLOW_IPS="*",  # An ambient Uvicorn variable must not enable trust.
    )
    command = [sys.executable, "-B", "-m", "companion", "serve", "--port", str(port)]
    if proxy:
        command += ["--forwarded-allow-ips", proxy]
    with (tmp_path / "server.log").open("wb") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, "synthetic console server exited"
                    try:
                        if http.get("/console/", timeout=0.5).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "synthetic server did not become ready"
                    time.sleep(0.05)
                def attempt(source, password):
                    return http.post("/v1/console/session", json={"password": password}, headers={
                        "Origin": ORIGIN, "X-Forwarded-For": source,
                        "X-Forwarded-Proto": "https",
                    })
                for _ in range(5):
                    assert attempt("198.51.100.10", "wrong").status_code == 401
                assert attempt("198.51.100.10", PASSWORD).status_code == 429
                assert attempt("203.0.113.99, 198.51.100.10", PASSWORD).status_code == 429
                # Trusted proxy: a different peer is not locked out. Untrusted client:
                # changing an arbitrary XFF header cannot reset its own socket's limit.
                expected = 200 if proxy == "127.0.0.1" else 429
                accepted = attempt("198.51.100.20", PASSWORD)
                assert accepted.status_code == expected
                if expected == 200:
                    headers = {
                        "Origin": ORIGIN, "X-CSRF-Token": accepted.json()["csrf_token"],
                        "Cookie": "companion_owner_session="
                        + accepted.cookies["companion_owner_session"],
                        "Idempotency-Key": "tcp-definite-refusal",
                    }
                    for _ in range(2):
                        refused = http.post("/v1/console/mode",
                                            json={"target": "proactive", "enabled": True},
                                            headers=headers)
                        assert refused.status_code == 409
                        assert refused.json() == {"error": "PROACTIVE_DISABLED_IN_CONFIG"}
                assert http.get("/internal/v1/status").status_code in {401, 404}
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "proxy.invalid", "127.0.0.1,"])
def test_cli_rejects_implicit_or_malformed_proxy_trust(value, monkeypatch):
    from companion import __main__

    monkeypatch.setattr(sys, "argv", ["companion", "serve", "--forwarded-allow-ips", value])
    with pytest.raises(SystemExit) as error:
        __main__.main()
    assert error.value.code == 2
