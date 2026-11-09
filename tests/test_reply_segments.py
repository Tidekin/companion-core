import pytest
from pydantic import ValidationError

from companion.reply_segments import SegmentedReplyConfig, split_text


@pytest.mark.parametrize("source", ["toml", "environment"])
def test_segmented_reply_configuration_loads_through_normal_settings_entrypoint(
    core, tmp_path, monkeypatch, source,
):
    from companion.config import Settings

    for field in Settings.model_fields:
        monkeypatch.delenv("COMPANION_" + field.upper(), raising=False)
    monkeypatch.setenv("COMPANION_DATABASE", core.settings.database.get_secret_value())
    monkeypatch.setenv("COMPANION_STATE_DIRECTORY", core.settings.state_directory)
    path = tmp_path / "config.toml"
    contents = 'owner_qq_id="10001"\nbot_qq_id="10002"\nbridge_token="synthetic-config-token-32"\n'
    policy = SegmentedReplyConfig(max_segments=2, min_chars=8, interval_ms=500)
    if source == "toml":
        contents += "[segmented_reply]\nmax_segments=2\nmin_chars=8\ninterval_ms=500\n"
    else:
        monkeypatch.setenv("COMPANION_SEGMENTED_REPLY", policy.model_dump_json())
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("COMPANION_CONFIG", str(path))
    assert Settings.load().segmented_reply == policy


def test_policy_defaults_are_bounded_strict_and_immutable():
    policy = SegmentedReplyConfig()
    assert policy.model_dump() == {"max_segments": 3, "min_chars": 12, "interval_ms": 1500}
    with pytest.raises(ValidationError):
        policy.interval_ms = 10


@pytest.mark.parametrize("changes", [
    {"max_segments": 1}, {"max_segments": 2**31}, {"max_segments": True},
    {"min_chars": 3}, {"min_chars": 101}, {"min_chars": "12"},
    {"interval_ms": -1}, {"interval_ms": 3001}, {"interval_ms": 1.5},
    {"interval_ms": False}, {"unknown": True},
])
def test_policy_rejects_invalid_or_coerced_fields(changes):
    with pytest.raises(ValidationError):
        SegmentedReplyConfig(**changes)


@pytest.mark.parametrize("text", ["", " ", "\r\n\t  "])
def test_empty_or_all_whitespace_has_no_sendable_segments(text):
    assert split_text(text, SegmentedReplyConfig()) == []


def test_sentence_boundaries_retain_every_punctuation_and_whitespace_character():
    first = "  第一条内容已经足够完整了？！\r\n"
    second = "  第二条内容也已经足够完整。\n"
    third = "第三条内容同样能够独立发送。  "
    assert split_text(first + second + third, SegmentedReplyConfig()) == [first, second, third]


def test_short_opening_merges_forward_and_short_tail_merges_back():
    first = "嗯。第一条内容现在已经足够完整了。"
    second = "第二条内容同样可以独立发送。好。"
    assert split_text(first + second, SegmentedReplyConfig()) == [first, second]


@pytest.mark.parametrize("maximum", [2, 3])
def test_maximum_keeps_the_complete_overflow_tail(maximum):
    sentences = [f"这里是第{number}条能够独立发送的完整句子。" for number in range(6)]
    assert split_text("".join(sentences), SegmentedReplyConfig(max_segments=maximum)) == (
        sentences[:maximum - 1] + ["".join(sentences[maximum - 1:])]
    )


def test_long_sentence_is_not_hard_cut():
    text = "长句" * 10000
    assert split_text(text, SegmentedReplyConfig()) == [text]


@pytest.mark.parametrize("opening,closing", [("```python", "```"), ("~~~~", "~~~~~"),
                                            ("   ```text", "   ```")])
def test_fenced_code_is_an_atomic_span(opening, closing):
    first = "这里是代码之前的一条完整说明。\n"
    code = opening + "\r\nprint('你好！')\nvalue = '问题？'\n" + closing + "\n"
    last = "这里是代码之后的另一条完整说明。"
    assert split_text(first + code + last, SegmentedReplyConfig()) == [first, code, last]


def test_fence_like_text_inside_code_does_not_end_the_block():
    first = "这是一条放在代码之前的完整说明。\n"
    code = "```python\nvalue = '```'\n# 这里不应该切开！\n```\n"
    last = "这是一条放在代码之后的完整说明。"
    assert split_text(first + code + last, SegmentedReplyConfig()) == [first, code, last]


@pytest.mark.parametrize("opening", ["```python", "~~~"])
def test_unclosed_fence_preserves_all_remaining_text_and_terminates(opening):
    first = "这是一条放在代码之前的完整说明。\n"
    code = opening + "\n" + "未闭合的代码！不要切开？\n" * 5000
    assert split_text(first + code, SegmentedReplyConfig()) == [first, code]


@pytest.mark.parametrize("code", ["`a? b! c。`", "``a ` b? c!``", "`未闭合？后续！"])
def test_inline_code_never_splits_at_its_own_punctuation(code):
    text = "前面的完整说明已经超过最短长度。" + "代码示例为" + code + " 后面继续说明。"
    parts = split_text(text, SegmentedReplyConfig(min_chars=4))
    assert "".join(parts) == text
    assert any(code in part for part in parts)


@pytest.mark.parametrize("url", [
    "https://example.test/a?x=1!y=2", "HTTP://example.test/a?q=why?!",
    "https://example.test/路径?问题=你好！", "www.example.test/a?x=1!y=2",
])
def test_url_query_punctuation_is_not_a_sentence_boundary(url):
    text = "前面的完整说明已经超过最短长度。\n链接是" + url + " 后面仍有普通说明。"
    parts = split_text(text, SegmentedReplyConfig(min_chars=4))
    assert "".join(parts) == text
    assert any(url in part for part in parts)


def test_markdown_table_can_remain_a_single_reply():
    text = "这是一段前置说明。\n| 名称 | 内容 |\n| --- | --- |\n| A | 为什么？ |\n说明结束。"
    assert split_text(text, SegmentedReplyConfig(min_chars=4)) == [text]


@pytest.mark.parametrize("body", [
    "短。", "一！二？三。四！", "🙂👨‍👩‍👧‍👦你好！下一句？", "第一行\r第二行\r\n第三行\n",
    "这是第一个完整句子！\n\n\t这是第二个完整句子。", "版本 1.2.3 和数字 3.14 保持不变。",
    "\n" * 5000 + "正文不能丢失？" + " " * 5000,
])
def test_nonblank_inputs_roundtrip_and_never_create_whitespace_only_parts(body):
    for maximum in (2, 3):
        parts = split_text(body, SegmentedReplyConfig(max_segments=maximum, min_chars=4))
        assert 1 <= len(parts) <= maximum
        assert all(part.strip() for part in parts)
        assert "".join(parts) == body
        assert parts == split_text(body, SegmentedReplyConfig(max_segments=maximum, min_chars=4))
