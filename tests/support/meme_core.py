import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager

import httpx
from PIL import Image

from companion import meme_core
from companion.core import Core
from companion.image_input import ImageInputPolicy
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from companion_meme_protocol import digest
from tests.support.meme_service import claim, discovery_environment, reservation, result, submit

CAPTION = {
    "description": "小猫张开双臂送上温暖的抱抱",
    "visible_text": "抱抱",
    "tone": "warm",
    "target": "recipient",
    "suitable": ["comfort"],
    "avoid": [],
    "use_when": ["希望得到安慰"],
    "avoid_when": [],
    "tags": ["抱抱", "温暖"],
    "confidence": 0.95,
    "content_status": "clear",
    "temporal_status": "static",
}


@asynccontextmanager
async def caption_environment(core, tmp_path, monkeypatch):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    async with discovery_environment(core, tmp_path, monkeypatch,
        meme_semantics_enabled=True, meme_caption_daily_micro_usd=50000,
        paid={"max_output_tokens": 4096, "meme_input": ImageInputPolicy(
            approval_sha256="e" * 64, estimated_tokens_per_image=100,
            max_bytes_per_image=4000000, max_pixels_per_image=16000000,
            max_images_per_request=5)}) as values:
        event, accepted, (client, config, plan, now) = values
        created = submit(core, event)
        item = claim(core)
        assert item["job_id"] == created["job_id"]
        reservation(core, item)
        result(core, item)
        yield event, accepted, client, config, plan, now, item


def body(core, event, item, now, *, data=None):
    if data is None:
        output = io.BytesIO()
        Image.new("RGB", (8, 6), "orange").save(output, format="PNG")
        data = output.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    page, asset = "https://public.example/source", "https://public.example/cat.png"
    source = {
        "schema_version": 1,
        "source_page": page,
        "asset_url": asset,
        "license": "CC0",
        "usage_basis": "Synthetic public-domain fixture",
        "fetched_at": now.isoformat(),
        "revision": "fixture-v1",
        "acquisition": {
            "source_page": page,
            "discovered_asset_url": asset,
            "revision": "fixture-v1",
            "image_sha256": sha,
            "archive_member": "",
            "fetch": {
                "requested_url": asset,
                "final_url": asset,
                "hops": [{"url": asset, "address": "8.8.8.8", "status": 200}],
                "body_sha256": sha,
                "fetched_at": now.isoformat(),
            },
        },
    }
    return {
        "asset": {
            "data_base64": base64.b64encode(data).decode(),
            "asset_sha256": sha,
            "source_evidence_sha256": digest(source),
            "mime": "image/png",
            "byte_length": len(data),
        },
        "context": {
            "event_id": event["event_id"],
            "reply_revision": 1,
            "discovery_job_id": item["job_id"],
            "attempt_key": item["attempt_key"],
        },
        "identity": meme_core.identity(core.settings).model_dump(mode="json"),
        "source_evidence": source,
    }


async def worker(core, calls, *, hook=None, content=None, tokens=True):
    async def respond(request):
        calls.append(json.loads(request.content))
        if hook:
            await hook()
        answer = {"choices": [{"message": {"content": json.dumps(content or CAPTION)}}]}
        if tokens:
            answer["usage"] = {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
        return httpx.Response(200, json=answer)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with Core(core.settings, ModelClient(core.settings, client), core.clock) as independent:
            assert independent.store is not core.store
            return await independent.run_once("caption-independent")
