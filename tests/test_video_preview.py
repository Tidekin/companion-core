"""Generated animation previews retain later frames within the original allowance."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from companion import video_input
from companion.contracts import CoreError
from companion.image_input import text_and_estimate
from tests.support.animated_owner_images import policy
from tests.support.video_preview import COLORS, FORMATS, animation


@pytest.mark.parametrize("format,mime", FORMATS)
def test_blank_first_frame_and_ten_ms_transitions_are_visible_in_one_image(format, mime):
    prepared, animated = video_input.preview(animation(format), mime, policy())
    assert animated and prepared.mime == "image/png"
    assert "第1,2,3,4帧" in prepared.note
    assert "0,80,90,100" in prepared.note and "180ms" in prepared.note
    assert "understand_video" in prepared.note
    with Image.open(io.BytesIO(prepared.data)) as sheet:
        assert sheet.size == (144, 48) and sheet.n_frames == 1
        assert [sheet.getpixel(point) for point in [(24, 12), (72, 12), (120, 12), (24, 36)]] == [
            (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prepared.note}, prepared.part(),
    ]}]
    assert text_and_estimate(policy(), messages)[1] == 100


def test_more_than_six_frames_report_selected_indices_and_source_times():
    prepared, animated = video_input.preview(animation("GIF", count=7), "image/gif", policy())
    assert animated and "共7帧" in prepared.note and "第1,2,3,5,6,7帧" in prepared.note
    assert "0,80,90,180,300,700" in prepared.note
    assert "仅为抽样画面" in prepared.note and "不能声称看过完整动画" in prepared.note


def test_fractional_apng_timestamps_match_the_original_timeline():
    prepared, _ = video_input.preview(animation("PNG", fractional=True), "image/apng", policy())
    assert "0,1.4,2.8,4.2" in prepared.note and "5.6ms" in prepared.note


def test_apng_poster_is_excluded_from_the_sheet_and_timeline():
    prepared, animated = video_input.preview(animation("PNG", poster=True), "image/apng", policy())
    assert animated and "共4帧" in prepared.note and "180ms" in prepared.note
    with Image.open(io.BytesIO(prepared.data)) as sheet:
        assert sheet.getpixel((24, 12)) == (255, 255, 255)
        assert sheet.getpixel((120, 12)) == (0, 255, 0)


@pytest.mark.parametrize("format,mime", FORMATS)
def test_disposal_and_alpha_are_composited_before_ordered_sampling(format, mime):
    frames = [Image.new("RGBA", (12, 8), (0, 0, 0, 0)) for _ in range(4)]
    for index, frame in enumerate(frames):
        frame.paste(COLORS[index + 1], (index * 2, 0, index * 2 + 2, 4))
    output = io.BytesIO()
    options = ({"disposal": [1, 2, 1, 1]} if format == "GIF" else
               {"disposal": [0, 1, 2, 0], "blend": [0, 1, 1, 0]} if format == "PNG" else {})
    frames[0].save(output, format=format, save_all=True, append_images=frames[1:],
                   duration=100, loop=0, optimize=False, lossless=True, **options)
    prepared, animated = video_input.preview(output.getvalue(), mime, policy())
    assert animated
    with Image.open(io.BytesIO(output.getvalue())) as original, Image.open(
        io.BytesIO(prepared.data),
    ) as sheet:
        for index in range(4):
            original.seek(index)
            original.load()
            rgba = original.convert("RGBA")
            expected = Image.new("RGB", rgba.size, "white")
            expected.paste(rgba, mask=rgba)
            cell = sheet.crop(((index % 3) * 12, (index // 3) * 8,
                               (index % 3 + 1) * 12, (index // 3 + 1) * 8))
            assert cell.tobytes() == expected.tobytes()


def test_complete_sheet_has_one_512_edge_and_revalidates_image_allowance():
    original = animation("PNG", count=6, size=(512, 256))
    image_policy = policy().model_copy(update={"max_pixels_per_image": 1_000_000})
    prepared, animated = video_input.preview(original, "image/apng", image_policy)
    assert animated and len(prepared.data) <= image_policy.max_bytes_per_image
    with Image.open(io.BytesIO(prepared.data)) as sheet:
        assert max(sheet.size) == 512
        assert sheet.width * sheet.height <= image_policy.max_pixels_per_image
    text_and_estimate(image_policy, [{"role": "user", "content": [
        {"type": "text", "text": prepared.note}, prepared.part(),
    ]}])


@pytest.mark.parametrize("limit", ["bytes", "pixels", "frames", "decoded", "mime"])
def test_original_bounds_are_not_relaxed_by_sampling(limit):
    original, mime, image_policy = animation("GIF"), "image/gif", policy()
    if limit == "bytes":
        image_policy = image_policy.model_copy(update={"max_bytes_per_image": len(original) - 1})
    elif limit == "pixels":
        image_policy = image_policy.model_copy(update={"max_pixels_per_image": 48 * 24 - 1})
    elif limit in {"frames", "decoded"}:
        from tests.support.animated_owner_images import animation as many_frames

        original = many_frames(count=241 if limit == "frames" else 65,
                               size=(12, 8) if limit == "frames" else (1000, 1000))
        image_policy = image_policy.model_copy(update={"max_pixels_per_image": 1_000_000,
                                                       "max_bytes_per_image": 4_000_000})
    else:
        mime = "image/png"
    with pytest.raises(CoreError, match="CONTENT_VIDEO_INPUT_INVALID"):
        video_input.preview(original, mime, image_policy)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "100", None])
def test_invalid_duration_in_an_unsampled_frame_still_rejects(monkeypatch, value):
    original_animation = video_input._animation

    def malformed(*args):
        image, count, start = original_animation(*args)
        seek = image.seek

        def seek_with_invalid_duration(index):
            seek(index)
            if index == 3:  # Frame 4 is omitted by the seven-frame sample.
                image.info["duration"] = value

        image.seek = seek_with_invalid_duration
        return image, count, start

    monkeypatch.setattr(video_input, "_animation", malformed)
    with pytest.raises(CoreError, match="CONTENT_VIDEO_INPUT_INVALID"):
        video_input.preview(animation("GIF", count=7), "image/gif", policy())


def test_static_image_keeps_the_original_result_and_animated_flag():
    from companion.image_frames import prepare

    output = io.BytesIO()
    Image.new("RGB", (20, 10), "orange").save(output, format="PNG")
    original = output.getvalue()
    prepared, animated = video_input.preview(original, "image/png", policy())
    assert not animated and prepared == prepare(original, "image/png", policy())


def test_large_static_preview_resizes_only_the_model_copy():
    import random

    original = io.BytesIO()
    image = Image.frombytes("RGB", (192, 128), random.Random(32).randbytes(192 * 128 * 3))
    image.save(original, format="PNG")
    data = original.getvalue()
    image_policy = policy().model_copy(update={"max_bytes_per_image": 5000,
                                               "max_pixels_per_image": 192 * 128})
    preview, animated = video_input.preview(data, "image/png", image_policy,
                                           original_limit=len(data))
    assert not animated and preview.mime == "image/jpeg"
    assert len(preview.data) <= 5000 < len(data)
    with Image.open(io.BytesIO(preview.data)) as decoded:
        assert decoded.format == "JPEG" and 1 < decoded.width < image.width
        decoded.load()


def test_final_sheet_byte_limit_is_checked_after_resize(monkeypatch):
    from companion import image_frames

    output = io.BytesIO()
    Image.new("RGB", (600, 200), "red").save(output, format="PNG")
    monkeypatch.setattr(image_frames, "prepare",
                        lambda *_, **__: SimpleNamespace(data=output.getvalue()))
    image_policy = policy().model_copy(update={"max_bytes_per_image": len(animation("WEBP"))})
    with pytest.raises(CoreError, match="CONTENT_VIDEO_INPUT_INVALID"):
        video_input.preview(animation("WEBP"), "image/webp", image_policy)
