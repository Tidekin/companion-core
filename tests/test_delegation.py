"""Owner-delegated companion adjustments stay bounded, audited, revertible and restorable."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import delegation
from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.retrieval import SemanticIndex
from tests.conftest import run
from tests.support.consolidation import enable_nightly
from tests.support.delegation import delegate, submit
from tests.support.diary import DAY

ORIGIN = "https://console.example.test"
PASSWORD = "owner-delegation-test-password-" * 2


def test_owner_mode_and_ranges_bound_every_adjustment(core):
    core.settings.wake_enabled = True
    core.settings.wake_max_daily_runs = 3
    with pytest.raises(CoreError, match="DELEGATION_OWNER_ONLY"):
        submit(core, "wake_max_daily_runs", 2)
    with pytest.raises(CoreError, match="DELEGATION_RANGE_OUTSIDE_CONFIGURATION"):
        delegate(core, ranges={"wake_max_daily_runs": {"min": 4}})
    with pytest.raises(CoreError, match="DELEGATION_RANGES_INVALID"):
        delegate(core, ranges={"wake_max_daily_runs": {"max": 5}})
    delegate(core, ranges={"wake_max_daily_runs": {"min": 1}})
    for value in (0, 4):  # Below the owner's floor, or above the configured count.
        with pytest.raises(CoreError, match="DELEGATION_VALUE_OUT_OF_RANGE"):
            submit(core, "wake_max_daily_runs", value)
    with pytest.raises(CoreError, match="DELEGATION_FIELD_NOT_ALLOWED"):
        submit(core, "wake_min_interval_minutes", 240)
    assert submit(core, "wake_max_daily_runs", 2)["state"] == "ACTIVE"
    assert delegation.effective(core.store, core.settings, "wake", "wake_max_daily_runs",
                                core.clock()) == 2
    core.settings.wake_max_daily_runs = 1  # A lower configuration always wins.
    assert delegation.effective(core.store, core.settings, "wake", "wake_max_daily_runs",
                                core.clock()) == 1


def test_propose_waits_for_owner_and_revert_restores_configuration(core):
    core.settings.wake_min_interval_minutes = 180
    delegate(core, mode="propose", ranges={"wake_min_interval_minutes": {"max": 360}})
    row = submit(core, "wake_min_interval_minutes", 300)
    assert row["state"] == "PROPOSED"
    get = lambda: delegation.effective(core.store, core.settings, "wake",  # noqa: E731
                                       "wake_min_interval_minutes", core.clock())
    assert get() == 180
    with core.store.transaction():
        assert delegation.decide(core.store, core.settings, row["override_id"], "approve",
                                 core.clock())["state"] == "ACTIVE"
    assert get() == 300
    with core.store.transaction():
        delegation.decide(core.store, core.settings, row["override_id"], "revert", core.clock())
    assert get() == 180
    with pytest.raises(CoreError, match="DELEGATION_DECISION_INVALID"), core.store.transaction():
        delegation.decide(core.store, core.settings, row["override_id"], "approve", core.clock())


def test_pause_expires_next_morning_never_resumes_owner_pause_and_owner_mode_reverts(core):
    core.settings.diary_enabled = True
    delegate(core, capability="diary", allow_pause=True)
    submit(core, "pause", True, capability="diary")
    assert delegation.paused(core.store, "diary", core.clock())
    assert not core.diary.repo.enabled()
    core.clock.advance(86400)
    assert not delegation.paused(core.store, "diary", core.clock())
    submit(core, "pause", True, capability="diary")
    delegate(core, capability="diary", mode="owner", allow_pause=False)
    assert not delegation.paused(core.store, "diary", core.clock())
    with pytest.raises(CoreError, match="DELEGATION_OWNER_ONLY"):
        submit(core, "pause", True, capability="diary")
    core.settings.diary_enabled = False
    delegate(core, capability="diary", allow_pause=True)
    with pytest.raises(CoreError, match="DELEGATION_CAPABILITY_DISABLED"):
        submit(core, "pause", True, capability="diary")


def test_wake_planner_uses_companion_pause(core):
    core.settings.wake_enabled = core.settings.goals_enabled = True
    delegate(core, capability="wake", allow_pause=True)
    submit(core, "pause", True)
    core.wake.evaluate()
    decision = core.store.one("SELECT * FROM wake_decisions ORDER BY rowid DESC LIMIT 1")
    assert decision["reason_code"] == "COMPANION_PAUSED"


def test_daily_limit_on_companion_changes(core):
    delegate(core, ranges={"wake_max_daily_runs": {"min": 0}})
    for value in (2, 1, 0):
        submit(core, "wake_max_daily_runs", value)
    with pytest.raises(CoreError, match="DELEGATION_DAILY_LIMIT"):
        submit(core, "wake_max_daily_runs", 1)


def test_reflection_adjustment_applies_and_erasure_withdraws_it(core):
    model = enable_nightly(core)
    core.settings.wake_max_daily_runs = 3
    delegate(core, ranges={"wake_max_daily_runs": {"min": 1}})

    def adjust(draft):
        item = draft["suggestions"][0]
        draft["suggestions"].append({**item, "kind": "capability_adjustment",
                                     "text": "主人这几天很忙，少打扰一些。",
                                     "adjustment": {"capability": "wake",
                                                    "field": "wake_max_daily_runs", "value": 1}})

    model.reflection_transform = adjust
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    assert "adjustable" in model.calls[-1][0][-1]["content"]
    row = core.store.one("SELECT * FROM capability_overrides")
    assert row["state"] == "ACTIVE" and row["suggestion_id"]
    assert delegation.effective(core.store, core.settings, "wake", "wake_max_daily_runs",
                                core.clock()) == 1
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_3"], core.clock())
    row = core.store.one("SELECT * FROM capability_overrides")
    assert row["state"] == "REVERTED" and row["reason"] == ""


def test_reflection_without_delegation_cannot_adjust(core):
    model = enable_nightly(core)

    def adjust(draft):
        item = draft["suggestions"][0]
        draft["suggestions"] = [{**item, "kind": "capability_adjustment", "adjustment": {
            "capability": "wake", "field": "pause", "value": True}}]

    model.reflection_transform = adjust
    assert run(core.reflections.run_day(DAY))["state"] == "FAILED"
    assert "adjustable" not in model.calls[-1][0][-1]["content"]
    assert core.store.rows("SELECT * FROM capability_overrides") == []


def test_restore_discards_adjustments(core, tmp_path):
    delegate(core, ranges={"wake_max_daily_runs": {"min": 1}})
    submit(core, "wake_max_daily_runs", 1)
    key, archive, dest = Fernet.generate_key(), tmp_path / "a.enc", tmp_path / "restored.db"
    backup(core.settings.database, archive, key)
    restore(archive, dest, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        assert restored.store.one("SELECT state FROM capability_overrides")["state"] == "DISCARDED"
        assert delegation.effective(restored.store, restored.settings, "wake",
                                    "wake_max_daily_runs", core.clock()) == 3
    finally:
        restored.store.close()


def test_console_autonomy_routes_require_session_and_are_idempotent(core, monkeypatch):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    app = create_app(core.settings)
    with TestClient(app, base_url=ORIGIN) as client:
        app.state.core.clock = core.clock
        assert client.get("/v1/console/autonomy").status_code == 401
        csrf = client.post("/v1/console/session", json={"password": PASSWORD},
                           headers={"Origin": ORIGIN}).json()["csrf_token"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "delegate-wake-1"}
        body = {"mode": "propose", "allow_pause": True,
                "ranges": {"wake_max_daily_runs": {"min": 1}}}
        assert client.put("/v1/console/autonomy/wake", json=body,
                          headers={"Origin": ORIGIN}).status_code == 403
        first = client.put("/v1/console/autonomy/wake", json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert client.put("/v1/console/autonomy/wake", json=body, headers=headers).json() \
            == first.json()
        bad = client.put("/v1/console/autonomy/wake", json={**body, "ranges": {
            "wake_max_daily_runs": {"min": 99}}}, headers={**headers, "Idempotency-Key": "bad-1"})
        assert bad.status_code == 400
        assert client.put("/v1/console/autonomy/executive", json=body, headers={
            **headers, "Idempotency-Key": "bad-2"}).status_code == 404
        live = app.state.core
        with live.store.transaction():
            row = delegation.submit(live.store, live.settings, "wake", "wake_max_daily_runs", 1,
                                    "理由", live.clock())
        view = client.get("/v1/console/autonomy").json()
        assert view["adjustments"][0]["state"] == "PROPOSED"
        assert client.get("/v1/console/overview").json()["counts"]["companion_proposals"] == 1
        decided = client.post(f"/v1/console/autonomy/adjustments/{row['override_id']}/decision",
                              json={"decision": "approve"},
                              headers={**headers, "Idempotency-Key": "approve-1"})
        assert decided.json() == {"state": "ACTIVE"}
        again = client.post(f"/v1/console/autonomy/adjustments/{row['override_id']}/decision",
                            json={"decision": "approve"},
                            headers={**headers, "Idempotency-Key": "approve-2"})
        assert again.status_code == 409


@pytest.mark.parametrize("value,capability,field,accepted", [
    (0.175, "expression", "expression_selection_probability", True),
    (1, "wake", "wake_max_daily_runs", True),
    (0.5, "wake", "wake_max_daily_runs", False),
    (True, "wake", "wake_max_daily_runs", False),
    ("0.175", "expression", "expression_selection_probability", False),
    (0.9, "expression", "expression_selection_probability", False),
])
def test_console_ranges_preserve_numbers_and_native_bounds(
    core, monkeypatch, value, capability, field, accepted,
):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    with TestClient(create_app(core.settings), base_url=ORIGIN) as client:
        csrf = client.post("/v1/console/session", json={"password": PASSWORD},
            headers={"Origin": ORIGIN}).json()["csrf_token"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "range-1"}
        body = {"mode": "propose", "allow_pause": False, "ranges": {field: {"min": value}}}
        response = client.put("/v1/console/autonomy/" + capability, json=body, headers=headers)
        if accepted:
            assert response.status_code == 200, response.text
            assert client.put("/v1/console/autonomy/" + capability, json=body,
                headers=headers).json() == response.json()
            saved = delegation.delegation(core.store, capability)["ranges"][field]["min"]
            assert saved == value and type(saved) is type(value)
        else:
            assert response.status_code == 400, response.text
            assert delegation.delegation(core.store, capability)["mode"] == "owner"
            assert not core.store.rows("SELECT * FROM console_operations")


def test_every_delegable_capability_accepts_owner_ranges_and_reports_integration(core):
    core.settings.proactive_daily_limit = 3
    delegate(core, capability="proactive", ranges={"proactive_daily_limit": {"min": 1},
                                                   "proactive_interval_seconds": {"max": 21600}})
    with pytest.raises(CoreError, match="DELEGATION_RANGE_OUTSIDE_CONFIGURATION"):
        delegate(core, capability="proactive", ranges={"proactive_interval_seconds": {"max": 1}})
    view = delegation.view(core.store, core.settings, core.clock())
    rows = {row["capability"]: row for row in view["capabilities"]}
    assert set(rows) == set(delegation.DELEGABLE)
    assert rows["proactive"]["wired"] == [] and rows["wake"]["wired"]
    assert rows["proactive"]["limits"]["proactive_daily_limit"] == {
        "kind": "floor", "configured": 3, "effective": 3}
    assert "memory.manage" not in rows and "executive" not in rows
