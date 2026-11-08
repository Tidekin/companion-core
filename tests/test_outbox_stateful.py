"""Native owner/private delivery histories; no provider or bridge I/O."""
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    multiple,
    precondition,
    rule,
    run_state_machine_as_test,
)

from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.core import Core
from companion.transport import begin_send, delivery_result, lease_deliveries
from tests.conftest import event, run


class OutboxMachine(RuleBasedStateMachine):
    tokens = Bundle("tokens")

    def __init__(self, core_factory):
        super().__init__()
        self.resources = ExitStack()
        self.core = self.resources.enter_context(core_factory())
        self.input = event(self.core)
        self.accepted = self.core.ingest(self.input, "key-1")
        run(self.core.run_once())
        self.usage = self.core.store.rows("SELECT * FROM model_usage")
        self.calls = list(self.core.model.calls)
        self.message_id = self.row()["message_id"]
        self.starts = 0
        self.no_replay = False
        self.attempts = 0
        self.sent = False

    def row(self):
        return self.core.store.one("SELECT * FROM outbox")

    @rule()
    def duplicate(self):
        result = self.core.ingest(self.input, "key-1")
        assert result["duplicate"]
        assert result["job_id"] == self.accepted["job_id"]

    @rule()
    def conflicting_duplicate(self):
        changed = self.input.model_copy(deep=True)
        changed.payload.text = "changed"
        with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
            self.core.ingest(changed, "key-1")

    @rule(target=tokens)
    def lease(self):
        items = lease_deliveries(self.core.store, self.core.settings,
                                LeaseRequest(worker_id="property"), self.core.clock())["items"]
        if self.no_replay:
            assert items == []
        assert len(items) <= 1
        return multiple(*(item["lease_token"] for item in items))

    @rule(token=tokens)
    def begin(self, token):
        before = self.row()
        valid = (before["state"] == "LEASED" and before["lease_token"] == token
                 and before["lease_until"] > self.core.clock())
        if valid:
            assert begin_send(self.core.store, self.message_id, token,
                              self.core.clock()) == {"state": "SENDING"}
            self.starts += 1
            self.no_replay = True
        else:
            with pytest.raises(CoreError, match="STALE_DELIVERY_LEASE"):
                begin_send(self.core.store, self.message_id, token, self.core.clock())
            assert self.row() == before

    @rule(token=tokens, result=st.sampled_from(
        ["SENT", "RETRYABLE_FAILURE", "DELIVERY_UNCERTAIN"]))
    def report(self, token, result):
        before = self.row()
        valid = (before["lease_token"] == token and (
            before["state"] in {"LEASED", "SENDING", "DELIVERY_UNCERTAIN"}
            or before["state"] == result == "SENT")
            and not (before["state"] == "LEASED" and result == "SENT"))
        request = DeliveryResult(lease_token=token, result=result)
        if not valid:
            with pytest.raises(CoreError):
                delivery_result(self.core.store, self.core.settings, self.message_id,
                                request, self.core.clock())
            assert self.row() == before
            return
        delivery_result(self.core.store, self.core.settings, self.message_id,
                        request, self.core.clock())
        if result == "SENT":
            assert self.row()["state"] == "SENT"
        if result == "RETRYABLE_FAILURE" and before["state"] != "LEASED":
            assert self.row()["state"] == "DELIVERY_UNCERTAIN"
        self.no_replay |= self.row()["state"] in {"SENT", "DELIVERY_UNCERTAIN"}

    @precondition(lambda self: self.row()["state"] == "DELIVERY_UNCERTAIN")
    @rule()
    def late_ack(self):
        self.report(self.row()["lease_token"], "SENT")

    @rule(seconds=st.sampled_from([0, 1, 5, 121]))
    def advance(self, seconds):
        self.core.clock.advance(seconds)

    @precondition(lambda self: self.row()["lease_until"] is not None)
    @rule(offset=st.sampled_from([-1, 0, 1]))
    def lease_boundary(self, offset):
        delta = (datetime.fromisoformat(self.row()["lease_until"])
                 - datetime.fromisoformat(self.core.clock())).total_seconds() + offset
        if delta >= 0:
            self.core.clock.advance(delta)

    @rule()
    def reopen(self):
        before = self.row()
        previous = self.core
        previous.store.close()
        self.core = self.resources.enter_context(
            Core(previous.settings, previous.model, previous.clock))
        assert self.row() == before

    @invariant()
    def accounting_and_delivery(self):
        assert self.core.store.rows("SELECT * FROM model_usage") == self.usage
        assert self.core.model.calls == self.calls
        for table in ("events", "jobs", "outbox"):
            assert self.core.store.one(f"SELECT count(*) AS n FROM {table}")["n"] == 1
        row = self.row()
        assert row["attempt_count"] >= self.attempts
        self.attempts = row["attempt_count"]
        assert self.starts <= 1
        if self.sent:
            assert row["state"] == "SENT"
        self.sent |= row["state"] == "SENT"

    def teardown(self):
        self.resources.close()
        super().teardown()


def test_outbox_histories(core_factory):
    run_state_machine_as_test(lambda: OutboxMachine(core_factory), settings=settings(
        max_examples=25, stateful_step_count=20, deadline=None,
        database=DirectoryBasedExampleDatabase(Path(".local/hypothesis/outbox")),
    ))


def test_expiry_stale_token_and_late_ack(core_factory):
    machine = OutboxMachine(core_factory)
    try:
        first = machine.lease().values[0]
        machine.lease_boundary(0)
        machine.begin(first)  # Expiry rejects I/O even before recovery.
        second = machine.lease().values[0]
        assert second != first
        machine.begin(first)
        machine.report(first, "DELIVERY_UNCERTAIN")
        machine.begin(second)
        machine.lease_boundary(0)
        machine.lease()
        assert machine.row()["state"] == "DELIVERY_UNCERTAIN"
        machine.reopen()
        machine.late_ack()
        machine.report(second, "SENT")
        machine.lease()
        machine.accounting_and_delivery()
    finally:
        machine.teardown()


def test_scenario_factory_isolates_state(core_factory, core, tmp_path):
    assert Path(core.settings.state_directory) == tmp_path / "state"
    with core_factory() as first, core_factory() as second:
        assert first.settings.database != second.settings.database
        assert first.settings.erasure_ledger != second.settings.erasure_ledger
        first.ingest(event(first), "key-1")
        assert second.store.rows("SELECT * FROM events") == []
