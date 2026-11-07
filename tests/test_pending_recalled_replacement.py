"""Native NOT_SENT retirement survives recall of the pending replacement and restart."""

import json

import httpx
import pytest

from companion import budget
from companion.core import Core
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from tests.conftest import run
from tests.sdk_reader_fixture import answer
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("case", ["plain", "segmented_first", "segmented_tail"])
def test_recalled_buffered_replacement_cannot_resume_old_reply(core, tmp_path, case):
    calls, pending = [], {}
    old_parts = ["旧的第一段", "旧的第二段", "旧的第三段"]
    replacement_text, fresh_text = "被撤回的替代输入", "新的独立输入"

    def provider(request):
        calls.append(json.loads(request.content)["messages"])
        old = "旧的单条回复" if case == "plain" else "\n".join(old_parts)
        return httpx.Response(200, json=answer(old if len(calls) == 1 else "新的独立回复"))

    async def scenario():
        options = {} if case == "plain" else {
            "segmented_reply": SegmentedReplyConfig(mode="semantic", interval_ms=0)}
        async with environment(core, tmp_path, purposes=("chat", "history"),
            core_options=options) as (entry, sends), httpx.AsyncClient(
                transport=httpx.MockTransport(provider)) as transport:
            model = ModelClient(core.settings, transport)
            with Core(core.settings, model, core.clock) as worker:
                first = await entry.admit(1, "先回答原来的问题")
                assert await worker.run_once("generate-old-reply")
            old_event = first["event_id"]
            old_usage = core.store.one("SELECT * FROM model_usage")
            old_admission = core.store.one("SELECT * FROM model_admissions")
            assert old_usage["state"] == "SETTLED" and old_usage["billed_micro_usd"] > 0
            prefix_count = int(case == "segmented_tail")
            if prefix_count:
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                assert [item["message"][0]["data"]["text"] for item in sends] == old_parts[:1]

            async def pending_replacement(request):
                if request.url.path == "/send_private_msg":
                    pending.update(await entry.capture(2, replacement_text))

            entry.protocol.event_hooks["request"].append(pending_replacement)
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            entry.protocol.event_hooks["request"].remove(pending_replacement)
            attempts_before = {"NOT_SENT": 1} | ({"ACK": 1} if prefix_count else {})
            assert dict(entry.db.execute(
                "SELECT state,count(*) FROM window_attempts GROUP BY state")) == attempts_before
            assert entry.runtime.row()["state"] == "ACTIVE"
            assert len(sends) == prefix_count
            old_deliveries = core.store.rows(
                (
                    (
                        'SELECT '
                        'message_id,state,attempt_count,provider_message_id '
                        'FROM outbox WHERE origin_event_id=%s ORDER BY '
                        'record_sequence'
                    )
                ), (old_event,))
            assert [row["state"] for row in old_deliveries] == {
                "plain": ["CANCELLED"],
                "segmented_first": ["CANCELLED", "CANCELLED", "CANCELLED"],
                "segmented_tail": ["SENT", "CANCELLED", "CANCELLED"],
            }[case]
            assert [row["attempt_count"] for row in old_deliveries] == {
                "plain": [1], "segmented_first": [1, 0, 0], "segmented_tail": [1, 1, 0],
            }[case]
            cancelled = next(row for row in old_deliveries
                             if row["state"] == "CANCELLED" and row["attempt_count"] == 1)
            proof = core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                   ("input_cancel_before_send:" + cancelled["message_id"],))
            assert json.loads(proof["value"])["cause"] == "OWNER_INPUT_PENDING"

            replacement = pending["event"]
            await entry.recall(2)
            assert await entry.runtime.flush_one(entry.client)
            old_state, replacement_state = await entry.state(first), await entry.state(replacement)
            assert old_state["state"] == "COMPLETE"
            assert replacement_state["state"] == "RECALLED"
            for value in (old_state, replacement_state):
                assert value["terminal"] and not value["unsettled_model"]
                assert not value["unsettled_delivery"]
            assert core.store.one("SELECT reply_revision FROM conversations")["reply_revision"] == 1
            assert core.store.one('SELECT state FROM jobs WHERE event_id=%s', (old_event,))[
                "state"] == "SUCCEEDED"
            assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s',
                                       (replacement["event_id"],))
            assert not core.store.one('SELECT 1 FROM jobs WHERE event_id=%s',
                                       (replacement["event_id"],))
            assert core.store.one("SELECT * FROM model_usage") == old_usage
            assert core.store.one("SELECT * FROM model_admissions") == old_admission
            assert budget.total_monthly(core.store, "2000-01-01") == old_usage["billed_micro_usd"]

            entry.runtime = OwnerRuntime(entry.spool, clock=lambda: core.clock.now)
            with Core(core.settings, model, core.clock) as independent:
                assert not await independent.run_once("retired-inputs-cannot-resume")
                fresh = await entry.admit(3, fresh_text)
                assert await independent.run_once("fresh-owner-input")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert (await entry.state(fresh))["state"] == "COMPLETE"
            assert dict(entry.db.execute(
                "SELECT state,count(*) FROM window_attempts GROUP BY state")) == {
                    "ACK": 1 + prefix_count, "NOT_SENT": 1}
            assert [row[0] for row in entry.db.execute(
                "SELECT state FROM inbox ORDER BY rowid")] == ["accepted", "erased", "accepted"]
            assert [item["message"][0]["data"]["text"] for item in sends] == (
                old_parts[:prefix_count] + ["新的独立回复"])
            assert core.store.one("SELECT reply_revision FROM conversations")["reply_revision"] == 2
            assert core.store.rows(
                (
                    (
                        'SELECT '
                        'message_id,state,attempt_count,provider_message_id '
                        'FROM outbox WHERE origin_event_id=%s ORDER BY '
                        'record_sequence'
                    )
                ), (old_event,)) == old_deliveries
            usages = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            assert len(calls) == len(usages) == 2 and usages[0] == old_usage
            assert all(row["state"] == "SETTLED" for row in usages)
            assert budget.total_monthly(core.store, "2000-01-01") == sum(
                row["billed_micro_usd"] for row in usages)
            assert core.store.one('SELECT * FROM model_admissions WHERE request_id=%s',
                                  (old_usage["request_id"],)) == old_admission
            assert replacement_text not in json.dumps(calls[-1], ensure_ascii=False)
            entry.runtime.verify(active=False)
    run(scenario())
