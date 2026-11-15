"""A private Life channel revision keeps historical UNKNOWN and original scope."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority
from companion.contracts import CoreError
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.owner_authority_protocol import OwnerGrantPolicy
from tests import owner_model_fixtures
from tests.conftest import run
from tests.model_carry_support import migrated
from tests.support.core_http import real_core_http
from tests.support.model_carry_lifecycle import current_unknown, stop_current
from tests.support.owner_authority import activate


def carried_life(core, tmp_path, monkeypatch):
    core.settings.life_loop = LifeLoopConfig(mode='SHADOW')
    scope = LifeShadowApproval(scope='owner_private_life_shadow_v1',
        authorization_sha256='c' * 64, conversation_id='conv_qq_owner',
        source_scope='owner_private_history', purposes=['PRIVATE_MESSAGE_DRAFT'],
        max_items=2, expires_at=core.clock.now + timedelta(days=3))

    def policy(**values):
        return OwnerGrantPolicy(**{**values, 'purposes': ['chat', 'history', 'life', 'proactive'],
            'history': {'scope': 'owner_private', 'not_before': core.clock.now - timedelta(days=1),
                        'max_turns': 15, 'max_utf8_bytes': 16000}, 'life_shadow': scope})

    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, 'OwnerGrantPolicy', policy)
        return migrated(core, tmp_path)


def revised(case):
    return case.policy.model_copy(update={'life_shadow': case.policy.life_shadow.model_copy(
        update={'conversation_id': 'conv_native_owner'})})


def test_private_life_channel_change_real_http_preserves_original_liability(
    core, tmp_path, monkeypatch,
):
    case = carried_life(core, tmp_path, monkeypatch)
    before = {table: core.store.rows('SELECT * FROM ' + table)
              for table in ('model_usage', 'model_admissions')}
    prefix = Path(core.settings.paid_model.journal_path).read_bytes()
    core.settings.control_token = SecretStr('synthetic-life-carry-control')

    async def exercise():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f'http://127.0.0.1:{port}', trust_env=False,
            headers={'Authorization': 'Bearer ' + core.settings.control_token.get_secret_value()},
        ) as http:
            stopped = await http.post('/internal/v1/owner-authority/stop', json={
                'authority': case.successor['authority'], 'operation_id': 'stop-life-channel'})
            assert stopped.status_code == 200, stopped.text
            query = {'expected_revision': 4, 'policy': revised(case).model_dump(mode='json')}
            preview = await http.post('/internal/v1/owner-authority/preview', json=query)
            assert preview.status_code == 200, preview.text
            activated = await http.post('/internal/v1/owner-authority/activate', json={
                **query, 'preview_sha256': preview.json()['preview_sha256'],
                'operation_id': 'move-private-life-channel'})
            assert activated.status_code == 200, activated.text
            active = activated.json()
            assert active['authority']['revision'] == 5
            assert active['authority']['policy_sha256'] != case.successor['authority'][
                'policy_sha256']
            assert owner_authority.current(core.store, core.settings, core.clock,
                purpose='life').policy.life_shadow.conversation_id == 'conv_native_owner'
        return active

    active = run(exercise())
    for table, rows in before.items():
        assert core.store.rows('SELECT * FROM ' + table) == rows
    assert before['model_usage'][0]['state'] == 'UNKNOWN'
    assert Path(core.settings.paid_model.journal_path).read_bytes().startswith(prefix)
    assert not core.store.rows('SELECT * FROM outbox')
    stop_current(core, SimpleNamespace(successor=active))
    activate(core, case.policy, revision=6, operation='restore-private-life-channel')
    for table, rows in before.items():
        assert core.store.rows('SELECT * FROM ' + table) == rows


@pytest.mark.parametrize('change', [
    'max_items', 'purposes', 'expires_at', 'authorization_sha256', 'source_scope',
])
def test_life_channel_revision_cannot_expand_scope(core, tmp_path, monkeypatch, change):
    case = carried_life(core, tmp_path, monkeypatch)
    stop_current(core, case)
    policy = revised(case)
    changes = {'max_items': 3, 'purposes': ['PRIVATE_MESSAGE_DRAFT', 'PRIVATE_DIARY'],
        'expires_at': policy.life_shadow.expires_at + timedelta(days=1),
        'authorization_sha256': 'b' * 64, 'source_scope': 'selected'}
    scope_change = {change: changes[change]}
    if change == 'source_scope':
        scope_change['source_event_ids'] = ['event_selected_life']
    policy = policy.model_copy(update={'life_shadow': policy.life_shadow.model_copy(
        update=scope_change)})
    before = Path(core.settings.paid_model.journal_path).read_bytes()
    with pytest.raises(CoreError):
        activate(core, policy, revision=4, operation='reject-wider-life-channel')
    assert Path(core.settings.paid_model.journal_path).read_bytes() == before


def test_current_unknown_blocks_life_channel_revision(core, tmp_path, monkeypatch):
    case = carried_life(core, tmp_path, monkeypatch)
    current_unknown(core)
    stop_current(core, case)
    with pytest.raises(CoreError, match='OWNER_MODEL_CARRY_NOT_QUIESCENT'):
        activate(core, revised(case), revision=4, operation='reject-active-life-cost')
