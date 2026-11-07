"""Real offline FFmpeg-library extraction and bounded child-process failure cases."""

import asyncio
import hashlib
import io
import json
import sys
import time
from dataclasses import replace

import av
import pytest
from PIL import Image

from companion.video_media_contract import ClipRange, DownloadedMedia, MediaLimits
from source_reader import video_extract
from tests.support.video_extract import local_media
from tests.support.video_media_contract import clips, preview, target


def extract(tmp_path, value, selected, artifact, *, limits=None, validate=lambda: None,
            cancel_event=None):
    return asyncio.run(video_extract.extract_media(
        value, selected, limits or MediaLimits(), artifact, validate=validate,
        work_dir=tmp_path / "owned-work", cancel_event=cancel_event,
    ))


def assert_clean(tmp_path):
    work = tmp_path / "owned-work"
    assert not work.exists() or list(work.iterdir()) == []


def test_native_preview_and_clip_report_actual_pts_and_silent_bounded_encoding(tmp_path):
    value, artifact = local_media(tmp_path)
    frames = extract(tmp_path, value, preview(value), artifact)
    assert [frame.manifest.actual_start_seconds for frame in frames] == [0.2, 2.1]
    assert frames[0].manifest.requested_start_seconds == 0.15
    assert frames[0].manifest.actual_end_seconds == 0.2
    for frame in frames:
        with Image.open(io.BytesIO(frame.data)) as picture:
            assert picture.format == "PNG" and picture.size == (64, 48)
    assert_clean(tmp_path)
    media, = extract(tmp_path, value, clips(value), artifact)
    info = media.manifest
    assert info.requested_start_seconds == 0.25 and info.requested_end_seconds == 1.25
    assert info.actual_start_seconds == 0.3 and info.actual_end_seconds == 1.2
    assert info.source_pts_seconds == tuple(round(0.3 + i / 10, 1) for i in range(10))
    with av.open(io.BytesIO(media.data), format="mp4") as decoded:
        assert len(decoded.streams) == 1 and len(decoded.streams.video) == 1
        stream = decoded.streams.video[0]
        assert stream.codec_context.name == "h264"
        assert len(list(decoded.decode(video=0))) == info.frame_count == 10
        assert float(stream.duration * stream.time_base) == info.output_duration_seconds
    assert artifact.sha256 == hashlib.sha256((tmp_path / "source.mp4").read_bytes()).hexdigest()
    assert_clean(tmp_path)


def test_native_stream_origin_is_distinct_from_verified_upstream_range(tmp_path):
    value, artifact = local_media(tmp_path, origin=20, offset=10)
    frame, = extract(tmp_path, value, preview(value, (10.25,)), artifact)
    assert frame.manifest.actual_start_seconds == 10.3
    assert frame.manifest.requested_start_seconds == 10.25
    assert_clean(tmp_path)


def test_native_webm_source_is_decoded_locally_and_reencoded_as_silent_h264(tmp_path):
    value, artifact = local_media(tmp_path, container_format="webm")
    clip, = extract(tmp_path, value, clips(value), artifact)
    assert clip.manifest.actual_start_seconds == 0.3 and clip.manifest.actual_end_seconds == 1.2
    with av.open(io.BytesIO(clip.data), format="mp4") as result:
        assert len(result.streams) == 1 and result.streams.video[0].codec_context.name == "h264"
    assert_clean(tmp_path)


@pytest.mark.parametrize("change", ["hash", "size", "input_limit"])
def test_changed_input_is_rejected_before_starting_any_child(tmp_path, monkeypatch, change):
    value, artifact = local_media(tmp_path)
    limits = MediaLimits()
    if change == "hash":
        artifact = replace(artifact, sha256="f" * 64)
    elif change == "size":
        artifact = replace(artifact, byte_length=artifact.byte_length + 1)
    else:
        limits = replace(limits, max_input_bytes=1024)

    async def forbidden(*args, **kwargs):
        raise AssertionError("input was not validated before process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises((ValueError, video_extract.MediaExtractionError)):
        extract(tmp_path, value, preview(value), artifact, limits=limits)
    assert_clean(tmp_path)


@pytest.mark.parametrize("address", ["https://synthetic.invalid/movie.ts", "file:///outside.ts"])
def test_playlist_disguised_as_mp4_cannot_resolve_protocols(tmp_path, address):
    path, value = tmp_path / "playlist.mp4", target()
    data = ("#EXTM3U\n#EXTINF:3,\n" + address + "\n").encode()
    path.write_bytes(data)
    artifact = DownloadedMedia(value.sha256, str(path), hashlib.sha256(data).hexdigest(), len(data),
                               "video/mp4", ClipRange(0, 3))
    with pytest.raises(video_extract.MediaExtractionError, match="MEDIA_INPUT_INVALID"):
        extract(tmp_path, value, preview(value), artifact)
    assert path.read_bytes() == data
    assert_clean(tmp_path)


def test_custom_readonly_avio_disables_protocols_and_external_movie_references():
    calls = []

    class AV:
        @staticmethod
        def open(stream, **kwargs):
            calls.append((stream, kwargs))
            return "container"

    incoming = io.BytesIO(b"bounded downloaded bytes")
    assert video_extract._open_media(AV, incoming, "video/mp4") == "container"
    assert calls == [(incoming, {"mode": "r", "format": "mov", "options": {
        "protocol_whitelist": "", "enable_drefs": "0", "use_absolute_path": "0",
    }})]


@pytest.mark.parametrize("case", ["decode", "output", "timeline"])
def test_native_decoder_enforces_resource_and_verified_timeline_bounds(tmp_path, case):
    value, artifact = local_media(tmp_path)
    limits, selected = MediaLimits(), preview(value, (2.1,))
    if case == "decode":
        limits = replace(limits, max_decoded_frames=1)
    elif case == "output":
        limits, selected = replace(limits, max_asset_bytes=1024), clips(value)
    else:
        value = replace(value, duration_seconds=4)
        artifact = replace(artifact, target_sha256=value.sha256, source_range=ClipRange(0, 4))
        selected = preview(value)
    with pytest.raises(video_extract.MediaExtractionError):
        extract(tmp_path, value, selected, artifact, limits=limits)
    assert_clean(tmp_path)


@pytest.mark.parametrize("kind", ["cancel", "revoked"])
def test_live_revalidation_stops_a_real_child_and_removes_all_owned_material(
    tmp_path, monkeypatch, kind,
):
    value, artifact = local_media(tmp_path)
    original, children = asyncio.create_subprocess_exec, []

    async def capture(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    async def scenario():
        cancelled, calls = asyncio.Event(), 0

        def validate():
            nonlocal calls
            calls += 1
            if calls >= 3:
                if kind == "cancel":
                    cancelled.set()
                else:
                    raise RuntimeError("source grant revoked")

        with pytest.raises((video_extract.MediaExtractionError, RuntimeError)):
            await video_extract.extract_media(value, preview(value), MediaLimits(), artifact,
                validate=validate, cancel_event=cancelled, work_dir=tmp_path / "owned-work")
    asyncio.run(scenario())
    assert len(children) == 1 and children[0].returncode is not None
    assert_clean(tmp_path)


@pytest.mark.parametrize("failure", ["timeout", "stdout", "stderr"])
def test_supervisor_terminates_stalled_or_unbounded_child_output(tmp_path, monkeypatch, failure):
    value, artifact = local_media(tmp_path)
    original, children = asyncio.create_subprocess_exec, []
    program = "import time; time.sleep(30)" if failure == "timeout" else (
        "import sys,time; sys." + failure + ".write('x'*1000000); sys."
        + failure + ".flush(); time.sleep(30)")

    async def substitute(*args, **kwargs):
        child = await original(sys.executable, "-c", program, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", substitute)
    limits = replace(MediaLimits(), timeout_seconds=0.3 if failure == "timeout" else 5,
                     max_process_output_bytes=1024)
    code = "MEDIA_EXTRACTION_TIMEOUT" if failure == "timeout" else "MEDIA_PROCESS_OUTPUT_LIMIT"
    started = time.monotonic()
    with pytest.raises(video_extract.MediaExtractionError, match=code):
        extract(tmp_path, value, preview(value), artifact, limits=limits)
    assert time.monotonic() - started < 4
    assert len(children) == 1 and children[0].returncode is not None
    assert_clean(tmp_path)


def test_unsupported_resource_control_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(video_extract.MediaExtractionError,
                       match="MEDIA_RESOURCE_LIMIT_UNAVAILABLE"):
        video_extract._resource_limits(MediaLimits())


def test_extracted_manifest_contains_no_local_path_or_raw_metadata(tmp_path):
    value, artifact = local_media(tmp_path)
    frame, = extract(tmp_path, value, preview(value, (0.1,)), artifact)
    from dataclasses import asdict

    serialized = json.dumps(asdict(frame.manifest))
    assert str(tmp_path) not in serialized and artifact.path not in repr(artifact)
    assert "canonical_url" not in serialized and "text" not in serialized


def test_initial_authorization_callback_shares_the_total_deadline(tmp_path, monkeypatch):
    value, artifact = local_media(tmp_path)

    async def stalled():
        await asyncio.sleep(30)

    async def forbidden(*args, **kwargs):
        raise AssertionError("an unvalidated source must not start a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(video_extract.MediaExtractionError, match="MEDIA_EXTRACTION_TIMEOUT"):
        extract(tmp_path, value, preview(value), artifact,
                limits=replace(MediaLimits(), timeout_seconds=0.1), validate=stalled)
    assert_clean(tmp_path)


def test_nonregular_source_is_rejected_before_open_or_process_creation(tmp_path, monkeypatch):
    value, artifact = local_media(tmp_path)
    artifact = replace(artifact, path=str(tmp_path))

    async def forbidden(*args, **kwargs):
        raise AssertionError("a nonregular source must not start a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(video_extract.MediaExtractionError, match="MEDIA_INPUT_CHANGED"):
        extract(tmp_path, value, preview(value), artifact)
    assert_clean(tmp_path)


def test_reader_finishes_without_importing_the_core_evidence_module(tmp_path, monkeypatch):
    value, artifact = local_media(tmp_path)
    monkeypatch.setitem(sys.modules, "companion.video_evidence", None)
    frame, = extract(tmp_path, value, preview(value, (0.15,)), artifact)
    assert frame.manifest.actual_start_seconds == 0.2
    assert_clean(tmp_path)
