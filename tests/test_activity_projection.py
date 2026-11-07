"""Inspection is bounded, source checked and has no execution or maintenance effects."""

import json
import sqlite3
from contextlib import closing

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion import activity_projection, privacy
from companion.activity_projection import ActivitySelection
from companion.backup import backup, restore
from companion.cognition_read import CognitionRead
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.memory import Memory
from companion.observation import ObservationEvent
from companion.runtime import Runtime
from companion.store import Store
from companion.transport import begin_send, delivery_result, lease_deliveries, reconcile_delivery
from tests.conftest import ingest, run
from tests.test_goals import setup_goal
from tests.test_m1 import remember
from tests.test_memory_projections import fetched
from tests.test_next_memory_context import episode
from tests.test_observations import enable_observations, observation


def conversation(core):
    return core.store.one("SELECT conversation_id FROM events WHERE event_id='evt_test_1'")[
        "conversation_id"]


def inspect(core, **selection):
    return CognitionRead(core).activity_snapshot(
        {"conversation_id": conversation(core), **selection})


def goal_item(core, key):
    return next(item for item in inspect(core)["items"] if item["id"] == key)


def test_read_is_metadata_only_and_sqlite_rejects_any_write(core, monkeypatch):
    goal = setup_goal(core)
    mid = remember(core, 2, "合成私有原话不能成为快照正文", confirmed=True)
    eid = episode(core, 2)

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not execute or maintain")

    monkeypatch.setattr(core.goals, "recent", forbidden)
    monkeypatch.setattr(core.reading, "lease", forbidden)
    monkeypatch.setattr(core.memory, "search", forbidden)
    before = "\n".join(core.store.db.iterdump())
    changes = core.store.db.total_changes
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}
    core.store.db.set_authorizer(lambda action, *args:
                                 sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK)
    try:
        result = inspect(core, memory_ids=[mid], episode_ids=[eid])
        assert result == inspect(core, memory_ids=[mid], episode_ids=[eid])
    finally:
        core.store.db.set_authorizer(None)
    assert core.store.db.total_changes == changes
    assert "\n".join(core.store.db.iterdump()) == before
    assert core.model.calls == []
    assert result["presence"]["state"] == "UNKNOWN"
    assert result["attention"]["used_in_request"] == "NOT_RECORDED"
    assert result["clues"] == {"state": "NOT_RECORDED", "items": []}
    assert goal in {item["id"] for item in result["items"]}
    assert "合成私有原话" not in json.dumps(result, ensure_ascii=False)
    assert "lease_token" not in json.dumps(result)


def test_real_goal_result_and_sent_feedback_remain_distinct(core):
    key = setup_goal(core, commitment=True)
    step = core.goals.claim()
    core.goals.finish(step, fetched(core, key))
    item = goal_item(core, key)
    assert item["execution"]["state"] == "done"
    assert item["verified_completed_steps"] == 1
    assert item["result"]["result_status"] == "FETCHED"
    assert not item["steps"][0]["lease"]["valid_at_as_of"]
    assert item["commitment"]["told"] is False
    feedback = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert run(core.run_once(job_id=feedback["job_id"]))
    assert goal_item(core, key)["commitment"]["told"] is False
    for outcome in ("SENT", "DELIVERY_UNCERTAIN"):
        delivery = lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                                    core.clock())["items"][0]
        begin_send(core.store, delivery["message_id"], delivery["lease_token"],
                   core.clock(), core.settings)
        delivery_result(core.store, core.settings, delivery["message_id"],
                        DeliveryResult(lease_token=delivery["lease_token"], result=outcome),
                        core.clock())
    assert goal_item(core, key)["commitment"]["told"] is False
    reconcile_delivery(core.store, delivery["message_id"], "SENT", core.clock())
    assert goal_item(core, key)["commitment"]["told"] is True


@pytest.mark.parametrize("damage", ["missing_step", "uncertain", "fake_done"])
def test_native_labels_never_supply_completion_proof(core, damage):
    key = setup_goal(core)
    core.goals.accept(key)
    step = core.goals.claim()
    if damage == "uncertain":
        core.store.execute("UPDATE goal_steps SET state='EXECUTION_UNCERTAIN' WHERE step_id=?",
                           (step["step_id"],))
    else:
        core.store.execute("UPDATE goals SET state='DONE' WHERE goal_id=?", (key,))
        if damage == "missing_step":
            core.store.execute("DELETE FROM goal_step_evidence WHERE step_id=?", (step["step_id"],))
            core.store.execute("DELETE FROM goal_steps WHERE goal_id=?", (key,))
    item = goal_item(core, key)
    assert item["execution"]["state"] == "UNKNOWN" and item["result"] is None


@pytest.mark.parametrize("invalid", ["expired", "redacted", "tombstone", "upstream", "future"])
def test_unavailable_source_ids_are_absent(core, invalid):
    mid = remember(core, 1, "私有来源", confirmed=True)
    eid = episode(core, 1)
    cid = conversation(core)
    if invalid == "expired":
        core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    elif invalid == "redacted":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    elif invalid == "tombstone":
        core.store.insert("runtime_meta", key="erased_event:evt_test_1", value="erased")
    elif invalid == "future":
        core.store.execute("UPDATE events SET occurred_at='2099-01-01T00:00:00+00:00'")
    else:
        ingest(core, 2, "上游")
        core.store.insert("event_dependencies", event_id="evt_test_1", source_event_id="evt_test_2")
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_2'")
    result = CognitionRead(core).activity_snapshot(
        {"conversation_id": cid, "memory_ids": [mid], "episode_ids": [eid]})
    assert result["attention"]["items"] == []
    assert "evt_test_1" not in json.dumps(result)


def test_combined_limit_keeps_unknown_ahead_of_newer_jobs(core):
    key = setup_goal(core)
    core.goals.accept(key)
    step = core.goals.claim()
    core.store.execute("UPDATE goal_steps SET state='EXECUTION_UNCERTAIN' WHERE step_id=?",
                       (step["step_id"],))
    for number in range(2, 27):
        ingest(core, number, "合成新消息")
    result = inspect(core, limit=1)
    assert result["truncated"] and len(result["items"]) == 1
    assert result["items"][0]["id"] == key


@pytest.mark.parametrize("selection", [{"limit": True}, {"limit": 0}, {"limit": 21},
                                      {"limit": "1"}, {"current_event_ids": ["forged"]},
                                      {"memory_ids": ["x"] * 21},
                                      {"memory_ids": ["x"] * 10, "episode_ids": ["y"] * 11}])
def test_strict_selection(selection):
    with pytest.raises(ValidationError):
        ActivitySelection.model_validate({"conversation_id": "native", **selection})


def test_latest_epoch_is_checked_after_read_transaction_closes(core, monkeypatch):
    setup_goal(core)
    original = activity_projection._work
    changed = []
    with closing(Store(core.settings.database)) as writer:
        def concurrent(reader, sources, candidate):
            item = original(reader, sources, candidate)
            if not changed:
                assert core.store.db.in_transaction
                with writer.transaction():
                    writer.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
                    privacy.advance_epoch(writer)
                changed.append(True)
            return item
        monkeypatch.setattr(activity_projection, "_work", concurrent)
        with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
            inspect(core)
    assert changed and not core.store.db.in_transaction
    assert inspect(core)["items"] == []


def test_independent_ledger_erasure_during_read_cannot_publish_old_snapshot(core, monkeypatch):
    setup_goal(core)
    original = activity_projection._work
    changed = []

    def erase(reader, sources, candidate):
        item = original(reader, sources, candidate)
        if not changed:
            with Runtime(core.settings, core.clock) as writer:
                assert writer.store is not core.store and core.store.db.in_transaction
                with writer.store.transaction():
                    writer.ledger.expire_events(writer.store, ["evt_test_1"], core.clock())
            changed.append(True)
        return item

    monkeypatch.setattr(activity_projection, "_work", erase)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        inspect(core)
    assert changed and inspect(core)["items"] == []


def test_newer_unreplayed_ledger_fails_after_ending_the_read_snapshot(core, monkeypatch):
    setup_goal(core)
    original = activity_projection._work
    changed = []

    def interrupted_erasure(reader, sources, candidate):
        item = original(reader, sources, candidate)
        if not changed:
            with Runtime(core.settings, core.clock) as writer:
                with pytest.raises(RuntimeError, match="synthetic commit loss"):
                    with writer.store.transaction():
                        writer.ledger.expire_events(writer.store, ["evt_test_1"], core.clock())
                        raise RuntimeError("synthetic commit loss")
            changed.append(True)
        return item

    monkeypatch.setattr(activity_projection, "_work", interrupted_erasure)
    before = core.store.db.total_changes
    with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
        inspect(core)
    assert changed and not core.store.db.in_transaction
    assert core.store.db.total_changes == before  # Inspection never replays or repairs.


def test_native_work_rows_share_one_consistent_sqlite_read_snapshot(core, monkeypatch):
    first, second = setup_goal(core), setup_goal(core, number=2)
    original = activity_projection._work
    changed = []
    with closing(Store(core.settings.database)) as writer:
        def update_work(reader, sources, candidate):
            item = original(reader, sources, candidate)
            if not changed:
                with writer.transaction():
                    writer.execute("UPDATE goals SET state='CANCELLED'")
                changed.append(True)
            return item
        monkeypatch.setattr(activity_projection, "_work", update_work)
        result = inspect(core)
    goals = [item for item in result["items"] if item["id"] in {first, second}]
    assert len(goals) == 2 and all(item["native_state"] == "CANDIDATE" for item in goals)
    assert all(goal_item(core, key)["native_state"] == "CANCELLED" for key in (first, second))


def test_source_expiring_during_snapshot_is_not_published(core, monkeypatch):
    setup_goal(core)
    original = activity_projection._work
    changed = []

    def expires(reader, sources, candidate):
        item = original(reader, sources, candidate)
        if not changed:
            core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
            changed.append(True)
        return item

    monkeypatch.setattr(activity_projection, "_work", expires)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        inspect(core)


def test_foreign_conversation_sources_never_supply_activity_or_attention(core):
    key = setup_goal(core)
    mid = remember(core, 2, "另一个会话的私有记忆", confirmed=True)
    eid = episode(core, 2)
    core.store.insert("conversations", conversation_id="foreign-conversation", platform="qq",
                      platform_session_key="foreign-session", scope="private",
                      owner_actor_id="owner", created_at=core.clock())
    core.store.execute("UPDATE events SET conversation_id='foreign-conversation' "
                       "WHERE event_id='evt_test_2'")
    core.store.insert("goal_evidence", goal_id=key, event_id="evt_test_2")
    result = inspect(core, memory_ids=[mid], episode_ids=[eid])
    assert result["attention"]["items"] == []
    assert key not in {item["id"] for item in result["items"]}
    assert "evt_test_2" not in json.dumps(result)


def test_expired_reader_lease_is_only_a_record_never_presence_or_a_retry(core):
    from tests.support.reading_service import queued

    _, leased, _ = queued(core)
    core.clock.advance(core.settings.lease_seconds + 1)
    before = "\n".join(core.store.db.iterdump())
    result = inspect(core)
    item = next(row for row in result["items"] if row["id"] == leased["request_id"])
    assert item["attempt_state"] == "RUNNING" and not item["lease"]["valid_at_as_of"]
    assert item["lease"]["worker_presence"] == "UNKNOWN"
    assert "\n".join(core.store.db.iterdump()) == before


def test_watched_is_an_analysis_receipt_and_never_current_playback(core):
    key = setup_goal(core)
    core.goals.accept(key)
    step = core.goals.claim()
    enable_observations(core, source="web", media=True)
    core.settings.web_reading_enabled = True
    core.settings.web_reading_hosts = ("example.org",)
    body = observation(core, 2, source="web", source_kind="video_analysis",
                       result_status="WATCHED", source_event_ids=["evt_test_1"]).model_dump()
    body["payload"].update(duration_seconds=20, media_sha256="a" * 64,
        processor_id="synthetic-analyzer", analyzed_spans=[{
            "start_seconds": 0, "end_seconds": 20, "sha256": "b" * 64}])
    core.observations.append("reader", ObservationEvent.model_validate(body), reading=True)
    core.goals.finish(step, body["event_id"])
    result = inspect(core)
    item = next(item for item in result["items"] if item["id"] == key)
    assert item["result"]["meaning"] == "MEDIA_ANALYSIS_COVERAGE"
    assert result["presence"]["state"] == "UNKNOWN"
    assert result["progress"] == result["interruptibility"] == "NOT_RECORDED"


def test_source_cap_never_silently_drops_required_goal_roots(core):
    key = setup_goal(core)
    for number in range(2, 103):
        ingest(core, number, "合成来源")
        core.store.insert("goal_evidence", goal_id=key, event_id=f"evt_test_{number}")
    result = inspect(core)
    assert result["truncated"] and key not in {item["id"] for item in result["items"]}


def test_corrected_memory_reuses_native_correction_lineage(core):
    old = remember(core, 1, "项目约定周二交接", confirmed=True)
    revised = core.memory.decide(old, "correct", core.clock(), "项目约定周三交接")["memory_id"]
    result = inspect(core, memory_ids=[old, revised])
    assert [item["id"] for item in result["attention"]["items"]] == [revised]
    assert "项目约定" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("boundary", ["expiry", "read_revoke", "processing_withdrawal"])
def test_future_read_permission_and_historical_processing_stay_separate(core, boundary):
    from tests.support.source_grants import prepare
    from tests.test_heybox_reader import URL

    key = setup_goal(core, url=URL)
    grant = prepare(core)
    core.goals.accept(key)
    step = core.goals.claim()
    evidence = observation(core, text="原始授权阅读结果", source="heybox",
                           source_event_ids=["evt_test_1"],
                           source_grant=grant, payload={"text": "原始授权阅读结果", "url": URL})
    core.observations.append("heybox", evidence, reading=True)
    core.goals.finish(step, evidence.event_id)
    assert goal_item(core, key)["result"]["event_id"] == evidence.event_id
    if boundary == "expiry":
        core.clock.advance(3601)
    elif boundary == "read_revoke":
        revoked = core.observations.grants.revoke(grant.grant_id)
        assert revoked["history_erased"] is False
    else:
        forgotten = core.observations.grants.forget(grant.grant_id, core.ledger)
        assert forgotten["history_erased"] is True
    before = "\n".join(core.store.db.iterdump())
    result = inspect(core)
    assert "\n".join(core.store.db.iterdump()) == before
    if boundary == "processing_withdrawal":
        assert evidence.event_id not in json.dumps(result)
    else:
        assert goal_item(core, key)["result"]["event_id"] == evidence.event_id


@pytest.mark.parametrize("body", ["broken", "{}", '["forged"]', "[[]]"])
def test_malformed_or_unavailable_reader_result_cannot_leak_an_id(core, body):
    from tests.support.reading_service import queued

    _, leased, _ = queued(core)
    core.store.execute("UPDATE reading_attempts SET result_event_ids_json=?", (body,))
    result = inspect(core)
    assert result["truncated"]
    assert leased["request_id"] not in {item["id"] for item in result["items"]}
    assert "forged" not in json.dumps(result)


def test_read_does_not_end_the_callers_transaction(core):
    setup_goal(core)
    with core.store.transaction():
        with pytest.raises(CoreError, match="ACTIVITY_SNAPSHOT_BUSY"):
            inspect(core)
        assert core.store.db.in_transaction


def test_restore_replays_ledger_before_source_and_attention_rebuild(core, tmp_path):
    mid = remember(core, 1, "后来明确遗忘的内容", confirmed=True)
    cid, eid = conversation(core), episode(core, 1)
    archive, restored = tmp_path / "before.enc", tmp_path / "restored.db"
    secret = Fernet.generate_key()
    backup(core.settings.database, archive, secret)
    core.memory.decide(mid, "forget", core.clock())
    restore(archive, restored, secret, core.settings.erasure_ledger)
    with closing(Store(str(restored))) as store:
        from types import SimpleNamespace

        memory = Memory(store, core.settings, core.ledger)
        reader = CognitionRead(SimpleNamespace(store=store, memory=memory,
                                              settings=core.settings, clock=core.clock))
        result = reader.activity_snapshot(
            {"conversation_id": cid, "memory_ids": [mid], "episode_ids": [eid]})
        assert result["items"] == result["attention"]["items"] == []
