"""Native preparation checks real Core authority and leaves persistence to its caller."""

import hashlib

import httpx
import pytest

from companion import owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from tests.conftest import run
from tests.support.onebot_events import raw_event
from tests.support.prepare_owner_input import native_spool, window


def media_frame(core):
    return encode(raw_event(time=int(core.clock.now.timestamp()), message=[
        {"type": "text", "data": {"text": "收到的附件"}},
        {"type": "image", "data": {"url": "https://gchat.qpic.cn/image-original"}},
        {"type": "file", "data": {"file_id": "original-file", "file": "notes.txt",
                                  "file_size": 12}},
    ]))


def test_native_prepare_returns_source_bound_media_without_effects(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, media=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                before = runtime.path.read_bytes()
                frame = media_frame(core)
                result = await prepare_owner_input(runtime, client, None, frame,
                    core.clock.now, ("127.0.0.1", 3001))
                assert result["frame_sha256"] == hashlib.sha256(frame.encode()).hexdigest()
                assert result["event"]["payload"]["shared_content"] == [
                    {"kind": "image", "slot": 0}, {"kind": "file", "slot": 0}]
                image, file = result["media"]["images"][0], result["media"]["files"][0]
                assert image["url"] == "https://gchat.qpic.cn/image-original"
                assert file["file_id"] == "original-file"
                projections = result["event"]["payload"]["rich"]["segments"]
                assert {row["source_sha256"] for row in projections} == {
                    image["source_sha256"], file["source_sha256"]}
                assert "image-original" not in encode(result["event"])
                assert runtime.path.read_bytes() == before
                assert spool.pending_count == 0
                assert not core.store.one("SELECT 1 FROM events")
            finally:
                spool.close()
    run(scenario())


def test_native_prepare_denies_ungranted_media_before_persistence(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                with pytest.raises(httpx.HTTPStatusError) as error:
                    await prepare_owner_input(runtime, client, None, media_frame(core),
                        core.clock.now, ("127.0.0.1", 3001))
                assert error.value.response.status_code == 403
                rejected = await client.post("/internal/v1/owner-authority/check", json={
                    "authority": runtime.authority, "purpose": "image"})
                assert rejected.json()["error"] == "OWNER_AUTHORITY_PURPOSE_REQUIRED"
                assert spool.pending_count == 0
                assert not core.store.one("SELECT 1 FROM events")
            finally:
                spool.close()
    run(scenario())


@pytest.mark.parametrize("boundary", ["during_read", "before_dispatch", "body_mutation"])
def test_native_prepare_rechecks_reference_boundary(core, tmp_path, boundary):
    async def scenario():
        async with window(core, tmp_path) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            calls = []

            def remote(request):
                calls.append(request)
                if boundary == "during_read":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-during-reference-read", authority=plan.authority))
                return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
                    "message_id": 7, "message": [{"type": "text", "data": {"text": "资料"}}]}})

            async def request_hook(request):
                if boundary == "before_dispatch":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-before-reference-read", authority=plan.authority))
                elif boundary == "body_mutation":
                    request._content = encode({"message_id": 999}).encode()
                    request.headers["Content-Length"] = str(len(request._content))

            try:
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(remote)) as protocol:
                    protocol.event_hooks["request"].append(request_hook)
                    frame = encode(raw_event(time=int(core.clock.now.timestamp()), message=[
                        {"type": "reply", "data": {"id": "7"}}]))
                    expected = ValueError if boundary == "body_mutation" else httpx.HTTPStatusError
                    with pytest.raises(expected):
                        await prepare_owner_input(runtime, client, OneBotClient(config, protocol),
                            frame, core.clock.now, ("127.0.0.1", 3001))
                assert len(calls) == (1 if boundary == "during_read" else 0)
                assert spool.pending_count == 0
                assert not core.store.one("SELECT 1 FROM events")
            finally:
                spool.close()
    run(scenario())
