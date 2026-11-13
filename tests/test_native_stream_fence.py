"""Preview writes between full conversation checks stay fenced by the original guards."""

import asyncio
import json

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import native_stream
from tests.conftest import run
from tests.sdk_reader_fixture import worker_for
from tests.support.model_stream import chunk
from tests.support.native_chat import enroll, packet
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def stream(client, core, tmp_path, monkeypatch, between):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    accepted = client.post('/v1/console/chat/messages', headers=headers,
                           json=packet(core, key, authority)).json()
    checks = []
    original = native_stream.check_conversation

    def counted(*args, **kwargs):
        checks.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(native_stream, 'check_conversation', counted)

    async def scenario():
        release = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield chunk({'content': '首段'})
                await asyncio.wait_for(release.wait(), 30)
                for _ in range(30):
                    await asyncio.sleep(0.05)
                    yield chunk({'content': '字'})
                yield chunk(finish='stop')
                yield chunk(usage={'prompt_tokens': 100, 'completion_tokens': 20})
                yield b'data: [DONE]\n\n'

        def provider(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  stream=Body())

        async with worker_for(core, provider) as worker:
            task = asyncio.create_task(worker.run_once('fence', job_id=accepted['job_id']))
            async with asyncio.timeout(30):
                while not (core.store.one('SELECT preview_json FROM agent_conversations')
                           or {}).get('preview_json'):
                    assert not task.done(), task.result() if task.done() else None
                    await asyncio.sleep(0.02)
            first = len(checks)
            between(core)
            release.set()
            await task
            return first

    first = run(scenario())
    return checks, first, accepted


def test_a_burst_of_deltas_shares_one_full_check(client, core, tmp_path, monkeypatch):
    checks, first, accepted = stream(client, core, tmp_path, monkeypatch, lambda core: None)
    # About fifteen flushes inside the 2 s fence window need no further full check; the final
    # forced flush at the end of the text part still takes one.
    assert len(checks) - first <= 2, len(checks)
    turn = client.get('/v1/console/chat/messages/' + accepted['event_id']).json()['items'][0]
    assert [reply['text'] for reply in turn['replies']] == ['首段' + '字' * 30]


def test_a_withdrawn_preview_forces_the_full_check_again(client, core, tmp_path, monkeypatch):
    def withdraw(core):
        with core.store.transaction():
            native_stream.withdraw(core.store)

    checks, first, _ = stream(client, core, tmp_path, monkeypatch, withdraw)
    # The version fence refuses the cheap path right after the withdrawal.
    assert len(checks) - first >= 1
    row = core.store.one('SELECT preview_json FROM agent_conversations')
    assert row['preview_json'] is None or json.loads(row['preview_json'])['state'] != 'GENERATING'
