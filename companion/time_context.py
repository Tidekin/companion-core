"""Local time labels for the chat context, so past turns and memories are not read as today.

The model has no clock of its own. Without these labels, an old turn or a memory about
"今天去复查" reads as current. History stamps are absolute and never change, which keeps
the cached prompt prefix stable; only the current-time line before the new input moves.
That line uses the owner message's stored time, so a rebuilt context renders the same text.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

WEEKDAYS = "一二三四五六日"


def _local(value, timezone):
    return datetime.fromisoformat(value).astimezone(ZoneInfo(timezone))


def stamp(value, timezone):
    """Absolute local label such as `10月27日 周二 21:05`."""
    local = _local(value, timezone)
    return f"{local.month}月{local.day}日 周{WEEKDAYS[local.weekday()]} {local:%H:%M}"


def day(value, timezone):
    local = _local(value, timezone)
    return f"{local.year}年{local.month}月{local.day}日"


def now_message(now, timezone, *, reference="主人这条消息的时间"):
    local = _local(now, timezone)
    return {"role": "user", "content": (
        f"当前时间参考（{reference}，不是主人原话）："
        f"{local.year}年{local.month}月{local.day}日 "
        f"星期{WEEKDAYS[local.weekday()]} {local:%H:%M}（{timezone}）。"
        "当前主人原话中的‘今天、昨天、刚才’才按当前时间换算。"
        "历史原话的相对时间以当时发言为准；资料明确写出的发言日期优先于上传、导入和记录日期。"
        "资料晚于发言才上传是正常的，接收日期不同本身不构成原话日期错误的证据。"
        "被问到按那句话当天换算时，只按其发言日期回答，不并列用上传日期重算的备选答案。"
        "未给年份时保留年份不确定，不能靠上传时间否定原文日期。"
        "历史消息前的【时间】是消息时间，记忆的记录日期是保存时间；几天前的事不要说成今天发生的；"
        "主人说的钟点结合上下文和时间判断上午下午，比如午睡醒来说的三点是下午三点。")}


def stamp_turn(turn, source, timezone):
    """Copy of one history turn with its owner message prefixed by the original time."""
    if not source.get("occurred_at"):
        return turn
    index = len(turn) - 2 if turn and turn[-1]["role"] == "assistant" else len(turn) - 1
    if index < 0 or turn[index]["role"] != "user" or not isinstance(turn[index]["content"], str):
        return turn
    stamped = list(turn)
    stamped[index] = {**turn[index], "content": "【" + stamp(source["occurred_at"], timezone)
                      + "】" + turn[index]["content"]}
    return stamped
