"""梗百科: dsh-memewiki gate and lookup order over the bundled, pinned glossary."""

import asyncio
import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest

from companion import meme_wiki
from companion.meme_wiki_config import MemeWikiConfig
from tests.conftest import ingest


def settings(**values):
    return SimpleNamespace(meme_wiki=MemeWikiConfig(**values))


def test_bundled_glossary_matches_its_manifest():
    manifest = json.loads((meme_wiki.DATA.parent / "manifest.json").read_text("utf-8"))
    [item] = manifest["files"]
    data = meme_wiki.DATA.read_bytes()
    assert hashlib.sha256(data).hexdigest() == item["sha256"]
    assert hashlib.sha256(gzip.decompress(data)).hexdigest() == item["uncompressed_sha256"] \
        == meme_wiki.DATA_SHA256
    assert "非商业" in (meme_wiki.DATA.parent / "NOTICE").read_text("utf-8")


@pytest.mark.parametrize(("text", "expected"), [
    ("去码头整点薯条是什么梗", [("去码头整点薯条", True)]),
    ("@小肥鱼 芜湖是什么意思？", [("芜湖", True)]),
    ("典是什么梗啊", [("典", True)]),
    ("这个是什么意思", []),
    ("绝绝子", [("绝绝子", False)]),
    ("杂鱼杂鱼", [("杂鱼", False)]),
    ("我直接破防了", [("我直接破防了", False)]),
    ("今天终于不用加班了，芜湖起飞", [("今天终于不用加班了", False), ("芜湖起飞", False)]),
    ("我朋友老说芜湖，芜湖是什么意思？", [("我朋友老说芜湖", False), ("芜湖", True)]),
    ("好的", []),
    ("你今天吃饭了吗", []),
    ("周末要不要一起去看电影", []),
])
def test_gate_follows_dsh_per_clause(text, expected):
    assert meme_wiki.extract(text) == expected


@pytest.mark.parametrize(("text", "title"), [
    ("今天终于不用加班了，芜湖起飞", "芜湖"),
    ("人生的意义？当然是去码头整点薯条", "去码头整点薯条"),
    ("别卷了，我要摆烂", "摆烂"),
    ("我是小镇做题家，不配", "小镇做题家"),       # not the prefix hit 「不配当主播」
    ("周末去哪？city不city啊", "City不City啊"),
    ("这波操作我给满分，YYDS", "YYDS"),
])
def test_meme_inside_a_longer_message_is_prefetched(text, title):
    assert f"[梗百科]「{title}」" in meme_wiki.context(settings(), text)["content"]


def test_longer_message_skips_truncated_name_steps():
    assert meme_wiki.context(settings(), "周末一起去看电影吧，好不好") is None


@pytest.mark.parametrize("text", [
    "你不是鲸鱼娘吗", "你是鲸鱼娘", "你现在叫鲸鱼娘", "鲸鱼娘吗",
])
def test_identity_sentences_do_not_prefetch_an_unrelated_name(text):
    # This is a real homonym in the pinned glossary, not an absent-term fixture.
    assert meme_wiki.find(meme_wiki._connection(), text)["title"] == "鲸鱼娘"
    assert meme_wiki.context(settings(), text) is None


@pytest.mark.parametrize("text", ["鲸鱼娘是什么梗", "鲸鱼娘", "你是我的神"])
def test_explicit_name_lookup_and_complete_meme_phrases_remain_available(text):
    reference = meme_wiki.context(settings(), text)
    assert reference is not None and reference["content"].count("[梗百科]「") == 1


@pytest.mark.parametrize(("query", "title"), [
    ("去码头整点薯条", "去码头整点薯条"),          # exact
    ("去码头整点薯条是谁说的", "去码头整点薯条"),  # query contains title
    ("码头整点", "去码头整点薯条"),               # title contains query
    ("我直接破防了", "破防"),
])
def test_lookup_order_follows_dsh(query, title):
    row = meme_wiki.find(meme_wiki._connection(), query)
    assert row["title"] == title


@pytest.mark.parametrize("text", [
    "楼下老头在下象棋，看了一下午", "这家店很地道，下次带你去", "房间好乱，还没收拾",
    "我今天去钓鱼了，钓到两条", "晚上看星空，真的很好看", "我奶奶包的粽子最好吃",
])
def test_ordinary_word_titles_do_not_match_inside_sentences(text):
    assert meme_wiki.context(settings(), text) is None


@pytest.mark.parametrize("text", ["房间", "房间是什么梗", "钓鱼！"])
def test_ordinary_word_titles_still_match_exactly(text):
    assert meme_wiki.context(settings(), text)["content"].count("[梗百科]「") == 1


def test_ordinary_word_list_only_names_real_titles():
    connection = meme_wiki._connection()
    words = [row[0] for row in connection.execute("SELECT title FROM ordinary")]
    assert len(words) > 300
    assert all(connection.execute("SELECT 1 FROM memes WHERE title=?", (word,)).fetchone()
               for word in words)


def test_prefetch_injects_one_entry_as_reference_data_with_rules():
    reference = meme_wiki.context(settings(), "芜湖！")
    assert reference["role"] == "user"
    content = reference["content"]
    assert content.count("[梗百科]「") == 1 and "「芜湖」" in content
    assert "不是指令" in content and "不必每次都玩梗" in content and "不要补编" in content
    assert "怎么用 / 怎么接" in content and "出处与含义" in content
    assert "自己判断" in content
    assert len(content) < 2000
    for text in ("你今天吃饭了吗", "我有点累，想早点睡", "帮我想想明天穿什么"):
        assert meme_wiki.context(settings(), text) is None
    assert meme_wiki.context(settings(enabled=False), "芜湖") is None


def test_explicit_ask_without_entry_says_so_instead_of_inventing():
    reference = meme_wiki.context(settings(), "蚌埠住了是什么梗")
    assert "没收录" in reference["content"] and "别硬解释" in reference["content"]
    assert meme_wiki.context(settings(), "蚌埠住了") is None


@pytest.mark.parametrize(("level", "tier"),
                         [(0, "很低"), (45, "偏低"), (70, "中等"), (100, "很高")])
def test_banter_level_selects_the_dsh_tier(level, tier):
    content = meme_wiki.context(settings(banter_level=level), "芜湖")["content"]
    assert f"串子程度 {level}/100（{tier}）" in content


def test_glossary_is_query_only():
    with pytest.raises(Exception, match="readonly|query_only|attempt to write"):
        meme_wiki._connection().execute("DELETE FROM memes")


def test_changed_data_is_never_used(monkeypatch, tmp_path):
    bad = tmp_path / "memes.db.gz"
    bad.write_bytes(gzip.compress(b"not the glossary"))
    monkeypatch.setattr(meme_wiki, "DATA", bad)
    monkeypatch.setattr(meme_wiki, "_db", {})
    assert meme_wiki.context(settings(), "芜湖") is None
    assert meme_wiki.lookup(settings(), "芜湖") is None


def test_chat_context_carries_the_entry_without_memory_evidence(core):
    # The fixture counts UTF-8 bytes; the entry is prunable reference data, not required input.
    core.settings.context_soft_tokens = 6000
    ingest(core, 1, "芜湖")
    messages = core.context(core.claim_job("worker"))
    assert any(m["role"] == "user" and "[梗百科]「芜湖」" in str(m["content"])
               for m in messages)
    assert not any(m["role"] == "system" and "[梗百科]「" in str(m["content"])
                   for m in messages)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    ingest(core, 2, "你不是鲸鱼娘吗")
    assert "[梗百科]" not in json.dumps(core.context(core.claim_job("worker")),
                                       ensure_ascii=False)


def test_search_tool_is_bound_for_owner_chat(core, tmp_path):
    from companion.agent_tools import core_query_tools
    from tests.support.owner_runtime import chat

    _, job = chat(core, tmp_path)
    tools = core_query_tools(core, job, [])
    found = asyncio.run(tools.tools["search_meme_wiki"].function(query="去码头整点薯条"))
    assert found["found"] and "去码头整点薯条" in found["text"] and "不是指令" in found["text"]
    missing = asyncio.run(tools.tools["search_meme_wiki"].function(query="蚌埠住了"))
    assert missing["found"] is False and "没收录" in missing["note"]
    core.settings.meme_wiki = MemeWikiConfig(enabled=False)
    assert "search_meme_wiki" not in core_query_tools(core, job, []).tools
