"""Each background domain must hold its own current purpose and history scope."""

import pytest

from companion import owner_authority, owner_input
from companion.contracts import CoreError
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import after
from tests.support.owner_authority import activate
from tests.test_owner_input import captured, inbound


@pytest.mark.parametrize("purpose", ["memory", "diary", "reflection", "consolidation"])
def test_background_source_uses_its_own_purpose_without_chat(core, tmp_path, purpose):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "source-purpose-input")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    original = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**original.policy.model_dump(),
        "purposes": [purpose, "history"],
        "memory_scope": "owner_private" if purpose == "memory" else "none",
        "history": {"scope": "owner_private", "not_before": after(core.clock(), -60),
                    "max_turns": 8, "max_utf8_bytes": 8192}})
    activate(core, policy, revision=1, operation="source-purpose-revision")
    view = owner_authority.current(core.store, core.settings, core.clock)
    event, _, job = owner_input.source(core.store, core.settings, view, capture.event_id,
                                       core.clock(), history=True, purpose=purpose)
    assert event["event_id"] == capture.event_id
    assert job["authority_json"] != "{}"
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        owner_input.source(core.store, core.settings, view, capture.event_id, core.clock(),
                           history=True, purpose="chat")
    with pytest.raises(CoreError, match="OWNER_SOURCE_CHANGED"):
        owner_input.source(core.store, core.settings, view, capture.event_id, core.clock(),
                           purpose=purpose)


def test_domain_purpose_does_not_imply_history(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "source-purpose-no-history")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    original = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({
        **original.policy.model_dump(), "purposes": ["diary"]})
    activate(core, policy, revision=1, operation="source-purpose-without-history")
    view = owner_authority.current(core.store, core.settings, core.clock)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        owner_input.source(core.store, core.settings, view, capture.event_id, core.clock(),
                           history=True, purpose="diary")
