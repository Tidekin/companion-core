import json

from companion import input_debounce, reply_revision
from tests.conftest import ingest


def accept(core, number, text="在吗"):
    """The production order: read the waiting batch, supersede, then delay the new job."""
    result = ingest(core, number, text)
    event = core.store.one("SELECT * FROM events WHERE event_id=?", (result["event_id"],))
    with core.store.transaction():
        due = input_debounce.due(core.store, core.settings, event["conversation_id"],
                                 event["event_id"], json.loads(event["body_json"]), core.clock())
        reply_revision.advance(core.store, event["conversation_id"], event["event_id"],
                               core.clock())
        if due:
            core.store.execute("UPDATE jobs SET next_attempt_at=? WHERE job_id=?",
                               (due, result["job_id"]))
    return result, due
