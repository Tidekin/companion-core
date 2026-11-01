"""Typed configuration for local voice nodes; no prompts, media, or cloud billing."""

import hashlib
import re
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from companion.voice_protocol import Digest, NodeId, digest


class VoiceNode(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True
    )

    enabled: bool = Field(default=True, strict=True)
    token: SecretStr = Field(repr=False)
    manifest_sha256: Digest
    capabilities: tuple[Literal["tts", "asr"], ...] = ("tts", "asr")
    max_tts_chars: int = Field(default=1000, ge=1, le=4000, strict=True)
    max_tts_output_bytes: int = Field(default=4_000_000, ge=44, le=4_000_000, strict=True)
    max_tts_seconds: float = Field(default=60, gt=0, le=60, strict=True, allow_inf_nan=False)
    max_asr_input_bytes: int = Field(default=8_000_000, ge=44, le=8_000_000, strict=True)
    max_asr_seconds: float = Field(default=120, gt=0, le=120, strict=True, allow_inf_nan=False)
    max_asr_segments: int = Field(default=128, ge=1, le=128, strict=True)
    max_asr_chars: int = Field(default=12000, ge=1, le=12000, strict=True)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        token = value.get_secret_value()
        if not 32 <= len(token) <= 256 or any(
            ord(char) <= 32 or ord(char) >= 127 for char in token
        ):
            raise ValueError("Voice node token must be an independent printable credential")
        return value

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value):
        if not value or len(value) != len(set(value)):
            raise ValueError("Voice node capabilities must be nonempty and unique")
        return tuple(sorted(value))


class VoiceReplyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    enabled: bool = Field(default=True, strict=True)
    mode: Literal["model", "voice_input", "auto"] = "model"
    max_chars: int = Field(default=400, ge=1, le=4000, strict=True)
    wait_seconds: int = Field(default=15, ge=1, le=120, strict=True)
    proactive: bool = Field(default=False, strict=True)


class VoiceASRPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    enabled: bool = Field(default=True, strict=True)
    wait_seconds: int = Field(default=20, ge=1, le=120, strict=True)
    max_records: int = Field(default=2, ge=1, le=4, strict=True)
    max_context_chars: int = Field(default=4000, ge=128, le=12000, strict=True)


class VoiceConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True
    )

    enabled: bool = Field(default=False, strict=True)
    signing_private_key: SecretStr | None = Field(default=None, repr=False)
    nodes: dict[NodeId, VoiceNode] = Field(default_factory=dict, max_length=8)
    job_ttl_seconds: int = Field(default=90, ge=5, le=600, strict=True)
    lease_ttl_seconds: int = Field(default=30, ge=1, le=120, strict=True)
    result_ttl_seconds: int = Field(default=90, ge=1, le=300, strict=True)
    max_pending_jobs: int = Field(default=32, ge=1, le=256, strict=True)
    max_private_blob_bytes: int = Field(default=32_000_000, ge=1024, le=256_000_000, strict=True)
    reply: VoiceReplyPolicy = Field(default_factory=VoiceReplyPolicy)
    asr: VoiceASRPolicy = Field(default_factory=VoiceASRPolicy)

    @field_validator("signing_private_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None and not re.fullmatch(r"[a-f0-9]{64}", value.get_secret_value()):
            raise ValueError("Voice signing key must be a raw Ed25519 private key in hex")
        return value

    @model_validator(mode="after")
    def complete_configuration(self):
        tokens = [node.token.get_secret_value() for node in self.nodes.values()]
        if len(tokens) != len(set(tokens)):
            raise ValueError("Each voice node must have its own credential")
        if self.enabled and (
            self.signing_private_key is None
            or not any(node.enabled for node in self.nodes.values())
        ):
            raise ValueError("Enabled voice requires a signing key and an enabled node")
        return self

    def private_key(self) -> Ed25519PrivateKey:
        if self.signing_private_key is None:
            raise ValueError("VOICE_SIGNING_KEY_REQUIRED")
        return Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(self.signing_private_key.get_secret_value())
        )

    def node_snapshot(self, node_id: str) -> dict:
        node = self.nodes[node_id]
        return {
            "node_id": node_id,
            "node": node.model_dump(mode="json", exclude={"token"}),
            "policy": self.model_dump(mode="json", exclude={"nodes", "signing_private_key"}),
            "credential_sha256": hashlib.sha256(node.token.get_secret_value().encode()).hexdigest(),
            "signing_public_key": self.private_key().public_key().public_bytes_raw().hex(),
        }

    def node_fingerprint(self, node_id: str) -> str:
        return digest(self.node_snapshot(node_id))

    def authority_sha256(self) -> str:
        return digest(
            {
                "policy": self.model_dump(mode="json", exclude={"nodes", "signing_private_key"}),
                "nodes": {
                    node_id: self.node_fingerprint(node_id) for node_id in sorted(self.nodes)
                },
            }
        )
