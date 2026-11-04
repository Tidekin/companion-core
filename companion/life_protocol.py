"""Bounded Life proposals and preview configuration; no execution authority."""

from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from companion.contracts import StrictModel
from companion.goal_protocol import GoalContinuationDecision, GoalProposal
from companion_delivery_protocol.authority import validate_authority_ref

LifeChannelAction = Literal["qq.private.proactive", "diary.save", "qzone.publish", "rem.save"]


class LifeLoopConfig(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["OFF", "PREVIEW", "SHADOW"] = "OFF"
    source_limit: int = Field(default=8, ge=1, le=8)
    topic_ttl_seconds: int = Field(default=7 * 86400, ge=60, le=7 * 86400)
    follow_up_delay_seconds: int = Field(default=86400, ge=60, le=7 * 86400)
    max_daily_decisions: int = Field(default=6, ge=0, le=10)
    min_interval_seconds: int = Field(default=3600, ge=60, le=86400)
    read_goals_enabled: bool = False
    autonomy_enabled: bool = False  # Source-bound guidance; native goals own execution.


class LifeQZoneSourceGrant(StrictModel):
    grant_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, strict=True)
    purpose: Literal["qzone_draft"]
    event_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime
    audience: Literal["owner_only"]
    owner_qq_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")


class LifeShadowApproval(StrictModel):
    """Bounded Life sources; channel execution still needs its independent purpose."""

    scope: Literal["owner_private_life_shadow_v1"]
    authorization_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    conversation_id: str = Field(min_length=1, max_length=128)
    source_scope: Literal["selected", "owner_private_history"] = Field(
        default="selected", exclude_if=lambda value: value == "selected",
    )
    source_event_ids: list[str] = Field(default_factory=list, max_length=64)
    purposes: list[Literal["PRIVATE_DIARY", "PRIVATE_MESSAGE_DRAFT", "PRIVATE_CREATION"]] = Field(
        min_length=0,
        max_length=3,
    )
    expires_at: AwareDatetime
    max_items: int = Field(ge=1, le=100, strict=True)
    qzone_draft_sources: list[LifeQZoneSourceGrant] = Field(default_factory=list, max_length=64)
    automatic_actions: list[LifeChannelAction] = Field(
        default_factory=list, max_length=4, exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def unique_scope(self):
        if self.source_scope == "selected" and not self.source_event_ids:
            raise ValueError("Selected Life sources require exact event IDs")
        if self.source_scope == "owner_private_history" and (
            self.source_event_ids or self.qzone_draft_sources
        ):
            raise ValueError("Private history cannot include selected or QZone sources")
        if not self.purposes and not self.qzone_draft_sources:
            raise ValueError("Life requires at least one explicitly approved destination")
        if (
            len(set(self.source_event_ids)) != len(self.source_event_ids)
            or len(set(self.purposes)) != len(self.purposes)
            or len(set(self.automatic_actions)) != len(self.automatic_actions)
            or len({g.event_id for g in self.qzone_draft_sources}) != len(self.qzone_draft_sources)
            or any(
                g.event_id not in self.source_event_ids or g.expires_at > self.expires_at
                for g in self.qzone_draft_sources
            )
        ):
            raise ValueError("Life sources and purposes must be unique and bounded by approval")
        if ("qq.private.proactive" in self.automatic_actions
            and "PRIVATE_MESSAGE_DRAFT" not in self.purposes
            or "diary.save" in self.automatic_actions and "PRIVATE_DIARY" not in self.purposes
            or "rem.save" in self.automatic_actions and "PRIVATE_CREATION" not in self.purposes
            or "qzone.publish" in self.automatic_actions and not self.qzone_draft_sources):
            raise ValueError("automatic consumption requires its original draft purpose")
        return self


class LifeDecision(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["SKIP", "CONTINUE", "EXPLORE", "FOLLOW_UP", "SHARE", "DEFER"]
    destination: Literal[
        "NONE", "PRIVATE_DIARY", "PRIVATE_MESSAGE_DRAFT", "QZONE_DRAFT", "PRIVATE_CREATION",
    ]
    source_event_ids: list[str] = Field(max_length=8)
    reason: str = Field(min_length=1, max_length=160)
    text: str = Field(default="", max_length=500)
    goal_id: str | None = Field(default=None, max_length=128)
    goal: GoalProposal | None = None
    continuation: GoalContinuationDecision | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    defer_seconds: int | None = Field(default=None, ge=60, le=86400)
    interest_shift: str | None = Field(
        default=None, min_length=1, max_length=200,
        description="Private diary reflection about self-exploration interests",
    )

    @model_validator(mode="after")
    def coherent(self):
        if self.interest_shift is not None and (
            self.destination != "PRIVATE_DIARY" or not self.interest_shift.strip()
        ):
            raise ValueError("interest reflection requires a private diary destination")
        if self.action in {"SKIP", "DEFER", "CONTINUE", "EXPLORE"}:
            if self.destination != "NONE" or self.text:
                raise ValueError("non-expressive actions have no prose or destination")
        elif self.destination == "NONE" or not self.text.strip() or not self.source_event_ids:
            raise ValueError("expression requires sources, a destination and text")
        if self.action == "FOLLOW_UP" and self.destination != "PRIVATE_MESSAGE_DRAFT":
            raise ValueError("follow-up is private")
        if (self.action == "DEFER") != (self.defer_seconds is not None):
            raise ValueError("only DEFER supplies a bounded delay")
        if self.action == "CONTINUE" and self.goal_id is None:
            raise ValueError("continuation requires an existing goal")
        if self.continuation is not None and (self.action != "CONTINUE" or self.goal_id is None):
            raise ValueError("plan continuation requires the original goal reference")
        if self.action not in {"CONTINUE", "EXPLORE"} and self.goal_id is not None:
            raise ValueError("unrelated goal reference")
        if self.goal is not None and (
            self.action != "EXPLORE"
            or self.goal_id is not None
            or not set(self.goal.source_event_ids) <= set(self.source_event_ids)
        ):
            raise ValueError("new exploration uses one native goal within the proposed source set")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("duplicate source references")
        return self


class TopicClosure(StrictModel):
    source_event_id: str = Field(min_length=1, max_length=128)


class LifeConsumeRequest(StrictModel):
    """Owner-controlled release of an exact retained draft, never a model proposal."""

    authority: dict
    action: LifeChannelAction
    expected_content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("authority")
    @classmethod
    def exact_authority(cls, value):
        return validate_authority_ref(value)


class LifeQZoneDraft(StrictModel):
    """Review artifact only; never accepted by SocialAutonomy.reserve or the Bridge."""

    schema_version: Literal[1] = 1
    draft_id: str = Field(min_length=1, max_length=128)
    purpose: Literal["qzone_draft"] = "qzone_draft"
    audience: Literal["owner_only"] = "owner_only"
    owner_qq_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    text: str = Field(min_length=1, max_length=500)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources: list[LifeQZoneSourceGrant] = Field(min_length=1, max_length=8)
    expires_at: AwareDatetime

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value
