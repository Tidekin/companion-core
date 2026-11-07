import asyncio
import hashlib
import sqlite3

import pytest

from companion_onebot_bridge.meme_caption import (
    CaptionContext,
    CaptionIdentity,
    EmbeddingSpec,
    sample_frames,
)
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_library import MemeLibrary
from companion_onebot_bridge.meme_semantic import SemanticLibrary
from tests.support.next_memes_catalog import check, metadata, png
from tests.support.next_memes_discovery import async_check

IDENTITY = CaptionIdentity(model_version="mock-vision-revision-1")


EMBEDDING = EmbeddingSpec(
    model_version="BAAI/bge-small-zh-v1.5@46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
    dimensions=512,
)


CONTEXT = CaptionContext(
    event_id="evt_" + "1" * 64,
    reply_revision=1,
    discovery_job_id="job_" + "2" * 32,
    attempt_key="caption-attempt-1",
)


def caption(**overrides):
    result = {
        "description": "小猫伸手抱抱，温柔地安慰接收者",
        "visible_text": "给你抱抱",
        "tone": "warm",
        "target": "recipient",
        "suitable": ["comfort"],
        "avoid": [],
        "use_when": ["需要温柔陪伴"],
        "avoid_when": ["严肃事务"],
        "tags": ["抱抱", "安慰", "陪伴"],
        "confidence": 0.95,
        "content_status": "clear",
        "temporal_status": "static",
    }
    return result | overrides


class MockCoreCaptionClient:
    """Simulates Core's asynchronous admission, sampling and content/version cache.

    These outputs are authored fixtures, not measurements of a real vision model.
    """

    def __init__(self, library):
        self.library = library
        self.submissions, self.polls, self.model_calls = [], [], []
        self.jobs, self.cache, self.results = {}, {}, {}
        self.after_get = None
        self.state = "SUCCEEDED"

    def envelope(self, request, job_id, state, result, usage, *, cache_hit=False):
        return {
            "schema_version": 1,
            "job_id": job_id,
            "state": state,
            "asset_sha256": request.asset.asset_sha256,
            "source_evidence_sha256": request.asset.source_evidence_sha256,
            "request_sha256": request.sha256,
            "caption_model_version": request.identity.model_version,
            "prompt_version": request.identity.prompt_version,
            "result": result,
            "usage": usage,
            "cache_hit": cache_hit,
        }

    async def submit(self, request):
        assert request.asset.asset_sha256 == hashlib.sha256(request.asset.data).hexdigest()
        assert request.asset.byte_length == len(request.asset.data)
        self.submissions.append(request)
        job_id = "job_" + hashlib.sha256(request.sha256.encode()).hexdigest()[:32]
        self.jobs[job_id] = request
        if request.cache_key in self.cache:
            result, usage = self.cache[request.cache_key]
            return self.envelope(request, job_id, "SUCCEEDED", result, usage, cache_hit=True)
        return self.envelope(
            request,
            job_id,
            "QUEUED",
            None,
            {
                "usage_request_id": "usage_" + request.cache_key,
                "state": "RESERVED",
                "input_tokens": None,
                "output_tokens": None,
                "billed_micro_usd": None,
            },
        )

    async def get(self, job_id):
        self.polls.append(job_id)
        request = self.jobs[job_id]
        self.model_calls.append(request.cache_key)
        sampling, _ = sample_frames(request.asset.data, self.library)
        result = dict(self.results.get(request.asset.asset_sha256, caption()))
        result["sampled_frames"] = sampling.model_dump(mode="json")
        usage = {
            "usage_request_id": "usage_" + request.cache_key,
            "state": "UNKNOWN" if self.state == "UNKNOWN" else "SETTLED",
            "input_tokens": None if self.state == "UNKNOWN" else 120,
            "output_tokens": None if self.state == "UNKNOWN" else 80,
            "billed_micro_usd": None if self.state == "UNKNOWN" else 1250,
        }
        if self.state == "SUCCEEDED":
            self.cache[request.cache_key] = result, usage
        response = self.envelope(
            request, job_id, self.state, result if self.state == "SUCCEEDED" else None, usage
        )
        if self.after_get:
            self.after_get(response)
        return response


class MockEmbedder:
    def __init__(self, spec=EMBEDDING):
        self.spec, self.calls, self.after = spec, [], None

    async def embed(self, texts):
        assert 1 <= len(texts) <= 8 and all(len(text) <= 2000 for text in texts)
        self.calls.append(texts)
        vector = [1.0] + [0.0] * (self.spec.dimensions - 1)
        response = {
            "model_version": self.spec.model_version,
            "dimensions": self.spec.dimensions,
            "vectors": [vector for _ in texts],
            "local": True,
        }
        if self.after:
            self.after(response)
        return response


@pytest.fixture
def semantic(tmp_path):
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    catalog = MemeCatalog(db, MemeLibrary(tmp_path, enabled=True))
    semantic = SemanticLibrary(catalog, IDENTITY, EMBEDDING)
    client = MockCoreCaptionClient(catalog.library)
    yield semantic, client
    db.close()


def add(semantic, data=None, **caption_fields):
    store, client = semantic
    sha = store.catalog.add(data or png(), metadata(), check=check)
    client.results[sha] = caption(**caption_fields)
    return sha


def step(semantic, sha, **kwargs):
    store, client = semantic
    return asyncio.run(
        store.advance(sha, client, CONTEXT, check=async_check, commit_check=check, **kwargs)
    )


def ready(semantic, sha):
    assert step(semantic, sha)["state"] == "PENDING"
    assert step(semantic, sha)["state"] == "READY"
