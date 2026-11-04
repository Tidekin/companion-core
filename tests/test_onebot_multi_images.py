"""Ordered authenticated image batches; only synthetic CDN/model transports are used."""

import io
import json

import httpx
import pytest
from PIL import Image

from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.window import digest
from tests.conftest import run
from tests.support.native_images import image_host, raw_event, upload
from tests.support.onebot_events import config
from tests.test_live_image_input import options
from tests.test_onebot_images import image_segment


def image_inputs(count):
    return [image_segment(f"https://gchat.qpic.cn/synthetic-{index}.png") for index in range(count)]


def test_normalized_images_preserve_slots_positions_and_owner_question():
    first, second = image_inputs(2)
    raw = raw_event(message=[
        {"type": "text", "data": {"text": "比较这两张图片"}},
        first,
        {"type": "node", "data": {"content": [image_segment()]}},
        {"type": "text", "data": {"text": "，按发送顺序编号。"}},
        second,
    ])
    payload = normalize(json.dumps(raw), config(), now=raw["time"],
                        rich_input_scope="references_v1", image_scope="owner_image_v1")["payload"]
    assert payload["text"] == "比较这两张图片，按发送顺序编号。"
    assert payload["shared_content"] == [{"kind": "image", "slot": 0}, {"kind": "image", "slot": 1}]
    images = [item for item in payload["rich"]["segments"] if item["type"] == "image"]
    assert [(item["index"], item["slot"], item["source_sha256"]) for item in images] == [
        (1, 0, digest(first)), (4, 1, digest(second)),
    ]
    assert "gchat.qpic.cn" not in json.dumps(payload)


@pytest.mark.parametrize("count, failure", [
    (2, None), (7, None), (3, "second"), (2, "policy"),
])
def test_authenticated_batch_downloads_once_in_order_and_isolates_download_failure(
    core, tmp_path, count, failure,
):
    downloads, uploads, calls = [], [], []
    raw = raw_event(message=[
        {"type": "text", "data": {"text": "按顺序比较这些图片的颜色。"}}, *image_inputs(count),
    ])
    data = []
    for index in range(count):
        out = io.BytesIO()
        Image.new("RGB", (3, 2), ("red", "blue", "green", "yellow")[index % 4]).save(
            out, format="PNG",
        )
        data.append(out.getvalue())

    async def source(ws):
        raw["time"] = int(core.clock.now.timestamp())
        await ws.send(json.dumps(raw))
        await ws.wait_closed()

    async def resolve(host, port):
        assert (host, port) == ("gchat.qpic.cn", 443)
        return ["1.1.1.1"]

    async def cdn(request):
        index = len(downloads)
        assert request.url.path == f"/synthetic-{index}.png"
        downloads.append(request)
        if failure == "second" and index == 1:
            core.clock.advance(61)
            await core.content.run_once()
        return httpx.Response(
            404 if failure and index == 1 else 200,
            content=data[index], headers={"Content-Type": "image/png"},
        )

    def model(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "图1红色，图2蓝色，颜色不同。"}}],
            "usage": {"prompt_tokens": count * 100, "completion_tokens": 20},
        })

    opts = options()
    opts["core_options"]["content"] = opts["core_options"]["content"].model_copy(
        update={"max_images_per_event": count, "wait_seconds": 90},
    )
    opts["paid_options"]["image_input"] = opts["paid_options"]["image_input"].model_copy(
        update={"max_images_per_request": 1 if failure == "policy" else count},
    )

    async def scenario():
        async with image_host(core, tmp_path, source, options=opts, model_reply=model) as (
            active, _, sends, _,
        ):
            async def checked(response):
                if response.is_error:
                    await response.aread()
                    raise AssertionError(response.text)

            if failure is None:
                active.client.event_hooks["response"].append(checked)
            async def observe_upload(request):
                if request.url.path.endswith("/image"):
                    slots = [slot for slot in range(count)
                             if not (failure == "second" and slot == 1)]
                    index = slots[len(uploads)]
                    assert request.headers["X-Content-Key"] == f"image:{index}"
                    assert request.content == data[index]
                    proof = json.loads(request.headers["X-Content-Proof"])
                    assert proof["source_sha256"] == digest(raw["message"][index + 1])
                    uploads.append(request)

            active.client.event_hooks["request"].append(observe_upload)
            await active.capture()
            if failure == "policy":
                with pytest.raises(httpx.HTTPStatusError):
                    await active.flush()
                assert downloads == uploads == calls == []
                assert not core.store.one("SELECT 1 FROM model_usage")
            else:
                await active.flush()
                await upload(active, resolver=resolve, transport=httpx.MockTransport(cdn))
                accepted = count - int(failure == "second")
                assert len(downloads) == count and len(uploads) == accepted and calls == []
                assert not core.store.one("SELECT 1 FROM model_usage")
                assert await core.run_once()
                assert len(calls) == 1
                parts = [part for message in calls[0]["messages"]
                         if isinstance(message["content"], list) for part in message["content"]]
                assert len([part for part in parts if part["type"] == "image_url"]) == accepted
                assert [row["state"] for row in core.store.rows(
                    "SELECT state FROM content_items ORDER BY slot")] == [
                        "FAILED" if failure == "second" and slot == 1 else "SUCCEEDED"
                        for slot in range(count)]
                assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1
                assert core.store.one("SELECT state FROM outbox")["state"] == "PENDING"
            assert not sends

    run(scenario())
