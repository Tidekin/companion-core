"""Caption answers are normalized only where meaning cannot change, then strictly parsed."""

import json

import pytest
from pydantic import ValidationError

from companion.meme_caption_output import failure_detail, normalize, unfence
from companion_meme_protocol import CaptionResult, Sampling, parse

SHA = "a" * 64


def sampling(frames):
    return Sampling.model_validate_json(json.dumps({
        "total_frames": frames, "duration_ms": 0 if frames == 1 else 900,
        "coverage": "complete",
        "frames": [{"frame_index": i, "timestamp_ms": i * 100, "sample_sha256": SHA}
                   for i in range(frames)]}))


def answer(**changes):
    return {"description": "一只猫在挥手", "visible_text": "", "tone": "playful",
            "target": "neutral", "suitable": ["like", "playful"], "avoid": ["sleep"],
            "use_when": ["打招呼时"], "avoid_when": ["严肃场合"], "tags": ["猫"],
            "confidence": 0.9, "content_status": "clear", "temporal_status": "static",
            **changes}


def result(raw, frames=1):
    shape = sampling(frames)
    return parse(CaptionResult, {**normalize(raw, shape),
                                 "sampled_frames": shape.model_dump(mode="json")})


def test_core_known_frame_count_decides_static():
    assert result(answer(temporal_status="consistent")).temporal_status == "static"
    assert result(answer(temporal_status="static"), frames=3).temporal_status == "consistent"
    assert result(answer(temporal_status="changes"), frames=3).temporal_status == "changes"


def test_labels_are_deduplicated_unknown_ones_dropped_and_avoid_wins():
    value = result(answer(suitable=["like", "like", "happy", "sleep"], avoid=["sleep", "x"]))
    assert value.suitable == ("like",) and value.avoid == ("sleep",)


def test_long_list_items_are_cut_to_the_field_limit():
    value = result(answer(tags=["标" * 40, "标" * 40, " "], use_when=["场" * 100]))
    assert value.tags == ("标" * 32,) and value.use_when == ("场" * 80,)


def test_integer_confidence_and_a_fenced_answer_are_accepted():
    assert result(answer(confidence=1)).confidence == 1.0
    fenced = "```json\n" + json.dumps(answer(), ensure_ascii=False) + "\n```"
    assert json.loads(unfence(fenced))["tone"] == "playful"


def test_meaningful_violations_still_fail_and_report_only_fields():
    with pytest.raises(ValidationError) as caught:
        result(answer(tone="not-a-tone", description=""))
    detail = failure_detail(caught.value)
    assert {item["loc"] for item in detail["errors"]} >= {"tone", "description"}
    assert "not-a-tone" not in json.dumps(detail)


def test_the_input_is_not_mutated():
    raw = answer(suitable=["like", "like"])
    normalize(raw, sampling(1))
    assert raw["suitable"] == ["like", "like"]
