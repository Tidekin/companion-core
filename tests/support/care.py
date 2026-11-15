"""Source-bound caring fixtures; each scenario owns its database and clock."""

from datetime import datetime

import pytest

from companion import care
from companion.care_protocol import CarePlan
from companion.store import after
from tests.conftest import ingest


@pytest.fixture
def caring(core):
    core.clock.now = datetime.fromisoformat("2026-11-16T10:00:00+00:00")
    core.settings.proactive_enabled = True
    core.settings.care.jitter_seconds = 0
    core.settings.quiet_start = core.settings.quiet_end = 0
    return core


def owner_job(core, number, text):
    source = ingest(core, number, text)
    job = core.store.one(
        "SELECT j.*,e.trace_id FROM jobs j JOIN events e USING(event_id) WHERE j.event_id=%s",
        (source["event_id"],),
    )
    return source, job


def plan(core, number=1, *, seconds=3600):
    source, job = owner_job(core, number, f"今天要出门，晚些时候回家，计划{number}。")
    request = CarePlan(
        about=f"回家计划{number}",
        quote=f"晚些时候回家，计划{number}",
        check_at=after(core.clock(), seconds),
    )
    result = care.change(core.store, core.settings, job, "call-care", request, core.clock())
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    return result, source, job, request
