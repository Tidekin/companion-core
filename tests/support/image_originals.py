import hashlib

from companion import image_spool
from companion.store import after, uid
from tests.conftest import ingest
from tests.support.live_image_input import png


def staged_original(core):
    event_id = ingest(core)["event_id"]
    data, item_id = png(), uid("content")
    now = core.clock()
    core.store.insert(
        "content_items", item_id=item_id, owner_event_id=event_id, kind="qq_image", slot=0,
        state="QUEUED", captured_privacy_epoch=0, reserved_bytes=len(data),
        media_sha256=hashlib.sha256(data).hexdigest(),
        fingerprint=hashlib.sha256(b"image/png\0" + data).hexdigest(),
        deadline=after(now, 30), created_at=now, updated_at=now,
    )
    row = core.store.one('SELECT * FROM content_items WHERE item_id=%s', (item_id,))
    image_spool.put(core.store, row, data, "image/png", after(now, image_spool.RETENTION_SECONDS))
    return row, data
