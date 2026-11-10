"""Typed final composition; Core owns synthesis, assets and ordered delivery."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import ToolOutput
from pydantic_ai.messages import ModelResponse, ToolCallPart

from companion import transport, voice_authority
from companion.contracts import CoreError
from companion_delivery_protocol.reply_groups import mixed_part


class ReplyPart(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextPart(ReplyPart):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=20000)


class VoicePart(ReplyPart):
    kind: Literal["voice"]
    text: str = Field(min_length=1, max_length=4000)


class MemePart(ReplyPart):
    kind: Literal["meme"]
    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class GeneratedImagePart(ReplyPart):
    kind: Literal["generated_image"]
    image_id: str = Field(pattern=r"^gimg_[a-f0-9]{32}$")


class SingingPart(ReplyPart):
    kind: Literal["singing"]
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(pattern=r"^voice_[a-f0-9]{32}$")


class MixedReply(ReplyPart):
    parts: list[
        Annotated[
            TextPart | VoicePart | MemePart | GeneratedImagePart | SingingPart,
            Field(discriminator="kind"),
        ]
    ] = Field(min_length=1, max_length=32)

    @property
    def text(self):
        return "\n".join(part.text for part in self.parts if hasattr(part, "text"))

    @model_validator(mode="after")
    def bounded(self):
        if not self.text.strip() or len(self.text) > 20000:
            raise ValueError("REPLY_TEXT_REQUIRED_OR_TOO_LONG")
        for kind in ("meme", "generated_image", "singing"):
            if sum(part.kind == kind for part in self.parts) > 1:
                raise ValueError("REPLY_ASSET_ONCE_REQUIRED")
        if any(hasattr(part, "text") and not part.text.strip() for part in self.parts):
            raise ValueError("REPLY_TEXT_REQUIRED")
        return self


class LegacyVoiceReply(ReplyPart):
    """Read an already observed old final call; never offer it to a fresh model."""

    text: str = Field(min_length=1, max_length=4000)


class LegacySongReply(LegacyVoiceReply):
    request_id: str = Field(pattern=r"^voice_[a-f0-9]{32}$")


def restored_output(conversation):
    if conversation is None or not conversation.messages:
        return None
    response = conversation.messages[-1]
    if not isinstance(response, ModelResponse):
        return None
    calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
    if len(calls) == 1 and calls[0].tool_name in {"reply_with_voice", "reply_with_song"}:
        codec = LegacyVoiceReply if calls[0].tool_name == "reply_with_voice" else LegacySongReply
        return ToolOutput(codec, name=calls[0].tool_name)
    return None


def final_text(output):
    return output.text if isinstance(output, (MixedReply, LegacyVoiceReply)) else output


def reply_output(core, job):
    if transport.native_text(core.store, job["event_id"]):
        return None
    policy = core.settings.voice.reply
    if job["kind"] != "chat" or not voice_authority.permitted(
        core.store, core.settings, core.clock
    ):
        return None
    view = voice_authority.current(
        core.store, core.settings, core.clock, authority=json.loads(job["authority_json"])
    )
    can_speak = (
        policy.enabled
        and policy.mode == "model"
        and any(n.enabled and "tts" in n.capabilities for n in core.settings.voice.nodes.values())
    )
    can_sing = core.settings.voice.singing is not None and "content" in view.policy.purposes
    if not can_speak and not can_sing:
        return None
    return ToolOutput(
        MixedReply,
        name="reply_with_parts",
        description=(
            "结束本轮，自主选择有序的独立气泡：text文字、voice朗读、meme表情、"
            "generated_image已生成图片、singing完整歌曲。可混合、自由安排顺序，也可只选一种。"
            "voice.text只写要朗读的话，成功后不会自动重复发送文字；需要补充文字时另列text。"
            f"每条voice最多{policy.max_chars}字。代码、链接和长清单适合text。"
            "meme只能引用本轮候选candidate_id，沿用严肃语境、概率和冷却限制。"
            "generated_image只能引用本轮generate_image成功返回的image_id。"
            "singing只能引用本轮sing_song返回READY的request_id，Core发送完整连续歌曲，"
            "其text是内部介绍；要公开的介绍请另列text。每种图片和歌曲最多一项。"
            "先完成必要查询；不得编造任务、图片路径、网址或声称已经发送。"
        ),
    )


def save_format(conversation, output):
    if isinstance(output, (MixedReply, LegacyVoiceReply)):
        response = next(
            message
            for message in reversed(conversation.messages)
            if isinstance(message, ModelResponse)
        )
        if isinstance(output, MixedReply):
            selected = {
                "core_reply_format": "mixed",
                "core_reply_parts": output.model_dump(mode="json"),
            }
        else:
            selected = {
                "core_reply_format": "singing" if isinstance(output, LegacySongReply) else "voice",
                **(
                    {"core_singing_request_id": output.request_id}
                    if isinstance(output, LegacySongReply)
                    else {}
                ),
            }
        response.metadata = {**(response.metadata or {}), **selected}
        return output.text
    return output


def _metadata(store, event_id):
    row = store.one(
        (
            (
                'SELECT (CASE WHEN m.value IS JSON THEN '
                "(m.value::json#>>'{metadata}') END) AS metadata FROM "
                'agent_conversations c JOIN jobs j USING(job_id), '
                "json_array_elements(c.conversation_json::json->'message"
                "s') WITH ORDINALITY AS m(value,position) WHERE "
                "j.event_id=%s AND j.kind='chat' AND c.phase IN "
                "('OUTPUT','COMPLETE') AND (CASE WHEN m.value IS JSON "
                "THEN (m.value::json#>>'{kind}') END)='response' ORDER "
                'BY m.position DESC LIMIT 1'
            )
        ),
        (event_id,),
    )
    return json.loads(row["metadata"]) if row and row["metadata"] else {}


def composition(store, event_id):
    metadata = _metadata(store, event_id)
    try:
        return (
            MixedReply.model_validate(metadata["core_reply_parts"])
            if metadata.get("core_reply_format") == "mixed"
            else None
        )
    except (ValueError, TypeError, KeyError):
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from None


def reply_format(store, event_id, row=None):
    """Old frozen voice/song receipts retain their original event-wide semantics."""
    value = _metadata(store, event_id).get("core_reply_format")
    if value not in {None, "voice", "singing", "mixed"}:
        raise CoreError("VOICE_REPLY_FORMAT_INVALID", 409)
    content = json.loads(row["content_json"]) if row else {}
    if value == "mixed" and row:
        try:
            choice = mixed_part(content)
        except (ValueError, TypeError):
            raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from None
        output = composition(store, event_id)
        index = choice["source_index"]
        if index >= len(output.parts) or output.parts[index].kind != choice["kind"]:
            raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
        return choice["kind"]
    if "reply_part" in content:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    return value or "text"


def song_choice(store, event_id, row=None):
    if row and reply_format(store, event_id, row) == "singing":
        choice = json.loads(row["content_json"]).get("reply_part")
        if choice:
            return choice["request_id"]
    return _metadata(store, event_id).get("core_singing_request_id")
