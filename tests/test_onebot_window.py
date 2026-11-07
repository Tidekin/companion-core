"""Only synthetic owners, messages and transports; never live QQ evidence."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import timedelta

import httpx
import pytest

from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow, WindowPlan, digest
from tests.conftest import run
from tests.journal_assertions import assert_private_journal
from tests.support.onebot import provider
from tests.support.onebot_delivery import core_client
from tests.support.onebot_events import normalized, raw_event
from tests.support.onebot_window import input_approval, plan_fixture, stage


def test_cli_exposes_separate_one_shot_window_commands():
    result = subprocess.run(
        [sys.executable, "-m", "companion_onebot_bridge", "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0
    assert "window-send-once" in result.stdout
    assert "window-stop" in result.stdout


def test_cli_parser_failure_never_echoes_private_arguments():
    marker = "SYNTHETIC_PRIVATE_PATH_OR_TOKEN"
    result = subprocess.run(
        [sys.executable, "-m", "companion_onebot_bridge", "--wrong-private-option", marker],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr


def test_actual_core_five_attempts_restart_duplicate_and_sixth_input_cap(core, tmp_path):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends = WindowPlan(value, cfg, inventory), []

    async def scenario():
        async with (
            core_client(core, bridge_config=cfg) as http,
            httpx.AsyncClient(
                base_url=cfg.onebot_http_url,
                headers={"Authorization": "Bearer " + cfg.onebot_token},
                transport=httpx.MockTransport(lambda r: provider(r, sends)),
            ) as protocol,
        ):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                assert (await window.open(http))["attempts_reserved"] == 0
            for number in range(1, 6):
                with Spool(cfg) as spool:
                    window = TestWindow(spool, plan, clock=lambda: now)
                    approval = await stage(window, http, core, number)
                    result = await window.send_once(http, protocol, approval)
                    assert result["attempts_reserved"] == number
                    assert result["attempt_states"] == {"ACK": number}
                    assert result["state"] == ("STOPPED" if number == 5 else "OPEN")
                    assert result["phone_receipts"] is None
                    assert result["e2e_p50_ms"] is None and result["e2e_p95_ms"] is None
                    # A repeated original event does not gain input quota or another reply.
                    if number < 5:
                        raw = raw_event(number)
                        await window.ingest(http, raw, input_approval(plan, raw))
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                raw = raw_event(6)
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    await window.ingest(http, raw, input_approval(plan, raw))
                assert window.status()["new_inputs"] == 5
                assert cfg.delivery_enabled is False
                assert window.status()["visible_replies_upper_bound"] == 5

    run(scenario())
    assert len(sends) == 5
    assert core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 5
    audit = (tmp_path / "window-audit.jsonl").read_text("utf-8")
    assert_private_journal(
        audit, ("10001", "10002", cfg.bridge_token, cfg.onebot_token, "你好", cfg.core_url),
    )


@pytest.mark.parametrize(
    "receipt",
    [
        httpx.ReadTimeout("SYNTHETIC_ACK_LOST"),
        httpx.ConnectError("SYNTHETIC_DISCONNECT"),
        {"status": "failed", "retcode": 1200, "data": None},
    ],
)
def test_unknown_counts_as_attempt_stops_and_never_retries_after_restart(core, tmp_path, receipt):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends = WindowPlan(value, cfg, inventory), []

    async def scenario():
        async with (
            core_client(core, bridge_config=cfg) as http,
            httpx.AsyncClient(
                base_url=cfg.onebot_http_url,
                headers={"Authorization": "Bearer " + cfg.onebot_token},
                transport=httpx.MockTransport(lambda r: provider(r, sends, receipt)),
            ) as protocol,
        ):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                await window.open(http)
                approval = await stage(window, http, core)
                status = await window.send_once(http, protocol, approval)
                assert status["state"] == "STOPPED"
                assert status["attempt_states"] == {"UNKNOWN": 1}
            with Spool(cfg) as spool:
                restarted = TestWindow(spool, plan, clock=lambda: now)
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    await restarted.send_once(http, protocol, approval)
                assert restarted.status()["attempts_reserved"] == 1

    run(scenario())
    assert len(sends) == 1
    assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"


@pytest.mark.parametrize("mutation", ["stranger", "bot", "group", "media", "approval", "old"])
def test_unapproved_inputs_never_reach_core(core, tmp_path, mutation):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan = WindowPlan(value, cfg, inventory)
    raw = raw_event(1)
    approval = input_approval(plan, raw)
    if mutation == "stranger":
        raw["user_id"] = 99999
    elif mutation == "bot":
        raw["self_id"] = 99999
    elif mutation == "group":
        raw["message_type"] = "group"
    elif mutation == "media":
        raw["message"].append({"type": "image", "data": {"url": "private"}})
    elif mutation == "approval":
        approval["input_sha256"] = "0" * 64
    elif mutation == "old":
        raw["time"] -= 600

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as http:
                await window.open(http)
                with pytest.raises(ValueError):
                    await window.ingest(http, raw, approval)
                assert window.status()["new_inputs"] == 0

    run(scenario())
    assert core.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 0


@pytest.mark.parametrize("mutation", ["body", "time"])
def test_same_source_id_changed_body_or_time_is_conflict(core, tmp_path, mutation):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan = WindowPlan(value, cfg, inventory)

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as http:
                await window.open(http)
                raw = raw_event(1)
                await window.ingest(http, raw, input_approval(plan, raw))
                changed = (
                    raw_event(1, text="changed")
                    if mutation == "body"
                    else raw_event(1, time=raw["time"] + 1)
                )
                with pytest.raises(ValueError, match="SOURCE_EVENT_CONFLICT"):
                    await window.ingest(http, changed, input_approval(plan, changed))
                assert window.status()["new_inputs"] == 1

    run(scenario())
    assert core.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "persistent-delivery",
        "epoch",
        "image",
        "inventory",
        "time",
        "invalid-count",
        "concurrency",
        "live-without-approval",
        "nonlocal",
    ],
)
def test_mutated_runtime_or_window_rejected_before_any_io(tmp_path, mutation):
    cfg, value, inventory, _ = plan_fixture(tmp_path)
    if mutation == "persistent-delivery":
        cfg = replace(cfg, delivery_enabled=True)
    elif mutation == "epoch":
        cfg = replace(cfg, protocol_epoch="other-epoch")
    elif mutation == "image":
        value["image_ids"] = {**value["image_ids"], "core": "latest"}
    elif mutation == "inventory":
        inventory = {**inventory, "config_version": "UNAPPROVED"}
    elif mutation == "time":
        value["ends_at"] = "2026-10-20T18:00:00"
    elif mutation == "invalid-count":
        value["max_attempts"] = 0
    elif mutation == "concurrency":
        value["concurrency"] = 2
    elif mutation == "live-without-approval":
        value["execution"] = "approved_live"
    elif mutation == "nonlocal":
        cfg = replace(cfg, core_url="http://companion-api:8765")
        value["config_sha256"] = inventory["config_sha256"] = digest(cfg.snapshot())
        value["runtime_attestation_sha256"] = digest(inventory)
    with pytest.raises(ValueError):
        WindowPlan(value, cfg, inventory)


def test_local_docker_image_ids_bind_the_existing_plan_and_inventory(tmp_path):
    cfg, value, inventory, _ = plan_fixture(tmp_path)
    plan = WindowPlan(value, cfg, inventory)
    assert plan.value["image_ids"] == inventory["image_ids"]
    assert plan.sha256 == digest(value)
    assert value["runtime_attestation_sha256"] == digest(inventory)


@pytest.mark.parametrize(
    "image_id",
    [
        "companion-core:latest",
        "companion/core@sha256:" + "a" * 64,
        "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "A" * 64,
        123,
    ],
)
def test_non_docker_image_ids_are_rejected_even_with_matching_attestation(tmp_path, image_id):
    cfg, value, inventory, _ = plan_fixture(tmp_path)
    value["image_ids"] = inventory["image_ids"] = {**value["image_ids"], "core": image_id}
    value["runtime_attestation_sha256"] = digest(inventory)
    with pytest.raises(ValueError, match="PINNED_RUNTIME_IMAGES_REQUIRED"):
        WindowPlan(value, cfg, inventory)


@pytest.mark.parametrize("mutation", ["missing", "extra", "renamed"])
def test_image_roles_must_be_exact_even_with_matching_attestation(tmp_path, mutation):
    cfg, value, inventory, _ = plan_fixture(tmp_path)
    images = dict(value["image_ids"])
    if mutation == "missing":
        images.pop("protocol")
    elif mutation == "extra":
        images["worker"] = images["core"]
    else:
        images["snowluma"] = images.pop("protocol")
    value["image_ids"] = inventory["image_ids"] = images
    value["runtime_attestation_sha256"] = digest(inventory)
    with pytest.raises(ValueError, match="PINNED_RUNTIME_IMAGES_REQUIRED"):
        WindowPlan(value, cfg, inventory)


@pytest.mark.parametrize("mutation", ["inventory-only", "changed-attestation", "stale-attestation"])
def test_image_id_changes_require_the_original_exact_runtime_attestation(tmp_path, mutation):
    cfg, value, inventory, _ = plan_fixture(tmp_path)
    images = {**inventory["image_ids"], "core": "sha256:" + "d" * 64}
    inventory = {**inventory, "image_ids": images}
    if mutation == "changed-attestation":
        value["runtime_attestation_sha256"] = digest(inventory)
    elif mutation == "stale-attestation":
        value["image_ids"] = images
    with pytest.raises(ValueError, match="RUNTIME_ATTESTATION_MISMATCH"):
        WindowPlan(value, cfg, inventory)


def test_previous_image_digest_field_is_not_a_new_window_contract(tmp_path):
    cfg, value, inventory, _ = plan_fixture(tmp_path)
    value["image_digests"] = value.pop("image_ids")
    with pytest.raises(ValueError, match="INVALID_WINDOW_PLAN"):
        WindowPlan(value, cfg, inventory)


def test_invalid_input_stops_window_before_later_valid_input(core, tmp_path):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan = WindowPlan(value, cfg, inventory)

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as http:
                await window.open(http)
                raw = raw_event(1)
                with pytest.raises(ValueError):
                    await window.ingest(http, {**raw, "user_id": 99999}, input_approval(plan, raw))
                assert window.status()["state"] == "STOPPED"
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    await window.ingest(http, raw, input_approval(plan, raw))

    run(scenario())
    assert core.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 0


@pytest.mark.parametrize("phase", ["expired-before-lease", "deadline-during-probe", "in-flight"])
def test_deadline_pre_io_and_already_in_flight_receipt(core, tmp_path, phase):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends, clock = WindowPlan(value, cfg, inventory), [], [now]

    def transport(request):
        if phase == "deadline-during-probe" and request.url.path == "/get_version_info":
            clock[0] = plan.end
        if phase == "in-flight" and request.url.path == "/send_private_msg":
            clock[0] = plan.end + timedelta(seconds=10)
        return provider(request, sends)

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: clock[0])
            async with (
                core_client(core, bridge_config=cfg) as http,
                httpx.AsyncClient(
                    base_url=cfg.onebot_http_url,
                    headers={"Authorization": "Bearer " + cfg.onebot_token},
                    transport=httpx.MockTransport(transport),
                ) as protocol,
            ):
                await window.open(http)
                approval = await stage(window, http, core)
                if phase == "expired-before-lease":
                    clock[0] = plan.end
                    with pytest.raises(ValueError, match="WINDOW_OUTSIDE_INTERVAL"):
                        await window.send_once(http, protocol, approval)
                    assert window.status()["attempts_reserved"] == 0
                else:
                    clock[0] = plan.end - timedelta(microseconds=1)
                    core.clock.now = clock[0]
                    result = await window.send_once(http, protocol, approval)
                    assert result["attempts_reserved"] == 1
                    assert result["attempt_states"] == {
                        "UNKNOWN" if phase == "deadline-during-probe" else "ACK": 1
                    }
                    assert result["phone_receipts"] is None

    run(scenario())
    assert len(sends) == (1 if phase == "in-flight" else 0)


@pytest.mark.parametrize("fault", ["before-begin", "after-begin", "after-result"])
def test_core_disconnects_stop_and_preserve_attempt_before_restart(core, tmp_path, fault):
    from companion.api import create_app

    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends = WindowPlan(value, cfg, inventory), []
    app = create_app(core.settings)
    app.state.core = core
    inner = httpx.ASGITransport(app=app)
    tripped = False

    class LoseAck(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal tripped
            if not tripped and (
                (request.url.path.endswith("/begin-send") and fault != "after-result")
                or (request.url.path.endswith("/result") and fault == "after-result")
            ):
                tripped = True
                if fault != "before-begin":
                    await inner.handle_async_request(request)
                raise httpx.ReadTimeout("SYNTHETIC_CORE_ACK_LOSS")
            return await inner.handle_async_request(request)

    async def scenario():
        async with (
            core_client(core, LoseAck(), bridge_config=cfg) as http,
            httpx.AsyncClient(
                base_url=cfg.onebot_http_url,
                headers={"Authorization": "Bearer " + cfg.onebot_token},
                transport=httpx.MockTransport(lambda r: provider(r, sends)),
            ) as protocol,
        ):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                await window.open(http)
                approval = await stage(window, http, core)
                with pytest.raises(httpx.ReadTimeout):
                    await window.send_once(http, protocol, approval)
                assert window.status()["attempts_reserved"] == 1
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                    await window.send_once(http, protocol, approval)
                assert window.status()["attempts_reserved"] == 1

    run(scenario())
    assert len(sends) == (1 if fault == "after-result" else 0)
    assert (
        core.store.one("SELECT state FROM outbox")["state"]
        == {"before-begin": "LEASED", "after-begin": "SENDING", "after-result": "SENT"}[fault]
    )


@pytest.mark.parametrize("mutation", ["hash", "message", "event", "origin", "bot"])
def test_reply_approval_and_live_identity_must_match_exact_item(core, tmp_path, mutation):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends = WindowPlan(value, cfg, inventory), []

    def response(request):
        if mutation == "bot" and request.url.path == "/get_login_info":
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"user_id": 99999}}
            )
        return provider(request, sends)

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with (
                core_client(core, bridge_config=cfg) as http,
                httpx.AsyncClient(
                    base_url=cfg.onebot_http_url,
                    headers={"Authorization": "Bearer " + cfg.onebot_token},
                    transport=httpx.MockTransport(response),
                ) as protocol,
            ):
                await window.open(http)
                approval = await stage(window, http, core)
                if mutation in {"hash", "message", "event"}:
                    field = {"hash": "reply_sha256", "message": "message_id", "event": "event_id"}[
                        mutation
                    ]
                    approval[field] = {
                        "hash": "0" * 64,
                        "message": "msg_" + "0" * 32,
                        "event": "evt_" + "0" * 64,
                    }[mutation]
                if mutation == "origin":
                    core.store.execute("UPDATE conversations SET platform_session_key='stranger'")
                with pytest.raises(ValueError):
                    await window.send_once(http, protocol, approval)
                assert window.status()["state"] == "STOPPED"
                assert window.status()["attempts_reserved"] == (1 if mutation == "bot" else 0)

    run(scenario())
    assert sends == []


@pytest.mark.parametrize(
    "initial",
    ["PENDING", "LEASED", "SENDING", "DELIVERY_UNCERTAIN", "spool-pending", "spool-uncertain"],
)
def test_initial_unconfirmed_history_cannot_be_drained(core, tmp_path, initial):
    from companion.contracts import InboundEvent

    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan = WindowPlan(value, cfg, inventory)

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as http:
                if initial.startswith("spool"):
                    if initial == "spool-pending":
                        spool.put(normalized())
                    else:
                        spool.start_delivery(
                            {"message_id": "msg_" + "a" * 32, "lease_token": "lease_" + "b" * 32}
                        )
                else:
                    event = InboundEvent.model_validate(normalized())
                    core.ingest(event, event.event_id)
                    await core.run_once("synthetic-history")
                    core.store.execute("UPDATE outbox SET state=?", (initial,))
                with pytest.raises(ValueError):
                    await window.open(http)
                assert not plan.journal.exists()

    run(scenario())


def test_full_spool_rejects_input_before_core_http(core, tmp_path):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    cfg = replace(cfg, max_pending=1)
    value["config_sha256"] = inventory["config_sha256"] = digest(cfg.snapshot())
    value["runtime_attestation_sha256"] = digest(inventory)
    plan = WindowPlan(value, cfg, inventory)

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as http:
                await window.open(http)
                # Unattended process staged an unapproved input; the bounded command stops.
                spool.put(normalized(raw_event(99)))
                raw = raw_event(1)
                with pytest.raises(ValueError, match="SPOOL_RECONCILIATION_REQUIRED"):
                    await window.ingest(http, raw, input_approval(plan, raw))
                assert window.status()["state"] == "STOPPED"

    run(scenario())
    assert core.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 0


def test_journal_survives_spool_reset_and_detects_rollback(core, tmp_path):
    import sqlite3

    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan = WindowPlan(value, cfg, inventory)

    async def scenario():
        async with core_client(core, bridge_config=cfg) as http:
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                await window.open(http)
                old_head = window.row()["journal_head"]
                raw = raw_event(1)
                await window.ingest(http, raw, input_approval(plan, raw))
                # Fault injection into synthetic DB only: restoring an older DB head.
                spool.db.execute("UPDATE test_window SET journal_head=?", (old_head,))
                spool.db.commit()
                with pytest.raises(ValueError, match="RECONCILIATION_REQUIRED"):
                    window.verify()
            with sqlite3.connect(str(tmp_path / "spool" / "spool.sqlite3")) as db:
                db.executescript(
                    "DROP TABLE test_window;DROP TABLE window_inputs;"
                    "DROP TABLE window_attempts;DELETE FROM inbox;"
                )
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: now)
                with pytest.raises(ValueError, match="WINDOW_ALREADY_REGISTERED"):
                    await window.open(http)

    run(scenario())


def spawn_child(payload):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.process_helpers import window_child;window_child()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    return process


def test_two_executors_use_actual_os_process_lock(tmp_path):
    cfg, _, _, _ = plan_fixture(tmp_path)
    payload = {"config": asdict(cfg), "operation": "lock"}
    first = spawn_child(payload)
    try:
        assert first.stdout.readline().strip() == "LOCKED"
        second = spawn_child(payload)
        try:
            second.stdin.close()
            assert second.wait(timeout=15) == 24
            assert second.stdout.read().strip() == "LOCK_REJECTED"
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=15)
    finally:
        first.stdin.close()
        if first.poll() is None:
            first.wait(timeout=15)
    # Closing/terminating the original owner releases the OS lock, not persisted quotas.
    with Spool(cfg):
        pass


def test_process_death_after_durable_reserve_never_restores_quota(core, tmp_path):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends = WindowPlan(value, cfg, inventory), []

    async def setup():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with core_client(core, bridge_config=cfg) as http:
                await window.open(http)
                return await stage(window, http, core)

    approval = run(setup())
    row = core.store.one("SELECT * FROM outbox")
    payload = {
        "config": asdict(cfg),
        "operation": "reserve-crash",
        "plan": value,
        "inventory": inventory,
        "now": now.isoformat(),
        "approval": approval,
        "item": {
            "message_id": row["message_id"],
            "lease_token": "lease_" + "a" * 32,
            "lease_until": (now + timedelta(seconds=120)).isoformat(),
            "conversation_platform_session_key": cfg.origin,
            "content": json.loads(row["content_json"]),
            "delivery_key": row["delivery_key"],
            "action_type": "qq.private.reply",
        },
    }
    process = spawn_child(payload)
    process.stdin.close()
    assert process.wait(timeout=20) == 23
    assert process.stdout.read() == ""

    async def restarted():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            assert window.status()["attempt_states"] == {"RESERVED": 1}
            async with (
                core_client(core, bridge_config=cfg) as http,
                httpx.AsyncClient(
                    base_url=cfg.onebot_http_url,
                    headers={"Authorization": "Bearer " + cfg.onebot_token},
                    transport=httpx.MockTransport(lambda r: provider(r, sends)),
                ) as protocol,
            ):
                with pytest.raises(ValueError, match="WINDOW_ATTEMPT_RECONCILIATION_REQUIRED"):
                    await window.send_once(http, protocol, approval)
                assert window.status()["attempts_reserved"] == 1

    run(restarted())
    assert sends == []


def test_in_process_concurrent_once_invocations_send_at_most_one(core, tmp_path):
    import asyncio

    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends = WindowPlan(value, cfg, inventory), []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def response(request):
            if request.url.path == "/get_login_info":
                entered.set()
                await release.wait()
            return provider(request, sends)

        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: now)
            async with (
                core_client(core, bridge_config=cfg) as http,
                httpx.AsyncClient(
                    base_url=cfg.onebot_http_url,
                    headers={"Authorization": "Bearer " + cfg.onebot_token},
                    transport=httpx.MockTransport(response),
                ) as protocol,
            ):
                await window.open(http)
                approval = await stage(window, http, core)
                first = asyncio.create_task(window.send_once(http, protocol, approval))
                await asyncio.wait_for(entered.wait(), 5)
                with pytest.raises(ValueError, match="WINDOW_EXECUTOR_BUSY"):
                    await window.send_once(http, protocol, approval)
                release.set()
                assert (await first)["attempt_states"] == {"ACK": 1}

    run(scenario())
    assert len(sends) == 1


def test_async_http_preflight_expiry_blocks_dispatch_and_retains_attempt(core, tmp_path):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    core.clock.now = now
    plan, sends, clock = WindowPlan(value, cfg, inventory), [], [now]

    async def preflight(request):
        if request.url.path == "/send_private_msg":
            # The shipped private client's DNS pinning is asynchronous.
            clock[0] = plan.end

    async def scenario():
        with Spool(cfg) as spool:
            window = TestWindow(spool, plan, clock=lambda: clock[0])
            async with (
                core_client(core, bridge_config=cfg) as http,
                httpx.AsyncClient(
                    base_url=cfg.onebot_http_url,
                    headers={"Authorization": "Bearer " + cfg.onebot_token},
                    transport=httpx.MockTransport(lambda r: provider(r, sends)),
                    event_hooks={"request": [preflight]},
                ) as protocol,
            ):
                await window.open(http)
                approval = await stage(window, http, core)
                clock[0] = plan.end - timedelta(microseconds=1)
                core.clock.now = clock[0]
                status = await window.send_once(http, protocol, approval)
                assert status["attempts_reserved"] == 1
                assert status["attempt_states"] == {"UNKNOWN": 1}
                assert status["state"] == "STOPPED"
                assert protocol.event_hooks["request"] == [preflight]

    run(scenario())
    assert sends == []
