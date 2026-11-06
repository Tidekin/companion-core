"""One actual Core worker process with synthetic public/model transport only."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from tests.test_meme_core import CAPTION
from tests.test_meme_gallery import GalleryUpstream
from tests.test_meme_pack_files import Upstream
from tests.test_next_memes_discovery import resolver

config, now, candidate, output, *control = sys.argv[1:]
assert control in ([], ["gate"])
settings = Settings.model_validate_json(Path(config).read_text("utf-8"))
SemanticIndex.warmup = lambda self: None  # Existing fixture: never download chat index weights.
calls = []
upstream = GalleryUpstream() if settings.meme_gallery_source else Upstream(count=1)


def provider(request):
    payload = json.loads(request.content)
    caption = any(isinstance(m["content"], list) for m in payload["messages"])
    if caption:
        content = json.dumps(CAPTION, ensure_ascii=False)
    else:
        if candidate != "-":
            assert candidate in json.dumps(payload, ensure_ascii=False)
        content = "给你温暖的抱抱。" + ("⬡" + candidate + "⬡" if candidate != "-" else "")
    calls.append("caption" if caption else "chat")
    return httpx.Response(200, json={"choices": [{"message": {"content": content},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with Core(settings, ModelClient(settings, client), lambda: now) as core:
            core.memes.discovery.transport = httpx.MockTransport(upstream.handle)
            core.memes.discovery.resolver = resolver
            if control:
                print("READY", flush=True)
                assert await asyncio.to_thread(sys.stdin.readline) == "run\n"
            worked = await core.run_once("native-file-process")
            return {"worked": worked, "calls": calls, "requests": len(upstream.calls),
                    "download_bytes": (sum(map(len, upstream.bodies))
                                       if isinstance(upstream, Upstream) else
                                       len(upstream.terms) + len(upstream.image)
                                       if upstream.calls else 0),
                    "jobs": core.store.rows("SELECT kind,state,last_error_code FROM jobs")}


Path(output).write_text(json.dumps(asyncio.run(main())), encoding="utf-8")
