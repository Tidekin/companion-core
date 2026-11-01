"""Native final-output choice reuses one paid request and the original voice custody."""

import json

import pytest

from companion import agent_runtime, owner_delivery
from companion.agent_output import reply_format
from companion.agent_persistence import ConversationStore, recoverable
from companion.contracts import CoreError
from companion.reply_segments import SegmentedReplyConfig
from companion.voice_jobs import VoiceJobs
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.test_voice_flow import finish_node, prepare
from tests.test_voice_jobs import voice_config
from tests.voice_owner_fixtures import native_voice

PARTS = ["今天想和你聊聊刚才那件有趣的事情。", "你说的时候我也忍不住跟着笑了起来。"]
TEXT = "".join(PARTS)


async def generate(core, tmp_path, choice, *, voice=True, reply_text=TEXT):
    core.settings.segmented_reply = SegmentedReplyConfig(min_chars=12, interval_ms=500)
    native_voice(core, tmp_path, record=False, voice=voice,
                 config=voice_config(reply={"mode": "model"}))
    response = (answer(calls=[call("voice-final", "reply_with_voice", {"text": reply_text})])
                if choice == "voice" else answer(reply_text))
    provider = Provider(response)
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("model-voice-choice")
    assert core.store.one("SELECT state,last_error_code FROM jobs")["state"] == "SUCCEEDED"
    assert len(provider.requests) == 1
    return provider.requests[0]


@pytest.mark.parametrize("choice", ["voice", "text"])
def test_model_selects_voice_or_segmented_text_in_the_same_generation(core, tmp_path, choice):
    async def scenario():
        request = await generate(core, tmp_path, choice)
        assert "reply_with_voice" in [t["function"]["name"] for t in request["tools"]]
        rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
        assert [json.loads(row["content_json"])["text"] for row in rows] == (
            [TEXT] if choice == "voice" else PARTS)
        assert core.store.one("SELECT output_text,phase FROM agent_conversations") == {
            "output_text": TEXT, "phase": "COMPLETE"}
        usage = core.store.rows("SELECT * FROM model_usage")
        assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
        if choice == "voice":
            assert rows[0]["reply_group_id"] is None
            assert prepare(core, core.voice, rows[0]["message_id"]).state == "WAITING"
            assert prepare(core, core.voice, rows[0]["message_id"]).state == "WAITING"
            assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
            finish_node(VoiceJobs(core.store, core.settings, core.clock))
            assert prepare(core, core.voice, rows[0]["message_id"]).state == "AUDIO"
        else:
            result = prepare(core, core.voice, rows[0]["message_id"])
            assert (result.state, result.reason) == ("TEXT", "VOICE_MODEL_TEXT")
            assert not core.store.rows("SELECT * FROM voice_jobs")
        assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())


def test_model_voice_timeout_falls_back_on_the_original_outbox(core, tmp_path):
    async def scenario():
        await generate(core, tmp_path, "voice")
        before = core.store.one("SELECT * FROM outbox")
        usage = core.store.rows("SELECT * FROM model_usage")
        assert prepare(core, core.voice, before["message_id"]).state == "WAITING"
        core.clock.advance(core.settings.voice.reply.wait_seconds + 1)
        decision = prepare(core, core.voice, before["message_id"])
        assert (decision.state, decision.reason) == ("TEXT", "VOICE_REPLY_WAIT_EXPIRED")
        assert core.store.rows("SELECT * FROM outbox") == [before]
        assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
        assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())


def test_voice_speaks_reply_text_without_quote_control_markers(core, tmp_path):
    async def scenario():
        core.settings.quote_policy = "often"
        await generate(core, tmp_path, "voice", reply_text="【引用 m1a2b】" + TEXT)
        row = core.store.one("SELECT * FROM outbox")
        assert json.loads(row["content_json"]) == {"text": TEXT}
        assert prepare(core, core.voice, row["message_id"]).state == "WAITING"
        assert core.store.one("SELECT input_blob FROM voice_jobs")["input_blob"] == TEXT.encode()
    run(scenario())


def test_saved_voice_output_resumes_without_another_paid_request(core, tmp_path):
    native_voice(core, tmp_path, record=False, config=voice_config(reply={"mode": "model"}))
    job = core.claim_job("voice-output-resume")
    snapshots = []
    persistence = ConversationStore(core, job, snapshots)
    provider = Provider(answer(calls=[call("voice-final", "reply_with_voice", {"text": TEXT})]))

    class SimulatedExit(BaseException):
        pass

    save = persistence.after_model_request

    async def after_response(ctx, *, request_context, response):
        await save(ctx, request_context=request_context, response=response)
        raise SimulatedExit()

    persistence.after_model_request = after_response

    async def scenario():
        import httpx

        from companion.model import ModelClient

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            core.model = ModelClient(core.settings, http)
            with pytest.raises(SimulatedExit):
                await agent_runtime.run_conversation(core, job, core.context(job),
                    snapshots=snapshots, persistence=persistence)
            assert recoverable(core.store, job["job_id"])
            restored = ConversationStore(core, job, [])
            result = await agent_runtime.run_conversation(core, job, [],
                conversation=restored.load(), persistence=restored)
            assert result.output.text == TEXT
            assert reply_format(core.store, job["event_id"]) == "voice"
            assert restored.row()["phase"] == "OUTPUT"
            assert len(provider.requests) == 1
            assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 1
            assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())


def test_chat_only_permission_does_not_offer_the_voice_output(core, tmp_path):
    async def scenario():
        request = await generate(core, tmp_path, "text", voice=False)
        assert "reply_with_voice" not in [t["function"]["name"] for t in request["tools"]]
        assert not core.store.rows("SELECT * FROM voice_jobs")
    run(scenario())


@pytest.mark.parametrize("choice", ["voice", "text"])
def test_frozen_delivery_rejects_replacing_the_model_choice(core, tmp_path, choice):
    async def scenario():
        await generate(core, tmp_path, choice)
        saved = core.store.one("SELECT job_id,conversation_json FROM agent_conversations")
        conversation = json.loads(saved["conversation_json"])
        response = next(m for m in reversed(conversation["messages"]) if m["kind"] == "response")
        if choice == "voice":
            del response["metadata"]["core_reply_format"]
        else:
            response.setdefault("metadata", {})["core_reply_format"] = "voice"
        core.store.execute("UPDATE agent_conversations SET conversation_json=? WHERE job_id=?",
                           (json.dumps(conversation), saved["job_id"]))
        row = core.store.one("SELECT message_id FROM outbox ORDER BY segment_index LIMIT 1")
        with pytest.raises(CoreError, match="OWNER_DELIVERY_SOURCE_CHANGED"):
            owner_delivery.check_delivery(core.store, core.settings, core.clock, row["message_id"])
        assert not core.store.rows("SELECT * FROM voice_jobs")
    run(scenario())
