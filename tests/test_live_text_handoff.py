"""Stopped-window succession with synthetic durable state and no model/network I/O."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from companion import live_text, ops_status, paid_budget
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import Store, encode
from tests.conftest import event, run
from tests.support.live_text_handoff import (
    bridge_receipt,
    configure,
    initial,
    journal_bytes,
    open_successor,
    reference,
    stop_and_successor,
)
from tests.support.onebot_delivery import core_client


def test_http_zero_call_handoff_needs_signed_bridge_commit_then_separate_open(core, tmp_path):
    old = initial(core, tmp_path)
    # Expired predecessor is still explicitly retireable.
    core.clock.advance(601)
    _, new = stop_and_successor(core, tmp_path)
    journal_id = new.budget_journal_id
    ledger = Path(core.settings.erasure_ledger).read_bytes()

    async def scenario():
        async with core_client(core) as client:
            invalid = await client.post(
                "/internal/v1/live-text/handoff",
                json={
                    "window_sha256": new.plan_sha256,
                    "handoff": {"arbitrary": True},
                },
            )
            assert invalid.status_code == 400
            response = await client.post(
                "/internal/v1/live-text/handoff", json=reference(new).model_dump()
            )
            assert response.status_code == 200, response.text
            prepared = response.json()
            assert set(prepared) == {
                "state",
                "window_sha256",
                "predecessor_window_sha256",
                "handoff_sha256",
                "budget_journal_head_sha256",
                "privacy_epoch",
                "erasure_ledger_sha256",
            }
            assert prepared["state"] == "PREPARED"
            assert not live_text.status(core.store, core.settings, core.clock)["approved"]
            assert (
                await client.post(
                    "/internal/v1/live-text/handoff", json=reference(new).model_dump()
                )
            ).json() == prepared
            assert (
                await client.post("/internal/v1/live-text/open", json=reference(new).model_dump())
            ).status_code == 403
            bad = bridge_receipt(new, prepared)
            bad["signature"] = "0" * 128
            assert (
                await client.post(
                    "/internal/v1/live-text/open",
                    json={
                        "window_sha256": new.plan_sha256,
                        "handoff_receipt": bad,
                    },
                )
            ).status_code == 403
            opened = await client.post(
                "/internal/v1/live-text/open",
                json={
                    "window_sha256": new.plan_sha256,
                    "handoff_receipt": bridge_receipt(new, prepared),
                },
            )
            assert opened.status_code == 200 and opened.json()["approved"]
            before = journal_bytes(core)
            assert (
                await client.post("/internal/v1/live-text/stop", json=reference(old).model_dump())
            ).json() == {"stopped": True}
            assert journal_bytes(core) == before

    run(scenario())
    assert live_text._meta(core.store, live_text.HISTORY + old.plan_sha256)["state"] == "RETIRED"
    assert (
        core.store.one("SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'")[
            "value"
        ]
        == journal_id
    )
    assert Path(core.settings.erasure_ledger).read_bytes() == ledger and epoch(core.store) == 0
    assert core.store.rows("SELECT * FROM model_usage") == [] and core.model.calls == []
    snapshot = ops_status.snapshot(core.settings, integrity=True)
    assert "LIVE_TEXT_STOPPED" not in snapshot["blockers"]
    assert "BUDGET_JOURNAL_RECONCILIATION_REQUIRED" not in snapshot["blockers"]


@pytest.mark.parametrize("stage", ["handoff", "open"])
def test_fsync_then_db_failure_never_reopens_or_replenishes(core, tmp_path, monkeypatch, stage):
    initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    prepared = (
        None
        if stage == "handoff"
        else live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    )
    before = list(core.store.db.iterdump())
    original = live_text._put

    def fail_after_fsync(store, key, value):
        if (stage == "handoff" and key.startswith(live_text.HISTORY)) or (
            stage == "open" and key == live_text.BINDING
        ):
            raise RuntimeError("synthetic crash after durable journal")
        return original(store, key, value)

    monkeypatch.setattr(live_text, "_put", fail_after_fsync)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        if stage == "handoff":
            live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
        else:
            open_successor(core, new, prepared)
    assert list(core.store.db.iterdump()) == before
    assert not live_text.status(core.store, core.settings, core.clock)["approved"]
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    assert core.model.calls == []


def test_zero_call_old_database_and_missing_lifecycle_are_rejected(core, tmp_path):
    initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    with closing(sqlite3.connect(tmp_path / "synthetic-old.db")) as old_db:
        core.store.db.backup(old_db)
        prepared = live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
        assert open_successor(core, new, prepared)["approved"]
        old_db.backup(core.store.db)
    assert (
        live_text.status(core.store, core.settings, core.clock)["code"]
        == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
    )
    assert core.model.calls == []


def test_existing_legacy_zero_input_stop_is_archived_without_replacing_any_journal(core, tmp_path):
    old = configure(core, tmp_path)
    binding = {
        "approval_sha256": paid_budget.sha(old.model_dump(mode="json", exclude={"handoff"})),
        "privacy_epoch": epoch(core.store),
        "plan_sha256": old.plan_sha256,
        "window_id": old.window_id,
        "budget_journal_id": old.budget_journal_id,
        "budget_journal_path": str(Path(core.settings.paid_model.journal_path).resolve()),
    }
    live_text._put(core.store, live_text.BINDING, binding)
    core.store.execute("INSERT INTO runtime_meta VALUES ('live_text_stopped','OWNER_STOP')")
    journal = Path(core.settings.paid_model.journal_path)
    header = json.loads(journal.read_text().splitlines()[0])
    record = {
        "kind": "window_stop",
        "window_id": old.window_id,
        "plan_sha256": old.plan_sha256,
        "previous": header["sha256"],
    }
    record["sha256"] = paid_budget.sha(record)
    # Exact original persisted format: no lifecycle metadata and no window_open record.
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(encode(record) + "\n")
    original = journal.read_bytes()
    assert paid_budget.live_lifecycle(core.store) is None
    new = configure(core, tmp_path, successor=True)
    prepared = live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    assert open_successor(core, new, prepared)["approved"]
    assert journal.read_bytes().startswith(original)
    assert live_text._meta(core.store, live_text.HISTORY + old.plan_sha256)["binding"] == binding
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='live_text_stopped'") == {
        "value": "OWNER_STOP"
    }
    core.settings.live_text_window = old
    assert not live_text.status(core.store, core.settings, core.clock)["approved"]
    assert core.model.calls == []


@pytest.mark.parametrize("case", ["privacy_epoch", "ledger", "binding", "new_failure"])
def test_change_after_prepare_or_successor_failure_never_opens(core, tmp_path, case):
    initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    prepared = live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    if case == "privacy_epoch":
        core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('privacy_epoch','1')")
    elif case == "ledger":
        Path(core.settings.erasure_ledger).write_text(
            encode({"ledger_id": "ledger_changed"}) + "\n"
        )
    elif case == "binding":
        binding = live_text._meta(core.store, live_text.BINDING)
        live_text._put(core.store, live_text.BINDING, {**binding, "window_id": "win_" + "9" * 32})
    else:
        assert open_successor(core, new, prepared)["approved"]
        live_text.record_failure(core.store, core.settings, "SYNTHETIC_NEW_WINDOW_FAILED")
    original = journal_bytes(core)
    with pytest.raises(CoreError):
        open_successor(core, new, prepared)
    assert journal_bytes(core) == original
    assert not live_text.status(core.store, core.settings, core.clock)["approved"]
    assert core.model.calls == []


@pytest.mark.parametrize(
    "case",
    [
        "UNKNOWN",
        "RESERVED",
        "pending_input",
        "ledger_identity",
        "epoch",
        "budget_head",
        "owner",
        "old_capture_epoch",
    ],
)
def test_handoff_refuses_unresolved_or_changed_custody_without_writing_journal(
    core, tmp_path, case
):
    old = initial(core, tmp_path)
    _, new = stop_and_successor(core, tmp_path)
    if case in {"UNKNOWN", "RESERVED"}:
        core.store.insert(
            "model_usage",
            usage_id="synthetic_usage",
            request_id="synthetic_request",
            scenario="chat",
            provider="synthetic",
            model="synthetic",
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            billed_micro_usd=123,
            is_estimate=1,
            pricing_known=1,
            state=case,
            occurred_at=core.clock(),
        )
    elif case in {"pending_input", "old_capture_epoch"}:
        value = {
            "window_sha256": old.plan_sha256,
            "event_id": "evt_" + "a" * 64,
            "capture": {
                "identity_sha256": new.handoff.identity_sha256,
                "protocol_epoch": "WRONG_EPOCH",
            },
        }
        live_text._put(core.store, live_text.INPUT + value["event_id"], value)
    elif case == "ledger_identity":
        Path(core.settings.erasure_ledger).write_text(encode({"ledger_id": "ledger_wrong"}) + "\n")
    elif case == "epoch":
        core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('privacy_epoch','1')")
    elif case == "budget_head":
        core.settings.live_text_window = new.model_copy(
            update={
                "handoff": new.handoff.model_copy(update={"budget_journal_head_sha256": "0" * 64}),
            }
        )
    else:
        core.store.execute("UPDATE runtime_meta SET value='10009' WHERE key='owner_qq_id'")
    before = list(core.store.db.iterdump())
    journal = Path(core.settings.paid_model.journal_path)
    original = journal.read_bytes()
    with pytest.raises(CoreError):
        live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    assert list(core.store.db.iterdump()) == before and journal.read_bytes() == original
    assert core.model.calls == []


def test_old_approved_inputs_do_not_consume_or_authorize_successor_inputs(core, tmp_path):
    old = initial(core, tmp_path)
    prior = []
    for number in range(5):
        inbound = event(core, f"old-input-{number}", text="synthetic old input").model_copy(
            update={"event_id": "evt_" + hashlib.sha256(f"old-{number}".encode()).hexdigest()}
        )
        live_text.approve_input(
            core.store,
            core.settings,
            core.clock,
            live_text.InputApproval(
                window_sha256=old.plan_sha256,
                event_id=inbound.event_id,
                input_sha256=paid_budget.sha(inbound.payload.text),
                evidence_sha256="e" * 64,
            ),
        )
        # Frozen pre-cutover event/job rows: retain the old approval and custody
        # without invoking the retired live inbox under current native authority.
        if number == 0:
            core.store.insert("conversations", conversation_id="conv_archived_handoff",
                platform=inbound.source,
                platform_session_key=inbound.conversation.platform_session_key,
                scope="private", owner_actor_id="owner", created_at=core.clock())
        core.store.insert("events", event_id=inbound.event_id, schema_version=1,
            source=inbound.source, source_event_id=inbound.source_event_id,
            conversation_id="conv_archived_handoff", actor_id="owner",
            event_type=inbound.event_type, occurred_at=inbound.occurred_at.isoformat(),
            ingested_at=core.clock(), privacy_scope="private",
            body_json=encode(inbound.payload.model_dump(mode="json")), trace_id=inbound.trace_id)
        job_id = core.new_job(inbound.event_id, "chat", inbound.event_id, core.clock())
        core.store.execute("UPDATE jobs SET state='FAILED' WHERE job_id=?", (job_id,))
        prior.append(inbound)
    _, new = stop_and_successor(core, tmp_path)
    prepared = live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    assert open_successor(core, new, prepared)["approved"]
    with pytest.raises(CoreError, match="LIVE_TEXT_APPROVED_INPUT_REQUIRED"):
        live_text.check_input(core.store, core.settings, core.clock, prior[0])
    old_job = core.store.one("SELECT * FROM jobs WHERE event_id=?", (prior[0].event_id,))
    with pytest.raises(CoreError, match="LIVE_TEXT_APPROVED_JOB_REQUIRED"):
        live_text.check_job(core.store, core.settings, core.clock, old_job)
    inbound = event(core, "new-input", text="new synthetic input").model_copy(
        update={"event_id": "evt_" + "f" * 64}
    )
    assert live_text.approve_input(
        core.store,
        core.settings,
        core.clock,
        live_text.InputApproval(
            window_sha256=new.plan_sha256,
            event_id=inbound.event_id,
            input_sha256=paid_budget.sha(inbound.payload.text),
            evidence_sha256="e" * 64,
        ),
    ) == {"approved": True}
    assert core.model.calls == []


@pytest.mark.parametrize("phase", ["OPEN", "PREPARED"])
def test_zero_call_restore_cannot_keep_authority_at_the_same_logical_config_path(
    core,
    tmp_path,
    phase,
):
    approval = initial(core, tmp_path)
    prepared = None
    if phase == "PREPARED":
        _, approval = stop_and_successor(core, tmp_path)
        prepared = live_text.prepare_handoff(
            core.store, core.settings, core.clock, reference(approval)
        )
    assert core.store.rows("SELECT * FROM model_admissions") == []
    key = Fernet.generate_key()
    archive, destination = tmp_path / "synthetic.enc", tmp_path / "restored.db"
    backup(core.settings.database, archive, key)
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Store(destination)
    try:
        # A fresh volume can appear at the original container's same /data path.
        # Keep the approved logical settings and inspect the restored durable state.
        assert not live_text.status(restored, core.settings, core.clock)["approved"]
        if prepared:
            with pytest.raises(CoreError, match="LIVE_TEXT_RECONCILIATION_REQUIRED"):
                live_text.open_window(
                    restored,
                    core.settings,
                    core.clock,
                    live_text.OpenWindow(
                        window_sha256=approval.plan_sha256,
                        handoff_receipt=bridge_receipt(approval, prepared),
                    ),
                )
        assert restored.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_restore_required'")
    finally:
        restored.close()
    assert core.model.calls == []
