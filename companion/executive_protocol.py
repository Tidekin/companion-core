"""Storage-free contracts shared by Core and the separately deployed executor."""

import hashlib
import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolArguments(Strict):
    path: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=12000)
    prompt: str = Field(default="", max_length=12000)


class ToolRequest(Strict):
    idempotency_key: str = Field(min_length=8, max_length=128)
    capability: Literal["windows.read_text", "windows.write_text", "dsh.run"]
    workspace: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: ToolArguments
    source_event_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def arguments_match(self):
        args = self.arguments
        if self.capability == "dsh.run":
            if not args.prompt.strip() or args.path or args.text:
                raise ValueError("dsh.run accepts only a prompt")
        elif not args.path or args.prompt or (self.capability == "windows.read_text" and args.text):
            raise ValueError("file operation arguments do not match the capability")
        return self


class ToolDecision(Strict):
    decision: Literal["APPROVE", "REJECT"]
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutorEndpoint(Strict):
    url: str
    token: SecretStr
    workspace: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    capabilities: list[Literal["windows.read_text", "windows.write_text", "dsh.run"]]
    max_model_calls: int = Field(default=1, ge=1, le=10)
    max_input_bytes: int = Field(default=16000, ge=1024, le=100000)
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)

    @model_validator(mode="after")
    def private_endpoint(self):
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("executor URL must be HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("executor URL must not contain credentials or query")
        # Plain HTTP is only acceptable on loopback or explicitly private IPs.
        if parsed.scheme == "http" and parsed.hostname != "localhost":
            try:
                addr = ipaddress.ip_address(parsed.hostname)
            except ValueError as error:
                raise ValueError("HTTP executor requires a private IP") from error
            if not (addr.is_private or addr in ipaddress.ip_network("100.64.0.0/10")):
                raise ValueError("HTTP executor requires a private IP")
        if len(self.token.get_secret_value()) < 24:
            raise ValueError("executor token must be at least 24 characters")
        return self


class ExecutiveConfig(Strict):
    enabled: bool = False
    allow_changes: bool = False
    daily_calls: int = Field(default=0, ge=0, le=100)
    timeout_seconds: int = Field(default=45, ge=5, le=180)
    endpoints: dict[str, ExecutorEndpoint] = Field(default_factory=dict)


class ExecutionEnvelope(Strict):
    schema_version: Literal[1] = 1
    task_id: str
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_target_sha256: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    capability: Literal["windows.read_text", "windows.write_text", "dsh.run"]
    workspace: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: ToolArguments
    max_model_calls: int = Field(ge=1, le=10)
    max_input_bytes: int = Field(ge=1024, le=100000)
    max_output_tokens: int = Field(ge=128, le=4096)
    timeout_seconds: int = Field(ge=5, le=180)


class ToolReceipt(Strict):
    schema_version: Literal[1] = 1
    task_id: str
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    text: str = Field(default="", max_length=12000)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    billed_micro_usd: int = Field(ge=0)
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    unknown_model_calls: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")

    @model_validator(mode="after")
    def evidence_matches(self):
        if self.unknown_model_calls > self.model_calls:
            raise ValueError("unknown usage must belong to a dispatched model call")
        if hashlib.sha256(self.text.encode()).hexdigest() != self.evidence_sha256:
            raise ValueError("receipt evidence digest mismatch")
        if self.status != "SUCCEEDED" and self.text:
            raise ValueError("unsuccessful receipt cannot claim content")
        if self.status == "SUCCEEDED" and not self.text.strip():
            raise ValueError("successful receipt requires real result content")
        return self
