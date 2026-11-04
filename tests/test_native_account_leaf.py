"""Native Account leaf, with real Core planning/lease/send-check and provider-only fake IO.

Reuse: DSH jobs-local exact-owner binding (packages/jobs/jobs-local/src/index.ts),
https://github.com/deepseek-ai/deepseek-harness; SnowLuma explicit friend whitelist,
https://snowluma.github.io/zh/docs/api/qzone/send_qzone_msg.
The leaf consumes host context; custody and authority approval remain host/Core checks.
"""

import asyncio
import base64
import copy
import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from companion.api import create_app
from companion.contracts import LeaseRequest
from companion.model import ModelClient
from companion.transport import lease_deliveries
from companion_onebot_bridge import account_actions as leaf
from companion_onebot_bridge.native_actions import send_account
from tests.social_profile_support import confirmed_profile_material
from tests.support.onebot_events import config
from tests.test_account_owner_transport import prepared
from tests.test_onebot_account_actions import catalog, model_response, plan_action
from tests.test_social_autonomy import background
from tests.test_social_owner_authority import authorize, close_provider_clients  # noqa: F401


@pytest.fixture
def delivery(core, tmp_path):
    ref, frozen = prepared(core, tmp_path)
    app = create_app(core.settings)
    app.state.core = core
    with TestClient(app, headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()
    }) as client:
        # TestClient lifespan owns a separate Core; use the fixture's prepared real store.
        app.state.core = core
        request = leaf.lease_request_owner(ref, frozen.authorization_sha256)
        response = client.post("/internal/v1/deliveries/lease", json=request)
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        context = SimpleNamespace(config=config(), authority=ref,
                                  clock=lambda: datetime.fromisoformat(core.clock()))
        yield context, item, frozen.authorization_sha256, client


def test_native_account_leaf_real_plan_approval_and_provider_bytes(delivery):
    runtime, item, authorization, client = delivery
    assert authorization != runtime.authority["policy_sha256"]
    prepared_action = leaf.prepare_owner(runtime, item, authorization)
    response = client.post(f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                           json={"lease_token": item["lease_token"]})
    assert response.status_code == 200, response.text
    response = client.post("/internal/v1/account-actions/send-check",
                           json=leaf.send_check_owner(runtime.authority, item))
    assert response.status_code == 200, response.text
    sent = []

    async def scenario():
        def native(request):
            sent.append(request)
            assert request.url.path == "/send_qzone_msg"
            assert json.loads(request.content) == {
                "content": item["content"]["proposal"]["text"], "ugc_right": 16,
                "target_uins": [10001]}
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": {"tid": "synthetic-tid"}})

        async with httpx.AsyncClient(base_url="https://synthetic.invalid",
                                     transport=httpx.MockTransport(native)) as provider:
            ack = await send_account(provider, prepared_action, item, runtime.config)
            assert ack.result == "SUCCEEDED"
            with pytest.raises(ValueError, match="ACCOUNT_ACTION_CHANGED"):
                await send_account(provider, replace(prepared_action, body=b"{}"),
                                   item, runtime.config)
        assert len(sent) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["mixed", "missing", "invalid", "foreign", "scope",
                                    "expired", "audience", "hash", "identity"])
def test_native_account_leaf_rejects_changed_binding(delivery, change):
    runtime, original, authorization, _ = delivery
    item = copy.deepcopy(original)
    if change == "mixed":
        item["window_sha256"] = "a" * 64
    elif change == "missing":
        del item["authority"]
    elif change == "invalid":
        item["authority"] = {}
    elif change == "foreign":
        item["authority"]["revision"] += 1
    elif change == "scope":
        authorization = runtime.authority["policy_sha256"]
    elif change == "expired":
        runtime.clock = lambda: datetime.fromisoformat(item["content"]["expires_at"])
    elif change == "audience":
        item["content"]["audience"]["ugc_right"] = 1
    elif change == "hash":
        item["content_sha256"] = "0" * 64
    else:
        runtime.config = config(owner_qq_id="99999")
    with pytest.raises(ValueError):
        leaf.prepare_owner(runtime, item, authorization)


def test_native_account_request_builders_reject_foreign_and_legacy(delivery):
    runtime, item, authorization, _ = delivery
    foreign = {**runtime.authority, "revision": runtime.authority["revision"] + 1}
    for changed in ({**item, "authority": foreign}, {**item, "window_sha256": "a" * 64}):
        with pytest.raises(ValueError):
            leaf.send_check_owner(runtime.authority, changed)
    with pytest.raises(ValueError):
        leaf.lease_request_owner(runtime.authority, "bad")
    legacy = {key: value for key, value in item.items() if key != "authority"}
    legacy["window_sha256"] = "a" * 64
    assert leaf.validate(legacy, runtime.config) == item["content"]
    with pytest.raises(ValueError):
        leaf.prepare_owner(runtime, legacy, authorization)


@pytest.mark.parametrize("kind", ["nickname", "signature", "avatar"])
def test_native_profile_preparation_keeps_frozen_fields_and_reviewed_asset(core, tmp_path, kind):
    _, source, conversation = background(core, nickname_enabled=True, signature_enabled=True,
        avatar_enabled=True, asset_policy_sha256="e" * 64, max_output_tokens=512)
    if kind != "avatar":
        confirmed_profile_material(core)
    _, authority = authorize(core, tmp_path)
    directory = tmp_path / "reviewed"
    review, picture, _ = catalog(core, directory) if kind == "avatar" else (None, None, None)
    proposal = ({"kind": kind, "asset_id": review.asset_id} if review else
                {"kind": kind, "value": "观星"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: model_response(proposal))) as provider:
            core.model = ModelClient(core.settings, provider)
            return await plan_action(core, source, conversation, proposal, review=review)

    frozen = asyncio.run(scenario())
    item = lease_deliveries(core.store, core.settings, LeaseRequest(**leaf.lease_request_owner(
        authority, frozen.authorization_sha256)), core.clock())["items"][0]
    runtime = SimpleNamespace(authority=authority,
        config=config(account_asset_directory=str(directory), account_asset_policy_sha256="e" * 64),
        clock=lambda: datetime.fromisoformat(core.clock()))
    result = leaf.prepare_owner(runtime, item, frozen.authorization_sha256)
    expected = ({"file": "base64://" + base64.b64encode(picture).decode()} if review else
                {"nickname" if kind == "nickname" else "longNick": proposal["value"]})
    assert json.loads(result.body) == expected
    assert result.payload_sha256 == frozen.payload_sha256
    if review:
        (directory / (review.asset_sha256 + ".bin")).write_bytes(b"tampered")
        with pytest.raises(ValueError):
            leaf.prepare_owner(runtime, item, frozen.authorization_sha256)
