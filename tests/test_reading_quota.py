"""Configured deep-read quotas retain native UNKNOWN accounting and stop at the bound."""

from companion.config import Settings
from companion.exploration import ReadResult
from tests.support.exploration import URL, configure, discover


def test_explicit_thirty_read_quota_keeps_unknown_reservations_and_stops_at_thirty(core):
    configure(core)
    cfg = core.settings
    cfg.exploration_daily_requests = 100
    cfg.exploration_monthly_requests = 3000
    cfg.exploration_daily_bytes = 104857600
    approved = Settings.model_validate({**cfg.model_dump(), "exploration_daily_deep_reads": 30})
    cfg.exploration_daily_deep_reads = approved.exploration_daily_deep_reads
    discover(core)
    goal = core.store.one("SELECT goal_id FROM goals")
    sources = core.goals.source_ids(goal["goal_id"])
    for number in range(31):
        with core.store.transaction():
            request = core.exploration.create(
                "bili", "read", f"quota-thirty-{number}", URL, source_event_ids=sources,
            )
        lease = core.exploration.lease("bili")
        if number == 30:
            assert lease is None
            break
        assert lease and lease["request_id"] == request
        core.exploration.complete("bili", request, ReadResult(
            lease_token=lease["lease_token"], status="UNKNOWN", error_code="NETWORK_TIMEOUT",
        ))
    unknown = core.store.rows("SELECT * FROM exploration_requests WHERE operation='read' "
                              "AND state='UNKNOWN'")
    assert len(unknown) == 30
    assert all(row["used_bytes"] is None and row["reserved_bytes"] > 0 for row in unknown)
    assert core.store.one("SELECT COUNT(*) n FROM exploration_requests "
                          "WHERE operation='read' AND state='QUEUED'")["n"] == 1
