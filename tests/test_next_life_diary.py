"""Recompute invalidated daily projections through the original diary worker contract."""

from datetime import date, datetime

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.diary import DiaryDraft, render_draft
from tests.conftest import run
from tests.support.diary import DAY, DiaryModel
from tests.support.next_life_diary import seed, withdraw


def recompute(core):
    return core.diary.recompute_day(DAY, core.settings.owner_qq_id)


def test_withdrawn_source_recomputes_in_independent_worker_without_resetting_usage(core):
    original_model = seed(core)
    original = core.diary.get_day(DAY, core.settings.owner_qq_id)
    original_usage = core.store.one("SELECT * FROM model_usage")
    assert recompute(core) is None  # Current evidence is not a request to overwrite a diary.
    withdraw(core)
    run_id = recompute(core)
    assert run_id and recompute(core) == run_id
    assert len(original_model.calls) == 1  # Scheduling never calls the provider.
    model = DiaryModel()
    with Core(core.settings, model, core.clock) as worker:
        assert worker.diary.schedule_day(DAY) == run_id
        assert run(worker.diary.generate_once("independent-life-worker"))
        assert not run(worker.diary.generate_once("duplicate-worker-tick"))
        updated = worker.diary.get_day(DAY, worker.settings.owner_qq_id)
        assert updated["entry_id"] == original["entry_id"]
        assert updated["version"] == original["version"] + 1
        assert "第 0 步" not in updated["body_md"] and "第 1 步" in updated["body_md"]
        assert worker.store.one("SELECT attempts FROM diary_runs")["attempts"] == 2
        evidence = worker.store.rows("SELECT event_id FROM diary_evidence ORDER BY event_id")
        assert [row["event_id"] for row in evidence] == [
            "evt_test_2", "evt_test_3", "evt_test_4",
        ]
        assert worker.store.rows("SELECT * FROM diary_revisions") == []
        assert worker.store.one('SELECT * FROM model_usage WHERE request_id=%s',
                                (original_usage["request_id"],)) == original_usage
        assert len(worker.store.rows("SELECT * FROM model_usage")) == 2
        for table in ("memories", "traits", "outbox"):
            assert worker.store.rows("SELECT * FROM " + table) == []
    assert len(model.calls) == 1


def test_recompute_after_old_backup_replay_uses_current_remaining_evidence(
    empty_database, core, tmp_path,
):
    seed(core)
    archive = tmp_path / "before.enc"
    restored_state = tmp_path / "restored-state"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    withdraw(core)
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    settings = core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    })
    model = DiaryModel()
    with Core(settings, model, core.clock) as restored:
        assert restored.diary.get_day(DAY, settings.owner_qq_id) is None
        sources = restored.diary.repo.sources(DAY)
        assert [(source["event_id"], source["text"]) for source in sources] == [
            (f"evt_test_{i + 1}", f"今天讨论了读书计划的第 {i} 步。") for i in range(1, 4)
        ]
        usage = restored.store.rows("SELECT * FROM model_usage")
        entries = restored.store.rows("SELECT * FROM diary_entries")
        assert recompute(restored)
        assert run(restored.diary.generate_once("restored-worker"))
        assert model.calls == []
        assert restored.diary.get_day(DAY, settings.owner_qq_id) is None
        assert restored.store.rows("SELECT * FROM model_usage") == usage
        assert restored.store.rows("SELECT * FROM diary_entries") == entries
        assert restored.store.one("SELECT state,last_error_code FROM diary_runs") == {
            "state": "FAILED", "last_error_code": "CLOUD_BUDGET_RESTORE_RECONCILIATION_REQUIRED",
        }
    with Core(settings, model, core.clock) as reopened:
        # Replaying erasure preserves independent evidence, but cannot authorize new spending.
        assert reopened.diary.repo.sources(DAY) == sources
        assert reopened.diary.get_day(DAY, settings.owner_qq_id) is None
        assert reopened.store.one(
            "SELECT value FROM runtime_meta WHERE key='cloud_budget_restore_required'"
        ) == {"value": "1"}
        assert not run(reopened.diary.generate_once("reopened-worker"))
        assert reopened.store.rows("SELECT * FROM model_usage") == usage
        assert model.calls == []


@pytest.mark.parametrize("usage_state", ["UNKNOWN", "RESERVED"])
def test_recompute_never_replays_or_releases_unresolved_model_attempt(core, usage_state):
    model = seed(core)
    core.store.execute('UPDATE model_usage SET state=%s', (usage_state,))
    withdraw(core)
    before = core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError, match="DIARY_RECOMPUTE_USAGE_UNRESOLVED"):
        recompute(core)
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert len(model.calls) == 1
    assert not run(core.diary.generate_once("blocked-worker"))


def test_recompute_respects_original_attempt_limit(core):
    core.settings.diary_max_attempts = 1
    model = seed(core)
    withdraw(core)
    with pytest.raises(CoreError, match="DIARY_RECOMPUTE_ATTEMPTS_EXHAUSTED"):
        recompute(core)
    assert len(model.calls) == 1
    assert core.store.one("SELECT attempts,max_attempts FROM diary_runs") == {
        "attempts": 1, "max_attempts": 1,
    }


def test_insufficient_remaining_sources_skip_without_fabricating_a_new_day(core):
    model = seed(core, count=3)
    withdraw(core)
    assert recompute(core)
    assert run(core.diary.generate_once("sparse-day-worker"))
    assert core.store.one("SELECT state FROM diary_runs")["state"] == "SKIPPED"
    assert len(model.calls) == 1 and core.diary.get_day(DAY, core.settings.owner_qq_id) is None
    assert core.store.rows("SELECT * FROM diary_evidence") == []


def test_pause_and_restart_preserve_recompute_intent_without_new_attempts(core):
    seed(core)
    withdraw(core)
    core.diary.set_mode(False, core.settings.owner_qq_id)
    assert recompute(core) is None
    core.diary.set_mode(True, core.settings.owner_qq_id)
    run_id = recompute(core)
    core.diary.set_mode(False, core.settings.owner_qq_id)
    with Core(core.settings, DiaryModel(), core.clock) as worker:
        assert recompute(worker) is None
        assert not run(worker.diary.generate_once("paused-worker"))
        worker.diary.set_mode(True, worker.settings.owner_qq_id)
        assert recompute(worker) == run_id
        assert run(worker.diary.generate_once("resumed-worker"))
        assert worker.store.one("SELECT attempts FROM diary_runs")["attempts"] == 2


def test_owner_correction_is_preserved_until_its_source_is_erased(core):
    seed(core)
    corrected = core.diary.correct(DAY, "只保留主人已核对的记录。", core.settings.owner_qq_id)
    assert recompute(core) is None
    assert core.diary.get_day(DAY, core.settings.owner_qq_id) == corrected
    withdraw(core)
    assert core.store.rows("SELECT * FROM diary_revisions") == []
    assert recompute(core)
    assert run(core.diary.generate_once("remaining-sources-worker"))
    current = core.diary.get_day(DAY, core.settings.owner_qq_id)
    assert current["version"] == corrected["version"] + 1
    assert "只保留主人已核对" not in current["body_md"]


def test_day_deletion_during_recompute_fences_worker_and_survives_restore(
    empty_database, core, tmp_path,
):
    seed(core)
    withdraw(core)
    assert recompute(core)
    key = Fernet.generate_key()
    archive = tmp_path / "pending.enc"
    restored_state = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    core.diary.forget(DAY, core.settings.owner_qq_id)
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    with Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }),
              DiaryModel(), core.clock) as restored:
        for instance in (core, restored):
            assert recompute(instance) is None
            assert instance.diary.schedule_day(DAY) is None
            assert not run(instance.diary.generate_once("deleted-day-worker"))
            assert instance.diary.get_day(DAY, instance.settings.owner_qq_id) is None


def test_recompute_source_race_rejects_stale_draft_and_retains_actual_usage(core):
    core.settings.diary_max_attempts = 3
    model = seed(core, count=5)
    withdraw(core)
    assert recompute(core)
    model.callback = lambda: withdraw(core, "evt_test_2")
    assert run(core.diary.generate_once("source-race-worker"))
    assert core.store.one("SELECT state FROM diary_runs")["state"] == "CANCELLED"
    assert core.diary.get_day(DAY, core.settings.owner_qq_id) is None
    assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 2
    model.callback = None
    assert recompute(core)
    assert run(core.diary.generate_once("current-sources-worker"))
    current = core.diary.get_day(DAY, core.settings.owner_qq_id)
    assert "第 0 步" not in current["body_md"] and "第 1 步" not in current["body_md"]
    assert "第 2 步" in current["body_md"]


def test_crash_before_reservation_recovers_and_old_worker_cannot_commit(core):
    core.settings.diary_max_attempts = 3
    seed(core)
    withdraw(core)
    assert recompute(core)
    old = core.diary.repo.claim("crashed-before-reservation")
    core.clock.advance(core.settings.lease_seconds + 1)
    with Core(core.settings, DiaryModel(), core.clock) as worker:
        assert run(worker.diary.generate_once("replacement-worker"))
        current = worker.diary.get_day(DAY, worker.settings.owner_qq_id)
        assert current
    draft = DiaryDraft(schema_version=1, worth_saving=False)
    with pytest.raises(CoreError, match="DIARY_STALE_LEASE"):
        core.diary.repo.finish(old, "SKIPPED", draft=draft,
                               body=render_draft(draft, old["sources"], old["local_date"],
                                                 core.settings))
    assert core.diary.get_day(DAY, core.settings.owner_qq_id) == current


def test_recompute_requires_owner_and_a_closed_local_day(core):
    seed(core)
    with pytest.raises(CoreError, match="OWNER_REQUIRED"):
        core.diary.recompute_day(DAY, "10003")
    core.clock.now = datetime.fromisoformat("2026-10-19T16:00:00+00:00")
    with pytest.raises(CoreError, match="DIARY_DAY_NOT_CLOSED"):
        core.diary.recompute_day(date(2026, 10, 20), core.settings.owner_qq_id)
    # 19th is already closed in Shanghai while still today's UTC calendar date.
    assert core.diary.recompute_day(date(2026, 10, 19), core.settings.owner_qq_id) is None
