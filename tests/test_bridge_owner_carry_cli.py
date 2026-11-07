"""Actual CLI/process lock and TCP contract shim; full Core integration is separate."""

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from companion_onebot_bridge import journal
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.bridge_owner_carry_handoff import capture, handler, proof, stopped
from tests.support.bridge_owner_runtime import config as config


def write_config(path, config):
    path.write_text("\n".join(f"{key} = {json.dumps(value)}"
        for key, value in asdict(config).items() if value is not None and key != "owner_authority")
        + "\n[owner_authority]\n" + "\n".join(f"{key} = {json.dumps(value)}"
            for key, value in config.owner_authority.items()), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def test_real_cli_carry_prepare_lock_rebind_and_original_input_fence(config, tmp_path):
    original = capture(config)
    retained = proof(original)
    calls, errors = [], []
    respond = handler(config, retained, calls=calls, credential=config.bridge_token)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                request = httpx.Request("POST", "http://127.0.0.1" + self.path,
                    headers=dict(self.headers),
                    content=self.rfile.read(int(self.headers["Content-Length"])))
                response = respond(request)
                data = response.content
                self.send_response(response.status_code)
            except Exception as error:
                errors.append(type(error).__name__)
                data = b'{}'
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = replace(config, core_url=f"http://127.0.0.1:{server.server_port}",
            onebot_http_url=f"http://127.0.0.1:{server.server_port}",
            onebot_ws_url=f"ws://127.0.0.1:{server.server_port}")
        path = tmp_path / "carry-bridge.toml"
        write_config(path, config)

        def cli(command, *args):
            result = subprocess.run([sys.executable, "-m", "companion_onebot_bridge",
                "--config", str(path), command, *args], capture_output=True, text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=20)
            return result.returncode, json.loads(result.stdout)

        arguments = ("--carry-sha256", "a" * 64, "--successor-policy-sha256", "c" * 64)
        runtime = stopped(config)
        try:
            with runtime.spool:
                code, body = cli("owner-carry-handoff-prepare", *arguments)
                assert code == 1 and body["state"] == "STOPPED" and not calls
            code, prepared = cli("owner-carry-handoff-prepare", *arguments)
            assert code == 0 and prepared["state"] == "CARRY_HANDOFF_PREPARED"
            successor = config.owner_authority | {"revision": 3, "policy_sha256": "c" * 64}
            write_config(path, replace(config, owner_authority=successor))
            code, result = cli("owner-handoff")
            assert code == 0 and result["state"] == "ACTIVE" and result["authority"] == successor
            with Spool(replace(config, owner_authority=successor)) as spool:
                new = OwnerRuntime(spool)
                records = new.verify()
                assert records[-1]["kind"] == "OWNER_REBOUND"
                assert records[-1]["data"]["prepared_sha256"] == journal.digest(records[-2])
                assert records[-2]["kind"] == "OWNER_CARRY_HANDOFF_PREPARED"
                assert tuple(spool.db.execute("SELECT state,body FROM inbox").fetchone()) == (
                    "closed", None)
                assert not spool.db.execute("SELECT 1 FROM window_attempts").fetchone()
            # A new authenticated native capture is independent of the retained old input.
            new_capture = capture(replace(config, owner_authority=successor), 2)
            assert new_capture["authority"] == successor
            write_config(path, config)
            count = len(calls)
            code, result = cli("owner-handoff-prepare")
            assert code == 1 and result["state"] == "STOPPED" and len(calls) == count
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert not errors and not thread.is_alive()
    assert set(calls) == {"/internal/v1/owner-authority/" + item for item in (
        "stop-receipt", "model-carry-receipt", "inputs/state", "check")}
