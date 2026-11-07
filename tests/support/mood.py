from companion.transport import enqueue
from tests.conftest import ingest


def feedback(core, number=1, text="这个回答很有帮助"):
    core.settings.mood_enabled = True
    ingest(core, number * 2, "一起看看这个问题")
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (f"evt_test_{number * 2}",))
    mid = enqueue(
        core.store, event["event_id"], event["conversation_id"], "有依据的回答", core.clock()
    )
    core.store.execute(
        "UPDATE outbox SET state='SENT',sent_at=%s WHERE message_id=%s", (core.clock(), mid)
    )
    core.clock.advance(1)
    ingest(core, number * 2 + 1, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    return f"evt_test_{number * 2 + 1}"


def erase(core, ids):
    with core.store.transaction():
        core.ledger.expire_events(core.store, ids, core.clock())
