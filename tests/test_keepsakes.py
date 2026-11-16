"""An owner-granted keepsake trims the owner's message instead of expiring its memory."""

import json

import pytest

from companion import keepsakes, owner_input
from companion.contracts import Candidate
from companion.memory_authority import source_allowed
from companion.owner_authority_protocol import KeepsakePolicy
from tests.support.owner_input import captured, inbound

TEXT = "synthetic owner input"


def kept_memory(core, tmp_path, *, keepsake=True, importance=0.95):
    core.settings.memory_enabled = True
    core.settings.raw_event_retention_days = 3
    capture, _ = captured(core, tmp_path, purposes=["chat", "memory"],
                          memory_scope="owner_private", source_retention_days=3,
                          **({"keepsake": KeepsakePolicy().model_dump()} if keepsake else {}))
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "keepsake-source")
    with core.store.transaction(control_write=True):
        memory_id = core.memory.add(Candidate(
            subject="user", kind="fact", fact_key="synthetic", content="主人说过 synthetic input",
            evidence_event_id=capture.event_id, evidence_quote=TEXT), core.clock(),
            confirmed=True)
    core.store.execute("UPDATE memories SET importance=%s WHERE memory_id=%s",
                       (importance, memory_id))
    core.store.execute("UPDATE events SET memory_processed=1")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    return memory_id, capture.event_id


def allowed(core, event_id, purpose):
    return source_allowed(core.store, core.settings, core.clock, event_id, purpose)


def test_granted_salient_memory_keeps_an_excerpt_past_retention(core, tmp_path):
    memory_id, event_id = kept_memory(core, tmp_path)
    assert allowed(core, event_id, "memory")
    core.clock.advance(4 * 86400)
    core.expire_raw_events()
    row = core.store.one("SELECT * FROM keepsakes")
    assert (row["event_id"], row["memory_id"], row["reason"], row["until"]) == (
        event_id, memory_id, "SALIENCE", None)
    event = core.store.one("SELECT body_json, redacted FROM events WHERE event_id=%s",
                           (event_id,))
    body = json.loads(event["body_json"])
    assert event["redacted"] == 0 and body["text"] == TEXT
    assert body["keepsake"]["excerpt_sha256"] == keepsakes.digest(TEXT)
    assert core.store.one("SELECT status, confidence FROM memories WHERE memory_id=%s",
                          (memory_id,)) == {"status": "CONFIRMED", "confidence": 1.0}
    assert allowed(core, event_id, "memory")
    assert not allowed(core, event_id, "chat")
    assert core.store.one("SELECT COUNT(*) n FROM audit_log WHERE action='memory.keepsake_kept'"
                          )["n"] == 1
    core.clock.advance(86400)
    core.expire_raw_events()  # A kept message is not selected again.
    assert core.store.one("SELECT COUNT(*) n FROM keepsakes")["n"] == 1


@pytest.mark.parametrize(("keepsake", "importance"), [(False, 0.95), (True, 0.5)])
def test_without_a_grant_or_salience_the_memory_expires_as_before(
    core, tmp_path, keepsake, importance,
):
    memory_id, event_id = kept_memory(core, tmp_path, keepsake=keepsake, importance=importance)
    core.clock.advance(4 * 86400)
    core.expire_raw_events()
    assert core.store.rows("SELECT * FROM keepsakes") == []
    assert core.store.one("SELECT redacted FROM events WHERE event_id=%s",
                          (event_id,))["redacted"] == 1
    assert core.store.one("SELECT confidence FROM memories WHERE memory_id=%s",
                          (memory_id,))["confidence"] == 0


def test_forgetting_the_memory_removes_the_keepsake(core, tmp_path):
    memory_id, event_id = kept_memory(core, tmp_path)
    core.clock.advance(4 * 86400)
    core.expire_raw_events()
    core.memory.decide(memory_id, "forget", core.clock())
    assert core.store.rows("SELECT * FROM keepsakes") == []
    assert core.store.one("SELECT redacted FROM events WHERE event_id=%s",
                          (event_id,))["redacted"] == 1


def test_replay_trims_the_same_excerpt_and_expires_on_mismatch(core, tmp_path):
    memory_id, event_id = kept_memory(core, tmp_path)
    entry = {"event_id": event_id, "memory_id": memory_id, "reason": "SALIENCE",
             "excerpt_sha256": keepsakes.digest(TEXT), "limit": 120, "until": None}
    with core.store.transaction(control_write=True):
        assert keepsakes.apply(core.store, [entry], core.clock()) == ({event_id}, set())
        # Re-applying to the already trimmed body is a no-op with the same result.
        assert keepsakes.apply(core.store, [entry], core.clock()) == ({event_id}, set())
        wrong = {**entry, "excerpt_sha256": "0" * 64}
        assert keepsakes.apply(core.store, [wrong], core.clock()) == (set(), {event_id})


def test_capacity_reports_held_keepsakes_under_a_grant_only(core, tmp_path):
    assert keepsakes.capacity(core.store, core.settings, core.clock) is None
    kept_memory(core, tmp_path)
    core.clock.advance(4 * 86400)
    core.expire_raw_events()
    assert keepsakes.capacity(core.store, core.settings, core.clock) == {
        "held": 1, "max_items": 10000}
