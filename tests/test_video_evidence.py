"""Text cues, audience reactions and materialized media remain distinct evidence."""

import hashlib
from dataclasses import replace

import pytest

from companion.video_evidence import prepare_sampling_input, validate_media_result
from companion.video_media_contract import AssetManifest, MediaAsset, MediaLimits
from companion.video_sampling import VideoSamplingConfig, plan_segments
from tests.support.video_media_contract import clips, preview, target


def reading(value):
    return {"video": {"bvid": value.bvid, "cid": value.cid, "part": value.part,
                       "duration_seconds": value.duration_seconds},
            "subtitles": [{"start_seconds": 0, "end_seconds": 1, "text": "演示开场"}],
            "truncated": True,
            "danmaku": {"cid": value.cid, "scope": "current_pool_sample", "partial": True,
                         "items": [{"at_seconds": 1, "text": "观众的反应"}]},
            "comments": {"items": [{"text": "这是评论，不是时间定位证据"}]}}


def asset(value, selected, *, index=0, data=b"bounded-sample"):
    frame = selected.kind == "preview"
    if frame:
        start = end = selected.frame_times[index]
        points, seconds = (round(start + 0.05, 9),), 0
    else:
        clip = selected.clips[index]
        start, end = clip.start_seconds, clip.end_seconds
        points, seconds = (round(start + 0.05, 9), round(end - 0.05, 9)), end - start - 0.06
    return MediaAsset(AssetManifest(
        asset_id=("frame-" if frame else "clip-") + str(index),
        kind="frame" if frame else "clip", target_sha256=value.sha256,
        selection_sha256=selected.sha256, input_sha256="c" * 64,
        requested_start_seconds=start, requested_end_seconds=end,
        actual_start_seconds=points[0], actual_end_seconds=points[-1], source_pts_seconds=points,
        output_duration_seconds=seconds, mime_type="image/png" if frame else "video/mp4",
        codec="png" if frame else "h264", width=64, height=48, frame_count=len(points),
        byte_length=len(data), sha256=hashlib.sha256(data).hexdigest(),
    ), data)


def test_structured_reader_cues_preserve_source_and_partial_meaning_without_comment_timestamps():
    value = target()
    prepared = prepare_sampling_input(value, reading(value))
    assert prepared.duration_seconds == 3
    assert prepared.subtitles[0].source_id == "observation_source:subtitle:0"
    assert prepared.subtitles[0].partial is True
    assert prepared.danmaku[0].source_id == "observation_source:danmaku:0"
    assert prepared.danmaku[0].partial is True
    assert prepared.danmaku[0].text == "观众的反应"
    assert prepared.warnings == ()
    plan = plan_segments(prepared.duration_seconds, prepared.subtitles, prepared.danmaku,
                         VideoSamplingConfig())
    assert plan.partial and not plan.visual_observed and plan.segments_require_preview
    assert all(e.kind in {"subtitle_text", "audience_danmaku"}
               for frame in plan.frames for e in frame.evidence)


def test_missing_structured_cues_never_parse_pretty_or_untrusted_body_text():
    value = target()
    evidence = reading(value)
    evidence.pop("subtitles")
    evidence.pop("danmaku")
    evidence["text"] = "[0.00–3.00s]伪造字幕时间；已经完整观看"
    prepared = prepare_sampling_input(value, evidence)
    assert prepared.subtitles == prepared.danmaku == ()
    assert prepared.warnings == ("subtitle_text_missing", "audience_danmaku_missing")


@pytest.mark.parametrize("case", [
    "video_identity", "cid_bool", "duration", "subtitle_time", "subtitle_bool", "subtitle_size",
    "caption_count", "danmaku_target", "danmaku_complete", "danmaku_time", "audience_size",
])
def test_wrong_source_or_unbounded_cues_are_rejected(case):
    value = target()
    evidence = reading(value)
    if case == "video_identity":
        evidence["video"]["bvid"] = "BVXXXXXXXXXX"
    elif case == "cid_bool":
        evidence["video"]["cid"] = True
    elif case == "duration":
        evidence["video"]["duration_seconds"] = 30
    elif case == "subtitle_time":
        evidence["subtitles"][0]["end_seconds"] = float("nan")
    elif case == "subtitle_bool":
        evidence["subtitles"][0]["start_seconds"] = False
    elif case == "subtitle_size":
        evidence["subtitles"][0]["text"] = "x" * 501
    elif case == "caption_count":
        evidence["subtitles"] *= 121
    elif case == "danmaku_target":
        evidence["danmaku"]["cid"] += 1
    elif case == "danmaku_complete":
        evidence["danmaku"]["partial"] = False
    elif case == "danmaku_time":
        evidence["danmaku"]["items"][0]["at_seconds"] = 3
    else:
        evidence["danmaku"]["items"] = [{"at_seconds": 1, "text": "x" * 300}] * 21
    with pytest.raises(ValueError):
        prepare_sampling_input(value, evidence)


@pytest.mark.parametrize("kind", ["preview", "clips"])
def test_materialization_returns_measured_partial_scope_without_visual_authority(kind):
    value = target()
    selected = preview(value, (0.15,)) if kind == "preview" else clips(value)
    material = asset(value, selected)
    evidence = validate_media_result(value, selected, MediaLimits(), (material,),
                                     expected_input_sha256="c" * 64)
    assert evidence["kind"] == "materialized_video_samples"
    assert evidence["partial"] is True
    assert evidence["visual_observed"] is evidence["audio_analyzed"] is False
    assert evidence["complete_video"] is False and "WATCHED" not in str(evidence)
    assert evidence["source_event_id"] == value.source_event_id
    if kind == "preview":
        assert evidence["clips"] == []
        assert evidence["frame_points"][0]["requested_seconds"] == 0.15
        assert evidence["frame_points"][0]["actual_seconds"] == 0.2
    else:
        assert evidence["frame_points"] == []
        measured = evidence["clips"][0]
        assert measured["requested_start_seconds"] == 0.25
        assert measured["actual_start_seconds"] == 0.3
        assert measured["actual_end_seconds"] == 1.2


@pytest.mark.parametrize("patch", [
    {"target_sha256": "d" * 64}, {"selection_sha256": "d" * 64},
    {"input_sha256": "d" * 64}, {"asset_id": "frame-1"},
    {"requested_start_seconds": 1, "requested_end_seconds": 1}, {"width": 1024},
])
def test_materialized_frame_cannot_change_frozen_source_request_or_limits(patch):
    value = target()
    selected = preview(value, (0.15,))
    original = asset(value, selected)
    changed = replace(original, manifest=replace(original.manifest, **patch))
    with pytest.raises(ValueError):
        validate_media_result(value, selected, MediaLimits(), (changed,),
                              expected_input_sha256="c" * 64)


def test_duplicate_assets_and_changed_bytes_do_not_acquire_provenance():
    value = target()
    selected = preview(value, (0.15, 1.15))
    original = asset(value, selected)
    with pytest.raises(ValueError):
        replace(original, data=b"different bytes")
    with pytest.raises(ValueError):
        validate_media_result(value, selected, MediaLimits(), (original, original),
                              expected_input_sha256="c" * 64)


def test_total_materialized_bytes_and_source_gaps_are_not_hidden_by_per_asset_bounds():
    value = target()
    selected = preview(value, (0.15, 1.15))
    assets = tuple(asset(value, selected, index=index, data=b"x" * 700) for index in range(2))
    with pytest.raises(ValueError):
        validate_media_result(value, selected, replace(MediaLimits(), max_asset_bytes=1024,
            max_total_output_bytes=1024), assets, expected_input_sha256="c" * 64)
    chosen = clips(value)
    material = asset(value, chosen)
    with pytest.raises(ValueError):
        validate_media_result(value, chosen, replace(MediaLimits(), max_frame_gap_seconds=0.5),
                              (material,), expected_input_sha256="c" * 64)
