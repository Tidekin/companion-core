"""Real extraction/commit and authenticated HTTP plus the independent life worker."""

import asyncio
import json
import os
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
from companion.memory_work import checkpoint_state
from companion.store import encode
from tests.conftest import ingest, run
from tests.sdk_reader_fixture import worker_for
from tests.support.native_consumer_entry import environment
from tests.test_mood import erase
from tests.test_next_memory_worker import provider_response


def prepare_extraction(core, *, quote="想你了", mood_enabled=True):
    core.settings.memory_enabled = True
    core.settings.mood_enabled = mood_enabled
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    key = ingest(core, 1, "想你了，什么时候再聊")["event_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.extraction = encode({"memories": [], "interaction_signals": [{
        "event_id": key, "reason": "OWNER_AFFECTION", "quote": quote,
    }]})
    return key


@pytest.mark.parametrize("mood_enabled", [True, False])
def test_memory_commit_records_only_enabled_appraisals(core, mood_enabled):
    key = prepare_extraction(core, mood_enabled=mood_enabled)
    assert run(core.extract_once())
    assert core.store.rows("SELECT event_id,reason FROM mood_appraisals") == (
        [{"event_id": key, "reason": "OWNER_AFFECTION"}] if mood_enabled else [])
    assert core.store.rows("SELECT scenario,state FROM model_usage") == [
        {"scenario": "memory_extract", "state": "SETTLED"}]
    assert bool(core.mood.snapshot()["signals"]) is mood_enabled
    assert core.store.one("SELECT memory_processed FROM events")["memory_processed"] == 1
    assert not run(core.extract_once())
    assert len(core.model.calls) == 1


def test_interaction_signals_validation_rejects_invalid_quotes_without_retry(core):
    prepare_extraction(core, quote="虚构文本")
    assert run(core.extract_once())
    assert core.store.rows("SELECT * FROM mood_appraisals") == []
    assert len(core.model.calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def test_erasure_between_extract_and_commit_cannot_restore_appraisal(core):
    key = prepare_extraction(core)
    assert run(core.extract_step()).progressed
    assert checkpoint_state(core.store)["batch"]["phase"] == "REVIEW"
    erase(core, [key])
    run(core.extract_step())
    assert core.store.rows("SELECT * FROM mood_appraisals") == []
    assert core.mood.snapshot()["signals"] == []
    assert len(core.model.calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def run_isolated_worker(settings_path, now):
    """Actual life-worker entry point; only the external model HTTP transport is mocked."""
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    calls = []
    original_client = httpx.AsyncClient

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        sources = json.loads(payload["messages"][-1]["content"])
        # The extractor sees short refs (e0, ...); Core maps them back to event IDs.
        key = next(row["ref"] for row in sources["events"] if row["text"].startswith("想你了"))
        calls.append("memory_extract")
        return provider_response(encode({"memories": [], "interaction_signals": [{
            "event_id": key, "reason": "OWNER_AFFECTION", "quote": "想你了",
        }]}))

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(provider))

    async def stop(*_args):
        raise asyncio.CancelledError

    life_worker.Core = partial(Core, clock=lambda: now)
    life_worker.httpx = SimpleNamespace(AsyncClient=client)
    life_worker.Wakeup.wait = stop
    try:
        asyncio.run(life_worker.life_worker(settings))
    except asyncio.CancelledError:
        pass
    print(encode({"calls": calls, "pid": os.getpid()}))


def test_http_acceptance_independent_worker_and_restart(core, tmp_path):
    workspace = Path(__file__).resolve().parents[1]
    async def scenario():
        async with environment(core, tmp_path,
            purposes=("chat", "memory", "history", "expression"),
            core_options={"mood_enabled": True, "extraction_min_events": 1,
                          "extraction_idle_seconds": 0},
        ) as (entry, _sends):
            denied = await entry.client.post("/internal/v1/inbox/events", json={},
                headers={"Authorization": "Bearer wrong"})
            assert denied.status_code == 401
            await entry.admit(1, "想你了，什么时候再聊")
            async with worker_for(core, lambda _: provider_response("我也想你。")) as worker:
                assert await worker.run_once("before-appraisal")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            settings = core.settings.model_dump(mode="json")
            for name in ("model_key", "bridge_token", "control_token"):
                settings[name] = getattr(core.settings, name).get_secret_value()
            settings_path = tmp_path / "worker-settings.json"
            settings_path.write_text(encode(settings), encoding="utf-8")
            script = (
                "import sys; from tests.test_mood_appraisals_integration import "
                "run_isolated_worker; run_isolated_worker(sys.argv[1],sys.argv[2])")
            for attempt in range(3):
                completed = await asyncio.to_thread(subprocess.run,
                    [sys.executable, "-B", "-c", script, str(settings_path), core.clock()],
                    cwd=workspace, capture_output=True,
                    text=True, encoding="utf-8", timeout=60, check=True)
                result = json.loads(completed.stdout.splitlines()[-1])
                assert result["pid"] != os.getpid()
                assert result["calls"] == (["memory_extract"] if attempt == 0 else [])
            rows = core.store.rows("SELECT event_id,request_id FROM mood_appraisals")
            assert len(rows) == 1
            assert core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid") == [
                {"scenario": "chat", "state": "SETTLED"},
                {"scenario": "memory_extract", "state": "SETTLED"},
            ]
            proof = core.mood.snapshot()
            assert proof["signals"][0]["event_id"] == rows[0]["event_id"]
            assert proof["signals"][0]["reason"] == "OWNER_AFFECTION"
            assert core.store.one("SELECT state FROM owner_authority_revisions "
                                  "ORDER BY revision DESC")["state"] == "ACTIVE"
            erase(core, [rows[0]["event_id"]])
            assert core.store.rows("SELECT * FROM mood_appraisals") == []
            assert core.mood.snapshot()["signals"] == []
    run(scenario())
