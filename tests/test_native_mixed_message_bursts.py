"""Mixed owner messages reach the SDK with original images and Reader evidence."""

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from companion import api
from companion.core import Core
from companion_onebot_bridge import pending_media
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.sdk_reader_fixture import answer, call
from tests.support.live_image_input import options as image_options
from tests.support.live_image_input import png
from tests.support.onebot_images import image_segment
from tests.test_live_card_content import TOKEN, URL, result
from tests.test_live_card_content import options as card_options
from tests.test_native_bili_video import sdk_step
from tests.test_qq_rich_input import card


@pytest.mark.parametrize("layout", ["same_message", "card_image_text", "image_card_text"])
def test_card_image_and_question_keep_all_sources_through_independent_workers(
    core, tmp_path, monkeypatch, layout,
):
    # The HTTP reader result and child SDK worker must use the same fixture clock;
    # wall-clock completion can otherwise schedule the resume ahead of the child.
    monkeypatch.setattr(api, "Core", lambda *args, **kwargs: Core(
        *args, clock=core.clock, **kwargs))
    text = {"type": "text", "data": {"text": "结合文章和这张图回答我的问题"}}
    attachment = image_segment("https://gchat.qpic.cn/mixed-original")
    shared = card(url=URL)
    messages = ([[shared, attachment, text]] if layout == "same_message" else
                [[shared], [attachment], [text]] if layout == "card_image_text" else
                [[attachment], [shared], [text]])
    downloads = []

    async def source(ws):
        for number, message in enumerate(messages, 1):
            await ws.send(frame(number, message=message))
        await ws.wait_closed()

    async def resolve(*_):
        return ["1.1.1.1"]

    def download(request):
        downloads.append(request.url.path)
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    images, cards = image_options(), card_options()
    options = {**cards["core_options"], **images["core_options"],
        "rich_input_scope": "references_v1", "observation_adapters": {
            **cards["core_options"]["observation_adapters"],
            **images["core_options"]["observation_adapters"],
        }}

    def assert_inputs(payload, *, article=False):
        messages = payload["messages"]
        parts = [part for message in messages if isinstance(message.get("content"), list)
                 for part in message["content"] if part["type"] == "image_url"]
        assert len(parts) == 1
        assert base64.b64decode(parts[0]["image_url"]["url"].split(",", 1)[1]) == png()
        material = json.dumps(messages, ensure_ascii=False)
        assert text["data"]["text"] in material and URL in material
        if article:
            assert "作者比较两款耳机的佩戴体验" in material
            assert any(message["role"] == "tool" and message.get("tool_call_id") == "read-card"
                       for message in messages)

    async def scenario():
        async with host(core, tmp_path, source, core_options=options,
            paid_options=images["paid_options"], purposes=("chat", "content", "image", "history"),
            bridge_options={"rich_input_scope": "references_v1"}, independent_api=True,
        ) as (active, handshakes, sends, _):
            await active.capture(len(messages))
            await active.flush()
            # WS receipt time is real UTC; the shared fixture ticker can lag it.
            # Align after flush so earlier captures are historical before claim.
            core.clock.now = max(core.clock.now, datetime.now(UTC))
            assert handshakes == ["Bearer " + active.config.onebot_token]
            latest = core.store.one(
                "SELECT * FROM jobs WHERE kind='chat' ORDER BY rowid DESC LIMIT 1")
            assert core.claim_job("wait-for-mixed-original", job_id=latest["job_id"]) is None
            pump = pending_media.MediaPump(active.runtime, active.client, protocol=active.onebot,
                image_transport=httpx.MockTransport(download), image_resolver=resolve)
            row = pending_media._execute(active.runtime,
                "SELECT * FROM automatic_media ORDER BY rowid").fetchone()
            await pump.execute(row)
            assert len(downloads) == 1
            assert core.store.one("SELECT state FROM content_items WHERE kind='qq_image'")[
                "state"] == "SUCCEEDED"
            first = await sdk_step(core, tmp_path, latest["job_id"], [answer(calls=[
                call("read-card", "read_content", {"url": URL}),
            ])], "RETRY_WAIT", "mixed-select")
            assert len(first) == 1
            assert_inputs(first[0])
            assert not core.store.one("SELECT 1 FROM outbox")

            auth = {"Authorization": "Bearer " + TOKEN}
            prefix = "/internal/v3/readers/reader"
            response = await active.client.post(prefix + "/lease", json={}, headers=auth)
            assert response.status_code == 200, response.text
            work = response.json()["item"]
            assert work and work["url"] == URL
            checked = await active.client.post(prefix + "/" + work["request_id"] + "/validate",
                json={"lease_token": work["lease_token"], "source_grant": None}, headers=auth)
            assert checked.status_code == 200 and checked.json() == {"valid": True}
            finished = await active.client.post(prefix + "/" + work["request_id"] + "/result",
                json=result(work, core).model_dump(mode="json"), headers=auth)
            assert finished.status_code == 200, finished.text
            final = await sdk_step(core, tmp_path, latest["job_id"],
                [answer("结合原图和文章回答。")], "SUCCEEDED", "mixed-answer")
            assert len(final) == 1
            assert_inputs(final[0], article=True)
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]}]
            assert len(core.store.rows("SELECT * FROM reading_attempts")) == 1
            assert core.store.rows("SELECT state FROM model_usage") == [
                {"state": "SETTLED"}, {"state": "SETTLED"}]
            assert await active.pump()
            assert len(sends) == 1 and active.attempts() == {"ACK": 1}
            active.runtime.verify()

    run(scenario())
