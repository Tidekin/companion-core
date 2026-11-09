"""Independent full-content envelopes preserve arbitrary image/voice positions."""

import copy
import itertools
import json

import pytest

from companion.contracts import CoreError
from companion.transport import digest, enqueue_mixed_reply, reply_group, segment_envelopes
from companion_onebot_bridge import reply_segments
from tests.conftest import event


def freeze(core, order):
    core.ingest(event(core, text="这是一条合成的混合回复请求").model_copy(
        update={"event_id": "evt_" + "1" * 64}), "mixed-protocol")
    job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id)")
    contents = []
    for index, kind in enumerate(order):
        content = {"text": "" if kind == "meme" else "原始绑定文字。",
                   "reply_part": {"kind": kind, "source_index": index}}
        if kind == "meme":
            content["image"] = {"source": "companion.meme_catalog", "category": "happy",
                "candidate_id": "a" * 64, "offer_key": "meme:" + job["event_id"]}
        contents.append(content)
    ids = enqueue_mixed_reply(core.store, job, contents, 500, core.clock())
    assert enqueue_mixed_reply(core.store, job, contents, 500, core.clock()) == ids
    group = reply_group(core.store, ids[0])
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    envelopes = segment_envelopes(group)
    items = [{**row, "content": json.loads(row["content_json"]),
              "authority": {"present": True}, "reply_segment": envelopes[row["message_id"]]}
             for row in group["rows"]]
    return items


@pytest.mark.parametrize("order", list(itertools.permutations(("text", "voice", "meme"))))
def test_every_order_is_independently_content_bound_and_keeps_image_separate(core, order):
    items = freeze(core, order)
    for item, kind in zip(items, order, strict=True):
        envelope = reply_segments.envelope(item)
        assert envelope["version"] == 3
        assert envelope["part_sha256s"][envelope["index"]] == digest(item["content"])
        assert reply_segments.image_only(item) is (kind == "meme")
        assert reply_segments.voice_content(item) is (kind == "voice")


@pytest.mark.parametrize("change", ["kind", "source_index", "image", "text", "version"])
def test_content_or_type_substitution_is_rejected_by_core_and_independent_bridge(core, change):
    items = freeze(core, ("meme", "voice", "text"))
    item = copy.deepcopy(items[0])
    if change == "version":
        item["reply_segment"]["version"] = 2
    elif change == "kind":
        item["content"]["reply_part"]["kind"] = "voice"
    elif change == "source_index":
        item["content"]["reply_part"]["source_index"] = 2
    elif change == "image":
        item["content"]["image"]["candidate_id"] = "b" * 64
    else:
        item["content"]["text"] = "新增未绑定文字。"
    with pytest.raises(ValueError):
        reply_segments.envelope(item)
    if change != "version":
        core.store.execute((
            'UPDATE outbox SET content_json=%s,content_sha256=%s WHERE '
            'message_id=%s'
        ),
            (json.dumps(item["content"]), digest(item["content"]), item["message_id"]))
        with pytest.raises(CoreError):
            reply_group(core.store, item["message_id"])


@pytest.mark.parametrize("outcome", ["PERMANENT_FAILURE", "DELIVERY_UNCERTAIN",
                                     "interrupted", "erased"])
def test_mixed_group_tail_cannot_escape_failure_interruption_or_erasure(core, outcome):
    from companion.transport import cancel_interrupted_replies
    from tests.support.segmented_delivery import acknowledge, lease

    items = freeze(core, ("text", "voice", "meme"))
    first = lease(core)[0]
    if outcome in {"PERMANENT_FAILURE", "DELIVERY_UNCERTAIN"}:
        acknowledge(core, first, outcome)
    elif outcome == "interrupted":
        cancel_interrupted_replies(core.store, items[0]["conversation_id"], core.clock())
    else:
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [items[0]["origin_event_id"]], core.clock())
    core.clock.advance(10)
    assert not lease(core)
    assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                          (items[-1]["message_id"],))["state"] in {"CANCELLED", "FAILED"}
