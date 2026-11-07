import json

from companion.contracts import InboundEvent
from companion_onebot_bridge import reference_reader as reader
from companion_onebot_bridge.event_normalizer import normalize, redact
from tests.conftest import run
from tests.support.onebot_events import config, raw_event


def text(value):
    return {"type": "text", "data": {"text": value}}


def reply(reference="-42", **data):
    return {"type": "reply", "data": {"id": reference, **data}}


def forward(reference="shared", **data):
    return {"type": "forward", "data": {"id": reference, **data}}


def node(name, *message):
    return {"sender": {"nickname": name}, "message": list(message)}


def prepared(message):
    return normalize(json.dumps(raw_event(message=message)), config(),
                     rich_input_scope="references_v1")


class Protocol:
    def __init__(self, responses=None):
        self.responses, self.calls = responses or {}, []

    async def get_msg(self, reference):
        return await self.read("reply", reference)

    async def get_forward_msg(self, reference):
        return await self.read("forward", reference)

    async def read(self, kind, reference):
        self.calls.append((kind, reference))
        value = self.responses.get((kind, reference), ValueError("INVALID_ONEBOT_ACCEPTANCE"))
        if isinstance(value, Exception):
            raise value
        return value


def enriched(message, responses=None):
    event, protocol = prepared(message), Protocol(responses)
    run(reader.enrich(event, message, protocol, lambda value: redact(value, config())))
    InboundEvent.model_validate(event)
    return event, protocol
