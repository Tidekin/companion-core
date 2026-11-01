"""Typed local singing execution payload, with a compact independently verifiable manifest."""

from typing import Literal

from pydantic import Field

from companion.voice_protocol import VoiceDTO


class SingingRecipe(VoiceDTO):
    version: Literal[1] = 1
    bvid: str = Field(pattern=r"^BV[0-9A-Za-z]{10}$")
    part: int = Field(default=1, ge=1, le=100, strict=True)
    source_event_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chat_job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")
    tool_call_id: str = Field(min_length=1, max_length=256)
    max_seconds: int = Field(default=600, ge=1, le=600, strict=True)
    max_input_bytes: int = Field(default=32 * 1024 * 1024, ge=1024,
                                 le=32 * 1024 * 1024, strict=True)
