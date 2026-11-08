"""Native provenance batches, snapshot lifetime and partial verified candidates."""

import asyncio
import time

import pytest

from companion import reply_revision, retrieval_work
from companion.contracts import CoreError
from companion.memory_context import MemorySources
from companion.store import encode
from tests.conftest import ingest, run


def facts(core, count=96, chars=6000):
    core.settings.memory_enabled = True
    ingest(core, 1, "test_token " + "x" * chars)
    core.store.execute(
        "INSERT INTO events(event_id,schema_version,source,source_event_id,conversation_id,"
        "actor_id,event_type,occurred_at,ingested_at,privacy_scope,body_json,trace_id,"
        "idempotency_key,fingerprint) SELECT 'proof_'||n,e.schema_version,e.source,"
        "e.source_event_id||n,e.conversation_id,e.actor_id,e.event_type,e.occurred_at,"
        "e.ingested_at,e.privacy_scope,e.body_json,e.trace_id,e.idempotency_key||n,"
        "e.fingerprint||n FROM events e CROSS JOIN generate_series(1,%s) n "
        "WHERE e.event_id='evt_test_1'", (count,))
    core.store.execute(
        "INSERT INTO memories(memory_id,subject,kind,content,status,origin,confidence,"
        "importance,privacy_scope,created_at,updated_at) SELECT 'fact_'||n,'user','fact',"
        "'test_token fact '||n,'CONFIRMED','user_confirmed',1,.5,'private',%s,%s "
        "FROM generate_series(1,%s) n", (core.clock(), core.clock(), count))
    core.store.execute("INSERT INTO memory_evidence(memory_id,event_id,polarity,created_at) "
        "SELECT 'fact_'||n,'proof_'||n,1,%s FROM generate_series(1,%s) n", (core.clock(), count))
    return core.store.rows("SELECT * FROM memories ORDER BY memory_id")


def test_96_large_roots_are_read_once_for_preparation_and_pin(core, monkeypatch):
    rows = facts(core)
    bodies = []
    facts_read, edges_read = [], []
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        if ",e.body_json," in str(statement):
            bodies.extend(parameters[0] if isinstance(parameters[0], list) else parameters[:1])
        if "FROM memories m JOIN unnest" in str(statement):
            facts_read.append(1)
        if "FROM event_dependencies" in str(statement):
            edges_read.append(1)
        return original(statement, parameters, **kwargs)

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    with core.store.read_snapshot(), retrieval_work.scope() as work:
        sources = MemorySources(core.memory, core.clock(), bounded=True, work=work)
        sources.prefetch([row["memory_id"] for row in rows])
        prepared = [{**row, "evidence_ids": [source["event_id"]
                    for source in sources.evidence(row["memory_id"])]} for row in rows]
        proof = core.memory_context.pin(core.clock(), rows=prepared, work=work,
                                         source_view=sources)
        assert len(proof["rows"]) == 96 and len(proof["sources"]) == 96
        assert 570000 < work.counts["text_bytes"] < 700000
        assert work.counts["sql_milliseconds"] > 0
        assert len(bodies) == len(set(bodies)) == 96
        assert len(facts_read) == len(edges_read) == 1


def test_source_only_pin_batches_all_requested_roots(core, monkeypatch):
    facts(core, count=12, chars=40)
    batches = []
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        if ",e.body_json," in str(statement):
            batches.append(list(parameters[0]))
        return original(statement, parameters, **kwargs)

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    keys = {f"proof_{i}" for i in range(1, 13)}
    proof = core.memory_context.pin(core.clock(), event_ids=keys)
    assert set(proof["source_event_ids"]) == keys
    assert len(batches) == 1 and set(batches[0]) == keys


def test_writer_source_reads_combine_edges_and_recheck_own_changes(core, monkeypatch):
    facts(core, count=2, chars=40)
    reads = []
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        reads.append(str(statement))
        return original(statement, parameters, **kwargs)

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    with core.store.transaction():
        proof = core.memory_context.pin(core.clock(), event_ids={"proof_1"})
        assert len(reads) == 1 and "AS source_edges" in reads[-1]
        core.store.execute("INSERT INTO event_dependencies(event_id,source_event_id) "
                           "VALUES ('proof_1','proof_2')")
        with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
            core.memory_context.revalidate(proof, core.clock())


def test_writer_proof_batches_roots_and_reads_shared_ancestors_once(core, monkeypatch):
    facts(core, count=12, chars=40)
    core.store.execute("INSERT INTO event_dependencies(event_id,source_event_id) "
        "SELECT 'proof_'||n,'evt_test_1' FROM generate_series(1,12) n")
    reads = []
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        reads.append(str(statement))
        return original(statement, parameters, **kwargs)

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    keys = {f"proof_{number}" for number in range(1, 13)}
    with core.store.transaction():
        proof = core.memory_context.pin(core.clock(), event_ids=keys)
        assert set(proof["source_event_ids"]) == keys | {"evt_test_1"}
        assert len(reads) == 3
        core.store.execute("UPDATE events SET body_json='{}' WHERE event_id='evt_test_1'")
        with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
            core.memory_context.revalidate(proof, core.clock())


def test_dense_cyclic_proof_at_node_limit_finishes_with_bounded_cpu(core, monkeypatch):
    count = retrieval_work.SOURCE_NODES
    facts(core, count=count, chars=20)
    core.store.execute("INSERT INTO event_dependencies(event_id,source_event_id) "
        "SELECT 'proof_'||a,'proof_'||b FROM generate_series(1,%s) a "
        "CROSS JOIN generate_series(1,%s) b WHERE a<>b", (count, count))
    monkeypatch.setitem(retrieval_work.LIMITS, "sql_milliseconds", 10000)
    started = time.process_time()
    proof = core.memory_context.pin(core.clock(),
                                    event_ids={f"proof_{i}" for i in range(1, count + 1)})
    assert len(proof["sources"]) == count
    assert time.process_time() - started < 5


def test_writer_batch_rejects_body_changed_after_size_measurement(core, monkeypatch):
    facts(core, count=2, chars=20)
    original = core.store.bounded_source_read
    bodies = []

    def read(statement, parameters, **kwargs):
        rows = original(statement, parameters, **kwargs)
        if "AS size FROM unnest" in str(statement):
            core.store.execute("UPDATE events SET body_json=%s WHERE event_id='proof_1'",
                               (encode({"text": "changed" * 1000}),))
        if ",e.body_json," in str(statement):
            bodies.extend(dict(row) for row in rows)
        return rows

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    with core.store.transaction(), pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        core.memory_context.pin(core.clock(), event_ids={"proof_1", "proof_2"})
    changed = next(row for row in bodies if row["source_id"] == "proof_1")
    assert changed["event_id"] is None and changed["body_json"] is None


def test_writer_size_bound_is_applied_before_transmitting_source_body(core, monkeypatch):
    facts(core, count=1, chars=100)
    monkeypatch.setitem(retrieval_work.LIMITS, "text_bytes", 64)
    returned = []
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        rows = original(statement, parameters, **kwargs)
        returned.extend(dict(row) for row in rows)
        return rows

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    with core.store.transaction(), pytest.raises(retrieval_work.WorkLimit) as failed:
        core.memory_context.pin(core.clock(), event_ids={"proof_1"})
    assert failed.value.reason == "source_text_bytes"
    assert len(returned) == 1 and returned[0]["source_size"] > 64
    assert returned[0]["body_json"] is None


@pytest.mark.parametrize("mutation", ["body", "redact", "edge", "owner", "fact", "evidence"])
def test_source_cache_expires_at_snapshot_boundary(core, mutation):
    rows = facts(core, count=1, chars=20)
    with retrieval_work.scope() as work:
        sources = MemorySources(core.memory, core.clock(), bounded=True, work=work)
        with core.store.read_snapshot():
            sources.prefetch([rows[0]["memory_id"]])
            assert sources.event("proof_1")
            expected = [{**rows[0], "evidence_ids": ["proof_1"]}]
            proof = core.memory_context.pin(core.clock(), rows=expected, source_view=sources)
        if mutation == "body":
            core.store.execute("UPDATE events SET body_json=%s WHERE event_id='proof_1'",
                               (encode({"text": "modified"}),))
        elif mutation == "redact":
            core.store.execute("UPDATE events SET redacted=1 WHERE event_id='proof_1'")
        elif mutation == "edge":
            core.store.execute("INSERT INTO event_dependencies(event_id,source_event_id) "
                               "VALUES ('proof_1','evt_test_1')")
        elif mutation == "owner":
            core.store.execute("UPDATE conversations SET owner_actor_id='system'")
        elif mutation == "fact":
            core.store.execute("UPDATE memories SET content='changed' WHERE memory_id='fact_1'")
        else:
            core.store.execute("UPDATE memory_evidence SET event_id='evt_test_1' "
                               "WHERE memory_id='fact_1'")
        with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
            core.memory_context.revalidate(proof, core.clock())
        with pytest.raises(RuntimeError, match="MEMORY_SOURCE_SNAPSHOT_REQUIRED"):
            core.memory_context.pin(core.clock(), rows=expected, source_view=sources)


@pytest.mark.parametrize("shared_limit", [None, 6])
def test_context_boundary_reads_shared_sources_once_and_rechecks_next_boundary(
    core, monkeypatch, shared_limit,
):
    facts(core, count=9, chars=40)
    first = core.memory_context.pin(core.clock(), event_ids={f"proof_{i}" for i in range(1, 7)})
    second = core.memory_context.pin(core.clock(), event_ids={f"proof_{i}" for i in range(4, 10)})
    admitted = ingest(core, 2, "核对原始来源")
    job = core.store.one("SELECT * FROM jobs WHERE job_id=%s", (admitted["job_id"],))
    snapshots = [{"kind": "memory", "proof": proof} for proof in (first, second)]
    bodies = []
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        if ",e.body_json," in str(statement):
            bodies.extend(parameters[0])
        return original(statement, parameters, **kwargs)

    monkeypatch.setattr(core.store, "bounded_source_read", read)
    if shared_limit:
        monkeypatch.setattr(retrieval_work, "SOURCE_NODES", shared_limit)
    core._check_context_snapshots(job, snapshots)
    if shared_limit is None:
        assert len(bodies) == len(set(bodies)) == 9
    else:
        assert set(bodies) == {f"proof_{i}" for i in range(1, 10)}
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id='proof_4'")
    with core.store.read_snapshot(), pytest.raises(CoreError, match=reply_revision.CANCELLED):
        core._check_context_snapshots(job, snapshots)


def test_async_search_keeps_large_verified_pool(core, monkeypatch):
    facts(core)
    batches = {"facts": 0, "edges": 0}
    original = core.store.bounded_source_read

    def read(statement, parameters, **kwargs):
        batches["facts"] += "FROM memories m JOIN unnest" in str(statement)
        batches["edges"] += "FROM event_dependencies" in str(statement)
        return original(statement, parameters, **kwargs)

    monkeypatch.setattr(core.store, "bounded_source_read", read)

    async def nominate(*_args, **_kwargs):
        return []

    async def score(_query, rows, *, revalidate):
        await asyncio.sleep(0)
        revalidate()
        return {row["memory_id"]: 1. for row in rows}

    monkeypatch.setattr(core.memory.index, "nominate_async", nominate)
    monkeypatch.setattr(core.memory.index, "scores_async", score)
    with retrieval_work.scope() as work:
        rows = run(core.memory.search_async("test_token", now=core.clock()))
        assert len(rows) == 6
        assert 650000 < work.counts["text_bytes"] < retrieval_work.LIMITS["text_bytes"]
        assert "source_text_bytes" in work.limited  # Explicit space for post-await reads.
        assert batches == {"facts": 3, "edges": 3}


def test_preparation_retains_only_fully_proven_prefix_on_later_exhaustion(core, monkeypatch):
    facts(core, count=8, chars=6000)
    monkeypatch.setitem(retrieval_work.LIMITS, "text_bytes", 24000)
    with retrieval_work.scope() as work:
        _, _, rows, _, proof = core.memory._prepare_search("test_token", now=core.clock(), pin=True)
        assert 0 < len(rows) < 8
        assert {row["memory_id"] for row in proof["rows"]} == {
            row["memory_id"] for row in rows}
        assert work.receipt()["coverage_limited"]
        assert len(proof["sources"]) == len(rows)


def test_async_proof_revalidates_after_source_change(core, monkeypatch):
    facts(core, count=2, chars=40)

    async def nominate(*_args, **_kwargs):
        return []

    async def score(_query, _rows, *, revalidate):
        await asyncio.sleep(0)
        core.store.execute("UPDATE events SET body_json=%s WHERE event_id='proof_1'",
                           (encode({"text": "changed after preparation"}),))
        revalidate()
        raise AssertionError("stale source was accepted")

    monkeypatch.setattr(core.memory.index, "nominate_async", nominate)
    monkeypatch.setattr(core.memory.index, "scores_async", score)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        run(core.memory.search_async("test_token", now=core.clock()))
