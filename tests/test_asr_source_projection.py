"""Unconfirmed ASR display uses its native receipt, not an old assistant's interpretation."""

import json

import pytest

from companion import context_selection, retrieval_work
from companion import memory_references as refs
from companion.contracts import CoreError
from companion.model import MEMORY_HEADING
from tests.support.memory_references import prepared


@pytest.mark.parametrize("entry", ["reply", "tool"])
def test_asr_source_display_keeps_original_quote_and_pins_unchanged_fact(core, entry, monkeypatch):
    _, item = prepared(core, "transcript")
    raw = "今天8月7日，约好两天后到枫汀取凭条。"
    receipt = refs.capture(core.memory, "evt_test_1", "transcript", raw,
                           {"fixture": "late-upload"}, core.clock())
    item = item.model_copy(update={
        "content": "转写说今天8月7日，但助手认为收件日期不同，日期矛盾。",
        "evidence_quote": "今天8月7日", "evidence_reference_id": receipt["reference_id"]})
    key = core.memory.add(item, core.clock())
    original = core.memory.search("8月7日")[0]
    reads = []
    native_read = refs.read

    def read(*args, **kwargs):
        reads.append(args[1])
        return native_read(*args, **kwargs)

    monkeypatch.setattr(refs, "read", read)
    if entry == "reply":
        core.settings.chat_time_labels = True
        facts = core.memory_context.reply_facts(core.settings, core.clock, "取凭条哪天", [original],
                                               allowed_ids=None, include_standing=False)
        selection = context_selection.References()
        core.memory_context.add_reply_facts(selection, facts, core.settings, core.clock,
                                            chat=True, paused=False)
        content = selection.messages[0]["content"][len(MEMORY_HEADING):]
        displayed = json.loads(content)["memories"][0]
        assert "happened_on" not in displayed
        from companion.time_context import day

        source = core.store.one('SELECT occurred_at FROM events WHERE event_id=%s', ("evt_test_1",))
        assert displayed["source_message_on"] == day(source["occurred_at"], core.settings.timezone)
        proof = facts[3]
    else:
        result = core.memory_context.recall("8月7日", core.clock())
        displayed = next(row for row in result["facts"] if row["memory_id"] == key)
        proof = core.memory_context.pin(core.clock(), rows=result["rows"])
    assert raw in displayed["content"] and "机器转写" in displayed["content"]
    assert "助手认为" not in displayed["content"] and "日期矛盾" not in displayed["content"]
    stored = core.store.one('SELECT content FROM memories WHERE memory_id=%s', (key,))
    assert stored["content"] == original["content"]
    pinned = next(row for row in proof["rows"] if row["memory_id"] == key)
    assert pinned["content"] == original["content"]
    if entry == "reply":
        assert reads.count(receipt["reference_id"]) == 1
    core.memory_context.revalidate(proof, core.clock())
    if entry == "reply":
        assert reads.count(receipt["reference_id"]) == 2


def test_asr_projection_cannot_revive_a_withdrawn_receipt(core):
    receipt, item = prepared(core, "transcript")
    core.memory.add(item, core.clock())
    selected = core.memory.search("入场日期")
    core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                       (refs.INPUT + receipt["reference_id"],))
    with pytest.raises(CoreError):
        core.memory_context.reply_facts(core.settings, core.clock, "入场日期", selected,
                                       allowed_ids=None, include_standing=False)


@pytest.mark.parametrize("resource", ["sql_milliseconds", "text_bytes", "source_nodes"])
def test_recalled_asr_with_exhausted_shared_work_never_retries_or_exposes_unverified_fact(
        core, monkeypatch, resource):
    _, item = prepared(core, "transcript")
    core.memory.add(item, core.clock())
    selected = core.memory.search("入场日期")
    assert selected
    writes = []
    native_transaction = core.store.transaction

    def transaction(*args, **kwargs):
        writes.append(True)
        return native_transaction(*args, **kwargs)

    monkeypatch.setattr(core.store, "transaction", transaction)
    with retrieval_work.scope() as work:
        assert work.take(resource, retrieval_work.LIMITS[resource])
        facts = core.memory_context.reply_facts(core.settings, core.clock, "入场日期", selected,
                                               allowed_ids=None, include_standing=False)
        assert not facts[0] and not facts[1] and not facts[3]["source_event_ids"]
        assert work.counts[resource] == retrieval_work.LIMITS[resource]
        assert work.receipt()["coverage_limited"]
        selection = context_selection.References()
        assert not core.memory_context.add_reply_facts(selection, facts, core.settings, core.clock,
                                                       chat=True, paused=False)
        assert not selection.messages
    assert not writes


def test_long_single_line_asr_retains_the_selected_detail_within_fact_capacity(core):
    _, item = prepared(core, "transcript")
    raw = "随口聊些其他事情，" * 120 + "凭条码是桐桥四号。"
    receipt = refs.capture(core.memory, "evt_test_1", "transcript", raw,
                           {"fixture": "long-asr"}, core.clock())
    item = item.model_copy(update={"content": "转写记载凭条码是桐桥四号。",
        "evidence_quote": "凭条码是桐桥四号", "evidence_reference_id": receipt["reference_id"]})
    key = core.memory.add(item, core.clock())
    result = core.memory_context.recall("凭条码", core.clock())
    displayed = next(row for row in result["facts"] if row["memory_id"] == key)
    assert "桐桥四号" in displayed["content"]
    assert len(displayed["content"]) <= 500


def test_recalled_asr_retains_reply_as_interpretation_beside_exact_original():
    from companion.turn_recall import _data

    original = "八月七日下午，在风亭见。"
    reply = "我猜是枫汀，时间可能听错了。"
    rows = [{"user_text": "补一份资料", "reply_text": reply}]
    notes = [[{"kind": "机器转写", "text": original, "excerpt": False}]]
    shown = _data(rows, notes, [0], "Asia/Shanghai")["turns"][0]
    assert shown["attachments_read"][0]["text"] == original
    assert shown["prior_assistant_interpretation"] == reply and "you_replied" not in shown
    ordinary = _data(rows, [[]], [0], "Asia/Shanghai")["turns"][0]
    assert ordinary["you_replied"] == reply
