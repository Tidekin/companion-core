"""Ranker policy tests use synthetic resolved facts; native authority is tested separately."""

from types import SimpleNamespace

import pytest

from companion import meme_index as index
from companion import meme_retrieval
from companion.contracts import CoreError
from companion.meme_retrieval import OFFER
from companion.store import encode
from tests.conftest import run
from tests.support.meme_core_runtime import runtime_case as runtime_case
from tests.support.meme_index import local_encoder, vector


@pytest.mark.parametrize("runtime_case", ["no_match"], indirect=True)
def test_no_intent_keeps_strict_null_offer_when_scope_exhausts_optional_budget(
    runtime_case, monkeypatch
):
    c, elapsed, checks = runtime_case, [0.0], []
    original_scope = meme_retrieval.MemeRetrieval._scope

    def checked_scope(self, job, *, executing):
        result = original_scope(self, job, executing=executing)
        checks.append(job["job_id"])
        elapsed[0] = 3.0
        return result

    monkeypatch.setattr(meme_retrieval, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(meme_retrieval.MemeRetrieval, "_scope", checked_scope)
    monkeypatch.setattr(
        index, "encoder", lambda *args, **kwargs: pytest.fail("no-intent query inference")
    )
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    assert len(checks) >= 3 and saved["offer"] is None
    assert saved["ranking"]["query_vector_base64"] is None
    assert saved["ranking"]["selected"] == {}
    header = saved["ranking"]["header"]
    assert header["query_fallback_reason"] is None
    assert {
        key: header[key] for key in ("scanned", "eligible", "cache_hits", "cold_batch", "unindexed")
    } == {
        "scanned": 0,
        "eligible": 0,
        "cache_hits": 0,
        "cold_batch": 0,
        "unindexed": 0,
    }


def test_partial_index_batches_32_and_never_reranks_after_later_cache_fill(
    runtime_case, monkeypatch
):
    c = runtime_case
    original = c.retrieval._resolve(c.asset_id, c.caption_id)
    binding, caption, meta, roots, image = original
    # Forty deterministic, already-resolved source facts exercise the bounded ranker.
    # This seam does not claim to create forty native source/fee receipts.
    sources = {}
    for i in range(40):
        current = binding.model_copy(
            update={
                "asset_id": f"{i + 1:064x}",
                "asset_sha256": f"{100 - i:064x}",
                "caption_job_id": "job_" + f"{i + 1:032x}",
            }
        )
        sources[current.asset_id] = (current, caption, meta, roots, image)
    store_rows = c.core.store.rows

    def rows(sql, params=()):
        if sql.startswith("SELECT a.asset_id, a.asset_sha256, src.caption_job_id"):
            return [
                {
                    "asset_id": b.asset_id,
                    "asset_sha256": b.asset_sha256,
                    "caption_job_id": b.caption_job_id,
                }
                for b, *_ in sources.values()
            ]
        return store_rows(sql, params)

    from companion.meme_retrieval import MemeRetrieval

    monkeypatch.setattr(c.core.store, "rows", rows)
    monkeypatch.setattr(MemeRetrieval, "_resolve", lambda self, asset, job, **_: sources[asset])
    cache, writes = {}, []
    monkeypatch.setattr(index, "cached", lambda store, identity: cache.get(identity.key))

    def persist(store, identity, encoded):
        writes.append(identity.key)
        cache[identity.key] = encoded

    monkeypatch.setattr(index, "persist", persist)
    calls = local_encoder(monkeypatch)
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    head = saved["ranking"]["header"]
    assert (head["eligible"], head["cache_hits"], head["cold_batch"], head["unindexed"]) == (
        40,
        0,
        32,
        8,
    )
    assert head["mode"] == "HYBRID" and len(writes) == 32 and len(calls) == 1
    assert head["profile"]["coverage_policy"] == "per-candidate-bounded-v2"
    selected = [v["candidate_id"] for v in saved["offer"]["candidates"]]
    # Equal semantic scores use physical SHA ascending within the indexed batch.
    assert selected == sorted(b.asset_sha256 for b, *_ in list(sources.values())[:32])[:8]
    for current, *_ in sources.values():
        cache[index.CacheIdentity.source(current).key] = index.pack(vector())
    run(c.service.prepare_offer(c.job))
    assert c.retrieval.check(c.job) == saved and len(calls) == 1
    # A separate synthetic first generation sees hot coverage, with the same tie rule.
    c.core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (OFFER + c.job["event_id"],))
    run(c.service.prepare_offer(c.job))
    hot = c.retrieval.check(c.job)
    assert hot["ranking"]["header"]["mode"] == "SEMANTIC"
    assert hot["ranking"]["header"]["cache_hits"] == 40
    assert hot["ranking"]["header"]["cold_batch"] == 0
    assert [v["candidate_id"] for v in hot["offer"]["candidates"]] == sorted(
        b.asset_sha256 for b, *_ in sources.values()
    )[:8]
    hot["offer"]["candidates"].reverse()
    c.core.store.execute(
        'UPDATE runtime_meta SET value=%s WHERE key=%s', (encode(hot), OFFER + c.job["event_id"])
    )
    with pytest.raises(CoreError, match="MEME_OFFER_BINDING_INVALID"):
        c.retrieval.check(c.job)


@pytest.mark.parametrize("runtime_case", ["no_match"], indirect=True)
@pytest.mark.parametrize("field", ["scanned", "query", "fallback"])
def test_no_intent_empty_proof_cannot_claim_query_inference(runtime_case, monkeypatch, field):
    c = runtime_case
    monkeypatch.setattr(
        index, "encoder", lambda *args, **kwargs: pytest.fail("unexpected inference")
    )
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    assert saved["offer"] is None and saved["ranking"]["header"]["query_fallback_reason"] is None
    header = saved["ranking"]["header"]
    if field == "scanned":
        header["scanned"] = 1
    elif field == "fallback":
        header["query_fallback_reason"] = "LOCAL_DEADLINE"
    else:
        vector_bytes = index.pack(vector())
        saved["ranking"]["query_vector_base64"] = index.vector_text(vector_bytes)
        header["query_vector_sha256"] = index.vector_sha(vector_bytes)
        header["mode"] = "SEMANTIC"
    c.core.store.execute(
        'UPDATE runtime_meta SET value=%s WHERE key=%s', (encode(saved), OFFER + c.job["event_id"])
    )
    with pytest.raises(CoreError, match="MEME_OFFER_BINDING_INVALID"):
        c.retrieval.check(c.job)


def test_unindexed_selected_candidate_records_actual_batch_limit(runtime_case, monkeypatch):
    c = runtime_case
    original = c.retrieval._resolve(c.asset_id, c.caption_id)
    binding, caption, meta, roots, image = original
    # All 33 sources are lexical matches. Orthogonal passage vectors contribute no cosine;
    # the final source wins the physical-SHA tie and retains an explicit lexical proof.
    sources = {}
    for i in range(33):
        current = binding.model_copy(
            update={
                "asset_id": f"{i + 1:064x}",
                "asset_sha256": f"{100 - i:064x}",
                "caption_job_id": "job_" + f"{i + 1:032x}",
            }
        )
        sources[current.asset_id] = (current, caption, meta, roots, image)
    original_rows = c.core.store.rows

    def rows(sql, params=()):
        if sql.startswith("SELECT a.asset_id, a.asset_sha256, src.caption_job_id"):
            return [
                {
                    "asset_id": b.asset_id,
                    "asset_sha256": b.asset_sha256,
                    "caption_job_id": b.caption_job_id,
                }
                for b, *_ in sources.values()
            ]
        return original_rows(sql, params)

    from companion.meme_retrieval import MemeRetrieval
    from tests.support.meme_index import Encoder

    class Orthogonal(Encoder):
        def passage_embed(self, texts, *, batch_size):
            return (vector(0, 1) for _ in texts)

    monkeypatch.setattr(c.core.store, "rows", rows)
    monkeypatch.setattr(MemeRetrieval, "_resolve", lambda self, asset, job, **_: sources[asset])
    monkeypatch.setattr(index, "cached", lambda *args: None)
    monkeypatch.setattr(index, "persist", lambda *args: None)
    local_encoder(monkeypatch, Orthogonal)
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    first = saved["offer"]["candidates"][0]["candidate_id"]
    proof = saved["ranking"]["selected"][first]
    assert proof["basis"] == "LEXICAL" and proof["reason"] == "BATCH_LIMIT"
    assert proof["vector_base64"] is proof["cache_key"] is proof["cosine"] is None
    assert saved["ranking"]["header"]["unindexed"] == 1
