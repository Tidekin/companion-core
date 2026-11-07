"""A required Reader grant is checked for existence before the lease binds it, exactly after."""

import pytest

from companion import agent_reading
from companion.contracts import CoreError, InboundEvent
from tests.support.exploration import URL as BILI_URL
from tests.support.exploration import configure
from tests.support.onebot_events import normalized, raw_event
from tests.test_plugin_readonly import registered_grant


@pytest.fixture
def queued(core, tmp_path, monkeypatch):
    configure(core)
    core.settings.observation_adapters["bili"].source_grant_required = True
    registered_grant(core, tmp_path, "bilibili")
    event = InboundEvent.model_validate(normalized(raw_event(91, text=BILI_URL)))
    core.ingest(event, event.event_id)
    # The owner-turn checks have their own tests; this one is about the grant only.
    monkeypatch.setattr(agent_reading, "_read_owner_source", lambda *a, **k: None)
    monkeypatch.setattr(agent_reading.reply_revision, "owner_input", lambda *a: True)
    with core.store.transaction():
        request = core.exploration.create("bili", "read", "shared-link", BILI_URL,
                                          source_event_ids=[event.event_id])
    return request


def result(core, request):
    return agent_reading._reading_result(core.store, core.settings, core.clock, None, request,
                                         executing=False)


def test_a_new_request_with_a_valid_grant_waits_for_its_lease(core, queued):
    # The original failure: SOURCE_GRANT_REQUIRED here rolled the new request back.
    assert result(core, queued)["state"] == "QUEUED"


def test_no_valid_grant_still_refuses_before_the_lease(core, queued):
    core.store.execute("UPDATE source_grants SET revoked_at=? ", (core.clock(),))
    with pytest.raises(CoreError, match="SOURCE_GRANT_REVOKED"):
        result(core, queued)
    core.store.execute("DELETE FROM source_grant_revisions")
    core.store.execute("DELETE FROM source_grants")
    with pytest.raises(CoreError, match="SOURCE_GRANT_REQUIRED"):
        result(core, queued)


def test_after_the_lease_only_the_bound_grant_counts(core, queued):
    core.store.execute("UPDATE exploration_requests SET state='RUNNING' WHERE request_id=?",
                       (queued,))
    with pytest.raises(CoreError, match="SOURCE_GRANT_REQUIRED"):
        result(core, queued)  # Leased without a binding is never accepted.
    grant = core.store.one("SELECT grant_id,epoch FROM source_grant_revisions")
    core.store.execute("UPDATE exploration_requests SET source_grant_id=?,source_grant_epoch=? "
                       "WHERE request_id=?", (grant["grant_id"], grant["epoch"], queued))
    assert result(core, queued)["state"] == "RUNNING"
