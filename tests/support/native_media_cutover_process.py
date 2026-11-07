import base64
import json
import sys
from datetime import datetime

import httpx

from companion.model import ModelClient
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.live_image_input import png


async def worker(packet):
    from companion.config import Settings
    from companion.core import Core
    from companion_onebot_bridge import pending_media

    if packet["operation"] == "chat":
        settings = Settings.model_validate(packet["settings"])

        def respond(request):
            assert base64.b64encode(png()).decode() in request.content.decode()
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "我看到蓝色的图了。"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
            with Core(settings, ModelClient(settings, model), lambda: packet["clock"]) as core:
                return {"worked": await core.run_once("independent-cutover-chat")}
    cfg = BridgeConfig(**packet["config"])
    downloads = []

    def download(request):
        downloads.append(True)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def resolve(*args):
        return ["1.1.1.1"]

    transition = pending_media.transition

    def interrupted(window, event_id, state, slot, receipt=None):
        if state == "DONE":
            raise RuntimeError("synthetic interruption after Core acceptance")
        return transition(window, event_id, state, slot, receipt)

    if packet["mode"] == "upload_receipt_lost":
        pending_media.transition = interrupted
    with Spool(cfg) as spool:
        window = OwnerRuntime(spool, clock=lambda: datetime.fromisoformat(packet["clock"]))
        row = pending_media._execute(window, "SELECT * FROM automatic_media").fetchone()
        async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
            pump = pending_media.MediaPump(window, client,
                image_transport=httpx.MockTransport(download), image_resolver=resolve)
            error = None
            if row["state"] not in pending_media.TERMINAL:
                try:
                    await pump.execute(row)
                except (ValueError, httpx.HTTPError, RuntimeError) as exc:
                    error = str(exc)
            state = spool.db.execute("SELECT state FROM automatic_media").fetchone()[0]
            return {"downloads": len(downloads), "state": state, "error": error}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    print(json.dumps(run(worker(json.loads(sys.stdin.buffer.read())))))
