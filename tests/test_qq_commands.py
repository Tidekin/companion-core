"""QQ controls require an explicit slash at the start of the complete message."""

import json

import pytest

from companion.commands import command_parts
from companion.diary import DIARY_COMMANDS
from companion.life import COMMANDS
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember
from tests.support.memory_workflows import reply

BARE_COMMANDS = (
    "记住这个：我喜欢绿茶",
    "记住这个",
    "你记得什么",
    "确认这一条 mem_a",
    "纠正这一条 mem_a 我喜欢红茶",
    "忘记这一条 mem_a",
    "暂停主动分享",
    "恢复主动分享",
    "发送状态",
    "核对发送 msg_a 已发送",
    "核对发送 msg_a 取消",
    *DIARY_COMMANDS,
    "读日记 2026-10-18",
    "修改日记 2026-10-18 新正文",
    "删除日记 2026-10-18",
    *COMMANDS,
    "接受目标 goal_a",
    "取消目标 goal_a",
    "安排任务 https://example.org/article 阅读天文介绍",
)


@pytest.mark.parametrize(
    "text",
    [
        *BARE_COMMANDS,
        "/记住这个人喜欢绿茶",
        "/读日记本",
        "/目标列表项",
        "引用：/暂停写日记",
        "> /暂停主动分享",
        "我想讨论\n/删除日记 2026-10-18",
        "“/忘记这一条 mem_a”",
        "／暂停自主活动",
        "//暂停写日记",
        "/未知命令",
    ],
)
def test_ordinary_or_similar_text_uses_chat_without_control_side_effects(core, text):
    core.settings.memory_enabled = True
    core.settings.diary_enabled = True
    ingest(core, text=text)
    body = json.loads(core.store.one("SELECT body_json FROM events")["body_json"])
    assert not body.get("diary_command") and not body.get("control_command")
    assert core.store.one("SELECT memory_processed FROM events")["memory_processed"] == 0
    assert run(core.run_once())
    assert reply(core, 1) == "回复：" + text
    assert core.model.calls[-1][1] == "chat"
    assert core.store.rows("SELECT * FROM memories") == []
    assert core.store.rows("SELECT * FROM diary_revisions") == []
    assert core.store.rows("SELECT * FROM goals") == []
    assert core.store.rows(
        "SELECT * FROM runtime_meta WHERE key IN "
        "('diary_paused','proactive_paused','life_paused')"
    ) == []


@pytest.mark.parametrize("separator", [" ", "\t", "\n", "\u3000", ":", "：", " ： "])
def test_slash_remember_accepts_explicit_argument_boundaries(core, separator):
    core.settings.memory_enabled = True
    ingest(core, text=" \n/记住这个" + separator + "我喜欢绿茶\t ")
    assert run(core.run_once())
    memory = core.store.one("SELECT content,status FROM memories")
    assert memory == {"content": "我喜欢绿茶", "status": "CONFIRMED"}
    assert not core.model.calls


def test_slash_remember_previous_message_and_memory_decisions_keep_erasure(core):
    core.settings.memory_enabled = True
    ingest(core, 1, "我喜欢绿茶")
    assert run(core.run_once())
    ingest(core, 2, "/记住这个")
    assert run(core.run_once())
    memory = core.store.one("SELECT memory_id FROM memories")["memory_id"]
    assert core.store.one("SELECT event_id FROM memory_evidence") == {"event_id": "evt_test_1"}
    ingest(core, 3, f"/纠正这一条：{memory} 我喜欢红茶")
    assert run(core.run_once())
    replacement = core.store.one("SELECT memory_id FROM memories WHERE status='CONFIRMED'")[
        "memory_id"
    ]
    ingest(core, 4, f"/忘记这一条 {replacement}")
    assert run(core.run_once())
    assert core.memory.search("红茶") == []
    assert "我喜欢绿茶" not in str(core.store.rows("SELECT body_json FROM events"))
    assert "我喜欢红茶" not in str(core.store.rows("SELECT content_json FROM outbox"))


def test_slash_confirm_promotes_only_the_requested_memory(core):
    memory = remember(core, 1, "我喜欢绿茶")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    ingest(core, 2, f"/确认这一条 {memory}")
    assert run(core.run_once())
    assert core.store.one("SELECT status FROM memories")["status"] == "CONFIRMED"
    assert not core.model.calls


def test_slash_delivery_reconciliation_does_not_replay_uncertain_send(core):
    ingest(core, 1, "你好")
    assert run(core.run_once())
    message = core.store.one("SELECT message_id FROM outbox")["message_id"]
    core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN'")
    core.model.calls.clear()
    ingest(core, 2, "/发送状态")
    assert run(core.run_once())
    assert message in reply(core, 2)
    ingest(core, 3, f"核对发送 {message} 已发送")
    assert run(core.run_once())
    assert core.store.one("SELECT state FROM outbox WHERE message_id=?", (message,)) == {
        "state": "DELIVERY_UNCERTAIN"
    }
    ingest(core, 4, f"/核对发送：{message} 已发送")
    assert run(core.run_once())
    assert core.store.one("SELECT state FROM outbox WHERE message_id=?", (message,)) == {
        "state": "SENT"
    }
    assert len(core.model.calls) == 1


def test_slash_diary_usage_has_complete_new_syntax(core):
    ingest(core, text="/修改日记")
    assert run(core.run_once())
    assert "/修改日记 YYYY-MM-DD 新正文" in reply(core, 1)
    assert "/读日记 [YYYY-MM-DD]" in reply(core, 1)
    assert command_parts(" /安排任务：https://example.org/article 主题 ") == (
        "安排任务", "https://example.org/article 主题"
    )


@pytest.mark.parametrize(
    "pause,resume,key",
    [
        ("暂停写日记", "恢复写日记", "diary_paused"),
        ("暂停主动分享", "恢复主动分享", "proactive_paused"),
        ("暂停自主活动", "恢复自主活动", "life_paused"),
    ],
)
def test_slash_pause_and_resume_require_complete_no_argument_command(core, pause, resume, key):
    core.settings.diary_enabled = True
    ingest(core, 1, "/" + pause + " 只是讨论")
    assert not core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    assert run(core.run_once())
    assert not core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    ingest(core, 2, " \n/" + pause + "\t ")
    if key in {"diary_paused", "proactive_paused"}:
        assert core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))["value"] == "1"
    assert run(core.run_once())
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))["value"] == "1"
    ingest(core, 3, "/" + resume)
    assert run(core.run_once())
    assert core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))["value"] == "0"
