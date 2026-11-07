"""Settled private Life wording enters original Diary custody once, without I/O."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelClient
from tests.conftest import ingest, run
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.life_expression_consumption import prepared, request
from tests.support.owner_authority import activate


def draft(core, tmp_path):
    core.settings.diary_enabled = True
    return prepared(core, tmp_path, destination="PRIVATE_DIARY", private=True,
                    purposes=["diary"])


def consume(core, job_id, ref):
    return core.life_topics.consume(job_id, request(core, job_id, ref, "diary.save"),
                                   diary=core.diary)


def test_save_reuses_settled_wording_usage_and_original_job_once(core, tmp_path):
    job, _, ref, source = draft(core, tmp_path)
    usage = core.store.rows("SELECT * FROM model_usage")
    calls = list(core.model.calls)
    result = core.life_topics.retained_result(job)["result"]
    saved = consume(core, job, ref)
    entry = core.diary.get_day(date.fromisoformat(saved["local_date"]), core.settings.owner_qq_id)
    assert entry["body_md"] == result["text"]
    assert not entry["body_md"].startswith("主人修订")
    assert saved["source_job_id"] == job and saved["version"] == 1
    repeated = consume(core, job, ref)
    assert repeated == {**saved, "reused": True}
    assert core.store.one("SELECT COUNT(*) n FROM diary_entries")["n"] == 1
    assert core.store.rows("SELECT * FROM diary_revisions") == []
    ids = {row["event_id"] for row in core.store.rows("SELECT * FROM diary_evidence")}
    assert source in ids
    assert core.store.one("SELECT event_id FROM jobs WHERE job_id=?", (job,))["event_id"] in ids
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.model.calls == calls
    assert core.store.rows("SELECT * FROM outbox") == []


def test_two_independent_connections_consume_once(core, tmp_path):
    job, _, ref, _ = draft(core, tmp_path)
    body = request(core, job, ref, "diary.save")
    barrier = Barrier(2)
    with Core(core.settings, core.model, core.clock) as first, Core(
        core.settings, core.model, core.clock,
    ) as second:
        def save(worker):
            barrier.wait(timeout=10)
            return worker.diary.save_life_expression(job,
                expected_content_sha256=body.expected_content_sha256, authority=ref)
        with ThreadPoolExecutor(max_workers=2) as executor:
            values = list(executor.map(save, (first, second)))
    assert len({value["entry_id"] for value in values}) == 1
    assert sorted(value["reused"] for value in values) == [False, True]
    assert core.store.one("SELECT COUNT(*) n FROM diary_entries")["n"] == 1
    assert core.store.rows("SELECT * FROM diary_revisions") == []


def test_repeated_save_preserves_owner_edit_and_deleted_day(core, tmp_path):
    job, _, ref, _ = draft(core, tmp_path)
    saved = consume(core, job, ref)
    day = date.fromisoformat(saved["local_date"])
    edited = core.diary.correct(day, "这是我保留的修改。", core.settings.owner_qq_id)
    again = consume(core, job, ref)
    assert again["version"] == saved["version"] and again["current_version"] == edited["version"]
    assert core.diary.get_day(day, core.settings.owner_qq_id) == edited
    core.diary.forget(day, core.settings.owner_qq_id)
    with pytest.raises(CoreError):
        consume(core, job, ref)
    assert core.diary.get_day(day, core.settings.owner_qq_id) is None
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=?", ("diary_life:" + job,))


def test_save_appends_to_native_daily_entry_without_overwriting_owner_edit(core, tmp_path):
    core.settings.diary_enabled = True
    core.settings.diary_min_source_events = 1
    job, _, ref, _ = prepared(core, tmp_path, destination="PRIVATE_DIARY", purposes=["diary"])
    result = core.life_topics.retained_result(job)["result"]
    day = datetime.fromisoformat(result["updated_at"]).astimezone(
        ZoneInfo(core.settings.timezone)).date()
    core.clock.advance(86400)

    def provider(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        source = payload["sources"][0]
        value = {"schema_version": 1, "local_date": payload["local_date"],
                 "title": "原有日记", "worth_saving": True,
                 "sections": [{"kind": "observation", "text": source["text"], "evidence": [
                     {"source_id": source["source_id"], "exact_quote": source["text"]}]}]}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(value, ensure_ascii=False)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80}})

    async def generate_existing():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            core.diary.model = ModelClient(core.settings, transport)
            assert core.diary.schedule_day(day)
            assert await core.diary.generate_once("existing-daily-entry")
            saved_run = core.store.one("SELECT * FROM diary_runs")
            assert saved_run["state"] == "COMMITTED", saved_run
    run(generate_existing())
    old = core.diary.correct(day, "这段主人编辑必须保留。", core.settings.owner_qq_id)
    original_request = core.store.one("SELECT model_request_id FROM diary_runs")["model_request_id"]
    usage = core.store.rows("SELECT * FROM model_usage")
    saved = consume(core, job, ref)
    current = core.diary.get_day(day, core.settings.owner_qq_id)
    assert current["entry_id"] == old["entry_id"] == saved["entry_id"]
    assert current["body_md"].startswith(old["body_md"] + "\n\n## 自主随记\n\n")
    assert current["body_md"].endswith(result["text"])
    assert current["version"] == old["version"] + 1
    assert core.store.one("SELECT reason,changed_by FROM diary_revisions ORDER BY rowid DESC") == {
        "reason": "life_expression", "changed_by": "companion"}
    assert core.store.one("SELECT model_request_id FROM diary_runs")["model_request_id"] == (
        original_request)
    assert core.store.rows("SELECT * FROM model_usage") == usage


@pytest.mark.parametrize("change", ["hash", "revision", "source", "expired", "fiction"])
def test_private_diary_consumer_rejects_changed_or_unproven_result(core, tmp_path, change):
    job, policy, ref, source = draft(core, tmp_path)
    body = request(core, job, ref, "diary.save")
    usage = core.store.rows("SELECT * FROM model_usage")
    if change == "hash":
        body = body.model_copy(update={"expected_content_sha256": "f" * 64})
    elif change == "revision":
        activate(core, policy, revision=1, operation="revise-diary-consumption")
    elif change == "source":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    elif change == "expired":
        core.clock.advance(4 * 86400)
    else:
        core.store.execute("UPDATE life_activity_results SET proposal_json="
                           "json_set(proposal_json,'$.fiction',json('true')) WHERE job_id=?",
                           (job,))
    with pytest.raises(CoreError):
        core.life_topics.consume(job, body, diary=core.diary)
    assert core.store.rows("SELECT * FROM diary_entries") == []
    assert core.store.rows("SELECT * FROM model_usage") == usage


def test_native_source_erasure_scrubs_saved_life_diary_but_not_unrelated_erasure(core, tmp_path):
    ingest(core, 99, "独立的历史素材")
    job, _, ref, source = draft(core, tmp_path)
    saved = consume(core, job, ref)
    day = date.fromisoformat(saved["local_date"])
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_99"], core.clock())
    assert core.diary.get_day(day, core.settings.owner_qq_id) is not None
    usage = core.store.rows("SELECT * FROM model_usage")
    with core.store.transaction():
        core.ledger.expire_events(core.store, [source], core.clock())
    assert core.diary.get_day(day, core.settings.owner_qq_id) is None
    assert core.store.rows("SELECT * FROM diary_evidence") == []
    with pytest.raises(CoreError):
        consume(core, job, ref)
    assert core.store.rows("SELECT * FROM model_usage") == usage


def test_day_deletion_replays_into_backup_taken_before_life_save(core, tmp_path):
    job, _, ref, _ = draft(core, tmp_path)
    path, key = tmp_path / "before-save.enc", Fernet.generate_key()
    backup(core.settings.database, path, key)
    saved = consume(core, job, ref)
    day = date.fromisoformat(saved["local_date"])
    core.diary.forget(day, core.settings.owner_qq_id)
    target = tmp_path / "restored.db"
    restore(path, target, key, core.settings.erasure_ledger)
    settings = core.settings.model_copy(update={"database": str(target)})
    with Core(settings, core.model, core.clock) as reopened:
        assert reopened.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
            ("diary_deleted:" + saved["timezone"] + ":" + saved["local_date"],))
        assert reopened.diary.get_day(day, settings.owner_qq_id) is None
        with pytest.raises(CoreError):
            consume(reopened, job, ref)
