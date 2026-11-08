"""Authenticated recall before inbox admission and stopped handoff cancellation."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
import pytest
from psycopg import sql

from companion import budget, input_recall, live_text, paid_budget
from companion.backup import _client, _run
from companion.config import LiveTextHandoff
from companion.model import ModelResponse
from companion.privacy import epoch
from tests.conftest import run
from tests.postgres_fixture import logical_state
from tests.support.handoff_recall_recovery import approve_only
from tests.support.input_protocol import environment, notice
from tests.support.live_text_handoff import initial, journal_bytes, reference, stop_and_successor
from tests.support.onebot_delivery import core_client

HANDOFF = "/internal/v1/live-text/handoff"
ABORT = HANDOFF + "/abort"


async def successor(client, core, config):
    old = core.settings.live_text_window
    stopped = await client.post("/internal/v1/live-text/stop", json=reference(old).model_dump())
    assert stopped.status_code == 200, stopped.text
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={"window_id": "win_" + "2" * 32},
    )
    with core.store.transaction():
        custody = paid_budget.journal(core.store, core.settings.paid_model,
                                      allow_settlement=True, initialize=False)
    scope = LiveTextHandoff(
        predecessor_plan_sha256=old.plan_sha256, predecessor_window_id=old.window_id,
        budget_journal_head_sha256=custody["head_sha256"],
        bridge_journal_head_sha256="b" * 64,
        identity_sha256=paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id]),
        protocol_epoch=config.protocol_epoch, privacy_epoch=epoch(core.store),
        erasure_ledger_sha256=hashlib.sha256(core.ledger.path.read_bytes()).hexdigest(),
        evidence_sha256="d" * 64,
    )
    new = old.model_copy(update={
        "window_id": core.settings.paid_model.window_id, "plan_sha256": "2" * 64,
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        "core_config_sha256": live_text.config_sha256(core.settings),
        "core_code_sha256": live_text.code_sha256(), "handoff": scope,
    })
    core.settings.live_text_window = new
    return old, new


def snapshot(core):
    journal = Path(core.settings.paid_model.journal_path)
    return (logical_state(core.store), journal.read_bytes() if journal.exists() else None,
            core.ledger.path.read_bytes() if core.ledger.path.exists() else None)


def assert_no_inbox(core):
    for table in ("events", "jobs", "outbox", "model_usage", "model_admissions"):
        assert not core.store.rows("SELECT * FROM " + table)
    assert core.model.calls == []


def abort_body(approval):
    return {"window_sha256": approval.plan_sha256,
            "bridge_journal_head_sha256": "f" * 64}


def test_signed_recall_before_inbox_can_prepare_without_recreating_erased_input(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            event = await approve_only(client, config, plan, now)
            recalled = await client.post("/internal/v1/live-text/inputs/recall",
                                         json=notice(config, plan, now))
            assert recalled.status_code == 200 and recalled.json()["state"] == "RECALLED"
            marker = live_text._meta(core.store, input_recall.marker_key(event["source_event_id"]))
            assert marker["state"] == "RECALLED" and marker["event_id"] == event["event_id"]
            assert core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                  ("erased_event:" + event["event_id"],))["value"] == (
                                      marker["erasure_id"])
            assert core.store.one('SELECT state FROM privacy_erasure_ledger WHERE erasure_id=%s',
                                  (marker["erasure_id"],)) == {"state": "COMPLETE"}
            assert core.ledger.read()[-1]["target_type"] == "input_recall"
            assert_no_inbox(core)
            old, new = await successor(client, core, config)
            ledger = core.ledger.path.read_bytes()
            prepared = await client.post(HANDOFF, json=reference(new).model_dump())
            assert prepared.status_code == 200, prepared.text
            assert prepared.json()["state"] == "PREPARED"
            assert prepared.json()["predecessor_window_sha256"] == old.plan_sha256
            assert core.ledger.path.read_bytes() == ledger
            assert_no_inbox(core)
            original = snapshot(core)
            assert (await client.post(HANDOFF, json=reference(new).model_dump())).json() == (
                prepared.json())
            assert snapshot(core) == original
    run(scenario())


@pytest.mark.parametrize("case", [
    "pending", "fake_marker", "missing_marker", "wrong_event_marker", "missing_complete",
    "capture_identity", "capture_epoch", "capture_source", "nonrecall_erasure",
])
def test_missing_inbox_requires_complete_exact_independent_recall_proof(
    core, tmp_path, monkeypatch, case,
):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            if case == "pending":
                result = await client.post("/internal/v1/live-text/inputs/recall",
                                           json=notice(config, plan, now))
                assert result.status_code == 200 and result.json()["state"] == "PENDING_TARGET"
            event = await approve_only(client, config, plan, now)
            key = input_recall.marker_key(event["source_event_id"])
            if case not in {"pending", "fake_marker", "nonrecall_erasure"}:
                result = await client.post("/internal/v1/live-text/inputs/recall",
                                           json=notice(config, plan, now))
                assert result.status_code == 200 and result.json()["state"] == "RECALLED"
            if case == "fake_marker":
                source = input_recall.canonical_source(
                    bot_qq_id=config.bot_qq_id, owner_qq_id=config.owner_qq_id,
                    protocol_epoch=config.protocol_epoch, provider_message_id="1",
                )
                live_text._put(core.store, key, {**source, "state": "RECALLED",
                    "erasure_id": "erase_forged", "event_id": event["event_id"],
                    "occurred_at": now.isoformat()})
                core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)',
                                   ("erased_event:" + event["event_id"], "erase_forged"))
            elif case == "missing_marker":
                core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (key,))
            elif case == "wrong_event_marker":
                core.store.execute("UPDATE runtime_meta SET value='erase_other' WHERE key=%s",
                                   ("erased_event:" + event["event_id"],))
            elif case == "missing_complete":
                core.store.execute("DELETE FROM privacy_erasure_ledger")
            elif case.startswith("capture_"):
                receipt = live_text._meta(core.store, live_text.INPUT + event["event_id"])
                field, value = {
                    "capture_identity": ("identity_sha256", "0" * 64),
                    "capture_epoch": ("protocol_epoch", "OTHER_PROTOCOL_EPOCH"),
                    "capture_source": ("source_event_id", event["source_event_id"] + "0"),
                }[case]
                receipt["capture"][field] = value
                live_text._put(core.store, live_text.INPUT + event["event_id"], receipt)
            elif case == "nonrecall_erasure":
                with core.store.transaction(control_write=True):
                    core.ledger.expire_events(core.store, [event["event_id"]], core.clock())
                erased = core.ledger.read()[-1]
                source = input_recall.canonical_source(
                    bot_qq_id=config.bot_qq_id, owner_qq_id=config.owner_qq_id,
                    protocol_epoch=config.protocol_epoch, provider_message_id="1",
                )
                # Even COMPLETE erasure plus matching projections is insufficient
                # when the independent record is not an authenticated input recall.
                live_text._put(core.store, key, {**source, "state": "RECALLED",
                    "erasure_id": erased["erasure_id"], "event_id": event["event_id"],
                    "occurred_at": now.isoformat()})
            _, new = await successor(client, core, config)
            before = snapshot(core)
            result = await client.post(HANDOFF, json=reference(new).model_dump())
            assert result.status_code in {403, 409, 503}, result.text
            assert snapshot(core) == before
            assert_no_inbox(core)
    run(scenario())


def test_abort_is_authenticated_cancel_only_with_one_independent_stop_fence(core, tmp_path):
    old = initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    # Cancellation remains possible after deploy changes the approved code hash.
    core.settings.live_text_window = new.model_copy(update={"core_code_sha256": "0" * 64})
    new = core.settings.live_text_window

    async def scenario():
        async with core_client(core) as client:
            original = snapshot(core)
            denied = await client.post(ABORT, json=abort_body(new),
                                       headers={"Authorization": "Bearer wrong"})
            assert denied.status_code in {401, 403}
            assert snapshot(core) == original
            response = await client.post(ABORT, json=abort_body(new))
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert set(receipt) == {
                "state", "window_sha256", "predecessor_window_sha256", "handoff_sha256",
                "predecessor_budget_journal_head_sha256", "budget_journal_head_sha256",
                "privacy_epoch", "erasure_ledger_sha256", "bridge_journal_head_sha256",
            }
            assert receipt == {
                "state": "ABORTED_BEFORE_PREPARE", "window_sha256": new.plan_sha256,
                "predecessor_window_sha256": old.plan_sha256,
                "handoff_sha256": paid_budget.sha(new.handoff.model_dump(mode="json")),
                "predecessor_budget_journal_head_sha256": new.handoff.budget_journal_head_sha256,
                "budget_journal_head_sha256": json.loads(journal_bytes(core).splitlines()[-1])[
                    "sha256"],
                "privacy_epoch": epoch(core.store),
                "erasure_ledger_sha256": new.handoff.erasure_ledger_sha256,
                "bridge_journal_head_sha256": "f" * 64,
            }
            lines = journal_bytes(core).splitlines()
            previous = original[1].splitlines()
            assert lines[:-1] == previous
            assert json.loads(lines[-1])["kind"] == "window_stop"
            assert json.loads(lines[-1])["window_id"] == old.window_id
            assert receipt["budget_journal_head_sha256"] != new.handoff.budget_journal_head_sha256
            assert live_text._meta(core.store, live_text.HISTORY + new.plan_sha256)["state"] == (
                "ABORTED")
            assert live_text._meta(core.store, live_text.BINDING)["plan_sha256"] == old.plan_sha256
            assert live_text.stopped(core.store, old.plan_sha256)
            assert not core.store.one('SELECT 1 FROM model_budget_windows WHERE window_id=%s',
                                      (new.window_id,))
            assert_no_inbox(core)
            after = snapshot(core)
            repeated = await client.post(ABORT, json=abort_body(new))
            assert repeated.status_code == 200 and repeated.json() == receipt
            assert snapshot(core) == after
            conflict = await client.post(ABORT, json={**abort_body(new),
                "bridge_journal_head_sha256": "e" * 64})
            assert conflict.status_code == 409, conflict.text
            assert snapshot(core) == after
            prepared = await client.post(HANDOFF, json=reference(new).model_dump())
            assert prepared.status_code == 409, prepared.text
            assert prepared.json()["error"] != "LIVE_TEXT_RUNTIME_CHANGED"
            assert snapshot(core) == after
    run(scenario())


@pytest.mark.parametrize("case", [
    "no_stop", "no_handoff", "wrong_window", "missing_binding", "wrong_owner",
    "wrong_epoch", "wrong_budget_head", "missing_journal", "changed_ledger", "prepared",
    "new_input", "used_window", "pending_usage",
])
def test_abort_rejects_missing_changed_or_used_custody_without_writes(core, tmp_path, case):
    old = initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    if case == "no_stop":
        core.store.execute('DELETE FROM runtime_meta WHERE key IN (%s,%s)',
                           (live_text.STOP + old.plan_sha256, "live_text_stopped"))
    elif case == "no_handoff":
        core.settings.live_text_window = new.model_copy(update={"handoff": None})
    elif case == "missing_binding":
        core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (live_text.BINDING,))
    elif case == "wrong_owner":
        core.store.execute("UPDATE runtime_meta SET value='10009' WHERE key='owner_qq_id'")
    elif case == "wrong_epoch":
        core.store.execute(
            'INSERT INTO runtime_meta (key,value) VALUES '
            "('privacy_epoch','1') ON CONFLICT(key) DO UPDATE SET "
            'value=excluded.value,record_sequence=DEFAULT'
        )
    elif case == "wrong_budget_head":
        core.settings.live_text_window = new.model_copy(update={
            "handoff": new.handoff.model_copy(update={"budget_journal_head_sha256": "0" * 64}),
        })
    elif case == "missing_journal":
        Path(core.settings.paid_model.journal_path).unlink()
    elif case == "changed_ledger":
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [], core.clock())
    elif case == "prepared":
        live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    elif case == "new_input":
        event_id = "evt_" + "a" * 64
        value = live_text.InputApproval(window_sha256=new.plan_sha256, event_id=event_id,
            input_sha256="b" * 64, evidence_sha256="c" * 64).model_dump(mode="json")
        live_text._put(core.store, live_text.INPUT + event_id, {**value, "privacy_epoch": 0})
    elif case == "used_window":
        core.store.insert("model_budget_windows", window_id=new.window_id,
                          contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)))
    elif case == "pending_usage":
        core.store.insert("model_usage", usage_id="usage_unresolved", request_id="model_unresolved",
            scenario="chat", provider="synthetic", model="synthetic", input_tokens=0,
            output_tokens=0, billed_micro_usd=123, is_estimate=1, pricing_known=1,
            state="UNKNOWN", occurred_at=core.clock())

    async def scenario():
        async with core_client(core) as client:
            before = snapshot(core)
            body = abort_body(new)
            if case == "wrong_window":
                body["window_sha256"] = "9" * 64
            result = await client.post(ABORT, json=body)
            assert result.status_code in {403, 409, 503}, result.text
            assert snapshot(core) == before
    run(scenario())


def test_abort_fsync_survives_failed_database_receipt_and_cannot_be_blindly_retried(core, tmp_path):
    initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    core.store.execute(
        sql.SQL("ALTER TABLE runtime_meta ADD CONSTRAINT reject_abort_receipt "
                "CHECK (key <> {}) NOT VALID").format(
                    sql.Literal("live_text_window:" + new.plan_sha256))
    )

    async def scenario():
        async with core_client(core) as client:
            before = snapshot(core)
            with pytest.raises(psycopg.errors.CheckViolation, match="reject_abort_receipt"):
                await client.post(ABORT, json=abort_body(new))
            assert logical_state(core.store) == before[0]
            assert core.ledger.path.read_bytes() == before[2]
            assert journal_bytes(core).splitlines()[:-1] == before[1].splitlines()
            assert json.loads(journal_bytes(core).splitlines()[-1])["kind"] == "window_stop"
            core.store.execute("ALTER TABLE runtime_meta DROP CONSTRAINT reject_abort_receipt")
            fenced = snapshot(core)
            for endpoint, body in (
                (ABORT, abort_body(new)), (HANDOFF, reference(new).model_dump()),
            ):
                result = await client.post(endpoint, json=body)
                assert result.status_code == 503, result.text
                assert result.json()["error"] == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
                assert snapshot(core) == fenced
            assert_no_inbox(core)
    run(scenario())


def test_abort_refuses_a_successor_with_real_settled_model_admission(core, tmp_path):
    from companion import owner_authority
    from companion.owner_authority_protocol import (
        AuthorityStop,
        OwnerGrantPolicy,
        OwnerRuntimeConfig,
    )
    from tests.support.owner_authority import activate

    initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    activated, _ = activate(core, OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=["memory"], memory_scope="owner_private",
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings))))
    request = budget.reserve(core.store, core.settings, None, "memory_review",
                             [{"role": "user", "content": "synthetic admission"}], core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("done", 100, 20))
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-settled-handoff-evidence", authority=activated["authority"]))
    assert core.store.one("SELECT window_id FROM model_admissions")["window_id"] == new.window_id

    async def scenario():
        async with core_client(core) as client:
            before = snapshot(core)
            result = await client.post(ABORT, json=abort_body(new))
            assert result.status_code in {403, 409, 503}, result.text
            assert result.json()["error"] == "LIVE_TEXT_HANDOFF_ABORT_UNPROVEN"
            assert snapshot(core) == before
    run(scenario())
    assert core.model.calls == []


@pytest.mark.parametrize("rollback", ["database", "history_only"])
def test_abort_independent_stop_fence_survives_loss_of_database_abort_marker(
    core, tmp_path, rollback,
):
    initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)

    async def scenario():
        async with core_client(core) as client:
            previous = logical_state(core.store)
            dump = str(tmp_path / "before-abort.dump")
            if rollback == "database":
                with TemporaryDirectory(dir=tmp_path) as directory:
                    with _client(core.settings.database, directory) as (_, environment):
                        _run("pg_dump", ["--format=custom", "--no-owner", "--no-privileges",
                                         "--file", dump], environment)
            aborted = await client.post(ABORT, json=abort_body(new))
            assert aborted.status_code == 200, aborted.text
            fenced_journal = journal_bytes(core)
            if rollback == "database":
                with TemporaryDirectory(dir=tmp_path) as directory:
                    with _client(core.settings.database, directory) as (_, environment):
                        _run("pg_restore", ["--clean", "--if-exists", "--single-transaction",
                                            "--no-owner", "--no-privileges", dump], environment)
                assert logical_state(core.store) == previous
            else:
                core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                                   (live_text.HISTORY + new.plan_sha256,))
            before = snapshot(core)
            rejected = await client.post(HANDOFF, json=reference(new).model_dump())
            assert rejected.status_code in {409, 503}, rejected.text
            assert rejected.json()["error"] in {
                "MODEL_BUDGET_RECONCILIATION_REQUIRED", "LIVE_TEXT_HANDOFF_STOP_REQUIRED",
            }
            assert snapshot(core) == before and journal_bytes(core) == fenced_journal
    run(scenario())
