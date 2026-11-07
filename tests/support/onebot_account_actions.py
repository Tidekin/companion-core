import hashlib
import io
from datetime import UTC, datetime

import httpx
from PIL import Image

from companion.social_autonomy import SocialAutonomy
from companion.social_autonomy_protocol import ProfileBaseline, SocialAutonomyConfig
from companion.store import encode
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import ingest
from tests.social_profile_support import confirmed_profile_material
from tests.support.private_companion import companion_options
from tests.support.social_autonomy import avatar, read_public


def fixture(core, kind="qzone_publish"):
    ref = read_public(core, "bilibili")
    owner = ingest(core, 99, "合成绑定输入", origin="onebot-direct:FriendMessage:10001")
    conversation = core.store.one('SELECT conversation_id FROM events WHERE event_id=%s',
                                  (owner["event_id"],))["conversation_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    if kind in {"nickname", "signature"}:
        confirmed_profile_material(core)
    policy = SocialAutonomyConfig(enabled=True, scope="qq_social_v1",
        authorization_sha256="a" * 64, qzone_enabled=True, nickname_enabled=True,
        signature_enabled=True, avatar_enabled=True, asset_policy_sha256="e" * 64,
        max_output_tokens=128)
    options = companion_options(core)
    options.update(account_actions_scope="qq_social_v1",
                   account_actions_authorization_sha256=policy.authorization_sha256)
    options["core_options"].update(social_autonomy=policy, content_enabled=False,
                                   observation_adapters={})
    proposal = ({"kind": kind, "text": "公开阅读中的恒星知识。", "source_refs": [ref]}
                if kind == "qzone_publish" else {"kind": kind, "asset_id": "avatar_reviewed_one"}
                if kind == "avatar" else {"kind": kind, "value": "观星" if kind == "nickname"
                                         else "保持好奇"})
    return ref, conversation, options, proposal


def native_fixture(core, kind="qzone_publish"):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    ref, conversation, options, proposal = fixture(core, kind)
    options["core_options"]["social_autonomy"] = options["core_options"][
        "social_autonomy"].model_copy(update={"max_output_tokens": 512})
    return ref, conversation, options, proposal


async def pump_account(active):
    guard = OwnerSendGuard(active.runtime, active.client)
    return await deliver_tick(active.client, OwnerOneBot(guard, active.protocol),
                              active.spool, guard=guard, delivery_lane="qq_account")


def model_response(proposal):
    return httpx.Response(200, json={"choices": [{"message": {"content": encode(proposal)}}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def catalog(core, directory):
    directory.mkdir()
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "navy").save(image, format="PNG")
    data = image.getvalue()
    review = avatar(core, asset_sha256=hashlib.sha256(data).hexdigest(), width=16, height=16,
                    size_bytes=len(data))
    (directory / (review.asset_sha256 + ".bin")).write_bytes(data)
    manifest = directory / (hashlib.sha256(review.asset_id.encode()).hexdigest() + ".json")
    manifest.write_text(review.model_dump_json(), encoding="utf-8")
    return review, data, manifest


async def plan_action(core, ref, conversation, proposal, *, review=None):
    module = SocialAutonomy(core.store, core.settings, core.model, core.clock)
    kind = proposal["kind"]
    if kind != "qzone_publish":
        module.record_profile(ProfileBaseline(bot_qq_id=core.settings.bot_qq_id, field=kind,
            value="0" * 64 if kind == "avatar" else "原资料", observed_at=core.clock(),
            receipt_sha256="f" * 64))
    if review:
        module.register_asset(review)
    memory_ids = ([core.store.one(
        "SELECT memory_id FROM memories WHERE fact_key='profile-preference-800'"
    )["memory_id"]] if kind in {"nickname", "signature"} else [])
    planned = await module.plan("bridge-account-plan", profile_memory_ids=memory_ids,
        source_ids=[ref] if kind == "qzone_publish" else [],
        profile_fields=[] if kind == "qzone_publish" else [kind],
        asset_ids=[review.asset_id] if review else [])
    with core.store.transaction():
        return module.reserve(planned["plan_id"], conversation)
