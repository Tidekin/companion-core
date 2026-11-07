import asyncio
import json
import os
import ssl
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

from companion.voice_protocol import VoiceProtocolError
from companion_voice_node.worker import VoiceNodeConfig, VoiceNodeWorker
from tests.support.voice_node_api import node_wall_clock
from tests.support.voice_node_worker import FixtureSupervisor


async def independent_cancel_node():
    config = VoiceNodeConfig.model_validate_json(await asyncio.to_thread(
        Path(sys.argv[2]).read_text, "utf-8"))
    trust = ssl.create_default_context(cafile=sys.argv[3])
    counter, gate, loss = Path(sys.argv[4]), sys.argv[5] == "gate", sys.argv[6]

    class GatedSupervisor(FixtureSupervisor):
        async def start(self, seconds):
            await super().start(seconds)
            if gate:
                print(json.dumps({"stage": "child-ready", "pid": self.process.pid,
                    "identity": self.identity, "node_pid": os.getpid()}), flush=True)
                assert await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), 15) == (
                    "continue\n")

    class LostControl(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            nonlocal loss
            cancel = (request.url.path.endswith("/stop")
                      and json.loads(request.content).get("outcome") == "CANCELLED")
            if cancel and loss == "before":
                loss = "none"
                raise httpx.ReadError("synthetic cancel request lost", request=request)
            response = await super().handle_async_request(request)
            if cancel and loss == "after":
                loss = "none"
                await response.aread()
                await response.aclose()
                raise httpx.ReadError("synthetic cancel ACK lost", request=request)
            return response

    # Both processes advance from the same anchor, including startup and imports.
    clock = node_wall_clock(float(sys.argv[7]), float(sys.argv[8]))
    with patch("companion_voice_node.worker.time.time", side_effect=clock):
        try:
            outcome = await VoiceNodeWorker(config, transport=LostControl(verify=trust),
                supervisor_factory=lambda workspace, temporary: GatedSupervisor(
                    workspace, temporary, counter=counter)).run(once=True)
        except VoiceProtocolError as error:
            outcome = error.code
    print(json.dumps({"stage": "finished", "outcome": outcome, "pid": os.getpid()}), flush=True)


if __name__ == "__main__" and sys.argv[1:2] == ["--cancel-node"]:
    asyncio.run(independent_cancel_node())
