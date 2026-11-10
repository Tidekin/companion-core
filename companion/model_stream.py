"""Provider SSE decoding only; admission, persistence and delivery belong to callers."""

from dataclasses import dataclass

import httpx
from httpx_sse import EventSource, SSEError

from companion.model import (
    MAX_REASONING_CHARS,
    ModelResponse,
    ModelResponseError,
    ModelTransportError,
)


@dataclass(frozen=True)
class ModelDelta:
    kind: str
    text: str = ""
    index: int = 0
    name: str | None = None
    call_id: str | None = None


async def consume(response, on_delta, *, tools=True):
    received = ModelResponse("")
    calls = {}
    terminal = False
    done = False
    total = 0

    async def emit(delta):
        if on_delta is not None:
            await on_delta(delta)

    try:
        async for event in EventSource(response).aiter_sse():
            if event.data == "[DONE]":
                done = True
                break
            if not event.data:
                continue
            total += len(event.data)
            if total > 2 * 1024 * 1024:
                raise ValueError("stream too large")
            body = event.json()
            if not isinstance(body, dict) or body.get("error") is not None:
                raise ValueError("invalid provider stream")
            usage = body.get("usage")
            if usage is not None:
                if not isinstance(usage, dict):
                    raise ValueError("invalid usage")
                prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
                cached = usage.get("prompt_cache_hit_tokens", 0)
                received.cache_usage_present = "prompt_cache_hit_tokens" in usage
                if all(type(x) is int and x >= 0 for x in (prompt, output, cached)):
                    received.input_tokens, received.output_tokens = prompt, output
                    received.cached_tokens = cached
                else:
                    received.input_tokens = received.output_tokens = None
                    received.cached_tokens = 0
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise ValueError("invalid choices")
            if not choices:
                continue  # The provider's final usage-only chunk.
            choice = choices[0]
            if terminal:
                raise ValueError("choice after terminal")
            if not isinstance(choice, dict) or choice.get("index") != 0:
                raise ValueError("invalid choice index")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("invalid delta")
            reason = choice.get("finish_reason")
            if reason is not None:
                if terminal or reason not in {"stop", "length", "content_filter", "tool_calls"}:
                    raise ValueError("invalid terminal")
                terminal = True
                received.finish_reason = reason if reason != "tool_calls" else None
            if delta.get("role") not in {None, "assistant"}:
                raise ValueError("invalid role")
            text = delta.get("content")
            if text is not None:
                if not isinstance(text, str) or len(received.text) + len(text) > 32000:
                    raise ValueError("invalid content")
                received.text += text
                if text:
                    await emit(ModelDelta("text", text))
            thinking = delta.get("reasoning_content")
            if thinking is not None:
                if not isinstance(thinking, str):
                    raise ValueError("invalid reasoning")
                prior = received.reasoning_content or ""
                kept = thinking[:max(0, MAX_REASONING_CHARS - len(prior))]
                received.reasoning_content = prior + kept
                received.reasoning_truncated |= len(kept) < len(thinking)
                if kept:
                    await emit(ModelDelta("thinking", kept))
            tool_deltas = delta.get("tool_calls")
            if tool_deltas is not None:
                if not isinstance(tool_deltas, list) or tool_deltas and not tools:
                    raise ValueError("invalid tools")
                for tool in tool_deltas:
                    if (not isinstance(tool, dict) or type(tool.get("index")) is not int
                            or not 0 <= tool["index"] < 128
                            or tool.get("type") not in {None, "function"}):
                        raise ValueError("invalid tool delta")
                    index = tool["index"]
                    function = tool.get("function", {})
                    if not isinstance(function, dict):
                        raise ValueError("invalid tool function")
                    call_id, name = tool.get("id"), function.get("name")
                    arguments = function.get("arguments")
                    if any(value is not None and not isinstance(value, str)
                           for value in (call_id, name, arguments)):
                        raise ValueError("invalid tool strings")
                    call = calls.setdefault(index, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                    call["id"] += call_id or ""
                    call["function"]["name"] += name or ""
                    call["function"]["arguments"] += arguments or ""
                    await emit(ModelDelta("tool", arguments or "", index, name, call_id))
        received.tool_calls = tuple(calls[index] for index in sorted(calls))
        if not done or not terminal:
            raise ModelResponseError("MODEL_STREAM_INCOMPLETE", 502, received)
        if any(not call["id"] or not call["function"]["name"] for call in received.tool_calls):
            raise ValueError("incomplete tool")
        if not received.text.strip() and not received.tool_calls:
            raise ModelResponseError("MODEL_EMPTY_RESPONSE", 502, received)
        return received
    except (ValueError, TypeError, KeyError, SSEError) as error:
        raise ModelResponseError("MODEL_INVALID_RESPONSE", 502, received) from error
    except (httpx.TimeoutException, httpx.TransportError) as error:
        if received.input_tokens is not None and received.output_tokens is not None:
            raise ModelResponseError("MODEL_STREAM_INCOMPLETE", 502, received) from error
        raise ModelTransportError("MODEL_RETRYABLE", 503, type(error).__name__) from error
    finally:
        await response.aclose()
