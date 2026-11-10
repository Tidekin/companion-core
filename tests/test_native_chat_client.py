"""DPAPI at rest and exactly-once recovery through the actual native HTTP routes."""

import sys

import httpx
import pytest

from companion_chat_client.api import Api, ApiError, origin_url
from companion_chat_client.cache import Cache
from companion_chat_client.engine import Engine
from tests.conftest import run
from tests.support.native_history_delivery import sdk_step
from tests.support.owner_console import ORIGIN, PASSWORD
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


class LoseAcceptedResponse(httpx.BaseTransport):
    def __init__(self, inner):
        self.inner, self.lost = inner, False

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        try:
            content = b''.join(response.stream)
        finally:
            response.close()
        response = httpx.Response(response.status_code, headers=response.headers, content=content)
        if (request.method == 'POST' and request.url.path.endswith('/chat/messages')
                and not self.lost):
            self.lost = True
            response.close()
            raise httpx.ReadTimeout('synthetic lost acceptance', request=request)
        return response


@pytest.mark.parametrize('url', ['http://localhost', 'https://user:secret@core.test',
                                'https://core.test/other', 'https://core.test?secret=value'])
def test_client_rejects_non_origin_or_plaintext_server(url):
    with pytest.raises(ValueError):
        origin_url(url)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows DPAPI client')
def test_native_client_lost_response_restart_and_history_are_one_original_turn(
    client, core, tmp_path,
):
    captured(core, tmp_path)
    core.settings.memory_enabled = False
    directory = tmp_path / 'client'
    events = []
    cache = Cache(directory, ORIGIN)
    api = Api(ORIGIN, transport=LoseAcceptedResponse(client._transport))
    engine = Engine(api, cache, lambda *item: events.append(item), clock=core.clock)
    engine.login(PASSWORD)
    assert events[-1][0] == 'approval'
    with pytest.raises(ApiError) as denied:
        api.history()
    assert denied.value.status == 403
    engine.approve()
    text = '断网重连验收：青竹七号。'
    with pytest.raises(httpx.ReadTimeout):
        engine.send(text)
    original = cache.pending()[0]['packet']
    assert len(core.store.rows("SELECT 1 FROM events WHERE source='native.chat'")) == 1
    assert not cache.pending()[0]['admitted']
    engine.sync()
    assert cache.pending()[0]['packet'] == original
    assert cache.pending()[0]['admitted']
    assert len(core.store.rows('SELECT 1 FROM jobs')) == 1
    run(sdk_step(core, tmp_path, original['capture']['event_id'], 1))
    engine.sync()
    assert not cache.pending()
    assert cache.turns()[0]['replies'][0]['text'] == '原始历史答复'
    public_key = engine.public_key
    before = cache.turns()
    engine.close()
    persisted = b''.join(path.read_bytes() for path in directory.iterdir())
    assert text.encode() not in persisted
    assert PASSWORD.encode() not in persisted
    cache = Cache(directory, ORIGIN)
    other_origin = Cache(directory, 'https://another-core.test')
    assert other_origin.turns() == []
    other_origin.close()
    transport = LoseAcceptedResponse(client._transport)
    transport.lost = True
    restored = Engine(Api(ORIGIN, transport=transport), cache, lambda *item: None,
                      clock=core.clock)
    assert restored.public_key == public_key
    assert cache.turns() == before
    assert not restored.connected
    restored.login(PASSWORD)
    assert restored.connected
    assert cache.turns() == before
    assert len(core.store.rows('SELECT 1 FROM model_usage')) == 1
    restored.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows DPAPI client')
def test_offline_client_queue_never_rewrites_old_authority(tmp_path):
    directory = tmp_path / 'offline'
    cache = Cache(directory, ORIGIN)
    # Use a real valid authority reference from the protocol fixture shape.
    from companion_delivery_protocol.authority import AuthorityRef

    reference = AuthorityRef(2, 'grant_' + 'a' * 32, 1, 'b' * 64, 'test_generation').as_dict()
    cache.set('authority', reference)
    engine = Engine(Api(ORIGIN), cache, lambda *item: None)
    engine.send('离线待发送原文')
    first = cache.pending()[0]['packet']
    engine.authority = {**reference, 'revision': reference['revision'] + 1}
    engine.send('新的离线原文')
    assert cache.pending()[0]['packet'] == first
    assert not cache.pending()[0]['admitted']
    engine.close()
