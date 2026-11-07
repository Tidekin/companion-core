"""Cancel one unprepared proposal; real Spool/journals, no QQ or paid transport."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge import network
from companion_onebot_bridge.spool import Spool, encode
from companion_onebot_bridge.window import TestWindow, WindowPlan, command, digest, stored_plan
from tests.conftest import run
from tests.support.onebot_window_handoff import CoreContract, plan_for, source

ABORT = "/internal/v1/live-text/handoff/abort"


class AbortContract(CoreContract):
    def __init__(self, plan):
        super().__init__(plan)
        self.receipt = None
        self.stops = 0
        self.mutate = None
        self.lose_abort = False

    def __call__(self, request):
        if request.url.path != ABORT:
            return super().__call__(request)
        body = json.loads(request.content)
        self.calls.append((ABORT, body))
        scope = self.successor.value["live_text"]["handoff"]
        if self.receipt is None:
            self.stops += 1
            self.receipt = {
                "state": "ABORTED_BEFORE_PREPARE", "window_sha256": self.successor.sha256,
                "predecessor_window_sha256": self.current.sha256,
                "handoff_sha256": digest(scope),
                "predecessor_budget_journal_head_sha256": scope["budget_journal_head_sha256"],
                "budget_journal_head_sha256": "9" * 64,
                "privacy_epoch": scope["privacy_epoch"],
                "erasure_ledger_sha256": scope["erasure_ledger_sha256"],
                "bridge_journal_head_sha256": body["bridge_journal_head_sha256"],
            }
        if self.lose_abort:
            self.lose_abort = False
            raise httpx.ReadTimeout("synthetic lost abort acknowledgment")
        receipt = dict(self.receipt)
        if self.mutate:
            self.mutate(receipt)
        return httpx.Response(200, json=receipt)


async def failed_proposal(spool, plan, now, contract, http, tmp_path):
    old = await source(spool, plan, now, http)
    _, successor, _ = plan_for(tmp_path, 2, predecessor=old)
    contract.successor = successor
    contract.failure = "lost_handoff"
    new = TestWindow(spool, successor, clock=lambda: now)
    with pytest.raises(httpx.ReadTimeout):
        await new.handoff(http)
    contract.failure = None
    contract.calls.clear()
    return old, new


def test_abort_preserves_all_rows_and_original_journal_and_remains_stopped(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                before = plan.journal.read_bytes()
                rows = list(spool.db.execute("SELECT * FROM window_inputs"))
                receipt = await new.abort_handoff(http)
                assert receipt["state"] == "ABORTED_BEFORE_PREPARE"
                assert plan.journal.read_bytes().startswith(before)
                assert plan.journal.read_bytes().count(b'"HANDOFF_ABORTED"') == 1
                assert old.status()["state"] == "STOPPED"
                assert old.status()["core_stop_confirmed"]
                assert old.status()["new_inputs"] == old.status()["attempts_reserved"] == 0
                assert list(spool.db.execute("SELECT * FROM window_inputs")) == rows
                assert not spool.db.execute("SELECT 1 FROM window_history").fetchone()
                assert not new.plan.journal.exists()
                assert old.stop_path.read_bytes() == b"OWNER_STOP\n"
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    old.verify()
                calls, journal = len(contract.calls), plan.journal.read_bytes()
                assert await new.abort_handoff(http) == receipt
                assert len(contract.calls) == calls and plan.journal.read_bytes() == journal
                with pytest.raises(ValueError):
                    await new.handoff(http)
                assert len(contract.calls) == calls
                # A fresh plan starts from the advanced Bridge head; aborted
                # proposals stay in the retired predecessor's verified history.
                _, replacement, _ = plan_for(tmp_path, 3, predecessor=old)
                contract.successor = replacement
                assert (await TestWindow(spool, replacement, clock=lambda: now).handoff(http))[
                    "state"] == "PREPARED"
                assert contract.stops == 1
    run(scenario())


@pytest.mark.parametrize("field,value", [
    ("state", "PREPARED"), ("window_sha256", "0" * 64),
    ("predecessor_window_sha256", "0" * 64), ("handoff_sha256", "0" * 64),
    ("predecessor_budget_journal_head_sha256", "0" * 64),
    ("budget_journal_head_sha256", "7" * 64), ("budget_journal_head_sha256", None),
    ("privacy_epoch", False), ("privacy_epoch", 1),
    ("erasure_ledger_sha256", "0" * 64), ("bridge_journal_head_sha256", "0" * 64),
    ("extra", "unbound"), ("state", None),
])
def test_bad_or_unbound_core_receipt_never_reopens_local_window(tmp_path, field, value):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)
    contract.mutate = lambda receipt: receipt.update({field: value})

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                before = plan.journal.read_bytes(), list(spool.db.iterdump())
                with pytest.raises(ValueError, match="ABORT_CORE_UNCONFIRMED"):
                    await new.abort_handoff(http)
                assert (plan.journal.read_bytes(), list(spool.db.iterdump())) == before
                assert old.row()["state"] == "HANDOFF_UNCONFIRMED"
    run(scenario())


def test_lost_core_abort_ack_can_only_retrieve_same_receipt(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                contract.lose_abort = True
                before = plan.journal.read_bytes()
                with pytest.raises(httpx.ReadTimeout):
                    await new.abort_handoff(http)
                assert plan.journal.read_bytes() == before
                assert old.row()["state"] == "HANDOFF_UNCONFIRMED"
                assert await new.abort_handoff(http) == contract.receipt
                assert contract.stops == 1
                assert contract.calls[0] == contract.calls[1]
    run(scenario())


@pytest.mark.parametrize("case", ["wrong_plan", "pending_input", "prepared", "missing_stop",
                                   "journal_rollback", "append_after_request", "wrong_identity"])
def test_changed_or_unsettled_source_refuses_before_http(tmp_path, case):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                if case == "wrong_plan":
                    value = json.loads(encode(new.plan.value))
                    value["live_text"]["candidate_sha"] = "c" * 40
                    new.plan = stored_plan(encode(value), cfg)
                elif case == "pending_input":
                    spool.db.execute("INSERT INTO window_inputs VALUES(?,?,?,?,?,?)",
                                     ("evt_pending", "a" * 64, "b" * 64, "APPROVED", None,
                                      old.plan.sha256))
                elif case == "prepared":
                    new.plan.journal.write_text("{}\n", encoding="utf-8")
                elif case == "missing_stop":
                    old.stop_path.unlink()
                elif case == "journal_rollback":
                    lines = plan.journal.read_bytes().splitlines(keepends=True)
                    plan.journal.write_bytes(b"".join(lines[:-1]))
                elif case == "append_after_request":
                    head = old.append("STOP", {"reason": "OWNER_STOP"})
                    spool.db.execute("UPDATE test_window SET journal_head=?", (head,))
                elif case == "wrong_identity":
                    new.plan = stored_plan(encode(new.plan.value),
                                           replace(cfg, owner_qq_id="99999"))
                with pytest.raises(ValueError):
                    await new.abort_handoff(http)
                assert not contract.calls
    run(scenario())


def test_fsync_before_db_crash_requires_reconciliation_and_never_retries_http(
    tmp_path, monkeypatch,
):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                append = TestWindow.append

                def crash(window, kind, data, **options):
                    head = append(window, kind, data, **options)
                    if kind == "HANDOFF_ABORTED":
                        raise OSError("synthetic crash after durable append")
                    return head

                monkeypatch.setattr(TestWindow, "append", crash)
                with pytest.raises(OSError):
                    await new.abort_handoff(http)
                assert old.row()["state"] == "HANDOFF_UNCONFIRMED"
                calls = len(contract.calls)
                with pytest.raises(ValueError, match="JOURNAL_RECONCILIATION"):
                    await new.abort_handoff(http)
                assert len(contract.calls) == calls
    run(scenario())


def test_cli_revocation_tolerates_code_drift_and_expiry_without_inventory_or_onebot(
    tmp_path, monkeypatch,
):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)
    proposal = tmp_path / "failed-plan.json"

    async def prepare():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                _, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                proposal.write_text(encode(new.plan.value), encoding="utf-8")
                proposal.chmod(0o600)
    run(prepare())
    actual_client = network.private_http_client

    def client(url, token):
        assert url == cfg.core_url and token == cfg.bridge_token
        return actual_client(url, token, transport=httpx.MockTransport(contract))

    monkeypatch.setattr("companion_onebot_bridge.window.private_http_client", client)
    monkeypatch.setattr("companion_onebot_bridge.window.bridge_code_sha256", lambda: "f" * 64)
    # Revocation of historical custody retains its original confirmed stop even
    # after the deployment code changes and the authorization expires.
    assert now + timedelta(minutes=30) < __import__("datetime").datetime.now(now.tzinfo)
    before = plan.journal.read_bytes()
    with pytest.raises(ValueError, match="LEGACY_WINDOW_EXECUTION_RETIRED"):
        run(command(cfg, SimpleNamespace(command="window-abort-handoff", window=proposal)))
    assert not contract.calls and plan.journal.read_bytes() == before
    result = run(command(cfg, SimpleNamespace(command="window-stop")))
    assert result["state"] == "STOPPED" and result["core_stop_confirmed"]
    assert not contract.calls  # The original confirmed Core stop is idempotent.
    with Spool(cfg) as spool:
        old = TestWindow(spool, stored_plan(encode(plan.value), cfg))
        assert old.status()["state"] == "STOPPED"


def test_abort_rechecks_journal_after_request_hooks_before_transport(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)

                async def mutate(request):
                    head = old.append("STOP", {"reason": "OWNER_STOP"})
                    spool.db.execute("UPDATE test_window SET journal_head=?", (head,))

                http.event_hooks["request"].append(mutate)
                with pytest.raises(ValueError):
                    await new.abort_handoff(http)
                assert not contract.calls and contract.stops == 0
    run(scenario())


@pytest.mark.parametrize("case", ["old_database", "bad_journal_receipt"])
def test_aborted_journal_cannot_be_rolled_back_or_replaced_by_an_unbound_receipt(tmp_path, case):
    cfg, plan, now = plan_for(tmp_path)
    contract = AbortContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old, new = await failed_proposal(spool, plan, now, contract, http, tmp_path)
                with sqlite3.connect(tmp_path / "old-spool.sqlite3") as backup:
                    spool.db.backup(backup)
                await new.abort_handoff(http)
                if case == "old_database":
                    with sqlite3.connect(tmp_path / "old-spool.sqlite3") as backup:
                        backup.backup(spool.db)
                else:
                    records, _ = old.read_journal()
                    receipt = records[-1]["data"]["core_receipt"]
                    receipt["budget_journal_head_sha256"] = receipt[
                        "predecessor_budget_journal_head_sha256"]
                    plan.journal.write_text("".join(encode(r) + "\n" for r in records),
                                            encoding="utf-8")
                    spool.db.execute("UPDATE test_window SET journal_head=?",
                                     (digest(records[-1]),))
                calls = len(contract.calls)
                with pytest.raises(ValueError):
                    old.verify(active=False)
                with pytest.raises(ValueError):
                    await new.abort_handoff(http)
                assert len(contract.calls) == calls
    run(scenario())


def test_real_bridge_abort_reaches_real_core_api_and_preserves_independent_stop_fence(
    core, tmp_path,
):
    from companion import api, live_text, paid_budget
    from companion.config import LiveTextWindowApproval
    from tests.support.live_text_handoff import configure, journal_bytes
    from tests.support.onebot_capture import pinned_plan
    from tests.support.onebot_delivery import core_client

    cfg, plan = pinned_plan(core, tmp_path, 3001, live=True,
                            bridge_options={"capture_signing_key": "12" * 32})

    async def scenario():
        with Spool(cfg) as spool:
            async with core_client(core, bridge_config=cfg) as http:
                old = await source(spool, plan, core.clock.now, http)
                approval = configure(core, tmp_path, successor=True, handoff_changes={
                    "bridge_journal_head_sha256": old.row()["journal_head"],
                    "protocol_epoch": cfg.protocol_epoch,
                })
                value = json.loads(encode(plan.value))
                value.update(window_id=approval.window_id,
                             journal_path=str(tmp_path / "failed-successor.jsonl"))
                value["live_text"].update(
                    window_id=approval.window_id,
                    core_config_sha256=live_text.config_sha256(core.settings),
                    paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
                    handoff=approval.handoff.model_dump(mode="json"),
                )
                inventory = {key: value[key] for key in (
                    "identity_sha256", "protocol_epoch", "config_sha256", "config_version",
                    "image_ids",
                )}
                failed = WindowPlan(value, cfg, inventory)
                core.settings.live_text_window = LiveTextWindowApproval(
                    **value["live_text"], plan_sha256=failed.sha256,
                )

                def fail(request):
                    assert request.url.path == "/internal/v1/live-text/handoff"
                    raise httpx.ReadTimeout("synthetic handoff unavailable before prepare")

                async with httpx.AsyncClient(base_url=cfg.core_url,
                    headers={"Authorization": "Bearer " + cfg.bridge_token},
                    transport=httpx.MockTransport(fail)) as unavailable:
                    new = TestWindow(spool, failed, clock=lambda: core.clock.now)
                    with pytest.raises(httpx.ReadTimeout):
                        await new.handoff(unavailable)
                before = journal_bytes(core)
                app = api.create_app(core.settings)
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                    base_url=cfg.core_url, transport=httpx.ASGITransport(app=app),
                    headers={"Authorization": "Bearer " + cfg.bridge_token},
                ) as independent:
                    assert app.state.core is not core
                    receipt = await new.abort_handoff(independent)
                assert receipt["state"] == "ABORTED_BEFORE_PREPARE"
                assert old.status()["state"] == "STOPPED"
                assert journal_bytes(core).startswith(before)
                assert len(journal_bytes(core).splitlines()) == len(before.splitlines()) + 1
                assert receipt["budget_journal_head_sha256"] == json.loads(
                    journal_bytes(core).splitlines()[-1])["sha256"]
                assert live_text._meta(core.store, live_text.HISTORY + failed.sha256)[
                    "state"] == "ABORTED"
                assert live_text.stopped(core.store, plan.sha256)
                assert not core.store.rows("SELECT * FROM model_usage")
                assert not core.store.rows("SELECT * FROM model_admissions")
                assert core.model.calls == []
                assert not core.store.rows("SELECT * FROM events")
                assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())
