"""Fragmented public audio must not turn one transfer into hundreds of Core RPCs."""

import asyncio
import time
from types import SimpleNamespace

import httpcore
import pytest

from companion.singing_protocol import SingingRecipe
from companion.voice_protocol import VoiceProtocolError
from companion_voice_node.worker import VoiceNodeWorker
from source_reader.network import PublicHTTP
from tests.conftest import run
from tests.support.bili_media import pool
from tests.support.source_reader import RecordingStream, response
from tests.test_bili_audio import BVID, NAV, PLAY, VIEW
from tests.test_voice_node_worker import node as node  # noqa: F401


async def transfer(config, tmp_path, monkeypatch, *, latency, fragment_seconds,
                   deadline_seconds=600, revoke_after=None):
    payload = b"a" * 4096
    transport, backend = pool(response(VIEW), response(NAV), response(PLAY), response(payload))
    clock = [1000.0]
    controls = []
    original_connect = backend.connect_tcp

    class AudioStream(RecordingStream):
        async def read(self, max_bytes, timeout=None):  # noqa: ASYNC109 -- transport protocol
            clock[0] += fragment_seconds
            await asyncio.sleep(0)
            return await super().read(max_bytes, timeout)

    async def connect(host, port, **kwargs):
        if len(backend.rows) != 1:
            return await original_connect(host, port, **kwargs)
        backend.hosts.append(host)
        header, body = backend.rows.pop(0).split(b"\r\n\r\n", 1)
        stream = AudioStream(b"", backend)
        httpcore.AsyncMockStream.__init__(stream,
            [header + b"\r\n\r\n", *[body[i:i + 32] for i in range(0, len(body), 32)]])
        return stream

    backend.connect_tcp = connect
    worker = VoiceNodeWorker(config.model_copy(update={"heartbeat_seconds": 3}))
    worker._lease_until = clock[0] + 30
    lease = SimpleNamespace()

    async def control(client, state, original, phase, **kwargs):
        assert original is lease and phase == "RUNNING"
        controls.append(clock[0])
        if revoke_after is not None and clock[0] >= 1000 + revoke_after:
            raise VoiceProtocolError("VOICE_LEASE_CANCEL")
        clock[0] += latency
        worker._lease_until = clock[0] + 30
        return SimpleNamespace(action="CONTINUE"), lease

    monkeypatch.setattr(worker, "_control", control)
    monkeypatch.setattr("companion_voice_node.worker.time",
                        SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    public = PublicHTTP(transport)
    monkeypatch.setattr("source_reader.network.PublicHTTP", lambda: public)
    recipe = SingingRecipe(bvid=BVID, source_event_id="observed_song", source_sha256="a" * 64,
        chat_job_id="job_" + "b" * 32, tool_call_id="original_song")
    packet = await worker._download_song(None, None, lease, recipe.model_dump_json().encode(),
                                        tmp_path, 1000 + deadline_seconds)
    assert packet["song_source"]["used_requests"] == 4
    assert packet["song_source"]["audio_bytes"] == len(payload)
    assert (tmp_path / "source.m4a").read_bytes() == payload
    assert len(backend.requests) == 4 and len(controls) >= 12
    return controls, clock[0] - 1000


@pytest.mark.parametrize("latency", [0.5, 5])
def test_fragmented_song_download_reserves_time_for_native_render(node, tmp_path, monkeypatch,
                                                                 latency):
    controls, elapsed = run(transfer(node[0], tmp_path, monkeypatch, latency=latency,
                                    fragment_seconds=0.01))
    assert len(controls) < 25
    assert elapsed < 120


def test_slow_stream_still_observes_live_stop_between_fragments(node, tmp_path, monkeypatch):
    with pytest.raises(VoiceProtocolError, match="VOICE_LEASE_CANCEL"):
        run(transfer(node[0], tmp_path, monkeypatch, latency=0.1, fragment_seconds=1,
                     revoke_after=20))
    assert (tmp_path / "source.m4a").stat().st_size < 4096


def test_stream_checks_original_deadline_before_accepting_late_bytes(node, tmp_path, monkeypatch):
    with pytest.raises(VoiceProtocolError, match="VOICE_EXECUTION_DEADLINE"):
        run(transfer(node[0], tmp_path, monkeypatch, latency=0.1, fragment_seconds=4,
                     deadline_seconds=12))
    assert (tmp_path / "source.m4a").stat().st_size < 4096
