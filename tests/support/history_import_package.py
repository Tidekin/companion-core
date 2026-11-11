"""Synthetic owner-supplied history package shared by import tests."""

from companion import history_import
from companion.store import encode


def package():
    parts = ["【角色设定】虚构的鲸鱼女孩。\r\n",
             "分析：先理解问题，再用一点玩笑和温柔回应。\r\n",
             "主人：搬家后的暗号用米饭。\r\n",
             "（尾巴尖轻轻晃了晃）好啦，记住要发🍚哦。\r\n"]
    segments, cursor = [], 0
    for text, role, kind in zip(parts, ["user", "assistant", "user", "assistant"],
                                ["prompt", "analysis", "dialogue", "dialogue"], strict=True):
        segments.append({"role": role, "kind": kind, "start": cursor, "end": cursor + len(text)})
        cursor += len(text)
    raw = "".join(parts).encode()
    request = history_import.HistoryRequest(source_label="合成的网页手动复制记录",
        source_sha256=history_import.digest(raw), segments=segments, memories=[{
            "subject": "relationship", "kind": "episode", "fact_key": "move_rice_greeting",
            "content": "之前商量搬进QQ时，主人约定把米饭作为搬家后的暗号。",
            "segment": 2, "quote": "搬家后的暗号用米饭"}], character={
            "name": "汐米", "aliases": ["鱼鱼"], "fictional_identity": "虚构的鲸鱼女孩",
            "traits": ["聪明又慵懒", "傲娇但温柔"],
            "speech_style": ["保留括号动作、尾巴动作和神态，频率随语境变化"],
            "response_habits": ["先理解对方的意图，再决定玩笑还是认真回应"],
            "examples": [{"user": "🍚", "assistant": parts[-1].strip()}]})
    return request, raw, history_import.digest(encode(request.model_dump()).encode())


def imported(core):
    core.settings.memory_enabled = True
    request, raw, sha = package()
    return history_import.apply(core, request, raw, sha)
