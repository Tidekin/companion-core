from companion import reply_revision
from tests.conftest import ingest


def accept(core, number=1, text="一起聊接下来的计划", **kwargs):
    """Exercise the durable transition without enabling the production live guard."""
    result = ingest(core, number, text, **kwargs)
    if not result["duplicate"]:
        event = core.store.one("SELECT * FROM events WHERE event_id=?", (result["event_id"],))
        with core.store.transaction():
            result["reply_revision"] = reply_revision.advance(
                core.store, event["conversation_id"], event["event_id"], core.clock(),
            )
    return result
