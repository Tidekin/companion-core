"""New image bytes reach the independent SDK worker and ACK before the next turn."""

import json

import httpx
import pytest

from companion import image_spool, image_understanding
from companion_onebot_bridge import pending_media
from tests.conftest import run
from tests.native_continuous_support import until
from tests.test_native_image_bursts import (
    bridge_tasks,
    model_images,
    native_host,
    observe_delivery,
    picture,
    resolve,
    send,
    wait_for_ack,
)
from tests.test_onebot_images import image_segment
from tests.test_owner_image_recovery import run_worker


@pytest.mark.parametrize("count,expire", [(1, False), (3, False), (1, True)],
                         ids=["single_image", "continuous_images", "expired_followup"])
def test_new_images_reach_vision_reply_ack_and_following_text(
    core, tmp_path, monkeypatch, count, expire,
):
    async def scenario():
        async with native_host(core, tmp_path, monkeypatch, count=count,
                               history=True, poll_seconds=1.0) as values:
            active, inbound, handshakes, sends, calls = values
            assert active.config.poll_seconds == 1.0
            retries, responses, rpc = observe_delivery(active, monkeypatch)
            originals = [picture(color) for color in ("red", "blue", "green")][:count]
            downloads, uploads = [], []
            question = "按顺序描述这些图片的颜色"

            def cdn(request):
                index = int(request.url.path.rsplit("/", 1)[1])
                downloads.append(index)
                return httpx.Response(200, content=originals[index],
                                      headers={"Content-Type": "image/png"})

            async def uploaded(response):
                if response.request.url.path.endswith("/image"):
                    assert response.status_code == 200
                    uploads.append(response.request.url.path)

            active.client.event_hooks["response"].append(uploaded)
            pump = pending_media.MediaPump(active.runtime, active.client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
            async with bridge_tasks(active, pump) as tasks:
                for index in range(count):
                    segments = [image_segment(f"https://gchat.qpic.cn/new/{index}", subtype=1)]
                    if count == 1:
                        segments.insert(0, {"type": "text", "data": {"text": question}})
                    await send(core, inbound, index + 1, segments)
                    await until(lambda n=index + 1: core.store.one(
                        "SELECT COUNT(*) n FROM captured_images")["n"] == n,
                        tasks=tasks, deadline_seconds=20)
                inputs = count
                if count > 1:
                    inputs += 1
                    await send(core, inbound, inputs,
                               [{"type": "text", "data": {"text": question}}])
                await until(lambda: len(core.store.rows("SELECT * FROM events")) == inputs,
                            tasks=tasks, deadline_seconds=20)
                stored = core.store.rows("SELECT * FROM content_items WHERE kind='qq_image' "
                                         "ORDER BY rowid")
                assert len(stored) == count and all(row["state"] == "SUCCEEDED" for row in stored)
                assert [image_spool.get(core.store, row, core.clock(), 50000)[0]
                        for row in stored] == originals
                first = await run_worker(core, tmp_path, 1, "已按顺序看到图片颜色。")
                assert len(first) == 1 and model_images(first[0]) == originals
                assert image_understanding.RULES in first[0]["messages"][0]["content"]
                assert question in json.dumps(first[0]["messages"], ensure_ascii=False)
                await wait_for_ack(core, active, sends, tasks, 1, retries, responses, rpc)
                if expire:
                    core.clock.advance(image_spool.RETENTION_SECONDS + 1)
                await send(core, inbound, inputs + 1,
                           [{"type": "text", "data": {"text": "现在继续文字聊天"}}])
                await until(lambda: len(core.store.rows("SELECT * FROM events")) == inputs + 1,
                            tasks=tasks, deadline_seconds=20)
                second = await run_worker(core, tmp_path, 2, "可以，继续文字聊天。")
                assert len(second) == 1
                assert model_images(second[0]) == ([] if expire else originals)
                assert (image_understanding.RULES in second[0]["messages"][0]["content"]) != expire
                assert "现在继续文字聊天" in json.dumps(second[0]["messages"], ensure_ascii=False)
                await wait_for_ack(core, active, sends, tasks, 2, retries, responses, rpc)
                assert [body["message"][0]["data"]["text"] for body in sends] == [
                    "已按顺序看到图片颜色。", "可以，继续文字聊天。"]
                assert downloads == list(range(count)) and len(uploads) == count
                assert active.attempts() == {"ACK": 2}
                assert core.store.rows("SELECT state FROM outbox ORDER BY rowid") == [
                    {"state": "SENT"}, {"state": "SENT"}]
                assert core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid") == [
                    {"scenario": "chat", "state": "SETTLED"},
                    {"scenario": "chat", "state": "SETTLED"}]
                assert active.runtime.row()["state"] == "ACTIVE"
                assert all(not task.done() for task in tasks)
                assert len(handshakes) == 1 and calls == []
                active.runtime.verify()
    run(scenario())
