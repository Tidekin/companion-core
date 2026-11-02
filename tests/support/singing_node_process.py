"""Independent real node and provider processes with only external media/provider mocked."""

import asyncio
import copy
import json
import os
import ssl
import sys
from pathlib import Path
from unittest.mock import patch

import httpcore
import httpx

from companion.voice_protocol import SingingResult
from companion_voice_node.supervisor import ProcessSupervisor, child_entry
from companion_voice_node.worker import VoiceNodeConfig, VoiceNodeWorker
from source_reader.network import PublicHTTP
from tests.test_bili_audio import NAV, PLAY, VIEW
from tests.test_bili_media import pool
from tests.test_singing_protocol import result
from tests.test_source_reader import RecordingStream, response

AUDIO = b"native audio" * 256


def provider(packet):
    counter = Path(packet.pop("_counter"))
    counter.write_text(json.dumps({"pid": os.getpid(), "environment": sorted(os.environ),
        "packet_keys": sorted(packet), "provider_keys": sorted(packet["provider"])}))
    assert Path(packet["song_path"]).read_bytes() == AUDIO
    value = result(65).model_dump(mode="json")
    value["manifest"].update(source=packet["song_source"],
        node_manifest_sha256=packet["node_manifest_sha256"])
    return {"status": "SUCCEEDED", "payload": SingingResult.model_validate(value).model_dump(
        mode="json")}


class ProviderProcess(ProcessSupervisor):
    def __init__(self, workspace, temporary, counter):
        super().__init__(workspace, temporary, command=(sys.executable, "-B", "-m",
            "tests.support.singing_node_process", "--child", str(os.getpid())))
        self.counter = counter

    async def dispatch(self, packet):
        assert "credential" not in packet and "core_origin" not in packet
        await super().dispatch(packet | {"_counter": str(self.counter)})


async def node(config_path, certificate, counter, timestamp):
    config = VoiceNodeConfig.model_validate_json(config_path.read_bytes())
    view, play = copy.deepcopy(VIEW), copy.deepcopy(PLAY)
    view["data"].update(bvid="BV1xx411c7mD")
    view["data"]["pages"][0]["duration"] = 65
    play["data"]["timelength"] = 65000
    connection_pool, backend = pool(response(view), response(NAV), response(play),
                                     response(AUDIO))
    connect = backend.connect_tcp

    async def fragmented_audio(host, port, **kwargs):
        if len(backend.rows) != 1:
            return await connect(host, port, **kwargs)
        backend.hosts.append(host)
        header, audio = backend.rows.pop(0).split(b"\r\n\r\n", 1)
        stream = RecordingStream(b"", backend)
        httpcore.AsyncMockStream.__init__(stream, [header + b"\r\n\r\n",
            *[audio[i:i + 16] for i in range(0, len(audio), 16)]])
        return stream

    backend.connect_tcp = fragmented_audio
    public = PublicHTTP(connection_pool)
    trust = ssl.create_default_context(cafile=str(certificate))
    controls = []

    class ObservedCore(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/control"):
                controls.append(request.url.path)
            return await super().handle_async_request(request)

    with (patch("source_reader.network.PublicHTTP", return_value=public),
          patch("companion_voice_node.worker.time.time", return_value=timestamp)):
        outcome = await VoiceNodeWorker(config,
            transport=ObservedCore(verify=trust),
            supervisor_factory=lambda workspace, temporary: ProviderProcess(
                workspace, temporary, counter)).run(once=True)
    return {"pid": os.getpid(), "outcome": outcome, "requests": len(backend.requests),
            "control_requests": len(controls)}


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        raise SystemExit(child_entry(int(sys.argv[2]), runner=provider))
    print(json.dumps(asyncio.run(node(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
                                      float(sys.argv[4])))))
