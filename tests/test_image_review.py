"""Source-bound image corrections through the real API and an independent SDK worker.

External model/QQ/CDN transport is synthetic; these are contract checks, not a
measurement of semantic model quality.
"""

import json

import httpx
import pytest

from companion import image_spool, memory_references, retrieval_work
from companion.config import ObservationAdapterConfig
from companion.contracts import Candidate, CoreError
from companion.memory_context import MemoryContext
from tests.conftest import run
from tests.sdk_reader_fixture import answer, worker_for
from tests.support.native_image_memory import native_image_memory
from tests.test_native_history_delivery import sdk_step
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


def test_image_derived_fact_survives_native_checkpoint_and_delivery(core, tmp_path):
    async def scenario():
        async with native_image_memory(core, tmp_path, record_visible=True,
                                       core_options=OBSERVATION) as context:
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            source = core.store.one("SELECT * FROM events WHERE event_id=?",
                                    (context.job["event_id"],))
            reference = next(value for value in memory_references.inputs(core, source)
                             if value["kind"] == "image")
            memory_id = core.memory.add(Candidate(subject="project", kind="fact",
                fact_key="ticket", content="青栎资料卡：十二月九日入场，座位B17。",
                evidence_event_id=source["event_id"], evidence_quote="十二月九日入场，座位B17",
                evidence_reference_id=reference["reference_id"]), core.clock())
            current = await context.entry.admit(2, "青栎资料卡的入场日期和座位是什么？")
            _, request = await sdk_step(core, tmp_path, current["event_id"], 2)
            delivered = await context.entry.send(context.entry.client, context.entry.protocol)
            assert delivered["attempt_states"].get("ACK")
            job = core.store.one("SELECT * FROM jobs WHERE event_id=?", (current["event_id"],))
            assert job["state"] == "SUCCEEDED", job["last_error_code"]
            assert "青栎资料卡" in json.dumps(request, ensure_ascii=False)
            saved = core.store.one("SELECT phase,snapshots_json FROM agent_conversations "
                                   "WHERE job_id=?", (job["job_id"],))
            assert saved["phase"] == "COMPLETE"
            assert any(memory_id == row["memory_id"]
                       for snapshot in json.loads(saved["snapshots_json"])
                       if snapshot["kind"] == "memory"
                       for row in snapshot["proof"]["rows"])
            assert core.store.one("SELECT state FROM model_usage WHERE job_id=?",
                                  (job["job_id"],))["state"] == "SETTLED"
    run(scenario())


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
            image_proof = memory_references.read(core.memory, image_reference["reference_id"],
                                                 core.clock())["proof"]
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
            with retrieval_work.scope() as work:
                with pytest.raises(CoreError, match="MEMORY_REFERENCE_CHANGED"):
                    memory_references.read(core.memory, image_reference["reference_id"],
                                           core.clock(), work=work)
                assert work.receipt()["counts"]["attachment_rows"] == 1
            with retrieval_work.scope() as work:
                assert work.take("attachment_rows", retrieval_work.LIMITS["attachment_rows"])
                with pytest.raises(retrieval_work.WorkLimit):
                    memory_references.read(core.memory, image_reference["reference_id"],
                                           core.clock(), work=work)
            # Enough source work for the original proof, but not its later review:
            # a nested work limit must not be mistaken for an invalid objection.
            probe = retrieval_work.Work()
            MemoryContext(core.memory).revalidate(image_proof, core.clock(), work=probe)
            with retrieval_work.scope() as work:
                assert work.take("source_nodes", retrieval_work.LIMITS["source_nodes"]
                                 - probe.receipt()["counts"]["source_nodes"])
                with pytest.raises(retrieval_work.WorkLimit):
                    memory_references.read(core.memory, image_reference["reference_id"],
                                           core.clock(), work=work)
                assert work.receipt()["counts"]["attachment_rows"] == 1
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
