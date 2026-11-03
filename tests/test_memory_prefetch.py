"""Signed HTTP input, independent worker, first request recall, erasure and accounting."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from companion import agent_chat, reply_revision
from companion.agent_persistence import ConversationStore
from companion.contracts import Candidate, CoreError
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call, worker_for
from tests.support.native_consumer_entry import environment

ROOT = Path(__file__).resolve().parents[1]


def test_prefetch_enters_first_request_without_tool_and_erases_with_source(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory", "history")) as (
            entry, sends,
        ):
            text = "葭岸项目的样机编号是GL-84。"
            original = await entry.admit(1, text)
            async with worker_for(core, lambda request: httpx.Response(
                200, json=answer("收到这条项目资料。"))) as worker:
                assert await worker.run_once("source-before-prefetch")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == 1
            with core.store.transaction():
                mid = core.memory.add(Candidate(subject="project", kind="fact",
                    fact_key="model_number", content=text,
                    evidence_event_id=original["event_id"], evidence_quote=text), core.clock())
            core.settings.max_recent_messages = 0
            fresh = await entry.admit(2, "葭岸项目的样机编号是多少？")
            job_id = core.store.one("SELECT job_id FROM jobs WHERE event_id=?",
                                   (fresh["event_id"],))["job_id"]
            config = core.settings.model_dump(mode="json")
            for key in ("model_key", "bridge_token", "control_token"):
                config[key] = getattr(core.settings, key).get_secret_value()
            settings_path = tmp_path / "prefetch-worker.json"
            settings_path.write_text(encode(config), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
                "import sys; from tests.support.memory_prefetch_process import run_worker; "
                "run_worker(*sys.argv[1:])", str(settings_path), core.clock(), job_id, "GL-84",
                cwd=ROOT,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
                assert child.returncode == 0, stderr.decode(errors="replace")
                receipt = json.loads(stdout.decode().splitlines()[-1])
                assert receipt["pid"] != os.getpid() and receipt["provider_calls"] == 1
                queries = receipt["query_inferences"]
                assert queries.count("葭岸项目的样机编号是多少？") == 1
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.communicate()
            saved = core.store.one("SELECT * FROM agent_conversations WHERE job_id=?", (job_id,))
            snapshots = json.loads(saved["snapshots_json"])
            trace = next(row["retrieval"] for row in snapshots if row.get("retrieval"))
            assert trace["stage"] == "prefetch" and len(trace["query_sha256"]) == 64
            assert set(trace["selection"]["selected"]) == {mid}
            assert "葭岸项目的样机编号是多少" not in encode(trace)
            assert not core.store.rows("SELECT * FROM agent_tool_calls WHERE job_id=?", (job_id,))
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            assert len(usage) == 2 and all(row["state"] == "SETTLED" for row in usage)
            with core.store.transaction():
                core.ledger.expire_events(core.store, [original["event_id"]], core.clock())
            saved = core.store.one("SELECT * FROM agent_conversations WHERE job_id=?", (job_id,))
            assert saved["conversation_json"] is None and saved["snapshots_json"] == "[]"
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert not core.memory.search("葭岸项目")

    run(scenario())


@pytest.mark.parametrize("memory_granted,memory_enabled", [(False, True), (True, False)])
def test_prefetch_requires_memory_grant_and_enabled_setting(
    core, tmp_path, monkeypatch, memory_granted, memory_enabled,
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Unpermitted memory read")

    async def scenario():
        async with environment(core, tmp_path,
            purposes=("chat", "memory") if memory_granted else ("chat",)) as (entry, _):
            core.settings.memory_enabled = memory_enabled
            source = await entry.admit(1, "你记得我的项目编号吗？")
            async with worker_for(core, lambda request: httpx.Response(
                200, json=answer("目前没有可用记录。"))) as worker:
                monkeypatch.setattr(worker.memory_context, "recall_async", forbidden)
                assert await worker.run_once("no-memory-read")
            job = core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                                  (source["event_id"],))
            assert job["state"] == "SUCCEEDED", job
            assert not core.store.rows("SELECT * FROM memory_vectors")
            assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 1
    run(scenario())


@pytest.mark.parametrize("error,state,stored_code", [
    ("PRIVACY_CHANGED", "CANCELLED", reply_revision.CANCELLED),
    ("MEMORY_INDEX_UNAVAILABLE", "RETRY_WAIT", "MEMORY_INDEX_UNAVAILABLE"),
])
def test_invalidated_prefetch_cancels_and_unavailable_index_is_not_empty_history(
    core, tmp_path, monkeypatch, error, state, stored_code,
):
    async def invalidated(*args, **kwargs):
        raise CoreError(error, 409)

    def forbidden(request):
        raise AssertionError("A failed recall must not admit a provider request")

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory")) as (entry, _):
            source = await entry.admit(1, "你记得我的项目编号吗？")
            async with worker_for(core, forbidden) as worker:
                monkeypatch.setattr(worker.memory_context, "recall_async", invalidated)
                assert await worker.run_once("invalidated-prefetch")
            row = core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                                  (source["event_id"],))
            assert row == {"state": state, "last_error_code": stored_code}
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM agent_conversations")
    run(scenario())


def test_restored_conversation_does_not_repeat_prefetch_or_paid_request(
    core, tmp_path, monkeypatch,
):
    class SimulatedExit(BaseException):
        pass

    save = ConversationStore.after_model_request

    async def stop_after_save(self, ctx, *, request_context, response):
        await save(self, ctx, request_context=request_context, response=response)
        raise SimulatedExit

    async def forbidden(*args, **kwargs):
        raise AssertionError("Restoration must use its pinned retrieval")

    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json=answer("已保存的回复。"))

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory")) as (entry, _):
            await entry.admit(1, "你记得我的项目编号吗？")
            async with worker_for(core, provider) as worker:
                job = worker.claim_job("prefetch-restore")
                monkeypatch.setattr(ConversationStore, "after_model_request", stop_after_save)
                with pytest.raises(SimulatedExit):
                    await agent_chat.reply(worker, job, [])
                saved = worker.store.one("SELECT snapshots_json FROM agent_conversations")
                assert len([s for s in json.loads(saved["snapshots_json"])
                            if s.get("retrieval", {}).get("stage") == "prefetch"]) == 1
                monkeypatch.setattr(worker.memory_context, "recall_async", forbidden)
                monkeypatch.setattr(ConversationStore, "after_model_request", save)
                output, persistence = await agent_chat.reply(worker, job, [])
                assert output == "已保存的回复。" and persistence.row()["phase"] == "OUTPUT"
                assert persistence.row()["snapshots_json"] == saved["snapshots_json"]
            assert len(calls) == 1
            assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 1
    run(scenario())


@pytest.mark.parametrize("erase_after_tool", [False, True])
def test_raw_tool_fallback_uses_original_sources_and_erasure_fence(
    core, tmp_path, erase_after_tool,
):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory", "history")) as (
            entry, _sends,
        ):
            original = await entry.admit(1, "葭岸项目的样机编号是GL-84。")
            response = lambda _: httpx.Response(200, json=answer("知道了。"))  # noqa: E731
            async with worker_for(core, response) as w:
                assert await w.run_once("raw-source")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert not core.store.rows("SELECT * FROM memories")
            fresh = await entry.admit(2, "葭岸项目的样机编号是多少？")
            calls = []

            def provider(request):
                payload = json.loads(request.content)
                calls.append(payload)
                if len(calls) == 1:
                    return httpx.Response(200, json=answer(calls=[call(
                        "raw-lookup", "memory_recall", {"query": "葭岸项目样机编号"})]))
                tool = next(m for m in payload["messages"] if m.get("role") == "tool")
                result = json.loads(tool["content"])
                assert "GL-84" in json.dumps(result["raw_turns"], ensure_ascii=False)
                if erase_after_tool:
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [original["event_id"]], core.clock())
                return httpx.Response(200, json=answer("编号是GL-84。"))

            async with worker_for(core, provider) as worker:
                assert await worker.run_once("raw-fallback")
            assert len(calls) == 2
            outbox = core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                     (fresh["event_id"],))
            assert bool(outbox) is not erase_after_tool
            assert all(row["state"] == "SETTLED" for row in core.store.rows(
                "SELECT state FROM model_usage"))

    run(scenario())
