"""Owner policy projections and signed ingress gate the existing Life selector."""

from datetime import datetime, timedelta

import pytest

from companion import owner_authority, owner_input, worker_deadlines
from companion.autonomous_selector import foreground_clear, native_selector
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after
from tests.conftest import run
from tests.support.autonomous_selector import drain
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.owner_input import captured, inbound


def test_selector_stays_silent_without_owner_grant_in_live_mode(core):
    core.settings.mode = "live"
    result = run(drain(native_selector(core, "owner-worker")))
    assert result[0].blocked
    assert core.store.rows("SELECT * FROM jobs") == []
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_selector_checks_purposes_before_native_hooks_and_step_requirements(core, tmp_path,
                                                                          monkeypatch):
    core.settings.memory_enabled = core.settings.consolidation_enabled = True
    policy = prepare(core, tmp_path)
    activate(core, policy)  # evaluation only, no background-domain purpose

    def forbidden():
        pytest.fail("ungranted native domain was invoked")

    monkeypatch.setattr(core.diary, "schedule_due", forbidden)
    monkeypatch.setattr(core.consolidation, "schedule_due", forbidden)
    monkeypatch.setattr(core, "extract_step", None, raising=False)
    monkeypatch.setattr(core.consolidation, "run_step", None, raising=False)
    assert not run(drain(native_selector(core, "owner-worker")))[-1].blocked
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.model.calls == []


def test_signed_pre_ingress_and_pending_chat_preempt_every_native_turn(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    assert foreground_clear(core)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s', (capture.event_id,))
    assert not foreground_clear(core)
    assert run(native_selector(core, "owner-worker").step()).blocked
    core.ingest(inbound(core, capture), "owner-selector-ingress")
    assert not foreground_clear(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_old_signed_approval_does_not_preempt_a_successor_authority(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    assert not foreground_clear(core)
    policy = owner_authority.current(core.store, core.settings, core.clock).policy
    activate(core, policy, revision=1, operation="selector-new-authority")
    assert foreground_clear(core)
    with pytest.raises(CoreError, match="OWNER_APPROVED_INPUT_REQUIRED"):
        core.ingest(inbound(core, capture), "stale-selector-ingress")


@pytest.mark.parametrize("kind", worker_deadlines.PROVIDERS)
@pytest.mark.parametrize("expires", [False, True])
def test_deadlines_use_optional_owner_expiry_without_a_window(core, tmp_path, kind, expires):
    policy = prepare(core, tmp_path)
    if expires:
        policy = policy.model_copy(update={
            "expires_at": datetime.fromisoformat(after(core.clock(), 60)),
        })
    activate(core, policy)
    assert core.settings.live_text_window is None
    before = core.store.db.total_changes
    start = datetime.fromisoformat(core.clock())
    deadline = worker_deadlines.PROVIDERS[kind](core.store, core.settings, start)
    assert core.store.db.total_changes == before
    if expires:
        assert deadline == start + timedelta(seconds=60)
    else:
        assert deadline > start + timedelta(seconds=60)


def test_stopped_or_restored_custody_never_falls_back_to_unscoped_lanes(core, tmp_path):
    policy = prepare(core, tmp_path)
    value, _ = activate(core, policy)
    selector = native_selector(core, "owner-worker")
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="selector-stop", authority=value["authority"],
    ))
    core.settings.mode = "mock"
    assert run(selector.step()).blocked
    with core.store.transaction():
        owner_authority.restore(core.store)
    assert run(selector.step()).blocked
    assert core.store.rows("SELECT * FROM model_usage") == []
