"""Signed reference media reuse the original bounded attachment boundary."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from companion import qq_files, qq_images, qq_rich
from companion.contracts import InboundEvent
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.reference_reader import captured_media, enrich, restore
from tests.conftest import run
from tests.support.onebot_events import config, raw_event
from tests.test_onebot_images import image_segment
from tests.test_onebot_reference_reader import Protocol, forward, node, reply, text


def captured():
    media = [image_segment(), {"type": "file", "data": {
        "file_id": "private-file", "file": "notes.txt", "file_size": 12}},
        {"type": "json", "data": {"data": json.dumps({
            "title": "A source", "url": "https://b23.tv/abc"})}},
        {"type": "face", "data": {"id": "14"}}]
    message = [reply(message=media), image_segment(), text("分析引用内容"), forward()]
    event = normalize(json.dumps(raw_event(message=message)), config(),
                      rich_input_scope="references_v1", image_scope="owner_image_v1",
                      file_scope="owner_file_v1")
    baseline = deepcopy(event)
    originals = []
    protocol = Protocol({("forward", "shared"): {"messages": [node("Alice", image_segment())]}})
    run(enrich(event, message, protocol, lambda value: value,
               image_scope=True, file_scope=True, originals=originals))
    return event, baseline, message, originals, protocol


def test_nested_originals_reconstruct_without_refetch_and_keep_top_slots():
    event, baseline, message, originals, protocol = captured()
    InboundEvent.model_validate(event)
    schema = json.loads(Path("contracts/event.v1.schema.json").read_text("utf8"))
    Draft202012Validator(schema).validate(event)
    images, files = captured_media(event, message, originals)
    assert protocol.calls == [("forward", "shared")]
    assert images[0].path == () and images[0].index == 1
    assert images[1].path == (0, 0)
    assert images[-1].path == (3, 0, 0)
    assert [image.slot for image in images] == list(range(len(images)))
    assert len(files) == 1 and files[0].path == (0, 1)
    assert qq_images.manifest(event["payload"]) == [image.projection() for image in images]
    assert qq_files.manifest(event["payload"]) == [file.projection() for file in files]
    assert any(item["type"] == "json" for item in qq_rich.media_segments(event["payload"]["rich"]))
    assert restore(baseline, event["payload"]["rich"]) == event


@pytest.mark.parametrize("change", ["path", "origin", "raw", "missing", "duplicate"])
def test_captured_reference_media_cannot_change_at_approval(change):
    event, _, message, originals, _ = captured()
    if change == "path":
        originals[0]["path"] = [0, 63]
    elif change == "origin":
        originals[0]["origin"] = {**originals[0]["origin"], "reference": "999"}
    elif change == "raw":
        originals[0]["segment"]["data"]["url"] = "https://gchat.qpic.cn/changed"
    elif change == "missing":
        originals.pop()
    else:
        originals.append(originals[0])
    with pytest.raises(ValueError, match="CAPTURE_MEDIA_CHANGED"):
        captured_media(event, message, originals)


@pytest.mark.parametrize("change", ["path", "slot", "origin", "body", "duplicate"])
def test_http_rejects_invalid_nested_attachment_contract(change):
    event, *_ = captured()
    body = event["payload"]["rich"]["segments"][0]["body"]
    item = body["media"][0]
    if change == "path":
        item["path"][0] = 1
    elif change == "slot":
        item["slot"] = 0
    elif change == "origin":
        item["origin"]["reference"] = "https://external.example"
    elif change == "body":
        item["body"] = deepcopy(body)
    else:
        body["media"].append(deepcopy(item))
    with pytest.raises(ValueError):
        qq_rich.validate(event["payload"]["rich"])
