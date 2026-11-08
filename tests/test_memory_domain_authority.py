"""Domain switches never substitute for independent native owner purposes."""

import json
from pathlib import Path

import pytest

from companion import memory_work, owner_authority, owner_input
from companion.contracts import CoreError
from companion.memory_authority import source_allowed
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import after, encode
from tests.conftest import run
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.owner_input import captured, inbound


def grant(core, tmp_path, purposes):
    policy = prepare(core, tmp_path)
    policy = policy.model_copy(update={
        "purposes": purposes,
        "memory_scope": "owner_private" if "memory" in purposes else "none",
    })
    result, _ = activate(core, policy)
    return policy, result


def test_reply_source_check_rereads_external_custody_even_in_the_same_snapshot(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "reply-source-custody")
    authority = owner_authority.current(core.store, core.settings, core.clock)
    check = core.memory_context.reply_source_check(core.settings, core.clock, authority)
    path = Path(core.settings.paid_model.journal_path)
    saved = path.read_bytes()
    try:
        with core.store.read_snapshot():
            assert check([capture.event_id], "chat")
            path.write_bytes(saved + b"\n")
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_CUSTODY_REQUIRED"):
                check([capture.event_id], "chat")
    finally:
        path.write_bytes(saved)


def test_unrelated_purpose_does_not_start_memory_domains(core, tmp_path):
    enable_nightly(core)
    core.settings.memory_enabled = True
    grant(core, tmp_path, ["evaluation"])
    assert not core.diary.repo.enabled()
    assert core.diary.schedule_day(DAY) is None
    assert core.reflections.claim(DAY) is None
    assert core.consolidation.schedule_day(DAY) is None
    assert not run(memory_work.extract_step(core)).progressed
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM consolidation_runs")


def test_consolidation_revision_does_not_rebind_queued_day(core, tmp_path):
    enable_nightly(core)
    policy, first = grant(core, tmp_path, ["consolidation"])
    key = core.consolidation.schedule_day(DAY)
    row = core.store.one('SELECT * FROM consolidation_runs WHERE run_id=%s', (key,))
    assert json.loads(row["steps_json"])["authority"] == first["authority"]
    activate(core, policy, revision=1, operation="operation-activate-2")
    assert core.consolidation.claim() is None
    row = core.store.one('SELECT * FROM consolidation_runs WHERE run_id=%s', (key,))
    assert row["state"] == "CANCELLED"
    assert row["last_error_code"] == "OWNER_AUTHORITY_REVISION_CHANGED"


def test_memory_checkpoint_revision_is_terminal_without_new_call(core, tmp_path):
    enable_nightly(core)
    core.settings.memory_enabled = True
    policy, first = grant(core, tmp_path, ["memory"])
    record = {"batch_id": "test-batch", "timezone": core.settings.timezone,
              "local_day": DAY.isoformat(), "source_event_ids": [], "next_attempt_at": None,
              "batch": {"authority": first["authority"], "phase": "EXTRACT",
                        "candidates": [{"content": "pending private material"}]}}
    core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)',
                       (memory_work.CHECKPOINT_KEY, encode(record)))
    activate(core, policy, revision=1, operation="operation-activate-2")
    assert run(memory_work.extract_step(core)).progressed
    saved = memory_work.checkpoint_state(core.store)
    assert saved["batch"]["phase"] == "CANCELLED"
    assert saved["batch"]["error"] == "OWNER_AUTHORITY_REVISION_CHANGED"
    assert "candidates" not in saved["batch"]
    assert not core.store.rows("SELECT * FROM model_usage")


def test_prior_v2_raw_revision_requires_explicit_history_for_domain(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "domain-source-test")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    original = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**original.policy.model_dump(),
        "purposes": ["diary"]})
    activate(core, policy, revision=1, operation="domain-source-revision")
    assert not source_allowed(core.store, core.settings, core.clock, capture.event_id, "diary")
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "purposes": ["diary", "history"],
        "history": dict(scope="owner_private", not_before=after(core.clock(), -60),
                        max_turns=8, max_utf8_bytes=8192)})
    activate(core, policy, revision=2, operation="domain-history-revision")
    assert source_allowed(core.store, core.settings, core.clock, capture.event_id, "diary")
    core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s', (capture.event_id,))
    assert not source_allowed(core.store, core.settings, core.clock, capture.event_id, "diary")


def test_raw_source_reuses_one_checked_view_and_rechecks_next_read(core, tmp_path, monkeypatch):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "source-view-test")
    checked = []
    original = owner_authority.current

    def current(store, *args, **kwargs):
        checked.append(store.in_read_snapshot)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(owner_authority, "current", current)
    assert source_allowed(core.store, core.settings, core.clock, capture.event_id, "chat")
    assert checked == [True]
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id=%s", (capture.event_id,))
    assert not source_allowed(core.store, core.settings, core.clock, capture.event_id, "chat")
    assert checked == [True, True]


def test_legacy_memory_read_uses_original_archive_without_rebinding(core, tmp_path):
    from tests.support.legacy_owner_input import archived

    _, view, job, _, _ = archived(core, tmp_path)
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        "purposes": ["memory"], "history": {}})
    activate(core, policy, revision=1, operation="memory-only-legacy-read")
    before = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],))
    assert source_allowed(core.store, core.settings, core.clock, job["event_id"], "memory")
    assert core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],)) == before
    core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                       ("live_text_input:" + job["event_id"],))
    assert not source_allowed(core.store, core.settings, core.clock, job["event_id"], "memory")


def test_raw_read_rejection_does_not_discard_retained_memory_fact(core, tmp_path):
    from tests.support.confirmed_memory import remember

    mid = remember(core, 1, "松林项目的封面选用绿色", confirmed=True)
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE state='QUEUED'")
    grant(core, tmp_path, ["memory"])
    assert not source_allowed(core.store, core.settings, core.clock, "evt_test_1", "memory")
    assert mid in {row["memory_id"] for row in core.memory.search("绿色", now=core.clock())}


def test_old_ungranted_revision_cannot_starve_current_memory_batch(core, tmp_path):
    import hashlib

    from companion.owner_authority_protocol import OwnerCapture
    from companion_delivery_protocol.authority import capture_signing_payload

    template, key = captured(core, tmp_path)
    core.settings.memory_enabled = True
    core.settings.extraction_idle_seconds = 0

    def insert(number, authority):
        value = template.model_dump(mode="json", exclude={"signature"})
        value["authority"] = authority
        value["source_event_id"] = template.source_event_id + str(number)
        value["event_id"] = "evt_" + hashlib.sha256(value["source_event_id"].encode()).hexdigest()
        value["signature"] = key.sign(capture_signing_payload(value)).hex()
        capture = OwnerCapture.model_validate(value)
        owner_input.approve(core.store, core.settings, core.clock, capture)
        core.ingest(inbound(core, capture), "memory-source-" + str(number))
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id=%s",
                           (capture.event_id,))
        return capture.event_id

    for number in range(20):
        insert(number, template.authority)
    view = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        "purposes": ["chat", "memory"], "memory_scope": "owner_private"})
    current, _ = activate(core, policy, revision=1, operation="memory-new-source-revision")
    selected = insert(21, current["authority"])
    assert [row["event_id"] for row in memory_work.extraction_sources(core, None)] == [selected]
