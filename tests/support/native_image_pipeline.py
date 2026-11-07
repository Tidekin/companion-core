import hashlib
import io

from PIL import Image

from companion.store import encode
from companion_onebot_bridge.capture import prepare_owner_input
from tests.support.onebot_events import raw_event
from tests.support.prepare_owner_input import native_spool


def pixels(mime="image/png"):
    stream = io.BytesIO()
    first = Image.new("RGB", (8, 8), "teal")
    if mime == "image/png":
        first.save(stream, format="PNG")
    else:
        first.save(stream, format="GIF" if mime == "image/gif" else "PNG", save_all=True,
                   append_images=[Image.new("RGB", (8, 8), "orange")], duration=120, loop=0)
    return stream.getvalue()


async def admit_image(core, client, config, plan, tmp_path, *, message=None, number=42):
    spool, runtime = native_spool(core, tmp_path, config, plan)
    try:
        frame = encode(raw_event(number=number, time=int(core.clock.now.timestamp()),
            message=message or [
                {"type": "image", "data": {"url": "https://gchat.qpic.cn/original"}}]))
        prepared = await prepare_owner_input(runtime, client, None, frame, core.clock.now,
                                              ("127.0.0.1", 3001))
        event = prepared["event"]
        assert runtime.persist_capture(event, frame, received_at=core.clock.now,
                                       peer=("127.0.0.1", 3001))
        assert await runtime.flush_one(client)
        assert not await runtime.flush_one(client)
        return event, next(iter(prepared["media"]["images"]), None)
    finally:
        spool.close()


async def claim(client, event, image):
    response = await client.post("/internal/v2/content/" + event["event_id"] + "/image/claim",
        json={"request_key": "image:0", "source_sha256": image["source_sha256"]})
    assert response.status_code == 200, response.text
    return response.json()


def upload_headers(plan, event, image, lease, body, mime="image/png"):
    from companion_delivery_protocol.authority import image_upload_signing_payload

    value = {"authority": plan.authority, "event_id": event["event_id"], "slot": 0,
        "source_sha256": image["source_sha256"], "lease_token": lease["lease_token"],
        "media_sha256": hashlib.sha256(body).hexdigest(), "mime": mime}
    value["signature"] = plan.key.sign(image_upload_signing_payload(value)).hex()
    return {"Content-Type": mime, "X-Content-Key": "image:0",
            "X-Content-Lease": lease["lease_token"], "X-Content-Proof": encode(value)}
