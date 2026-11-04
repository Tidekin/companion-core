"""Bounded read of a Core-generated reply picture; bytes live only in the prepared send."""

import base64
import re

from companion_meme_protocol import validate_bytes

from .delivery_asset_http import RECEIPT_FIELDS, read_leased_asset

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

    def prepare(value):
        if (
            set(value) != RECEIPT_FIELDS | {"image_id", "sha256"}
            or any(value[k] != reference[k] for k in reference)
        ):
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
        return prepared

    return await read_leased_asset(
        runtime, client, item,
        path="/internal/v1/generated-images/deliveries/" + item["message_id"] + "/asset",
        purpose="expression", check=check, request_error="GENERATED_IMAGE_REQUEST_CHANGED",
        receipt_error="GENERATED_IMAGE_RECEIPT_INVALID", prepare=prepare,
    )
