"""Native API/worker composition with generated media and synthetic provider transport."""

import base64
import io
import json

import av
import httpx
import pytest
from pydantic import SecretStr

from companion import budget, owner_authority, paid_budget
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from companion.video_input import encode_animation, preview, text_and_estimate
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call, worker_for
from tests.support.animated_owner_images import animation, policy
from tests.support.failure_notice import assert_failure_notice
from tests.support.live_image_input import png
from tests.support.onebot_images import image_segment
from tests.support.prepare_owner_input import native_spool, window
from tests.support.video_understanding import video_policy


@pytest.mark.parametrize("format,mime", [
    ("GIF", "image/gif"), ("WEBP", "image/webp"), ("PNG", "image/apng"),
])
def test_animation_keeps_timeline_and_ordered_preview(format, mime):
    data, image_policy, video = animation(format), policy(), video_policy()
    first, animated = preview(data, mime, image_policy)
    assert animated and "抽样预览" in first.note and "300ms" in first.note
    from PIL import Image

    with Image.open(io.BytesIO(first.data)) as frame:
        assert frame.size == (36, 8) and frame.n_frames == 1
    encoded = encode_animation(data, mime, image_policy, video)
    assert 0.3 <= encoded.seconds <= 0.34
    with av.open(io.BytesIO(encoded.data)) as container:
        assert len(container.streams) == 1
        frames = [frame.to_image() for frame in container.decode(video=0)]
        first_color, last_color = frames[0].getpixel((2, 2)), frames[-1].getpixel((2, 2))
        assert first_color[0] > first_color[2] + 100
        assert last_color[2] > last_color[0] + 100
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "说明动作。"}, encoded.part(),
    ]}]
    assert 60 <= text_and_estimate(video, messages)[1] <= 70


def test_video_limits_and_remote_urls_are_rejected_locally():
    with pytest.raises(CoreError, match="CONTENT_VIDEO_INPUT_INVALID"):
        encode_animation(animation(count=20), "image/gif", policy(), video_policy(max_seconds=1))
    with pytest.raises(CoreError, match="CONTENT_VIDEO_INPUT_INVALID"):
        encode_animation(animation(count=241), "image/gif", policy(), video_policy())
    with pytest.raises(CoreError, match="MODEL_VIDEO_INPUT_INVALID"):
        text_and_estimate(video_policy(), [{"role": "user", "content": [
            {"type": "text", "text": "read"},
            {"type": "video_url", "video_url": {"url": "https://internal.invalid/secret.mp4"}},
        ]}])


def test_fractional_apng_frame_durations_do_not_accumulate_rounding_drift():
    from PIL import Image

    frames = [Image.new("RGB", (4, 4), color) for color in ("red", "green", "blue")]
    data = io.BytesIO()
    frames[0].save(data, format="PNG", save_all=True,
                   append_images=[frames[index % 3] for index in range(1, 200)], duration=1.4)
    original = data.getvalue()
    duration = 0
    with Image.open(io.BytesIO(original)) as animation:
        for index in range(animation.n_frames):
            animation.seek(index)
            animation.load()
            duration += animation.info["duration"]
    encoded = encode_animation(original, "image/apng", policy(), video_policy())
    assert abs(encoded.seconds - duration / 1000) <= 0.040001


async def native_media(core, tmp_path, client, config, plan, *, extra_static=False):
    from companion_onebot_bridge.images import OriginalImage, upload_original
    from companion_onebot_bridge.network import private_http_client
    from tests.support.native_image_pipeline import admit_image

    message = [image_segment("https://gchat.qpic.cn/animation")]
    if extra_static:
        message.append(image_segment("https://gchat.qpic.cn/static"))
    event, _ = await admit_image(core, client, config, plan, tmp_path, message=message)
    downloads = []

    async def resolve(*_):
        return ["1.1.1.1"]

    def fetch(request):
        downloads.append(str(request.url))
        static = request.url.path == "/static"
        return httpx.Response(200, content=png() if static else animation(size=(12, 8)),
            headers={"Content-Type": "image/png" if static else "image/gif"})

    spool, runtime = native_spool(core, tmp_path, config, plan)
    try:
        async with private_http_client(config.core_url, config.bridge_token) as transfer:
            for image in event["payload"]["rich"]["segments"]:
                original = OriginalImage(index=image["index"], slot=image["slot"],
                    source_sha256=image["source_sha256"],
                    url=message[image["index"]]["data"]["url"])
                await upload_original(runtime, transfer, event["event_id"], original,
                    transport=httpx.MockTransport(fetch), resolver=resolve)
    finally:
        spool.close()
    assert len(downloads) == (2 if extra_static else 1)
    return event


@pytest.mark.parametrize("selected,model,extra_static,tool_index,different_rates", [
    (True, "configurable-video-one", False, None, False),
    (True, "configurable-video-two", False, None, False),
    (False, "configurable-video-one", False, None, False),
    (True, "configurable-video-one", True, None, False),
    (True, "configurable-video-one", False, 0, False),
    (True, "configurable-video-one", False, 0, True),
])
def test_sdk_selects_video_tool_then_independent_worker_chats(
    core, tmp_path, selected, model, extra_static, tool_index, different_rates,
):
    calls = []
    prices = ({"input_rate": 8990, "cached_input_rate": 899, "output_rate": 44949,
               "max_output_tokens": 1000} if different_rates else {})
    video = video_policy(model=model, **prices)

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == model:
            assert str(request.url) == "https://video.example.test/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer synthetic-video-provider-key"
            assert "thinking" not in body and "tools" not in body
            parts = body["messages"][0]["content"]
            assert parts[1]["type"] == "video_url"
            if extra_static:
                assert len(parts) == 3 and parts[2]["type"] == "image_url"
            raw = base64.b64decode(parts[1]["video_url"]["url"].split(",", 1)[1])
            with av.open(io.BytesIO(raw)) as container:
                assert len(list(container.decode(video=0))) == 8
            result = answer("红色先变成绿色，再变成蓝色，连续发生。synthetic-video-provider-key")
            result["usage"]["completion_tokens"] = 895 if different_rates else 20
        elif len(calls) == 1 and selected:
            tool = call("motion-1", "understand_video", {
                "question": "描述颜色变化过程", "preview_reply": "抽帧里看得到几种颜色。"})
            if tool_index is not None:
                tool["index"] = tool_index
            result = answer(calls=[tool])
        else:
            result = answer("它先变绿，再变蓝。" if selected else "我只看到了红色首帧。")
        return httpx.Response(200, json=result)

    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True,
                          video_config=video, image_count=2 if extra_static else 1) as (
            client, config, plan,
        ):
            await native_media(core, tmp_path, client, config, plan, extra_static=extra_static)
            async with worker_for(core, respond) as worker:
                assert await worker.run_once("native-video-models")
            assert len(calls) == (3 if selected else 1)
            usages = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            assert [row["scenario"] for row in usages] == (
                ["chat", "content_video", "chat"] if selected else ["chat"])
            assert all(row["state"] == "SETTLED" for row in usages)
            if selected:
                assert usages[1]["provider"] == "synthetic-video" and usages[1]["model"] == model
                assert usages[1]["billed_micro_usd"] == (42 if different_rates else 140)
                assert not core.store.one("SELECT 1 FROM runtime_meta "
                                           "WHERE key='model_budget_blocked'")
                assert budget.total_monthly(core.store, "2000-01-01") >= 140
            final = calls[-1]
            assert final["model"] == core.settings.model_name
            assert ("连续发生" in encode(final)) is selected
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
            assert "synthetic-video-provider-key" not in encode(final)
    run(scenario())


@pytest.mark.parametrize("boundary", [
    "stop", "key", "payload", "unknown", "missing_usage", "invalid_tool",
    "invalid_question", "boolean_question", "unknown_argument", "video_price", "video_disabled",
])
def test_video_boundary_changes_and_unknown_usage_never_send_or_repeat(core, tmp_path, boundary):
    calls = []
    invalid = boundary in {
        "invalid_tool", "invalid_question", "boolean_question", "unknown_argument"}

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "configurable-video-one":
            if boundary == "unknown":
                raise httpx.ReadTimeout("synthetic unknown video result", request=request)
            result = answer("分析结果。")
            if boundary == "missing_usage":
                result.pop("usage")
            elif boundary == "video_price":
                current = core.settings.paid_model.video_input
                core.settings.paid_model = core.settings.paid_model.model_copy(update={
                    "video_input": current.model_copy(update={
                        "input_rate": current.input_rate + 1})})
            elif boundary == "video_disabled":
                core.settings.paid_model = core.settings.paid_model.model_copy(
                    update={"video_input": None})
        else:
            arguments = {"question": "描述动作", "preview_reply": "我看到的是抽样画面。"}
            if boundary == "invalid_question":
                arguments["question"] = 1
            elif boundary == "boolean_question":
                arguments["question"] = False
            elif boundary == "unknown_argument":
                arguments["source_url"] = "https://untrusted.invalid/private"
            result = answer(calls=[call("bad-video", "unknown_tool" if boundary == "invalid_tool"
                                       else "understand_video", arguments)])
        return httpx.Response(200, json=result)

    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            await native_media(core, tmp_path, client, config, plan)

            async def hook(request):
                if request.url.host == "video.example.test":
                    if boundary == "stop":
                        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                            operation_id="stop-native-video", authority=plan.authority))
                    elif boundary == "key":
                        core.settings.video_model_key = SecretStr("different-synthetic-video-key")
                    elif boundary == "payload":
                        request._content = b'{}'

            async with worker_for(core, respond) as worker:
                worker.model.client.event_hooks["request"].append(hook)
                assert await worker.run_once("native-video-fault")
            video_calls = [body for body in calls if body["model"] == "configurable-video-one"]
            expected_video = boundary in {
                "unknown", "missing_usage", "video_price", "video_disabled"}
            assert len(video_calls) == int(expected_video)
            usages = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            assert usages[0]["state"] == "SETTLED"
            if not invalid:
                assert len(usages) == 2
                if boundary in {"stop", "key", "payload"}:
                    assert usages[1]["state"] == "SETTLED"
                    assert usages[1]["billed_micro_usd"] == 0
                    admission = core.store.one((
                        'SELECT * FROM model_admissions WHERE request_id=%s'
                    ), (usages[1]["request_id"],))
                    proof = core.store.one((
                        'SELECT safe_detail_json FROM audit_log WHERE '
                        "action='model.not_dispatched' AND entity_id=%s"
                    ),
                        (usages[1]["request_id"],))
                    detail = json.loads(proof["safe_detail_json"])
                    assert detail["evidence_sha256"] == paid_budget.sha({
                        "kind": "reply_not_dispatched", "request_id": usages[1]["request_id"],
                        "lease_token": admission["lease_token"],
                        "payload_sha256": admission["payload_sha256"], "reason": detail["reason"]})
                else:
                    assert usages[1]["state"] == "UNKNOWN"
                    assert usages[1]["billed_micro_usd"] > 0
                if boundary in {"video_price", "video_disabled"}:
                    detail = core.store.one((
                        'SELECT safe_detail_json FROM audit_log WHERE '
                        "action='model.usage_diagnostic' AND entity_id=%s"
                    ),
                        (usages[1]["request_id"],))
                    assert json.loads(detail["safe_detail_json"])["code"] == (
                        "PRICING_CONFIG_CHANGED")
            else:
                assert all(row["scenario"] == "chat" for row in usages)
                assert not core.store.one("SELECT 1 FROM agent_tool_calls "
                                           "WHERE metadata_json IS NOT NULL")
            if boundary == "payload" or invalid:
                assert_failure_notice(core)
            else:
                assert not core.store.rows("SELECT * FROM outbox")
            before = len(calls)
            core.clock.advance(100)
            async with worker_for(core, respond) as restarted:
                await restarted.run_once("native-video-no-replay")
            assert len(calls) == before
            assert core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence') == usages
    run(scenario())


