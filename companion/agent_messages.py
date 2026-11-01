"""Pure Pydantic AI 2.60 message mappings for the existing paid HTTP channel.

The SDK owns tool schemas, argument validation, call/result pairing and execution.
These functions neither execute tools nor perform I/O. The pinned SDK owns
message and tool types; Core retains admission and delivery responsibilities.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.messages import ModelResponse as RuntimeResponse
    from pydantic_ai.tools import ToolDefinition

    from companion.model import ModelResponse


def tool_payload(tools: Sequence[ToolDefinition | dict] = ()):
    """Map SDK-owned declarations; a previously mapped wire list remains unchanged."""
    result = []
    for tool in tools:
        if isinstance(tool, dict):
            result.append(deepcopy(tool))
            continue
        from pydantic_ai.tools import ToolDefinition

        if not isinstance(tool, ToolDefinition):
            raise TypeError("expected an SDK tool definition or its wire declaration")
        function = {"name": tool.name, "parameters": deepcopy(tool.parameters_json_schema)}
        if tool.description is not None:
            function["description"] = tool.description
        if tool.strict is not None:
            function["strict"] = tool.strict
        result.append({"type": "function", "function": function})
    return result


def _user_content(content):
    from pydantic_ai.messages import BinaryContent, ImageUrl, TextContent

    if isinstance(content, str):
        return content
    result = []
    for item in content:
        if isinstance(item, (str, TextContent)):
            result.append({"type": "text", "text": item if isinstance(item, str) else item.content})
        elif isinstance(item, BinaryContent) and item.is_image:
            result.append({"type": "image_url", "image_url": {"url": item.data_uri}})
        elif isinstance(item, ImageUrl):
            result.append({"type": "image_url", "image_url": {"url": item.url}})
        else:
            raise ValueError("unsupported content in the existing model input contract")
    return result


def from_wire_messages(messages) -> list[ModelMessage]:
    """Import existing Core context; bytes are parsed by the SDK, never downloaded."""
    from pydantic_ai.messages import (
        BinaryContent,
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        UserPromptPart,
    )

    result = []
    for message in messages:
        role, content = message["role"], message["content"]
        if set(message) != {"role", "content"}:
            raise ValueError("only existing plain Core context can be imported")
        if role == "system" and isinstance(content, str):
            result.append(ModelRequest([SystemPromptPart(content)]))
        elif role == "assistant" and isinstance(content, str):
            result.append(ModelResponse([TextPart(content)]))
        elif role == "user":
            if isinstance(content, list):
                parts = []
                for item in content:
                    if item["type"] == "text" and set(item) == {"type", "text"}:
                        parts.append(item["text"])
                    elif (item["type"] == "image_url" and set(item) == {"type", "image_url"}
                          and set(item["image_url"]) == {"url"}
                          and item["image_url"]["url"].startswith("data:image/")):
                        parts.append(BinaryContent.from_data_uri(item["image_url"]["url"]))
                    else:
                        raise ValueError("unsupported existing Core content part")
                content = parts
            elif not isinstance(content, str):
                raise ValueError("unsupported existing Core user content")
            result.append(ModelRequest([UserPromptPart(content)]))
        else:
            raise ValueError("unsupported existing Core context role")
    return result


def to_wire_messages(messages: Sequence[ModelMessage]):
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        RetryFeedbackPart,
        RetryPromptPart,
        SystemPromptPart,
        TextPart,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    result = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    result.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart):
                    result.append({"role": "user", "content": _user_content(part.content)})
                elif isinstance(part, ToolReturnPart):
                    if part.files:
                        raise ValueError("multimodal tool results are outside the input contract")
                    result.append({"role": "tool", "tool_call_id": part.tool_call_id,
                                   "content": part.model_response_str()})
                elif isinstance(part, RetryPromptPart):
                    result.append({"role": "tool", "tool_call_id": part.tool_call_id,
                                   "content": part.model_response()}
                                  if part.tool_name else
                                  {"role": "user", "content": part.model_response()})
                elif isinstance(part, RetryFeedbackPart):
                    result.append({"role": "user", "content": part.model_response()})
                else:
                    raise ValueError("unsupported runtime request part")
        elif isinstance(message, ModelResponse):
            texts, calls = [], []
            for part in message.parts:
                if isinstance(part, TextPart):
                    texts.append(part.content)
                elif isinstance(part, ToolCallPart):
                    calls.append({"id": part.tool_call_id, "type": "function", "function": {
                        "name": part.tool_name, "arguments": part.args_as_json_str()}})
                elif isinstance(part, ThinkingPart):
                    continue  # Diagnostic content stays outside the existing input contract.
                else:
                    raise ValueError("unsupported runtime response part")
            value = {"role": "assistant", "content": "".join(texts) if texts else None}
            if calls:
                value["tool_calls"] = calls
            elif value["content"] is None:
                value["content"] = ""
            result.append(value)
        else:
            raise TypeError("expected SDK model messages")
    return result


def to_runtime_response(response: ModelResponse, *, model_name: str,
                        provider_name: str | None = None) -> RuntimeResponse:
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
    from pydantic_ai.usage import RequestUsage

    parts = [TextPart(response.text)] if response.text else []
    if response.reasoning_content:
        parts.insert(0, ThinkingPart(response.reasoning_content, provider_name=provider_name))
    parts.extend(ToolCallPart(call["function"]["name"], call["function"]["arguments"],
                              tool_call_id=call["id"]) for call in response.tool_calls)
    usage = RequestUsage(input_tokens=response.input_tokens or 0,
                         output_tokens=response.output_tokens or 0,
                         cache_read_tokens=response.cached_tokens,
                         unmeasured_requests=int(response.input_tokens is None
                                                 or response.output_tokens is None))
    return ModelResponse(parts, model_name=model_name, provider_name=provider_name, usage=usage,
        provider_details={"reasoning": {
            "state": "RECORDED" if response.reasoning_content else "NOT_RETURNED",
            "truncated": response.reasoning_truncated}}, finish_reason=response.finish_reason)


def request_wire(messages, parameters):
    """The same resolved instructions and messages for planning and model dispatch."""
    return [*({"role": "system", "content": part.content}
              for part in parameters.instruction_parts or []), *to_wire_messages(messages)]


def estimate_messages(messages, tools=()):
    """Map only for the existing estimator; never rewrite the actual provider payload."""
    result = []
    for message in messages:
        if "tool_calls" in message or message.get("role") == "tool":
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ValueError("tool messages cannot carry owner image parts")
            result.append({"role": "assistant" if message["role"] == "assistant" else "user",
                           "content": json.dumps(message, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":"), allow_nan=False)})
        else:
            result.append(dict(message))
    catalog = tool_payload(tools)
    metadata = {"tools": catalog, "tool_choice": "auto"} if catalog else {}
    size = (len(json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8"))
            if metadata else 0)
    return result, (size + 2) // 3
