"""First-party native entry never requires a QQ transport or an external send lease."""

import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion_delivery_protocol.authority import capture_signing_payload
from companion_delivery_protocol.native_chat import capture, device_identity
from tests.conftest import run
from tests.support.native_history_delivery import sdk_step
from tests.support.owner_authority import activate
from tests.support.owner_console import ORIGIN, PASSWORD
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def enroll(client, key):
    public_key = key.public_key().public_bytes_raw().hex()
    login = client.post('/v1/console/session', json={
        'password': PASSWORD, 'device_public_key': public_key}, headers={'Origin': ORIGIN})
    assert login.status_code == 200, login.text
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': login.json()['csrf_token'],
               'Idempotency-Key': 'enroll-native-device'}
    body = {'public_key': public_key}
    preview = client.post('/v1/console/chat/devices/preview', json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    if not value['registered']:
        result = client.post('/v1/console/chat/devices/authorize', headers=headers,
            json={**body, **{name: value[name]
                            for name in ('expected_revision', 'preview_sha256')}})
        assert result.status_code == 200, result.text
        value = result.json()
    assert value['registered'], value
    return headers, value['authority']


def packet(core, key, authority, text='原生文本输入', request_id=None):
    value = capture(key.public_key().public_bytes_raw().hex(), authority,
                    request_id or uuid.uuid4().hex, text, core.clock())
    value['signature'] = key.sign(capture_signing_payload(value)).hex()
    return {'capture': value, 'text': text}


def test_native_session_capture_isolated_and_idempotent(client, core, tmp_path):
    original, _ = captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    assert authority['revision'] == original.authority['revision'] + 1
    view = owner_authority.current(core.store, core.settings, core.clock, purpose='chat')
    assert view.policy.capture is not None
    device_id, _ = device_identity(key.public_key().public_bytes_raw().hex())
    assert device_id in view.policy.native_devices
    assert client.get('/v1/console/configuration').status_code == 403
    body = packet(core, key, authority)
    assert client.post('/v1/console/chat/messages', json=body).status_code == 403
    response = client.post('/v1/console/chat/messages', json=body, headers=headers)
    assert response.status_code == 202, response.text
    duplicate = client.post('/v1/console/chat/messages', json=body, headers=headers)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()['duplicate']
    changed = client.post('/v1/console/chat/messages', json={**body, 'text': 'tampered'},
                          headers=headers)
    assert changed.status_code == 403
    forged = packet(core, key, authority)
    forged['capture']['signature'] = '0' * 128
    assert client.post('/v1/console/chat/messages', json=forged, headers=headers).status_code == 403
    assert len(core.store.rows("SELECT 1 FROM events WHERE source='native.chat'")) == 1
    row = core.store.one('SELECT source,actor_id,privacy_scope FROM events WHERE event_id=%s',
                         (response.json()['event_id'],))
    assert tuple(row.values()) == ('native.chat', 'owner', 'private')
    history = client.get('/v1/console/chat/messages').json()
    assert history['items'][0]['text'] == body['text']
    assert history['items'][0]['replies'] == []
    newer = packet(core, key, authority, '下一条原生消息')
    assert client.post('/v1/console/chat/messages', json=newer, headers=headers).status_code == 202
    page = client.get('/v1/console/chat/messages?limit=1').json()
    assert page['items'][0]['event_id'] == newer['capture']['event_id']
    older = client.get('/v1/console/chat/messages', params={
        'limit': 1, 'before': page['next_before']}).json()
    assert older['items'][0]['event_id'] == body['capture']['event_id']


def test_native_http_independent_worker_publishes_and_recovers_history(client, core, tmp_path):
    from tests.support.owner_authority_setup import prepare

    core.settings.memory_enabled = False
    key = Ed25519PrivateKey.generate()
    policy = prepare(core, tmp_path)
    device_id, identity = device_identity(key.public_key().public_bytes_raw().hex())
    activate(core, OwnerGrantPolicy.model_validate({**policy.model_dump(),
        'purposes': ['chat'], 'capture': None, 'native_devices': {device_id: identity}}))
    headers, authority = enroll(client, key)
    body = packet(core, key, authority)
    first = client.post('/v1/console/chat/messages', json=body, headers=headers)
    assert first.status_code == 202, first.text
    job_id, wire = run(sdk_step(core, tmp_path, first.json()['event_id'], 1))
    assert any(body['text'] in str(item) for item in wire['messages'])
    status = client.get('/v1/console/chat/messages/' + first.json()['event_id'])
    assert status.status_code == 200, status.text
    turn = status.json()['items'][0]
    assert turn['state'] == 'SUCCEEDED'
    assert [item['text'] for item in turn['replies']] == ['原始历史答复']
    assert all(item['state'] == 'SENT' for item in turn['replies'])
    assert core.store.one('SELECT action_type FROM outbox')['action_type'] == 'native.private.reply'
    from companion.contracts import LeaseRequest
    from companion.transport import lease_deliveries

    request = LeaseRequest(worker_id='synthetic-qq-bridge', authority=authority)
    assert lease_deliveries(core.store, core.settings, request, core.clock()) == {'items': []}
    # Re-login/restart reads the same persisted original messages; never invokes a model again.
    headers, same = enroll(client, key)
    assert same == authority
    assert client.get('/v1/console/chat/messages').json()['items'] == [turn]
    assert len(core.store.rows('SELECT 1 FROM model_usage')) == 1


def test_revoked_device_cannot_regrant_using_old_login(client, core, tmp_path):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    public_key = key.public_key().public_bytes_raw().hex()
    view = owner_authority.current(core.store, core.settings, core.clock, purpose='chat')
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(), 'native_devices': {}})
    activate(core, policy, revision=authority['revision'], operation='remove-native-device')
    denied = client.post('/v1/console/chat/messages', json=packet(core, key, authority),
                         headers=headers)
    assert denied.status_code == 403
    preview = client.post('/v1/console/chat/devices/preview', json={'public_key': public_key},
                          headers=headers).json()
    denied = client.post('/v1/console/chat/devices/authorize', headers=headers, json={
        'public_key': public_key, **{name: preview[name]
                                   for name in ('expected_revision', 'preview_sha256')}})
    assert denied.status_code in {403, 409}, denied.text
    assert not owner_authority.current(core.store, core.settings, core.clock,
                                       purpose='chat').policy.native_devices


def memory_scope(core):
    from companion.store import after

    view = owner_authority.current(core.store, core.settings, core.clock, purpose='chat')
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        'purposes': ['chat', 'memory', 'history'], 'memory_scope': 'owner_private',
        'history': {'scope': 'owner_private', 'not_before': after(core.clock(), -1),
                    'max_turns': 15, 'max_utf8_bytes': 16000}})
    activate(core, policy, revision=view.authority.revision, operation='native-memory-history')


def test_native_memory_commands_context_history_and_erasure(client, core, tmp_path, monkeypatch):
    import json

    import numpy as np

    from companion.reply_segments import SegmentedReplyConfig
    from companion.retrieval import SemanticIndex
    from tests.sdk_reader_fixture import answer, response, worker_for

    captured(core, tmp_path)
    memory_scope(core)
    core.settings.memory_enabled = True
    core.settings.memory_rerank = False
    core.settings.turn_recall_rerank = False
    core.settings.segmented_reply = SegmentedReplyConfig(min_chars=4, interval_ms=1500)
    # Only local embedding inference is fixed; nomination, provenance, scope, pinning,
    # history and final publication all use their real implementations.
    monkeypatch.setattr(SemanticIndex, '_infer', lambda self, query, contents:
        (tuple(np.asarray([1., 0.], dtype='<f4') for _ in contents),
         np.asarray([1., 0.], dtype='<f4')))

    class Encoder:
        def passage_embed(self, texts, **kwargs):
            return (np.asarray([1., 0.], dtype='<f4') for _ in texts)

        query_embed = passage_embed

    monkeypatch.setattr(SemanticIndex, 'warmup', lambda self: Encoder())
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return response(answer('第一段足够长。第二段也足够长。'), stream=requests[-1].get('stream'))

    def send(text):
        response = client.post('/v1/console/chat/messages', json=packet(core, key, authority, text),
                               headers=headers)
        assert response.status_code == 202, response.text
        return response.json()

    async def scenario():
        async with worker_for(core, provider) as worker:
            remember = send('/记住这个 我喜欢青竹茶。')
            assert await worker.run_once('native-memory-command', job_id=remember['job_id'])
            assert not requests
            mid = core.store.one("SELECT memory_id FROM memories WHERE content='我喜欢青竹茶。'")
            assert mid is not None
            first = send('我喜欢什么茶？')
            assert await worker.run_once('native-memory-chat', job_id=first['job_id'])
            assert '青竹茶' in json.dumps(requests[-1], ensure_ascii=False)
            first_history = client.get('/v1/console/chat/messages/' + first['event_id']).json()
            assert first_history['items'][0]['state'] == 'SUCCEEDED', first_history
            assert len(first_history['items'][0]['replies']) == 2
            assert all(reply['state'] == 'SENT' for reply in first_history['items'][0]['replies'])
            core.clock.advance(1)
            second = send('你刚才说了什么？')
            from companion import owner_input

            job = worker.store.one('SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
                                   'USING(event_id) WHERE job_id=%s', (second['job_id'],))
            sources = owner_input.history_sources(worker.store, worker.settings, worker.clock, job)
            assert first['event_id'] in {item['event_id'] for item in sources}, sources
            assert await worker.run_once('native-history-chat', job_id=second['job_id'])
            assert worker.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                     (second['job_id'],)) == {
                                         'state': 'SUCCEEDED', 'last_error_code': None}
            assert '第一段足够长' in json.dumps(requests[-1], ensure_ascii=False)
            forget = send('/忘记这一条 ' + mid['memory_id'])
            assert await worker.run_once('native-forget-command', job_id=forget['job_id'])
            history = client.get('/v1/console/chat/messages/' + remember['event_id']).json()
            assert history['items'][0]['redacted']
            assert not history['items'][0]['text']
            assert all(not reply['text'] for reply in history['items'][0]['replies'])
            assert len(requests) == 2

    run(scenario())


def test_native_publication_rechecks_stop_after_model(client, core, tmp_path):
    import httpx

    from companion.owner_authority_protocol import AuthorityStop
    from tests.sdk_reader_fixture import answer, worker_for

    captured(core, tmp_path)
    core.settings.memory_enabled = False
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    first = client.post('/v1/console/chat/messages', json=packet(core, key, authority),
                        headers=headers).json()

    def provider(request):
        owner_authority.stop(core.store, core.settings, core.clock,
            AuthorityStop(operation_id='stop-native-after-model', authority=authority))
        return httpx.Response(200, json=answer('撤销后不可发布的原文'))

    async def scenario():
        async with worker_for(core, provider) as worker:
            assert await worker.run_once('native-stop-fence', job_id=first['job_id'])
            assert not worker.store.rows('SELECT 1 FROM outbox')
            assert worker.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                     (first['job_id'],))['state'] == 'CANCELLED'

    run(scenario())


def test_native_unknown_model_result_never_regenerates_on_reconnect(client, core, tmp_path):
    import httpx

    from tests.sdk_reader_fixture import worker_for

    captured(core, tmp_path)
    core.settings.memory_enabled = False
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    body = packet(core, key, authority)
    first = client.post('/v1/console/chat/messages', json=body, headers=headers).json()
    calls = []

    def provider(request):
        calls.append(1)
        raise httpx.ReadTimeout('synthetic unknown result', request=request)

    async def scenario():
        async with worker_for(core, provider) as worker:
            assert await worker.run_once('native-unknown-result', job_id=first['job_id'])
            assert not await worker.run_once('native-unknown-restart', job_id=first['job_id'])
            assert worker.store.one('SELECT state FROM model_usage')['state'] == 'UNKNOWN'
            core.clock.advance(6)
            await worker.run_once('native-unknown-due-retry', job_id=first['job_id'])
            assert len(calls) == 1

    run(scenario())
    duplicate = client.post('/v1/console/chat/messages', json=body, headers=headers)
    assert duplicate.status_code == 202 and duplicate.json()['duplicate']
    assert duplicate.json()['state'] != 'SUCCEEDED'
    assert len(calls) == 1
    history = client.get('/v1/console/chat/messages').json()
    assert history['items'][0]['uncertain']
    assert len(core.store.rows('SELECT 1 FROM jobs')) == 1
