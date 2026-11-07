from companion.image_input import ImageInputPolicy
from tests.meme_authority_fixture import admit
from tests.support.meme_service import claim, reservation


def configure(core, *, caption=False):
    core.settings.meme_discovery_enabled = True
    core.settings.meme_semantics_enabled = caption
    core.settings.meme_caption_daily_micro_usd = 50000
    return {"max_output_tokens": 4096, **({"meme_input": ImageInputPolicy(
        approval_sha256="e" * 64, estimated_tokens_per_image=100,
        max_bytes_per_image=4000000, max_pixels_per_image=16000000,
        max_images_per_request=5)} if caption else {})}


async def discovery(core, client, config, grant, now):
    event, response = await admit(client, config, grant, now)
    assert response.status_code == 202, response.text
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=?",
                       (response.json()["job_id"],))
    queued = await client.post("/internal/v1/memes/discovery/submit", json={
        "event_id": event["event_id"], "reply_revision": 1,
        "intent": "celebrate", "provider": "community"})
    assert queued.status_code == 200, queued.text
    item = claim(core)
    reservation(core, item)
    return event, item
