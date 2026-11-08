"""Strict proposals and server-issued QQ account action evidence; no I/O or storage."""

from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SHA = r"^[a-f0-9]{64}$"
Sha = Annotated[str, Field(pattern=SHA)]
Ref = Annotated[str, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")]
Action = Literal[
    "qzone.publish", "qq.profile.nickname", "qq.profile.signature", "qq.profile.avatar",
]
ProfileField = Literal["nickname", "signature", "avatar"]


def _aware_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("an aware timestamp is required")
    return value


Timestamp = Annotated[str, AfterValidator(_aware_time)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Versioned(Strict):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value


class SocialAutonomyConfig(Strict):
    enabled: bool = False
    scope: Literal["none", "qq_social_v1"] = "none"
    authorization_sha256: Sha | None = None
    policy_version: int = Field(default=1, ge=1)
    qzone_enabled: bool = False
    qzone_public_enabled: bool = Field(default=False, exclude_if=lambda value: not value)
    nickname_enabled: bool = False
    signature_enabled: bool = False
    avatar_enabled: bool = False
    qzone_daily_limit: int = Field(default=1, ge=0, le=100)
    profile_daily_limit: int = Field(default=1, ge=0, le=100)
    qzone_cooldown_seconds: int = Field(default=3600, ge=0, le=366 * 86400)
    profile_cooldown_seconds: int = Field(default=7 * 86400, ge=0, le=366 * 86400)
    model_daily_calls: int = Field(default=4, ge=0, le=100)
    decision_interval_seconds: int = Field(default=3600, ge=0, le=366 * 86400)
    max_publish_chars: int = Field(default=500, ge=1, le=5000)
    max_nickname_chars: int = Field(default=24, ge=1, le=128)
    max_signature_chars: int = Field(default=120, ge=1, le=1000)
    max_source_refs: int = Field(default=3, ge=1, le=20)
    max_source_chars: int = Field(default=3000, ge=100, le=12000)
    max_input_bytes: int = Field(default=16000, ge=1024, le=100000)
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    timeout_seconds: int = Field(default=45, ge=1, le=180)
    plan_ttl_seconds: int = Field(default=3600, ge=1, le=86400)
    profile_baseline_seconds: int = Field(default=300, ge=1, le=86400)
    max_pending_plans: int = Field(default=4, ge=1, le=100)
    asset_policy_sha256: Sha | None = None
    max_asset_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    max_asset_dimension: int = Field(default=2048, ge=1, le=8192)

    @model_validator(mode="after")
    def authorization(self):
        if self.enabled and (self.scope != "qq_social_v1" or not self.authorization_sha256):
            raise ValueError("enabled autonomy requires its explicit scope and authorization hash")
        if self.avatar_enabled and not self.asset_policy_sha256:
            raise ValueError("avatar selection requires an explicit asset review policy")
        return self


class Skip(Strict):
    kind: Literal["skip"]
    reason_code: Literal["NO_ACTION", "NO_SUITABLE_SOURCE", "KEEP_CURRENT_PROFILE"]


class Publish(Strict):
    kind: Literal["qzone_publish"]
    text: str = Field(min_length=1, max_length=5000)
    source_refs: list[Ref] = Field(min_length=1, max_length=20)

    @field_validator("source_refs")
    @classmethod
    def unique_sources(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("source references must be unique")
        return value


def _profile_text(value, *, nickname=False):
    # Match the existing account-probe plain-text contract; do not interpret text as commands.
    if (not value.strip() or any(
        ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
        or ord(c) < 32 and (nickname or c not in "\n\t") for c in value
    )):
        raise ValueError("profile value must be valid nonblank plain text")
    return value


class SetNickname(Strict):
    kind: Literal["nickname"]
    value: str = Field(min_length=1, max_length=128)

    @field_validator("value")
    @classmethod
    def plain_text(cls, value):
        return _profile_text(value, nickname=True)


class SetSignature(Strict):
    kind: Literal["signature"]
    value: str = Field(min_length=1, max_length=1000)

    @field_validator("value")
    @classmethod
    def plain_text(cls, value):
        return _profile_text(value)


class SetAvatar(Strict):
    kind: Literal["avatar"]
    asset_id: Ref


Proposal = Annotated[Skip | Publish | SetNickname | SetSignature | SetAvatar,
                     Field(discriminator="kind")]
proposal_adapter = TypeAdapter(Proposal)


class OwnerAudience(Strict):
    ugc_right: Literal[16] = 16
    target_uins: list[str] = Field(min_length=1, max_length=1)

    @field_validator("ugc_right", mode="before")
    @classmethod
    def exact_right(cls, value):
        if type(value) is not int:
            raise ValueError("visibility must be an integer")
        return value


class PublicAudience(Strict):
    ugc_right: Literal[1] = 1
    target_uins: list[str] = Field(default_factory=list, max_length=0)

    @field_validator("ugc_right", mode="before")
    @classmethod
    def exact_right(cls, value):
        if type(value) is not int:
            raise ValueError("visibility must be an integer")
        return value


class FrozenPlan(Versioned):
    """Output only: the module creates this from native evidence, never accepts it as a proposal."""

    schema_version: Literal[1, 2] = 1
    plan_id: Ref
    message_id: Ref
    delivery_key: Ref
    action_type: Action
    policy_version: int = Field(ge=1)
    policy_sha256: Sha
    authorization_sha256: Sha
    bot_qq_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    owner_qq_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    proposal: Proposal
    source_snapshot_sha256: Sha
    payload_sha256: Sha
    profile_baseline_sha256: Sha | None = None
    asset_sha256: Sha | None = None
    audience: OwnerAudience | PublicAudience | None = None
    expires_at: Timestamp

    @model_validator(mode="after")
    def public_review_version(self):
        public = isinstance(self.audience, PublicAudience)
        if (self.schema_version == 2) != public or public and self.action_type != "qzone.publish":
            raise ValueError("Public QZone plans require the reviewed v2 contract")
        return self


class ReviewedAvatar(Strict):
    """Authenticated reviewer input, not model output. Bytes remain in the Bridge catalog."""

    asset_id: Ref
    asset_sha256: Sha
    description: str = Field(min_length=1, max_length=1000)
    source_page: str = Field(min_length=1, max_length=2048)
    source_url: str = Field(min_length=1, max_length=2048)
    license_label: str = Field(min_length=1, max_length=256)
    usage_conditions: str = Field(min_length=1, max_length=2000)
    license_sha256: Sha
    review_receipt_sha256: Sha
    review_policy_sha256: Sha
    purpose: Literal["profile_avatar"]
    origin: Literal["public_download"]
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    reviewed_at: Timestamp
    expires_at: Timestamp

    @field_validator("description", "license_label", "usage_conditions")
    @classmethod
    def meaningful_review(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("review evidence must be nonblank text")
        return value

    @field_validator("source_page", "source_url")
    @classmethod
    def public_url(cls, value):
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.port not in {None, 443}
            or url.fragment or url.query or "\\" in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise ValueError("public asset provenance requires a credential-free HTTPS URL")
        if (url.hostname == "localhost"
            or url.hostname.endswith((".localhost", ".local", ".internal"))):
            raise ValueError("private asset hosts are not public provenance")
        try:
            address = ip_address(url.hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("private asset addresses are not public provenance")
        return value


class ProfileBaseline(Strict):
    """Authenticated account probe evidence; absent fields do not become empty defaults."""

    bot_qq_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    field: ProfileField
    value: str = Field(max_length=2048)
    observed_at: Timestamp
    receipt_sha256: Sha


class ActionReceipt(Versioned):
    message_id: Ref
    lease_token: Ref
    payload_sha256: Sha
    action_type: Action
    result: Literal["SUCCEEDED", "REJECTED", "UNKNOWN"]
    native_receipt_sha256: Sha | None = None
    native_retcode: int | None = None
    resource_id: str | None = Field(default=None, min_length=1, max_length=128)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")

    @model_validator(mode="after")
    def real_acceptance(self):
        if self.result != "UNKNOWN" and self.native_receipt_sha256 is None:
            raise ValueError("a known outcome requires the actual protocol receipt hash")
        if self.result == "SUCCEEDED":
            if self.native_retcode != 0:
                raise ValueError("success requires the actual protocol success code")
            if self.action_type == "qzone.publish" and not self.resource_id:
                raise ValueError("published QZone content requires the actual tid")
        if self.result == "REJECTED" and self.native_retcode in {None, 0}:
            raise ValueError("rejection requires an actual nonzero protocol rejection code")
        if self.action_type != "qzone.publish" and self.resource_id is not None:
            raise ValueError("profile acknowledgements do not manufacture message IDs")
        if self.resource_id is not None and (
            not self.resource_id.strip() or self.resource_id == "0"
            or any(ord(c) < 32 for c in self.resource_id)
        ):
            raise ValueError("a resource id must be an actual nonzero protocol identifier")
        return self
