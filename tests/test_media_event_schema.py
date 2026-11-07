"""Published inbound schema accepts the native media projection without widening trust."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from companion.contracts import InboundEvent
from companion_onebot_bridge.event_normalizer import normalize
from tests.support.onebot_events import config, raw_event
from tests.support.onebot_files import segment
from tests.support.onebot_images import image_segment
from tests.support.qq_face_images import face


def validator():
    schema = json.loads((Path(__file__).parents[1] / "contracts/event.v1.schema.json")
                        .read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def projected(segments):
    raw = raw_event(message=segments)
    return normalize(json.dumps(raw), config(), now=raw["time"],
                     rich_input_scope="references_v1", image_scope="owner_image_v1",
                     file_scope="owner_file_v1")


@pytest.mark.parametrize("segments", [
    [segment(), face(), image_segment(), face("999999")],
    [segment()] * 64,
    [image_segment()] * 64,
    [{"type": "file", "data": {"name": "来源.md", "id": "opaque-id"}}],
])
def test_native_media_projection_matches_both_contracts(segments):
    value = projected(segments)
    validator().validate(value)
    assert InboundEvent.model_validate(value)


@pytest.mark.parametrize("changes", [
    {"filename": "../outside.docx"}, {"filename": "C:outside.txt"},
    {"filename": "nested\\file.txt"}, {"filename": ".."}, {"filename": " "},
    {"filename": "bad\x85.txt"}, {"file_id": "bad\x00id"}, {"file_id": " "},
    {"size": 2**53}, {"size": True}, {"actor": "owner"},
    {"download_url": "https://private.invalid/untrusted"},
])
def test_published_file_contract_rejects_paths_controls_and_untrusted_authority(changes):
    value = projected([segment()])
    value["payload"]["rich"]["segments"][0].update(changes)
    assert list(validator().iter_errors(value))
    with pytest.raises(ValidationError):
        InboundEvent.model_validate(value)
