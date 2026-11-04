"""Bounded private delivery asset read; bytes live only in the prepared send."""

import base64
import re

from companion_meme_protocol import digest, validate_bytes

from .delivery_asset_http import RECEIPT_FIELDS, read_leased_asset


async def read_asset(runtime, client, item, *, check):
    check()
    if runtime.config.meme_catalog_owner != "core":
        raise ValueError("MEME_CORE_MODE_REQUIRED")
    if not re.fullmatch(r"msg_[a-f0-9]{32}", item["message_id"]):
        raise ValueError("MEME_DELIVERY_BINDING_INVALID")

    def prepare(value):
        hashes = {
            "asset_id",
            "asset_sha256",
            "source_sha256",
            "custody_sha256",
            "record_sha256",
            "caption_sha256",
            "binding_sha256",
        }
        if (
            set(value) != RECEIPT_FIELDS | hashes
            or type(value["reply_revision"]) is not int
            or any(
                not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k])
                for k in hashes
            )
            or value["asset_sha256"] != item["content"]["image"]["candidate_id"]
            or value["asset_id"]
            != digest([value["custody_sha256"], value["source_sha256"], value["asset_sha256"]])
            or type(value["byte_length"]) is not int
            or not 1 <= value["byte_length"] <= 4 * 1024 * 1024
        ):
            raise ValueError()
        data = base64.b64decode(value["data_base64"], validate=True)
        prepared = validate_bytes(data)
        if (
            prepared is None
            or prepared.sha256 != value["asset_sha256"]
            or prepared.mime_type != value["mime"]
            or len(data) != value["byte_length"]
            or base64.b64encode(data).decode() != value["data_base64"]
        ):
            raise ValueError()
        return prepared

    return await read_leased_asset(
        runtime, client, item,
        path="/internal/v1/memes/deliveries/" + item["message_id"] + "/asset",
        purpose="meme", check=check, request_error="MEME_CORE_REQUEST_CHANGED",
        receipt_error="MEME_ASSET_RECEIPT_INVALID", prepare=prepare,
    )
