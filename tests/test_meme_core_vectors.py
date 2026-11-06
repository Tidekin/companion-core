"""Native source/fee authority around the rebuildable, optional vector projection."""

import asyncio
import json
import threading
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion import erasure, meme_assets, owner_authority
from companion import meme_index as index
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.meme_retrieval import OFFER
from companion.owner_authority_protocol import AuthorityStop
from companion.privacy import advance_epoch
from companion.store import Store, encode
from companion_meme_protocol import digest
from tests.conftest import run
from tests.test_meme_core_runtime import runtime_case as runtime_case
from tests.test_meme_index import Encoder, local_encoder, vector


def freeze(c, monkeypatch):
    calls = local_encoder(monkeypatch)
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    return saved, calls


def drop_offer(c):
    c.core.store.execute("DELETE FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],))


def test_cold_hot_and_deleted_cache_preserve_one_frozen_arithmetic_proof(runtime_case, monkeypatch):
    c, before = runtime_case, runtime_case.core.store.rows("SELECT * FROM model_usage")
    saved, calls = freeze(c, monkeypatch)
    header = saved["ranking"]["header"]
    assert (header["mode"], header["cache_hits"], header["cold_batch"], header["unindexed"]) == (
        "SEMANTIC",
        0,
        1,
        0,
    )
    cache = c.core.store.one("SELECT * FROM meme_vectors")
    assert len(cache["vector"]) == 2048 and cache["cache_key"]
    assert len(calls) == 1 and saved["binding_version"] == 2
    c.core.store.execute("DELETE FROM meme_vectors")

    def forbidden(*args, **kwargs):
        raise AssertionError("A frozen choice tried to infer or rerank")

    monkeypatch.setattr(index, "encoder", forbidden)
    run(c.service.prepare_offer(c.job))
    assert c.retrieval.check(c.job) == saved
    assert c.core.store.rows("SELECT * FROM model_usage") == before
    assert not c.core.store.rows("SELECT * FROM meme_vectors")
    # A separate synthetic first generation can reuse the exact derived row.
    c.core.store.insert("meme_vectors", **cache)
    drop_offer(c)
    monkeypatch.setattr(index, "encoder", lambda *args, **kwargs: Encoder())
    run(c.service.prepare_offer(c.job))
    hot = c.retrieval.check(c.job)
    assert (hot["ranking"]["header"]["cache_hits"], hot["ranking"]["header"]["cold_batch"]) == (
        1,
        0,
    )
    assert hot["offer"] == saved["offer"]


def test_semantic_candidate_with_zero_lexical_overlap_still_obeys_context(
    runtime_case, monkeypatch
):
    c = runtime_case
    caption = json.loads(
        c.core.store.one("SELECT result_json FROM meme_captions WHERE job_id=?", (c.caption_id,))[
            "result_json"
        ]
    )
    caption.update(description="柔软云朵", visible_text="", tags=["绒球"], use_when=["陪伴时刻"])
    c.core.store.execute(
        "UPDATE meme_captions SET result_json=? WHERE job_id=?", (encode(caption), c.caption_id)
    )
    saved, _ = freeze(c, monkeypatch)
    proof = next(iter(saved["ranking"]["selected"].values()))
    assert proof["lexical_score"] == 0.0 and proof["score"] == pytest.approx(1.0)
    assert proof["basis"] == "SEMANTIC"
    assert len(saved["offer"]["candidates"]) == 1


@pytest.mark.parametrize("corruption", ["model", "dimension", "checksum", "nan", "zero", "length"])
def test_bad_derived_cache_is_precise_miss_then_rebuilt(runtime_case, monkeypatch, corruption):
    c = runtime_case
    freeze(c, monkeypatch)
    row = c.core.store.one("SELECT * FROM meme_vectors")
    field, bad = {
        "model": ("model_version", "old-model"),
        "dimension": ("dimensions", 1),
        "checksum": ("vector_sha256", "f" * 64),
        "nan": ("vector", b"\x00\x00\xc0\x7f" * 512),
        "zero": ("vector", b"\x00" * 2048),
        "length": ("vector", b"\x00"),
    }[corruption]
    c.core.store.execute("PRAGMA ignore_check_constraints=ON")
    c.core.store.execute(f"UPDATE meme_vectors SET {field}=?", (bad,))
    c.core.store.execute("PRAGMA ignore_check_constraints=OFF")
    drop_offer(c)
    saved, calls = freeze(c, monkeypatch)
    assert saved["ranking"]["header"]["cache_hits"] == 0 and len(calls) == 1
    assert c.core.store.one("SELECT * FROM meme_vectors") == row


@pytest.mark.parametrize("runtime_case", ["two_sources"], indirect=True)
def test_same_bytes_cannot_borrow_another_licensed_sources_vector(runtime_case, monkeypatch):
    c = runtime_case
    assets = c.core.store.rows("SELECT asset_id FROM meme_assets ORDER BY asset_id")
    other = c.core.store.one(
        "SELECT s.caption_job_id FROM meme_asset_sources s "
        "JOIN meme_assets a ON a.asset_id=s.asset_id "
        "JOIN meme_captions c ON c.job_id=s.caption_job_id "
        "WHERE s.asset_id=? AND c.asset_sha256=a.asset_sha256 "
        "AND c.source_sha256=a.source_sha256 ORDER BY s.caption_job_id",
        (assets[1]["asset_id"],),
    )
    binding, *_ = c.retrieval._resolve(assets[1]["asset_id"], other["caption_job_id"])
    with c.core.store.transaction():
        index.persist(c.core.store, index.CacheIdentity.source(binding), index.pack(vector()))

    def missing(*args, **kwargs):
        raise FileNotFoundError("SYNTHETIC_NO_LOCAL_WEIGHTS")

    monkeypatch.setattr(index, "encoder", missing)
    run(c.service.prepare_offer(c.job))
    saved = c.retrieval.check(c.job)
    proof = next(iter(saved["ranking"]["selected"].values()))
    assert proof["asset_id"] == assets[0]["asset_id"] and proof["basis"] == "LEXICAL"
    assert proof["reason"] == "LOCAL_WEIGHTS_MISSING"
    assert saved["ranking"]["header"]["cache_hits"] == 0


@pytest.mark.parametrize(
    "change", ["epoch", "erasure", "usage", "caption", "stop", "lease", "revision"]
)
def test_after_await_current_facts_fence_cache_and_offer(runtime_case, monkeypatch, change):
    c, release = runtime_case, threading.Event()
    before = c.core.store.rows("SELECT * FROM model_usage")
    corrected = []

    async def scenario():
        loop, started = asyncio.get_running_loop(), asyncio.Event()

        class Pending(Encoder):
            def query_embed(self, query):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                return super().query_embed(query)

        local_encoder(monkeypatch, Pending)
        request = asyncio.create_task(c.service.prepare_offer(c.job))
        await asyncio.wait_for(started.wait(), 2)
        assert not c.core.store.db.in_transaction
        try:
            if change == "epoch":
                advance_epoch(c.core.store)
            elif change == "erasure":
                with c.core.store.transaction():
                    c.core.ledger.expire_events(
                        c.core.store, [c.source["event_id"]], c.core.clock()
                    )
            elif change == "usage":
                c.core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
            elif change == "caption":
                row = c.core.store.one(
                    "SELECT result_json FROM meme_captions WHERE job_id=?", (c.caption_id,)
                )
                result = json.loads(row["result_json"])
                c.core.store.execute(
                    "UPDATE meme_captions SET result_json=? WHERE job_id=?",
                    (encode({**result, "description": "修正的温暖抱抱"}), c.caption_id),
                )
                binding, *_ = c.retrieval._resolve(c.asset_id, c.caption_id)
                with c.core.store.transaction():
                    index.persist(
                        c.core.store, index.CacheIdentity.source(binding), index.pack(vector(0, 1))
                    )
                corrected.extend(c.core.store.rows("SELECT * FROM meme_vectors"))
            elif change == "stop":
                owner_authority.stop(
                    c.core.store,
                    c.core.settings,
                    c.core.clock,
                    AuthorityStop(operation_id="stop-pending-vector", authority=c.grant.authority),
                )
            elif change == "lease":
                c.core.store.execute(
                    "UPDATE jobs SET lease_token=NULL WHERE job_id=?", (c.job["job_id"],)
                )
            else:
                c.core.store.execute(
                    "UPDATE conversations SET reply_revision=reply_revision+1 "
                    "WHERE conversation_id=?",
                    (c.job["conversation_id"],),
                )
        finally:
            release.set()
        if change == "caption":
            await request  # An item not offered yet is left out, never re-captioned in place.
            return
        with pytest.raises(CoreError):
            await request

    run(scenario())
    assert c.core.store.rows("SELECT * FROM meme_vectors") == corrected
    if change == "caption":
        saved = c.retrieval.check(c.job)
        assert saved["offer"]["candidates"] == [] and saved["catalog_bindings"] == {}
        assert saved["ranking"]["selected"] == {}
    else:
        assert not c.core.store.one(
            "SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],)
        )
    if change != "usage":
        assert c.core.store.rows("SELECT * FROM model_usage") == before


@pytest.mark.parametrize(
    "field",
    [
        "score",
        "cosine",
        "vector",
        "profile",
        "bool",
        "extra",
        "missing_version",
        "ranking_size",
        "outer_size",
    ],
)
def test_corrupt_frozen_proof_is_fenced_even_with_rehashed_binding(
    runtime_case, monkeypatch, field
):
    c = runtime_case
    saved, _ = freeze(c, monkeypatch)
    sha = next(iter(saved["catalog_bindings"]))
    proof, header = saved["ranking"]["selected"][sha], saved["ranking"]["header"]
    if field in {"score", "cosine"}:
        proof[field] -= 0.1
    elif field == "vector":
        proof["vector_base64"] = index.vector_text(index.pack(vector(0, 1)))
        proof["vector_sha256"] = index.vector_sha(index.pack(vector(0, 1)))
    elif field == "profile":
        header["profile"]["cold_batch_limit"] = 33
        header["profile_sha256"] = digest(header["profile"])
    elif field == "bool":
        header["scanned"] = True
    elif field == "extra":
        proof["unrecognized"] = 1
    elif field == "missing_version":
        del proof["version"]
    elif field == "ranking_size":
        header["padding"] = "x" * index.RANKING_BYTES
    else:
        saved["padding"] = "x" * index.OFFER_BYTES
    saved["catalog_bindings"][sha]["ranking_sha256"] = index.ranking_sha(header, proof)
    c.core.store.execute(
        "UPDATE runtime_meta SET value=? WHERE key=?", (encode(saved), OFFER + c.job["event_id"])
    )
    with pytest.raises(CoreError):
        c.retrieval.check(c.job)
    assert not c.core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("record", [None, {}, {"binding_version": 1}, {"binding_version": 3}])
def test_legacy_and_opaque_records_remain_explicit_fences(runtime_case, record, monkeypatch):
    c = runtime_case
    original = encode(record)
    c.core.store.insert("runtime_meta", key=OFFER + c.job["event_id"], value=original)
    monkeypatch.setattr(index, "encoder", lambda *args, **kwargs: pytest.fail("refroze old offer"))
    with pytest.raises(CoreError):
        run(c.service.prepare_offer(c.job))
    assert (
        c.core.store.one(
            "SELECT value FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],)
        )["value"]
        == original
    )


def test_erasure_rollback_and_fsynced_replay_remove_vectors_and_whole_proof(
    runtime_case, monkeypatch
):
    c = runtime_case
    saved, _ = freeze(c, monkeypatch)
    cache = c.core.store.rows("SELECT * FROM meme_vectors")
    before = c.core.store.rows("SELECT * FROM model_usage")
    original = meme_assets.erase

    def fail(store, event_ids):
        original(store, event_ids)
        raise RuntimeError("SYNTHETIC_CACHE_ERASURE_FAILURE")

    monkeypatch.setattr(meme_assets, "erase", fail)
    with pytest.raises(RuntimeError, match="SYNTHETIC_CACHE_ERASURE_FAILURE"):
        with c.core.store.transaction():
            c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
    assert (
        json.loads(
            c.core.store.one(
                "SELECT value FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],)
            )["value"]
        )
        == saved
    )
    with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
        c.retrieval.check(c.job)
    assert c.core.store.rows("SELECT * FROM meme_vectors") == cache
    monkeypatch.setattr(meme_assets, "erase", original)
    c.core.ledger.replay(c.core.store)
    assert not c.core.store.rows("SELECT * FROM meme_vectors")
    assert not c.core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],)
    )
    assert c.core.store.rows("SELECT * FROM model_usage") == before


def test_revision_two_complete_repairs_only_original_derived_proof(runtime_case, monkeypatch):
    c = runtime_case
    saved, _ = freeze(c, monkeypatch)
    cache = c.core.store.rows("SELECT * FROM meme_vectors")
    with c.core.store.transaction():
        c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
    for row in cache:
        c.core.store.insert("meme_vectors", **row)
    c.core.store.insert("runtime_meta", key=OFFER + c.job["event_id"], value=encode(saved))
    # Exact old revision signature, rather than deleting the marker in the migration.
    with monkeypatch.context() as patch:
        patch.setattr(erasure, "CONTRACT_REVISION", 2)
        old_signature = erasure.contract_signature()
    c.core.store.execute(
        "UPDATE runtime_meta SET value=? WHERE key=?", (old_signature, erasure.CONTRACT_KEY)
    )
    independent = "meme_offer:synthetic-later-unrelated"
    c.core.store.insert("runtime_meta", key=independent, value='{"keep":"later"}')
    c.core.ledger.replay(c.core.store)
    assert not c.core.store.rows("SELECT * FROM meme_vectors")
    assert c.core.store.one("SELECT value FROM runtime_meta WHERE key=?", (independent,)) == {
        "value": '{"keep":"later"}'
    }
    assert not c.core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],)
    )


def test_actual_pre0048_encrypted_snapshot_migrates_before_latest_ledger_replay(
    runtime_case, monkeypatch, tmp_path
):
    c = runtime_case
    freeze(c, monkeypatch)
    before = c.core.store.rows("SELECT * FROM model_usage")
    legacy = tmp_path / "pre0048.sqlite3"
    from tests.test_test_fixtures import historical_database

    historical_database(c.core.store, legacy, "0047_meme_assets")
    archive, restored, key = (
        tmp_path / "pre0048.enc",
        tmp_path / "restored.sqlite3",
        Fernet.generate_key(),
    )
    backup(str(legacy), archive, key)
    with c.core.store.transaction():
        c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
    restore(archive, restored, key, c.core.settings.erasure_ledger)
    with closing(Store(str(restored))) as store:
        assert (
            store.one("SELECT version_num FROM alembic_version")["version_num"]
            == "0068_image_review_target"
        )
        assert not store.rows("SELECT * FROM meme_vectors")
        assert store.one("SELECT state FROM meme_assets")["state"] == "ERASED"
        assert not store.one("SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],))
        assert store.rows("SELECT * FROM model_usage") == before


def test_two_independent_connections_keep_the_first_offer_while_cpu_finishes(
    runtime_case, monkeypatch
):
    from companion.meme_core import MemeCaptionService
    from companion.meme_retrieval import MemeRetrieval

    c, release = runtime_case, threading.Event()

    async def scenario():
        loop, started = asyncio.get_running_loop(), asyncio.Event()

        class Pending(Encoder):
            def query_embed(self, query):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                return super().query_embed(query)

        calls = local_encoder(monkeypatch, Pending)
        with closing(Store(c.core.settings.database)) as other:
            service = MemeCaptionService(other, c.core.settings, None, c.core.clock)
            first = asyncio.create_task(c.service.prepare_offer(c.job))
            await asyncio.wait_for(started.wait(), 2)
            try:
                await service.prepare_offer(c.job)
                winner = MemeRetrieval(service).check(c.job)
                assert winner["ranking"]["header"]["query_fallback_reason"] == "INFERENCE_BUSY"
            finally:
                release.set()
            await first
            assert c.retrieval.check(c.job) == winner
        assert len(calls) == 1 and not c.core.store.rows("SELECT * FROM meme_vectors")

    run(scenario())


@pytest.mark.parametrize("finish", ["cancel", "timeout"])
def test_optional_caller_exit_never_commits_late_cpu_result(runtime_case, monkeypatch, finish):
    c, release = runtime_case, threading.Event()

    async def scenario():
        loop, started = asyncio.get_running_loop(), asyncio.Event()

        class Pending(Encoder):
            def query_embed(self, query):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                return super().query_embed(query)

        calls = local_encoder(monkeypatch, Pending)
        request = asyncio.create_task(c.service.prepare_offer(c.job))
        await asyncio.wait_for(started.wait(), 2)
        future = c.service.index._inflight
        try:
            if finish == "cancel":
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert not c.core.store.one(
                    "SELECT 1 FROM runtime_meta WHERE key=?", (OFFER + c.job["event_id"],)
                )
            else:
                await request
                saved = c.retrieval.check(c.job)
                assert saved["ranking"]["header"]["query_fallback_reason"] == "LOCAL_DEADLINE"
                await c.service.prepare_offer(c.job)
                assert c.retrieval.check(c.job) == saved
            assert c.service.index._inflight is future and len(calls) == 1
        finally:
            release.set()
        await asyncio.shield(future)
        await asyncio.sleep(0)
        assert not c.core.store.rows("SELECT * FROM meme_vectors")
        if finish == "timeout":
            assert c.retrieval.check(c.job) == saved

    run(scenario())


def test_cache_capacity_uses_fresh_proof_without_inserting_another_slot(runtime_case, monkeypatch):
    c = runtime_case
    job = c.core.store.one("SELECT * FROM jobs WHERE job_id=?", (c.caption_id,))
    caption = c.core.store.one("SELECT * FROM meme_captions WHERE job_id=?", (c.caption_id,))
    binding, *_ = c.retrieval._resolve(c.asset_id, c.caption_id)
    encoded = index.pack(vector())
    # Synthetic settled-slot copies populate only the cache capacity fixture. They are
    # not candidate sources and never pass _resolve; the selected source remains native.
    with c.core.store.transaction():
        for i in range(index.LIMIT):
            job_id = "job_" + f"{i:032x}"
            c.core.store.insert(
                "jobs",
                **{**job, "job_id": job_id, "idempotency_key": "synthetic-cache-cap:" + job_id},
            )
            c.core.store.insert(
                "meme_captions",
                **{
                    **caption,
                    "job_id": job_id,
                    "request_sha256": f"{i:064x}",
                },
            )
            identity = index.CacheIdentity.source(
                binding.model_copy(update={"caption_job_id": job_id})
            )
            index.persist(c.core.store, identity, encoded)
    assert c.core.store.one("SELECT COUNT(*) AS n FROM meme_vectors")["n"] == 4096
    saved, _ = freeze(c, monkeypatch)
    assert saved["ranking"]["header"]["cold_batch"] == 1
    assert next(iter(saved["ranking"]["selected"].values()))["basis"] == "SEMANTIC"
    assert c.core.store.one("SELECT COUNT(*) AS n FROM meme_vectors")["n"] == 4096
    assert not c.core.store.one(
        "SELECT 1 FROM meme_vectors WHERE caption_job_id=?", (c.caption_id,)
    )
    # Replacing the same exact slot remains possible at capacity.
    identity = index.CacheIdentity.source(
        binding.model_copy(update={"caption_job_id": "job_" + "0" * 32})
    )
    with c.core.store.transaction():
        index.persist(c.core.store, identity, index.pack(vector(0, 1)))
    assert c.core.store.one("SELECT COUNT(*) AS n FROM meme_vectors")["n"] == 4096
    assert c.core.store.one("SELECT vector FROM meme_vectors WHERE cache_key=?", (identity.key,))[
        "vector"
    ] == index.pack(vector(0, 1))


def test_normal_downgrade_discards_only_projection_and_upgrade_rebuilds_empty(
    runtime_case, monkeypatch, tmp_path
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from companion.meme_core import MemeCaptionService
    from companion.meme_retrieval import MemeRetrieval
    from companion.store import migrate
    from tests.test_test_fixtures import historical_database

    c = runtime_case
    saved, _ = freeze(c, monkeypatch)
    usage = c.core.store.rows("SELECT * FROM model_usage")
    assets = c.core.store.rows("SELECT * FROM meme_assets")
    path = historical_database(c.core.store, tmp_path / "projection.db", "0060_mood_appraisals")
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    command.downgrade(cfg, "0047_meme_assets")
    with closing(Store(path)) as store:
        retrieval = MemeRetrieval(MemeCaptionService(store, c.core.settings, None, c.core.clock))
        assert not store.one("SELECT 1 FROM sqlite_master WHERE name='meme_vectors'")
        assert store.rows("SELECT * FROM model_usage") == usage
        assert store.rows("SELECT * FROM meme_assets") == assets
        assert retrieval.check(c.job) == saved
        migrate(path)
        assert not store.rows("SELECT * FROM meme_vectors")
        assert retrieval.check(c.job) == saved
