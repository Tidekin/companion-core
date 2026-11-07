"""Model-proposed continuations cannot change native scope, version or unknown custody."""

import copy
import json

import pytest

from companion.reading_protocol import ReadObservation
from companion.store import after, encode
from tests.conftest import run
from tests.life_plan_process import choice
from tests.support.reading_protocol import envelope
from tests.support.social_owner_authority import close_provider_clients as close_provider_clients
from tests.test_life_read_plan import prepare


def turn(core, phase, mutate=None):
    def provider():
        material = json.loads(core.model.calls[-1][0][-1]["content"])
        result = choice(material, phase, core.clock())
        if mutate:
            mutate(material, result)
        core.model.activity = encode(result)

    core.model.callback = provider
    job = core.life_topics.schedule()
    assert job
    assert run(core.run_once(job_id=job))
    return core.store.one("SELECT * FROM life_activity_results WHERE job_id=?", (job,))


def first_read(core, tmp_path, *, unknown=False):
    prepare(core, tmp_path)
    assert turn(core, "PROPOSE")["state"] == "SHADOW"
    assert core.exploration.schedule_goal()
    item = core.reading.lease("bili")
    assert item
    if unknown:
        payload = ReadObservation(schema_version=1, lease_token=item["lease_token"],
            status="UNKNOWN", observations=[], used_requests=None, used_bytes=None,
            observed_requests=1, observed_bytes=0, error_code="READER_CONNECTION_LOST")
    else:
        payload = envelope("bilibili")
        payload["lease_token"] = item["lease_token"]
        payload["observations"][0]["provenance"] = copy.deepcopy(item["context"])
        payload["observations"][0]["fetched_at"] = core.clock()
        payload["observations"][0]["evidence"]["fetched_at"] = core.clock()
        payload = ReadObservation.model_validate(payload)
    completed = core.reading.complete("bili", item["request_id"], payload)
    core.clock.advance(61)
    return core.store.one("SELECT goal_id FROM goals")["goal_id"], completed


def test_life_cannot_invent_a_plan_target(core, tmp_path):
    prepare(core, tmp_path)

    def change(_, decision):
        decision["goal"]["scope"]["urls"] = ["https://www.bilibili.com/video/BV1xx411c7mZ"]

    result = turn(core, "PROPOSE", change)
    assert result["state"] != "SHADOW"
    assert not core.store.rows("SELECT * FROM goals")
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


@pytest.mark.parametrize("change", ["BAD_ORDER", "STALE_VERSION", "WAIT_EXPIRED",
                                    "MISSING_EVIDENCE", "INFLIGHT_VERSION"])
def test_plan_continuation_rejects_stale_or_expanded_proposals(core, tmp_path, change):
    goal_id, _ = first_read(core, tmp_path)
    before = core.goals.plan(goal_id)

    def mutate(material, decision):
        if change == "WAIT_EXPIRED":
            decision["continuation"]["wait_until"] = after(material["topic_expires_at"], 1)
        elif change == "MISSING_EVIDENCE":
            decision["source_event_ids"] = ["evt_test_1", "evt_test_2"]
        elif change == "INFLIGHT_VERSION":
            core.goals.continue_plan(goal_id, {"action": "WAIT",
                "expected_version": before["version"], "wait_until": after(core.clock(), 600)})

    phase = (change if change in {"BAD_ORDER", "STALE_VERSION"}
             else "WAIT" if change == "WAIT_EXPIRED" else "CONTINUE")
    result = turn(core, phase, mutate)
    assert result["state"] != "SHADOW"
    current = core.goals.plan(goal_id)
    assert current["steps"] == before["steps"]
    assert current["version"] == before["version"] + int(change == "INFLIGHT_VERSION")
    assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
    assert [row["state"] for row in core.store.rows("SELECT * FROM model_usage")] == [
        "SETTLED", "SETTLED",
    ]


@pytest.mark.parametrize("change", ["UNKNOWN", "ERASE"])
def test_plan_unknown_or_erasure_blocks_new_life_model_admission(core, tmp_path, change):
    goal_id, result = first_read(core, tmp_path, unknown=change == "UNKNOWN")
    if change == "ERASE":
        with core.store.transaction():
            core.ledger.expire_events(core.store, result["event_ids"], core.clock())
    else:
        assert core.goals.plan(goal_id)["unknown"]
    assert core.life_topics.schedule() is None
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1
    assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
    assert not core.store.rows("SELECT * FROM outbox")
