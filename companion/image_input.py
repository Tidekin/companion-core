"""Bounded original images with per-image planning estimates, never remote token proof."""

import base64
import binascii
import io
import re
import warnings

from pydantic import BaseModel, ConfigDict, Field

from companion.contracts import CoreError
from companion.trial_limits import CountLimit


class ImageInputPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    estimated_tokens_per_image: int = Field(strict=True, gt=0, le=1000000)
    max_bytes_per_image: int = Field(strict=True, gt=0, le=4000000)
    max_pixels_per_image: int = Field(strict=True, gt=0, le=16000000)
    max_images_per_request: CountLimit = 1


def text_and_estimate(policy, messages):
    """Validate the exact inline-image payload before reserving or dispatching it.

    Only the text bytes use the text estimator. The image uses the separately
    approved fixed estimate regardless of its base64 length or compression ratio.
    """
    if policy is None:
        raise CoreError("PAID_IMAGE_APPROVAL_REQUIRED", 503)
    try:
        from PIL import Image
    except ImportError:
        raise CoreError("CONTENT_IMAGE_DECODER_UNAVAILABLE", 503) from None
    try:
        if not isinstance(messages, list) or len(messages) != 1:
            raise ValueError()
        message = messages[0]
        if set(message) != {"role", "content"} or message["role"] != "user":
            raise ValueError()
        parts = message["content"]
        if (not isinstance(parts, list) or len(parts) < 2
            or (policy.max_images_per_request != "unlimited"
                and len(parts) > policy.max_images_per_request + 1)):
            raise ValueError()
        text, *images = parts
        if (
            set(text) != {"type", "text"}
            or text["type"] != "text"
            or not isinstance(text["text"], str)
        ):
            raise ValueError()
        for image in images:
            _validate_image(policy, image, Image)
        return text["text"], policy.estimated_tokens_per_image * len(images)
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        SyntaxError,
        Warning,
        binascii.Error,
        Image.DecompressionBombError,
    ) as error:
        raise CoreError("MODEL_IMAGE_INPUT_INVALID", 503) from error


def _validate_image(policy, image, Image):
    if (
        set(image) != {"type", "image_url"}
        or image["type"] != "image_url"
        or set(image["image_url"]) != {"url"}
    ):
        raise ValueError()
    url = image["image_url"]["url"]
    if not isinstance(url, str) or len(url) > 64 + 4 * ((policy.max_bytes_per_image + 2) // 3):
        raise ValueError()
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})", url)
    if not match:
        raise ValueError()
    data = base64.b64decode(match[2], validate=True)
    if (
        not data
        or len(data) > policy.max_bytes_per_image
        or base64.b64encode(data).decode("ascii") != match[2]
    ):
        raise ValueError()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as decoded:
            if (
                decoded.format
                != {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[match[1]]
                or min(decoded.size) < 1
                or max(decoded.size) > 4096
                or decoded.width * decoded.height > policy.max_pixels_per_image
                or getattr(decoded, "n_frames", 1) != 1
            ):
                raise ValueError()
            decoded.verify()
        with Image.open(io.BytesIO(data)) as decoded:
            decoded.load()


def messages_and_estimate(policy, messages):
    """Validate originals on their actual user turns, with one request-wide image cap."""
    if not isinstance(messages, list) or not messages:
        raise CoreError("MODEL_IMAGE_INPUT_INVALID", 503)
    result, estimate, count = [], 0, 0
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"}
        ):
            raise CoreError("MODEL_IMAGE_INPUT_INVALID", 503)
        if isinstance(message["content"], str):
            result.append(dict(message))
            continue
        text, tokens = text_and_estimate(policy, [message])
        count += len(message["content"]) - 1
        estimate += tokens
        result.append({"role": "user", "content": text})
    if (not count or policy is None
        or policy.max_images_per_request != "unlimited" and count > policy.max_images_per_request):
        raise CoreError("MODEL_IMAGE_INPUT_INVALID", 503)
    return result, estimate


def contract(settings, job_id, logical_request_id):
    """A content call shares its actual chat job, credentials and paid ledger."""
    if (
        settings.mode != "live"
        or settings.paid_model is None
        or not settings.paid_model.image_input
    ):
        raise CoreError("PAID_IMAGE_APPROVAL_REQUIRED", 503)
    if not job_id or logical_request_id != job_id:
        raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
    if settings.owner_runtime.mode == "ENABLED":
        return  # Ordinary SDK chat owns the original per-request attempt and global budgets.
    if not settings.content.use_chat_model_for_vision:
        raise CoreError("PAID_IMAGE_SEPARATE_PROVIDER_UNSUPPORTED", 503)
    approval = settings.live_text_window
    calls = 3 if approval and approval.memory_scope != "none" else 2
    if settings.paid_model.video_input:
        from companion.video_input import contract as video_contract

        video_contract(settings, job_id, logical_request_id)
        calls += 1
    from companion.video_input import call_limit_matches

    if not call_limit_matches(settings, calls):
        raise CoreError("MODEL_IMAGE_TWO_CALL_CONTRACT_REQUIRED", 503)


def check_evaluation_input(approval, messages):
    if not approval or not approval.image_input:
        raise CoreError("PAID_IMAGE_APPROVAL_REQUIRED", 503)
    messages_and_estimate(approval.image_input, messages)


def check_chat_scope(store, settings, job_id, logical_id, scenario):
    from companion import owner_authority

    contract(settings, job_id, logical_id)
    if scenario == "chat" and owner_authority.required(store, settings):
        return True
    if scenario != "content_vision" and (
        scenario != "chat" or not settings.live_text_window
        or settings.live_text_window.image_scope != "owner_image_v1"
    ):
        raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
    return False


def check_attempt(store, job_id, scenario, video_binding):
    if not store.one("SELECT 1 FROM jobs WHERE job_id=? AND kind='chat'", (job_id,)):
        raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
    if video_binding is None and store.one(
        "SELECT 1 FROM model_usage WHERE job_id=? AND scenario=?", (job_id, scenario),
    ):
        raise CoreError("MODEL_IMAGE_ALREADY_ATTEMPTED", 409)
