"""Real native activity admission; only external model transport is synthetic."""

import copy
import json

import pytest

from companion.contracts import CoreError
from companion.reading_protocol import ReadObservation
from companion.store import encode
from tests.conftest import run
from tests.support.exploration import URL
from tests.support.life_decision_context import prepared
from tests.support.reading_protocol import envelope
from tests.support.social_owner_authority import close_provider_clients as close_provider_clients


def choose(core, action="SHARE", **extra):
    proposal = dict(
        action=action, destination="PRIVATE_MESSAGE_DRAFT" if action == "SHARE" else "NONE",
        source_event_ids=["evt_test_1", "evt_test_2"], reason="依据本次证据判断",
        text="恒星的颜色与温度有关。" if action == "SHARE" else "",
    )
    core.model.activity = encode({**proposal, **extra})


@pytest.mark.parametrize("affect", [False, True])
def test_explicit_feedback_changes_actual_model_input_and_choice(core, tmp_path, affect):
    prepared(core, tmp_path, affect=affect)

    def provider():
        material = json.loads(core.model.calls[-1][0][-1]["content"])
        feedback = material["decision_context"]["owner_feedback"]
        assert bool(feedback) == affect
        if affect:
            assert feedback[0]["event_id"] == "evt_test_2"
            assert feedback[0]["new_state"] == "concise"
        choose(core, "SHARE" if feedback else "SKIP")

    core.model.callback = provider
    job = core.life_topics.schedule()
    assert job and run(core.run_once(job_id=job))
    row = core.store.one('SELECT * FROM life_activity_results WHERE job_id=%s', (job,))
    assert row["action"] == ("SHARE" if affect else "SKIP")
    assert len(core.model.calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert not core.store.rows("SELECT * FROM outbox")


def test_feedback_reset_invalidates_retained_wording_without_another_model(core, tmp_path):
    prepared(core, tmp_path)
    choose(core)
    job = core.life_topics.schedule()
    assert job and run(core.run_once(job_id=job))
    assert core.life_topics.retained_result(job)["result"]["text"]
    core.affect.reset(core.settings.owner_qq_id, "reset-life-feedback", core.ledger)
    with pytest.raises(CoreError, match="LIFE_DRAFT_CHANGED"):
        core.life_topics.retained_result(job)
    assert len(core.model.calls) == 1


def test_actual_goal_failure_informs_one_bounded_later_choice(core, tmp_path):
    prepared(core, tmp_path)
    choose(core, "EXPLORE", goal={
        "topic": "阅读天文介绍", "source_event_ids": ["evt_test_1", "evt_test_2"],
        "scope": {"operation": "read_content", "url": URL},
    })
    first = core.life_topics.schedule()
    assert first and run(core.run_once(job_id=first))
    result = core.store.one('SELECT * FROM life_activity_results WHERE job_id=%s', (first,))
    assert result["goal_id"]
    step = core.goals.claim()
    assert step and core.goals.fail(step, "SOURCE_NOT_AVAILABLE")
    core.clock.advance(3601)

    def provider():
        material = json.loads(core.model.calls[-1][0][-1]["content"])
        outcomes = material["decision_context"]["execution_outcomes"]
        assert outcomes == [{
            "goal_id": result["goal_id"], "version": core.goals.get(result["goal_id"])["version"],
            "state": "FAILED", "failure_code": "SOURCE_NOT_AVAILABLE",
            "requires_reconciliation": False, "source_event_ids": ["evt_test_1", "evt_test_2"],
        }]
        choose(core, "SKIP")

    core.model.callback = provider
    second = core.life_topics.schedule()
    assert second and second != first and run(core.run_once(job_id=second))
    core.clock.advance(86400)
    assert core.life_topics.schedule() is None
    assert len(core.model.calls) == 2
    assert len(core.store.rows("SELECT * FROM goals")) == 1
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("outcome", ["SUCCESS", "UNKNOWN"])
@pytest.mark.parametrize("autonomy", [False, True])
def test_native_read_receipt_supplies_interest_or_blocks_continuation(
    core, tmp_path, outcome, autonomy,
):
    prepared(core, tmp_path)
    core.settings.life_loop.autonomy_enabled = autonomy
    core.settings.memory_enabled = True
    payload = envelope("bilibili")
    choose(core, "EXPLORE", goal={
        "topic": "阅读天文介绍", "source_event_ids": ["evt_test_1", "evt_test_2"],
        "scope": {"operation": "read_content", "url": payload["observations"][0]["url"]},
    })
    first = core.life_topics.schedule()
    assert first and run(core.run_once(job_id=first))
    assert core.exploration.schedule_goal()
    item = core.reading.lease("bili")
    assert item
    if outcome == "SUCCESS":
        payload["lease_token"] = item["lease_token"]
        payload["observations"][0]["provenance"] = copy.deepcopy(item["context"])
        payload["observations"][0]["fetched_at"] = core.clock()
        payload["observations"][0]["evidence"]["fetched_at"] = core.clock()
        result = core.reading.complete("bili", item["request_id"],
                                       ReadObservation.model_validate(payload))
        assert result["event_ids"]
    else:
        result = core.reading.complete("bili", item["request_id"], ReadObservation(
            schema_version=1, lease_token=item["lease_token"], status="UNKNOWN",
            observations=[], error_code="READER_CONNECTION_LOST",
            used_requests=None, used_bytes=None, observed_requests=1, observed_bytes=0,
        ))
        assert result["state"] == "UNKNOWN"
    core.clock.advance(3601)
    second = core.life_topics.schedule()
    if outcome == "UNKNOWN":
        assert second is None
        assert len(core.model.calls) == 1
        return
    assert second
    seen = []

    def provider():
        material = json.loads(core.model.calls[-1][0][-1]["content"])
        seen.append(material)
        choose(core, "SHARE", source_event_ids=[source["event_id"]
                                               for source in material["sources"]])

    core.model.callback = provider
    assert run(core.run_once(job_id=second))
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (second,))["state"] == (
        "SUCCEEDED"
    ), core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s', (second,))
    assert {item["event_id"] for item in seen[0]["sources"]} == {
        "evt_test_1", "evt_test_2", *result["event_ids"],
    }
    context = seen[0]["decision_context"]
    assert context["self_interests"], context
    assert context["self_interests"][0]["namespace"] == "self.interest"
    assert context["self_interests"][0]["independent_sessions"] == 1
    assert context["self_interests"][0]["independent_days"] == 1
    assert context["execution_outcomes"][0]["state"] == "DONE"
    core.clock.advance(60)
    retained = core.life_topics.retained_result(second)
    assert retained["result"]["text"]
    # Association is verified readback metadata; model proposals stay strict.
    assert retained["related_trait_ids"] == [context["self_interests"][0]["trait_id"]]
    assert "related_trait_ids" not in json.loads(retained["result"]["proposal_json"])
    proposal = json.loads(retained["result"]["proposal_json"])
    for invalid in (
        retained["related_trait_ids"], ["unrelated-trait"], "unrelated-trait", None, [],
    ):
        changed = {**proposal, "related_trait_ids": invalid}
        core.store.execute('UPDATE life_activity_results SET proposal_json=%s WHERE job_id=%s',
                           (encode(changed), second))
        with pytest.raises(CoreError, match="LIFE_DRAFT_CHANGED"):
            core.life_topics.retained_result(second)
    core.store.execute('UPDATE life_activity_results SET proposal_json=%s WHERE job_id=%s',
                       (retained["result"]["proposal_json"], second))
    assert core.life_topics.retained_result(second)["result"]["text"]
    if autonomy:
        assert context["autonomy_guidance"]["depth_control"][0]["independent_sessions"] == 1
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    saved = core.store.one('SELECT * FROM life_activity_results WHERE job_id=%s', (second,))
    assert saved["state"] == "INVALIDATED" and saved["text"] == ""
    assert len(core.model.calls) == 2
