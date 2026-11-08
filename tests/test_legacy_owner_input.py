"""Archived v1 evidence is read without reviving its old execution authority."""

import hashlib
from pathlib import Path

import pytest

from companion import legacy_owner_input, owner_input
from companion.contracts import CoreError
from companion.owner_authority_protocol import OwnerCapture
from companion.qq_rich import input_digest
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import event
from tests.support.legacy_owner_input import archived


def turn(core, job):
    from companion.dialogue_history import recent_sent_sources

    return recent_sent_sources(core.store, job["conversation_id"], "", 5,
                               include_partial=True, cutoff_rowid=10000)[0]


@pytest.mark.parametrize("private,segmented", [(False, False), (True, False), (True, True)])
def test_v1_sent_history_preserves_original_custody(core, tmp_path, private, segmented):
    _, view, job, mids, _ = archived(core, tmp_path, private=private, segmented=segmented)
    before = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],))
    source = owner_input.source(core.store, core.settings, view, job["event_id"],
                                core.clock(), history=True)
    assert source[2]["authority_json"] == "{}"
    item = turn(core, job)
    legacy_owner_input.validate_turn(core.store, core.settings, view, item, core.clock())
    assert item["reply_text"] == "Already sent."
    assert item["message_id"] == mids[0]
    legacy_owner_input.source(core.store, core.settings, view, job["event_id"],
                              core.clock(), purpose="memory")
    assert core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],)) == before
    with pytest.raises(CoreError):
        owner_input.source(core.store, core.settings, view, job["event_id"], core.clock())


@pytest.mark.parametrize("damage", ["unsigned", "archive", "approval", "signature", "receipt",
                                    "ack", "payload", "journal", "erased"])
def test_v1_unprovable_history_is_rejected(core, tmp_path, damage):
    policy, view, job, mids, _ = archived(core, tmp_path)
    item = turn(core, job)
    if damage in {"unsigned", "archive", "receipt"}:
        key = {"unsigned": "live_text_input:" + job["event_id"],
               "archive": "live_text_window:" + policy.plan_sha256,
               "receipt": "live_text_reply:" + mids[0]}[damage]
        core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (key,))
    elif damage == "approval":
        core.store.execute((
            "UPDATE runtime_meta SET value=jsonb_set(value::jsonb,"
            "'{binding,approval_sha256}',to_jsonb(%s::text))::text WHERE key=%s"
        ),
            ("f" * 64, "live_text_window:" + policy.plan_sha256))
    elif damage == "signature":
        core.store.execute((
            "UPDATE runtime_meta SET value=jsonb_set(value::jsonb,"
            "'{capture,signature}',to_jsonb(%s::text))::text WHERE key=%s"
        ), ("0" * 128, "live_text_input:" + job["event_id"]))
    elif damage == "ack":
        core.store.execute("UPDATE outbox SET provider_message_id=NULL")
    elif damage == "payload":
        core.store.execute('UPDATE model_admissions SET payload_sha256=%s', ("f" * 64,))
    elif damage == "journal":
        Path(core.settings.paid_model.journal_path).write_text("{}\n", encoding="utf-8")
    elif damage == "erased":
        core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s', (job["event_id"],))
    with pytest.raises(CoreError):
        legacy_owner_input.validate_turn(core.store, core.settings, view, item, core.clock())


def test_native_history_selector_keeps_only_proven_legacy_sent_prefix(core, tmp_path):
    _, view, old_job, _, key = archived(core, tmp_path, private=True, segmented=True)
    message = event(core, 2, text="Current signed owner input",
                    origin="onebot-direct:FriendMessage:10001")
    source_id = (f"{core.settings.bot_qq_id}:{message.conversation.platform_session_key}"
                 ":epoch:new-epoch:2")
    message = message.model_copy(update={"source_event_id": source_id,
        "event_id": "evt_" + hashlib.sha256(source_id.encode()).hexdigest()})
    value = dict(kind="authenticated_onebot_ws", authority=view.authority.as_dict(),
        event_id=message.event_id, source_event_id=source_id,
        input_sha256=input_digest(message.payload.model_dump(mode="json")),
        frame_sha256="a" * 64, evidence_sha256="b" * 64,
        identity_sha256=view.policy.capture.identity_sha256, protocol_epoch="new-epoch",
        received_at=message.occurred_at.isoformat(), occurred_at=message.occurred_at.isoformat())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    accepted = core.ingest(message, "current-after-archive")
    job = core.claim_job("history-test-worker", job_id=accepted["job_id"])
    assert job
    # An unrelated privacy operation does not invalidate retained original evidence.
    core.store.execute(
        "INSERT INTO runtime_meta (key,value) VALUES ('privacy_epoch','1') ON"
        ' CONFLICT(key) DO UPDATE SET '
        'value=excluded.value,record_sequence=DEFAULT'
    )
    result = owner_input.history_sources(core.store, core.settings, core.clock, job)
    assert [(row["event_id"], row["reply_text"]) for row in result] == [
        (old_job["event_id"], "Already sent.")]
    core.store.execute("DELETE FROM runtime_meta WHERE key LIKE 'private_life_delivery:%%'")
    assert owner_input.history_sources(core.store, core.settings, core.clock, job) == []


@pytest.mark.parametrize("boundary", ["input", "sent"])
def test_clock_rollback_does_not_expose_future_legacy_history(core, tmp_path, boundary):
    _, view, job, mids, _ = archived(core, tmp_path)
    item = turn(core, job)
    if boundary == "input":
        core.clock.advance(-61)  # One second before the original signed frame.
    else:
        # Archived delivery occurred 30 seconds after input, before grant creation.
        core.store.execute('UPDATE outbox SET sent_at=%s WHERE message_id=%s',
                           (after(core.clock(), -30), mids[0]))
        core.clock.advance(-45)  # Input is past, but the confirmed delivery is future.
    with pytest.raises(CoreError):
        legacy_owner_input.validate_turn(core.store, core.settings, view, item, core.clock())
