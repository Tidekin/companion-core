"""Pydantic AI's native stream interface over the existing admitted completion."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from pydantic_ai.models import StreamedResponse

from companion.agent_messages import to_runtime_response
from companion.model_stream import ModelDelta


class ControlledStream(StreamedResponse):
    def __init__(self, parameters, complete, wire, tools, model_name):
        super().__init__(parameters)
        self.complete, self.wire, self.tools = complete, wire, tools
        self._name = model_name
        self._timestamp = datetime.now(UTC)
        self._task = None
        self._queue = asyncio.Queue(maxsize=8)

    @property
    def model_name(self):
        return self._name

    @property
    def provider_name(self):
        return "companion-controlled"

    @property
    def provider_url(self):
        return None

    @property
    def timestamp(self):
        return self._timestamp

    async def _produce(self):
        try:
            result = await self.complete(self.wire, tools=self.tools,
                                         on_delta=self._queue.put)
            await self._queue.put(result)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            await self._queue.put(error)

    async def _get_event_iterator(self):
        self._task = asyncio.create_task(self._produce())
        emitted = False
        while True:
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            if not isinstance(item, ModelDelta):
                response = to_runtime_response(item, model_name=self.model_name,
                                               provider_name=self.provider_name)
                self._usage = response.usage
                self.finish_reason = response.finish_reason
                self.provider_details = response.provider_details
                if not emitted:
                    for part in response.parts:
                        yield self._parts_manager.handle_part(vendor_part_id=None, part=part)
                break
            emitted = True
            if item.kind == "text":
                for event in self._parts_manager.handle_text_delta(
                    vendor_part_id="text", content=item.text):
                    yield event
            elif item.kind == "thinking":
                for event in self._parts_manager.handle_thinking_delta(
                    vendor_part_id="thinking", content=item.text,
                    provider_name=self.provider_name):
                    yield event
            else:
                event = self._parts_manager.handle_tool_call_delta(
                    vendor_part_id=("tool", item.index), tool_name=item.name,
                    args=item.text, tool_call_id=item.call_id)
                if event is not None:
                    yield event

    async def close_stream(self):
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
