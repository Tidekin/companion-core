"""Offline request measurement; counts never authorize I/O or settle provider charges."""

import gzip
import hashlib
from copy import deepcopy
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Literal

from pydantic import BaseModel, ConfigDict

from companion.contracts import CoreError

REVISION = "1bae0e1088993b9b82de08f59de5be5a34e42170"
ARTIFACTS = {
    "encoding.py": "13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2",
    "tokenizer.json": "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
    "tokenizer_config.json": "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547",
    "LICENSE": "f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040",
}
ARTIFACT_DIRECTORY = Path(__file__).parent / "token_artifacts" / "deepseek_v41"
TOKENIZER_GZIP_SHA256 = "d0a53c1a5e7f998a5f42b4f428888da70ed47ad33eb2a8afe32294f39f17ef5a"


class TokenMeasurementConfig(BaseModel):
    """Explicit local-render mapping, not an attestation about a hosted deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: Literal["prefer_exact", "exact_required"]
    provider: Literal["deepseek"]
    endpoint: Literal["https://api.deepseek.com"]
    model: Literal["deepseek-flash"]
    renderer: Literal["deepseek-v4.1-flash-1017"]
    revision: Literal["1bae0e1088993b9b82de08f59de5be5a34e42170"]


@dataclass(frozen=True)
class Measurement:
    payload_sha256: str
    provider: str
    endpoint: str
    model: str
    count_kind: Literal["exact_local_render", "provider_count", "estimate", "unsupported"]
    input_tokens: int | None
    reservation_margin: int
    output_allowance: int
    method: str
    reason: str | None = None
    artifact_revision: str | None = None
    artifact_hashes: dict | None = None

    @property
    def reserved_input_tokens(self):
        if self.input_tokens is None or self.count_kind == "unsupported":
            raise CoreError("MODEL_TOKEN_COUNT_UNSUPPORTED", 503)
        return self.input_tokens + self.reservation_margin

    def receipt(self):
        return {"version": 1, **asdict(self)}


def byte_estimate(messages):
    """Historical conservative text estimate, retained for unconfigured local callers."""
    return sum(len(m["content"].encode("utf-8")) + 24 for m in messages) + 32


def count_contract(settings):
    config = settings.token_measurement
    if config is None:
        return None
    return {"version": 1, **config.model_dump(mode="json"), "artifact_hashes": ARTIFACTS}


@lru_cache(maxsize=1)
def _local_encoder():
    """Load verified bytes once; no network, pickle, model weights or mutable external path."""
    try:
        from tokenizers import Tokenizer

        files = {name: (ARTIFACT_DIRECTORY / name).read_bytes()
                 for name in ARTIFACTS if name != "tokenizer.json"}
        packed = (ARTIFACT_DIRECTORY / "tokenizer.json.gz").read_bytes()
        if hashlib.sha256(packed).hexdigest() != TOKENIZER_GZIP_SHA256:
            raise ValueError("compressed artifact digest mismatch")
        files["tokenizer.json"] = gzip.decompress(packed)
        if any(hashlib.sha256(files[name]).hexdigest() != digest
               for name, digest in ARTIFACTS.items()):
            raise ValueError("artifact digest mismatch")
        # Compile the exact verified bytes, avoiding a second path read/import race.
        renderer = ModuleType("companion_deepseek_v41_encoding")
        exec(compile(files["encoding.py"], "deepseek-v41/encoding.py", "exec"), renderer.__dict__)
        tokenizer = Tokenizer.from_str(files["tokenizer.json"].decode("utf-8"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        return renderer, tokenizer
    except Exception as error:
        # Any import, parse or renderer-module failure is the same refusal: never a
        # raw crash and never a reservation from an unverified encoder.
        raise CoreError("MODEL_TOKEN_ARTIFACT_INVALID", 503) from error


def _tool_call(call):
    if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
        or call["type"] != "function" or not isinstance(call["id"], str)):
        return False
    function = call["function"]
    return (isinstance(function, dict) and set(function) == {"name", "arguments"}
            and all(isinstance(function[key], str) for key in function))


def _text_message(message):
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    if role == "tool":
        return (set(message) == {"role", "content", "tool_call_id"}
                and isinstance(message["content"], str)
                and isinstance(message["tool_call_id"], str))
    if role == "assistant" and "tool_calls" in message:
        calls = message["tool_calls"]
        return (set(message) == {"role", "content", "tool_calls"}
                and (message["content"] is None or isinstance(message["content"], str))
                and isinstance(calls, list) and bool(calls) and all(map(_tool_call, calls)))
    return (role in {"system", "user", "assistant"} and set(message) == {"role", "content"}
            and isinstance(message["content"], str))


def _tool_definition(tool):
    if (not isinstance(tool, dict) or set(tool) != {"type", "function"}
        or tool["type"] != "function"):
        return False
    function = tool["function"]
    return (isinstance(function, dict)
            and {"name", "parameters"} <= set(function) <= {
                "name", "parameters", "description", "strict"}
            and isinstance(function["name"], str) and isinstance(function["parameters"], dict)
            and ("description" not in function or isinstance(function["description"], str))
            and ("strict" not in function or type(function["strict"]) is bool))


def _exact_supported(config, provider, endpoint, payload):
    if (provider, endpoint, payload["model"]) != (
        config.provider, config.endpoint, config.model,
    ):
        return "route_mismatch"
    fields = {"model", "messages", "max_tokens", "stream", "thinking"}
    if payload.get("stream") is True:
        fields.add("stream_options")
        if payload.get("stream_options") != {"include_usage": True}:
            return "request_mode_not_verified"
    if "temperature" in payload:
        fields.add("temperature")
    if payload.get("thinking") == {"type": "enabled"}:
        fields.add("reasoning_effort")
    tools = payload.get("tools")
    if tools:
        fields |= {"tools", "tool_choice"}
        if (payload.get("tool_choice") != "auto" or not isinstance(tools, list)
            or not all(map(_tool_definition, tools))):
            return "tool_format_not_verified"
    if set(payload) != fields:
        return "request_format_not_verified"
    if (payload["thinking"] not in ({"type": "disabled"}, {"type": "enabled"})
        or payload.get("reasoning_effort", "high") not in {"low", "high", "max"}
        or type(payload["stream"]) is not bool):
        return "request_mode_not_verified"
    if not payload["messages"] or not all(map(_text_message, payload["messages"])):
        return "message_format_not_verified"
    if tools and payload["messages"][0]["role"] != "system":
        return "tool_system_mapping_not_verified"
    return None


def measure_payload(settings, payload, scenario, *, tools=(), generation=None, stream=False):
    """Measure the same complete generated payload used by admission and model transport.

    Provider-count is a distinct evidence kind; no remote backend is enabled here.
    Media and structured output stay estimates until their preprocessing is verified.
    """
    from companion import paid_budget
    from companion.model import model_payload, model_route

    messages = payload.get("messages")
    if not isinstance(messages, list) or payload != model_payload(
        settings, messages, scenario, tools=tools, generation=generation, stream=stream):
        raise CoreError("MODEL_TOKEN_PAYLOAD_INVALID", 503)
    approval = settings.paid_model
    provider, _, endpoint = model_route(settings, scenario)
    common = {"payload_sha256": paid_budget.sha(payload), "provider": provider,
              "endpoint": endpoint, "model": payload["model"],
              "output_allowance": payload["max_tokens"]}
    config = settings.token_measurement
    reason = "local_renderer_not_configured"
    if config is not None:
        reason = _exact_supported(config, provider, endpoint, payload)
        if reason is None:
            renderer, tokenizer = _local_encoder()
            try:
                rendered_messages = deepcopy(messages)
                if payload.get("tools"):
                    # The pinned upstream load_cases maps top-level OpenAI tools
                    # onto the first system message before encoding. Wire input
                    # and admission payload remain byte-for-byte unchanged.
                    rendered_messages[0]["tools"] = deepcopy(payload["tools"])
                thinking = payload["thinking"] == {"type": "enabled"}
                rendered = renderer.encode_messages(
                    rendered_messages, thinking_mode="thinking" if thinking else "chat",
                    reasoning_effort=payload.get("reasoning_effort"))
                tokens = len(tokenizer.encode(rendered, add_special_tokens=False).ids)
            except Exception as error:
                # The pinned renderer is upstream code; any failure means this input
                # has no exact count, so use the conservative estimate (or refuse).
                reason = "renderer_rejected_input"
                if config.policy == "exact_required":
                    raise CoreError("MODEL_TOKEN_COUNT_UNSUPPORTED", 503) from error
            else:
                percent = approval.input_estimate_safety_percent if approval else 100
                margin = (tokens * percent + 99) // 100 - tokens
                tool_history = payload.get("tools") or any(
                    message["role"] == "tool" or "tool_calls" in message for message in messages)
                method = ("deepseek-v41-" + ("thinking" if thinking else "chat")
                          + ("-tools-v1" if tool_history else "-v1"))
                return Measurement(**common, count_kind="exact_local_render", input_tokens=tokens,
                                   reservation_margin=margin, method=method,
                                   artifact_revision=REVISION, artifact_hashes=dict(ARTIFACTS))
        if config.policy == "exact_required":
            return Measurement(**common, count_kind="unsupported", input_tokens=None,
                               reservation_margin=0, method="none", reason=reason)
    if approval:
        base = approval.model_copy(update={"input_estimate_safety_percent": 100})
        tokens = paid_budget.estimate_input_tokens(base, messages, scenario, tools=tools)
        reserved = paid_budget.estimate_input_tokens(approval, messages, scenario, tools=tools)
        method = "utf8-third-v1"
    else:
        from companion.agent_messages import estimate_messages, tool_payload
        from companion.store import encode

        plain, _ = estimate_messages(messages)
        metadata = {"tools": tool_payload(tools), "tool_choice": "auto"} if tools else {}
        tokens = reserved = byte_estimate(plain) + (
            len(encode(metadata).encode()) if metadata else 0)
        method = "utf8-byte-v1"
    return Measurement(**common, count_kind="estimate", input_tokens=tokens,
                       reservation_margin=reserved - tokens, method=method, reason=reason)


def measure_request(settings, messages, scenario="chat", *, tools=(),
                    generation=None, stream=False):
    from companion.model import model_payload

    return measure_payload(settings, model_payload(settings, messages, scenario, tools=tools,
                           generation=generation, stream=stream), scenario, tools=tools,
                               generation=generation, stream=stream)
