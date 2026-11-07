"""Frozen source identity and bounded immutable extraction requests."""

from dataclasses import FrozenInstanceError, replace

import pytest

from companion.video_media_contract import (
    ClipRange,
    DownloadedMedia,
    MediaLimits,
    validate_selection,
)
from tests.support.video_media_contract import clips, preview, target


def test_frozen_target_keeps_one_content_task_and_exact_fingerprints():
    value = target()
    selected = preview(value)
    assert value.task_id == "content:" + value.content_item_id
    assert selected.target_sha256 == value.sha256
    assert replace(value, cid=124).sha256 != value.sha256
    assert replace(selected, plan_sha256="c" * 64).sha256 != selected.sha256
    with pytest.raises(FrozenInstanceError):
        value.cid = 456


@pytest.mark.parametrize("patch", [
    {"canonical_url": "https://secret@www.bilibili.com/video/BV1234567890"},
    {"canonical_url": "https://www.bilibili.com/video/BV1234567890?token=secret"},
    {"canonical_url": "file:///video.mp4"}, {"canonical_url": "https://127.0.0.1/video"},
    {"bvid": "BVXXXXXXXXXX"}, {"cid": True}, {"part": 2},
    {"duration_seconds": float("nan")}, {"duration_seconds": float("inf")},
    {"duration_seconds": True}, {"source_sha256": "not-a-hash"},
    {"content_item_id": "fresh-job-to-reset-budget"},
])
def test_target_rejects_foreign_identity_protocols_and_nonfinite_times(patch):
    with pytest.raises(ValueError):
        replace(target(), **patch)


@pytest.mark.parametrize("times", [(0.1, 0.1), (2, 1), (True,), (float("nan"),), [0.1]])
def test_preview_is_ordered_bounded_and_immutable(times):
    with pytest.raises(ValueError):
        preview(target(), times)


def test_selection_cannot_cross_downloaded_range_or_reset_its_target(tmp_path):
    value = target(20)
    downloaded = DownloadedMedia(value.sha256, str(tmp_path / "verified.mp4"), "c" * 64,
                                 2048, "video/mp4", ClipRange(10, 13))
    limits = MediaLimits()
    validate_selection(value, preview(value, (10.5, 12)), limits, downloaded)
    for selected in (preview(value, (0.5,)), preview(value, (13,)), clips(value, ((9, 11),))):
        with pytest.raises(ValueError):
            validate_selection(value, selected, limits, downloaded)
    with pytest.raises(ValueError):
        validate_selection(value, replace(preview(value, (11,)), target_sha256="d" * 64), limits)


def test_phase_and_task_resource_limits_cannot_be_evaded_by_more_assets():
    value = target(100)
    with pytest.raises(ValueError):
        validate_selection(value, preview(value, (1, 2, 3, 4, 5)), MediaLimits())
    with pytest.raises(ValueError):
        validate_selection(value, clips(value, ((0, 20), (30, 50))), MediaLimits())
    with pytest.raises(ValueError):
        clips(value, ((0, 10), (9, 20)))
    with pytest.raises(ValueError):
        replace(MediaLimits(), max_memory_bytes=True)
    with pytest.raises(ValueError):
        replace(MediaLimits(), timeout_seconds=float("nan"))
    with pytest.raises(ValueError):
        replace(preview(value, (1,)), clips=(ClipRange(0, 1),))


@pytest.mark.parametrize("path,mime", [
    ("https://cdn.invalid/movie.mp4", "video/mp4"), ("relative.mp4", "video/mp4"),
    ("C:/movie.m3u8", "application/vnd.apple.mpegurl"),
])
def test_download_contract_never_admits_a_network_or_playlist_input(path, mime):
    with pytest.raises(ValueError):
        DownloadedMedia(target().sha256, path, "c" * 64, 2048, mime, ClipRange(0, 3))
