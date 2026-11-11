"""Recent-turn lookup must not aggregate all previously delivered replies."""

import pytest

from companion.dialogue_history import recent_sent_sources
from tests.support.dialogue_context import current_job, sent_history


def test_partial_history_snapshot_uses_the_selected_delivery_without_changing_proof(
    core, monkeypatch,
):
    for number in range(1, 16):
        sent_history(core, number, f"合成轮次{number}", f"合成回复{number}")
    core.store.execute("UPDATE outbox SET sent_at=%s WHERE state='SENT'", (core.clock(),))
    job = current_job(core, 16)
    reads = []
    original = core.store.one

    def one(statement, parameters=()):
        if statement.startswith("SELECT outbox.message_id AS message_id"):
            reads.append(parameters[0])
        return original(statement, parameters)

    monkeypatch.setattr(core.store, "one", one)

    def history():
        return recent_sent_sources(core.store, job["conversation_id"], job["event_id"], 15,
                                   include_partial=True)

    with core.store.transaction():
        expected = history()
    assert len(expected) == len(reads) == 15
    reads.clear()
    with core.store.read_snapshot():
        assert history() == expected
    assert not reads
    assert all("history_delivery" not in row for row in expected)


@pytest.mark.parametrize("damage", ["missing_ack", "content_digest"])
def test_snapshot_partial_history_keeps_delivery_integrity_checks(core, damage):
    message_id = sent_history(core, 1, "合成输入", "合成回复")
    core.store.execute("UPDATE outbox SET sent_at=%s WHERE message_id=%s",
                       (core.clock(), message_id))
    job = current_job(core, 2)
    with core.store.read_snapshot():
        assert len(recent_sent_sources(core.store, job["conversation_id"], job["event_id"], 1,
                                       include_partial=True)) == 1
    if damage == "missing_ack":
        core.store.execute("UPDATE outbox SET sent_at=NULL WHERE message_id=%s", (message_id,))
    else:
        core.store.execute("UPDATE outbox SET content_sha256=%s WHERE message_id=%s",
                           ("0" * 64, message_id))
    with core.store.read_snapshot():
        assert not recent_sent_sources(core.store, job["conversation_id"], job["event_id"], 1,
                                       include_partial=True)


def test_recent_sources_work_does_not_follow_tenfold_history(core, record_property):
    def seed(start, stop):
        for number in range(start, stop):
            sent_history(core, number, f"合成轮次{number}", f"合成回复{number}")

    def measure(job):
        core.store.execute("ANALYZE events, outbox, jobs")
        count = ("SELECT sum(seq_tup_read + idx_tup_fetch) AS n FROM pg_stat_xact_user_tables "
                 "WHERE schemaname=current_schema()")
        with core.store.read_snapshot():
            before = core.store.one(count)["n"]
            rows = recent_sent_sources(core.store, job["conversation_id"], job["event_id"], 4)
            visits = core.store.one(count)["n"] - before
        assert len(rows) == 4 and visits > 0
        return int(visits)

    seed(1, 121)
    small = measure(current_job(core, 121))
    seed(122, 1202)
    large = measure(current_job(core, 1202))
    record_property("history_tuple_reads_1x_10x", f"{small},{large}")
    assert large <= small * 1.5 + 4
