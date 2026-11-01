"""SDK final-output selection; Core retains synthesis and the original delivery."""

import json

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import ToolOutput
from pydantic_ai.messages import ModelResponse

from companion import voice_authority
from companion.contracts import CoreError

VOICE_REPLY = "reply_with_voice"


class VoiceReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


class SongReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(pattern=r"^voice_[a-f0-9]{32}$")


def song_output(core, job):
    if (core.settings.voice.enabled and core.settings.voice.singing is not None
        and voice_authority.permitted(core.store, core.settings, core.clock)):
        view = voice_authority.current(core.store, core.settings, core.clock,
                                       authority=json.loads(job["authority_json"]))
        if "content" not in view.policy.purposes:
            return None
        return ToolOutput(SongReply, name="reply_with_song", description=(
            "结束本轮，发送 sing_song 已返回 READY 的完整歌曲。request_id 必须原样选择"
            "本轮工具的真实结果；text 是一句简短介绍。Core 自动拆成连续语音气泡。"
            "不要编造音频、任务号，也不要声称已经发送。"))
    return None


def voice_output(core, job):
    policy = core.settings.voice.reply
    if (not policy.enabled or policy.mode != "model"
        or not voice_authority.permitted(core.store, core.settings, core.clock)
        or not any(node.enabled and "tts" in node.capabilities
                   for node in core.settings.voice.nodes.values())):
        return None
    voice_authority.current(core.store, core.settings, core.clock,
                            authority=json.loads(job["authority_json"]))
    return ToolOutput(VoiceReply, name=VOICE_REPLY, description=(
        "结束本轮回复，选择用语音朗读 text；也可以直接输出普通文字回复。"
        "根据对话自行选择，主人想听你说话或适合口头表达时可选语音，"
        "代码、链接和长清单适合文字。先完成所需查询，再选择最终回复。"
        f"text 只包含要朗读的自然语言，最多 {policy.max_chars} 字。"
        "Core 随后负责合成和发送；不要声称音频已经发出。"))


def save_format(conversation, output):
    """Mark only a successfully validated SDK output in Core-owned response metadata."""
    if isinstance(output, (VoiceReply, SongReply)):
        response = next(message for message in reversed(conversation.messages)
                        if isinstance(message, ModelResponse))
        response.metadata = {**(response.metadata or {}),
            "core_reply_format": "singing" if isinstance(output, SongReply) else "voice",
            **({"core_singing_request_id": output.request_id}
               if isinstance(output, SongReply) else {})}
        return output.text
    return output


def reply_format(store, event_id):
    """Read the last native response's frozen choice without rehydrating image bytes."""
    row = store.one(
        "SELECT json_extract(m.value,'$.metadata.core_reply_format') AS format "
        "FROM agent_conversations c JOIN jobs j USING(job_id), "
        "json_each(c.conversation_json,'$.messages') m "
        "WHERE j.event_id=? AND j.kind='chat' AND c.phase IN ('OUTPUT','COMPLETE') "
        "AND json_extract(m.value,'$.kind')='response' ORDER BY CAST(m.key AS INTEGER) DESC "
        "LIMIT 1", (event_id,))
    value = row["format"] if row else None
    if value not in {None, "voice", "singing"}:
        raise CoreError("VOICE_REPLY_FORMAT_INVALID", 409)
    return value or "text"


def song_choice(store, event_id):
    row = store.one(
        "SELECT json_extract(m.value,'$.metadata.core_singing_request_id') AS request_id "
        "FROM agent_conversations c JOIN jobs j USING(job_id), "
        "json_each(c.conversation_json,'$.messages') m WHERE j.event_id=? AND j.kind='chat' "
        "AND c.phase IN ('OUTPUT','COMPLETE') AND json_extract(m.value,'$.kind')='response' "
        "ORDER BY CAST(m.key AS INTEGER) DESC LIMIT 1", (event_id,))
    return row["request_id"] if row else None
