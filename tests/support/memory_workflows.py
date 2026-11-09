import json

from tests.conftest import ingest, run


def message(core, number, text, origin="qq:FriendMessage:10001"):
    core.settings.memory_enabled = True
    ingest(core, number, text, origin=origin)
    assert run(core.run_once())
    return core.store.one('SELECT * FROM jobs WHERE event_id=%s', (f"evt_test_{number}",))


def reply(core, number):
    row = core.store.one(
        'SELECT content_json FROM outbox WHERE origin_event_id=%s', (f"evt_test_{number}",)
    )
    return json.loads(row["content_json"])["text"]
