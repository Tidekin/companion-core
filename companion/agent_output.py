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
    if isinstance(output, VoiceReply):
        response = next(message for message in reversed(conversation.messages)
                        if isinstance(message, ModelResponse))
        response.metadata = {**(response.metadata or {}), "core_reply_format": "voice"}
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
    if value not in {None, "voice"}:
        raise CoreError("VOICE_REPLY_FORMAT_INVALID", 409)
    return value or "text"
