"""Bounded, deterministic frame samples; signed originals remain the source of truth."""

import base64
import io
import math
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps

from companion.contracts import CoreError

FORMATS = {"image/png": "PNG", "image/apng": "PNG", "image/jpeg": "JPEG",
           "image/webp": "WEBP", "image/gif": "GIF"}
MAX_FRAMES = 240
MAX_DECODED_PIXELS = 64_000_000
SAMPLE_FRAMES = 6


@dataclass(frozen=True, repr=False)
class PreparedImage:
    data: bytes
    mime: str
    note: str = ""

    def part(self):
        return {"type": "image_url", "image_url": {
            "url": f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii"),
        }}


def _compressed_static(image, limit):
    """Preserve orientation and full resolution where the model byte allowance permits."""
    rgba = ImageOps.exif_transpose(image).convert("RGBA")
    output = io.BytesIO()
    rgba.info.clear()
    rgba.save(output, format="PNG", compress_level=1)
    if output.tell() <= limit:
        return PreparedImage(output.getvalue(), "image/png")

    rgb = Image.new("RGB", rgba.size, "white")
    rgb.paste(rgba, mask=rgba.getchannel("A"))
    while True:
        output = io.BytesIO()
        rgb.save(output, format="JPEG", quality=88)
        if output.tell() <= limit:
            return PreparedImage(output.getvalue(), "image/jpeg")
        if rgb.size == (1, 1):
            raise CoreError("CONTENT_IMAGE_TOO_LARGE", 413)
        ratio = min(0.8, math.sqrt(limit / output.tell()))
        rgb = rgb.resize(tuple(max(1, int(side * ratio)) for side in rgb.size),
                         Image.Resampling.LANCZOS)


def prepare(data, mime, policy, *, original_limit=None):
    """Represent each animation by one ordered sheet, within its existing image allowance.

    Pillow composites GIF/APNG disposal and WebP alpha before each selected frame.
    Cap the complete decode work, output pixels and bytes before provider admission.
    """
    limit = policy.max_bytes_per_image if original_limit is None else original_limit
    if not data or len(data) > limit:
        raise CoreError("CONTENT_IMAGE_TOO_LARGE", 413)
    if mime not in FORMATS:
        raise CoreError("CONTENT_IMAGE_TYPE_UNSUPPORTED", 415)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                count = getattr(image, "n_frames", 1)
                if (image.format != FORMATS[mime] or min(image.size) < 1
                    or max(image.size) > 4096
                    or image.width * image.height > policy.max_pixels_per_image
                    or count > MAX_FRAMES
                    or image.width * image.height * count > MAX_DECODED_PIXELS):
                    raise ValueError()
                if count == 1 and mime not in {"image/gif", "image/apng"}:
                    image.verify()
                    with Image.open(io.BytesIO(data)) as original:
                        original.load()
                        if len(data) > policy.max_bytes_per_image:
                            return _compressed_static(original, policy.max_bytes_per_image)
                    return PreparedImage(data, mime)
                # APNG may carry a poster outside its animation timeline.
                start = int(bool(image.info.get("default_image", False)))
                frames = count - start
                selected = sorted({start + round(i * (frames - 1) / max(1, min(frames, 6) - 1))
                                   for i in range(min(frames, SAMPLE_FRAMES))})
                columns, rows = min(3, len(selected)), math.ceil(len(selected) / 3)
                scale = min(1, 512 / max(image.size), math.sqrt(
                    policy.max_pixels_per_image / (columns * rows * image.width * image.height),
                ))
                width, height = max(1, int(image.width * scale)), max(1, int(image.height * scale))
                sheet = Image.new("RGB", (columns * width, rows * height), "white")
                if sheet.width * sheet.height > policy.max_pixels_per_image:
                    raise ValueError()
                elapsed, times = 0, []
                for index in range(count):
                    image.seek(index)
                    image.load()
                    if index in selected:
                        frame = image.convert("RGBA").resize(
                            (width, height), Image.Resampling.LANCZOS,
                        )
                        slot = len(times)
                        sheet.paste(frame, ((slot % columns) * width, (slot // columns) * height),
                                    frame)
                        times.append(elapsed)
                    if index >= start:
                        duration = image.info.get("duration", 0)
                        if not isinstance(duration, (int, float)) or not math.isfinite(duration):
                            raise ValueError()
                        elapsed += max(0, int(duration))
                for _ in range(8):
                    output = io.BytesIO()
                    sheet.save(output, format="PNG")
                    if len(output.getvalue()) <= policy.max_bytes_per_image:
                        break
                    sheet = sheet.resize((max(1, sheet.width // 2), max(1, sheet.height // 2)),
                                         Image.Resampling.LANCZOS)
                else:
                    raise ValueError()
                note = (f"[动图抽样：{FORMATS[mime]}共{frames}帧，抽取第"
                        + ",".join(str(index - start + 1) for index in selected)
                        + "帧；对应起始时间(ms)=" + ",".join(map(str, times))
                        + "。拼图按从左到右、从上到下排列，仅为抽样画面，不能声称看过完整动画。"
                        "表情包这类短动图看拼图通常就能接话，不必调用 understand_video；"
                        "主人问到具体动作或拼图确实看不懂时再调用。]")
                return PreparedImage(output.getvalue(), "image/png", note)
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Warning):
        raise CoreError("CONTENT_IMAGE_INVALID", 400) from None
