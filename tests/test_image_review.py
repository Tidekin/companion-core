"""Source-bound image corrections through the real API and an independent SDK worker.

External model/QQ/CDN transport is synthetic; these are contract checks, not a
measurement of semantic model quality.
"""

import json

import httpx
import pytest

from companion import image_spool, memory_references
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from tests.conftest import run
from tests.sdk_reader_fixture import answer, worker_for
from tests.support.native_image_memory import native_image_memory
from tests.test_native_image_bursts import model_images

OBSERVATION = {"observation_adapters": {"content_image": ObservationAdapterConfig(
    source="qq_image", token="synthetic-image-reference-token-12345", media_analysis=True)}}


async def reply(core, entry, text, calls):
    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=answer(text))

    async with worker_for(core, provider) as worker:
        assert await worker.run_once()
    delivered = await entry.send(entry.client, entry.protocol)
    assert delivered["attempt_states"].get("ACK")


@pytest.mark.parametrize("expired", [False, True])
def test_review_replays_objection_and_actual_reply_without_overwriting_receipts(
    core, tmp_path, expired,
):
    async def scenario():
        fixture = native_image_memory(core, tmp_path, history=True, record_visible=True,
                                      reply="下图推翻了上图。", core_options=OBSERVATION)
        async with fixture as context:
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            original = core.store.one("SELECT * FROM content_items WHERE owner_event_id=? "
                                       "AND slot=0", (context.job["event_id"],))
            receipt = core.store.one("SELECT * FROM events WHERE event_id=?",
                                      (original["output_event_id"],))
            assert receipt is not None
            source = core.store.one("SELECT * FROM events WHERE event_id=?",
                                    (context.job["event_id"],))
            references = memory_references.inputs(core, source)
            image_reference = next(value for value in references if value["kind"] == "image")
            if expired:
                core.clock.advance(image_spool.RETENTION_SECONDS + 1)
            objection = "你理解错了，两张图的动作和台词都一样，下面不是在推翻上面。"
            correction = await context.entry.admit(2, objection)
            calls = []
            revised = "我漏掉了相同的动作和台词，刚才的反转解释不成立。"
            await reply(core, context.entry, revised, calls)
            assert len(calls) == 1
            assert model_images(calls[0]) == ([] if expired else context.originals)
            payload = json.dumps(calls[0], ensure_ascii=False)
            assert '"originals_attached"' in payload.replace('\\"', '"')
            values = memory_references.image_reviews(core.store, context.job["event_id"])
            assert len(values) == 1
            saved = memory_references.read(core.memory, values[0]["reference_id"], core.clock())
            data = json.loads(saved["text"])
            assert data["owner_objection"] == objection
            assert data["previous_interpretation"] == "下图推翻了上图。"
            assert bool(data["evidence"]["originals_attached"]) != expired
            assert data["evidence"]["evidence_verified_correction"] is False
            with pytest.raises(CoreError, match="MEMORY_REFERENCE_CHANGED"):
                memory_references.read(core.memory, image_reference["reference_id"], core.clock())
            assert core.store.one("SELECT * FROM events WHERE event_id=?",
                                  (receipt["event_id"],)) == receipt
            assert core.store.one("SELECT output_event_id FROM content_items WHERE item_id=?",
                (original["item_id"],))["output_event_id"] == receipt["event_id"]
            await context.entry.admit(3, "再聊聊刚才图片的意思")
            await reply(core, context.entry, "沿着相同台词与处境的关系继续讨论。", calls)
            replayed = json.dumps(calls[-1], ensure_ascii=False)
            assert "owner_objection" in replayed and "later_machine_reply" in replayed
            assert "evidence_verified_correction" in replayed
            # Erasure of either source invalidates/removes the derived association.
            with core.store.transaction():
                core.ledger.expire_events(core.store, [correction["event_id"]], core.clock())
            assert memory_references.image_reviews(core.store, context.job["event_id"]) == []
            with pytest.raises(CoreError, match="MEMORY_REFERENCE_CHANGED"):
                memory_references.read(core.memory, saved["reference_id"], core.clock())
    run(scenario())


@pytest.mark.parametrize("explicit", [True, False])
def test_bounded_target_selection_prioritizes_old_group_or_marks_ambiguity(
    core, tmp_path, explicit,
):
    async def scenario():
        fixture = native_image_memory(core, tmp_path, history=True, record_visible=True)
        async with fixture as context:
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            newer = await context.admit_image(2)
            calls = []
            await reply(core, context.entry, "看到了新一组图片。", calls)
            text = "你看错了。" + (context.job["event_id"] if explicit else "")
            correction = await context.entry.admit(3, text)
            await reply(core, context.entry, "我来核对具体分歧。", calls)
            selected = core.store.rows("SELECT c.owner_event_id FROM agent_image_inputs a "
                "JOIN content_items c USING(item_id) WHERE a.job_id=(SELECT job_id FROM jobs "
                "WHERE event_id=?) ORDER BY a.position", (correction["event_id"],))
            assert {row["owner_event_id"] for row in selected} == {
                context.job["event_id"] if explicit else newer["event_id"]}
            payload = json.dumps(calls[-1], ensure_ascii=False)
            if explicit:
                reviews = memory_references.image_reviews(core.store, context.job["event_id"])
                assert len(reviews) == 1
            else:
                assert "target_ambiguous" in payload
                assert memory_references.image_reviews(core.store, context.job["event_id"]) == []
            unresolved = core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state!='SETTLED'")
            assert unresolved["n"] == 0
    run(scenario())
