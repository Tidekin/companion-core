"""Stopped custody reads native device inputs on their own admission path.

A STOP followed by activate re-reads every input bound to the stopped authority.
Native chat inputs used to fail that read because only the QQ source was accepted.
"""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import model_carry, owner_authority, owner_input, owner_input_state
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture
from tests.support.native_chat import enroll, packet
from tests.support.owner_console import client as client
from tests.support.owner_input import captured


def native_input(client, core, tmp_path):
    captured(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    headers, authority = enroll(client, key)
    body = packet(core, key, authority, "原生输入的停机核对")
    response = client.post("/v1/console/chat/messages", json=body, headers=headers)
    assert response.status_code == 202, response.text
    saved = core.store.one("SELECT value FROM runtime_meta WHERE key=%s",
                           (owner_input.INPUT + body["capture"]["event_id"],))
    return OwnerCapture.model_validate(json.loads(saved["value"])["capture"])


def test_stopped_native_input_reads_terminal_and_feeds_carry_work(client, core, tmp_path):
    capture = native_input(client, core, tmp_path)
    pending = owner_input_state.read(core.store, core.settings, capture)
    assert not pending["terminal"] and pending["unsettled_work"]
    owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-native-input", authority=capture.authority))
    core.store.execute("UPDATE jobs SET state='FAILED',last_error_code='JOB_LEASE_LOST' "
                       "WHERE event_id=%s AND kind='chat'", (capture.event_id,))
    result = owner_input_state.read(core.store, core.settings, capture)
    assert result["terminal"] and result["authority"] == capture.authority
    assert result["input_sha256"] == capture.input_sha256
    # The same input set model carry and same-policy activation read after a STOP.
    work = model_carry._work(core.store, core.settings, capture.authority)
    assert work[capture.event_id]["terminal"]


@pytest.mark.parametrize("change", [
    "UPDATE events SET source='qq.onebot' WHERE event_id=%s",
    "UPDATE conversations SET platform_session_key='native:other' WHERE conversation_id="
    "(SELECT conversation_id FROM events WHERE event_id=%s)",
    "UPDATE events SET body_json='{\"text\":\"changed\",\"media\":[],\"shared_content\":[]}' "
    "WHERE event_id=%s",
])
def test_native_input_must_match_its_original_admission(client, core, tmp_path, change):
    capture = native_input(client, core, tmp_path)
    core.store.execute(change, (capture.event_id,))
    with pytest.raises(CoreError, match="OWNER_APPROVED_INPUT_REQUIRED"):
        owner_input_state.read(core.store, core.settings, capture)
