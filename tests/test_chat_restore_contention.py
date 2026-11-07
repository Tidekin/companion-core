"""Reader contention resumes the original SDK checkpoint without repeating I/O."""

import asyncio
import json
import pickle
import sys
from pathlib import Path

import psycopg
import pytest

from companion import agent_chat, reply_revision
from companion.agent_persistence import ConversationStore
from tests.conftest import run
from tests.postgres_fixture import logical_state
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.reading_followup import READER_AUTH, admit, lease, window
from tests.support.reading_protocol import envelope
from tests.support.reply_revision import accept


def test_recorded_sources_revalidate_without_competing_for_the_writer(core):
    accepted = accept(core)
    job = core.claim_job("source-worker")
    reply_revision.remember_sources(core.store, job, [])
    before = logical_state(core.store)
    with psycopg.connect(core.settings.database.get_secret_value()) as writer:
        writer.execute("LOCK TABLE job_sources IN SHARE MODE")
        core.store.execute("SET lock_timeout='100ms'")
        try:
            reply_revision.remember_sources(core.store, job, [accepted["event_id"]])
        finally:
            core.store.execute("RESET lock_timeout")
    assert logical_state(core.store) == before
    assert not core.store.in_transaction
    assert core.store.one("SHOW transaction_read_only")["transaction_read_only"] == "off"


@pytest.mark.parametrize("failure,boundary", [
    ("busy", "load"), ("busy", "wait"), ("recalled", "load"), ("schema", "load"),
])
def test_deferred_reader_restore_handles_only_real_busy_and_resumes_once(
    core, tmp_path, monkeypatch, failure, boundary,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, grant):
            event, accepted = await admit(client, core, config, grant, 1, text="去刷会B站")
            selected = Provider(answer(calls=(call("discover-original", "discover_content",
                                                   {"source": "bilibili"}),)))
            async with worker_for(core, selected) as worker:
                assert await worker.run_once("select-discovery", job_id=accepted["job_id"])
            item = await lease(client)
            assert item is not None
            load = ConversationStore.load
            wait = agent_chat.wait
            attempts = []

            def contend(persistence):
                attempts.append(1)
                if len(attempts) == 1:
                    try:
                        with psycopg.connect(core.settings.database.get_secret_value()) as writer:
                            writer.execute("LOCK TABLE agent_conversations IN SHARE MODE")
                            if failure == "schema":
                                persistence.store.execute("SELECT * FROM missing_restore_table")
                            with persistence.store.transaction():
                                persistence.store.execute(
                                    "LOCK TABLE agent_conversations IN ROW EXCLUSIVE MODE NOWAIT"
                                )
                    finally:
                        if failure == "recalled":
                            core.store.execute("UPDATE events SET redacted=1 WHERE event_id=%s",
                                               (event["event_id"],))

            def restore(persistence):
                if boundary == "load":
                    contend(persistence)
                return load(persistence)

            def waiting(instance, job, persistence):
                if boundary == "wait":
                    contend(persistence)
                return wait(instance, job, persistence)

            provider = Provider()
            core.clock.advance(2)
            async with worker_for(core, provider) as worker:
                with monkeypatch.context() as patch:
                    patch.setattr(ConversationStore, "load", restore)
                    patch.setattr(agent_chat, "wait", waiting)
                    assert await worker.run_once("contended-restore", job_id=accepted["job_id"])
            assert provider.requests == []
            state = core.store.one((
                'SELECT state,last_error_code,attempts FROM jobs WHERE '
                'job_id=%s'
            ), (accepted["job_id"],))
            if failure == "schema":
                assert len(attempts) == 1
                assert state["state"] == "FAILED"
                assert state["last_error_code"] == "INTERNAL_ERROR"
                return
            if failure == "recalled":
                assert state["state"] == "CANCELLED", state
                assert not core.store.rows("SELECT * FROM outbox")
                return
            assert len(attempts) == 2
            assert state["state"] == "RETRY_WAIT", state
            assert state["attempts"] == 0
            assert core.store.one('SELECT phase FROM agent_conversations WHERE job_id=%s',
                                  (accepted["job_id"],))["phase"] == "WAITING"
            assert not core.store.rows("SELECT * FROM outbox")
            assert len(selected.requests) == 1
            body = envelope("bilibili", discovered=True)
            body["lease_token"] = item["lease_token"]
            body["observations"][0]["provenance"] = item["context"]
            body["observations"][0]["fetched_at"] = core.clock()
            completed = await client.post(
                "/internal/v3/readers/bili/" + item["request_id"] + "/result",
                json=body, headers=READER_AUTH)
            assert completed.status_code == 200, completed.text
            assert completed.json()["state"] == "DONE"

            output = tmp_path / "provider-requests.json"
            request = tmp_path / "worker.pickle"
            request.write_bytes(pickle.dumps((core.settings, core.clock.now,
                accepted["job_id"], [answer("读取完成，正常回复。")], "SUCCEEDED", str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable, "-B",
                str(Path(__file__).with_name("reader_sdk_worker.py")), str(request),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 45)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, stdout.decode(errors="replace")
            requests = json.loads(output.read_text(encoding="utf-8"))
            assert len(requests) == 1
            assert any(m["role"] == "tool" for m in requests[0]["messages"])
            assert core.store.one("SELECT COUNT(*) AS n FROM exploration_requests")["n"] == 1
            calls = core.store.rows((
                'SELECT tool_name,state FROM agent_tool_calls WHERE job_id=%s'
            ), (accepted["job_id"],))
            assert calls == [{"tool_name": "discover_content", "state": "DEFERRED"}]
            usage = core.store.rows('SELECT state FROM model_usage WHERE job_id=%s',
                                    (accepted["job_id"],))
            assert len(usage) == 2 and all(r["state"] == "SETTLED" for r in usage)
            replies = core.store.rows('SELECT content_json FROM outbox WHERE origin_event_id=%s',
                                      (event["event_id"],))
            assert len(replies) == 1
            assert json.loads(replies[0]["content_json"])["text"] == "读取完成，正常回复。"
    run(scenario())
