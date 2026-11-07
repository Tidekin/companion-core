"""Synthetic signed custody and real Core HTTP for native Meme regressions."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from weakref import WeakKeyDictionary

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.image_input import ImageInputPolicy
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion_onebot_bridge import owner_notices
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.core_http import real_core_http
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live

CASES = WeakKeyDictionary()


def case(runtime):
    return CASES[runtime.window]


class NativeCase:
    def __init__(self, core, directory, *, rules, enabled=True, discovery=True,
                 provider="none", key=None, semantic=False, catalog_owner="bridge"):
        self.core = core
        core.clock.now = datetime.now(UTC).replace(microsecond=0)
        approved_live(core, directory, **({"max_output_tokens": 4096,
            "meme_input": ImageInputPolicy(approval_sha256="e" * 64,
                estimated_tokens_per_image=100, max_bytes_per_image=4000000,
                max_pixels_per_image=16000000, max_images_per_request=5)} if semantic else {}))
        core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
        core.settings.expression_enabled = True
        core.settings.expression_selection_probability = 1.0
        core.settings.meme_discovery_enabled = True
        core.settings.meme_semantics_enabled = semantic
        core.settings.meme_caption_daily_micro_usd = 50000
        self.key = Ed25519PrivateKey.generate()
        policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
            purposes=["chat", "expression", "meme"], authorization_sha256="d" * 64,
            paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
            capability_policies={"meme": owner_authority.capability_hashes(core.settings)["meme"]},
            capture={"public_key": self.key.public_key().public_bytes_raw().hex(),
                "identity_sha256": paid_budget.sha(
                    [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                "protocol_epoch": "native-meme-fixture"})
        authority = activate(core, policy)[0]["authority"]
        self.policy = policy
        config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
            bot_qq_id=core.settings.bot_qq_id,
            bridge_token=core.settings.bridge_token.get_secret_value(),
            onebot_token="s" * 32, protocol_epoch=policy.capture.protocol_epoch,
            owner_authority=authority, capture_signing_key=self.key.private_bytes_raw().hex(),
            data_dir=str(directory / "bridge"), meme_enabled=enabled,
            meme_catalog_owner=catalog_owner,
            meme_discovery_enabled=discovery, meme_semantics_enabled=semantic,
            meme_search_provider=provider, meme_search_api_key=key, meme_trusted_sources=rules)
        self.spool = Spool(config)
        self.owner = OwnerRuntime(self.spool, clock=lambda: core.clock.now)
        CASES[self.owner] = self

    async def handoff(self, runtime, client):
        from companion.owner_authority_protocol import AuthorityStop
        from companion_onebot_bridge import owner_handoff

        runtime.window.stop()
        owner_authority.stop(self.core.store, self.core.settings, self.core.clock,
            AuthorityStop(operation_id="fixture-profile-stop", authority=runtime.authority))
        config = runtime.config
        runtime.spool.close()
        with Spool(config, reconciliation_only=True) as stopped_spool:
            # Actual exclusive-lock reacquisition establishes quiescence; never
            # fabricate the flag or a journal record on the active instance.
            self.core.clock.now = datetime.now(UTC)
            stopped = OwnerRuntime(stopped_spool, clock=lambda: self.core.clock.now)
            await owner_handoff.prepare(stopped, client)
            successor = activate(self.core, self.policy,
                revision=runtime.authority["revision"] + 1,
                operation="fixture-profile-successor")[0]["authority"]
            await owner_handoff.handoff(stopped, client, successor)
        return replace(config, owner_authority=successor)

    @asynccontextmanager
    async def client(self, runtime):
        async with real_core_http(self.core) as port:
            config = replace(runtime.config, core_url=f"http://127.0.0.1:{port}")
            runtime.config = runtime.window.config = runtime.spool.config = config
            async with private_http_client(config.core_url, config.bridge_token) as client:
                yield client


def capture(runtime, number=42, *, text="太棒了，成功了"):
    owner = runtime.window
    raw = json.dumps(raw_event(number, time=int(owner.clock().timestamp()), text=text))
    event = normalize(raw, runtime.config, now=owner.clock().timestamp())
    assert owner.persist_capture(event, raw, received_at=owner.clock(),
                                 peer=("127.0.0.1", 3001))
    return event


def recall(runtime, number=42):
    owner = runtime.window
    raw = json.dumps({"post_type": "notice", "notice_type": "friend_recall",
        "self_id": int(runtime.config.bot_qq_id), "user_id": int(runtime.config.owner_qq_id),
        "message_id": number, "time": int(owner.clock().timestamp())})
    assert owner_notices.put(owner, raw, owner.clock(), ("127.0.0.1", 3001))


def native_item(runtime, event, candidate, *, key=None):
    from tests.test_meme_runtime import item

    value = item(event, candidate, key=key)
    value["origin_event_id"] = value.pop("window_origin_event_id")
    return {**value, "authority": runtime.authority}
