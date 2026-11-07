import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.meme_semantic_http import BASE
from tests.support.meme_runtime import RULE, CoreHTTP, public_transport
from tests.support.native_meme import capture
from tests.support.next_memes_discovery import resolver


@pytest.fixture
def make_runtime(tmp_path, core_factory, monkeypatch):
    from contextlib import ExitStack

    from companion import meme_core
    from companion.retrieval import SemanticIndex
    from tests.support.native_meme import NativeCase

    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    monkeypatch.setattr(meme_core, "encoder", lambda *args, **kwargs: SimpleNamespace(
        query_embed=lambda text: [[1.0] + [0.0] * 511],
        passage_embed=lambda texts: [[1.0] + [0.0] * 511 for _ in texts]))
    with ExitStack() as resources:
        def make(*, discoveries=1):
            core = resources.enter_context(core_factory(tmp_path))
            core.settings.meme_discovery_daily_download_bytes = discoveries * 16 * 1024 * 1024
            core.settings.meme_discovery_daily_storage_bytes = discoveries * 4 * 1024 * 1024
            fixture = NativeCase(core, tmp_path, rules=(RULE,), semantic=True)
            resources.callback(fixture.spool.close)
            fixture.spool.db.row_factory = sqlite3.Row
            calls = []
            runtime = MemeRuntime(fixture.owner,
                transport=public_transport(calls, semantic=False), resolver=resolver)
            fixture.owner._memes = runtime
            return runtime, capture(runtime, text="我需要抱抱，想要小猫陪伴"), calls
        yield make


class SemanticCore(CoreHTTP):
    """Real caption/query/offer endpoints; only external model output is synthetic."""

    def __init__(self, runtime, event, **kwargs):
        from companion import meme_core

        super().__init__(runtime, event, **kwargs)
        self.caption = SimpleNamespace(submissions=[], polls=[], model_calls=[],
                                       state="SUCCEEDED", after_get=None)
        self.processed = set()
        self.semantic_calls = []
        self.query_started, self.query_release = asyncio.Event(), None
        self.query_change = None
        self.reject_offer = False
        self.identity = self.original_identity = meme_core.identity(self.core.settings)
        self.embedding = meme_core.SPEC

    async def worker(self, job_id):
        from companion.core import Core
        from companion.model import ModelClient
        from tests.support.meme_core import CAPTION

        def answer(request):
            self.caption.model_calls.append(json.loads(request.content))
            value = {"choices": [{"message": {"content": json.dumps(CAPTION)}}]}
            if self.caption.state != "UNKNOWN":
                value["usage"] = {"prompt_tokens": 1050, "completion_tokens": 100,
                                  "total_tokens": 1150}
            return httpx.Response(200, json=value)

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as provider:
            with Core(self.core.settings, ModelClient(self.core.settings, provider),
                      self.core.clock) as independent:
                assert independent.store is not self.core.store
                assert await independent.run_once("native-meme-caption-fixture")
        self.processed.add(job_id)

    async def request(self, request):
        await super().request(request)
        path = request.url.path
        if path.startswith(BASE + "/captions/"):
            job_id = path.rsplit("/", 1)[1]
            self.caption.polls.append(job_id)
            if job_id not in self.processed:
                await self.worker(job_id)
        if path == BASE + "/query":
            # The foreground-query tests inspect the pre-generation phase. The
            # discovery tests above independently used completed foreground work.
            self.core.store.execute((
                "UPDATE jobs SET state='QUEUED' WHERE kind='chat' AND "
                "state='SUCCEEDED' AND event_id=%s"
            ),
                (json.loads(request.content)["event_id"],))
            self.query_started.set()
            if self.query_release:
                await self.query_release.wait()
        if path == BASE + "/offers" and self.reject_offer:
            self.core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat'")

    async def response(self, response):
        path = response.request.url.path
        if path.startswith(BASE + "/discovery/"):
            return await super().response(response)
        if not path.startswith(BASE + "/"):
            return
        await response.aread()
        body = json.loads(response.request.content)
        self.semantic_calls.append((path, body))
        if response.status_code != 200:
            return
        value = response.json()
        if path == BASE + "/semantics/profile":
            # Explicit response-version injection exercises restart/profile
            # validation while real Core still authorizes the original purpose.
            value.update(identity=self.identity.model_dump(mode="json"),
                         embedding=self.embedding.model_dump(mode="json"))
        elif path == BASE + "/captions":
            self.caption.submissions.append(body)
        elif path.startswith(BASE + "/captions/") and self.caption.after_get:
            self.caption.after_get(value)
        elif path == BASE + "/query" and self.query_change:
            self.query_change(value)
        response._content = json.dumps(value).encode()
