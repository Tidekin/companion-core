"""Native fixtures separate bounded work, recall quality and source authority."""

import asyncio
from contextlib import nullcontext

import numpy as np
import psycopg
import pytest

from companion import memory_index_work, memory_references, retrieval, retrieval_work
from companion.contracts import CoreError
from companion.memory_context import MemorySources
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.confirmed_memory import remember
from tests.support.retrieval_query_reuse import Encoder
from tests.support.retrieval_work_bounds import assert_bounded


def test_old_exact_sources_and_work_survive_tenfold_growth(core, monkeypatch, record_property):
    model = Encoder((0., 0.))
    monkeypatch.setattr(core.memory.index, "warmup", lambda: model)
    target = remember(core, 1, "旧相机放在湖蓝色硬壳箱。", fact_key="camera_case", confirmed=True)
    receipts = []
    for start, stop in ((2, 121), (121, 1201)):
        for number in range(start, stop):
            remember(core, number, f"合成资料第{number}项放在档案室。", confirmed=True)
        for query in (target, "CAMERA_CASE"):
            trace = {}
            result = core.memory_context.recall(query, core.clock(), trace=trace)
            assert target in {row["memory_id"] for row in result["rows"]}
            assert result["retrieval_coverage"]["coverage_limited"]
            assert retrieval_work.NOTE == result["note"]
            receipt = trace["work"]
            assert_bounded(receipt)
            assert receipt["counts"]["candidates"] <= 96
            assert receipt["counts"]["cold_vectors"] <= 16
            receipts.append(receipt)
    assert receipts[0]["counts"]["candidates"] == receipts[2]["counts"]["candidates"]
    assert not core.model.calls
    record_property("work_1x_10x", str(receipts))


def test_nested_reply_scopes_cannot_reset_allowance_and_other_replies_are_independent():
    async def reply():
        async with retrieval.query_scope():
            work = retrieval_work.current()
            assert work.take("cold_vectors", 16)
            async with retrieval.query_scope():
                with retrieval_work.scope() as nested:
                    assert nested is work and not nested.take("cold_vectors")
            assert_bounded(work.receipt())
            return work

    async def scenario():
        first, second = await asyncio.gather(reply(), reply())
        assert first is not second and first.closed and second.closed
        assert retrieval_work.current() is None

    run(scenario())


def test_optional_sql_interrupt_never_replaces_store_handler_or_aborts_writer(core, monkeypatch):
    core.store.execute("SET statement_timeout='5s'")
    monkeypatch.setitem(retrieval_work.LIMITS, "sql_milliseconds", 25)
    try:
        with retrieval_work.scope() as work:
            with pytest.raises(retrieval_work.WorkLimit) as caught:
                work.rows(core.store, "SELECT pg_sleep(1)", limit=1)
            assert caught.value.reason == "sql_milliseconds"
            assert not core.store.in_transaction
            assert core.store.one("SHOW statement_timeout")["statement_timeout"] == "5s"
            with core.store.transaction():
                core.store.execute("INSERT INTO runtime_meta VALUES ('bounded-test','ok')")
                with pytest.raises(retrieval_work.WorkLimit) as refusal:
                    work.rows(core.store, "SELECT 1", limit=1)
                assert refusal.value.reason == "nomination_requires_committed_snapshot"
            assert core.store.one(
                "SELECT value FROM runtime_meta WHERE key='bounded-test'")["value"] == "ok"
            assert_bounded(work.receipt())
        with retrieval_work.scope() as work:
            with pytest.raises(psycopg.errors.UndefinedTable):
                work.rows(core.store, "SELECT * FROM definitely_missing", limit=1)
    finally:
        core.store.execute("RESET statement_timeout")


def test_too_many_evidence_roots_are_partial_coverage_and_never_a_truncated_fact(core):
    target = remember(core, 1, "相机放在湖蓝色硬壳箱。", confirmed=True)
    for number in range(2, 66):
        assert remember(core, number, "相机放在湖蓝色硬壳箱。", confirmed=True) == target
    result = core.memory_context.recall(target, core.clock())
    assert result["state"] == "COVERAGE_LIMITED" and not result["rows"]
    assert "evidence_roots" in result["retrieval_coverage"]["reasons"]
    assert len(MemorySources(core.memory, core.clock()).evidence(target)) == 65
    with pytest.raises(retrieval_work.WorkLimit):
        core.memory_context.pin(core.clock(), rows=[{
            **core.store.one('SELECT * FROM memories WHERE memory_id=%s', (target,)),
            "evidence_ids": [f"evt_test_{number}" for number in range(1, 66)],
        }])


def test_cached_semantic_nominee_remains_subject_to_live_evidence(core, monkeypatch):
    old = remember(core, 1, "相机放在湖蓝色硬壳箱。", confirmed=True)
    model = Encoder((1., 0.))
    monkeypatch.setattr(core.memory.index, "warmup", lambda: model)
    core.memory.index.scores("初始化", [core.store.one(
        'SELECT * FROM memories WHERE memory_id=%s', (old,))])
    for number in range(2, 110):
        remember(core, number, f"合成资料{number}在档案室。", confirmed=True)
    # No lexical overlap; the old cached row lies beyond the recency nominee head.
    result = core.memory_context.recall("摄影设备的收纳位置", core.clock())
    assert old in {row["memory_id"] for row in result["rows"]}
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    result = core.memory_context.recall("摄影设备的收纳位置", core.clock())
    assert old not in {row["memory_id"] for row in result["rows"]}


def test_shared_cold_budget_covers_turns_and_facts(core, monkeypatch):
    from companion import turn_recall

    model = Encoder()
    monkeypatch.setattr(retrieval.SemanticIndex, "warmup", lambda _: model)
    monkeypatch.setattr(turn_recall, "_vectors", turn_recall.OrderedDict())
    with retrieval_work.scope() as work:
        turn_recall.semantic(core.settings, "位置", [f"第{i}项" for i in range(30)])
        assert work.counts["cold_vectors"] == 16
        assert sum(value is not None for value in turn_recall.semantic(
            core.settings, "位置", [f"第{i}项" for i in range(30)])) == 16
        assert_bounded(work.receipt())


def test_approved_fact_not_crowded_out_by_other_cached_subjects(core, monkeypatch):
    model = Encoder()
    monkeypatch.setattr(core.memory.index, "warmup", lambda: model)
    for number in range(1, 61):
        remember(core, number, f"合成资料{number}在档案室。", confirmed=True)
    target = remember(core, 61, "相机收在湖蓝色硬壳箱里。", subject="project", confirmed=True)
    core.memory.index.scores("缓存准备", core.store.rows("SELECT * FROM memories"))
    # All other cached vectors tie above the target's insertion position. The scope
    # must apply before the finite semantic head, as it did before bounded nomination.
    rows = core.memory.search("摄影设备的收纳位置", subjects=("project",), memory_ids=[target],
                              now=core.clock())
    assert [row["memory_id"] for row in rows] == [target]


def test_vector_warmer_is_one_bounded_unit_and_rejects_withdrawal(core, monkeypatch):
    for number in range(1, 21):
        remember(core, number, f"合成资料{number}在档案室。", confirmed=True)
    batches = []

    def infer(query, texts):
        batches.append(len(texts))
        return tuple(np.array([1., 0.]) for _ in texts), np.array([1., 0.])

    monkeypatch.setattr(core.memory.index, "_infer", infer)
    assert not run(memory_index_work.warm_once(core.memory, core.clock, lambda: False))
    assert not batches
    assert run(memory_index_work.warm_once(core.memory, core.clock, lambda: True))
    assert batches == [8]
    assert core.store.one("SELECT COUNT(*) n FROM memory_vectors")["n"] == 8

    async def withdrawn(*args, revalidate):
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, ["evt_test_17"], core.clock())
        revalidate()

    monkeypatch.setattr(core.memory.index, "scores_async", withdrawn)
    with pytest.raises(CoreError):
        run(memory_index_work.warm_once(core.memory, core.clock, lambda: True))
    assert core.store.one("SELECT COUNT(*) n FROM memory_vectors")["n"] == 8


@pytest.mark.parametrize("snapshot,count,queries", [(True, 40, 1), (False, 2, 4)])
def test_empty_attachment_checks_batch_only_in_an_owned_snapshot(
    core, monkeypatch, snapshot, count, queries,
):
    ids = [f"empty_source_{number}" for number in range(count)]
    calls = []
    original = core.store.bounded_read

    def read(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(core.store, "bounded_read", read)
    with (core.store.read_snapshot() if snapshot else nullcontext()):
        with retrieval_work.scope() as work:
            assert memory_references.replay_many(core.store, ids, work=work) == {
                key: [] for key in ids}
            assert work.counts["attachment_rows"] == work.counts["text_bytes"] == 0
            assert len(calls) == queries


def test_attachment_presence_is_rechecked_after_the_snapshot(core, monkeypatch):
    ingest(core, 1, "合成附件消息")
    core.settings.memory_enabled = True
    value = memory_references.capture(core.memory, "evt_test_1", "transcript",
        "synthetic late transcript", {"fixture": "late-replay"}, core.clock())
    key = memory_references.INPUT + value["reference_id"]
    saved = core.store.one("SELECT value FROM runtime_meta WHERE key=%s", (key,))["value"]
    core.store.execute("DELETE FROM runtime_meta WHERE key=%s", (key,))
    original, committed = core.store.bounded_read, []

    def read(statement, parameters, **kwargs):
        rows = original(statement, parameters, **kwargs)
        if "AS transcript FROM unnest" in str(statement) and not committed:
            with psycopg.connect(
                core.settings.database.get_secret_value(), autocommit=True,
            ) as other:
                other.execute("INSERT INTO runtime_meta VALUES (%s,%s)", (key, saved))
            committed.append(1)
        return rows

    monkeypatch.setattr(core.store, "bounded_read", read)
    with core.store.read_snapshot(), retrieval_work.scope() as work:
        assert memory_references.replay_many(core.store, ["evt_test_1"], work=work) == {
            "evt_test_1": []}
    assert committed
    with core.store.read_snapshot(), retrieval_work.scope() as work:
        notes = memory_references.replay_many(core.store, ["evt_test_1"], work=work)
        assert notes["evt_test_1"][0]["text"] == value["text"]


@pytest.mark.parametrize("snapshot", [False, True])
def test_transcript_nomination_is_indexed_bounded_and_preserves_excerpt(
    core, monkeypatch, snapshot,
):
    ingest(core, 1, "附件内容")
    core.settings.memory_enabled = True
    value = memory_references.capture(core.memory, "evt_test_1", "transcript",
        "转写内容" * 500, {"fixture": "bounded-replay"}, core.clock())
    assert value
    with (
        core.store.read_snapshot() if snapshot else nullcontext(),
        retrieval_work.scope() as work,
    ):
        notes = memory_references.replay_many(core.store, ["evt_test_1"], work=work)
        assert len(notes["evt_test_1"]) == 1 and notes["evt_test_1"][0]["excerpt"]
        assert work.counts["attachment_rows"] == 1
        assert_bounded(work.receipt())
    monkeypatch.setitem(retrieval_work.LIMITS, "text_bytes", 10)
    with (
        core.store.read_snapshot() if snapshot else nullcontext(),
        retrieval_work.scope() as work,
    ):
        assert memory_references.replay_many(core.store, ["evt_test_1"], work=work) == {
            "evt_test_1": []}
        assert work.receipt()["coverage_limited"]


def test_history_and_episode_limits_are_partial_without_snapshot_write_retry(core, monkeypatch):
    from tests.support.next_memory_context import episode

    target = remember(core, 1, "相机放在湖蓝色硬壳箱。", confirmed=True)
    episode(core, 1)
    monkeypatch.setitem(retrieval_work.LIMITS, "sql_milliseconds", 0)
    history = core.memory_context.history(target, core.clock())
    assert history["retrieval_coverage"]["coverage_limited"]
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id='evt_test_1'")
    with retrieval_work.scope():
        before = logical_state(core.store)
        result = core.store.read_or_write(lambda: core.memory_context.continuation(
            conversation["conversation_id"], core.clock()))
        assert not result["items"] and result["retrieval_coverage"]["coverage_limited"]
        assert not core.store.in_transaction and logical_state(core.store) == before
