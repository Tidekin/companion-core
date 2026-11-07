"""Expired Reader custody is retired without replaying reads or discarding metering."""

import pytest

from companion.contracts import CoreError
from companion.reading_protocol import ReadObservation
from tests.conftest import run
from tests.support.reading_service import queued


@pytest.mark.parametrize("entry", ["recover", "lease", "worker"])
@pytest.mark.parametrize("parent_recovered", [False, True])
def test_expired_attempt_recovers_at_native_boundaries(core, entry, parent_recovered):
    adapter, item, _ = queued(core, owner=False)
    before = core.store.one("SELECT * FROM reading_attempts")
    reservation = core.store.one(
        "SELECT reserved_requests,reserved_bytes,used_requests,used_bytes,started_at "
        "FROM exploration_requests")
    core.clock.advance(31)
    if parent_recovered:
        core.store.execute("UPDATE exploration_requests SET state='UNKNOWN',"
                           "last_error_code='READ_TIMEOUT'")
    if entry == "recover":
        core.exploration.recover()
    elif entry == "lease":
        assert core.reading.lease(adapter) is None
    else:
        assert not run(core.run_once("recovery-worker"))
    after = core.store.one("SELECT * FROM reading_attempts")
    assert after == {**before, "state": "UNKNOWN", "updated_at": core.clock()}
    assert core.store.one("SELECT state,last_error_code FROM exploration_requests") == {
        "state": "UNKNOWN", "last_error_code": "READ_TIMEOUT"}
    assert core.store.one(
        "SELECT reserved_requests,reserved_bytes,used_requests,used_bytes,started_at "
        "FROM exploration_requests") == reservation
    assert not core.store.rows("SELECT * FROM observations")
    assert not core.model.calls
    core.exploration.recover()
    assert core.store.one("SELECT * FROM reading_attempts") == after
    assert core.store.one("SELECT request_id FROM exploration_requests")["request_id"] == item[
        "request_id"]


def test_live_reader_lease_survives_recovery(core):
    queued(core, owner=False)
    parent = core.store.one("SELECT * FROM exploration_requests")
    attempt = core.store.one("SELECT * FROM reading_attempts")
    assert not run(core.run_once("recovery-worker"))
    core.exploration.recover()
    assert core.store.one("SELECT * FROM exploration_requests") == parent
    assert core.store.one("SELECT * FROM reading_attempts") == attempt


@pytest.mark.parametrize("known_usage", [False, True])
def test_recovered_attempt_accepts_only_original_late_metering(core, known_usage):
    adapter, item, body = queued(core)
    core.clock.advance(31)
    core.exploration.recover()
    if not known_usage:
        body.update(status="UNKNOWN", observations=[], used_requests=None, used_bytes=None,
                    observed_requests=1, observed_bytes=1234, error_code="UPSTREAM_TIMEOUT")
    receipt = ReadObservation.model_validate(body)
    with pytest.raises(CoreError, match="STALE_READER_LEASE"):
        core.reading.complete(adapter, item["request_id"],
                              receipt.model_copy(update={"lease_token": "foreign-lease"}))
    result = core.reading.complete(adapter, item["request_id"], receipt)
    assert result["state"] == ("CANCELLED" if known_usage else "UNKNOWN")
    assert result["event_ids"] == []
    assert not core.store.rows("SELECT * FROM observations")
    parent = core.store.one("SELECT * FROM exploration_requests")
    assert parent["used_requests"] == (1 if known_usage else None)
    assert parent["used_bytes"] == (200 if known_usage else None)
    assert parent["reserved_requests"] > 0 and parent["reserved_bytes"] > 0
    attempt = core.store.one("SELECT * FROM reading_attempts")
    assert attempt["observed_bytes"] == receipt.observed_bytes
    assert core.reading.complete(adapter, item["request_id"], receipt)["duplicate"]
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        core.reading.complete(adapter, item["request_id"],
                              receipt.model_copy(update={"error_code": "CHANGED"}))


def test_parent_and_attempt_timeout_are_one_transaction(core, monkeypatch):
    queued(core, owner=False)
    core.clock.advance(31)
    execute = core.store.execute

    def fail_attempt(sql, *args):
        if sql.startswith("UPDATE reading_attempts"):
            raise RuntimeError("synthetic interrupted recovery")
        return execute(sql, *args)

    monkeypatch.setattr(core.store, "execute", fail_attempt)
    with pytest.raises(RuntimeError, match="interrupted recovery"):
        core.exploration.recover()
    assert core.store.one("SELECT state FROM exploration_requests")["state"] == "RUNNING"
    assert core.store.one("SELECT state FROM reading_attempts")["state"] == "RUNNING"
