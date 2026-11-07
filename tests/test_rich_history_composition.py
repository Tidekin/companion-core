"""Native rich input, history and memory tools retain independent source boundaries."""

import json

import httpx
import pytest

from companion import qq_rich
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer
from tests.support.failure_notice import assert_failure_notice
from tests.support.native_recall_entry import native_recall, returned
from tests.support.onebot_reference_reader import enriched
from tests.test_memory_review_output import assert_settled_calls
from tests.test_qq_rich_input import card

OWNER_FIRST = "本次临时项目的验收代号改说白帆九号。"
OWNER_NOW = "那这个临时项目的代号还是原来的？"
REFERENCE = "RICHDATA_ONLY_NOT_OWNER"


def mixed():
    return [
        {"type": "reply", "data": {"id": "-42"}},
        {"type": "text", "data": {"text": OWNER_FIRST}},
        card(text=REFERENCE),
    ]


def test_references_only_without_owner_or_history_text_freezes_no_facts_and_calls_chat_once(
    core, tmp_path,
):
    def provider(request, payload, number):
        return httpx.Response(200, json=answer("这是一条引用卡片。"))

    async def scenario():
        async with native_recall(core, tmp_path, history_bytes=16000, respond=provider) as value:
            source = await value.capture(message=[card(text=REFERENCE)])
            assert await value.worker.run_once("reference-only-no-auto-recall")
            assert value.scores == [] and len(value.calls) == 1
            assert_settled_calls(core, 1)
            assert not core.store.rows("SELECT * FROM agent_tool_calls")
            snapshots = json.loads(core.store.one(
                "SELECT snapshots_json FROM agent_conversations")["snapshots_json"])
            assert not any(item["kind"] == "memory" for item in snapshots)
            assert next(item["proof"] for item in snapshots if item["kind"] == "history") == []
            assert REFERENCE in encode(value.calls[0]["messages"])
            assert "青竹七号" not in encode(value.calls[0]["messages"])
            assert json.loads(core.store.one("SELECT body_json FROM events WHERE event_id=?",
                (source["event_id"],))["body_json"])["text"] == ""
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert len(value.sends) == 1
    run(scenario())


@pytest.mark.parametrize("drift", [False, True])
def test_native_rich_history_tool_and_delivery_preserve_source_boundaries(
    core, tmp_path, drift,
):
    async def scenario():
        async with native_recall(core, tmp_path, history_bytes=16000,
            tool_query="临时项目的验收代号", core_options={"context_soft_tokens": 12000}) as value:
            prior = await value.capture(message=mixed())
            assert await value.worker.run_once("rich-history-first")
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            source = await value.capture(OWNER_NOW)
            assert await value.worker.run_once("rich-history-followup")
            assert len(value.calls) == 4
            for payload in (value.calls[1], value.calls[3]):
                requested = next(message["tool_calls"] for message in payload["messages"]
                                 if message.get("tool_calls"))[0]
                query = json.loads(requested["function"]["arguments"])["query"]
                assert REFERENCE not in query and "QQ引用资料" not in query
                assert "青竹七号" in returned(payload)[0]["content"]
            initial = value.calls[2]["messages"]
            assert REFERENCE in encode(initial) and "白帆九号" in encode(initial)
            assert "非主人" in encode(initial) and qq_rich.RULES in initial[0]["content"]
            assert "青竹七号" in encode(initial)
            assert initial[-1] == {"role": "user", "content": OWNER_NOW}
            assert core.store.one("SELECT content FROM memories WHERE memory_id=?",
                (value.memory_id,))["content"].endswith("青竹七号。")
            assert_settled_calls(core, 4)
            usage = core.store.rows("SELECT * FROM model_usage")
            if drift:
                row = core.store.one("SELECT body_json FROM events WHERE event_id=?",
                                     (prior["event_id"],))
                body = json.loads(row["body_json"])
                body["rich"]["segments"][1]["fields"][0]["value"] = "changed prior card"
                core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                                   (encode(body), prior["event_id"]))
                assert not (await value.entry.send(value.entry.client,
                                                   value.entry.protocol))["worked"]
                assert core.store.one("SELECT state FROM outbox WHERE origin_event_id=?",
                    (source["event_id"],)) == {"state": "CANCELLED"}
            else:
                assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            assert len(value.sends) == (1 if drift else 2)
            assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())


@pytest.mark.parametrize("delta,kept", [(-1, False), (0, True)])
def test_rich_history_byte_limit_counts_and_crops_all_three_messages(core, tmp_path, delta, kept):
    first, _ = enriched(mixed())
    reference = qq_rich.context(first["payload"])["content"]
    actual = len((reference + OWNER_FIRST + "青竹七号。").encode("utf-8"))

    async def scenario():
        async with native_recall(core, tmp_path, history_bytes=actual + delta,
                                 tool_query="临时项目的验收代号") as value:
            source = await value.capture(message=mixed())
            body = json.loads(core.store.one("SELECT body_json FROM events WHERE event_id=?",
                (source["event_id"],))["body_json"])
            assert qq_rich.context(body)["content"] == reference
            assert await value.worker.run_once("rich-byte-bound-first")
            assert (await value.entry.send(value.entry.client, value.entry.protocol))["worked"]
            following = await value.capture(OWNER_NOW)
            assert await value.worker.run_once("rich-byte-bound-followup")
            assert len(value.calls) == 4
            initial = value.calls[2]["messages"]
            assert (REFERENCE in encode(initial)) is kept
            assert (OWNER_FIRST in encode(initial)) is kept
            assert initial[-1] == {"role": "user", "content": OWNER_NOW}
            job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?",
                                 (following["event_id"],))
            snapshots = json.loads(core.store.one(
                "SELECT snapshots_json FROM agent_conversations WHERE job_id=?",
                (job["job_id"],))["snapshots_json"])
            proof = next(item["proof"] for item in snapshots if item["kind"] == "history")
            assert [row["event_id"] for row in proof] == ([source["event_id"]] if kept else [])
    run(scenario())


@pytest.mark.parametrize("failure", ["unknown", "budget"])
def test_rich_combined_unknown_and_budget_never_fall_back_to_unaccounted_chat(
    core, tmp_path, failure,
):
    def unknown(request, payload, number):
        raise httpx.ReadTimeout("SYNTHETIC_RICH_UNKNOWN", request=request)

    async def scenario():
        async with native_recall(core, tmp_path, history_bytes=16000,
            respond=unknown if failure == "unknown" else None,
            paid={"max_input_tokens": 1} if failure == "budget" else None) as value:
            await value.capture(message=mixed())
            assert await value.worker.run_once("rich-failed-request")
            assert not value.sends
            if failure == "budget":
                assert_failure_notice(core)
            else:
                assert not core.store.rows("SELECT * FROM outbox")
            usage = core.store.rows("SELECT scenario,state,billed_micro_usd FROM model_usage")
            if failure == "unknown":
                assert len(value.calls) == len(usage) == 1
                assert usage[0]["scenario"] == "chat" and usage[0]["state"] == "UNKNOWN"
                reserved = core.store.one("SELECT reserved_micro_usd FROM model_admissions")
                assert usage[0]["billed_micro_usd"] == reserved["reserved_micro_usd"] > 0
            else:
                assert not value.calls and not usage
            assert not await value.worker.run_once("no-rich-failure-replay")
    run(scenario())
