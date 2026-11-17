"""The approved Life cap counts drafts still waiting, not drafts already sent or failed runs."""

from companion import life_protocol
from companion.life_topics import waiting_drafts
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.moments import moment_ready


def _authority(core, job_id):
    return core.store.one("SELECT authority_sha256 FROM life_activity_results WHERE job_id=%s",
                          (job_id,))["authority_sha256"]


def test_a_sent_draft_frees_its_slot(core, tmp_path):
    job, *_ = moment_ready(core, tmp_path)
    authority = _authority(core, job)
    assert waiting_drafts(core.store, authority) == 1
    assert core.life_topics.consume_due(diary=core.diary)
    assert core.store.one("SELECT 1 FROM moments WHERE source_job_id=%s", (job,))
    assert waiting_drafts(core.store, authority) == 0


def test_a_failed_decision_frees_its_slot(core, tmp_path):
    job, *_ = moment_ready(core, tmp_path)
    authority = _authority(core, job)
    core.store.execute("UPDATE life_activity_results SET state='PENDING' WHERE job_id=%s", (job,))
    core.store.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=%s", (job,))
    assert waiting_drafts(core.store, authority) == 1
    core.store.execute("UPDATE jobs SET state='FAILED' WHERE job_id=%s", (job,))
    assert waiting_drafts(core.store, authority) == 0


def test_a_decision_not_to_express_may_omit_its_destination():
    decision = life_protocol.LifeDecision.model_validate(
        {"action": "SKIP", "source_event_ids": [], "reason": "没有新内容"})
    assert decision.destination == "NONE"
    for action in ("SHARE", "FOLLOW_UP"):
        try:
            life_protocol.LifeDecision.model_validate(
                {"action": action, "source_event_ids": ["evt_a"], "reason": "r", "text": "t"})
        except ValueError:
            continue
        raise AssertionError("expression without a destination was accepted")
