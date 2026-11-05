"""Real captured history stays bound across SDK processes and checked delivery."""

import asyncio
import json
import pickle
import sys
from pathlib import Path

import httpx
import pytest

from companion import paid_budget
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer, worker_for
from tests.support.native_context import source_window
from tests.support.onebot_events import raw_event
from tests.test_qq_rich_input import card


async def sdk_step(core, tmp_path, event_id, index):
    job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?", (event_id,))
    config, output = tmp_path / f"history-{index}.pickle", tmp_path / f"history-{index}.json"
    config.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
        [answer("原始历史答复")], "SUCCEEDED", str(output))))
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).with_name("reader_sdk_worker.py")), str(config),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        logs, _ = await asyncio.wait_for(process.communicate(), 45)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, logs.decode(errors="replace")
    requests = json.loads(output.read_text(encoding="utf-8"))
    assert len(requests) == 1
    return job["job_id"], requests[0]


def change_input(core, event_id):
    row = core.store.one("SELECT body_json FROM events WHERE event_id=?", (event_id,))
    body = json.loads(row["body_json"])
    body["rich"]["segments"][0]["fields"][0]["value"] = "changed historical source"
    core.store.execute("UPDATE events SET body_json=? WHERE event_id=?", (encode(body), event_id))


@pytest.mark.parametrize("boundary", ["input", "reply", "snapshot", "provider"])
def test_original_history_changes_never_send_or_repeat_model_work(core, tmp_path, boundary):
    raw = raw_event(message=[{"type": "text", "data": {"text": "先前的问题"}}, card()])

    async def source(ws):
        await ws.send(encode(raw))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, rich_input_scope="references_v1",
            scope_options={"max_history_utf8_bytes": 8000,
                           "core_options": {"context_soft_tokens": 6000}}) as (
                window, client, protocol, _, sends):
            await window.capture_once(client, protocol)
            await window.runtime.flush_one(client)
            first = core.store.one("SELECT event_id FROM events")["event_id"]
            await sdk_step(core, tmp_path, first, 1)
            await window.send(client, protocol)
            assert len(sends) == 1
            raw["message_id"] += 1
            raw["message"] = [{"type": "text", "data": {"text": "接着聊刚才的问题"}}]
            await window.capture_once(client, protocol)
            await window.runtime.flush_one(client)
            current = core.store.one("SELECT event_id FROM events ORDER BY rowid DESC")["event_id"]
            calls = []
            if boundary == "provider":
                def provider(request):
                    calls.append(json.loads(request.content))
                    assert "QQ引用资料" in encode(calls[-1]["messages"])
                    change_input(core, first)
                    return httpx.Response(200, json=answer("不能发送的旧来源答复"))

                async with worker_for(core, provider) as worker:
                    assert await worker.run_once("history-changed-during-model")
                    assert not await worker.run_once("history-no-model-replay")
                assert len(calls) == 1
                assert core.store.one("SELECT state FROM jobs WHERE event_id=?",
                                      (current,))["state"] == "CANCELLED"
                assert not core.store.one("SELECT 1 FROM outbox WHERE origin_event_id=?",
                                          (current,))
            else:
                job_id, request = await sdk_step(core, tmp_path, current, 2)
                assert "QQ引用资料" in encode(request["messages"])
                checkpoint = core.store.one("SELECT snapshots_json FROM agent_conversations "
                                             "WHERE job_id=?", (job_id,))
                snapshots = json.loads(checkpoint["snapshots_json"])
                history = next(item["proof"] for item in snapshots if item["kind"] == "history")
                assert [item["event_id"] for item in history] == [first]
                if boundary == "input":
                    change_input(core, first)
                elif boundary == "reply":
                    content = {"text": "changed already delivered reply"}
                    core.store.execute("UPDATE outbox SET content_json=?,content_sha256=? "
                        "WHERE origin_event_id=?",
                        (encode(content), paid_budget.sha(content), first))
                else:
                    core.store.execute("UPDATE agent_conversations SET snapshots_json=? "
                        "WHERE job_id=?", (encode([item for item in snapshots
                                                  if item["kind"] != "history"]), job_id))
                usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
                await window.send(client, protocol)
                await window.send(client, protocol)
                assert core.store.one("SELECT state FROM outbox WHERE origin_event_id=?",
                                      (current,))["state"] == "CANCELLED"
                assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert len(sends) == 1
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage ORDER BY rowid")
            assert len(usage) == 2 and all(row["state"] == "SETTLED" for row in usage)
            assert all(row["billed_micro_usd"] > 0 for row in usage)
    run(scenario())


@pytest.mark.parametrize("padding", ["leading", "trailing"])
def test_large_journal_enters_independent_worker_and_checked_delivery(core, tmp_path, padding):
    from tests.support.native_consumer_entry import environment

    async def scenario():
        async with environment(core, tmp_path) as (entry, sends):
            path = Path(core.settings.paid_model.journal_path)
            raw = await asyncio.to_thread(path.read_bytes)
            whitespace = b" " * (paid_budget.CACHE_MAX_BYTES + 1)
            large = (whitespace + raw if padding == "leading"
                     else raw.rstrip(b"\n") + whitespace + b"\n")
            await asyncio.to_thread(path.write_bytes, large)
            paid_budget.forget_views(core.store)
            source = await entry.admit(1, "合成大账本输入")
            await sdk_step(core, tmp_path, source["event_id"], 1)
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == 1
            assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
            assert core.store.rows("SELECT state FROM outbox") == [{"state": "SENT"}]

    run(scenario())
