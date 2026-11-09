"""Original Reader outcomes reach Life without losing uncertain execution."""
import pytest

from companion.reading_protocol import ReadObservation
from tests.conftest import run
from tests.support.goal_read_plan import create, prepared


@pytest.mark.parametrize("status,metered,uncertain", [
    ("UNKNOWN", False, True), ("CANCELLED", False, True), ("FAILED", True, False),
])
def test_native_reader_propagates_original_execution_uncertainty(
    core, tmp_path, monkeypatch, status, metered, uncertain,
):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            item = core.reading.lease("bili")
            calls = []
            original = core.goals.fail

            def failure(*args, **kwargs):
                calls.append(kwargs["execution_uncertain"])
                return original(*args, **kwargs)

            monkeypatch.setattr(core.goals, "fail", failure)
            receipt = ReadObservation(schema_version=1, lease_token=item["lease_token"],
                status=status, observations=[], error_code="SYNTHETIC_READER_OUTCOME",
                used_requests=1 if metered else None, used_bytes=100 if metered else None,
                observed_requests=1, observed_bytes=100)
            result = core.reading.complete("bili", item["request_id"], receipt)
            assert calls == [uncertain]
            step = core.store.one('SELECT state FROM goal_steps WHERE goal_id=%s AND position=1',
                                  (goal_id,))
            assert step["state"] == ("EXECUTION_UNCERTAIN" if uncertain else "FAILED")
            before = core.store.rows("SELECT * FROM exploration_requests")
            assert before[0]["request_id"] == item["request_id"]
            assert not core.exploration.schedule_goal()
            assert core.reading.complete("bili", item["request_id"], receipt) == {
                **result, "duplicate": True}
            assert core.store.rows("SELECT * FROM exploration_requests") == before
            assert calls == [uncertain]
    run(scenario())
