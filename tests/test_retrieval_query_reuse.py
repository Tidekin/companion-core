"""Query reuse preserves encoder identity, task isolation and cancellation cleanup."""

import asyncio
import threading
from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
import pytest

from companion import retrieval, turn_recall
from tests.conftest import run


class Encoder:
    def __init__(self, vector=(1., 0.)):
        self.vector, self.queries = np.array(vector), []

    def query_embed(self, query):
        self.queries.append(query)
        return iter([self.vector.copy()])

    def passage_embed(self, texts, batch_size):
        return (self.vector.copy() for _ in texts)


def test_fact_and_turn_search_share_one_query_inference(tmp_path, monkeypatch):
    model = Encoder()
    settings = SimpleNamespace(embedding_cache_dir=str(tmp_path))
    index = retrieval.SemanticIndex(None, settings)
    monkeypatch.setattr(retrieval.SemanticIndex, "warmup", lambda _: model)
    monkeypatch.setattr(turn_recall, "_vectors", OrderedDict())

    async def scenario():
        async with retrieval.query_scope():
            _, question = await asyncio.to_thread(index._infer, "取件码", ())
            scores = await asyncio.to_thread(turn_recall.semantic, settings, "取件码", ["第三排"])
            assert scores == [1.] and question.tolist() == [1., 0.]
            assert model.queries == ["取件码"]
        async with retrieval.query_scope():
            await asyncio.to_thread(index._infer, "取件码", ())
        assert model.queries == ["取件码", "取件码"]
        assert retrieval._queries.get() is None

    run(scenario())


def test_encoder_identity_and_unscoped_calls_are_independent():
    first, second = Encoder(), Encoder((0., 1.))

    async def scenario():
        async with retrieval.query_scope():
            assert retrieval.query_vector(first, "同题").tolist() == [1., 0.]
            assert retrieval.query_vector(second, "同题").tolist() == [0., 1.]
            retrieval.query_vector(first, "同题")
            assert first.queries == second.queries == ["同题"]
        retrieval.query_vector(first, "同题")
        retrieval.query_vector(first, "同题")
        assert len(first.queries) == 3

    run(scenario())


def test_concurrent_replies_do_not_share_query_state():
    model = Encoder()

    async def reply():
        async with retrieval.query_scope():
            retrieval.query_vector(model, "同题")
            await asyncio.sleep(0)
            retrieval.query_vector(model, "同题")

    async def scenario():
        await asyncio.gather(reply(), reply())

    run(scenario())
    assert model.queries == ["同题", "同题"]


def test_cancelled_reply_discards_cache_even_if_inference_finishes_later():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    cache = None

    class SlowEncoder(Encoder):
        def query_embed(self, query):
            entered.set()
            assert release.wait(5)
            return super().query_embed(query)

    model = SlowEncoder()

    def infer():
        try:
            return retrieval.query_vector(model, "取消的消息")
        finally:
            finished.set()

    async def reply():
        nonlocal cache
        async with retrieval.query_scope():
            cache = retrieval._queries.get()
            await asyncio.to_thread(infer)

    async def scenario():
        task = asyncio.create_task(reply())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cache["closed"] and not cache["values"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)
        assert not cache["values"] and retrieval._queries.get() is None

    run(scenario())


def test_second_hop_builds_document_bags_once(monkeypatch):
    docs = ["喷壶锁在三号柜。", "三号柜钥匙在顾念手上。", "顾念周五值守。"]
    grams, calls = turn_recall._grams, []

    def record(text):
        calls.append(text)
        return grams(text)

    monkeypatch.setattr(turn_recall, "_grams", record)
    monkeypatch.setattr(turn_recall, "semantic", lambda *args: [0.] * len(docs))
    settings = SimpleNamespace(turn_recall_second_hop=1, turn_recall_rerank=False)
    first, hop = turn_recall.parts(settings, "喷壶钥匙找谁拿", docs)
    assert first and hop
    assert all(calls.count(doc) == 1 for doc in docs)
    query = turn_recall.expand("周五找顾念")
    bags = [turn_recall.Counter(grams(doc)) for doc in docs]
    assert turn_recall.lexical(query, docs) == turn_recall.lexical(query, docs, bags=bags)
