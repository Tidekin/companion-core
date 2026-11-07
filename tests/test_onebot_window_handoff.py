"""Offline lifecycle tests: real Spool/journals, synthetic HTTP, no platform I/O."""

import json
import sqlite3
from dataclasses import replace
from zoneinfo import ZoneInfo

import httpx
import pytest

from companion_onebot_bridge.capture import capacity
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import validate_item
from companion_onebot_bridge.spool import Spool, encode
from companion_onebot_bridge.window import (
    SendOperation,
    TestWindow,
    WindowPlan,
    bridge_code_sha256,
    digest,
    request_stop,
)
from tests.conftest import run
from tests.support.onebot import provider
from tests.support.onebot_events import normalized, raw_event
from tests.support.onebot_window import input_approval
from tests.support.onebot_window_handoff import CoreContract, plan_for, source


def test_zero_input_handoff_is_closed_until_separate_open_and_preserves_evidence(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old = await source(spool, plan, now, http)
                old_bytes = plan.journal.read_bytes()
                identity = spool.db.execute("SELECT value FROM identity").fetchone()[0]
                _, next_plan, _ = plan_for(tmp_path, 2, predecessor=old)
                contract.successor = next_plan
                successor = TestWindow(spool, next_plan, clock=lambda: now)
                assert (await successor.handoff(http))["state"] == "PREPARED"
                assert plan.journal.read_bytes().startswith(old_bytes)
                assert spool.db.execute("SELECT COUNT(*) FROM window_history").fetchone()[0] == 1
                assert spool.db.execute("SELECT value FROM identity").fetchone()[0] == identity
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    successor.verify()
                assert (await successor.open(http))["state"] == "OPEN"
                with pytest.raises(ValueError, match="WINDOW_IMMUTABLE_OR_MISSING"):
                    old.verify(active=False)
                assert (spool.directory / "window.stop").read_bytes() == b"OWNER_STOP\n"
                assert not successor.stop_path.exists()
                # Stop still reaches the process holding the Spool OS lock, and
                # targets only the current plan; the legacy stop remains intact.
                request_stop(spool.directory)
                assert successor.stop_path.read_bytes() == b"OWNER_STOP\n"
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    capacity(successor)

    run(scenario())


def test_relative_spool_directory_can_receive_emergency_stop(tmp_path, monkeypatch):
    cfg, plan, now = plan_for(tmp_path)
    cfg = replace(cfg, data_dir="spool")
    value = json.loads(encode(plan.value))
    value["config_sha256"] = digest(cfg.snapshot())
    inventory = {
        key: value[key]
        for key in (
            "identity_sha256",
            "protocol_epoch",
            "config_sha256",
            "config_version",
            "image_ids",
        )
    }
    value["runtime_attestation_sha256"] = digest(inventory)
    plan = WindowPlan(value, cfg, inventory)
    contract = CoreContract(plan)
    monkeypatch.chdir(tmp_path)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                window = TestWindow(spool, plan, clock=lambda: now)
                await window.open(http)
                request_stop("spool")
                assert window.stop_path.read_bytes() == b"OWNER_STOP\n"
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    window.verify()

    run(scenario())


def test_completed_rounds_keep_global_dedup_but_successor_caps_start_at_zero(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old = TestWindow(spool, plan, clock=lambda: now)
                await old.open(http)
                for number in (1, 2):
                    raw = raw_event(number)
                    event = normalized(raw, cfg)
                    await old.ingest(http, raw, input_approval(plan, raw))
                    item = {
                        "message_id": f"msg_{number:032x}",
                        "action_type": "qq.private.reply",
                        "conversation_platform_session_key": cfg.origin,
                        "lease_token": f"lease_{number:032x}",
                        "delivery_key": f"reply:{event['event_id']}:v1",
                        "content": {"text": "synthetic reply"},
                        "lease_until": plan.end.isoformat(),
                    }
                    validate_item(item, cfg)
                    operation = SendOperation(
                        old,
                        http,
                        {
                            "window_sha256": plan.sha256,
                            "event_id": event["event_id"],
                            "message_id": item["message_id"],
                            "reply_sha256": digest("synthetic reply"),
                            "evidence_sha256": "d" * 64,
                        },
                    )
                    operation.reserve(item)
                    operation.complete(item, str(number))
                    contract.sent += 1
                await old.stop_live(http)
                _, successor_plan, _ = plan_for(tmp_path, 2, predecessor=old)
                contract.successor = successor_plan
                new = TestWindow(spool, successor_plan, clock=lambda: now)
                await new.handoff(http)
                status = await new.open(http)
                assert status["new_inputs"] == status["attempts_reserved"] == 0
                capacity(new)
                raw = raw_event(3)
                await new.ingest(http, raw, input_approval(successor_plan, raw))
                assert new.status()["new_inputs"] == 1
                assert old.count("window_inputs") == old.count("window_attempts") == 2
                assert spool.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 3
                # Calling the underlying ingest avoids its deliberate stop-on-error
                # wrapper, so the dedup assertion does not obscure quota inspection.
                with pytest.raises(ValueError, match="HISTORICAL_INPUT_REFUSED"):
                    await new._ingest(
                        http, raw_event(1), input_approval(successor_plan, raw_event(1))
                    )
                assert new.status()["new_inputs"] == 1

    run(scenario())


@pytest.mark.parametrize("blocker", ["pending", "unapproved", "unknown", "capture", "no_core_ack"])
def test_unresolved_source_never_crosses_core_handoff(tmp_path, blocker):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old = TestWindow(spool, plan, clock=lambda: now)
                await old.open(http)
                if blocker == "capture":
                    head = old.append("CAPTURE_STARTED", {"fence": 1})
                    with spool.db:
                        spool.db.execute("UPDATE test_window SET journal_head=?", (head,))
                old.stop()
                if blocker != "no_core_ack":
                    await old.stop_core(http)
                if blocker == "pending":
                    spool.put(normalized(raw_event(), cfg))
                elif blocker == "unapproved":
                    with spool.db:
                        spool.db.execute(
                            "INSERT INTO window_inputs VALUES('event','h','','CAPTURED',NULL,?)",
                            (plan.sha256,),
                        )
                elif blocker == "unknown":
                    with spool.db:
                        spool.db.execute(
                            "INSERT INTO window_attempts VALUES('msg','event',1,"
                            "'UNKNOWN','h','a',NULL,'i',?)",
                            (plan.sha256,),
                        )
                _, successor_plan, _ = plan_for(tmp_path, 2, predecessor=old)
                contract.successor = successor_plan
                before = plan.journal.read_bytes()
                contract.calls.clear()
                with pytest.raises(ValueError):
                    await TestWindow(spool, successor_plan, clock=lambda: now).handoff(http)
                assert not contract.calls and plan.journal.read_bytes() == before
                assert old.row()["state"] == "STOPPED"

    run(scenario())


@pytest.mark.parametrize(
    "failure", ["lost_handoff", "lost_open", "retirement_commit", "new_commit"]
)
def test_ambiguous_http_and_fsync_before_commit_remain_closed(tmp_path, monkeypatch, failure):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old = await source(spool, plan, now, http)
                _, successor_plan, _ = plan_for(tmp_path, 2, predecessor=old)
                contract.successor = successor_plan
                new = TestWindow(spool, successor_plan, clock=lambda: now)
                original = TestWindow.append

                def append(window, kind, data, **options):
                    result = original(window, kind, data, **options)
                    if (failure == "retirement_commit" and kind == "RETIRED") or (
                        failure == "new_commit" and kind == "HANDOFF_PREPARED"
                    ):
                        raise OSError("synthetic crash after fsync")
                    return result

                monkeypatch.setattr(TestWindow, "append", append)
                contract.failure = failure
                with pytest.raises((ValueError, OSError, httpx.ReadTimeout)):
                    await new.handoff(http)
                    await new.open(http)
                current = spool.db.execute("SELECT state FROM test_window").fetchone()[0]
                assert current in {"HANDOFF_UNCONFIRMED", "OPEN_UNCONFIRMED"}
                calls = len(contract.calls)
                with pytest.raises(ValueError):
                    await new.open(http)
                assert len(contract.calls) == calls

    run(scenario())


def test_old_spool_with_latest_journal_rejects_even_zero_inputs(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old = await source(spool, plan, now, http)
                with sqlite3.connect(tmp_path / "old.sqlite3") as backup:
                    spool.db.backup(backup)
                _, successor_plan, _ = plan_for(tmp_path, 2, predecessor=old)
                contract.successor = successor_plan
                await TestWindow(spool, successor_plan, clock=lambda: now).handoff(http)
                with sqlite3.connect(tmp_path / "old.sqlite3") as backup:
                    backup.backup(spool.db)
                with pytest.raises(ValueError, match="JOURNAL_RECONCILIATION"):
                    old.verify(active=False)
                with pytest.raises(ValueError):
                    await TestWindow(spool, successor_plan, clock=lambda: now).handoff(http)

    run(scenario())


def test_not_ready_protocol_refuses_before_core_open_or_any_window_write(tmp_path):
    cfg, plan, now = plan_for(tmp_path, approved=True)
    contract = CoreContract(plan)
    actions = []

    def protocol(request):
        actions.append(request.url.path)
        response = provider(request, [])
        if request.url.path == "/get_status":
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"online": False, "good": True}}
            )
        return response

    async def scenario():
        with Spool(cfg) as spool:
            async with (
                contract.client() as http,
                private_http_client(
                    cfg.onebot_http_url,
                    cfg.onebot_token,
                    transport=httpx.MockTransport(protocol),
                ) as onebot,
            ):
                with pytest.raises(ValueError, match="ONEBOT_NOT_READY"):
                    await TestWindow(spool, plan, clock=lambda: now).open(http, onebot)
                assert not contract.calls and not plan.journal.exists()
                assert not spool.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='test_window'"
                ).fetchone()

    run(scenario())
    assert sorted(actions) == ["/get_login_info", "/get_status", "/get_version_info"]


@pytest.mark.parametrize("corrupt", [False, "chain", "captured"])
def test_missing_window_ownership_is_refused_without_implicit_upgrade(tmp_path, corrupt):
    cfg, plan, now = plan_for(tmp_path)
    # This is the pre-change persisted format, not an old window created through
    # today's open path. Missing ownership is never guessed from the current plan.
    plan.value["live_text"].pop("handoff")
    plan.value["live_text"]["bridge_code_sha256"] = "f" * 64
    plan.sha256 = digest(plan.value)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            old = TestWindow(spool, plan, clock=lambda: now)
            head = old.append("OPEN", {"plan_sha256": plan.sha256}, create=True)
            spool.db.executescript(
                "CREATE TABLE test_window(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "plan_hash TEXT NOT NULL,plan_json TEXT NOT NULL,journal_head TEXT NOT NULL,"
                "state TEXT NOT NULL,fence INTEGER NOT NULL,initial_counts TEXT NOT NULL,"
                "initial_inbox_rowid INTEGER NOT NULL,reason TEXT);"
                "CREATE TABLE window_inputs(event_id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,"
                "approval_hash TEXT NOT NULL,state TEXT NOT NULL,capture_json TEXT);"
                "CREATE TABLE window_attempts(message_id TEXT PRIMARY KEY,event_id TEXT NOT NULL "
                "UNIQUE,fence INTEGER NOT NULL,state TEXT NOT NULL,text_hash TEXT NOT NULL,"
                "approval_hash TEXT NOT NULL,provider_ack_hash TEXT,item_hash TEXT NOT NULL);"
            )
            with spool.db:
                spool.db.execute(
                    "INSERT INTO test_window VALUES(1,?,?,?,'OPEN',0,'{}',0,NULL)",
                    (plan.sha256, encode(plan.value), head),
                )
            request_stop(spool.directory, plan=plan)
            for kind, data in (
                ("CAPTURE_STARTED", {"fence": 1}),
                ("STOP", {"reason": "CAPTURE_FAILED_CLOSED"}),
                ("CORE_STOP_CONFIRMED", {}),
            ):
                head = old.append(kind, data)
                with spool.db:
                    spool.db.execute(
                        "UPDATE test_window SET journal_head=?,state='STOPPED'", (head,)
                    )
            with spool.db:
                spool.db.execute("UPDATE test_window SET fence=1,reason='CAPTURE_FAILED_CLOSED'")
            if corrupt == "chain":
                with plan.journal.open("ab") as handle:
                    handle.write(b"{}\n")
            elif corrupt == "captured":
                old.append(
                    "CAPTURED",
                    {"event_hash": "a" * 64, "capture_sha256": "b" * 64, "frame_sha256": "c" * 64},
                )
            _, next_plan, _ = plan_for(tmp_path, 2, predecessor=old)
            contract.successor = next_plan
            before = list(spool.db.iterdump())
            journal = plan.journal.read_bytes()
            async with contract.client() as http:
                successor = TestWindow(spool, next_plan, clock=lambda: now)
                with pytest.raises(ValueError, match="WINDOW_SCHEMA_PREPARATION_REQUIRED"):
                    await successor.handoff(http)
                assert list(spool.db.iterdump()) == before
                assert plan.journal.read_bytes() == journal
                assert not contract.calls

    run(scenario())


@pytest.mark.parametrize(
    "case", ["expired", "stopped", "registered", "hook_expiry", "hook_stop", "hook_runtime"]
)
def test_open_probe_requires_current_authority_at_final_transport(tmp_path, case):
    cfg, plan, now = plan_for(tmp_path, approved=True)
    contract, probes, current = CoreContract(plan), [], [now]

    async def scenario():
        with Spool(cfg) as spool:
            if case == "expired":
                current[0] = plan.end
            elif case == "stopped":
                request_stop(spool.directory, plan=plan)
            elif case == "registered":
                plan.journal.touch(mode=0o600)

            async def after_dns(request):
                if case == "hook_expiry":
                    current[0] = plan.end
                elif case == "hook_stop":
                    request_stop(spool.directory, plan=plan)
                elif case == "hook_runtime":
                    plan.config = replace(cfg, bridge_token="changed-synthetic-bridge-token-32")

            def respond(request):
                probes.append(request.url.path)
                return provider(request, [])

            async with (
                contract.client() as http,
                private_http_client(
                    cfg.onebot_http_url, cfg.onebot_token, transport=httpx.MockTransport(respond)
                ) as protocol,
            ):
                protocol.event_hooks["request"].append(after_dns)
                with pytest.raises(ValueError):
                    await TestWindow(spool, plan, clock=lambda: current[0]).open(http, protocol)
                assert not probes and not contract.calls
                assert not spool.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='test_window'"
                ).fetchone()

    run(scenario())


@pytest.mark.parametrize("drift", ["head", "predecessor", "budget", "reused_id", "missing_stop"])
def test_handoff_scope_and_retirement_artifacts_are_not_inferred(tmp_path, drift):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)

    async def scenario():
        with Spool(cfg) as spool:
            async with contract.client() as http:
                old = await source(spool, plan, now, http)
                _, candidate, _ = plan_for(tmp_path, 2, predecessor=old)
                value = json.loads(encode(candidate.value))
                if drift == "head":
                    value["live_text"]["handoff"]["bridge_journal_head_sha256"] = "f" * 64
                elif drift == "predecessor":
                    value["live_text"]["handoff"]["predecessor_plan_sha256"] = "f" * 64
                elif drift == "budget":
                    value["live_text"]["budget_journal_id"] = "budget_" + "f" * 32
                elif drift == "reused_id":
                    value["window_id"] = value["live_text"]["window_id"] = plan.value["window_id"]
                else:
                    old.stop_path.unlink()
                inventory = {
                    key: value[key]
                    for key in (
                        "identity_sha256",
                        "protocol_epoch",
                        "config_sha256",
                        "config_version",
                        "image_ids",
                    )
                }
                successor_plan = WindowPlan(value, cfg, inventory)
                before = plan.journal.read_bytes()
                contract.calls.clear()
                with pytest.raises(ValueError):
                    await TestWindow(spool, successor_plan, clock=lambda: now).handoff(http)
                assert not contract.calls and not successor_plan.journal.exists()
                assert plan.journal.read_bytes() == before
                assert not spool.db.execute("SELECT * FROM window_history").fetchall()

    run(scenario())


def test_real_core_http_handoff_and_bridge_signed_separate_open(core, tmp_path):
    from companion.config import LiveTextWindowApproval
    from tests.support.live_text_handoff import configure
    from tests.support.live_text_window import live_fixture
    from tests.support.onebot_delivery import core_client

    cfg, plan = live_fixture(core, tmp_path, bridge_options={"capture_signing_key": "12" * 32})
    journal_id = core.settings.live_text_window.budget_journal_id
    ledger = core.settings.erasure_ledger

    async def scenario():
        with Spool(cfg) as spool:
            async with core_client(core, bridge_config=cfg) as http:
                old = await source(spool, plan, core.clock.now, http)
                approval = configure(
                    core,
                    tmp_path,
                    successor=True,
                    handoff_changes={
                        "bridge_journal_head_sha256": old.row()["journal_head"],
                        "protocol_epoch": cfg.protocol_epoch,
                    },
                )
                scope = approval.model_dump(mode="json", exclude={"plan_sha256"})
                scope.update(
                    bridge_code_sha256=bridge_code_sha256(),
                    capture_public_key=cfg.capture_public_key,
                    starts_at=approval.starts_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                    ends_at=approval.ends_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                )
                value = {
                    **plan.value,
                    "window_id": scope["window_id"],
                    "starts_at": scope["starts_at"],
                    "ends_at": scope["ends_at"],
                    "journal_path": str(tmp_path / "successor.jsonl"),
                    "live_text": scope,
                }
                inventory = {
                    key: value[key]
                    for key in (
                        "identity_sha256",
                        "protocol_epoch",
                        "config_sha256",
                        "config_version",
                        "image_ids",
                    )
                }
                next_plan = WindowPlan(value, cfg, inventory)
                core.settings.live_text_window = LiveTextWindowApproval(
                    **scope, plan_sha256=next_plan.sha256
                )
                successor = TestWindow(spool, next_plan, clock=lambda: core.clock.now)
                assert (await successor.handoff(http))["state"] == "PREPARED"
                assert core.status()["live_text_window"]["approved"] is False
                assert (await successor.open(http))["state"] == "OPEN"
                assert core.status()["live_text_window"]["approved"] is True
                assert core.settings.live_text_window.budget_journal_id == journal_id
                assert core.settings.erasure_ledger == ledger
                assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())
