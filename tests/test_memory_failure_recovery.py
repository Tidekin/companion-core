"""Failed batches survive restart, stay bounded and follow native erasure custody."""

import json
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion import budget
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.memory_recovery import PREFIX, failures, retry
from companion.memory_work import checkpoint_state
from companion.store import Store
from tests.conftest import run
from tests.test_nightly_stages import staged


def failed_batch(core):
    model = staged(core)
    model.review = lambda data: '{"relations":"invalid"}'
    for _ in range(8):
        run(core.extract_step())
        record = checkpoint_state(core.store)
        if record["batch"].get("phase") == "FAILED":
            return model, record
        core.clock.advance(61)
    raise AssertionError("review did not exhaust its bounded attempts")


def test_settled_failed_candidates_survive_restart_and_recover_once(core):
    model, record = failed_batch(core)
    batch_id = record["batch_id"]
    assert len(failures(core.store)) == 1
    assert core.store.rows("SELECT * FROM memories") == []
    calls = len(model.calls)
    assert not run(core.extract_step()).progressed
    assert len(model.calls) == calls
    model.review = None
    with Core(core.settings, model, core.clock) as restarted:
        assert retry(restarted, batch_id, "owner-retry-1")["state"] == "QUEUED"
        assert retry(restarted, batch_id, "owner-retry-1")["state"] == "QUEUED"
        assert run(restarted.extract_step()).progressed
        assert checkpoint_state(restarted.store)["batch"]["phase"] == "COMPLETE"
        assert len(restarted.store.rows("SELECT * FROM memories")) == 2
        assert failures(restarted.store)[0]["state"] == "COMPLETE"
        with pytest.raises(CoreError, match="MEMORY_RECOVERY_EXHAUSTED"):
            retry(restarted, batch_id, "owner-retry-2")
    assert [kind for _, kind in model.calls].count("memory_extract") == 1
    assert [kind for _, kind in model.calls].count("memory_review") == 4
    assert all(r["state"] == "SETTLED" for r in core.store.rows("SELECT * FROM model_usage"))


def test_recovery_failure_is_terminal_and_never_replayed_by_scheduler(core):
    model, record = failed_batch(core)
    retry(core, record["batch_id"], "owner-retry-1")
    for _ in range(5):
        run(core.extract_step())
        core.clock.advance(61)
    assert checkpoint_state(core.store)["batch"]["phase"] == "FAILED"
    assert not failures(core.store)[0]["recovery_available"]
    calls = len(model.calls)
    assert not run(core.extract_step()).progressed
    assert len(model.calls) == calls
    with pytest.raises(CoreError, match="MEMORY_RECOVERY_EXHAUSTED"):
        retry(core, record["batch_id"], "another-owner-request")


def test_erasure_removes_failed_candidate_payload_and_recovery_entry(core):
    _, record = failed_batch(core)
    with core.store.transaction():
        core.ledger.expire_events(core.store, record["source_event_ids"][:1], core.clock())
    assert failures(core.store) == []
    assert checkpoint_state(core.store) is None
    with pytest.raises(CoreError, match="MEMORY_FAILURE_NOT_FOUND"):
        retry(core, record["batch_id"], "owner-retry")
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key GLOB ?", (PREFIX + "*",))


def test_recovery_refuses_unsettled_usage_without_changing_sources(core):
    _, record = failed_batch(core)
    core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=?",
                       (record["batch"]["request_id"],))
    before = core.store.rows("SELECT event_id,memory_processed FROM events")
    with pytest.raises(CoreError, match="MEMORY_RECOVERY_UNSETTLED"):
        retry(core, record["batch_id"], "owner-retry")
    assert core.store.rows("SELECT event_id,memory_processed FROM events") == before
    assert failures(core.store)[0]["state"] == "FAILED"


def test_old_backup_cannot_restore_erased_failed_candidates(core, tmp_path):
    _, record = failed_batch(core)
    key = Fernet.generate_key()
    archive, restored = tmp_path / "failed.enc", tmp_path / "restored.db"
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, record["source_event_ids"][:1], core.clock())
    restore(archive, restored, key, core.settings.erasure_ledger)
    with closing(Store(str(restored))) as store:
        assert failures(store) == []
        assert checkpoint_state(store) is None


def test_unreserved_review_intent_can_recover_without_replaying_extraction(core, monkeypatch):
    model = staged(core)
    core.settings.max_attempts = 1
    assert run(core.extract_step()).progressed
    original = budget.reserve

    def undispatched(*args, **kwargs):
        raise CoreError("MEMORY_REVIEW_INVALID", 502)

    with monkeypatch.context() as patch:
        patch.setattr(budget, "reserve", undispatched)
        run(core.extract_step())
    record = checkpoint_state(core.store)
    assert record["batch"]["phase"] == "FAILED"
    archived = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                         (PREFIX + record["batch_id"],))["value"])
    assert not core.store.one("SELECT 1 FROM model_usage WHERE request_id=?",
                              (archived["batch"]["request_id"],))
    assert budget.reserve is original
    retry(core, record["batch_id"], "owner-unsent-review")
    assert run(core.extract_step()).progressed
    assert checkpoint_state(core.store)["batch"]["phase"] == "COMPLETE"
    assert [kind for _, kind in model.calls] == ["memory_extract", "memory_review"]
