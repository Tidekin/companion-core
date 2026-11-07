"""Image-derived facts retain their source checks inside native write transactions."""

import pytest

from companion import memory_references as refs
from companion import retrieval_work
from companion.contracts import CoreError
from companion.store import encode
from tests.conftest import ingest
from tests.support.memory_references import TEXT, prepared


def test_image_memory_proof_revalidates_inside_checkpoint_transaction(core):
    _, item = prepared(core, "image")
    memory_id = core.memory.add(item, core.clock())
    rows = core.memory.search(TEXT, now=core.clock())
    assert memory_id in {row["memory_id"] for row in rows}
    proof = core.memory_context.pin(core.clock(), rows=rows)
    before = core.store.db.total_changes
    with core.store.transaction():
        core.memory_context.revalidate(proof, core.clock())
    assert core.store.db.total_changes == before


def test_new_dispute_in_same_transaction_invalidates_image_proof(core):
    image, item = prepared(core, "image")
    core.memory.add(item, core.clock())
    proof = core.memory_context.pin(core.clock(), rows=core.memory.search(TEXT, now=core.clock()))
    delivered = core.store.one("SELECT * FROM outbox WHERE origin_event_id='evt_test_1'")
    core.store.execute("UPDATE outbox SET state='SENT' WHERE message_id=?",
                       (delivered["message_id"],))
    ingest(core, 2, "图片里的日期不是这个意思。")
    with core.store.transaction():
        review = refs.capture(core.memory, "evt_test_2", "image_review", "对图片解释有异议",
            {"target_event_id": "evt_test_1", "message_id": delivered["message_id"],
             "content_sha256": delivered["content_sha256"]}, core.clock(), sources=["evt_test_1"])
        assert review is not None
        with pytest.raises(CoreError, match="MEMORY_REFERENCE_CHANGED"):
            refs.read(core.memory, image["reference_id"], core.clock(), work=retrieval_work.Work())
        with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
            core.memory_context.revalidate(proof, core.clock())


def test_review_adjacency_work_does_not_grow_with_unrelated_references(core):
    counts = []
    for start, stop in ((0, 100), (100, 1000)):
        with core.store.transaction():
            for number in range(start, stop):
                core.store.execute("INSERT INTO runtime_meta VALUES (?,?)",
                    (refs.INPUT + str(number), encode({"kind": "image_review",
                     "receipt": {"target_event_id": f"unrelated-{number}"}})))
        ticks = []
        core.store.db.set_progress_handler(lambda ticks=ticks: ticks.append(1) and 0, 10)
        try:
            with core.store.transaction():
                assert refs.image_reviews(core.store, "target", work=retrieval_work.Work()) == []
        finally:
            core.store.db.set_progress_handler(None, 0)
        counts.append(len(ticks))
    assert counts[1] <= counts[0] + 2 and counts[1] < 20


@pytest.mark.parametrize("limit", ["attachment_rows", "text_bytes"])
def test_review_limits_never_return_partial_validity(core, monkeypatch, limit):
    core.store.execute("INSERT INTO runtime_meta VALUES (?,?)", (refs.INPUT + "synthetic-review",
        encode({"kind": "image_review", "receipt": {"target_event_id": "target"}})))
    monkeypatch.setitem(retrieval_work.LIMITS, limit, 0)
    with core.store.transaction(), pytest.raises(retrieval_work.WorkLimit):
        refs.image_reviews(core.store, "target", work=retrieval_work.Work())
