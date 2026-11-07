import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from companion_onebot_bridge.meme_caption import (
    EmbeddingSpec,
    canonical,
    digest,
    sample_frames,
    source_evidence,
)
from companion_onebot_bridge.meme_catalog import MemeCatalog
from companion_onebot_bridge.meme_semantic import SemanticLibrary
from tests.support.next_memes_animation import canvas_gif
from tests.support.next_memes_catalog import check, metadata, png
from tests.support.next_memes_discovery import async_check
from tests.support.next_memes_semantic import (
    CONTEXT,
    EMBEDDING,
    IDENTITY,
    MockEmbedder,
    add,
    caption,
    ready,
    step,
)
from tests.support.next_memes_semantic import (
    semantic as semantic,
)


def query(store, key="reply", **overrides):
    return store.candidates(
        key,
        {
            "text": "很难过，想要抱抱",
            "intent": "comfort",
            "target": "recipient",
            **overrides,
        },
        check=check,
    )


def test_pending_gate_cache_restart_and_exact_legal_selection(semantic, tmp_path):
    store, client = semantic
    sha = add(semantic)
    assert query(store) == []
    assert store.catalog.candidates("legacy", "comfort", check=check) == []
    assert store.catalog.prepare_selection("reply", sha, check=check) is None
    ready(semantic, sha)
    assert query(store) == []  # Empty first-round offer stays frozen after the cache fills.
    offers = query(store, "next")
    assert [row["candidate_id"] for row in offers] == [sha]
    assert "给你抱抱" in offers[0]["description"]
    assert step(semantic, sha)["cache_hit"] is True
    assert len(client.model_calls) == len(client.submissions) == 1
    db = sqlite3.connect(tmp_path / "spool.sqlite3")
    try:
        old_instance = MemeCatalog(db, store.catalog.library)
        assert old_instance.candidates("old", "comfort", check=check) == []
        image = old_instance.prepare_selection("next", sha, check=check)
        assert image.sha256 == sha and image.data == png()
        restarted = SemanticLibrary(old_instance, IDENTITY, EMBEDDING)
        result = asyncio.run(
            restarted.advance(sha, client, CONTEXT, check=async_check, commit_check=check)
        )
        assert result["cache_hit"] and result["usage"]["billed_micro_usd"] == 1250
        assert len(client.model_calls) == len(client.submissions) == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"confidence": 0.3},
        {"content_status": "uncertain"},
        {"content_status": "conflict"},
        {"visible_text": "你真笨，活该"},
        {"target": "uncertain"},
    ],
)
def test_uncertain_or_conflicting_caption_is_quarantined_without_retry(semantic, fields):
    store, client = semantic
    sha = add(semantic, **fields)
    step(semantic, sha)
    result = step(semantic, sha)
    assert result["state"] == "QUARANTINED" and query(store) == []
    assert step(semantic, sha)["cache_hit"] is True
    assert len(client.model_calls) == 1
    assert result["usage"]["billed_micro_usd"] == 1250


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "UNKNOWN"])
def test_failed_and_unknown_outcomes_never_supply_caption_or_repeat_calls(semantic, state):
    store, client = semantic
    sha = add(semantic)
    client.state = state
    step(semantic, sha)
    result = step(semantic, sha)
    assert result["state"] == "QUARANTINED" and query(store) == []
    assert step(semantic, sha)["cache_hit"] is False
    assert len(client.model_calls) == 1
    if state == "UNKNOWN":
        assert result["usage"]["state"] == "UNKNOWN"
        assert result["usage"]["billed_micro_usd"] is None


def test_invalid_model_json_keeps_actual_charge_and_stays_out_of_index(semantic):
    store, client = semantic
    sha = add(semantic)
    client.after_get = lambda response: response["result"].update(description="x" * 1001)
    step(semantic, sha)
    with pytest.raises(ValueError):
        step(semantic, sha)
    result = step(semantic, sha)
    assert result["state"] == "QUARANTINED" and not result["cache_hit"]
    assert result["usage"]["state"] == "SETTLED"
    assert result["usage"]["billed_micro_usd"] == 1250
    assert query(store) == [] and len(client.model_calls) == 1


def test_gif_last_frame_meaning_and_sample_evidence(semantic):
    store, client = semantic
    data = canvas_gif([(24, 24)] * 10)
    sampling, buffers = sample_frames(data, store.catalog.library)
    assert len(buffers) <= 5 and sampling.frames[0].frame_index == 0
    assert sampling.frames[-1].frame_index == 9 and sampling.coverage == "sampled"
    assert tuple(frame.sample_sha256 for frame in sampling.frames) == tuple(
        hashlib.sha256(raw).hexdigest() for raw in buffers
    )
    sha = add(semantic, data, temporal_status="changes", description="先安慰，末帧变成嘲笑")
    step(semantic, sha)
    assert step(semantic, sha)["state"] == "QUARANTINED"
    assert query(store) == [] and client.submissions[0].asset.data == data


def test_self_reported_complete_or_forged_sample_is_rejected(semantic):
    store, client = semantic
    sha = add(semantic, canvas_gif([(24, 24)] * 10), temporal_status="consistent")
    client.after_get = lambda response: response["result"]["sampled_frames"].update(
        coverage="complete"
    )
    step(semantic, sha)
    with pytest.raises(ValueError):
        step(semantic, sha)
    assert query(store) == []


def test_source_rebinding_reuses_model_cache_but_requires_current_core_proof(semantic):
    store, client = semantic
    sha = add(semantic)
    ready(semantic, sha)
    query(store)
    before = client.submissions[0]
    store.rebind_source(sha, replace(metadata(), fetched_at="2026-10-24T00:00:00Z"), check=check)
    assert store.catalog.prepare_selection("reply", sha, check=check) is None
    result = step(semantic, sha)
    after = client.submissions[-1]
    assert result["state"] == "READY" and result["cache_hit"] is True
    assert before.cache_key == after.cache_key and before.sha256 != after.sha256
    assert before.asset.source_evidence_sha256 != after.asset.source_evidence_sha256
    assert len(client.model_calls) == 1 and len(client.submissions) == 2
    assert result["usage"]["usage_request_id"] == "usage_" + before.cache_key
    assert query(store, "new")


def test_deleted_source_or_changed_bytes_invalidates_frozen_offer_and_old_catalog(semantic):
    store, _ = semantic
    sha = add(semantic)
    ready(semantic, sha)
    query(store)
    assert store.catalog.prepare_selection("reply", sha, check=check)
    store.catalog.remove(sha)
    assert query(store, "after") == []
    assert store.catalog.prepare_selection("reply", sha, check=check) is None
    with pytest.raises(ValueError, match="SEND_FENCED"):
        store.catalog.mark_sending("reply", check=check)
    with pytest.raises(ValueError):
        step(semantic, sha)
    store.catalog.add(png(), metadata(), check=check)
    assert query(store, "rediscovered") == []


@pytest.mark.parametrize("action", ["remove", "source", "stop"])
def test_caption_await_fence_retains_usage_but_never_commits_stale_result(semantic, action):
    store, client = semantic
    sha = add(semantic)
    stopped = False

    def change(response):
        nonlocal stopped
        if action == "remove":
            store.catalog.remove(sha)
        elif action == "source":
            store.invalidate(sha)
        else:
            stopped = True

    async def guard():
        if stopped:
            raise ValueError("STOPPED")

    client.after_get = change
    step(semantic, sha)
    with pytest.raises(ValueError):
        asyncio.run(store.advance(sha, client, CONTEXT, check=guard, commit_check=check))
    receipt = json.loads(
        store.db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()[0]
    )
    assert receipt["state"] == "SETTLED" and receipt["billed_micro_usd"] == 1250
    assert query(store) == [] and len(client.model_calls) == 1


def test_incremental_index_version_and_local_query_vector(semantic):
    store, client = semantic
    sha = add(semantic)
    ready(semantic, sha)
    embedder = MockEmbedder()
    for _ in range(2):
        assert asyncio.run(store.index(sha, embedder, check=async_check, commit_check=check))
    assert len(embedder.calls) == 1
    vector = asyncio.run(embedder.embed(("温暖鼓励",)))
    assert (
        store.candidates(
            "vector",
            {"text": "温暖鼓励", "intent": "comfort", "target": "recipient"},
            query_vector=vector,
            check=check,
        )[0]["candidate_id"]
        == sha
    )
    new_spec = EmbeddingSpec(model_version="new-local-index-v2", dimensions=512)
    changed = SemanticLibrary(store.catalog, IDENTITY, new_spec)
    assert query(changed, "lexical")  # A stale vector does not disable valid caption fallback.
    assert (
        changed.candidates(
            "no_vector",
            {"text": "温暖鼓励", "intent": "comfort", "target": "recipient"},
            check=check,
        )
        == []
    )
    with pytest.raises(ValueError, match="VERSION_MISMATCH"):
        changed.candidates(
            "wrong",
            {"text": "温暖鼓励", "intent": "comfort", "target": "recipient"},
            query_vector=vector,
            check=check,
        )
    assert asyncio.run(
        changed.advance(sha, client, CONTEXT, check=async_check, commit_check=check)
    )["cache_hit"]
    assert len(client.model_calls) == 1
    with pytest.raises(ValueError, match="PROFILE_CHANGED"):
        query(store, "old_instance")


@pytest.mark.parametrize("change", ["model", "prompt"])
def test_caption_identity_change_blocks_old_caption_and_requires_new_version(semantic, change):
    store, client = semantic
    sha = add(semantic)
    ready(semantic, sha)
    query(store)
    identity = IDENTITY.model_copy(
        update={
            "model_version" if change == "model" else "prompt_version": "revision-2",
        }
    )
    changed = SemanticLibrary(store.catalog, identity, EMBEDDING)
    assert query(changed, "fresh") == []
    assert store.catalog.prepare_selection("reply", sha, check=check) is None
    assert (
        asyncio.run(changed.advance(sha, client, CONTEXT, check=async_check, commit_check=check))[
            "state"
        ]
        == "PENDING"
    )
    assert (
        asyncio.run(changed.advance(sha, client, CONTEXT, check=async_check, commit_check=check))[
            "state"
        ]
        == "READY"
    )
    assert len(client.model_calls) == 2


@pytest.mark.parametrize("mode", ["dimension", "nan", "remote", "revoke"])
def test_bad_or_late_embedding_never_enters_index(semantic, mode):
    store, _ = semantic
    sha = add(semantic)
    ready(semantic, sha)
    embedder = MockEmbedder()

    def alter(response):
        if mode == "dimension":
            response["dimensions"] = 384
        elif mode == "nan":
            response["vectors"][0][0] = float("nan")
        elif mode == "remote":
            response["local"] = False
        else:
            store.invalidate(sha)

    embedder.after = alter
    with pytest.raises(ValueError):
        asyncio.run(store.index(sha, embedder, check=async_check, commit_check=check))
    assert store.db.execute("SELECT COUNT(*) FROM meme_semantic_vectors").fetchone()[0] == 0


def test_source_canonicalization_excludes_semantic_text_and_rejects_duplicate_keys():
    first = metadata(acquisition_json='{"b":2,"a":1}')
    second = replace(first, description="不同的上游描述", acquisition_json='{"a":1,"b":2}')
    assert digest(source_evidence(first)) == digest(source_evidence(second))
    with pytest.raises(ValueError, match="DUPLICATE"):
        source_evidence(replace(first, acquisition_json='{"a":1,"a":2}'))
    with pytest.raises(ValueError, match="NONFINITE"):
        source_evidence(replace(first, acquisition_json='{"a":NaN}'))
    assert '"schema_version":1' in canonical(source_evidence(first))


def test_discovery_queues_source_approved_image_until_caption_ready(semantic):
    from tests.support.next_memes_discovery import DurableBudget, discover

    store, client = semantic
    calls = []
    result = discover((store.catalog, DurableBudget(store.db)), calls)
    assert result["accepted"] == [] and len(result["pending_caption"]) == 1
    assert result["usage"]["storage_bytes"] == len(png())
    sha = result["pending_caption"][0]
    assert not client.model_calls and query(store) == []
    ready(semantic, sha)
    assert query(store, "after-caption")[0]["candidate_id"] == sha
    assert len(calls) == 3 and len(client.model_calls) == 1


def test_unknown_cost_is_not_fabricated_as_zero_or_lost_when_caption_succeeds(semantic):
    store, client = semantic
    sha = add(semantic)
    client.after_get = lambda response: response["usage"].update(
        state="UNKNOWN", billed_micro_usd=None
    )
    ready(semantic, sha)
    result = step(semantic, sha)
    assert result["cache_hit"] and result["usage"]["state"] == "UNKNOWN"
    assert result["usage"]["input_tokens"] == 120
    assert result["usage"]["output_tokens"] == 80
    assert result["usage"]["billed_micro_usd"] is None
    assert len(client.model_calls) == 1


def test_unknown_job_reconciliation_does_not_submit_another_model_request(semantic):
    store, client = semantic
    sha = add(semantic)
    client.state = "UNKNOWN"
    step(semantic, sha)
    assert step(semantic, sha)["state"] == "QUARANTINED"
    request = client.submissions[0]
    job_id = client.polls[0]
    sampling, _ = sample_frames(request.asset.data, store.catalog.library)
    response = client.envelope(
        request,
        job_id,
        "SUCCEEDED",
        {
            **caption(),
            "sampled_frames": sampling.model_dump(mode="json"),
        },
        {
            "usage_request_id": "usage_" + request.cache_key,
            "state": "SETTLED",
            "input_tokens": 120,
            "output_tokens": 80,
            "billed_micro_usd": 1250,
        },
    )
    result = asyncio.run(store.accept(request, response, check=async_check, commit_check=check))
    assert result["state"] == "READY" and result["usage"]["billed_micro_usd"] == 1250
    assert len(client.model_calls) == len(client.submissions) == 1
    assert (
        asyncio.run(store.accept(request, response, check=async_check, commit_check=check))["state"]
        == "READY"
    )


def test_submission_timeout_holds_unknown_and_never_resubmits(semantic):
    store, client = semantic
    sha = add(semantic)

    async def lost(request):
        client.submissions.append(request)
        raise TimeoutError("submit ACK lost")

    client.submit = lost
    with pytest.raises(TimeoutError):
        step(semantic, sha)
    result = step(semantic, sha)
    assert result["state"] == "QUARANTINED" and result["usage"] is None
    assert not result["cache_hit"] and len(client.submissions) == 1


def test_core_running_status_is_polled_without_resubmission(semantic):
    store, client = semantic
    sha = add(semantic)

    async def running(job_id):
        client.polls.append(job_id)
        request = client.jobs[job_id]
        return client.envelope(
            request,
            job_id,
            "RUNNING",
            None,
            {
                "usage_request_id": "usage_" + request.cache_key,
                "state": "RESERVED",
                "input_tokens": None,
                "output_tokens": None,
                "billed_micro_usd": None,
            },
        )

    client.get = running
    for _ in range(3):
        assert step(semantic, sha)["state"] == "PENDING"
    assert len(client.submissions) == 1 and len(client.polls) == 2 and not client.model_calls


@pytest.mark.parametrize("field", ["asset_sha256", "source_evidence_sha256", "request_sha256"])
def test_wrong_outcome_identity_cannot_populate_cache(semantic, field):
    store, client = semantic
    sha = add(semantic)
    client.after_get = lambda response: response.update({field: "0" * 64})
    step(semantic, sha)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        step(semantic, sha)
    assert query(store) == []
    assert store.db.execute("SELECT COUNT(*) FROM meme_caption_cache").fetchone()[0] == 0


def test_changed_stored_bytes_and_new_source_during_embedding_block_old_indices(semantic):
    store, _ = semantic
    sha = add(semantic)
    ready(semantic, sha)
    query(store)
    with store.catalog.atomic():
        store.db.execute("UPDATE meme_assets SET data=? WHERE sha=?", (png("black"), sha))
    assert query(store, "mutated") == []
    assert store.catalog.prepare_selection("reply", sha, check=check) is None


def test_source_gate_and_caption_limits_remain_bounded(semantic):
    store, client = semantic
    store.catalog.max_assets = 1
    sha = add(semantic)
    with pytest.raises(ValueError, match="STORAGE_LIMIT"):
        add(semantic, png("red"))
    store.catalog.max_bindings = 1
    step(semantic, sha)
    store.rebind_source(sha, replace(metadata(), fetched_at="2026-10-24T00:00:00Z"), check=check)
    with pytest.raises(ValueError, match="CAPTION_BINDING_LIMIT"):
        step(semantic, sha)
    assert len(client.submissions) == 1


@pytest.mark.parametrize("state", ["QUEUED", "CANCELLED", "FAILED"])
def test_before_admission_outcome_keeps_usage_null(semantic, state):
    store, client = semantic
    sha = add(semantic)

    async def no_admission(request):
        client.submissions.append(request)
        return client.envelope(request, "job_" + "a" * 32, state, None, None)

    client.submit = no_admission
    result = step(semantic, sha)
    assert result["usage"] is None
    assert result["state"] == ("PENDING" if state == "QUEUED" else "QUARANTINED")
    assert store.db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()[0] is None


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_fresh_discovery_replaces_terminal_binding_only_after_confirmed_no_usage(semantic, state):
    store, client = semantic
    sha = add(semantic)

    async def no_admission(request):
        client.submissions.append(request)
        job_id = "job_" + str(len(client.submissions)) * 32
        return client.envelope(request, job_id, state, None, None)

    client.submit = no_admission
    assert step(semantic, sha)["state"] == "QUARANTINED"
    old = tuple(store.db.execute("SELECT * FROM meme_caption_bindings").fetchone())
    store.catalog.add(png(), metadata(), check=check)
    assert step(semantic, sha)["state"] == "QUARANTINED"
    assert len(client.submissions) == 1  # Reopening cannot repeat the same operation.
    store.catalog.add(png(), metadata(), check=check)
    replacement = CONTEXT.model_copy(update={
        "event_id": "evt_" + "3" * 64,
        "discovery_job_id": "job_" + "4" * 32,
        "attempt_key": "replacement-discovery",
    })
    asyncio.run(store.advance(sha, client, replacement, check=async_check, commit_check=check))
    assert [request.context for request in client.submissions] == [CONTEXT, replacement]
    assert tuple(store.db.execute(
        "SELECT * FROM meme_caption_bindings WHERE request_sha=?", (old[0],),
    ).fetchone()) == old


@pytest.mark.parametrize("outcome", [
    "CANCELLED", "FAILED", "UNKNOWN", "RUNNING", "submission_timeout", "low_confidence",
    "malformed_paid",
])
def test_fresh_context_cannot_repeat_uncertain_or_used_caption(semantic, outcome):
    store, client = semantic
    sha = add(semantic)

    async def submit(request):
        client.submissions.append(request)
        if outcome == "submission_timeout":
            raise TimeoutError("submission outcome unknown")
        client.jobs["job_" + "a" * 32] = request
        return client.envelope(request, "job_" + "a" * 32, "QUEUED", None, None)

    async def poll(job_id):
        client.polls.append(job_id)
        request = client.jobs[job_id]
        usage_state = {"RUNNING": "RESERVED", "UNKNOWN": "UNKNOWN"}.get(outcome, "SETTLED")
        usage = {
            "usage_request_id": "actual-request-with-zero-charge",
            "state": usage_state,
            "input_tokens": 0 if usage_state == "SETTLED" else None,
            "output_tokens": 0 if usage_state == "SETTLED" else None,
            "billed_micro_usd": 0 if usage_state == "SETTLED" else None,
        }
        result = None
        state = outcome
        if outcome in {"low_confidence", "malformed_paid"}:
            state = "SUCCEEDED"
            sampling, _ = sample_frames(request.asset.data, client.library)
            result = caption(confidence=0.1)
            result["sampled_frames"] = sampling.model_dump(mode="json")
            if outcome == "malformed_paid":
                result["tone"] = "invalid-tone"
        return client.envelope(request, job_id, state, result, usage)

    client.submit, client.get = submit, poll
    if outcome == "submission_timeout":
        with pytest.raises(TimeoutError):
            step(semantic, sha)
    else:
        assert step(semantic, sha)["state"] == "PENDING"
        if outcome == "malformed_paid":
            with pytest.raises(ValueError):
                step(semantic, sha)
        else:
            step(semantic, sha)
    stored = store.db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()[0]
    store.catalog.add(png(), metadata(), check=check)
    replacement = CONTEXT.model_copy(update={
        "event_id": "evt_" + "3" * 64,
        "discovery_job_id": "job_" + "4" * 32,
        "attempt_key": "replacement-discovery",
    })
    result = asyncio.run(store.advance(
        sha, client, replacement, check=async_check, commit_check=check,
    ))
    assert result["state"] == ("PENDING" if outcome == "RUNNING" else "QUARANTINED")
    assert len(client.submissions) == 1
    assert store.db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()[0] == stored


@pytest.mark.parametrize("state", ["RUNNING", "SUCCEEDED", "UNKNOWN", "CANCELLED"])
def test_admitted_job_cannot_discard_usage_receipt(semantic, state):
    store, client = semantic
    sha = add(semantic)
    step(semantic, sha)
    client.after_get = lambda response: response.update(state=state, usage=None)
    with pytest.raises(ValueError, match="USAGE_REQUIRED"):
        step(semantic, sha)
    receipt = json.loads(
        store.db.execute("SELECT usage_json FROM meme_caption_bindings").fetchone()[0]
    )
    assert receipt["state"] == "RESERVED" and receipt["billed_micro_usd"] is None
    assert query(store) == []


def test_source_rebind_timeout_cannot_promote_old_cache_without_current_proof(semantic):
    store, client = semantic
    sha = add(semantic)
    ready(semantic, sha)
    store.rebind_source(sha, replace(metadata(), fetched_at="2026-10-25T00:00:00Z"), check=check)

    async def timeout(request):
        client.submissions.append(request)
        raise TimeoutError("new source binding not confirmed")

    client.submit = timeout
    with pytest.raises(TimeoutError):
        step(semantic, sha)
    result = step(semantic, sha)
    assert result["state"] == "QUARANTINED" and not result["cache_hit"]
    assert query(store) == [] and len(client.model_calls) == 1


def test_returning_to_a_verified_model_version_reuses_exact_existing_binding(semantic):
    store, client = semantic
    sha = add(semantic)
    ready(semantic, sha)
    changed = SemanticLibrary(
        store.catalog, IDENTITY.model_copy(update={"model_version": "v2"}), EMBEDDING
    )
    ready((changed, client), sha)
    restored = SemanticLibrary(store.catalog, IDENTITY, EMBEDDING)
    result = step((restored, client), sha)
    assert result["state"] == "READY" and result["cache_hit"]
    assert len(client.model_calls) == len(client.submissions) == 2
    assert query(restored)


def test_model_schema_cannot_author_sampling_or_usage_evidence():
    from companion_onebot_bridge.meme_caption import CaptionResult, caption_model_schema

    model_schema = caption_model_schema()
    assert "sampled_frames" not in model_schema["properties"]
    assert "sampled_frames" not in model_schema["required"]
    assert not {"usage", "source_evidence_sha256", "coverage"} & set(model_schema["properties"])
    assert "sampled_frames" in CaptionResult.model_json_schema()["required"]
