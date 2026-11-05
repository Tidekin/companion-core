"""Captured reference bytes keep the original expiry and erasure boundary."""
import json

import httpx
import pytest

from companion import image_spool
from companion.owner_authority_protocol import OwnerCapture
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.test_native_image_pipeline import admit_image, claim, pixels, upload_headers
from tests.test_owner_recall import receipt
from tests.test_prepare_owner_input import window


@pytest.mark.parametrize("kind", ["image", "file"])
def test_nested_media_requires_its_current_purpose_before_capture(core, tmp_path, kind):
    async def scenario():
        async with window(core, tmp_path) as (client, config, plan):
            async def read_denial(response):
                if response.status_code == 403:
                    await response.aread()
            client.event_hooks["response"].append(read_denial)
            segment = ({"type": "image", "data": {"url": "https://gchat.qpic.cn/original"}}
                if kind == "image" else {"type": "file", "data": {
                    "file_id": "original", "file": "notes.txt", "file_size": 12}})
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await admit_image(core, client, config, plan, tmp_path, message=[
                    {"type": "reply", "data": {"id": "-42", "message": [segment]}}])
            assert denied.value.response.status_code == 403
            assert denied.value.response.json()["error"] == "OWNER_AUTHORITY_PURPOSE_REQUIRED"
            assert not core.store.one("SELECT 1 FROM events")
            assert not core.store.one("SELECT 1 FROM model_usage")
    run(scenario())


@pytest.mark.parametrize("lifecycle", ["followup", "expiry", "recall"])
def test_native_nested_original_is_reused_or_retired_without_renewal(core, tmp_path, lifecycle):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, history=True) as (
            client, config, plan,
        ):
            first, image = await admit_image(core, client, config, plan, tmp_path, message=[
                {"type": "reply", "data": {"id": "-42", "message": [
                    {"type": "image", "data": {
                        "url": "https://gchat.qpic.cn/nested-original"}}]}}])
            assert image["path"] == (0, 0)
            lease = await claim(client, first, image)
            body = pixels()
            endpoint = "/internal/v2/content/" + first["event_id"] + "/image"
            headers = upload_headers(plan, first, image, lease, body)
            uploaded = await client.post(endpoint, content=body, headers=headers)
            assert uploaded.status_code == 200, uploaded.text
            frozen = core.store.one("SELECT * FROM captured_images")
            content = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            path = image_spool.directory(core.store) / (content["item_id"] + ".raw")
            assert path.exists()
            if lifecycle == "expiry":
                core.clock.advance(301)
            latest, _ = await admit_image(core, client, config, plan, tmp_path, number=43,
                message=[{"type": "text", "data": {"text": "继续刚才的话题。"}}])
            if lifecycle == "recall":
                original = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                    ("owner_input:" + first["event_id"],))["value"])
                withdrawn = await client.post("/internal/v1/owner-authority/inputs/recall",
                    json=receipt(core, OwnerCapture.model_validate(original["capture"]),
                                 plan.key).model_dump(mode="json"))
                assert withdrawn.status_code == 200, withdrawn.text
                assert not core.store.one("SELECT 1 FROM captured_images")
                assert not path.exists()
            provider = Provider(answer("按现有资料继续回答。"))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("nested-followup")
                if lifecycle == "expiry":
                    # The foreground refuses expired bytes; physical retirement
                    # belongs to the bounded native maintenance lane.
                    assert path.exists()
                    assert worker.cleanup_media() == 1
            assert len(provider.requests) == 1
            messages = provider.requests[0]["messages"]
            images = [part for message in messages if isinstance(message["content"], list)
                      for part in message["content"] if part["type"] == "image_url"]
            assert len(images) == (1 if lifecycle == "followup" else 0)
            assert "/nested-original" not in encode(messages)
            assert messages[-1]["content"] == "继续刚才的话题。"
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]}]
            retained = core.store.one("SELECT * FROM captured_images")
            if lifecycle != "recall":
                assert retained == frozen
            if lifecycle != "followup":
                assert not path.exists()
                replay = await client.post(endpoint, content=body, headers=headers)
                assert replay.status_code in {403, 409, 410}
                assert not path.exists()
            assert len(core.store.rows("SELECT * FROM model_usage")) == 1
    run(scenario())
