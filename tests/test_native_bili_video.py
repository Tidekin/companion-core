"""Native SDK deferral uses the real isolated Reader and original media phases."""
import json

import httpx
import pytest

from companion import owner_authority
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call
from tests.support import bili_video_entrypoint as reader_fixture
from tests.support.bili_video_entrypoint import URL, reader_files, reader_once, upstream
from tests.support.native_bili_video import sdk_step
from tests.support.native_image_pipeline import admit_image
from tests.support.prepare_owner_input import window


@pytest.mark.parametrize("boundary", ["complete", "stop", "binding", "unknown"])
def test_native_bili_sdk_selects_original_reader_media_and_resumes(
    core, tmp_path_factory, monkeypatch, boundary,
):
    # Keep the nested Windows child TEMP below the native MAX_PATH boundary.
    tmp_path = tmp_path_factory.mktemp("bili")
    if boundary == "unknown":
        # The isolated fixture clock is normally frozen. Let the original receipt
        # deadline elapse after one real read-only query of its UNKNOWN phase.
        clock_hook = '''
    async def elapsed(response):
        if response.request.url.path.endswith('/media/receipt'):
            from datetime import timedelta
            value['now'] = (datetime.fromisoformat(value['now'])
                            + timedelta(seconds=181)).isoformat()
    args[0].event_hooks['response'].append(elapsed)
'''
        monkeypatch.setattr(reader_fixture, "READER", reader_fixture.READER.replace(
            "async def tick(*args, **kwargs):", "async def tick(*args, **kwargs):" + clock_hook))
    isolated = reader_files(tmp_path)
    metadata, media = upstream(tmp_path)
    provider = Provider(answer(json.dumps({"analysis": "预览帧有颜色变化", "need_clips": True})),
                        answer(json.dumps({"analysis": "选定片段的颜色逐渐变化"})))

    dispatched = []

    def respond(request):
        dispatched.append(request)
        if boundary == "unknown":
            raise httpx.ReadTimeout("synthetic lost preview response", request=request)
        return provider(request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model_http:
            model = ModelClient(core.settings, model_http)
            async with window(core, tmp_path, media=True, content=True, video=True, bili=True,
                              model=model) as (client, config, plan):
                event, _ = await admit_image(core, client, config, plan, tmp_path, message=[
                    {"type": "text", "data": {"text": "分析这段视频 " + URL}}])
                job = core.store.one('SELECT * FROM jobs WHERE event_id=%s', (event["event_id"],))
                assert not core.store.one("SELECT 1 FROM exploration_requests")
                first = await sdk_step(core, tmp_path, job["job_id"], [answer(calls=[
                    call("read-1", "read_content", {"url": URL})])], "RETRY_WAIT", "read-select")
                assert len(first) == 1
                assert not core.store.one("SELECT 1 FROM bili_video_items")
                read = await reader_once(isolated, config.core_url, core.clock, metadata)
                assert read["remaining"] == 0
                assert not core.store.one("SELECT 1 FROM bili_video_items")
                second = await sdk_step(core, tmp_path, job["job_id"], [answer(calls=[
                    call("bili-1", "analyze_shared_video", {"url": URL})])],
                    "RETRY_WAIT", "video-select")
                assert len(second) == 1
                item = core.store.one("SELECT * FROM bili_video_items")
                assert item and item["window_sha256"] == ""
                original_request = item["request_id"]
                tool = core.store.one('SELECT * FROM agent_tool_calls WHERE tool_name=%s',
                                       ("analyze_shared_video",))
                assert tool["request_id"] == original_request and tool["state"] == "DEFERRED"

                async def before_model(request):
                    if boundary == "stop":
                        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                            operation_id="stop-bili-preview", authority=plan.authority))
                    elif boundary == "binding":
                        core.store.execute("UPDATE agent_tool_calls SET request_id=NULL "
                                           "WHERE tool_name='analyze_shared_video'")

                model_http.event_hooks["request"].append(before_model)
                analyzed = await reader_once(isolated, config.core_url, core.clock, media,
                                             media=True)
                assert analyzed["remaining"] == 0, analyzed
                item = core.store.one("SELECT * FROM bili_video_items")
                if boundary != "complete":
                    assert item["state"] in {"FAILED", "CANCELLED", "UNKNOWN"}, item
                    assert len(dispatched) == (1 if boundary == "unknown" else 0)
                    usage = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
                    assert len(usage) == 3
                    if boundary == "unknown":
                        assert usage[-1]["state"] == "UNKNOWN"
                    assert not core.store.one("SELECT 1 FROM outbox")
                    assert (await reader_once(isolated, config.core_url, core.clock, media,
                                               media=True))["requests"] == []
                    assert core.store.rows(
                        'SELECT * FROM model_usage ORDER BY record_sequence'
                    ) == usage
                    assert len(dispatched) == (1 if boundary == "unknown" else 0)
                    return
                assert item["state"] == "COMPLETE", {
                    "error": item["last_error_code"],
                    "phases": core.store.rows("SELECT phase,state,last_error_code "
                                               "FROM bili_video_phases"),
                    "usage": core.store.rows("SELECT scenario,state FROM model_usage"),
                    "provider_calls": len(provider.requests),
                }
                third = await sdk_step(core, tmp_path, job["job_id"],
                    [answer("字幕与有限片段显示了颜色变化。")], "SUCCEEDED", "video-answer")
                assert len(third) == 1
                assert "未完整观看" in encode(third[0]["messages"])
                assert len(provider.requests) == 2
                assert "image_url" in encode(provider.requests[0]["messages"])
                assert "video_url" in encode(provider.requests[1]["messages"])
                assert len(core.store.rows("SELECT * FROM exploration_requests")) == 2
                assert core.store.one("SELECT request_id FROM bili_video_items")["request_id"] == (
                    original_request)
                assert core.store.rows(
                    'SELECT scenario,state FROM model_usage ORDER BY '
                    'record_sequence'
                ) == [
                    {"scenario": "chat", "state": "SETTLED"},
                    {"scenario": "chat", "state": "SETTLED"},
                    {"scenario": "content_video_preview", "state": "SETTLED"},
                    {"scenario": "content_video", "state": "SETTLED"},
                    {"scenario": "chat", "state": "SETTLED"}]
                assert len(core.store.rows("SELECT * FROM outbox")) == 1
                assert (await reader_once(isolated, config.core_url, core.clock, media,
                                           media=True))["requests"] == []
    run(scenario())
