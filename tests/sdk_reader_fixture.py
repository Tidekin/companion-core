"""Synthetic provider and independent Core connections for SDK Reader tests."""

import json
from contextlib import asynccontextmanager

import httpx

from companion.core import Core
from companion.model import ModelClient


def answer(text="", *, calls=()):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = list(calls)
    return {
        "choices": [{"message": message,
                     "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def response(body, *, stream=False):
    if not stream:
        return httpx.Response(200, json=body)
    message = body["choices"][0]["message"]
    if "tool_calls" in message:
        message = {**message, "tool_calls": [
            {**item, "index": index} for index, item in enumerate(message["tool_calls"])]}
    chunks = [{"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason":
                  body["choices"][0]["finish_reason"]}]}, {"choices": [], "usage": body["usage"]}]
    data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return httpx.Response(200, text=data + "data: [DONE]\n\n",
                          headers={"Content-Type": "text/event-stream"})


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        assert request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        self.requests.append(json.loads(request.content))
        assert self.responses, "The harness repeated an already completed provider request"
        body = self.responses.pop(0)
        if isinstance(body, httpx.Response):
            return body
        return response(body, stream=self.requests[-1].get("stream", False))


@asynccontextmanager
async def worker_for(core, provider):
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
        with Core(core.settings, ModelClient(core.settings, model_http), core.clock) as worker:
            assert worker.store is not core.store
            yield worker

