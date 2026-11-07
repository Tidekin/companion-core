from companion.store import encode
from companion_onebot_bridge import pending_media
from companion_onebot_bridge.capture import prepare_owner_input
from tests.support.onebot_events import raw_event


async def captured(core, client, runtime, *, number=42, message=None):
    frame = encode(raw_event(number=number, time=int(core.clock.now.timestamp()),
        message=message or [
        {"type": "image", "data": {"url": "https://gchat.qpic.cn/queue-original"}}]))
    prepared = await prepare_owner_input(runtime, client, None, frame, core.clock.now,
                                          ("127.0.0.1", 3001))
    runtime.persist_capture(prepared["event"], frame, received_at=core.clock.now,
                            peer=("127.0.0.1", 3001), media=prepared["media"])
    runtime.verify()
    assert await runtime.flush_one(client)
    return prepared


def row(runtime):
    return pending_media._execute(runtime, "SELECT * FROM automatic_media").fetchone()
