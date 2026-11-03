"""Owner-session recovery through HTTP, consumed by an independent native worker."""

import asyncio
import json
import os
import sys
from pathlib import Path

from companion.memory_work import checkpoint_state
from tests.conftest import run
from tests.test_console_cognition import ORIGIN, login
from tests.test_console_cognition import client as client
from tests.test_memory_failure_recovery import failed_batch


def test_owner_http_recovery_and_separate_worker_save_once(client, core, tmp_path):
    _, record = failed_batch(core)
    batch_id = record["batch_id"]
    path = "/v1/console/memories/failures"
    assert client.get(path).status_code == 401
    headers = login(client)
    assert client.get(path).json()["items"][0]["entry_id"] == batch_id
    retry_path = path + "/" + batch_id + "/retry"
    assert client.post(retry_path, json={"confirm": True}, headers={"Origin": ORIGIN,
        "Idempotency-Key": "recovery-owner-1"}).status_code == 403
    headers["Idempotency-Key"] = "recovery-owner-1"
    reply = client.post(retry_path, json={"confirm": True}, headers=headers)
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"entry_id": batch_id, "state": "QUEUED"}
    assert client.post(retry_path, json={"confirm": True}, headers=headers).json() == reply.json()
    config = tmp_path / "worker.json"
    settings = core.settings.model_dump(mode="json")
    settings.update(bridge_token=core.settings.bridge_token.get_secret_value(),
                    control_token=core.settings.control_token.get_secret_value())
    config.write_text(json.dumps(settings), encoding="utf-8")
    workspace = Path(__file__).resolve().parents[1]

    async def consume():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c",
            "import json,os,sys; from pathlib import Path; "
            "from companion.config import Settings; from companion.core import Core; "
            "from tests.conftest import FakeModel,run; "
            "cfg=Settings.model_validate_json(Path(sys.argv[1]).read_text('utf-8')); "
            "model=FakeModel(); core=Core(cfg,model,lambda:sys.argv[2]); "
            "print(json.dumps({'pid':os.getpid(),'progressed':run(core.extract_step()).progressed,"
            "'calls':[s for _,s in model.calls]})); core.store.close()",
            str(config), core.clock(), cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        except BaseException:
            process.kill()
            await process.communicate()
            raise
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        return json.loads(stdout.decode("utf-8").splitlines()[-1])

    result = run(consume())
    assert result["pid"] != os.getpid() and result["progressed"]
    assert result["calls"] == ["memory_review"]
    assert checkpoint_state(core.store)["batch"]["phase"] == "COMPLETE"
    assert len(core.store.rows("SELECT * FROM memories")) == 2
    assert client.get(path).json()["items"][0]["state"] == "COMPLETE"
    headers["Idempotency-Key"] = "recovery-owner-2"
    assert client.post(retry_path, json={"confirm": True}, headers=headers).status_code == 409
