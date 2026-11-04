"""Native preparation checks real Core authority and leaves persistence to its caller."""

import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.core import Core
from companion.file_evidence import PrivateFilesConfig
from companion.image_input import ImageInputPolicy
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion.store import encode
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.test_owner_authority import prepare


@asynccontextmanager
async def window(core, tmp_path, *, media=False, content=False, history=False, video=False,
                 bili=False, voice=False, model=None, video_config=None, image_count=1, meme=False,
                 expression=False, video_candidates=None):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    policy = prepare(core, tmp_path)
    core.settings.rich_input_scope = "references_v1"
    core.settings.content_enabled = content
    if meme:
        core.settings.meme_semantics_enabled = True
        core.settings.expression_enabled = True
    if media:
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            "image_input": ImageInputPolicy(approval_sha256="b" * 64,
                estimated_tokens_per_image=1000, max_bytes_per_image=100000,
                max_pixels_per_image=10000, max_images_per_request=image_count)})
        core.settings.private_files = PrivateFilesConfig(
            enabled=True, parser_image="sha256:" + "f" * 64)
    if video:
        from pydantic import SecretStr

        from tests.test_video_understanding import video_policy

        core.settings.video_model_key = SecretStr("synthetic-video-provider-key")
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            "video_routes": {name: candidate.approval(core.settings.paid_model.window_id)
                             for name, candidate in (video_candidates or {}).items()},
            "video_input": video_config or video_policy()})
        core.settings.content = core.settings.content.model_copy(update={
            "image_daily_micro_usd": 10000})
    if bili:
        from companion.bili_video_config import BiliVideoConfig
        from companion.config import ObservationAdapterConfig

        core.settings.bili_video = BiliVideoConfig(enabled=True)
        core.settings.observation_enabled = core.settings.exploration_enabled = True
        core.settings.observation_adapters = {"bili": ObservationAdapterConfig(
            source="bilibili", media_analysis=True, reader_max_requests=8,
            token="synthetic-bili-reader-token-123456")}
        core.settings.exploration_daily_bytes = 200_000_000
        core.settings.exploration_daily_requests = 100
        core.settings.exploration_monthly_requests = 1000
        core.settings.exploration_daily_deep_reads = 20
        core.settings.exploration_max_response_bytes = 1_000_000
        core.settings.content = core.settings.content.model_copy(update={
            "image_daily_micro_usd": 1000000})
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            "per_request_calls": 6,
            "image_input": core.settings.paid_model.image_input.model_copy(update={
                "max_images_per_request": 4, "max_pixels_per_image": 512 * 512,
                "max_bytes_per_image": 4000000})})
    key = Ed25519PrivateKey.generate()
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch="native-capture-test",
        onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001",
        meme_enabled=meme, meme_semantics_enabled=meme)
    hashes = owner_authority.capability_hashes(core.settings)
    purposes = (["chat", "image", "file"] if media else ["chat"])
    purposes += ["content"] if content else []
    purposes += ["history"] if history else []
    purposes += ["bili_video"] if bili else []
    purposes += ["voice"] if voice else []
    purposes += ["meme", "expression"] if meme else []
    purposes += ["expression"] if expression and not meme else []
    capability_names = (["image", "file"] if media else []) + (["bili_video"] if bili else [])
    capability_names += ["voice"] if voice else []
    capability_names += ["meme"] if meme else []
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        "purposes": purposes,
        "history": {"scope": "owner_private", "not_before": core.clock(),
                    "max_turns": 16, "max_utf8_bytes": 64000} if history else {},
        "capability_policies": {name: hashes[name] for name in capability_names},
        "capture": {"public_key": key.public_key().public_bytes_raw().hex(),
            "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
            "protocol_epoch": config.protocol_epoch}})
    approved, _ = activate(core, policy)
    plan = SimpleNamespace(authority=approved["authority"], key=key)
    with Core(core.settings, model=model, clock=core.clock) as http_core:
        async with real_core_http(http_core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            yield client, replace(config, core_url=f"http://127.0.0.1:{port}"), plan


def native_spool(core, tmp_path, config, plan):
    from companion_onebot_bridge.owner_runtime import OwnerRuntime

    config = replace(config, owner_authority=plan.authority,
        capture_signing_key=plan.key.private_bytes_raw().hex(),
        rich_input_scope="references_v1", data_dir=str(tmp_path / "native-bridge"))
    spool = Spool(config)
    return spool, OwnerRuntime(spool, clock=lambda: core.clock.now)


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
