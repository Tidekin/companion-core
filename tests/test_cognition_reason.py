import json

import pytest

from companion.cognition_read import CognitionRead
from companion.privacy import epoch
from tests.conftest import run
from tests.support.confirmed_memory import remember
from tests.support.diary import generate, seed_day
from tests.support.life_topics import bug, due, proposal


def life_result(core, reason="今天先等新线索", action="SKIP"):
    bug(core)
    extra = {"defer_seconds": 300} if action == "DEFER" else {}
    proposal(core, action=action, destination="NONE", text="", reason=reason, **extra)
    job = due(core)
    assert job
    assert run(core.run_once("cognition-reason", job_id=job))
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job,))["state"] == "SUCCEEDED"
    return job


@pytest.mark.parametrize("action", ["SKIP", "DEFER"])
def test_native_reason_and_historical_candidate_survive_unrelated_memory(core, action):
    job = life_result(core, action=action)
    view = CognitionRead(core)
    before = view.page("life", record_id=job)["items"][0]
    assert before["decision_reason"] == {"kind": "decision_reason", "state": "RECORDED",
                                         "text": "今天先等新线索", "truncated": False}
    assert before["candidate_reference"]["source_event_ids"] == ["evt_test_1"]
    assert before["candidate_reference"]["allowed_items"] == {"state": "NOT_RECORDED"}
    old_epoch = epoch(core.store)
    remember(core, 99, "另一项目封面使用绿色", confirmed=True)
    assert epoch(core.store) > old_epoch
    assert view.page("life", record_id=job)["items"][0] == before
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert view.page("life", record_id=job)["record_status"] == "NOT_RECORDED"


def test_completed_diary_survives_unrelated_memory_but_not_source_erasure(core):
    seed_day(core)
    saved = generate(core)
    assert saved["state"] == "COMMITTED"
    view = CognitionRead(core)
    before = view.page("diary", record_id=saved["run_id"])["items"][0]
    remember(core, 99, "另一项目封面使用绿色", confirmed=True)
    assert epoch(core.store) > saved["captured_privacy_epoch"]
    assert view.page("diary", record_id=saved["run_id"])["items"][0] == before
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert view.page("diary", record_id=saved["run_id"])["items"] == []


@pytest.mark.parametrize("reason", ["Bearer synthetic-secret-value", "记录了病历内容"])
def test_saved_sensitive_reason_never_leaks(core, reason):
    job = life_result(core, reason=reason)
    result = CognitionRead(core).page("life", record_id=job)
    assert result["items"][0]["decision_reason"]["state"] == "REDACTED"
    assert reason not in json.dumps(result, ensure_ascii=False)
    assert result["reasoning"] == "NOT_EXPOSED"


def test_pending_native_record_has_no_inferred_reason(core):
    bug(core)
    job = due(core)
    result = CognitionRead(core).page("life", record_id=job)
    assert result["items"][0]["decision_reason"] == {
        "kind": "decision_reason", "state": "NOT_RECORDED"}
