"""Original result reads survive execution fences without admitting new work."""

import json
from dataclasses import replace

import httpx
import pytest

from companion import owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.meme_semantic_http import BASE, CaptionHTTP, SemanticHTTP
from tests.conftest import run
from tests.support.meme_semantic_runtime import SemanticCore
from tests.support.meme_semantic_runtime import make_runtime as make_runtime


async def submitted(runtime, core, client):
    assert (await runtime.run_once(client))["status"] == "completed"
    assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
    row = runtime.db.execute("SELECT sha,context_json,job_id FROM meme_caption_bindings").fetchone()
    request = runtime.semantics._request(row[0], json.loads(row[1]))
    await core.worker(row[2])
    return row[2], request


@pytest.mark.parametrize("fence", ["stop", "unknown", "restart_unknown"])
def test_original_usage_readback_survives_stop_unknown_and_restart(make_runtime, fence):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)
    if fence != "stop":
        core.caption.state = "UNKNOWN"

    async def scenario():
        async with core.client() as client:
            job_id, request = await submitted(runtime, core, client)
            if fence == "stop":
                runtime.window.stop()
                owner_authority.stop(core.core.store, core.core.settings, core.core.clock,
                    AuthorityStop(operation_id="stop-caption-readback",
                                  authority=runtime.authority))
            active = MemeRuntime(runtime.window) if fence == "restart_unknown" else runtime
            assert (await active.run_once(client))["status"] == "CAPTION_QUARANTINED"
            row = runtime.db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()
            usage = json.loads(row[0])
            assert usage["state"] == ("SETTLED" if fence == "stop" else "UNKNOWN")
            assert usage["billed_micro_usd"] == (1250 if fence == "stop" else None)
            assert runtime.db.execute("SELECT COUNT(*) FROM meme_caption_cache").fetchone()[0] == 0
            assert runtime.db.execute(
                "SELECT COUNT(*) FROM meme_semantic_vectors").fetchone()[0] == 0
            http = SemanticHTTP(runtime.window, client, check=runtime._owner_check)
            adapter = CaptionHTTP(http, runtime.catalog)
            assert (await adapter.get(job_id))["usage"] == usage
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                await adapter.submit(request)
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                await http.profile()
            assert len(core.caption.submissions) == len(core.caption.model_calls) == 1
            assert len(core.core.store.rows("SELECT * FROM model_usage")) == 1
            assert core.core.store.rows("SELECT * FROM outbox") == []

    run(scenario())


@pytest.mark.parametrize("change", ["unknown_job", "body", "path", "config", "job_id",
                                    "asset_sha256", "request_sha256", "retained_job"])
def test_result_readback_rejects_foreign_job_changed_request_and_receipt(make_runtime, change):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            job_id, _ = await submitted(runtime, core, client)
            before = tuple(runtime.db.execute("SELECT * FROM meme_caption_bindings").fetchone())
            adapter = CaptionHTTP(SemanticHTTP(runtime.window, client,
                check=runtime._owner_check), runtime.catalog)
            foreign = "job_" + "f" * 32
            if change == "retained_job":
                with runtime.db:
                    runtime.db.execute("UPDATE meme_caption_bindings SET job_id=?", (foreign,))
                job_id = foreign
            elif change == "unknown_job":
                job_id = foreign

            async def alter_request(request):
                if request.url.path.startswith(BASE + "/captions/"):
                    if change == "body":
                        request._content = b'{"retry":true}'
                    elif change == "path":
                        request.url = request.url.copy_with(path=BASE + "/captions/" + foreign)
                    elif change == "config":
                        runtime.window.config = replace(runtime.config, bridge_token="x" * 32)
                        request.headers["Authorization"] = "Bearer " + "x" * 32

            async def alter_response(response):
                if (response.request.url.path.startswith(BASE + "/captions/")
                    and change in {"job_id", "asset_sha256", "request_sha256"}):
                    value = response.json()
                    value[change] = foreign if change == "job_id" else "f" * 64
                    response._content = json.dumps(value).encode()

            client.event_hooks["request"].append(alter_request)
            client.event_hooks["response"].append(alter_response)
            with pytest.raises(ValueError, match="MEME_CAPTION_"):
                await adapter.get(job_id)
            after = tuple(runtime.db.execute("SELECT * FROM meme_caption_bindings").fetchone())
            assert after == (before[:5] + (foreign,) + before[6:] if change == "retained_job"
                             else before)
            assert len(core.caption.model_calls) == 1

    run(scenario())


def test_cancelled_unstarted_caption_result_is_closed_once_without_usage(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            assert (await runtime.run_once(client))["status"] == "completed"
            assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
            job_id = runtime.db.execute("SELECT job_id FROM meme_caption_bindings").fetchone()[0]
            runtime.window.stop()
            owner_authority.stop(core.core.store, core.core.settings, core.core.clock,
                AuthorityStop(operation_id="stop-before-caption", authority=runtime.authority))
            # Prevent the test harness from starting a deliberately cancelled worker
            # when observing this result-only request.
            core.processed.add(job_id)
            assert (await runtime.run_once(client))["status"] == "CAPTION_QUARANTINED"
            row = runtime.db.execute(
                "SELECT state,usage_json,outcome_json FROM meme_caption_bindings").fetchone()
            assert row[0] == "CANCELLED" and row[1] is None
            assert json.loads(row[2])["state"] == "CANCELLED"
            reads = len(core.caption.polls)
            assert (await runtime.run_once(client))["status"] == "CANCELLED"
            assert len(core.caption.polls) == reads == 1
            assert core.caption.model_calls == []
            assert core.core.store.rows("SELECT * FROM model_usage") == []

    run(scenario())
