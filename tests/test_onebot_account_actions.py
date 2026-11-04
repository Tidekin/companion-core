"""Frozen account plans use native owner custody, Spool and original Core Outbox."""

import hashlib
import io
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image

from companion import cumulative, paid_budget
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.social_autonomy import SocialAutonomy
from companion.social_autonomy_protocol import ProfileBaseline, SocialAutonomyConfig
from companion.store import encode
from companion_onebot_bridge import account_actions, account_assets
from companion_onebot_bridge.native_actions import content_hash
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick, validate_item
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import ingest, run
from tests.native_continuous_support import Host
from tests.social_profile_support import confirmed_profile_material
from tests.support.onebot import provider
from tests.support.onebot_events import config
from tests.test_cumulative_budget import policy as quota_policy
from tests.test_cumulative_budget import view
from tests.test_native_account_host import configure_host
from tests.test_onebot_capture import real_core_http
from tests.test_private_companion import companion_options
from tests.test_social_autonomy import avatar, read_public
from tests.test_social_owner_authority import authorize


def fixture(core, kind="qzone_publish"):
    ref = read_public(core, "bilibili")
    owner = ingest(core, 99, "合成绑定输入", origin="onebot-direct:FriendMessage:10001")
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
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



@asynccontextmanager
async def account_host(core, tmp_path, *, social_policy, bridge_options=None,
                       protocol_reply=None, quota=None):
    core.settings.social_autonomy = social_policy
    core.settings.content_enabled = False
    core.settings.observation_adapters = {}
    policy, _ = authorize(core, tmp_path)
    if quota is not None:
        quota_policy(core, account=quota)
        with core.store.transaction():
            cumulative.activate(core.store, core.settings, core.clock())
        policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings))})
    cfg = replace(configure_host(core, tmp_path, policy), **(bridge_options or {}))
    sends = []

    async def native(request):
        result = protocol_reply(request) if protocol_reply else None
        if hasattr(result, "__await__"):
            result = await result
        return result if result is not None else provider(request, sends)

    async with real_core_http(core) as port:
        cfg = replace(cfg, core_url=f"http://127.0.0.1:{port}")
        async with private_http_client(cfg.core_url, cfg.bridge_token) as client, (
            private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                                transport=httpx.MockTransport(native))
        ) as protocol:
            active = Host(core, cfg, client, protocol)
            try:
                yield active, client, protocol, sends
            finally:
                active.spool.close()


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


@pytest.mark.parametrize("kind,path,data", [
    ("qzone_publish", "/send_qzone_msg", {"tid": "real-synthetic-tid", "time": 1792382400}),
    ("nickname", "/set_qq_profile", None), ("signature", "/set_self_longnick", {}),
    ("avatar", "/set_qq_avatar", None),
])
def test_frozen_account_actions_keep_native_receipts_without_owner_input_or_message_id(
    core, tmp_path, kind, path, data,
):
    ref, conversation, options, proposal = native_fixture(core, kind)
    sends, checks = [], []

    async def scenario():
        directory = tmp_path / "reviewed"
        bridge_options = {"account_asset_directory": str(directory),
                          "account_asset_policy_sha256": "e" * 64} if kind == "avatar" else {}

        def native(request):
            if request.url.path == path:
                sends.append(request)
                return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

        async with account_host(core, tmp_path,
            social_policy=options["core_options"]["social_autonomy"],
            bridge_options=bridge_options, protocol_reply=native) as (
            window, client, protocol, private_sends,
        ), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: model_response(proposal))) as model:
            core.model = ModelClient(core.settings, model)
            review, picture, _ = (catalog(core, directory) if kind == "avatar"
                                  else (None, None, None))
            frozen = await plan_action(core, ref, conversation, proposal, review=review)
            usage = core.store.rows("SELECT * FROM model_usage")

            async def checked(request):
                if request.url.path.endswith("/account-actions/send-check"):
                    checks.append(json.loads(request.content))

            client.event_hooks["request"].append(checked)
            await pump_account(window)
            assert len(sends) == len(checks) == 1 and private_sends == []
            row = core.store.one("SELECT * FROM outbox")
            assert row["state"] == "SENT" and row["provider_message_id"] is None
            assert row["native_ack_json"] is None and row["attempt_count"] == 1
            receipt = json.loads(core.store.one("SELECT receipt_json FROM "
                                                 "social_autonomy_reservations")["receipt_json"])
            assert receipt["native_receipt_sha256"] == content_hash(
                {"status": "ok", "retcode": 0, "data": data})
            assert receipt["resource_id"] == (data["tid"] if kind == "qzone_publish" else None)
            assert window.captures() == 0
            attempt = window.db.execute("SELECT event_id,state FROM window_attempts").fetchone()
            assert attempt == (frozen.plan_id, "ACK")
            body = json.loads(sends[0].content)
            if kind == "qzone_publish":
                assert body == {"content": proposal["text"], "ugc_right": 16,
                                "target_uins": [10001]}
            elif kind == "nickname":
                assert body == {"nickname": proposal["value"]}
            elif kind == "signature":
                assert body == {"longNick": proposal["value"]}
            else:
                import base64

                assert body == {"file": "base64://" + base64.b64encode(picture).decode()}
            assert core.store.rows("SELECT * FROM model_usage") == usage
            window.runtime.verify()

    run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "negative", "missing_tid"])
def test_account_unknown_retains_responsibility_and_never_repeats_native_io(
    core, tmp_path, failure,
):
    ref, conversation, options, proposal = native_fixture(core)
    calls = []

    def native(request):
        if request.url.path == "/send_qzone_msg":
            calls.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("native synthetic timeout")
            return httpx.Response(200, json={"status": "failed" if failure == "negative" else "ok",
                                            "retcode": 100 if failure == "negative" else 0,
                                            "data": None if failure == "negative" else {}})

    async def scenario():
        async with account_host(core, tmp_path,
            social_policy=options["core_options"]["social_autonomy"],
            protocol_reply=native) as (window, client, protocol, _), \
            httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: model_response(proposal))) as model:
            core.model = ModelClient(core.settings, model)
            await plan_action(core, ref, conversation, proposal)
            await pump_account(window)
            assert len(calls) == 1 and window.runtime.row()["state"] == "STOPPED"
            assert window.attempts() == {"UNKNOWN": 1}
            row = core.store.one("SELECT * FROM outbox")
            assert row["state"] == "DELIVERY_UNCERTAIN" and row["attempt_count"] == 1
            receipt = json.loads(core.store.one("SELECT receipt_json FROM "
                                                 "social_autonomy_reservations")["receipt_json"])
            assert receipt["result"] == "UNKNOWN" and receipt["resource_id"] is None
            assert (receipt["native_receipt_sha256"] is None) == (failure == "timeout")
            window.restart()
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                await pump_account(window)
            assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize("phase", ["before_core_mark", "after_core_mark"])
def test_account_stop_boundary_distinguishes_proven_not_sent_from_marked_unknown(
    core, tmp_path, phase,
):
    ref, conversation, options, proposal = native_fixture(core)
    native_calls, check_calls = [], []

    def native(request):
        if request.url.path == "/send_qzone_msg":
            native_calls.append(request)
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": {"tid": "unexpected"}})

    async def scenario():
        async with account_host(core, tmp_path,
            social_policy=options["core_options"]["social_autonomy"],
            protocol_reply=native) as (window, client, protocol, _), \
            httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: model_response(proposal))) as model:
            core.model = ModelClient(core.settings, model)
            await plan_action(core, ref, conversation, proposal)

            async def before(request):
                if request.url.path == "/send_qzone_msg" and phase == "before_core_mark":
                    window.runtime.stop()

            async def after(response):
                if response.request.url.path.endswith("/account-actions/send-check"):
                    check_calls.append(response.request)
                    if phase == "after_core_mark":
                        window.runtime.stop()

            protocol.event_hooks["request"].append(before)
            client.event_hooks["response"].append(after)
            await pump_account(window)
            assert native_calls == [] and len(check_calls) == int(phase == "after_core_mark")
            state = "NOT_SENT" if phase == "before_core_mark" else "UNKNOWN"
            assert window.attempts() == {state: 1}
            assert core.store.one("SELECT state FROM outbox")["state"] == (
                "CANCELLED" if phase == "before_core_mark" else "DELIVERY_UNCERTAIN")
            window.runtime.verify(active=False)

    run(scenario())


def test_catalog_requires_real_reviewed_bytes_and_no_remote_path_fallback(core, tmp_path):
    directory = tmp_path / "assets"
    review, data, manifest = catalog(core, directory)
    cfg = replace(config(), account_asset_directory=str(directory),
                  account_asset_policy_sha256=review.review_policy_sha256)
    plan = {"proposal": {"asset_id": review.asset_id}, "asset_sha256": review.asset_sha256}
    original = account_assets.prepare(cfg, plan, core.clock.now)
    assert original.data == data
    for update in ({"purpose": "meme"}, {"usage_conditions": ""}, {"source_url": "file:///etc/a"},
                   {"review_policy_sha256": "0" * 64}, {"width": True}, {"width": 17},
                   {"expires_at": (core.clock.now - timedelta(seconds=1)).isoformat()}):
        manifest.write_text(encode(review.model_dump(mode="json") | update), encoding="utf-8")
        with pytest.raises(ValueError, match="REVIEWED_ACCOUNT_ASSET_REQUIRED"):
            account_assets.prepare(cfg, plan, core.clock.now)
    manifest.write_text(review.model_dump_json(), encoding="utf-8")
    (directory / (review.asset_sha256 + ".bin")).write_bytes(data + b"changed")
    with pytest.raises(ValueError, match="REVIEWED_ACCOUNT_ASSET_REQUIRED"):
        account_assets.prepare(cfg, plan, core.clock.now)


def test_account_scope_and_envelope_reject_caller_chosen_targets():
    cfg = config()
    material = {"schema_version": 1, "plan_id": "social_" + "a" * 32,
        "message_id": "msg_" + "b" * 32, "delivery_key": "social:social_" + "a" * 32,
        "action_type": "qzone.publish", "policy_version": 1, "policy_sha256": "c" * 64,
        "authorization_sha256": "d" * 64, "bot_qq_id": cfg.bot_qq_id,
        "owner_qq_id": cfg.owner_qq_id, "proposal": {"kind": "qzone_publish", "text": "公开文字",
            "source_refs": ["obs_public"]}, "source_snapshot_sha256": "e" * 64,
        "profile_baseline_sha256": None, "asset_sha256": None,
        "audience": {"ugc_right": 16, "target_uins": [cfg.owner_qq_id]},
        "expires_at": "2026-10-25T00:00:00+00:00"}
    plan = material | {"payload_sha256": content_hash(material)}
    item = {"message_id": plan["message_id"], "delivery_key": plan["delivery_key"],
        "action_type": plan["action_type"], "content": plan, "content_sha256": content_hash(plan),
        "lease_token": "lease_" + "f" * 32, "lease_until": "2026-10-25T00:00:00+00:00",
        "window_sha256": "f" * 64, "delivery_lane": "qq_account",
        "conversation_platform_session_key": cfg.origin}
    validate_item(item, cfg, companion=True, account_scope=True)
    with pytest.raises(ValueError, match="ACCOUNT_ACTION_SCOPE_REQUIRED"):
        validate_item(item, cfg, companion=True)
    for audience in ({"ugc_right": 1, "target_uins": [cfg.owner_qq_id]},
                     {"ugc_right": 16, "target_uins": ["10003"]}):
        altered = material | {"audience": audience}
        altered["payload_sha256"] = content_hash(altered)
        with pytest.raises(ValueError, match="ACCOUNT_FROZEN_PLAN_REQUIRED"):
            account_actions.validate(item | {"content": altered,
                "content_sha256": content_hash(altered)}, cfg)


@pytest.mark.parametrize("mutation", ["body", "path", "stream", "review"])
def test_actual_account_request_and_asset_are_rechecked_after_core_await(core, tmp_path, mutation):
    kind = "avatar" if mutation == "review" else "qzone_publish"
    path = account_actions.PATHS["qq.profile.avatar" if kind == "avatar" else "qzone.publish"]
    ref, conversation, options, proposal = native_fixture(core, kind)
    requests, wire, checks = [], [], []

    async def native(request):
        if request.url.path == path:
            wire.append(b"".join([chunk async for chunk in request.stream]))
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": None if kind == "avatar" else {"tid": "tid1"}})

    async def scenario():
        directory = tmp_path / "reviewed"
        bridge_options = {"account_asset_directory": str(directory),
                          "account_asset_policy_sha256": "e" * 64} if kind == "avatar" else {}
        # The normal fixture's synchronous responder delegates async native I/O unchanged.
        async with account_host(core, tmp_path,
            social_policy=options["core_options"]["social_autonomy"],
            bridge_options=bridge_options) as (
            window, client, protocol, private_sends,
        ), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: model_response(proposal))) as model:
            core.model = ModelClient(core.settings, model)
            review, _, manifest = (catalog(core, directory) if kind == "avatar"
                                    else (None, None, None))
            await plan_action(core, ref, conversation, proposal, review=review)
            delegate = protocol._transport

            async def protocol_reply(request):
                if request.url.path == path:
                    return await native(request)
                return await delegate.handle_async_request(request)

            protocol._transport = httpx.MockTransport(protocol_reply)

            async def remember(request):
                if request.url.path == path:
                    requests.append(request)

            async def changed(response):
                if response.request.url.path.endswith("/account-actions/send-check"):
                    checks.append(response.request)
                    if mutation == "body":
                        requests[-1]._content = b'{"content":"changed","ugc_right":1}'
                    elif mutation == "path":
                        requests[-1].url = requests[-1].url.copy_with(path="/set_qq_profile")
                    elif mutation == "stream":
                        requests[-1].stream = httpx.ByteStream(b'{"ugc_right":1}')
                    else:
                        manifest.write_text(encode(review.model_dump(mode="json")
                                                    | {"purpose": "meme"}), encoding="utf-8")

            protocol.event_hooks["request"].append(remember)
            client.event_hooks["response"].append(changed)
            await pump_account(window)
            assert len(checks) == 1 and private_sends == []
            if mutation == "stream":
                assert len(wire) == 1
                assert json.loads(wire[0]) == {"content": proposal["text"], "ugc_right": 16,
                                              "target_uins": [10001]}
                assert window.attempts() == {"ACK": 1}
            else:
                assert wire == [] and window.attempts() == {"UNKNOWN": 1}
                assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"

    run(scenario())


def test_account_only_spends_existing_send_quota_with_zero_owner_input_allowance(
    core, tmp_path, monkeypatch,
):
    ref, conversation, options, proposal = native_fixture(core)

    def native(request):
        if request.url.path == "/send_qzone_msg":
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": {"tid": "quota-tid"}})

    async def scenario():
        async with account_host(core, tmp_path,
            social_policy=options["core_options"]["social_autonomy"], protocol_reply=native,
            quota={"owner_inputs": 0, "send_attempts": 1}) as (window, client, protocol, _), \
            httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: model_response(proposal))) as model:
            core.model = ModelClient(core.settings, model)
            await plan_action(core, ref, conversation, proposal)
            await pump_account(window)
            assert window.captures() == 0
            operations = view(core)["operations"]
            assert len(operations) == 1 and next(iter(operations.values()))["state"] == "SENT"
            assert next(iter(operations)).startswith("send:")

    run(scenario())
