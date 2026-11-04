"""Real HTTP v1 acceptance followed by the independent life-worker process."""

import asyncio
import json
import subprocess
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from companion import life_worker
from companion.config import Settings
from companion.core import Core
from companion.image_input import ImageInputPolicy
from companion.memory_context import MemoryContext
from companion.model import EXTRACT_RULES, REVIEW_RULES
from companion.store import encode
from companion_onebot_bridge import pending_media
from tests.conftest import run
from tests.sdk_reader_fixture import worker_for
from tests.support.native_consumer_entry import environment
from tests.test_live_image_input import png
from tests.test_onebot_images import image_segment


def provider_response(text):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 0},
    })


def run_isolated_worker(settings_path, now):
    """Child process: actual loop and Core/store, fixed time, only provider HTTP mocked."""
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        if payload["messages"][0]["content"] == REVIEW_RULES:
            calls.append("memory_review")
            candidates = json.loads(payload["messages"][-1]["content"])["candidates"]
            return provider_response(encode({"relations": [{"supported": True,
                "update_authorized": False, "claims_replacement": False,
                "same": None, "conflicts": [], "replaces": []} for _ in candidates]}))
        assert payload["messages"][0]["content"] == EXTRACT_RULES
        assert payload["max_tokens"] == min(4096, settings.paid_model.max_output_tokens)
        calls.append("memory_extract")
        sources = json.loads(payload["messages"][-1]["content"])
        source = next((row for row in sources["events"]
                       if row["text"] == "青禾项目下一步检查配色"), None)
        candidates = [{
            "subject": "project", "kind": "fact", "fact_key": "next_step",
            "content": "青禾项目下一步检查配色", "evidence_event_id": source["ref"],
            "evidence_quote": "青禾项目下一步检查配色",
        }] if source else []
        return provider_response(encode({"memories": candidates}))

    original_client = httpx.AsyncClient

    def offline_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original_client(**kwargs)

    async def stop(*_args):
        raise asyncio.CancelledError

    life_worker.Core = partial(Core, clock=lambda: now)
    life_worker.httpx = SimpleNamespace(AsyncClient=offline_client)
    life_worker.Wakeup.wait = stop
    try:
        asyncio.run(life_worker.life_worker(settings))
    except asyncio.CancelledError:
        pass
    print(encode({"calls": calls}))


@pytest.mark.parametrize("batch,output_ceiling", [
    ("text", 4096), ("text", 1024), ("mixed_image", 4096),
    ("mixed_media_caption", 4096), ("only_media", 4096),
])
def test_http_acceptance_independent_worker_and_restart(core, tmp_path, batch, output_ceiling):
    workspace = Path(__file__).resolve().parents[1]
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory", "image", "content"),
            paid_options={"max_output_tokens": output_ceiling,
                "image_input": ImageInputPolicy(approval_sha256="e" * 64,
                estimated_tokens_per_image=100, max_bytes_per_image=50000,
                max_pixels_per_image=100)}, core_options={
            "extraction_min_events": 1, "extraction_idle_seconds": 0,
            "content_enabled": True, "observation_enabled": True,
        }) as (entry, sends):
            async def resolver(host, port):
                return ["1.1.1.1"]

            def cdn(request):
                return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

            messages = [[{"type": "text", "data": {"text": "青禾项目下一步检查配色"}}]]
            if batch != "text":
                messages.append([*([{"type": "text", "data": {"text": "配色参考图"}}]
                    if batch == "mixed_media_caption" else []), image_segment()])
            if batch == "only_media":
                messages[0] = [{"type": "record", "data": {
                    "file": "synthetic", "url": "https://media.example.test/voice.amr"}}]
            sources = []
            async with worker_for(core, lambda _: provider_response(
                "好，下次继续检查配色。")) as worker:
                for number, message in enumerate(messages, 1):
                    if number > 1:
                        core.clock.advance(1)
                    prepared = await entry.capture(number, message=message)
                    source = prepared["event"]
                    denied = await entry.client.post("/internal/v1/inbox/events", json=source,
                        headers={"Authorization": "Bearer wrong",
                                 "X-Idempotency-Key": source["event_id"]})
                    assert denied.status_code == 401
                    assert await entry.runtime.flush_one(entry.client)
                    if prepared["media"]["images"]:
                        row = pending_media._execute(entry.runtime,
                            "SELECT * FROM automatic_media WHERE event_id=?",
                            (source["event_id"],)).fetchone()
                        pump = pending_media.MediaPump(entry.runtime, entry.client,
                            image_transport=httpx.MockTransport(cdn), image_resolver=resolver)
                        await pump.execute(row)
                        assert not pump.pending(source["event_id"])
                    sources.append(source)
                    assert await worker.run_once("native-history-before-extraction")
                    assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == len(messages)
            assert all(row["state"] == "SUCCEEDED" for row in core.store.rows(
                "SELECT state FROM jobs"))
            assert not core.store.rows("SELECT * FROM episodes")
            chat_usage = core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid")
            assert chat_usage == [{"scenario": "chat", "state": "SETTLED"}] * len(messages)
            config = core.settings.model_dump(mode="json")
            for name in ("model_key", "bridge_token", "control_token"):
                config[name] = getattr(core.settings, name).get_secret_value()
            settings_path = tmp_path / "worker-settings.json"
            await asyncio.to_thread(settings_path.write_text, encode(config), encoding="utf-8")
            script = ("import sys; from tests.test_next_memory_worker import run_isolated_worker; "
                      "run_isolated_worker(sys.argv[1], sys.argv[2])")
            for attempt in range(3):
                completed = await asyncio.to_thread(subprocess.run,
                    [sys.executable, "-B", "-c", script, str(settings_path), core.clock()],
                    cwd=workspace, capture_output=True, text=True,
                    encoding="utf-8", timeout=30, check=True)
                result = json.loads(completed.stdout.splitlines()[-1])
                expected = (["memory_extract", *(["memory_review"]
                            if batch != "only_media" else [])] if attempt == 0 else [])
                assert result["calls"] == expected
            row = core.store.one("SELECT * FROM memories")
            if batch == "only_media":
                assert row is None
            else:
                assert row["status"] == "HYPOTHESIS" and row["content"] == "青禾项目下一步检查配色"
            assert all(row["memory_processed"] for row in core.store.rows("SELECT * FROM events"))
            assert not core.store.rows("SELECT * FROM audit_log WHERE action='memory.batch_failed'")
            assert not core.store.rows(
                "SELECT * FROM runtime_meta WHERE key LIKE 'extract_attempts:%'")
            assert core.store.one("SELECT COUNT(*) n FROM episodes")["n"] == int(
                batch != "only_media")
            assert core.store.one("SELECT COUNT(*) n FROM conversation_summaries")["n"] == int(
                batch != "only_media")
            view = MemoryContext(core.memory)
            conversation = core.store.one("SELECT conversation_id FROM conversations")
            continuation = view.continuation(conversation["conversation_id"], core.clock())
            expected_sources = ([] if batch == "only_media" else [source["event_id"] for source in (
                sources if batch == "mixed_media_caption" else sources[:1])])
            assert [excerpt["event_id"] for item in continuation["items"]
                    for excerpt in item["excerpts"]] == expected_sources
            assert {r["event_id"] for r in core.store.rows(
                "SELECT event_id FROM episode_events")} == set(expected_sources)
            assert core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid") == [
                *chat_usage, {"scenario": "memory_extract", "state": "SETTLED"},
                *([{"scenario": "memory_review", "state": "SETTLED"}]
                  if batch != "only_media" else [])]
            response = await entry.client.post("/internal/v1/memories/search", json={
                "query": "青禾项目", "requester_actor_id": "owner"})
            assert response.status_code == 200
            if row:
                assert row["memory_id"] in response.text
            else:
                assert response.json() == {"items": []}
    run(scenario())
