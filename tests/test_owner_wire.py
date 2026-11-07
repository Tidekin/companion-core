"""Shared pure codecs never grant a host driver permission to bypass its final hook."""

import asyncio

import pytest

from companion.owner_authority_protocol import OwnerSendCheck
from companion_delivery_protocol.authority import capture_signing_payload, validate_send_check
from companion_onebot_bridge.outbound import deliver_item
from tests.support.owner_input import captured


def test_capture_canonical_timestamp_and_v1_field_rejection(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    value = capture.model_dump(mode="json", exclude={"signature"})
    canonical = capture_signing_payload(value)
    value["occurred_at"] = capture.occurred_at.isoformat()
    value["received_at"] = capture.received_at.isoformat()
    assert capture_signing_payload(value) == canonical
    with pytest.raises(ValueError):
        capture_signing_payload({**value, "window_sha256": "e" * 64})


def test_send_check_shared_shape_matches_core_and_rejects_legacy_window(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    value = {"authority": capture.authority, "event_id": capture.event_id,
             "message_id": "msg_" + "a" * 32, "content_sha256": "b" * 64,
             "lease_token": "lease_" + "c" * 32, "reply_revision": 1}
    parsed = OwnerSendCheck.model_validate(value).model_dump(mode="json")
    assert validate_send_check(value) == parsed
    with pytest.raises(ValueError):
        validate_send_check({**value, "window_sha256": "d" * 64})
    with pytest.raises(ValueError):
        validate_send_check({**value, "reply_revision": True})


def test_authority_delivery_cannot_fall_through_unguarded_host_driver(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    with pytest.raises(ValueError, match="OWNER_AUTHORITY_GUARD_REQUIRED"):
        asyncio.run(deliver_item(None, None, None, {"authority": capture.authority}))
