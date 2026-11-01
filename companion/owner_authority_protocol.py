"""Bounded owner-policy data, distinct from a native execution lease or paid policy."""

from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from companion.contracts import StrictModel
from companion.life_protocol import LifeShadowApproval
from companion_delivery_protocol.authority import validate_authority_ref

SHA = r"^[a-f0-9]{64}$"
Purpose = Literal[
    "chat", "history", "memory", "diary", "affect", "consolidation", "reflection", "goals",
    "wake", "exploration", "social", "expression", "proactive", "content", "image", "file",
    "meme", "poke", "voice", "account_actions", "bili_video", "life", "evaluation", "rem",
    "qzone_publication",
]
CapabilityPolicy = Literal[
    "image", "file", "meme", "poke", "voice", "account_actions", "bili_video", "rem",
    "qzone_publication",
]


class OwnerRuntimeConfig(StrictModel):
    mode: Literal["OFF", "SHADOW", "ENABLED"] = "OFF"


class CaptureIdentity(StrictModel):
    public_key: str = Field(pattern=SHA)
    identity_sha256: str = Field(pattern=SHA)
    protocol_epoch: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


class HistoryPolicy(StrictModel):
    scope: Literal["none", "owner_private"] = "none"
    not_before: AwareDatetime | None = None
    max_turns: int = Field(default=0, ge=0, le=64, strict=True)
    max_utf8_bytes: int = Field(default=0, ge=0, le=128000, strict=True)

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.scope == "owner_private" and (
            self.not_before is None or not self.max_turns or not self.max_utf8_bytes
        ):
            raise ValueError("history requires explicit retention and context bounds")
        if self.scope == "none" and (self.not_before or self.max_turns or self.max_utf8_bytes):
            raise ValueError("disabled history has no data scope")
        return self


class OwnerGrantPolicy(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_qq_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    audience: Literal["owner_only"] = "owner_only"
    purposes: list[Purpose] = Field(min_length=1, max_length=25)
    authorization_sha256: str = Field(pattern=SHA)
    paid_contract_sha256: str = Field(pattern=SHA)
    unknown_carry_sha256: str | None = Field(
        default=None, pattern=SHA, exclude_if=lambda value: value is None,
    )
    capture: CaptureIdentity | None = None
    history: HistoryPolicy = Field(default_factory=HistoryPolicy)
    memory_scope: Literal["none", "owner_private"] = "none"
    source_retention_days: int = Field(default=365, ge=1, le=3650, strict=True)
    capability_policies: dict[CapabilityPolicy, str] = Field(default_factory=dict, max_length=9)
    life_shadow: LifeShadowApproval | None = None
    max_input_chars: int = Field(default=32000, ge=1, le=32000, strict=True)
    max_reply_chars: int = Field(default=32000, ge=1, le=32000, strict=True)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent(self):
        import re

        if len(set(self.purposes)) != len(self.purposes):
            raise ValueError("duplicate owner purposes")
        if "chat" in self.purposes and self.capture is None:
            raise ValueError("chat requires authenticated capture identity")
        if ("history" in self.purposes) != (self.history.scope != "none"):
            raise ValueError("history purpose and source scope must agree")
        if ("memory" in self.purposes) != (self.memory_scope != "none"):
            raise ValueError("memory purpose and source scope must agree")
        if ("life" in self.purposes) != (self.life_shadow is not None):
            raise ValueError("Life requires its bounded purpose/source approval")
        if (self.life_shadow and self.life_shadow.source_scope == "owner_private_history"
            and ("history" not in self.purposes or self.history.scope != "owner_private")):
            raise ValueError("Life history requires the existing private history approval")
        if any(key not in self.purposes or not re.fullmatch(SHA, value)
               for key, value in self.capability_policies.items()):
            raise ValueError("capability policies must match explicit purposes")
        if any(key in self.purposes and key not in self.capability_policies
               for key in ("image", "file", "meme", "poke", "voice", "account_actions",
                           "bili_video", "rem", "qzone_publication")):
            raise ValueError("capability purpose requires policy fingerprint")
        return self


class AuthorityChange(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    expected_revision: int = Field(ge=0, le=2**53 - 1, strict=True)
    policy: OwnerGrantPolicy
    preview_sha256: str = Field(pattern=SHA)


class AuthorityPreview(StrictModel):
    expected_revision: int = Field(ge=0, le=2**53 - 1, strict=True)
    policy: OwnerGrantPolicy


class AuthorityStop(StrictModel):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    authority: dict

    @field_validator("authority")
    @classmethod
    def valid_reference(cls, value):
        return validate_authority_ref(value)


class AuthorityReference(StrictModel):
    authority: dict

    @field_validator("authority")
    @classmethod
    def valid_reference(cls, value):
        return validate_authority_ref(value)


class ModelCarryReference(AuthorityReference):
    carry_sha256: str = Field(pattern=SHA)
    successor_policy_sha256: str = Field(pattern=SHA)


class OwnerCapture(AuthorityReference):
    kind: Literal["authenticated_onebot_ws", "onebot_history_backfill"]
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    source_event_id: str = Field(min_length=1, max_length=512)
    input_sha256: str = Field(pattern=SHA)
    frame_sha256: str = Field(pattern=SHA)
    evidence_sha256: str = Field(pattern=SHA)
    identity_sha256: str = Field(pattern=SHA)
    protocol_epoch: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    received_at: AwareDatetime
    occurred_at: AwareDatetime
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class OwnerAuthorityCheck(AuthorityReference):
    purpose: Purpose


class OwnerSendCheck(AuthorityReference):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_:-]{8,128}$")
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    content_sha256: str = Field(pattern=SHA)
    lease_token: str = Field(pattern=r"^lease_[a-f0-9]{32}$")
    reply_revision: int | None = Field(default=None, ge=0, lt=2**53, strict=True,
                                       exclude_if=lambda value: value is None)
    voice: dict | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("voice")
    @classmethod
    def voice_binding(cls, value):
        from companion_delivery_protocol import validate_voice_check

        return validate_voice_check(value) if value is not None else None


class OwnerRecall(AuthorityReference):
    kind: Literal["authenticated_onebot_friend_recall"]
    recall_id: str = Field(pattern=r"^recall_[a-f0-9]{64}$")
    target_source_event_id: str = Field(min_length=1, max_length=512)
    target_event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    protocol_epoch: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    identity_sha256: str = Field(pattern=SHA)
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    frame_sha256: str = Field(pattern=SHA)
    evidence_sha256: str = Field(pattern=SHA)
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")
