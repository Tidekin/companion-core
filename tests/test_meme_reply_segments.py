"""A selected meme is an ordered, separately receipted final image bubble."""

import json

import pytest

from companion.contracts import CoreError
from companion.transport import digest, enqueue_reply_parts, reply_group, segment_envelopes
from companion_onebot_bridge import reply_segments
from tests.conftest import event
from tests.support.segmented_delivery import acknowledge, lease

IMAGE = {"source": "companion.meme_catalog", "category": "happy",
         "candidate_id": "a" * 64}
PARTS = ["第一段文字。\n", "第二段文字。"]


def freeze(core):
    source = event(core, text="请发一张开心的表情包").model_copy(
        update={"event_id": "evt_" + "1" * 64})
    core.ingest(source, "meme-segment-test")
    job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id)")
    image = IMAGE | {"offer_key": "meme:" + job["event_id"]}
    ids = enqueue_reply_parts(core.store, job, PARTS, 500, core.clock(), image=image)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    return job, ids, image


def test_meme_tail_is_atomic_idempotent_ordered_and_preserves_visible_text(core):
    job, ids, image = freeze(core)
    assert len(ids) == 3
    assert enqueue_reply_parts(core.store, job, PARTS, 500, core.clock(), image=image) == ids
    group = reply_group(core.store, ids[0])
    assert group["text"] == "".join(PARTS)
    contents = [json.loads(row["content_json"]) for row in group["rows"]]
    assert contents == [{"text": PARTS[0]}, {"text": PARTS[1]}, {"text": "", "image": image}]
    envelopes = segment_envelopes(group)
    assert all(v["version"] == 2 and v["count"] == 3 for v in envelopes.values())
    assert envelopes[ids[-1]]["part_sha256s"] == [digest(c) for c in contents]
    for row in group["rows"]:
        item = {**row, "content": json.loads(row["content_json"]),
                "authority": {"synthetic": True}, "action_type": "qq.private.reply",
                "reply_segment": envelopes[row["message_id"]]}
        assert reply_segments.envelope(item) == item["reply_segment"]
    first = lease(core)
    assert [v["message_id"] for v in first] == ids[:1]
    acknowledge(core, first[0])
    assert not lease(core)
    core.clock.advance(0.5)
    second = lease(core)
    assert [v["message_id"] for v in second] == ids[1:2]
    acknowledge(core, second[0])
    core.clock.advance(0.5)
    assert [v["message_id"] for v in lease(core)] == ids[2:]


@pytest.mark.parametrize("change", ["image", "text", "missing_image", "reply_to"])
def test_group_and_independent_bridge_bind_image_identity_and_every_part(core, change):
    _, ids, _ = freeze(core)
    group = reply_group(core.store, ids[0])
    row = group["rows"][-1]
    content = json.loads(row["content_json"])
    envelope = segment_envelopes(group)[row["message_id"]]
    if change == "image":
        content["image"]["candidate_id"] = "b" * 64
    elif change == "text":
        content["text"] = "新增未经批准的文字"
    elif change == "missing_image":
        content.pop("image")
    else:
        content["reply_to"] = {"message_id": "12345"}
    item = {**row, "content": content, "authority": {"synthetic": True},
            "reply_segment": envelope}
    with pytest.raises(ValueError, match="INVALID_REPLY_SEGMENT"):
        reply_segments.envelope(item)
    core.store.execute('UPDATE outbox SET content_json=%s,content_sha256=%s WHERE message_id=%s',
                       (json.dumps(content), digest(content), row["message_id"]))
    with pytest.raises(CoreError, match="REPLY_GROUP_CHANGED"):
        reply_group(core.store, ids[0])


@pytest.mark.parametrize("outcome", [
    "PERMANENT_FAILURE", "DELIVERY_UNCERTAIN", "interrupted", "erased",
])
def test_unfinished_text_prefix_never_releases_the_meme_tail(core, outcome):
    job, ids, _ = freeze(core)
    first = lease(core)[0]
    if outcome in {"PERMANENT_FAILURE", "DELIVERY_UNCERTAIN"}:
        acknowledge(core, first, outcome)
    elif outcome == "interrupted":
        from companion.transport import cancel_interrupted_replies

        cancel_interrupted_replies(core.store, job["conversation_id"], core.clock())
    else:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
    core.clock.advance(10)
    assert not lease(core)
    assert core.store.one('SELECT state FROM outbox WHERE message_id=%s', (ids[-1],))["state"] in {
        "CANCELLED", "FAILED"}
