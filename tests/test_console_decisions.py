"""The decision observatory reads source-checked native records without reasoning."""

import pytest

from companion.store import after
from tests.conftest import run
from tests.support.console_cognition import client as client
from tests.support.console_cognition import login
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY


def test_decisions_list_wake_and_reflection_records_without_reasoning(core, client):
    model = enable_nightly(core)
    reflection = run(core.reflections.run_day(DAY))
    assert reflection["state"] == "COMMITTED"
    core.settings.wake_enabled = core.settings.goals_enabled = True
    core.wake.evaluate()
    core.clock.advance(120)  # Reading later must not rewrite the original record time.
    paths = ["/v1/console/cognition?kind=" + kind for kind in ("wake", "reflection")]
    for path in paths:
        assert client.get(path).status_code == 401
    headers = login(client)
    calls = list(model.calls)
    reader = client.app.state.core
    changes = reader.store.db.total_changes
    pages = []
    # These are the same kind-specific reads used by the current thinking page.
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert page["reasoning"] == "NOT_EXPOSED"
        assert page["candidate_set"] == "NOT_RECORDED"
        assert "下次可以再了解" not in str(page)
        pages.append(page)
    wake = pages[0]["items"][0]
    assert wake["choice"] == "SKIP" and wake["source_evidence"] == "NOT_RECORDED"
    original_wake = core.store.one("SELECT decision_id,created_at FROM wake_decisions")
    assert wake["id"] == original_wake["decision_id"]
    assert wake["occurred_at"] == original_wake["created_at"] != pages[0]["as_of"]
    item = pages[1]["items"][0]
    assert item["id"] == reflection["run_id"] and item["state"] == "COMMITTED"
    assert item["source_evidence"] == "RECORDED"
    original = core.store.one(
        'SELECT model_request_id,created_at FROM reflection_runs WHERE run_id=%s',
        (reflection["run_id"],))
    assert item["model_request_id"] == original["model_request_id"]
    assert item["occurred_at"] == original["created_at"] != pages[1]["as_of"]
    assert reader.store.db.total_changes == changes and model.calls == calls
    # Suggestion content belongs to the separate Life read, not cognition metadata.
    suggestions = client.get("/v1/console/reflections").json()["items"]
    assert len(suggestions) == 1 and suggestions[0]["run_id"] == item["id"]
    assert client.delete("/v1/console/session", headers=headers).status_code == 200
    for path in paths:
        assert client.get(path).status_code == 401


@pytest.mark.parametrize("withdrawal", ["redacted", "non_owner", "expired"])
def test_console_decisions_hide_reflection_when_original_source_is_unavailable(
    core, client, withdrawal,
):
    model = enable_nightly(core)
    reflection = run(core.reflections.run_day(DAY))
    assert reflection["state"] == "COMMITTED"
    login(client)
    path = "/v1/console/cognition"
    query = {"kind": "reflection", "record_id": reflection["run_id"]}
    before = client.get(path, params=query)
    assert before.status_code == 200 and len(before.json()["items"]) == 1
    source = before.json()["items"][0]["source_event_ids"][0]
    if withdrawal == "redacted":
        core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s', (source,))
    elif withdrawal == "non_owner":
        core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id=%s", (source,))
    else:
        core.store.execute('UPDATE events SET ingested_at=%s WHERE event_id=%s', (
            after(core.clock(), -86400 * (core.settings.raw_event_retention_days + 1)), source))
    calls = list(model.calls)
    changes = client.app.state.core.store.db.total_changes
    hidden = client.get(path, params=query)
    assert hidden.status_code == 200 and hidden.headers["cache-control"] == "no-store"
    assert hidden.json()["items"] == []
    assert hidden.json()["record_status"] == "NOT_RECORDED"
    assert hidden.json()["reasoning"] == "NOT_EXPOSED"
    assert client.app.state.core.store.db.total_changes == changes and model.calls == calls
