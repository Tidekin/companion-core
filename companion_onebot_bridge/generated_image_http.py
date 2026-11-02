"""Bounded read of a Core-generated reply picture; bytes live only in the prepared send."""

import base64
import binascii
import json
import re
from datetime import datetime

from companion_meme_protocol import validate_bytes

from . import journal, reply_segments
from .network import checked_http_client, private_request_matches, seal_protocol_content
from .onebot_client import bounded_post

SHA = re.compile(r"[a-f0-9]{64}")
IMAGE_ID = re.compile(r"gimg_[a-f0-9]{32}")
MIMES = {"image/png", "image/jpeg"}
MAX_BYTES = 4 * 1024 * 1024


def valid_reference(value):
    return (isinstance(value, dict)
            and set(value) == {"image_id", "sha256", "mime", "byte_length"}
            and isinstance(value["image_id"], str) and IMAGE_ID.fullmatch(value["image_id"])
            and isinstance(value["sha256"], str) and SHA.fullmatch(value["sha256"])
            and value["mime"] in MIMES
            and type(value["byte_length"]) is int and 1 <= value["byte_length"] <= MAX_BYTES)


async def read_image(runtime, client, item, *, check):
    check()
    reference = item["content"]["generated_image"]
    if not valid_reference(reference) or not re.fullmatch(r"msg_[a-f0-9]{32}",
                                                          item["message_id"]):
        raise ValueError("GENERATED_IMAGE_BINDING_INVALID")
    original = journal.digest(item)
    authority = dict(runtime.authority)
    body = {
        "authority": authority,
        "event_id": reply_segments.event_id(item),
        "reply_revision": item["reply_revision"],
        "lease_token": item["lease_token"],
        "content_sha256": journal.digest(item["content"]),
    }
    path = "/internal/v1/generated-images/deliveries/" + item["message_id"] + "/asset"
    expected = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    deadline = datetime.fromisoformat(item["lease_until"])
    if deadline.utcoffset() is None:
        raise ValueError("OWNER_DELIVERY_LEASE_LOST")

    def fence():
        check()
        runtime.verify()
        if (journal.digest(item) != original or runtime.authority != authority
                or runtime.clock() >= deadline):
            raise ValueError("OWNER_DELIVERY_LEASE_LOST")

    async def boundary(request):
        nonlocal deadline

        def validate():
            if (
                request.method != "POST"
                or "Cookie" in request.headers
                or not private_request_matches(request, runtime.config.core_url, path)
                or request.headers.get("Authorization") != "Bearer " + runtime.config.bridge_token
                or request.headers.get("Content-Type") != "application/json"
                or request.content != expected
            ):
                raise ValueError("GENERATED_IMAGE_REQUEST_CHANGED")

        fence()
        validate()
        approval = await runtime.check(client, purpose="expression")
        if approval["expires_at"]:
            deadline = min(deadline, datetime.fromisoformat(approval["expires_at"]))
        fence()
        validate()
        seal_protocol_content(request, expected)

    async with checked_http_client(client, boundary) as scoped:
        value = await bounded_post(scoped, path, body, max_bytes=6_000_000)
    fence()
    fields = {"message_id", "delivery_key", "event_id", "reply_revision", "authority",
              "content_sha256", "lease_until", "image_id", "sha256", "mime", "byte_length",
              "data_base64"}
    try:
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or any(value[k] != item[k] for k in ("message_id", "delivery_key", "authority"))
            or any(value[k] != body[k] for k in ("event_id", "reply_revision", "content_sha256"))
            or any(value[k] != reference[k] for k in reference)
            or not isinstance(value["data_base64"], str)
            or len(value["data_base64"]) > 5592408
        ):
            raise ValueError()
        expires = datetime.fromisoformat(value["lease_until"])
        if expires.utcoffset() is None or expires > deadline or runtime.clock() >= expires:
            raise ValueError()
        data = base64.b64decode(value["data_base64"], validate=True)
        prepared = validate_bytes(data)
        if (
            prepared is None
            or prepared.sha256 != reference["sha256"]
            or prepared.mime_type != reference["mime"]
            or len(data) != reference["byte_length"]
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError, binascii.Error) as error:
        raise ValueError("GENERATED_IMAGE_RECEIPT_INVALID") from error
    fence()
    return prepared
