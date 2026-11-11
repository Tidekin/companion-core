"""Real provider chunks cross the mature SDK before the provider reaches its end."""

import asyncio
import json

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta

from companion.agent_runtime import ControlledModel
from companion.config import Settings
from companion.model import ModelAdmission, ModelClient, ModelResponseError, model_payload
from companion.model_stream import consume
from companion.paid_budget import sha
from tests.conftest import run
from tests.support.model_stream import chunk


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts, self.closed = parts, False

    async def __aiter__(self):
        for part in self.parts:
            yield part

    async def aclose(self):
        self.closed = True


def test_sse_utf8_comments_usage_thinking_and_tools_are_distinct():
    async def scenario():
        data = b": keepalive\r\n\r\n" + chunk({"reasoning_content": "private reasoning"})
        data += chunk({"content": "你好🐟"}) + chunk({"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function",
            "function": {"name": "recall", "arguments": '{"q":'}}]})
        data += chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"茶"}'}}]})
        data += chunk(finish="tool_calls") + chunk(usage={
            "prompt_tokens": 40, "completion_tokens": 12, "prompt_cache_hit_tokens": 3})
        data += b"data: [DONE]\r\n\r\n"
        stream = Chunks([data[i:i + 1] for i in range(len(data))])
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        seen = []
        async def observe(delta):
            seen.append(delta)
        result = await consume(response, observe)
        assert result.text == "你好🐟" and result.reasoning_content == "private reasoning"
        assert (result.input_tokens, result.output_tokens, result.cached_tokens) == (40, 12, 3)
        assert result.tool_calls[0]["function"] == {"name": "recall", "arguments": '{"q":"茶"}'}
        assert {d.kind for d in seen} == {"thinking", "text", "tool"}
        assert stream.closed
    run(scenario())


def test_provider_eof_keeps_received_usage_but_does_not_complete():
    async def scenario():
        response = httpx.Response(200, headers={"content-type": "text/event-stream"},
            stream=Chunks([chunk({"content": "unfinished"}), chunk(usage={
                "prompt_tokens": 4, "completion_tokens": 2})]))
        with pytest.raises(ModelResponseError, match="MODEL_STREAM_INCOMPLETE") as error:
            await consume(response, None)
        assert error.value.response.text == "unfinished"
        assert error.value.response.output_tokens == 2
    run(scenario())


@pytest.mark.parametrize('data,tools', [
    (chunk(finish='stop') + chunk({'content': 'late'}), True),
    (chunk({'tool_calls': [{'index': 0, 'id': 'call', 'function': {
        'name': 'unadvertised', 'arguments': '{}'}}]}), False),
])
def test_invalid_stream_envelope_is_not_accepted(data, tools):
    async def scenario():
        response = httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  stream=Chunks([data + b'data: [DONE]\n\n']))
        with pytest.raises(ModelResponseError, match='MODEL_INVALID_RESPONSE'):
            await consume(response, None, tools=tools)
    run(scenario())


def test_admission_binds_stream_mode_and_sdk_receives_first_text_before_end():
    async def scenario():
        cfg = Settings(mode="live", model_key="synthetic-model-key", owner_qq_id="10001",
                       bot_qq_id="10002", bridge_token="synthetic-bridge-token-24-characters",
                       database="unused")
        arrived, release = asyncio.Event(), asyncio.Event()
        requests = []
        class Gated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield chunk({"content": "first "})
                await asyncio.wait_for(release.wait(), 5)
                yield chunk({"content": "last"})
                yield chunk(finish="stop")
                yield chunk(usage={"prompt_tokens": 4, "completion_tokens": 2})
                yield b"data: [DONE]\n\n"
        def provider(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Gated())
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            async def complete(wire, *, tools, on_delta=None):
                return await ModelClient(cfg, http).complete(wire, "chat", tools=tools,
                    stream=True, on_delta=on_delta, admission=ModelAdmission(sha(
                        model_payload(cfg, wire, "chat", tools=tools, stream=True))))
            seen = []
            async def events(ctx, stream):
                async for event in stream:
                    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                        seen.append(event.part.content)
                        arrived.set()
                    elif (isinstance(event, PartDeltaEvent)
                          and isinstance(event.delta, TextPartDelta)):
                        seen.append(event.delta.content_delta)
            agent = Agent(ControlledModel(cfg.model_name, complete, stream_complete=complete))
            task = asyncio.create_task(agent.run("synthetic", event_stream_handler=events))
            await asyncio.wait_for(arrived.wait(), 5)
            assert not task.done() and seen == ["first "]
            release.set()
            result = await task
            assert result.output == "first last" and len(requests) == 1
            assert requests[0]["stream"] is True
            assert requests[0]["stream_options"] == {"include_usage": True}
            with pytest.raises(Exception, match="MODEL_ADMISSION_REQUIRED"):
                await ModelClient(cfg, http).complete([{"role": "user", "content": "x"}], "chat",
                    stream=True, admission=ModelAdmission(sha(model_payload(
                        cfg, [{"role": "user", "content": "x"}], "chat"))))
            assert len(requests) == 1
    run(scenario())


def test_buffered_text_preserves_sdk_tool_boundary_and_final_result():
    async def scenario():
        cfg = Settings(mode='live', model_key='synthetic-model-key', owner_qq_id='10001',
            bot_qq_id='10002', bridge_token='synthetic-bridge-token-24-characters',
            database='unused')
        requests, executed, seen = [], [], []

        def provider(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                parts = [chunk({'content': value}) for value in ['A', 'B']]
                parts += [chunk({'reasoning_content': 'private reasoning'}),
                    chunk({'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {
                        'name': 'echo', 'arguments': '{"value":'}}]}),
                    chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '"x"}'}}]}),
                    chunk(finish='tool_calls')]
            else:
                parts = [chunk({'content': value}) for value in ['C', 'D']]
                parts.append(chunk(finish='stop'))
            parts += [chunk(usage={'prompt_tokens': 40, 'completion_tokens': 12}),
                      b'data: [DONE]\n\n']
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  stream=Chunks(parts))

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            async def complete(wire, *, tools, on_delta=None):
                return await ModelClient(cfg, http).complete(wire, 'chat', tools=tools,
                    stream=True, on_delta=on_delta, admission=ModelAdmission(sha(
                        model_payload(cfg, wire, 'chat', tools=tools, stream=True))))

            agent = Agent(ControlledModel(cfg.model_name, complete, stream_complete=complete))

            @agent.tool_plain
            def echo(value: str) -> str:
                executed.append(value)
                return 'returned:' + value

            async def observe(ctx, events):
                async for event in events:
                    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                        seen.append(event.part.content)
                    elif (isinstance(event, PartDeltaEvent)
                          and isinstance(event.delta, TextPartDelta)):
                        seen.append(event.delta.content_delta)

            result = await agent.run('synthetic', event_stream_handler=observe)
            assert result.output == 'CD' and executed == ['x'] and len(requests) == 2
            assert ''.join(seen) == 'ABCD'
            assert any(item.get('role') == 'tool' and 'returned:x' in item.get('content', '')
                       for item in requests[1]['messages'])
    run(scenario())
