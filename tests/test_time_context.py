"""Chat context carries the local time and when each past turn happened."""

from companion import time_context
from tests.support.dialogue_context import current_job, sent_history


def test_history_turns_are_dated_and_the_current_time_precedes_the_new_input(core):
    core.settings.chat_time_labels = True
    sent_history(core, 1, "今天去医院复查了", "辛苦啦")
    core.clock.advance(3 * 86400)
    messages = core.context(current_job(core))
    # 2026-10-19T04:00Z is Monday 12:00 in Asia/Shanghai; three days later is Thursday.
    assert messages[1] == {"role": "user", "content": "【10月19日 周一 12:00】今天去医院复查了"}
    assert messages[2] == {"role": "assistant", "content": "辛苦啦"}
    assert messages[-2]["content"].startswith(
        "当前时间参考（主人这条消息的时间，不是主人原话）："
        "2026年10月22日 星期四 12:00（Asia/Shanghai）")
    assert messages[-1] == {"role": "user", "content": "刚才聊到哪了？"}


def test_the_system_prefix_does_not_change_with_the_clock(core):
    core.settings.chat_time_labels = True
    sent_history(core, 1, "第一轮", "第一答")
    first = core.context(current_job(core, 98))
    core.clock.advance(3600)
    second = core.context(current_job(core, 99))
    assert first[0] == second[0] and first[1] == second[1]
    assert first[-2] != second[-2]


def test_disabled_labels_leave_the_context_unchanged(core):
    sent_history(core, 1, "第一轮", "第一答")
    messages = core.context(current_job(core))
    assert messages[1:-1] == [{"role": "user", "content": "第一轮"},
                              {"role": "assistant", "content": "第一答"}]
    assert "chat_time_labels" in core.settings.model_dump()


def test_default_is_on_and_absent_from_the_settings_dump():
    from companion.config import Settings

    settings = Settings(owner_qq_id="10001", bot_qq_id="10002", bridge_token="t" * 40)
    assert settings.chat_time_labels and "chat_time_labels" not in settings.model_dump()


def test_labels_use_the_configured_local_day():
    assert time_context.stamp("2026-10-29T16:30:00+00:00", "Asia/Shanghai") == "10月30日 周五 00:30"
    assert time_context.day("2026-10-29T16:30:00+00:00", "Asia/Shanghai") == "2026年10月30日"
