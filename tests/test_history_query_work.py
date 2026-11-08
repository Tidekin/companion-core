"""Recent-turn lookup must not aggregate all previously delivered replies."""

from companion.dialogue_history import recent_sent_sources
from tests.support.dialogue_context import current_job, sent_history


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
