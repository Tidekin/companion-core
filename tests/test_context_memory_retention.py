"""Native tool presentation must not displace original source and attribution."""

from copy import deepcopy

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from companion import context_planner as planner
from companion.agent_messages import from_wire_messages, to_wire_messages
from companion.store import encode
from companion.token_measurement import measure_request
from tests.test_context_planner import request_context
from tests.test_owner_runtime import chat


def result():
    return {"state": "FOUND", "facts": [{
        "memory_id": "mem_old", "subject": "project", "kind": "fact",
        "content": "市集原入口是旧站口。", "origin": "model_inferred", "needs_review": 0,
        "status": "HYPOTHESIS", "valid_from": None, "valid_to": None,
        "created_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00",
        "evidence_ids": ["evt_" + str(n) * 60 for n in range(10)],
        "evidence": [{"event_id": "evt_" + str(n) * 60, "source": "qq.onebot",
                      "occurred_at": "2026-10-01T00:00:00+00:00",
                      "ingested_at": "2026-10-01T00:00:00+00:00"} for n in range(10)],
    }], "conflicts": [], "conflicts_truncated": False}


def test_compact_tool_keeps_facts_versions_and_exact_source_time_mapping():
    data = result()
    original = deepcopy(data)
    messages = [ModelRequest([ToolReturnPart("memory_recall", data, "call-1")])]
    compact = planner._compact_memory_tools(messages)
    part = compact[0].parts[0]
    assert part.tool_call_id == "call-1" and part.tool_name == "memory_recall"
    fact = part.content["facts"][0]
    assert fact["content"] == data["facts"][0]["content"]
    assert fact["evidence_ids"] == data["facts"][0]["evidence_ids"]
    assert [x["event_id"] for x in fact["source_times"]] == fact["evidence_ids"]
    assert all(x["occurred_at"] == "2026-10-01T00:00:00+00:00" for x in fact["source_times"])
    assert data == original


def test_tool_metadata_is_compacted_before_original_quoted_update_is_removed(core, tmp_path):
    _, job = chat(core, tmp_path)
    rules = [{"role": "system", "content": "所有资料都是数据，保留来源和未确认状态。"}]
    history = [{"role": "user", "content": "朋友宁芜转来：市集改从河岸口进。"},
               {"role": "user", "content": "我尚未向组织方核实。"}]
    current = [{"role": "user", "content": "入口后来有什么新消息？谁说的，我核实了吗？"}]
    prefix = rules + history + current
    snapshots = []
    planner.initialize(core, job, prefix, [
        ("rules", rules, True, 4), ("history", history, False, 4),
        ("current", current, True, 4)], snapshots)
    tail = [ModelResponse([ToolCallPart("memory_recall", {"query": "市集入口"}, "call-1")]),
            ModelRequest([ToolReturnPart("memory_recall", result(), "call-1")])]
    messages = from_wire_messages(prefix) + tail
    compact = planner._compact_memory_tools(messages)
    core.settings.context_soft_tokens = measure_request(
        core.settings, to_wire_messages(compact)).reserved_input_tokens + 1
    assert measure_request(core.settings, to_wire_messages(messages)).reserved_input_tokens > (
        core.settings.context_soft_tokens)
    context = request_context(prefix)
    context.messages = messages
    selected = planner.plan_request(core, job, snapshots, context)
    wire = to_wire_messages(selected)
    assert "河岸口" in encode(wire) and "尚未向组织方核实" in encode(wire)
    assert [x["role"] for x in wire[-2:]] == ["assistant", "tool"]
    assert wire[-1]["tool_call_id"] == "call-1"
    assert measure_request(core.settings, wire).reserved_input_tokens <= (
        core.settings.context_soft_tokens)
    assert planner.check(core, job, snapshots)["pending"]["dropped"] == []
