"""Timed, source-bound caring uses native activity jobs and preserves other quotas."""

from datetime import datetime

import pytest

from companion import care, notices, social
from companion.care_protocol import CareClosure, CarePlan
from companion.contracts import CoreError
from companion.store import after
from tests.support.care import caring as caring
from tests.support.care import owner_job, plan


def test_morning_intent_becomes_one_evening_activity(caring):
    core = caring
    core.clock.now = datetime.fromisoformat("2026-11-16T00:00:00+00:00")
    result, source, job, request = plan(core, seconds=11 * 3600)
    assert not care.flush(core.store, core.settings, core.clock())
    assert care.change(core.store, core.settings, job, "call-care", request, core.clock()) == result
    assert core.store.one("SELECT count(*) AS n FROM care_plans")["n"] == 1
    core.clock.advance(11 * 3600)
    assert care.flush(core.store, core.settings, core.clock())
    assert not care.flush(core.store, core.settings, core.clock())
    row = core.store.one("SELECT * FROM care_plans")
    notice = core.store.one(
        "SELECT j.*,e.body_json,e.trace_id FROM jobs j JOIN events e "
        "USING(event_id) WHERE j.job_id=%s",
        (row["job_id"],),
    )
    material = notices.validate(core.store, core.settings, notice, core.clock())
    assert material["kind"] == "care" and material["topic"] == request.about
    assert source["event_id"] in material["source_events"]
    assert material == notices.validate(core.store, core.settings, notice, after(core.clock(), 5))
    assert social.proactive_counts(core.store, core.settings, core.clock()) == (0, False)
    assert not core.model.calls


def test_arrival_closes_the_source_plan_without_a_later_question(caring):
    core = caring
    result, source, _, _ = plan(core)
    core.clock.advance(1800)
    arrived, job = owner_job(core, 2, "我已经到家了，刚洗完手。")
    closed = care.change(
        core.store,
        core.settings,
        job,
        "arrived",
        CareClosure(plan_id=result["plan_id"], expected_version=1, quote="我已经到家了"),
        core.clock(),
    )
    assert closed["state"] == "CLOSED"
    core.clock.advance(1801)
    assert not care.flush(core.store, core.settings, core.clock())
    assert not core.store.rows("SELECT * FROM jobs WHERE idempotency_key LIKE 'notice:care:%%'")
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id=%s", (source["event_id"],))[
            "redacted"
        ]
        == 0
    )
    assert arrived["event_id"] in care._sources(
        core.store, care._row(core.store, result["plan_id"])["intent_event_id"]
    )


def test_time_update_withdraws_old_queued_work_and_keeps_evidence(caring):
    core = caring
    result, source, _, _ = plan(core)
    core.clock.advance(3600)
    assert care.flush(core.store, core.settings, core.clock())
    original_job = care._row(core.store, result["plan_id"])["job_id"]
    later, job = owner_job(core, 2, "改成两个小时后回家。")
    changed = care.change(
        core.store,
        core.settings,
        job,
        "move-time",
        CarePlan(
            plan_id=result["plan_id"],
            expected_version=1,
            quote="两个小时后回家",
            about="回家时间推迟",
            check_at=after(core.clock(), 7200),
        ),
        core.clock(),
    )
    assert changed["version"] == 2
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    assert (
        core.store.one("SELECT state FROM jobs WHERE job_id=%s", (original_job,))["state"]
        == "CANCELLED"
    )
    assert {source["event_id"], later["event_id"]} <= set(
        care._sources(core.store, care._row(core.store, result["plan_id"])["intent_event_id"])
    )
    core.clock.advance(7200)
    assert care.flush(core.store, core.settings, core.clock())
    assert (
        len(core.store.rows("SELECT * FROM jobs WHERE idempotency_key LIKE 'notice:care:%%'")) == 2
    )


@pytest.mark.parametrize("block", ["quiet", "active", "expired", "disabled"])
def test_due_plan_does_not_interrupt_or_replay_a_stale_check(caring, block):
    core = caring
    result, _, _, _ = plan(core)
    core.clock.advance(3600)
    if block == "quiet":
        core.settings.quiet_start, core.settings.quiet_end = 18, 21
    elif block == "active":
        owner_job(core, 2, "刚才说到的那本书，我又看了几页。")
    elif block == "expired":
        core.clock.advance(core.settings.care.useful_seconds + 1)
    else:
        core.settings.care.enabled = False
    assert not care.flush(core.store, core.settings, core.clock())
    assert care._row(core.store, result["plan_id"])["state"] == "CLOSED"
    core.clock.advance(86400)
    assert not care.flush(core.store, core.settings, core.clock())


def test_daily_care_cap_is_separate_and_survives_source_erasure(caring):
    core = caring
    plans = [plan(core, number)[0] for number in range(1, 4)]
    core.clock.advance(3600)
    assert care.flush(core.store, core.settings, core.clock())
    assert care.flush(core.store, core.settings, core.clock())
    assert not care.flush(core.store, core.settings, core.clock())
    assert (
        core.store.one("SELECT count(*) AS n FROM care_plans WHERE reason_code='DAILY_LIMIT'")["n"]
        == 1
    )
    assert social.proactive_counts(core.store, core.settings, core.clock()) == (0, False)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert care._row(core.store, plans[0]["plan_id"])["state"] == "ERASED"
    assert social.proactive_counts(core.store, core.settings, core.clock()) == (0, False)
    plan(core, 4, seconds=900)
    core.clock.advance(900)
    assert not care.flush(core.store, core.settings, core.clock())


def test_jitter_is_stable_and_quoted_evidence_is_required(caring):
    core = caring
    core.settings.care.jitter_seconds = 600
    result, _, job, request = plan(core)
    assert result["due_at"] != request.check_at.isoformat(timespec="microseconds")
    assert abs((datetime.fromisoformat(result["due_at"]) - request.check_at).total_seconds()) <= 600
    with pytest.raises(CoreError, match="CARE_QUOTE_UNSUPPORTED"):
        care.change(
            core.store,
            core.settings,
            job,
            "invented",
            CarePlan(about="面试", quote="明天面试", check_at=request.check_at),
            core.clock(),
        )
    assert care.change(core.store, core.settings, job, "call-care", request, core.clock()) == result


def test_many_reschedules_always_leave_room_for_owner_to_close(caring):
    core = caring
    result, _, _, _ = plan(core)
    for number in range(2, 32):
        _, job = owner_job(core, number, f"改成{number}分钟后回家。")
        result = care.change(
            core.store,
            core.settings,
            job,
            "move",
            CarePlan(
                plan_id=result["plan_id"],
                expected_version=result["version"],
                about="回家时间调整",
                quote=f"{number}分钟后回家",
                check_at=after(core.clock(), number * 60),
            ),
            core.clock(),
        )
    _, job = owner_job(core, 32, "又要晚一点回家，不过现在不用问了。")
    with pytest.raises(CoreError, match="CARE_SOURCE_LIMIT"):
        care.change(
            core.store,
            core.settings,
            job,
            "over-limit",
            CarePlan(
                plan_id=result["plan_id"],
                expected_version=31,
                about="再晚点回家",
                quote="晚一点回家",
                check_at=after(core.clock(), 3600),
            ),
            core.clock(),
        )
    closed = care.change(
        core.store,
        core.settings,
        job,
        "cancel",
        CareClosure(
            plan_id=result["plan_id"], expected_version=31, quote="现在不用问了", resolved=False
        ),
        core.clock(),
    )
    assert closed["state"] == "CLOSED"
    row = care._row(core.store, result["plan_id"])
    assert len(care._sources(core.store, row["intent_event_id"])) == 64
