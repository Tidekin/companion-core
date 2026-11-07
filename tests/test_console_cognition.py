"""Console session access to cognition, life and executive services reuses native records."""

import json

import pytest
from pydantic import SecretStr

from companion.store import after
from tests.conftest import run
from tests.support.cognition_goal_steps import finish_record, retained_plan
from tests.support.console_cognition import ORIGIN, PASSWORD, login
from tests.support.console_cognition import client as client
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY


@pytest.mark.parametrize("path", ["/cognition?kind=goal", "/affect", "/goals", "/reflections",
                                  "/life/topics", "/interests", "/expression-learning", "/tools",
                                  "/source-grants"])
def test_reads_require_the_owner_session(client, path):
    assert client.get("/v1/console" + path).status_code == 401
    login(client)
    response = client.get("/v1/console" + path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"


def test_cognition_goal_steps_without_scope_urls(client, core):
    goal, steps = retained_plan(core)
    finish_record(core, steps[0], 1)
    core.settings.control_token = SecretStr(PASSWORD)  # The observation helper sets its own.
    login(client)
    page = client.get("/v1/console/cognition", params={"kind": "goal"}).json()
    item = page["items"][0]
    assert item["id"] == goal and [s["state"] for s in item["steps"]][0] == "DONE"
    goals = client.get("/v1/console/goals").json()["items"]
    assert goals[0]["goal_id"] == goal and "example.org" not in json.dumps(goals)
    assert client.get("/v1/console/cognition", params={"kind": "other"}).status_code == 400


@pytest.mark.parametrize("decision", ["ACCEPT_AS_HYPOTHESIS", "REJECT", "NEED_MORE_EVIDENCE"])
def test_reflection_decision_uses_native_service_once(client, core, decision):
    enable_nightly(core)
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    headers = login(client)
    item = client.get("/v1/console/reflections").json()["items"][0]
    assert item["evidence"][0]["exact_quote"] and "evidence_json" not in item
    url = f"/v1/console/reflections/{item['suggestion_id']}/decision"
    body = {"decision": decision}
    assert client.post(url, json=body, headers=headers).status_code == 400  # Receipt key needed.
    keyed = {**headers, "Idempotency-Key": "reflection-decision-1"}
    first = client.post(url, json=body, headers=keyed)
    assert first.status_code == 200, first.text
    assert client.post(url, json=body, headers=keyed).json() == first.json()
    assert core.store.one("SELECT state FROM reflection_suggestions")["state"] == decision
    assert client.post(url, json=body, headers={"Origin": ORIGIN}).status_code == 403


def test_affect_reset_requires_confirmation(client):
    headers = {**login(client), "Idempotency-Key": "affect-reset-1"}
    assert client.post("/v1/console/affect/reset", json={}, headers=headers).status_code == 400


def test_goal_plan_console_wait_reorder_and_version_fence(client, core):
    goal, steps = retained_plan(core)
    core.settings.control_token = SecretStr(PASSWORD)
    headers = login(client)
    path = f"/v1/console/goals/{goal}/continuation"
    item = client.get("/v1/console/goals").json()["items"][0]
    assert item["allowed_actions"] == ["CONTINUE", "WAIT", "ABANDON"]
    assert item["version"] == 1 and [s["step_id"] for s in item["steps"]] == steps
    assert "example.org" not in json.dumps(item)
    wait = {"expected_version": item["version"], "action": "WAIT",
            "wait_until": after(core.clock(), 60)}
    first = client.post(path, json=wait,
        headers={**headers, "Idempotency-Key": "wait-plan"})
    assert first.status_code == 200, first.text
    assert first.json()["version"] == item["version"] + 1
    assert client.post(path, json=wait,
        headers={**headers, "Idempotency-Key": "wait-plan"}).json() == first.json()
    item = client.get("/v1/console/goals").json()["items"][0]
    assert item["allowed_actions"] == ["WAIT", "ABANDON"]
    order = list(reversed(steps))
    continuation = {"expected_version": item["version"] - 1, "action": "CONTINUE",
                    "step_order": order}
    stale = client.post(path, json=continuation,
        headers={**headers, "Idempotency-Key": "stale-plan"})
    assert stale.status_code == 409 and stale.json()["error"] == "GOAL_VERSION_CHANGED"
    assert not core.store.rows("SELECT * FROM exploration_requests")
    core.clock.advance(61)
    continuation["expected_version"] = item["version"]
    continued = client.post(path, json=continuation,
        headers={**headers, "Idempotency-Key": "continue-plan"})
    assert continued.status_code == 200, continued.text
    assert continued.json()["state"] == "QUEUED"
    actual = core.goals.plan(goal)
    assert [step["step_id"] for step in actual["steps"]] == order
    assert [step["state"] for step in actual["steps"]] == [
        "QUEUED", "WAITING_DECISION", "WAITING_DECISION"]


@pytest.mark.parametrize("blocked", ["disabled", "uncertain"])
def test_goal_console_keeps_history_without_continuation_authority(client, core, blocked):
    goal, steps = retained_plan(core)
    core.settings.control_token = SecretStr(PASSWORD)
    if blocked == "disabled":
        core.settings.goals_enabled = False
    else:
        core.store.execute("UPDATE goal_steps SET state='EXECUTION_UNCERTAIN' WHERE step_id=%s",
                           (steps[0],))
    login(client)
    item = client.get("/v1/console/goals").json()["items"][0]
    assert item["goal_id"] == goal and item["steps"]
    assert item["allowed_actions"] == []


def test_tool_list_shows_the_exact_scope_an_approval_releases(client, core):
    from tests.support.executive import configured, proposal

    executive = configured(core, allow_changes=True)
    task = executive.submit(proposal(2, "windows.write_text", text="明天的购物清单"))
    headers = login(client)
    value = client.get("/v1/console/tools").json()
    item = next(row for row in value["items"] if row["task_id"] == task["task_id"])
    assert value["enabled"] is True and "scope_json" not in item
    scope = item["scope"]
    assert scope["state"] == "RECORDED" and scope["capability"] == "windows.write_text"
    assert scope["path"]["value"] == "result.txt"
    assert scope["text"]["value"] == "明天的购物清单"
    assert scope["sources_live"] and scope["source_count"] == 1
    assert "127.0.0.1" not in json.dumps(value) and "executor-test-token" not in json.dumps(value)
    url = f"/v1/console/tools/{task['task_id']}/decision"
    body = {"decision": "APPROVE", "scope_sha256": item["scope_sha256"]}
    response = client.post(url, json=body, headers={**headers, "Idempotency-Key": "approve-1"})
    assert response.status_code == 200, response.text
    assert core.store.one('SELECT state FROM tool_tasks WHERE task_id=%s',
                          (task["task_id"],))["state"] == "QUEUED"


def test_tool_scope_that_no_longer_matches_its_digest_is_not_shown(client, core):
    from tests.support.executive import configured, proposal

    executive = configured(core, allow_changes=True)
    task = executive.submit(proposal(3, "windows.write_text", text="原始内容"))
    scope = json.loads(task["scope_json"])
    scope["arguments"]["text"] = "被改过的内容"
    core.store.execute('UPDATE tool_tasks SET scope_json=%s WHERE task_id=%s',
                       (json.dumps(scope, ensure_ascii=False), task["task_id"]))
    login(client)
    item = next(row for row in client.get("/v1/console/tools").json()["items"]
                if row["task_id"] == task["task_id"])
    assert item["scope"] == {"state": "MISMATCH"}


@pytest.mark.parametrize("source_state", ["expired", "missing_evidence", "redacted"])
def test_tool_scope_hides_arguments_when_owner_sources_are_unavailable(client, core, source_state):
    from tests.support.executive import configured, proposal

    executive = configured(core, allow_changes=True)
    text = "synthetic-private-tool-content"
    task = executive.submit(proposal(4, "windows.write_text", text=text))
    login(client)
    if source_state == "expired":
        cutoff = after(core.clock(), -86400 * core.settings.raw_event_retention_days - 1)
        core.store.execute((
            "UPDATE events SET ingested_at=%s WHERE event_id='evt_test_1'"
        ), (cutoff,))
    elif source_state == "missing_evidence":
        core.store.execute('DELETE FROM tool_evidence WHERE task_id=%s', (task["task_id"],))
    else:
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    response = client.get("/v1/console/tools")
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["items"] if row["task_id"] == task["task_id"])
    assert item["scope"] == {"state": "UNAVAILABLE", "sources_live": False,
                             "source_count": 0 if source_state == "missing_evidence" else 1}
    assert text not in response.text and "result.txt" not in response.text
    assert item["state"] == "WAITING_APPROVAL"
    assert executive.get(task["task_id"])["scope_json"] == task["scope_json"]
    assert not core.store.rows("SELECT * FROM tool_approvals")


@pytest.mark.parametrize("change", ["epoch", "erasure"])
def test_tool_scope_privacy_change_during_http_read_fails_closed(client, core, monkeypatch, change):
    from companion import console_cognition
    from companion.privacy import advance_epoch
    from tests.support.executive import configured, proposal

    executive = configured(core, allow_changes=True)
    text = "synthetic-private-tool-content"
    task = executive.submit(proposal(5, "windows.write_text", text=text))
    login(client)
    project = console_cognition.tool_scope

    def changed(view_core, row):
        result = project(view_core, row)
        assert result["text"]["value"] == text
        if change == "epoch":
            advance_epoch(core.store)
        else:
            with core.store.transaction():
                core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        return result

    monkeypatch.setattr(console_cognition, "tool_scope", changed)
    response = client.get("/v1/console/tools")
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "PRIVACY_CHANGED"
    assert text not in response.text and "result.txt" not in response.text
    assert not core.store.rows("SELECT * FROM tool_approvals")
    if change == "erasure":
        assert executive.get(task["task_id"])["redacted"] == 1
        assert executive.sources(task["task_id"]) == []


def test_affect_view_distinguishes_disabled_from_neutral(client, core):
    login(client)
    core.settings.affect_enabled = False
    value = client.get("/v1/console/affect").json()
    assert value["enabled"] is False and value["reset_at"] is None
    assert value["state"]["engagement"] == "neutral"


@pytest.mark.parametrize("appraisal", ["这个回答很有帮助", "这个想法很有意思"])
def test_mood_console_authentication_controls_and_idempotent_clear(client, core, appraisal):
    from tests.support.mood import feedback

    feedback(core, text=appraisal)
    assert client.get("/v1/console/mood").status_code == 401
    headers = login(client)
    path = "/v1/console/mood/control"
    assert client.post(path, json={"action": "clear"}).status_code == 403
    current = client.get("/v1/console/mood").json()
    assert current["expression"]["pleasure"] > 0
    for action in ("pause", "resume", "recompute", "clear"):
        result = client.post(path, json={"action": action},
            headers={**headers, "Idempotency-Key": "mood-" + action})
        assert result.status_code == 200, result.text
    assert client.get("/v1/console/mood").json()["signals"] == []
    feedback(core, 2)
    retry = client.post(path, json={"action": "clear"},
        headers={**headers, "Idempotency-Key": "mood-clear"})
    assert retry.status_code == 200
    assert client.get("/v1/console/mood").json()["expression"]["pleasure"] > 0
