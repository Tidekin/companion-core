from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from companion.social_autonomy_protocol import ActionReceipt


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Actor(StrictModel):
    platform_user_id: str = Field(min_length=1, max_length=32)
    role: Literal["owner"]


class Conversation(StrictModel):
    platform_session_key: str = Field(min_length=1, max_length=256)
    scope: Literal["private"]


class Media(StrictModel):
    kind: Literal["image", "file", "voice"]
    media_ref: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SharedLink(StrictModel):
    kind: Literal["link"]
    url: str = Field(min_length=1, max_length=2048)


class SharedImage(StrictModel):
    kind: Literal["image"]
    slot: int = Field(ge=0, le=63, strict=True)


class SharedFile(StrictModel):
    kind: Literal["file"]
    slot: int = Field(ge=0, le=63, strict=True)


class Payload(StrictModel):
    text: str = Field(max_length=20000)
    media: list[Media] = Field(max_length=10)
    shared_content: list[
        Annotated[SharedLink | SharedImage | SharedFile, Field(discriminator="kind")]
    ] = Field(default_factory=list, max_length=64)
    # Absent plain input serializes exactly as before, including historical fingerprints.
    rich: dict | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("rich")
    @classmethod
    def rich_projection(cls, value):
        from companion.qq_rich import validate

        return validate(value) if value is not None else None


class InboundEvent(StrictModel):
    schema_version: Literal[1]
    event_id: str = Field(min_length=8, max_length=128)
    source: Literal["qq.onebot"]
    source_event_id: str = Field(min_length=1, max_length=512)
    event_type: Literal["message.received", "message.media_received"]
    occurred_at: AwareDatetime
    conversation: Conversation
    actor: Actor
    payload: Payload
    trace_id: str = Field(min_length=8, max_length=128)


class LeaseRequest(StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=5, ge=1, le=20)
    delivery_lane: Literal["owner_private", "qq_account"] = "owner_private"
    authority: dict | None = None
    account_actions_authorization_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @field_validator("authority")
    @classmethod
    def owner_reference(cls, value):
        from companion_delivery_protocol.authority import validate_authority_ref

        return validate_authority_ref(value) if value is not None else None

    @model_validator(mode="after")
    def account_authority(self):
        if (
            self.delivery_lane == "qq_account"
            and (self.authority is None or not self.account_actions_authorization_sha256)
        ) or (
            self.delivery_lane == "owner_private"
            and self.account_actions_authorization_sha256 is not None
        ):
            raise ValueError("account delivery requires its owner reference and action authority")
        return self


class LeaseToken(StrictModel):
    lease_token: str = Field(min_length=1, max_length=128)


class NativeActionAck(StrictModel):
    kind: Literal["onebot_action"]
    action: Literal["friend_poke"]
    status: Literal["ok"]
    retcode: int = Field(strict=True, ge=0, le=0)
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeliveryResult(LeaseToken):
    result: Literal["SENT", "RETRYABLE_FAILURE", "DELIVERY_UNCERTAIN", "PERMANENT_FAILURE"]
    provider_message_id: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")
    native_ack: NativeActionAck | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    account_action_receipt: ActionReceipt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def native_receipt(self):
        if self.account_action_receipt is not None and (
            self.native_ack is not None
            or self.provider_message_id is not None
            or self.lease_token != self.account_action_receipt.lease_token
            or self.result
            != {
                "SUCCEEDED": "SENT",
                "REJECTED": "PERMANENT_FAILURE",
                "UNKNOWN": "DELIVERY_UNCERTAIN",
            }[self.account_action_receipt.result]
        ):
            raise ValueError("account action receipt differs from its native result")
        if self.native_ack is not None and (
            self.result != "SENT"
            or self.provider_message_id is not None
            or self.error_code is not None
        ):
            raise ValueError("native action acceptance has no QQ message identifier")
        return self


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    requester_actor_id: str
    subject_filter: list[Literal["user", "self", "relationship", "project"]] = Field(
        default_factory=list, max_length=4
    )
    limit: int = Field(default=6, ge=1, le=10)


class MemoryDecision(StrictModel):
    actor_id: Literal["owner"]
    decision: Literal["confirm", "correct", "forget"]
    corrected_content: str | None = Field(default=None, min_length=1, max_length=500)


class Candidate(StrictModel):
    subject: Literal["user", "self", "relationship", "project"]
    kind: Literal["fact", "preference", "boundary", "episode", "skill"]
    fact_key: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=500)
    evidence_event_id: str
    evidence_quote: str = Field(min_length=2, max_length=500)
    evidence_reference_id: str | None = Field(default=None, min_length=1, max_length=128,
                                             exclude_if=lambda value: value is None)
    salience: int | None = Field(default=None, ge=1, le=10, strict=True)
    # Who first stated the content, and whether the owner relayed it (forward/quote/notice).
    speaker: Literal["owner", "third_party", "assistant", "document"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    relayed_by_owner: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    # The content states a new value replacing an older value of this fact_key.
    update: Literal["replaces_previous"] | None = Field(
        default=None, exclude_if=lambda value: value is None)


class EpisodeSummary(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=600)
    source_event_ids: list[str] = Field(min_length=1, max_length=20)


class InteractionSignal(StrictModel):
    """What the owner's words meant to her; code, not the model, sets the mood change."""

    event_id: str
    reason: Literal[
        "OWNER_AFFECTION",
        "OWNER_HURT_BY_REPLY",
        "OWNER_DISTRESS",
        "OWNER_EXPECTATION",
        "OWNER_PLAYFUL",
    ]
    quote: str = Field(min_length=2, max_length=200)


class Extraction(StrictModel):
    memories: list[Candidate] = Field(default_factory=list, max_length=20)
    episode_summaries: list[EpisodeSummary] = Field(default_factory=list, max_length=4)
    interaction_signals: list[InteractionSignal] = Field(default_factory=list, max_length=8)


class MemoryRelation(StrictModel):
    supported: bool = Field(strict=True)
    update_authorized: bool = Field(strict=True)
    claims_replacement: bool = Field(strict=True)
    same: str | None = Field(default=None, pattern=r"^[mc]\d+$")
    conflicts: list[str] = Field(default_factory=list, max_length=40)
    replaces: list[str] = Field(max_length=40)


class MemoryReview(StrictModel):
    relations: list[MemoryRelation] = Field(max_length=20)


class MemorySelection(StrictModel):
    relevant: list[str] = Field(max_length=10)


class CoreError(Exception):
    def __init__(self, code, status=409):
        self.code = code
        self.status = status
        super().__init__(code)
