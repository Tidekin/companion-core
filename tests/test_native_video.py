"""The native SDK chooses motion analysis over original animation bytes."""
import asyncio
import base64
import io
import json
import pickle
import sys
from pathlib import Path

import av
import httpx
import pytest

from companion import owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.reply_segments import SegmentedReplyConfig
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.native_video import animation, responses
from tests.support.prepare_owner_input import window


@pytest.mark.parametrize("mime", ["image/gif", "image/apng"])
def test_native_sdk_motion_tool_uses_real_video_and_original_ledger(core, tmp_path, mime):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            event = await animation(core, client, config, plan, tmp_path, mime)
            job = core.store.one('SELECT * FROM jobs WHERE event_id=%s', (event["event_id"],))
            config_path, output = tmp_path / "video-worker.pickle", tmp_path / "video-wire.json"
            config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                responses(), "SUCCEEDED", str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name("reader_sdk_worker.py")), str(config_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                logs, _ = await asyncio.wait_for(process.communicate(), 45)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, logs.decode(errors="replace")
            requests = json.loads(output.read_text(encoding="utf-8"))
            assert len(requests) == 3
            assert "understand_video" in encode(requests[0]["tools"])
            assert "抽样预览" in encode(requests[0]["messages"])
            assert requests[1]["model"] == core.settings.paid_model.video_input.model
            parts = requests[1]["messages"][0]["content"]
            videos = [part for part in parts if part["type"] == "video_url"]
            assert len(videos) == 1
            raw = base64.b64decode(videos[0]["video_url"]["url"].split(",", 1)[1])
            with av.open(io.BytesIO(raw), format="mp4") as stream:
                frames = list(stream.decode(video=0))
                assert len(frames) >= 2 and len(stream.streams) == 1
                colors = [frame.to_image().getpixel((2, 2)) for frame in (frames[0], frames[-1])]
                assert colors[0] != colors[1]
            assert "one_animation_cycle" in encode(requests[2]["messages"])
            assert core.store.rows(
                'SELECT scenario,state FROM model_usage ORDER BY '
                'record_sequence'
            ) == [
                {"scenario": "chat", "state": "SETTLED"},
                {"scenario": "content_video", "state": "SETTLED"},
                {"scenario": "chat", "state": "SETTLED"}]
            saved = core.store.one("SELECT * FROM agent_tool_calls")
            assert saved["state"] == "RETURNED"
            metadata = json.loads(saved["metadata_json"])
            assert metadata["authority"] == plan.authority
            assert metadata["request_id"] == core.store.one(
                "SELECT request_id FROM model_usage WHERE scenario='content_video'")["request_id"]
            assert "data:video" not in encode(saved)
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
    run(scenario())


@pytest.mark.parametrize("body", [None, {"usage": {"prompt_tokens": 20, "completion_tokens": 1}}])
def test_video_503_delivers_prepared_preview_without_repeating_io(core, tmp_path, body):
    async def scenario():
        from companion import owner_delivery

        core.settings.segmented_reply = SegmentedReplyConfig(min_chars=4)
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            event = await animation(core, client, config, plan, tmp_path, "image/gif")
            job = core.store.one('SELECT * FROM jobs WHERE event_id=%s', (event["event_id"],))
            path, output = tmp_path / "worker.pickle", tmp_path / "requests.json"
            path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                [responses()[0], httpx.Response(503, json=body)], "SUCCEEDED", str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name("reader_sdk_worker.py")), str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            logs, _ = await asyncio.wait_for(process.communicate(), 60)
            assert process.returncode == 0, logs.decode(errors="replace")
            assert len(json.loads(output.read_text(encoding="utf-8"))) == 2
            usage = core.store.rows(
                'SELECT scenario,state,billed_micro_usd FROM model_usage ORDER BY record_sequence')
            assert [(r["scenario"], r["state"]) for r in usage] == [
                ("chat", "SETTLED"), ("content_video", "SETTLED" if body else "UNKNOWN")]
            assert usage[1]["billed_micro_usd"] > 0
            replies = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
            assert [json.loads(r["content_json"])["text"] for r in replies] == [
                "抽帧里这一团橙色好抢眼。", "像个小橘子。"]
            delivery = core.store, core.settings, core.clock, replies[0]["message_id"]
            owner_delivery.check_delivery(*delivery)
            with pytest.raises(CoreError, match="REPLY_SEGMENT_NOT_READY"):
                owner_delivery.check_delivery(*delivery[:3], replies[1]["message_id"])
            core.store.execute("UPDATE agent_tool_calls SET metadata_json='{}'")
            with pytest.raises(CoreError, match="OWNER_DELIVERY"):
                owner_delivery.check_delivery(*delivery)
    run(scenario())


@pytest.mark.parametrize("failure", ["stop", "expiry", "unknown", "binding"])
def test_native_video_final_boundary_or_unknown_never_replays(core, tmp_path, failure):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            await animation(core, client, config, plan, tmp_path, "image/gif")
            provider = Provider(*responses())
            video_calls = []

            async def hook(request):
                if request.url.host == "video.example.test":
                    if failure == "stop":
                        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                            operation_id="stop-video-dispatch", authority=plan.authority))
                    elif failure == "expiry":
                        core.clock.advance(301)
                    elif failure == "binding":
                        core.store.execute("UPDATE agent_tool_calls SET metadata_json='{}'")

            def respond(request):
                if request.url.host == "video.example.test":
                    video_calls.append(request)
                    if failure == "unknown":
                        raise httpx.ReadTimeout("synthetic lost video response", request=request)
                return provider(request)

            async with worker_for(core, respond) as worker:
                worker.model.client.event_hooks["request"].append(hook)
                assert await worker.run_once("video-boundary")
            assert len(video_calls) == (1 if failure == "unknown" else 0)
            original = core.store.rows("SELECT * FROM model_usage")
            assert len(original) == 2
            assert original[0]["state"] == "SETTLED"
            core.clock.advance(100)
            async with worker_for(core, respond) as restarted:
                await restarted.run_once("video-no-replay")
            assert core.store.rows("SELECT * FROM model_usage") == original
            assert len(video_calls) == (1 if failure == "unknown" else 0)
            if failure == "unknown":
                assert original[1]["state"] == "UNKNOWN"
    run(scenario())


@pytest.mark.parametrize("select", [False, True])
def test_native_video_selection_uses_only_real_animations(core, tmp_path, select):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            await animation(core, client, config, plan, tmp_path,
                            "image/png" if select else "image/gif")
            replies = ([responses()[0], answer("这是一张静态图。")] if select else
                       [answer("收到动图，暂不分析动作。")])
            provider = Provider(*replies)
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("selected-motion-only")
            assert len(provider.requests) == (2 if select else 1)
            assert all(row["scenario"] == "chat" and row["state"] == "SETTLED"
                       for row in core.store.rows("SELECT scenario,state FROM model_usage"))
            if select:
                tool = core.store.one("SELECT * FROM agent_tool_calls")
                assert tool["state"] == "RETURNED" and tool["metadata_json"] is None
                assert "CONTENT_VIDEO_ANIMATION_REQUIRED" in tool["result_json"]
            else:
                assert not core.store.one("SELECT 1 FROM agent_tool_calls")
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
    run(scenario())
