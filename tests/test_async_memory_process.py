"""Real TCP ingress and a separate native worker process, without a real provider or sender."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from companion.contracts import Candidate
from companion.store import encode
from tests.conftest import run
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("withdraw", [False, True])
def test_tcp_input_independent_worker_process_and_source_fence(core, tmp_path, withdraw):
    settings_path = tmp_path / "worker-settings.json"
    workspace = Path(__file__).resolve().parents[1]

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory")) as (entry, _):
            client = entry.client
            source = await entry.admit(1, "青禾项目使用蓝色配色")
            with core.store.transaction():
                mid = core.memory.add(Candidate(subject="project", kind="fact", fact_key="color",
                    content=source["payload"]["text"], evidence_event_id=source["event_id"],
                    evidence_quote=source["payload"]["text"]), core.clock(), confirmed=True)
            question = await entry.admit(2, "青禾项目使用什么配色")
            config = core.settings.model_dump(mode="json")
            # Public synthetic fixture credentials, never production settings.
            for name in ("model_key", "bridge_token", "control_token"):
                config[name] = getattr(core.settings, name).get_secret_value()
            await asyncio.to_thread(settings_path.write_text, encode(config), encoding="utf-8")
            script = (
                "import sys; from tests.support.memory_process import run_worker; "
                "run_worker(sys.argv[1], sys.argv[2])"
            )
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-c",
                script,
                str(settings_path),
                core.clock(),
                cwd=workspace,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                while True:
                    # Allow cold SDK/model initialization; HTTP liveness is checked after readiness.
                    line = await asyncio.wait_for(child.stdout.readline(), 60)
                    assert line, (await child.stderr.read()).decode("utf-8", errors="replace")
                    message = json.loads(line)
                    assert message.get("phase") != "worker_result", message
                    if message.get("phase") == "inference_wait":
                        break
                worker_pid = message["pid"]
                assert message["inference_thread"] != message["owner_thread"], message
                assert worker_pid != os.getpid() and child.pid != os.getpid()
                # Windows venv python.exe redirects to a second native Python process.
                assert child.pid in {worker_pid, message["parent_pid"]}
                assert (await client.get("/internal/v1/health/live")).status_code == 200
                if withdraw:
                    revoked = await client.post(
                        f"/internal/v1/memories/{mid}/decisions",
                        json={"actor_id": "owner", "decision": "forget"},
                    )
                    assert revoked.status_code == 200, revoked.text
                child.stdin.write(b"continue\n")
                await child.stdin.drain()
                stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
                assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
                result = json.loads(stdout.decode("utf-8").splitlines()[-1])
                assert result["phase"] == "finished" and result["pid"] == worker_pid
                assert result["inference_threads"]
                assert all(tid != result["owner_thread"] for tid in result["inference_threads"])
                assert result["store_thread_violations"] == []
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.communicate()
            job = core.store.one(
                "SELECT state,last_error_code FROM jobs WHERE event_id=?", (question["event_id"],),
            )
            if withdraw:
                assert result["calls"] == []
                assert job == {"state": "CANCELLED", "last_error_code": "REPLY_CANCELLED"}
                # The first source check now occurs before any paid chat admission.
                assert not core.store.rows("SELECT * FROM model_usage")
                assert not core.store.rows("SELECT * FROM memory_vectors")
                assert not core.store.rows("SELECT * FROM outbox")
            else:
                assert result["calls"] == ["chat", "chat"]
                assert job["state"] == "SUCCEEDED"
                usage = core.store.rows("SELECT scenario,state,billed_micro_usd FROM model_usage")
                assert [row["scenario"] for row in usage] == ["chat", "chat"]
                assert all(
                    row["state"] == "SETTLED" and row["billed_micro_usd"] > 0 for row in usage
                )
                queued = core.store.rows("SELECT origin_event_id,state FROM outbox")
                assert queued == [{"origin_event_id": question["event_id"], "state": "PENDING"}]

    run(scenario())
