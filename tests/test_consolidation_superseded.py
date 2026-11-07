"""Nightly memory drains valid owner evidence after reply supersession."""

import json
from datetime import datetime

from tests.conftest import run
from tests.support.diary import DAY
from tests.support.reply_revision import accept


def test_consolidation_drains_more_than_twenty_superseded_owner_inputs(core):
    core.settings.consolidation_enabled = core.settings.memory_enabled = True
    core.settings.extraction_min_events = 3
    core.settings.extraction_idle_seconds = 0
    core.clock.now = datetime.fromisoformat("2026-10-18T04:00:00+00:00")
    for number in range(1, 24):
        accept(core, number, text=f"整理第{number}段项目资料，今天学习恒星光谱。")
    assert run(core.run_once())
    cancelled = core.store.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE last_error_code='REPLY_SUPERSEDED'",
    )
    assert cancelled["n"] == 22
    core.clock.now = datetime.fromisoformat("2026-10-19T04:00:00+00:00")
    assert core.consolidation.schedule_day(DAY)
    for _ in range(20):
        assert run(core.consolidation.run_step()).progressed
        status = core.consolidation.status(DAY)
        remaining = core.store.one("SELECT COUNT(*) AS n FROM events WHERE memory_processed=0")["n"]
        if (json.loads(status["steps_json"]).get("memory") == "WAITING"
            and remaining == 3):
            break
    assert status["state"] == "PARTIAL" and json.loads(status["steps_json"])["memory"] == "WAITING"
    assert core.store.one("SELECT COUNT(*) AS n FROM events WHERE memory_processed=0")["n"] == 3
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    assert not core.store.one("SELECT 1 FROM events WHERE memory_processed=0")
    assert [scenario for _, scenario in core.model.calls].count("memory_extract") == 2
