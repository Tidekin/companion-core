"""Public previews retain original job custody and never become final deliveries."""

import asyncio
import json
import ssl
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import native_stream, owner_authority
from companion.owner_authority_protocol import AuthorityStop
from tests.conftest import run
from tests.sdk_reader_fixture import worker_for
from tests.support.model_stream import chunk
from tests.support.native_chat import enroll, packet
from tests.support.native_chat_tls import serve
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


@pytest.mark.parametrize('outcome', ['complete', 'eof', 'revoke', 'lease', 'erase',
                                     'progress', 'burst'])
def test_preview_first_block_final_fence_and_withdrawal(
    client, core, tmp_path, outcome, monkeypatch,
):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    accepted = client.post('/v1/console/chat/messages', headers=headers,
        json=packet(core, key, authority)).json()
    event_id, job_id = accepted['event_id'], accepted['job_id']
    calls = []
    checks = []
    if outcome == 'burst':
        original = native_stream.check_conversation

        def expensive_check(*args, **kwargs):
            result = original(*args, **kwargs)
            checks.append(1)
            time.sleep(0.11)  # Preserve real validation; emulate a slow production boundary.
            return result

        monkeypatch.setattr(native_stream, 'check_conversation', expensive_check)

    async def scenario():
        release = asyncio.Event()

        class Gated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield chunk({'reasoning_content': 'PRIVATE_THINKING_NEVER_PUBLIC'})
                yield chunk({'content': '首段正文'})
                await asyncio.wait_for(release.wait(), 30)
                if outcome == 'eof':
                    return
                if outcome == 'progress':
                    for _ in range(3):
                        await asyncio.sleep(0.12)
                        core.clock.advance(core.settings.lease_seconds // 2)
                        yield chunk({'content': '，持续正文'})
                elif outcome == 'burst':
                    for _ in range(32):
                        yield chunk({'content': '字'})
                yield chunk({'content': '，最终正文。'})
                yield chunk(finish='stop')
                yield chunk(usage={'prompt_tokens': 100, 'completion_tokens': 20})
                yield b'data: [DONE]\n\n'

        def provider(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  stream=Gated())

        async with worker_for(core, provider) as worker:
            task = asyncio.create_task(worker.run_once('stream-synthetic-worker', job_id=job_id))
            try:
                async with asyncio.timeout(30):
                    while True:
                        history = client.get('/v1/console/chat/messages/' + event_id).json()
                        turn = history['items'][0]
                        if (turn.get('preview') or {}).get('text'):
                            break
                        assert not task.done(), task.result() if task.done() else history
                        await asyncio.sleep(0.025)
                assert turn['state'] == 'RUNNING' and turn['replies'] == []
                assert turn['preview']['text'] == '首段正文'
                assert turn['preview']['state'] == 'GENERATING'
                assert 'PRIVATE_THINKING' not in json.dumps(history)
                assert not task.done() and not core.store.rows('SELECT 1 FROM outbox')
                if outcome == 'revoke':
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id='stop-stream', authority=authority))
                    assert core.store.one('SELECT preview_json FROM agent_conversations')[
                        'preview_json'] is None
                elif outcome == 'lease':
                    core.clock.advance(3600)
                elif outcome == 'erase':
                    with core.store.transaction(control_write=True):
                        core.ledger.expire_events(core.store, [event_id], core.clock())
                    assert core.store.one('SELECT preview_json FROM agent_conversations')[
                        'preview_json'] is None
                    with core.store.transaction(control_write=True):
                        core.ledger.replay(core.store)
                if outcome in {'revoke', 'lease', 'erase'}:
                    with core.store.read_snapshot():
                        assert native_stream.project(core, event_id)['text'] == ''
            finally:
                release.set()
                await task
            if outcome in {'complete', 'progress', 'burst'}:
                turn = client.get('/v1/console/chat/messages/' + event_id).json()['items'][0]
                assert turn['state'] == 'SUCCEEDED'
                assert turn['preview']['state'] == 'COMPLETE' and not turn['preview']['text']
                middle = '，持续正文' * 3 if outcome == 'progress' else (
                    '字' * 32 if outcome == 'burst' else '')
                assert [reply['text'] for reply in turn['replies']] == [
                    '首段正文' + middle + '，最终正文。']
                assert turn['preview']['version'] > 1
                if outcome == 'burst':
                    assert len(checks) < 20, 'buffered tiny deltas must share public validation'
            elif outcome == 'eof':
                turn = client.get('/v1/console/chat/messages/' + event_id).json()['items'][0]
                assert turn['uncertain'] and turn['state'] != 'SUCCEEDED'
                assert turn['preview']['state'] == 'INTERRUPTED'
                assert turn['preview']['text'] == '首段正文'
                core.clock.advance(6)
                await worker.run_once('stream-no-regeneration', job_id=job_id)
            else:
                assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                      (job_id,))['state'] != 'SUCCEEDED'
                assert all('最终正文' not in row['content_json'] for row in core.store.rows(
                    "SELECT content_json FROM outbox WHERE state='SENT'"))
            assert len(calls) == 1

    run(scenario())


def test_idle_subscription_skips_unchanged_history(client, core, tmp_path, monkeypatch):
    from companion import native_chat
    from companion_delivery_protocol.native_chat import device_identity

    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    client.post('/v1/console/chat/messages', headers=headers, json=packet(core, key, authority))
    context = SimpleNamespace(store=core.store, settings=core.settings, clock=core.clock,
        session={'native_device_id': device_identity(key.public_key().public_bytes_raw().hex())[0]})
    seen = []
    original = native_chat._turn
    def project(*args):
        seen.append(1)
        return original(*args)
    monkeypatch.setattr(native_chat, '_turn', project)
    with core.store.read_snapshot():
        first, signatures = native_chat.changes(context, {})
    with core.store.read_snapshot():
        idle, _ = native_chat.changes(context, signatures)
    assert len(first) == len(seen) == 1 and idle == []


def test_live_sse_subscription_revalidates_revoked_device(client, core, tmp_path):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    accepted = client.post('/v1/console/chat/messages', headers=headers,
        json=packet(core, key, authority)).json()
    cookie = client.cookies.get('companion_owner_session')

    async def scenario():
        async with serve(core, tmp_path) as (origin, certificate):
            async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=certificate),
                headers={'Origin': origin, 'Cookie': 'companion_owner_session=' + cookie}) as http:
                async with http.stream('GET', origin + '/v1/console/chat/events') as response:
                    assert response.status_code == 200
                    lines = response.aiter_lines()
                    async with asyncio.timeout(5):
                        async for line in lines:
                            if line.startswith('data:'):
                                assert accepted['event_id'] in line
                                break
                        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                            operation_id='stop-live-subscription', authority=authority))
                        async for line in lines:
                            if line.startswith('event: session'):
                                break
                        else:
                            raise AssertionError('revoked subscription did not report session end')
                        error = await anext(lines)
                        assert 'error' in error and '原生文本' not in error
                assert (await http.get(origin + '/v1/console/chat/events')).status_code == 403

    run(scenario())
