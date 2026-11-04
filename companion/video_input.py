"""Approved inline video and bounded animation conversion; no network or storage."""

import base64
import binascii
import io
import math
import re
import warnings
from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated
from urllib.parse import urlsplit

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from companion.contracts import CoreError
from companion.image_frames import FORMATS, MAX_DECODED_PIXELS, MAX_FRAMES, PreparedImage

SCENARIOS = {"content_video", "content_video_preview"}


class VideoProvider(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    endpoint: str
    input_rate: int = Field(strict=True, gt=0)
    cached_input_rate: int = Field(strict=True, gt=0)
    output_rate: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def valid_endpoint(self):
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("video endpoint requires HTTPS without credentials/query/fragment")
        if self.cached_input_rate > self.input_rate:
            raise ValueError("video cached rate cannot exceed input rate")
        return self



class ApprovedVideoProvider(VideoProvider):
    credential_tag: str = Field(pattern=r"^[a-f0-9]{64}$")


class VideoCandidate(VideoProvider):
    key: SecretStr = Field(min_length=1, max_length=4096)

    def approval(self, window_id):
        import hashlib
        import hmac

        from companion.store import encode

        tag = hmac.new(self.key.get_secret_value().encode(),
            encode([window_id, self.provider, self.endpoint]).encode(), hashlib.sha256).hexdigest()
        return ApprovedVideoProvider(**self.model_dump(exclude={"key"}), credential_tag=tag)


ProfileName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")]


class VideoModels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selected: ProfileName | None = None
    candidates: dict[ProfileName, VideoCandidate] = Field(default_factory=dict, max_length=16)

    def selected_route(self, paid):
        if self.selected is None:
            return None
        route = self.candidates.get(self.selected)
        if (not route or not paid or not paid.video_input
                or paid.video_routes.get(self.selected) != route.approval(paid.window_id)):
            raise ValueError("video model requires paid authorization")
        return route


class VideoInputPolicy(VideoProvider):
    approval_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_output_tokens: int = Field(default=1000, strict=True, ge=128, le=4096)
    max_context_tokens: int | None = Field(
        default=None, strict=True, gt=0, le=1000000, exclude_if=lambda value: value is None,
    )
    timeout_seconds: int = Field(default=45, strict=True, ge=5, le=60)
    estimated_tokens_per_second: int = Field(strict=True, ge=1, le=10000)
    estimated_tokens_per_image: int = Field(default=1024, strict=True, ge=1, le=1000000)
    max_seconds: int = Field(default=30, strict=True, ge=1, le=60)
    max_bytes: int = Field(default=4_000_000, strict=True, ge=1024, le=16_000_000)

TOOL = {"type": "function", "function": {
    "name": "understand_video",
    "description": "按需分析本次主人发送的动图的完整动作、时序和表情。只接受当前已捕获的动图。",
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "description": "需要从这些动图确认的动作或细节。"},
    }, "required": ["question"], "additionalProperties": False},
}}


def enabled(settings):
    return bool(settings.paid_model and settings.paid_model.video_input)


def call_limit_matches(settings, baseline):
    """Extra media stages need the same explicitly frozen Bili scope and policy."""
    paid, approval = settings.paid_model, settings.live_text_window
    if paid is None:
        return False
    if paid.per_request_calls == baseline:
        return True
    from companion.paid_budget import sha

    return bool(
        baseline < paid.per_request_calls <= 10 and enabled(settings) and paid.image_input
        and settings.bili_video.enabled and approval
        and approval.bili_video_scope == "bounded_video_media_v1"
        and approval.link_scope == "owner_links_v1"
        and approval.bili_video_policy_sha256 == sha(settings.bili_video.model_dump(mode="json"))
    )


def contract(settings, job_id, logical_request_id):
    from companion import private_life

    if settings.owner_runtime.mode == "ENABLED":
        if (not enabled(settings) or not settings.paid_model.image_input
            or not settings.video_model_key.get_secret_value() or not settings.content_enabled):
            raise CoreError("PAID_VIDEO_APPROVAL_REQUIRED", 503)
        if not job_id or logical_request_id != job_id:
            raise CoreError("MODEL_VIDEO_CHAT_JOB_REQUIRED", 503)
        return settings.paid_model.video_input
    if (not enabled(settings) or not settings.video_model_key.get_secret_value()
        or not private_life.enabled(settings) or not settings.content.use_chat_model_for_vision
        or settings.paid_model.cumulative is not None):
        raise CoreError("PAID_VIDEO_APPROVAL_REQUIRED", 503)
    if not job_id or logical_request_id != job_id:
        raise CoreError("MODEL_VIDEO_CHAT_JOB_REQUIRED", 503)
    return settings.paid_model.video_input


def preview_text_and_estimate(video_policy, image_policy, messages):
    """The same approved video gateway observes bounded Reader preview frames."""
    from companion.image_input import text_and_estimate as image_estimate

    if video_policy is None or image_policy is None:
        raise CoreError("PAID_VIDEO_APPROVAL_REQUIRED", 503)
    text, _ = image_estimate(image_policy, messages)
    return text, video_policy.estimated_tokens_per_image * (len(messages[0]["content"]) - 1)


def _animation(data, mime, policy):
    if not data or len(data) > policy.max_bytes_per_image or mime not in FORMATS:
        raise ValueError()
    image = Image.open(io.BytesIO(data))
    count = getattr(image, "n_frames", 1)
    start = int(bool(image.info.get("default_image", False)))
    if (image.format != FORMATS[mime] or min(image.size) < 1 or max(image.size) > 4096
        or image.width * image.height > policy.max_pixels_per_image
        or count > MAX_FRAMES or image.width * image.height * count > MAX_DECODED_PIXELS):
        image.close()
        raise ValueError()
    return image, count, start


def preview(data, mime, policy):
    """Reuse the ordered frame sheet within the original one-image, 512-edge preview."""
    from companion.image_frames import SAMPLE_FRAMES, prepare

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image, count, start = _animation(data, mime, policy)
            with image:
                if count - start <= 1:
                    return prepare(data, mime, policy), False
                # Match prepare's existing sample positions; retain fractional APNG times.
                frames = count - start
                selected = sorted({start + round(i * (frames - 1) / max(
                    1, min(frames, SAMPLE_FRAMES) - 1)) for i in range(min(frames, SAMPLE_FRAMES))})
                duration, times = 0, []
                for index in range(start, count):
                    image.seek(index)
                    image.load()
                    if index in selected:
                        times.append(f"{float(duration):g}")
                    value = image.info.get("duration", 100)
                    if (not isinstance(value, (int, float)) or not math.isfinite(value)
                        or value <= 0):
                        raise ValueError()
                    duration += Fraction(str(value))
                try:
                    prepared = prepare(data, mime, policy)
                except CoreError:
                    raise ValueError() from None
                output = io.BytesIO()
                with Image.open(io.BytesIO(prepared.data)) as sheet:
                    sheet.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    if sheet.width * sheet.height > policy.max_pixels_per_image:
                        raise ValueError()
                    sheet.save(output, format="PNG")
                if len(output.getvalue()) > policy.max_bytes_per_image:
                    raise ValueError()
                note = (f"[动图抽样预览：{FORMATS[mime]}共{frames}帧，"
                        f"时长{float(duration):g}ms，抽取第"
                        + ",".join(str(index - start + 1) for index in selected)
                        + "帧；对应起始时间(ms)=" + ",".join(times)
                        + "。拼图按从左到右、从上到下排列，仅为抽样画面，不能声称看过完整动画；"
                        "要确认动作、时序或表情变化时调用 understand_video。]")
                return PreparedImage(output.getvalue(), "image/png", note), True
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Warning):
        raise CoreError("CONTENT_VIDEO_INPUT_INVALID", 400) from None


@dataclass(frozen=True, repr=False)
class PreparedVideo:
    data: bytes
    seconds: float

    def part(self):
        return {"type": "video_url", "video_url": {
            "url": "data:video/mp4;base64," + base64.b64encode(self.data).decode("ascii"),
        }}


def encode_animation(data, mime, image_policy, video_policy):
    """Encode composited frames as H.264, preserving time within one 25fps interval."""
    import av

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image, count, start = _animation(data, mime, image_policy)
            with image:
                if count - start < 2:
                    raise ValueError()
                scale = min(1, 512 / max(image.size))
                width, height = (max(2, int(side * scale) // 2 * 2) for side in image.size)
                output, elapsed, encoded = io.BytesIO(), 0, 0
                with av.open(output, mode="w", format="mp4") as container:
                    stream = container.add_stream("libx264", rate=25)
                    stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
                    stream.options = {"crf": "23", "preset": "veryfast", "threads": "1"}
                    for index in range(start, count):
                        image.seek(index)
                        image.load()
                        duration = image.info.get("duration", 100)
                        if (not isinstance(duration, (int, float)) or not math.isfinite(duration)
                            or duration <= 0):
                            raise ValueError()
                        elapsed += Fraction(str(duration))
                        if elapsed > video_policy.max_seconds * 1000:
                            raise ValueError()
                        rgba = image.convert("RGBA").resize((width, height))
                        rgb = Image.new("RGB", (width, height), "white")
                        rgb.paste(rgba, mask=rgba)
                        while encoded * 40 < elapsed:
                            frame = av.VideoFrame.from_image(rgb)
                            frame.pts = encoded
                            for packet in stream.encode(frame):
                                container.mux(packet)
                            encoded += 1
                        if output.tell() > video_policy.max_bytes:
                            raise ValueError()
                    for packet in stream.encode():
                        container.mux(packet)
                video = output.getvalue()
                if not video or len(video) > video_policy.max_bytes:
                    raise ValueError()
                return PreparedVideo(video, encoded / 25)
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError, Warning):
        raise CoreError("CONTENT_VIDEO_INPUT_INVALID", 400) from None


def text_and_estimate(policy, messages, image_policy=None):
    """Inline MP4 only, with bounded local decode; never send URLs for providers to fetch."""
    import av

    try:
        if len(messages) != 1 or set(messages[0]) != {"role", "content"}:
            raise ValueError()
        message = messages[0]
        if message["role"] != "user" or not isinstance(message["content"], list):
            raise ValueError()
        text, *parts = message["content"]
        if (not parts or not isinstance(text, dict) or text.keys() != {"type", "text"}
            or text["type"] != "text" or not isinstance(text["text"], str)):
            raise ValueError()
        seconds, image_tokens, video_count = 0, 0, 0
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError()
            if part.get("type") == "image_url" and image_policy is not None:
                from companion.image_input import _validate_image

                _validate_image(image_policy, part, Image)
                image_tokens += policy.estimated_tokens_per_image
                continue
            if part.keys() != {"type", "video_url"} or part["type"] != "video_url":
                raise ValueError()
            if part["video_url"].keys() != {"url"}:
                raise ValueError()
            url = part["video_url"]["url"]
            if not isinstance(url, str) or len(url) > 64 + 4 * ((policy.max_bytes + 2) // 3):
                raise ValueError()
            match = re.fullmatch(r"data:video/mp4;base64,([A-Za-z0-9+/]*={0,2})", url)
            if not match:
                raise ValueError()
            data = base64.b64decode(match[1], validate=True)
            if (not data or len(data) > policy.max_bytes
                or base64.b64encode(data).decode("ascii") != match[1]):
                raise ValueError()
            with av.open(io.BytesIO(data), mode="r", format="mp4") as video:
                if len(video.streams) != 1 or len(video.streams.video) != 1:
                    raise ValueError()
                stream = video.streams.video[0]
                if stream.codec_context.name != "h264" or not stream.duration:
                    raise ValueError()
                duration = float(stream.duration * stream.time_base)
                if (duration <= 0 or duration > policy.max_seconds + 0.04
                    or min(stream.width, stream.height) < 1
                    or max(stream.width, stream.height) > 512):
                    raise ValueError()
                frames = 0
                for _frame in video.decode(video=0):
                    frames += 1
                    if frames > math.ceil(policy.max_seconds * 25) + 1:
                        raise ValueError()
                if not frames:
                    raise ValueError()
                seconds += duration
                video_count += 1
                if seconds > policy.max_seconds + 0.04:
                    raise ValueError()
        if not video_count:
            raise ValueError()
        return text["text"], image_tokens + math.ceil(seconds * policy.estimated_tokens_per_second)
    except (OSError, ValueError, TypeError, KeyError, IndexError, binascii.Error,
            Image.DecompressionBombError, Warning):
        raise CoreError("MODEL_VIDEO_INPUT_INVALID", 503) from None
