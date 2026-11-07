"""A crash between the cumulative journal append and the input receipt commit."""

import hashlib

import pytest

from companion import owner_input, paid_budget
from companion.owner_authority_protocol import OwnerCapture
from companion.qq_rich import input_digest
from companion_delivery_protocol.authority import capture_signing_payload
from tests.support.cumulative_budget import enable, view


class Crash(BaseException):
    """Process death after the external journal fsync, before the SQLite commit."""


def capture(core, number):
    active, key = core.authorized_model_test_capture
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:model-boundary-test:{9000 + number}")
    identity = paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id])
    event_id = "evt_" + hashlib.sha256(source.encode()).hexdigest()
    value = dict(authority=active["authority"], kind="authenticated_onebot_ws",
                 source_event_id=source, event_id=event_id,
                 input_sha256=input_digest({"text": "crash window"}), frame_sha256="b" * 64,
                 evidence_sha256="c" * 64, identity_sha256=identity,
                 protocol_epoch="model-boundary-test", occurred_at=core.clock(),
                 received_at=core.clock())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    return OwnerCapture.model_validate(value)


def inputs(core):
    return sum(key.startswith("input:") for key in view(core)["operations"])


def test_crash_after_cumulative_journal_append_replays_the_same_input_once(
    core, tmp_path, monkeypatch,
):
    enable(core, tmp_path)
    before = inputs(core)
    item = capture(core, 1)
    original = type(core.store).insert

    def crashing(store, table, **values):
        if table == "runtime_meta" and values["key"] == owner_input.INPUT + item.event_id:
            raise Crash()
        return original(store, table, **values)

    monkeypatch.setattr(type(core.store), "insert", crashing)
    with pytest.raises(Crash):
        owner_input.approve(core.store, core.settings, core.clock, item)
    monkeypatch.setattr(type(core.store), "insert", original)
    # The external journal kept the admitted input while SQLite rolled back its receipt.
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                              (owner_input.INPUT + item.event_id,))
    assert inputs(core) == before + 1

    assert owner_input.approve(core.store, core.settings, core.clock, item) == {
        "approved": True, "duplicate": False}
    assert owner_input.approve(core.store, core.settings, core.clock, item) == {
        "approved": True, "duplicate": True}
    # The retry reuses the journaled admission instead of counting the input twice.
    assert inputs(core) == before + 1
