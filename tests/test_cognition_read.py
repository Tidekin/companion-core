import json

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from companion.cognition_api import register_cognition_routes
from companion.cognition_read import KINDS, CognitionRead, measurement
from companion.contracts import CoreError
from companion.core import Core
from companion.privacy import advance_epoch
from tests.test_goals import setup_goal


@pytest.mark.parametrize("kind", KINDS)
def test_empty_native_collections_do_not_execute(core, kind):
    before = core.store.db.total_changes
    assert CognitionRead(core).page(kind)["items"] == []
    assert core.store.db.total_changes == before
    assert core.model.calls == []


def test_goal_native_sources_and_independent_reader(core):
    goal = setup_goal(core)
    reader = Core(core.settings, core.model, core.clock)
    try:
        result = CognitionRead(reader).page("goal")
        assert result["items"][0]["id"] == goal
        assert result["items"][0]["execution"]["state"] == "discussed"
        assert "天文" not in json.dumps(result, ensure_ascii=False)
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
        assert CognitionRead(reader).page("goal")["items"] == []
    finally:
        reader.store.db.close()


def test_wake_native_choice_and_cursor(core):
    setup_goal(core)
    core.settings.exploration_enabled = False
    core.wake.evaluate("evt_test_1")
    core.wake.evaluate()
    read = CognitionRead(core)
    first = read.page("wake", limit=1)
    second = read.page("wake", limit=1, before=first["next_cursor"])
    assert first["truncated"] and not second["truncated"]
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert first["items"][0]["choice"] == "SKIP"
    assert first["candidate_set"] == "NOT_RECORDED"
    with pytest.raises(CoreError, match="CURSOR_UNAVAILABLE"):
        read.page("wake", before="absent")


def test_all_same_source_goals_remain_reachable(core):
    from companion.goals import GoalProposal, GoalScope

    first = setup_goal(core)
    core.goals.cancel(first)
    ids = [first]
    for index in range(24):
        goal = core.goals.propose(GoalProposal(
            topic=f"synthetic topic {index}", source_event_ids=["evt_test_1"],
            scope=GoalScope(operation="read_content", url="https://example.org/article"),
        ))
        core.goals.cancel(goal)
        ids.append(goal)
    seen, before = [], None
    while True:
        page = CognitionRead(core).page("goal", limit=7, before=before)
        seen.extend(row["id"] for row in page["items"])
        before = page["next_cursor"]
        if before is None:
            break
    assert set(seen) == set(ids)
    assert len(seen) == len(ids)


def test_timer_wake_rechecks_selected_goal_sources(core):
    goal = setup_goal(core)
    assert core.wake.evaluate()[0]["goal_id"] == goal
    view = CognitionRead(core)
    row = view.page("wake")["items"][0]
    assert row["source_event_ids"] == ["evt_test_1"]
    assert row["reason_code"] == "GOAL_DUE"
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    assert view.page("wake")["items"] == []


def test_privacy_change_during_read_fails_closed(core, monkeypatch):
    setup_goal(core)
    view = CognitionRead(core)
    original = view._record

    def changed(*args):
        result = original(*args)
        advance_epoch(core.store)
        return result

    monkeypatch.setattr(view, "_record", changed)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        view.page("goal")


def test_measurement_is_saved_numeric_metadata_only():
    assert measurement("{}") == {"state": "NOT_RECORDED"}
    value = measurement(json.dumps({"count_kind": "estimate", "input_tokens": 37,
                                   "endpoint": "secret", "method": "private prompt",
                                   "reservation_margin": 3, "output_allowance": 20}))
    assert value == {"state": "RECORDED", "count_kind": "estimate", "input_tokens": 37,
                     "reservation_margin": 3, "output_allowance": 20}


@pytest.mark.parametrize("kind", KINDS)
def test_missing_record_is_explicit(core, kind):
    assert CognitionRead(core).page(kind, record_id="absent")["record_status"] == "NOT_RECORDED"


def test_native_goal_wait_and_cancel(core):
    goal = setup_goal(core)
    core.goals.accept(goal)
    step = core.goals.claim()
    assert step
    assert core.goals.fail(step, "RESOURCE_UNAVAILABLE", resource=True)
    view = CognitionRead(core)
    item = view.page("goal", record_id=goal)["items"][0]
    assert item["state"] == item["steps"][0]["state"] == "WAITING_RESOURCE"
    core.goals.cancel(goal)
    item = view.page("goal", record_id=goal)["items"][0]
    assert item["state"] == item["steps"][0]["state"] == "CANCELLED"


@pytest.mark.parametrize("response_usage", [True, False])
def test_native_model_measurement_and_unknown(core, tmp_path, response_usage):
    import httpx

    from companion import budget, paid_budget
    from companion.model import ModelClient
    from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
    from tests.conftest import run
    from tests.support.owner_authority import activate
    from tests.support.token_measurement import MESSAGES, configure

    configure(core, tmp_path)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    activate(core, OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=["diary"], authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
    ))
    request = budget.reserve(core.store, core.settings, None, "diary", MESSAGES, core.clock())
    payload = {"choices": [{"message": {"content": "synthetic result"}}]}
    if response_usage:
        payload["usage"] = {"prompt_tokens": 20, "completion_tokens": 2}

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload),
        )) as client:
            try:
                await budget.complete(ModelClient(core.settings, client), core.store,
                                      core.settings, request, MESSAGES, "diary", core.clock)
            except CoreError:
                if response_usage:
                    raise

    run(execute())
    item = CognitionRead(core).page("model", record_id=request)["items"][0]
    saved = core.store.one("SELECT measurement_json FROM model_admissions WHERE request_id=?",
                           (request,))
    assert item["measurement"] == measurement(saved["measurement_json"])
    assert item["measurement"]["count_kind"] == "estimate"
    assert item["state"] == ("SETTLED" if response_usage else "UNKNOWN")
    assert "synthetic result" not in json.dumps(item)
    if response_usage:
        assert item["usage"]["input_tokens"] == 20


def test_daily_model_summary_covers_more_than_500_visible_calls(core):
    from companion.console_read import overview

    with core.store.transaction():
        for n in range(513):
            core.store.insert("model_usage", usage_id=f"usage_{n}", request_id=f"request_{n}",
                              occurred_at=core.clock(), provider="demo", model="demo",
                              scenario="chat", state="UNKNOWN")
    result = overview(core)["model"]
    assert result["calls"] == result["uncertain_calls"] == 513
    assert result["complete"] and result["observed_calls"] == 0


def test_explicit_route_contract(core):
    # Module contract only. Application composition is owned by the integration lane.
    app = FastAPI()
    app.state.core = core

    def owner(authorization: str = Header(default="")):
        if authorization != "Bearer synthetic-owner":
            raise HTTPException(401)

    register_cognition_routes(app, [Depends(owner)])
    with TestClient(app) as client:
        assert client.post("/internal/v2/cognition", json={"kind": "wake"}).status_code == 401
        headers = {"Authorization": "Bearer synthetic-owner"}
        response = client.post("/internal/v2/cognition", json={"kind": "wake"}, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        for selection in ({"kind": "wake", "limit": 51}, {"kind": "wake", "limit": True},
                          {"kind": "wake", "actor": "owner"}, {"kind": "unknown"}):
            assert client.post("/internal/v2/cognition", json=selection,
                               headers=headers).status_code == 422
