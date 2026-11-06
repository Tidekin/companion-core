"""Database changes replay custody without copying an unchanged decoded journal."""

import copy
import sqlite3
import stat
from datetime import timedelta
from types import SimpleNamespace

import pytest

from companion_onebot_bridge import journal
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_events import raw_event
from tests.test_bridge_owner_runtime import config as config


@pytest.fixture
def runtime(config):
    journal._clear()
    with Spool(config) as spool:
        current = OwnerRuntime(spool)
        now = current.clock()
        frame = encode(raw_event(text="original", time=int(now.timestamp()),
            self_id=67890, user_id=12345, sender={"user_id": 12345}))
        event = normalize(frame, config, now=now.timestamp())
        current.persist_capture(event, frame, received_at=now, peer=("127.0.0.1", 1))
        item = {"authority": current.authority, "message_id": "msg_" + "a" * 32,
            "lease_token": "lease_" + "b" * 32,
            "lease_until": (now + timedelta(minutes=1)).isoformat(),
            "origin_event_id": event["event_id"], "action_type": "qq.private.reply",
            "content": {"text": "reply"}, "reply_revision": 1}
        OwnerSendGuard(current, None).reserve(item)
        for state in ("IO_STARTED", "ACK"):
            ack = journal.digest("provider") if state == "ACK" else None
            with current.db:
                current.append(state, {"message_id": item["message_id"],
                                       "provider_ack_hash": ack})
                current.db.execute("UPDATE window_attempts SET state=?,provider_ack_hash=?",
                                   (state, ack))
        current.db.execute("CREATE TABLE replay_probe(value INTEGER)")
        current.db.execute("INSERT INTO replay_probe VALUES(0)")
        current.db.commit()
        current.verify()
        yield current
    journal._clear()


def invalidate(runtime, *, foreign=False):
    if foreign:
        with sqlite3.connect(runtime.spool.directory / "spool.sqlite3") as other:
            other.execute("UPDATE replay_probe SET value=value+1")
    else:
        with runtime.db:
            runtime.db.execute("UPDATE replay_probe SET value=value+1")


@pytest.mark.parametrize("foreign", [False, True])
def test_db_only_changes_recheck_projections_without_materializing_or_mutating_records(
    runtime, monkeypatch, foreign,
):
    original = runtime.verify()
    before = copy.deepcopy(original)
    calls = []
    inputs = runtime._inputs

    def check(references):
        calls.append(True)
        return inputs(references)

    def materialize(*args):
        pytest.fail("unchanged decoded records were materialized again")

    monkeypatch.setattr(runtime, "_inputs", check)
    monkeypatch.setattr(journal.marshal, "loads", materialize)
    for _ in range(2):
        invalidate(runtime, foreign=foreign)
        found = runtime.verify()
        assert found == before and original == before
        assert found is not original and found[0] is original[0]
    assert len(calls) == 2
    with runtime.db:
        runtime.db.execute("UPDATE window_inputs SET input_hash=?", ("f" * 64,))
    with pytest.raises(ValueError, match="OWNER_CAPTURE_PROJECTION_CHANGED"):
        runtime.verify()
    assert runtime._verified is None and original == before


def test_db_only_replay_still_checks_current_journal_permissions(runtime, monkeypatch):
    invalidate(runtime)
    original = type(runtime.path).lstat

    def lstat(path):
        info = original(path)
        return (SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=info.st_size)
                if path == runtime.path else info)

    monkeypatch.setattr(type(runtime.path), "lstat", lstat)
    monkeypatch.setattr(journal, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="INVALID_WINDOW_JOURNAL"):
        runtime.verify()
    assert runtime._verified is None


@pytest.mark.parametrize("changed", ["count", "head"])
def test_inconsistent_count_only_result_cannot_reuse_decoded_records(runtime, monkeypatch, changed):
    invalidate(runtime)
    original = journal.read_count

    def read_count(path):
        count, head = original(path)
        return (count + 1, head) if changed == "count" else (count, "f" * 64)

    monkeypatch.setattr(journal, "read_count", read_count)
    with pytest.raises(ValueError, match="OWNER_JOURNAL_RECONCILIATION_REQUIRED"):
        runtime.verify()
    assert runtime._verified is None


def test_journal_append_between_key_and_count_only_read_is_rejected(runtime, monkeypatch):
    invalidate(runtime)
    original = journal.read_count

    def read_count(path):
        journal.append(path, "PROFILE_PROBE_STARTED", {"authority": runtime.authority},
                       at=runtime.clock().isoformat(), expected_head=runtime.row()["journal_head"])
        return original(path)

    monkeypatch.setattr(journal, "read_count", read_count)
    with pytest.raises(ValueError, match="OWNER_JOURNAL_RECONCILIATION_REQUIRED"):
        runtime.verify()
    assert runtime._verified is None


def test_change_during_projection_never_populates_the_verified_cache(runtime, monkeypatch):
    invalidate(runtime)
    original = runtime._inputs

    def inputs(references):
        result = original(references)
        journal.append(runtime.path, "PROFILE_PROBE_STARTED", {"authority": runtime.authority},
                       at=runtime.clock().isoformat(), expected_head=runtime.row()["journal_head"])
        return result

    monkeypatch.setattr(runtime, "_inputs", inputs)
    runtime.verify()
    assert runtime._verified is None
    with pytest.raises(ValueError, match="OWNER_JOURNAL_RECONCILIATION_REQUIRED"):
        runtime.verify()
