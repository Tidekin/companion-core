"""Real Bridge wiring with public fixtures and Core HTTP transport substitutes."""

import asyncio
import json
import sqlite3

import httpx
import pytest

from companion_onebot_bridge.meme_caption import (
    CaptionContext,
    source_evidence,
)
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.meme_semantic_http import BASE, CaptionHTTP, SemanticHTTP
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.meme_semantic_runtime import SemanticCore
from tests.support.meme_semantic_runtime import make_runtime as make_runtime
from tests.support.native_meme import CASES, capture, case, native_item, recall
from tests.support.next_memes_discovery import resolver


async def admit(runtime, event, client):
    while await runtime.window.flush_one(client):
        pass
    assert runtime.db.execute("SELECT state FROM inbox WHERE key=?",
                              (event["event_id"],)).fetchone()[0] == "accepted"


@pytest.mark.parametrize("cost_unknown", [False, True])
def test_source_only_download_caption_index_and_bound_post_inbox_offer(make_runtime, cost_unknown):
    runtime, event, public_calls = make_runtime()
    core = SemanticCore(runtime, event)
    core.caption.state = "UNKNOWN" if cost_unknown else "SUCCEEDED"
    assert runtime.prepare_offer(event) is None
    assert runtime.db.execute("SELECT COUNT(*) FROM meme_runtime_offers").fetchone()[0] == 0

    async def scenario():
        async with core.client() as client:
            downloaded = await runtime.run_once(client)
            assert downloaded["status"] == "completed"
            assert downloaded["accepted"] == [] and downloaded["pending_caption"]
            sha = downloaded["pending_caption"][0]
            assert runtime.catalog.candidates("old-mode", "comfort", check=lambda: None) == []
            assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
            assert (await runtime.run_once(client))["status"] == "CAPTION_READY"
            assert (await runtime.run_once(client))["status"] == "SEMANTIC_READY"
            assert (await runtime.run_once(client))["status"] == "IDLE"
            assert (await runtime.semantic_offer(client, event["event_id"], reply_revision=1)) == {
                "status": "OFFER_ACCEPTED",
            }
            assert (await runtime.semantic_offer(client, event["event_id"], reply_revision=1)) == {
                "status": "ALREADY_ATTEMPTED",
            }
            return sha

    sha = run(scenario())
    assert public_calls and len(core.caption.submissions) == len(core.caption.model_calls) == 1
    assert core.core.store.one("SELECT state FROM model_usage")["state"] == (
        "UNKNOWN" if cost_unknown else "SETTLED")
    usage = json.loads(runtime.db.execute(
        "SELECT usage_json FROM meme_caption_bindings").fetchone()[0])
    assert usage["state"] == ("UNKNOWN" if cost_unknown else "SETTLED")
    submitted = next(body for path, body in core.semantic_calls if path == BASE + "/captions")
    stored = runtime.db.execute("SELECT metadata FROM meme_assets WHERE sha=?", (sha,)).fetchone()
    assert submitted["source_evidence"] == source_evidence(runtime.catalog.metadata(stored[0]))
    assert submitted["source_evidence"]["acquisition"]
    assert submitted["context"]["discovery_job_id"] == core.claim["job_id"]
    offered = next(body for path, body in core.semantic_calls if path == BASE + "/offers")
    assert [candidate["candidate_id"] for candidate in offered["offer"]["candidates"]] == [sha]
    saved = runtime.db.execute(
        "SELECT query_binding FROM meme_runtime_semantic_offers"
    ).fetchone()[0]
    assert "小猫" not in saved and "vectors" not in saved
    assert runtime.db.execute("SELECT COUNT(*) FROM meme_runtime_offers").fetchone()[0] == 0
    restarted = MemeRuntime(runtime.window)
    selected = restarted.prepare(native_item(runtime, event, sha), check=lambda: None)
    assert selected.sha256 == sha


def test_lost_settlement_ack_restart_then_expired_download_lease_can_caption(make_runtime):
    runtime, event, calls = make_runtime()
    core = SemanticCore(runtime, event, lose_results=2)

    async def scenario():
        async with core.client() as client:
            assert (await runtime.run_once(client))["status"] == "RESULT_PENDING"
            assert core.caption.submissions == []
            count = len(calls)
            restarted = MemeRuntime(runtime.window, transport=runtime.transport, resolver=resolver)
            assert (await restarted.run_once(client))["status"] == "RESULT_RECONCILED"
            case(runtime).core.clock.advance(240)
            assert (await restarted.run_once(client))["status"] == "CAPTION_PENDING"
            assert len(calls) == count
            assert len(core.caption.submissions) == 1

    run(scenario())
    assert all(body == core.results[0] for body in core.results)


def test_new_discovery_indexes_ready_caption_with_current_turn_and_original_paid_job(make_runtime):
    runtime, first, public_calls = make_runtime(discoveries=2)
    core = SemanticCore(runtime, first)

    async def scenario():
        async with core.client() as client:
            downloaded = await runtime.run_once(client)
            sha = downloaded["pending_caption"][0]
            assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
            assert (await runtime.run_once(client))["status"] == "CAPTION_READY"
            assert runtime.db.execute(
                "SELECT COUNT(*) FROM meme_semantic_vectors",
            ).fetchone()[0] == 0
            original = dict(runtime.db.execute("SELECT * FROM meme_caption_bindings").fetchone())
            cached = dict(runtime.db.execute("SELECT * FROM meme_caption_cache").fetchone())

            second = capture(runtime, 43, text="我还想要抱抱")
            await core.provision(client, second, revision=2)
            rediscovered = await runtime.run_once(client)
            assert rediscovered["status"] == "completed" and rediscovered["accepted"] == [sha]
            assert (await runtime.run_once(client))["status"] == "SEMANTIC_READY"
            assert (await runtime.run_once(client))["status"] == "IDLE"
            embeddings = [body for path, body in core.semantic_calls
                          if path.endswith("/embeddings")]
            assert embeddings == [{
                "job_id": original["job_id"], "event_id": second["event_id"], "reply_revision": 2,
            }]
            assert runtime.db.execute(
                "SELECT COUNT(*) FROM meme_semantic_vectors",
            ).fetchone()[0] == 1
            assert dict(runtime.db.execute("SELECT * FROM meme_caption_bindings").fetchone()) == (
                original
            )
            assert dict(runtime.db.execute("SELECT * FROM meme_caption_cache").fetchone()) == cached
            assert [row[0] for row in runtime.db.execute(
                "SELECT state FROM meme_runtime_sources ORDER BY rowid",
            )] == ["CANCELLED", "INDEXED"]
            assert (await runtime.semantic_offer(client, second["event_id"], reply_revision=2)) == {
                "status": "OFFER_ACCEPTED",
            }
            offered = next(body for path, body in core.semantic_calls if path.endswith("/offers"))
            assert [candidate["candidate_id"] for candidate in offered["offer"]["candidates"]] == [
                sha,
            ]

    run(scenario())
    assert len(core.caption.submissions) == len(core.caption.model_calls) == 1
    assert len(public_calls) == 10


def test_unknown_download_never_submits_paid_caption(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event, unknown=True)

    async def scenario():
        async with core.client() as client:
            assert (await runtime.run_once(client))["status"] == "UNKNOWN"
            for _ in range(3):
                assert (await runtime.run_once(client))["status"] == "IDLE"

    run(scenario())
    assert core.caption.submissions == []


def test_late_caption_receipt_preserves_usage_after_stop(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            await runtime.run_once(client)
            await runtime.run_once(client)
            core.caption.after_get = lambda _: runtime.window.stop()
            assert (await runtime.run_once(client))["status"] == "CANCELLED"

    run(scenario())
    row = runtime.db.execute("SELECT state,usage_json FROM meme_caption_bindings").fetchone()
    assert row[0] == "FAILED" and json.loads(row[1])["billed_micro_usd"] == 1250
    assert runtime.db.execute("SELECT COUNT(*) FROM meme_caption_cache").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["revision", "job", "hash", "query_extra", "target"])
def test_query_receipt_binding_rejected(make_runtime, change):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    def corrupt(value):
        if change == "revision":
            value["reply_revision"] += 1
        elif change == "job":
            value["job_id"] = "not-a-job"
        elif change == "hash":
            value["query_sha256"] = "unbound"
        elif change == "query_extra":
            value["query"]["extra"] = "unexpected"
        else:
            value["query"]["target"] = "anybody"

    core.query_change = corrupt

    async def scenario():
        async with core.client() as client:
            with pytest.raises(ValueError, match="MEME_QUERY_RECEIPT_INVALID"):
                await runtime.semantic_offer(client, event["event_id"], reply_revision=1)

    run(scenario())
    assert not any(path.endswith("/offers") for path, _ in core.semantic_calls)


@pytest.mark.parametrize("erased", [False, True])
def test_foreground_query_starts_without_ack_and_never_blocks_delivery(make_runtime, erased):
    from companion_onebot_bridge.owner_memes import MemePump

    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            if erased:
                with runtime.db:
                    runtime.db.execute("UPDATE inbox SET state='erased'")
            core.query_release = asyncio.Event()
            pump = MemePump(runtime.window, client)
            pump.admitted(event["event_id"])
            if erased:
                assert pump.query_task is None
            else:
                await asyncio.wait_for(core.query_started.wait(), 1)
                assert pump.query_task is not None and not pump.query_task.done()
                assert pump.task is None or pump.task.done()
                pump.tick(idle=False)
                assert not pump.query_task.cancelling()
                pump.interrupt()
            await pump.close()

    run(scenario())
    assert not any(path.endswith("/offers") for path, _ in core.semantic_calls)


def test_new_input_cancels_old_query_and_new_offer_can_proceed(make_runtime):
    from companion_onebot_bridge.owner_memes import MemePump

    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            core.query_release = asyncio.Event()
            pump = MemePump(runtime.window, client)
            pump.admitted(event["event_id"])
            await asyncio.wait_for(core.query_started.wait(), 1)
            previous = pump.query_task
            newer = capture(runtime, 43)
            await admit(runtime, newer, client)
            core.query_release = None
            pump.admitted(newer["event_id"])
            await pump.query_task
            assert previous.cancelled()
            await pump.close()
            return newer["event_id"]

    current = run(scenario())
    offers = [body for path, body in core.semantic_calls if path.endswith("/offers")]
    assert len(offers) == 1 and offers[0]["event_id"] == current
    assert runtime.missing_offer(current)["candidates"] == []


def test_caption_adapter_checks_original_bytes_before_dispatch(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            downloaded = await runtime.run_once(client)
            sha = downloaded["pending_caption"][0]
            context = CaptionContext(
                event_id=event["event_id"], reply_revision=1,
                discovery_job_id=core.claim["job_id"], attempt_key=core.claim["attempt_key"],
            )
            request = runtime.semantics._request(sha, context)
            with runtime.db:
                runtime.db.execute("UPDATE meme_assets SET data=? WHERE sha=?", (b"changed", sha))
            adapter = CaptionHTTP(SemanticHTTP(runtime.window, client, check=lambda: None),
                                  runtime.catalog)
            with pytest.raises(ValueError, match="MEME_CAPTION_ASSET_CHANGED"):
                await adapter.submit(request)

    run(scenario())
    assert core.caption.submissions == []


def test_lost_caption_response_never_repeats_submission_or_enters_index(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)
    core.caption.state = "UNKNOWN"
    core.caption.missing_response = True

    async def scenario():
        async with core.client() as client:
            await runtime.run_once(client)
            assert (await runtime.run_once(client))["status"] == "CAPTION_PENDING"
            assert (await runtime.run_once(client))["status"] == "CAPTION_QUARANTINED"
            binding = runtime.db.execute(
                "SELECT state,usage_json FROM meme_caption_bindings").fetchone()
            assert binding[0] == "UNKNOWN" and json.loads(binding[1])["state"] == "UNKNOWN"
            assert runtime.db.execute("SELECT state FROM meme_semantic_assets").fetchone()[0] == (
                "QUARANTINED")
            await runtime.run_once(client)
            await runtime.run_once(client)
            assert runtime.db.execute("SELECT state FROM meme_semantic_assets").fetchone()[0] == (
                "QUARANTINED")

    run(scenario())
    assert len(core.caption.submissions) == len(core.caption.polls) == 1
    assert not any(path.endswith("/embeddings") for path, _ in core.semantic_calls)
    usage = json.loads(runtime.db.execute("SELECT usage_json FROM meme_caption_bindings")
                       .fetchone()[0])
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] is None


@pytest.mark.parametrize("late", ["recall", "new_input", "offer", "timeout"])
def test_late_query_is_optional_and_cannot_commit_stale_candidates(make_runtime, late):
    from companion_onebot_bridge.owner_memes import MemePump

    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    def change(_):
        if late == "recall":
            recall(runtime)
        elif late == "new_input":
            capture(runtime, 43)

    core.query_change = change
    core.reject_offer = late == "offer"

    async def scenario():
        async with core.client() as client:
            if late == "timeout":
                core.query_release = asyncio.Event()
            pump = MemePump(runtime.window, client)
            pump.admitted(event["event_id"])
            await pump.query_task
            assert pump.task is None or pump.task.done()
            await pump.close()

    run(scenario())
    assert runtime.db.execute(
        "SELECT state FROM meme_runtime_semantic_offers"
    ).fetchone()[0] == "DEFERRED"
    if late != "offer":
        assert not any(path.endswith("/offers") for path, _ in core.semantic_calls)


def test_restart_does_not_requery_uncertain_foreground_request(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            core.reject_offer = True
            with pytest.raises(httpx.HTTPStatusError):
                await runtime.semantic_offer(client, event["event_id"], reply_revision=1)
            before = len(core.semantic_calls)
            restarted = MemeRuntime(runtime.window)
            assert (await restarted.semantic_offer(client, event["event_id"], reply_revision=1))[
                "status"
            ] == "ALREADY_ATTEMPTED"
            assert len(core.semantic_calls) == before

    run(scenario())


def test_core_profile_must_supply_prompt_version_before_public_io(make_runtime):
    runtime, event, calls = make_runtime()
    core = SemanticCore(runtime, event)

    async def omit_prompt(response):
        if response.request.url.path == BASE + "/semantics/profile":
            value = response.json()
            del value["identity"]["prompt_version"]
            response._content = json.dumps(value).encode()

    async def scenario():
        async with core.client() as client:
            client.event_hooks["response"].append(omit_prompt)
            assert (await runtime.run_once(client))["status"] == "FAILED"

    run(scenario())
    assert calls == [] and runtime.semantics is None


@pytest.mark.parametrize("new_authority", [False, True])
def test_reopened_spool_refreshes_current_model_prompt_and_embedding_once(
    make_runtime, new_authority,
):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            await runtime._ensure_semantics(client, runtime._owner_check)
            fixture = case(runtime)
            cfg = runtime.config
            if new_authority:
                cfg = await fixture.handoff(runtime, client)
            runtime.spool.close()
            core.identity = core.original_identity.model_copy(update={
                "model_version": "approved-model-v2", "prompt_version": "approved-prompt-v2"})
            core.embedding = core.embedding.model_copy(update={
                "model_version": "local-embedding-v2"})
            with Spool(cfg) as spool:
                spool.db.row_factory = sqlite3.Row
                owner = OwnerRuntime(spool, clock=lambda: fixture.core.clock.now)
                CASES[owner] = fixture
                restarted = MemeRuntime(owner)
                assert restarted.semantics.identity == core.original_identity
                first = await restarted._ensure_semantics(client, restarted._owner_check)
                second = await restarted._ensure_semantics(client, restarted._owner_check)
                assert first is second and first.identity == core.identity
                assert first.embedding_spec == core.embedding
                saved = json.loads(spool.db.execute(
                    "SELECT value FROM meme_semantic_config").fetchone()[0])
                assert saved["caption"] == core.identity.model_dump(mode="json")
                assert saved["embedding"] == core.embedding.model_dump(mode="json")

    run(scenario())
    assert [path for path, _ in core.semantic_calls] == [BASE + "/semantics/profile"] * 2


def test_concurrent_new_work_shares_one_authoritative_profile_refresh(make_runtime):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            await runtime._ensure_semantics(client, runtime._owner_check)
            restarted = MemeRuntime(runtime.window)
            started, release, requests = asyncio.Event(), asyncio.Event(), []

            async def delay_profile(response):
                if response.request.url.path == BASE + "/semantics/profile":
                    requests.append(response.request.url.path)
                    started.set()
                    await release.wait()

            client.event_hooks["response"].append(delay_profile)
            first = asyncio.create_task(restarted._ensure_semantics(
                client, restarted._owner_check))
            await asyncio.wait_for(started.wait(), 5)
            second_started = asyncio.Event()

            async def concurrent_work():
                second_started.set()
                return await restarted._ensure_semantics(client, restarted._owner_check)

            second = asyncio.create_task(concurrent_work())
            await second_started.wait()
            release.set()
            refreshed = await asyncio.gather(first, second)
            assert refreshed[0] is refreshed[1]
            await restarted._ensure_semantics(client, restarted._owner_check)
            assert requests == [BASE + "/semantics/profile"]

    run(scenario())


@pytest.mark.parametrize("failure", ["http", "schema", "cancelled"])
def test_failed_refresh_preserves_saved_profile_and_can_retry(make_runtime, failure):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)

    async def scenario():
        async with core.client() as client:
            await runtime._ensure_semantics(client, runtime._owner_check)
            restarted = MemeRuntime(runtime.window)
            requests = []
            updated = core.original_identity.model_copy(update={
                "model_version": "newly-approved-model"})
            core.identity = updated

            async def fail_profile(response):
                if response.request.url.path != BASE + "/semantics/profile":
                    return
                requests.append(response.request.url.path)
                if len(requests) == 1:
                    if failure == "http":
                        response.status_code = 503
                    elif failure == "cancelled":
                        raise asyncio.CancelledError()
                    else:
                        response._content = b'{"identity":{},"embedding":{}}'

            client.event_hooks["response"].append(fail_profile)
            expected = {"http": httpx.HTTPStatusError, "schema": ValueError,
                        "cancelled": asyncio.CancelledError}[failure]
            with pytest.raises(expected):
                await restarted._ensure_semantics(client, restarted._owner_check)
            assert restarted.semantics.identity == core.original_identity
            assert (await restarted._ensure_semantics(
                client, restarted._owner_check)).identity == updated
            await restarted._ensure_semantics(client, restarted._owner_check)
            assert requests == [BASE + "/semantics/profile"] * 2

    run(scenario())


def test_profile_refresh_after_restart_keeps_unknown_caption_receipt_without_resubmission(
    make_runtime,
):
    runtime, event, _ = make_runtime()
    core = SemanticCore(runtime, event)
    core.caption.state = "UNKNOWN"
    core.caption.missing_response = True

    async def scenario():
        async with core.client() as client:
            await runtime.run_once(client)
            await runtime.run_once(client)
            assert (await runtime.run_once(client))["status"] == "CAPTION_QUARANTINED"
            assert json.loads(runtime.db.execute(
                "SELECT usage_json FROM meme_caption_bindings").fetchone()[0])["state"] == "UNKNOWN"
            original = tuple(runtime.db.execute("SELECT * FROM meme_caption_bindings").fetchone())
            core.identity = core.original_identity.model_copy(update={
                "model_version": "approved-model-v2"})
            core.embedding = core.embedding.model_copy(update={
                "model_version": "local-embedding-v2"})
            cfg, fixture = runtime.config, case(runtime)
            runtime.spool.close()
            with Spool(cfg) as spool:
                spool.db.row_factory = sqlite3.Row
                owner = OwnerRuntime(spool, clock=lambda: fixture.core.clock.now)
                CASES[owner] = fixture
                restarted = MemeRuntime(owner)
                await restarted.run_once(client)
                await restarted.run_once(client)
                assert restarted.semantics.identity == core.identity
                assert restarted.semantics.embedding_spec == core.embedding
                assert tuple(spool.db.execute(
                    "SELECT * FROM meme_caption_bindings").fetchone()) == original

    run(scenario())
    assert len(core.caption.submissions) == len(core.caption.polls) == 1
    assert not any(path.endswith("/embeddings") for path, _ in core.semantic_calls)
    assert len([path for path, _ in core.semantic_calls if path.endswith("/profile")]) == 2
