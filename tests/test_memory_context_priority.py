"""Actual request measurement and source groups under immediate-recall pressure."""

from types import SimpleNamespace

import pytest

from companion import context_selection as selection
from companion import turn_recall
from companion.dialogue_history import fit_context
from companion.token_measurement import measure_request


def source(key, text, *, attachment=None):
    row = {"event_id": key, "user_text": text, "reply_text": "记住了"}
    turn = [{"role": "user", "content": text}, {"role": "assistant", "content": "记住了"}]
    if attachment:
        turn.insert(0, {"role": "user", "content": attachment})
    return row, turn


@pytest.mark.parametrize("rich", [False, True])
def test_explicit_question_does_not_protect_unrelated_recent_chatter(core, rich):
    pairs = [source("a", "台布是靛青"), source("noise", "风铃今天真好听"),
             source("b", "台布是砂黄", attachment="台布的来源说明" if rich else None)]
    rows, turns = map(list, zip(*pairs, strict=True))
    current = [{"role": "user", "content": "台布前后是什么颜色？"}]
    rules = {"role": "system", "content": "始终保留来源和不确定性。" * 100}
    budget = measure_request(core.settings, [rules, *turns[0], *turns[2], *current])
    core.settings.context_soft_tokens = budget.reserved_input_tokens
    protected = selection.protected_recent(core.settings, rows, turns, current, "chat",
        query=current[0]["content"], required=[rules])
    assert protected == {"b"}
    topic = selection.focus(current[0]["content"], rows)
    blocks = [("rules", [rules], True, selection.TOPIC)] + [
        ("history", turn, row["event_id"] in protected,
         selection.TOPIC if row["event_id"] in protected else (
             selection.RELATED if selection.relevance(
                 topic, selection.history_text(row, turn)) == selection.RELATED
             else selection.OLD_HISTORY)) for row, turn in pairs]
    blocks.append(("current", current, True, selection.TOPIC))
    fitted = fit_context(rules, [], turns, current, core.settings.context_soft_tokens,
                         settings=core.settings, blocks=blocks)
    text = str(fitted)
    assert "靛青" in text and "砂黄" in text and "风铃" not in text


def test_continuation_protects_complete_rich_group_with_full_required_cost(core):
    row, turn = source("file", "这是补充资料", attachment="附件表格：周四由林舟取件。")
    current = [{"role": "user", "content": "然后呢"}]
    prefix = [{"role": "system", "content": "必需规则" * 300}]
    core.settings.context_soft_tokens = measure_request(
        core.settings, [*prefix, *turn, *current]).reserved_input_tokens
    assert selection.protected_recent(core.settings, [row], [turn], current, "chat",
        query="然后呢", required=prefix) == {"file"}
    core.settings.context_soft_tokens -= 1
    assert not selection.protected_recent(core.settings, [row], [turn], current, "chat",
        query="然后呢", required=prefix)


def test_attachment_content_can_establish_relevance_without_assistant_guesses(core):
    row, turn = source("file", "这是补充资料", attachment="附件表格：周四由林舟取件。")
    current = [{"role": "user", "content": "林舟哪天取件？"}]
    assert selection.protected_recent(core.settings, [row], [turn], current, "chat",
        query=current[0]["content"]) == {"file"}
    row, turn = source("noise", "风铃好听")
    turn[-1]["content"] = "林舟周四取件"
    assert not selection.protected_recent(core.settings, [row], [turn], current, "chat",
        query=current[0]["content"])


def test_recalled_recent_originals_replace_history_with_one_signed_allowance(core, monkeypatch):
    rows = [{"event_id": "a", "user_text": "台布是靛青", "reply_text": "记住了"},
            {"event_id": "b", "user_text": "台布是砂黄", "reply_text": "记住了"}]
    monkeypatch.setattr(turn_recall, "_candidates", lambda *_a: (rows, [[], []]))
    settings = SimpleNamespace(turn_recall_limit=2, turn_recall_scan_turns=10,
        turn_recall_second_hop=0, turn_recall_history_share=.75, timezone="Asia/Shanghai")
    store = core.store
    result = turn_recall.reference(store, settings, {}, "台布", set(), lambda _: True,
        max_turns=2, max_bytes=8000, ranked_ids={"first": ["a", "b"], "hop": []},
        history_costs={"a": 100, "b": 100})
    assert set(result["event_ids"]) == {"a", "b"}
    assert "靛青" in str(result["data"]) and "砂黄" in str(result["data"])
    assert result["cost_bytes"] <= 6000
    # Full history cost, not the small displayed excerpt, controls admission.
    monkeypatch.setattr("companion.dialogue_history.turn_messages", lambda *_a: [
        {"role": "user", "content": "x" * 8001}])
    assert turn_recall.reference(store, settings, {}, "台布", set(), lambda _: True,
        max_turns=2, max_bytes=8000, ranked_ids={"first": ["a", "b"], "hop": []},
        history_costs={"a": 100, "b": 100}) is None
