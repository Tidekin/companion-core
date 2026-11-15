"""Signed native input reaches Life's independent worker and native publication."""

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from companion import owner_authority, paid_budget
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


@pytest.mark.parametrize("erased", [False, True])
def test_native_input_life_draft_publishes_once_or_stops_after_erasure(
    client, core, tmp_path, erased,
):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
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
        purposes=['life', 'proactive'], authorization_sha256='d' * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        life_shadow=scope)
    authority = activate(core, policy, revision=revision,
                         operation='native-life-grant')[0]['authority']
    core.settings.control_token = SecretStr('synthetic-native-life-control')
    core.clock.advance(86400)

    async def exercise():
        observed = await native_child(core, tmp_path, source, 'PRIVATE_MESSAGE_DRAFT', 1,
                                      wording='你说的导出问题我还记着。\n\n等你方便时再一起看。')
        assert observed['provider_calls'] == 1
        result = core.store.one('SELECT * FROM life_activity_results')
        assert result['state'] == 'SHADOW', observed
        if erased:
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, [source], core.clock())
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f'http://127.0.0.1:{port}', trust_env=False,
            headers={'Authorization': 'Bearer ' + core.settings.control_token.get_secret_value()},
        ) as http:
            body = request(core, result['job_id'], authority, 'qq.private.proactive')
            response = await http.post(f"/internal/v2/life/results/{result['job_id']}/consume",
                                       json=body.model_dump(mode='json'))
            if erased:
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
        restarted = await native_child(core, tmp_path, source, 'PRIVATE_MESSAGE_DRAFT', 2)
        assert restarted['provider_calls'] == 0
        assert owner_authority.current(core.store, core.settings, core.clock,
                                       purpose='proactive').authority.as_dict() == authority

    run(exercise())
