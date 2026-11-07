from companion.transport import enqueue
from tests.conftest import ingest


def sent_history(core, number, text, reply="已收到这一轮。", state="SENT", **kwargs):
    ingest(core, number, text, **kwargs)
    event_id = f"evt_test_{number}"
    row = core.store.one('SELECT * FROM events WHERE event_id=%s', (event_id,))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id=%s", (event_id,))
    if state is not None:
        message_id = enqueue(core.store, event_id, row["conversation_id"], reply, core.clock())
        core.store.execute('UPDATE outbox SET state=%s WHERE message_id=%s', (state, message_id))
        return message_id


def current_job(core, number=99, text="刚才聊到哪了？", **kwargs):
    ingest(core, number, text, **kwargs)
    return core.store.one(
        (
            'SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events'
            ' e ON e.event_id=j.event_id WHERE j.event_id=%s'
        ),
        (f"evt_test_{number}",),
    )
