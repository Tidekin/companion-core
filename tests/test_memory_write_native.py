"""Real owner endpoints, independent workers and native saved SDK source custody."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai.toolsets import FunctionToolset

from companion import agent_runtime
from companion.agent_persistence import ConversationStore
from companion.api import create_app
from companion.contracts import Candidate
from companion.core import Core
from companion.memory_work import checkpoint_state
from companion.model import ModelClient
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call
from tests.support.owner_memory import environment
from tests.test_console_cognition import ORIGIN, PASSWORD, login
from tests.test_memory_write_consistency import CORRECTION, candidate, failed_semantic_batch


@pytest.fixture
def client(core):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    app = create_app(core.settings)
    with TestClient(app, base_url=ORIGIN) as http:
        app.state.core.clock = core.clock
        yield http


def test_semantic_recovery_http_then_independent_worker_reextracts_and_commits(
    client, core, tmp_path,
):
    record = failed_semantic_batch(core)
    path = "/v1/console/memories/failures/" + record["batch_id"] + "/retry"
    headers = login(client) | {"Idempotency-Key": "semantic-owner-repair"}
    response = client.post(path, json={"confirm": True}, headers=headers)
    assert response.status_code == 200, response.text
    assert checkpoint_state(core.store)["batch"]["phase"] == "EXTRACT"
    values = core.settings.model_dump(mode="json")
    values.update(bridge_token=core.settings.bridge_token.get_secret_value(),
                  control_token=core.settings.control_token.get_secret_value())
    config = tmp_path / "worker-config.json"
    config.write_text(encode(values), "utf-8")
    payload = tmp_path / "corrected-output.json"
    payload.write_text(encode({"memories": [candidate(
        CORRECTION, update="replaces_previous").model_dump(mode="json")]}), "utf-8")
    script = """
import json,os,sys
from pathlib import Path
from companion.config import Settings
from companion.core import Core
from tests.conftest import FakeModel,run
cfg=Settings.model_validate_json(Path(sys.argv[1]).read_text('utf-8'))
model=FakeModel()
model.extraction=Path(sys.argv[3]).read_text('utf-8')
model.review=lambda data:json.dumps({'relations':[{'supported':True,'update_authorized':True,
 'claims_replacement':True,'same':None,'conflicts':['m0'],'replaces':['m0']}]})
with Core(cfg,model,lambda:sys.argv[2]) as worker:
 assert run(worker.extract_step()).progressed
 assert run(worker.extract_step()).progressed
 print(json.dumps({'pid':os.getpid(),'calls':[phase for _,phase in model.calls]}))
"""

    workspace = Path(__file__).resolve().parents[1]

    async def consume():
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", script,
            str(config), core.clock(), str(payload), cwd=workspace,
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
    assert result["pid"] != os.getpid()
    assert result["calls"] == ["memory_extract", "memory_review"]
    assert checkpoint_state(core.store)["batch"]["phase"] == "COMPLETE"
    rows = core.store.rows("SELECT valid_to,content FROM memories ORDER BY rowid")
    assert len(rows) == 2 and rows[0]["valid_to"] and rows[1]["content"] == CORRECTION
    assert all(r["state"] == "SETTLED" for r in core.store.rows("SELECT * FROM model_usage"))


def test_nonrepeat_boundary_survives_restart_tools_and_forget_erases_source(core, tmp_path):
    secret, boundary = "私下称呼是青瓷叶。", "不要再复述私下称呼的具体内容，除非我明确解除。"

    async def scenario():
        async with environment(core, tmp_path) as (admit, _, _):
            core.settings.max_recent_messages = 0
            ids = []
            for n, (text, kind) in enumerate(((secret, "fact"), (boundary, "boundary")), 1):
                source, accepted = await admit(n, text)
                ids.append(core.memory.add(Candidate(subject="user", kind=kind,
                    fact_key=f"private-name-{n}", content=text, evidence_event_id=source.event_id,
                    evidence_quote=text), core.clock()))
                core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?",
                                   (accepted["job_id"],))
            await admit(3, "那个私下称呼是什么？")
            provider = Provider(answer(calls=[call("boundary-once", "memory_recall", {})]),
                                answer("之前约好不复述具体内容，我先守着这个约定。"))

            def memory_recall() -> dict:
                """Return the retained boundary from the synthetic native tool transport."""
                return {"state": "FOUND", "facts": [{"content": boundary}]}

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    job = worker.claim_job("nonrepeat-restarted")
                    snapshots = []
                    prefix = worker.context(job, memories=worker.memory.search("私下称呼"),
                                            snapshots=snapshots, planning=True, turn_ranking=[])
                    persistence = ConversationStore(worker, job, snapshots)
                    result = await agent_runtime.run_conversation(worker, job, prefix,
                        toolsets=[FunctionToolset([memory_recall])], snapshots=snapshots,
                        persistence=persistence)
                    assert "青瓷叶" not in result.output
                    assert len(provider.requests) == 2
                    for request in provider.requests:
                        prompt = encode(request["messages"])
                        assert boundary in prompt and "普通追问不解除约定" in prompt
                        assert "不复述不等于已经删除" in prompt
                    worker.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?",
                                         (job["job_id"],))
                    await admit(4, "/忘记这一条 " + ids[0])
                    assert await worker.run_once("explicit-forget")
                    assert len(provider.requests) == 2  # Forget is a native control, not model I/O.
                    assert worker.store.one("SELECT content,status FROM memories WHERE memory_id=?",
                                            (ids[0],)) == {"content": "", "status": "FORGOTTEN"}
                    assert worker.store.one("SELECT content FROM memories WHERE memory_id=?",
                                            (ids[1],))["content"] == boundary
                    assert worker.store.one("SELECT conversation_json FROM agent_conversations "
                                            "WHERE job_id=?", (job["job_id"],)) == {
                                                "conversation_json": None}
    run(scenario())
