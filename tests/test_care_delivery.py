"""Care cancellation keeps original money, attempt and uncertain delivery custody."""

import json

import pytest

from companion import care, notices
from companion.care_protocol import CareClosure
from companion.contracts import DeliveryResult, LeaseRequest
from companion.model import ModelTransportError
from companion.transport import begin_send, delivery_result, lease_deliveries
from tests.conftest import run
from tests.notice_owner_fixtures import run_notice
from tests.support.care import caring as caring
from tests.support.care import owner_job, plan
from tests.support.notices import NoticeModel


def queued(core, wording="你到家了吗？"):
    result, source, _, _ = plan(core)
    core.clock.advance(3600)
    assert care.flush(core.store, core.settings, core.clock())
    core.model = NoticeModel(wording)
    return result, source


def lease(core):
    return lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="care-bridge"), core.clock()
    )["items"]


def test_wording_can_skip_without_refunding_the_care_attempt(caring):
    core = caring
    queued(core, "[SKIP]")
    assert run(run_notice(core))
    assert (
        care._row(core.store, core.store.one("SELECT plan_id FROM care_plans")["plan_id"])[
            "reason_code"
        ]
        == "MODEL_SKIPPED"
    )
    assert core.store.one("SELECT state FROM jobs WHERE kind='activity'")["state"] == "SUCCEEDED"
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert not lease(core)
    assert not care.flush(core.store, core.settings, core.clock())
    assert len(core.model.calls) == 1


@pytest.mark.parametrize("phase", ["queued", "worded", "sending"])
def test_source_erasure_cancels_care_at_every_unsent_boundary(caring, phase):
    core = caring
    result, source = queued(core)
    if phase != "queued":
        assert run(run_notice(core))
    original_usage = core.store.rows("SELECT * FROM model_usage")
    if phase == "sending":
        item = lease(core)[0]
        begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [source["event_id"]], core.clock())
    row = care._row(core.store, result["plan_id"])
    assert row["state"] == "ERASED" and row["detail_json"] == "{}" and row["due_at"] is None
    assert core.store.rows("SELECT * FROM model_usage") == original_usage
    assert not lease(core) and not care.flush(core.store, core.settings, core.clock())
    if phase == "sending":
        assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
    elif phase == "worded":
        assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"


def test_new_owner_message_invalidates_wording_before_external_delivery(caring):
    core = caring
    queued(core)
    assert run(run_notice(core))
    owner_job(core, 2, "已经到家了，刚才忘了说。")
    assert not lease(core)
    assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
    assert len(core.model.calls) == 1


def test_uncertain_delivery_is_never_recreated_or_released_by_closing_plan(caring):
    core = caring
    result, _ = queued(core)
    assert run(run_notice(core))
    item = lease(core)[0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    delivery_result(
        core.store,
        core.settings,
        item["message_id"],
        DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN"),
        core.clock(),
    )
    _, job = owner_job(core, 2, "我到家了。")
    care.change(
        core.store,
        core.settings,
        job,
        "arrived",
        CareClosure(plan_id=result["plan_id"], expected_version=1, quote="我到家了"),
        core.clock(),
    )
    assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
    assert not lease(core) and not care.flush(core.store, core.settings, core.clock())
    assert len(core.model.calls) == 1


def test_acknowledged_care_keeps_original_evidence_after_owner_resolves(caring):
    core = caring
    result, _ = queued(core)
    assert run(run_notice(core))
    item = lease(core)[0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    delivery_result(
        core.store,
        core.settings,
        item["message_id"],
        DeliveryResult(lease_token=item["lease_token"], result="SENT"),
        core.clock(),
    )
    job = core.store.one(
        "SELECT j.*,e.body_json FROM jobs j JOIN events e USING(event_id) WHERE kind='activity'"
    )
    original = notices.validate(core.store, core.settings, job, core.clock())
    _, owner = owner_job(core, 2, "我已经到家了。")
    care.change(
        core.store,
        core.settings,
        owner,
        "resolved",
        CareClosure(plan_id=result["plan_id"], expected_version=1, quote="已经到家了"),
        core.clock(),
    )
    assert notices.validate(core.store, core.settings, job, core.clock()) == original
    assert (
        json.loads(core.store.one("SELECT content_json FROM outbox")["content_json"])["text"]
        == "你到家了吗？"
    )


def test_unknown_wording_attempt_is_not_retried(caring):
    core = caring
    queued(core)
    core.model.error = ModelTransportError("MODEL_TRANSPORT_UNKNOWN", 503, "ReadTimeout")
    # Use the normal fake transport error boundary, retaining the original reservation.
    from tests.conftest import FakeModel

    model = FakeModel()
    model.error = core.model.error
    core.model = model
    assert run(run_notice(core))
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    core.clock.advance(1000)
    assert not care.flush(core.store, core.settings, core.clock())
    assert not lease(core) and len(core.model.calls) == 1
