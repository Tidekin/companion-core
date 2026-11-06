"""Natural model pauses remain independent bubbles without a character target."""

import pytest

from companion.reply_segments import SegmentedReplyConfig, split_text


def policy(**changes):
    return SegmentedReplyConfig(mode="semantic", max_segments="unlimited", **changes)


def test_semantic_pauses_keep_short_units_and_do_not_split_each_comma():
    parts = ["嗯。\n", "刚才那个表情，好像在朝我招手。\n", "你是在叫我吗？"]
    assert split_text("".join(parts), policy(min_chars=100)) == parts


def test_semantic_single_paragraph_keeps_the_model_selected_bubble():
    assert split_text("嗯。我在。慢慢说。", policy()) == ["嗯。我在。慢慢说。"]
    long = "因为这几件事互相有关，" * 100
    assert split_text(long, policy()) == [long]


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_corrected_premise_stays_with_its_explanation_until_an_explicit_pause(ending):
    text = "哦，已经到了。我刚才以为你还在路上。" + ending
    assert split_text(text, policy()) == [text]
    assert split_text(text + "\n那就接着聊刚才那件事。", policy()) == [
        text + "\n", "那就接着聊刚才那件事。"]


@pytest.mark.parametrize("protected", [
    "“第一句。\n第二句？”", "（第一句。\n第二句？）", "`a?\nb!`",
    "```python\nprint('你好！')\n```",
])
def test_semantic_pauses_preserve_quoted_actions_and_code(protected):
    text = "听着。\n" + protected + "\n我在。"
    parts = split_text(text, policy())
    assert "".join(parts) == text and all(part.strip() for part in parts)
    assert any(protected in part for part in parts)


def test_semantic_policy_serializes_opt_in_and_retains_legacy_scope_shape():
    assert "mode" not in SegmentedReplyConfig().model_dump()
    assert policy().model_dump()["mode"] == "semantic"
