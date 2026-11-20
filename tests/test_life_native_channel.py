"""Signed native input reaches Life's independent worker and native publication."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import native_push, owner_authority, paid_budget
from companion.autonomous_selector import native_selector
from companion.core import Core
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.reply_segments import SegmentedReplyConfig
from companion.store import after
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.life_expression_consumption import request
from tests.support.life_expression_entrypoint import native_child
from tests.support.native_chat import enroll, packet
from tests.support.owner_authority import activate
from tests.support.owner_console import client as client
from tests.support.owner_input import captured
from tests.support.paid_model import approved_live
from tests.support.social_owner_authority import stop


@pytest.mark.parametrize("outcome", ["share", "erase-before", "erase-after", "timeout", "limited"])
def test_native_input_life_draft_publishes_once_or_stops_after_erasure(
    client, core, tmp_path, outcome, monkeypatch,
):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    core.settings.native_push_project_id = "demo-companion-push"
    registration = {"token": "synthetic-registration-token", "route": "3" * 32}
    registered = client.put("/v1/console/chat/push", json=registration, headers=headers)
    assert registered.status_code == 200, registered.text
    incoming = client.post('/v1/console/chat/messages', headers=headers,
        json=packet(core, key, authority, text='自研客户端的导出问题明天再看。'))
    assert incoming.status_code == 202, incoming.text
    source = incoming.json()['event_id']
    conversation = core.store.one('SELECT conversation_id FROM events WHERE event_id=%s',
                                  (source,))['conversation_id']
    stop(core, authority)
    revision = owner_authority._latest(core.store)['revision']
    core.settings.life_loop = LifeLoopConfig(mode='SHADOW')
    core.settings.wake_enabled = core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.segmented_reply = SegmentedReplyConfig(mode='semantic', max_segments='unlimited')
    scope = LifeShadowApproval(scope='owner_private_life_shadow_v1',
        authorization_sha256='c' * 64, conversation_id=conversation,
        source_event_ids=[source], purposes=['PRIVATE_MESSAGE_DRAFT'], max_items=1,
        expires_at=after(core.clock(), 3 * 86400))
    approved_live(core, tmp_path, max_output_tokens=4096)
    policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=['life', 'proactive', 'chat'], authorization_sha256='d' * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        life_shadow=scope)
    authority = activate(core, policy, revision=revision,
                         operation='native-life-grant')[0]['authority']
    core.clock.advance(86400)

    async def exercise():
        observed = await native_child(core, tmp_path, source, 'PRIVATE_MESSAGE_DRAFT', 1,
                                      wording='你说的导出问题我还记着。\n\n等你方便时再一起看。')
        assert observed['provider_calls'] == 1
        result = core.store.one('SELECT * FROM life_activity_results')
        assert result['state'] == 'SHADOW', observed
        if outcome == "erase-before":
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [source], core.clock())
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f'http://127.0.0.1:{port}', trust_env=False,
            headers={'Authorization': 'Bearer ' + core.settings.control_token.get_secret_value()},
        ) as http:
            body = request(core, result['job_id'], authority, 'qq.private.proactive')
            response = await http.post(f"/internal/v2/life/results/{result['job_id']}/consume",
                                       json=body.model_dump(mode='json'))
            if outcome == "erase-before":
                assert response.status_code in {403, 409}, response.text
                assert not core.store.rows('SELECT 1 FROM outbox')
                return
            assert response.status_code == 200, response.text
            rows = core.store.rows('SELECT conversation_id,action_type,state,content_json FROM '
                                   'outbox ORDER BY segment_index')
            assert len(rows) == 2
            assert all((row['conversation_id'], row['action_type'], row['state']) ==
                       (conversation, 'native.private.proactive', 'SENT') for row in rows)
            repeated = await http.post(f"/internal/v2/life/results/{result['job_id']}/consume",
                                       json=body.model_dump(mode='json'))
            assert repeated.status_code == 200, repeated.text
            assert core.store.rows('SELECT conversation_id,action_type,state,content_json FROM '
                                   'outbox ORDER BY segment_index') == rows
        deliveries = core.store.rows("SELECT * FROM native_push_deliveries")
        assert len(deliveries) == 1 and deliveries[0]["state"] == "PENDING"
        if outcome == "erase-after":
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [source], core.clock())
        sent = []

        def provider(request):
            assert core.store.one("SELECT state FROM native_push_deliveries")["state"] == "UNKNOWN"
            assert request.url == "https://fcm.googleapis.com/v1/projects/demo-companion-push/messages:send"
            message = json.loads(request.content)["message"]
            sent.append(message)
            assert message["token"] == registration["token"]
            assert message["data"] == {"route": registration["route"]}
            assert message["notification"] == {"title": "Companion", "body": "有新消息"}
            assert message["android"]["notification"]["channel_id"] == "companion_proactive"
            if outcome == "timeout":
                raise httpx.ReadTimeout("unconfirmed")
            if outcome == "limited" and len(sent) == 1:
                return httpx.Response(429, headers={"Retry-After": "180"},
                                      json={"error": {"status": "RESOURCE_EXHAUSTED"}})
            return httpx.Response(200, json={"name": "projects/demo-companion-push/messages/123"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as channel:
            sender = native_push.FcmSender(channel)
            sender.access_token = AsyncMock(return_value="synthetic-oauth")
            monkeypatch.setattr(native_push, "FcmSender", lambda: sender)
            with Core(core.settings, core.model, core.clock) as worker:
                lane = next(lane for lane in native_selector(worker, "push-worker").lanes
                            if lane.name == "native_push")
                if outcome == "erase-after":
                    await lane.run()
                else:
                    assert (await lane.run()).progressed
                assert not (await lane.run()).progressed
                if outcome == "limited":
                    core.clock.advance(179)
                    assert not (await lane.run()).progressed
                    core.clock.advance(1)
                    assert (await lane.run()).progressed
                    assert not (await lane.run()).progressed
        status = core.store.one("SELECT state,error_code FROM native_push_deliveries")
        assert status["state"] == {"share": "ACCEPTED", "limited": "ACCEPTED", "timeout": "UNKNOWN",
                                   "erase-after": "CANCELLED"}[outcome], status
        assert len(sent) == int(outcome != "erase-after") + int(outcome == "limited")
        assert len({m["android"]["notification"]["tag"] for m in sent}) <= 1
        if outcome == "erase-after":
            return
        restarted = await native_child(core, tmp_path, source, 'PRIVATE_MESSAGE_DRAFT', 2)
        assert restarted['provider_calls'] == 0
        assert owner_authority.current(core.store, core.settings, core.clock,
                                       purpose='proactive').authority.as_dict() == authority

    run(exercise())
