"""Real extraction/commit and authenticated HTTP plus the independent life worker."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.memory_work import checkpoint_state
from companion.store import encode
from tests.conftest import ingest, run
from tests.sdk_reader_fixture import worker_for
from tests.support.mood import erase
from tests.support.mood_appraisals_integration import prepare_extraction
from tests.support.native_consumer_entry import environment
from tests.support.next_memory_worker import provider_response


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
            for name in ("database", "model_key", "bridge_token", "control_token"):
                settings[name] = getattr(core.settings, name).get_secret_value()
            settings_path = tmp_path / "worker-settings.json"
            settings_path.write_text(encode(settings), encoding="utf-8")
            script = (
                "import sys; from tests.support.mood_appraisals_integration import "
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
            assert core.store.rows(
                'SELECT scenario,state FROM model_usage ORDER BY '
                'record_sequence'
            ) == [
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


def test_extraction_records_a_content_free_appraisal_funnel(core):
    key = prepare_extraction(core)
    core.model.extraction = encode({"memories": [], "interaction_signals": [
        {"event_id": key, "reason": "OWNER_AFFECTION", "quote": "想你了"},
        {"event_id": key, "reason": "OWNER_PLAYFUL", "quote": "想你了"},
        {"event_id": "evt_other", "reason": "OWNER_PLAYFUL", "quote": "想你了"},
        {"event_id": key, "reason": "OWNER_DISTRESS", "quote": "没说过的话"},
    ]})
    assert run(core.extract_once())
    row = core.store.one(
        "SELECT entity_id,safe_detail_json FROM audit_log WHERE action='mood.appraisals_extracted'"
    )
    assert row["entity_id"] == core.store.one("SELECT request_id FROM model_usage")["request_id"]
    assert json.loads(row["safe_detail_json"]) == {
        "field_present": True, "finish_reason": None, "returned": 4, "kept": 1,
        "rejected": {"DUPLICATE_EVENT": 2, "OUTSIDE_BATCH": 1},
        "reasons": {"OWNER_AFFECTION": 1},
    }
    assert "想你" not in row["safe_detail_json"]


def test_funnel_distinguishes_a_missing_field_from_an_empty_list(core):
    prepare_extraction(core)
    core.model.extraction = encode({"memories": []})
    assert run(core.extract_once())
    detail = json.loads(core.store.one(
        "SELECT safe_detail_json FROM audit_log WHERE action='mood.appraisals_extracted'"
    )["safe_detail_json"])
    assert detail["field_present"] is False and detail["returned"] == 0


def test_truncated_extraction_leaves_a_content_free_receipt(core):
    from companion.model import ModelResponse

    prepare_extraction(core)
    original = core.model.complete

    async def cut(messages, scenario):
        response = await original(messages, scenario)
        if scenario == "memory_extract":
            return ModelResponse('{"memories":[{"subject":"us', 100, 4096, 0,
                                 finish_reason="length")
        return response

    core.model.complete = cut
    run(core.extract_step())
    row = core.store.one(
        "SELECT safe_detail_json FROM audit_log WHERE action='memory.extract_truncated'"
    )
    assert json.loads(row["safe_detail_json"]) == {
        "output_tokens": 4096, "max_tokens": 4096, "events": 1,
    }
    assert core.store.rows("SELECT * FROM mood_appraisals") == []


def test_memory_extract_has_its_own_output_cap(core):
    core.settings.max_output_tokens = 2048
    assert core.settings.output_tokens("memory_extract") == 4096
    assert core.settings.output_tokens("chat") == 2048
    core.settings.memory_extract_max_output_tokens = 3000
    assert core.settings.output_tokens("memory_extract") == 3000


def test_memory_extract_cap_never_exceeds_the_paid_approval(core):
    def approved(limit):
        return core.settings.model_copy(
            update={"paid_model": SimpleNamespace(max_output_tokens=limit)})

    assert approved(2048).output_tokens("memory_extract") == 2048
    assert approved(8192).output_tokens("memory_extract") == 4096


def test_valid_appraisal_survives_exhausted_unrelated_fact_reviews(core):
    from tests.support.memory_write_consistency import candidate

    key = prepare_extraction(core)
    text = "讲座地点是海盐厅"
    ingest(core, 2, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.extraction = encode({
        "memories": [candidate(text).model_dump(mode="json")],
        "interaction_signals": [{"event_id": key, "reason": "OWNER_AFFECTION", "quote": "想你了"}],
    })
    core.model.review = lambda _: "{}"
    assert run(core.extract_step()).progressed
    assert core.mood.snapshot()["signals"][0]["event_id"] == key
    for _ in range(core.settings.max_attempts):
        run(core.extract_step())
        core.clock.advance(61)
    assert checkpoint_state(core.store)["batch"]["phase"] == "FAILED"
    assert core.store.rows("SELECT event_id,reason FROM mood_appraisals") == [
        {"event_id": key, "reason": "OWNER_AFFECTION"}]
    receipt = core.store.one("SELECT safe_detail_json FROM audit_log "
                             "WHERE action='mood.appraisals_committed'")
    assert json.loads(receipt["safe_detail_json"])["inserted"] == 1
    assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")["n"] == (
        1 + core.settings.max_attempts)
    assert core.mood.reference()["proof"]["source_event_ids"] == [key]
    assert not core.store.rows("SELECT 1 FROM memories")
    chat = ingest(core, 3, "聊聊电影吧")
    assert run(core.run_once(job_id=chat["job_id"]))
    messages, kind = core.model.calls[-1]
    assert kind == "chat" and any("你的心情参考" in m["content"] for m in messages)
    assert core.store.one('SELECT 1 FROM job_sources WHERE job_id=%s AND event_id=%s',
                          (chat["job_id"], key))
