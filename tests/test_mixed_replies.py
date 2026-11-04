"""One SDK decision, native group custody and independent checked media delivery."""

import json

import httpx
import pytest

from companion import owner_delivery
from companion.contracts import CoreError
from companion.reply_segments import SegmentedReplyConfig
from companion.transport import reply_group, segment_envelopes
from companion_onebot_bridge import reply_segments
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.onebot import provider as onebot_provider
from tests.test_image_generation import config, draw, image_reply, owner_text, png
from tests.test_onebot_voice_pipeline import tick
from tests.test_prepare_owner_input import native_spool, window
from tests.test_voice_flow import finish_node
from tests.test_voice_jobs import voice_config
from tests.voice_owner_fixtures import native_voice


@pytest.mark.parametrize(
    "kinds", [("voice", "text"), ("text", "voice"), ("voice", "text", "voice")]
)
def test_sdk_freezes_per_bubble_choices_in_one_final_generation(core, tmp_path, kinds):
    async def scenario():
        core.settings.segmented_reply = SegmentedReplyConfig(interval_ms=0)
        native_voice(core, tmp_path, record=False, config=voice_config(reply={"mode": "model"}))
        parts = [
            {"kind": kind, "text": f"这是第{index + 1}段内容。"} for index, kind in enumerate(kinds)
        ]
        provider = Provider(
            answer(calls=[call("mixed-final", "reply_with_parts", {"parts": parts})])
        )
        async with worker_for(core, provider) as worker:
            assert await worker.run_once("mixed-parts")
        assert len(provider.requests) == 1
        rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
        assert len(rows) == len(parts)
        assert [json.loads(row["content_json"])["text"] for row in rows] == [
            part["text"] for part in parts
        ]
        assert [json.loads(row["content_json"])["reply_part"]["kind"] for row in rows] == list(
            kinds
        )
        group = reply_group(core.store, rows[0]["message_id"])
        envelopes = segment_envelopes(group)
        assert all(value["version"] == 3 for value in envelopes.values())
        assert not core.store.rows("SELECT * FROM voice_jobs")
        for row in rows:
            content = json.loads(row["content_json"])
            item = {
                **row,
                "content": content,
                "authority": {"present": True},
                "reply_segment": envelopes[row["message_id"]],
            }
            assert reply_segments.envelope(item)["version"] == 3
        # The per-item kind and complete model composition are frozen in owner custody.
        first = rows[0]
        content = json.loads(first["content_json"])
        content["reply_part"]["kind"] = "text" if kinds[0] == "voice" else "voice"
        core.store.execute(
            "UPDATE outbox SET content_json=? WHERE message_id=?",
            (json.dumps(content), first["message_id"]),
        )
        with pytest.raises(CoreError):
            owner_delivery.check_delivery(
                core.store, core.settings, core.clock, first["message_id"]
            )

    run(scenario())


@pytest.mark.parametrize(
    "order", [("voice", "generated_image", "text"), ("generated_image", "voice", "text")]
)
@pytest.mark.parametrize("outcome", ["sent", "voice_timeout", "unknown"])
def test_native_voice_picture_and_text_keep_order_and_original_receipts(
    core,
    tmp_path,
    order,
    outcome,
):
    async def scenario():
        core.settings.voice = voice_config(reply={"mode": "model", "wait_seconds": 2})
        core.settings.segmented_reply = SegmentedReplyConfig(interval_ms=0)
        async with window(core, tmp_path, expression=True, voice=True) as (
            client,
            bridge_config,
            plan,
        ):
            core.settings.image_generation = config()
            await owner_text(
                core,
                client,
                bridge_config,
                plan,
                tmp_path,
                "请用语音说句话、画一只猫，再用文字补充一句。",
            )
            provider = Provider(draw())

            def model(request):
                if not provider.responses:
                    image = core.store.one(
                        "SELECT image_id FROM generated_images WHERE state='SUCCEEDED'"
                    )
                    parts = [
                        {"kind": kind, "image_id": image["image_id"]}
                        if kind == "generated_image"
                        else {
                            "kind": kind,
                            "text": "这句用语音说。" if kind == "voice" else "这句是独立补充文字。",
                        }
                        for kind in order
                    ]
                    provider.responses.append(
                        answer(calls=[call("mixed-final", "reply_with_parts", {"parts": parts})])
                    )
                return provider(request)

            async with worker_for(core, model) as worker:
                worker.image_transport = httpx.MockTransport(lambda request: image_reply(png()))
                assert await worker.run_once("mixed-native-image")
            assert len(provider.requests) == 2
            rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
            assert [json.loads(row["content_json"])["reply_part"]["kind"] for row in rows] == list(
                order
            )
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            spool, runtime = native_spool(core, tmp_path, bridge_config, plan)
            sends = []

            def transport(request):
                lose = outcome == "unknown" and len(sends) == order.index("voice")
                failure = httpx.ReadTimeout("synthetic lost ACK", request=request) if lose else None
                if request.url.path == "/send_private_msg":
                    body = json.loads(request.content)
                    message = body["message"]
                    if message[0]["type"] == "record":
                        assert len(message) == 1
                        assert message[0]["data"]["file"].startswith("base64://")
                        sends.append(body)
                        if failure:
                            raise failure
                        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                                        "data": {"message_id": 123456}})
                return onebot_provider(request, sends, failure)

            try:
                async with private_http_client(
                    bridge_config.core_url, bridge_config.bridge_token
                ) as bridge, private_http_client(
                    bridge_config.onebot_http_url,
                    bridge_config.onebot_token,
                    transport=httpx.MockTransport(transport),
                ) as protocol:
                    for kind in order:
                        if kind == "voice":
                            assert not await tick(runtime, bridge, protocol)
                            if outcome == "voice_timeout":
                                core.clock.advance(3)
                            else:
                                finish_node(core.voice.jobs)
                        assert await tick(runtime, bridge, protocol)
                        if kind == "voice" and outcome == "unknown":
                            break
                    expected = [
                        "record"
                        if kind == "voice" and outcome != "voice_timeout"
                        else "image"
                        if kind == "generated_image"
                        else "text"
                        for kind in order[: len(sends)]
                    ]
                    assert [item["message"][0]["type"] for item in sends] == expected
                    assert all(len(item["message"]) == 1 for item in sends)
                    current = core.store.rows(
                        "SELECT state,attempt_count FROM outbox ORDER BY segment_index"
                    )
                    if outcome == "unknown":
                        assert current[order.index("voice")] == {
                            "state": "DELIVERY_UNCERTAIN",
                            "attempt_count": 1,
                        }
                        assert runtime.row()["state"] == "STOPPED"
                    else:
                        assert current == [{"state": "SENT", "attempt_count": 1}] * 3
                        assert not await tick(runtime, bridge, protocol)
                    assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
                    if outcome != "unknown":
                        runtime.verify()
            finally:
                spool.close()

    run(scenario())
