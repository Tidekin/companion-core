"""Recent-turn lookup must not aggregate all previously delivered replies."""

from companion.dialogue_history import recent_sent_sources
from tests.support.dialogue_context import current_job, sent_history


def test_recent_sources_sqlite_work_does_not_follow_tenfold_history(core, record_property):
    def seed(start, stop):
        for number in range(start, stop):
            sent_history(core, number, f"合成轮次{number}", f"合成回复{number}")

    def measure(job):
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return 0

        core.store.db.set_progress_handler(progress, 100)
        try:
            rows = recent_sent_sources(core.store, job["conversation_id"], job["event_id"], 4)
        finally:
            core.store.db.set_progress_handler(None, 0)
        assert len(rows) == 4
        return steps

    seed(1, 121)
    small = measure(current_job(core, 121))
    seed(122, 1202)
    large = measure(current_job(core, 1202))
    record_property("history_vm_steps_1x_10x", f"{small},{large}")
    assert large <= small * 1.5 + 500
