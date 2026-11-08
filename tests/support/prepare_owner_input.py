from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.core import Core
from companion.file_evidence import PrivateFilesConfig
from companion.image_input import ImageInputPolicy
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.spool import Spool
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare


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

        from tests.support.video_understanding import video_policy

        core.settings.video_model_key = SecretStr("synthetic-video-provider-key")
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            "video_routes": {name: candidate.approval(core.settings.paid_model.window_id)
                             for name, candidate in (video_candidates or {}).items()},
            "video_input": video_config or video_policy()})
        core.settings.content = core.settings.content.model_copy(update={
            })
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
            })
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
