"""Cumulative accounting on the original Core/Bridge boundary, entirely synthetic."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from companion import cumulative, live_text, paid_budget
from companion.config import LiveTextHandoff, LiveTextWindowApproval
from companion.contracts import CoreError
from companion.privacy import epoch
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow, WindowPlan, digest
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.support.cumulative_budget import policy, view
from tests.support.cumulative_window import bind_policy
from tests.support.live_text_window import live_clients, live_fixture
from tests.support.paid_model import approved_live


@asynccontextmanager
async def native_quota(core, tmp_path, **changes):
    number = 0
    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number, text="这次讨论读书安排。"))
        await ws.wait_closed()

    approved_live(core, tmp_path, max_output_tokens=4096, per_attempt_micro_usd=100000,
                  per_request_micro_usd=200000)
    scope = policy(core, **changes)
    async with host(core, tmp_path, source, paid_options={"cumulative": scope}) as values:
        yield values


async def native_stage(core, bridge, *, count=None):
    await bridge.capture(count or bridge.captures() + 1)
    await bridge.flush()
    assert await core.run_once()


def archived_account(core, tmp_path):
    """Original v1 rows and signed timestamp inventory; no legacy live writer."""
    from companion_onebot_bridge import journal
    from tests.support.legacy_owner_input import archived

    old, _, job, mids, key = archived(core, tmp_path)
    # Frozen v1 send custody includes the single original reservation/ACK.
    core.store.execute('UPDATE outbox SET attempt_count=1 WHERE message_id=%s', (mids[0],))
    original = core.store.one('SELECT * FROM events WHERE event_id=%s', (job["event_id"],))
    sent = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (mids[0],))
    path = tmp_path / "original-window-reservations.jsonl"
    reservations, head = [], "0" * 64
    for kind, source_id, at, record_kind, field in (
        ("input", job["event_id"], original["ingested_at"], "CAPTURED", "event_hash"),
        ("send", mids[0], sent["sent_at"], "RESERVED", "message_hash"),
    ):
        head = journal.append(path, record_kind, {field: digest(source_id)}, at=at,
                              expected_head=head, create=not reservations)
        reservations.append({"kind": kind, "source_sha256": digest(source_id),
            "window_id": old.window_id, "at": at,
            "record_sha256": head, "journal_head_sha256": head})
    for item in reservations:
        item["journal_head_sha256"] = head
    value = {"kind": "cumulative_window_inventory_v1",
        "identity_sha256": digest([core.settings.owner_qq_id, core.settings.bot_qq_id]),
        "budget_journal_id": old.budget_journal_id,
        "reservations": sorted(
            reservations, key=lambda item: (item["kind"], item["source_sha256"]))}
    from companion.store import encode

    value = cumulative.LegacyWindowInventory.model_validate(
        {**value, "signature": "0" * 128}).model_dump(mode="json", exclude={"signature"})
    value["signature"] = key.sign(cumulative.LEGACY_DOMAIN + encode(value).encode()).hex()
    core.settings.live_text_window = old  # Historical verification key, never an execution grant.
    scope = policy(core, legacy_window_inventory=value)
    return scope, path


async def renew_native(core, bridge, number, **limits):
    from dataclasses import replace
    from datetime import UTC, datetime
    from math import ceil

    from companion import owner_authority
    from companion.owner_authority_protocol import AuthorityStop
    from companion_onebot_bridge import owner_handoff
    from companion_onebot_bridge.onebot_client import OneBotClient
    from companion_onebot_bridge.owner_runtime import OwnerRuntime
    from tests.support.owner_authority import activate

    original = owner_authority.current(core.store, core.settings, core.clock)
    bridge.runtime.stop()
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        authority=original.authority.as_dict(), operation_id=f"stop-native-quota-{number}"))
    bridge.spool.close()
    bridge.spool = Spool(bridge.config, reconciliation_only=True)
    core.clock.advance(1)
    bridge.runtime = OwnerRuntime(bridge.spool, clock=lambda: core.clock.now)
    prepared = await owner_handoff.prepare(bridge.runtime, bridge.client)
    assert prepared["state"] == "HANDOFF_PREPARED"
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "window_id": "win_" + str(number) * 32})
    policy(core, **limits)
    granted = original.policy.model_copy(update={
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings))})
    successor = activate(core, granted, revision=original.authority.revision + 1,
                          operation=f"renew-native-quota-{number}")[0]["authority"]
    rebound = await owner_handoff.handoff(bridge.runtime, bridge.client, successor)
    assert rebound["state"] == "ACTIVE"
    bridge.config = replace(bridge.config, owner_authority=successor)
    bridge.restart()
    bridge.onebot = OneBotClient(bridge.config, bridge.protocol)
    # OneBot timestamps have second precision; the next frame must postdate the grant.
    admitted_at = core.store.one(
        'SELECT created_at FROM owner_authority_revisions WHERE grant_id=%s AND revision=%s',
        (successor["grant_id"], successor["revision"]))["created_at"]
    earliest_frame = ceil(datetime.fromisoformat(admitted_at).timestamp())
    await asyncio.sleep(max(0, earliest_frame - datetime.now(UTC).timestamp()) + 0.1)
    assert datetime.now(UTC).timestamp() >= earliest_frame


def fixture(core, tmp_path, **changes):
    cfg, plan = live_fixture(core, tmp_path)
    return cfg, bind_policy(core, plan, **changes)


def successor(core, old, tmp_path, *, number=2, **changes):
    value = deepcopy(old.plan.value)
    value.update(window_id="win_" + str(number) * 32,
                 journal_path=str(tmp_path / f"window-{number}.jsonl"))
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "window_id": value["window_id"], "cumulative": None,
    })
    scope = policy(core, **changes)
    custody = paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    handoff = LiveTextHandoff.model_validate({
        "predecessor_plan_sha256": old.plan.sha256,
        "predecessor_window_id": old.plan.value["window_id"],
        "budget_journal_head_sha256": custody["head_sha256"],
        "bridge_journal_head_sha256": old.row()["journal_head"],
        "identity_sha256": value["identity_sha256"],
        "protocol_epoch": old.plan.config.protocol_epoch, "privacy_epoch": epoch(core.store),
        "erasure_ledger_sha256": hashlib.sha256(
            Path(core.settings.erasure_ledger).read_bytes()).hexdigest(),
        "evidence_sha256": "9" * 64,
    })
    live = core.settings.live_text_window.model_copy(update={
        "window_id": value["window_id"], "cumulative": scope, "handoff": handoff,
        "core_config_sha256": live_text.config_sha256(core.settings),
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
    })
    value["live_text"] = live.model_dump(mode="json", exclude={"plan_sha256"})
    core.settings.live_text_window = live.model_copy(update={"plan_sha256": digest(value)})
    inventory = {key: value[key] for key in (
        "identity_sha256", "protocol_epoch", "config_sha256", "config_version", "image_ids",
    )}
    return WindowPlan(value, old.plan.config, inventory)


def test_input_and_send_responsibility_are_retained_in_original_ledgers(core, tmp_path):
    from companion import owner_input
    from companion.owner_authority_protocol import OwnerCapture

    async def scenario():
        async with native_quota(core, tmp_path,
            account={"owner_inputs": 1, "send_attempts": 1}) as (bridge, _, sends, models):
            await native_stage(core, bridge)
            assert await bridge.pump()
            records = bridge.runtime.verify()
            assert sum(row["kind"] == "CAPTURED" for row in records) == 1
            assert sum(row["kind"] == "RESERVED" for row in records) == 1
            operations = view(core)["operations"]
            assert len(operations) == 2
            assert sorted(v["state"] for v in operations.values()) == ["ADMITTED", "SENT"]
            value = json.loads(core.store.one(
                'SELECT value FROM runtime_meta WHERE key LIKE '
                "'owner_input:%%'"
            )["value"])
            assert owner_input.approve(core.store, core.settings, core.clock,
                OwnerCapture.model_validate(value["capture"]))["duplicate"]
            assert len(view(core)["operations"]) == 2
            await bridge.capture(2)
            with pytest.raises(httpx.HTTPStatusError) as error:
                await bridge.flush()
            assert error.value.response.status_code == 429
            assert len(models) == len(sends) == 1
            assert len(view(core)["operations"]) == 2
    run(scenario())


@pytest.mark.parametrize("paused", [False, True])
def test_authenticated_ws_and_real_http_respect_cumulative_capture_boundary(core, tmp_path, paused):
    async def scenario():
        async with native_quota(core, tmp_path,
            account={"owner_inputs": 1, "send_attempts": 1},
        ) as (bridge, connections, sends, models):
            if paused:
                with core.store.transaction():
                    cumulative.pause(core.store, core.settings.paid_model, core.clock())
            await bridge.capture()
            if paused:
                with pytest.raises(httpx.HTTPStatusError):
                    await bridge.flush()
                assert core.store.rows("SELECT * FROM events") == []
                assert sends == models == []
            else:
                await bridge.flush()
                assert await core.run_once("synthetic-quota")
                assert await bridge.pump()
                assert len(models) == len(sends) == 1
            assert connections == ["Bearer " + bridge.config.onebot_token]
    run(scenario())


@pytest.mark.parametrize("dimension", ["owner_inputs", "send_attempts"])
@pytest.mark.parametrize("period", ["window", "day", "month", "account"])
def test_bridge_caps_reject_before_extra_model_or_send(core, tmp_path, period, dimension):
    async def scenario():
        async with native_quota(core, tmp_path,
            **{period: {dimension: 0}}) as (bridge, _, sends, models):
            await bridge.capture()
            if dimension == "owner_inputs":
                with pytest.raises(httpx.HTTPStatusError) as error:
                    await bridge.flush()
                assert error.value.response.status_code == 429
                assert models == []
                assert not core.store.rows("SELECT * FROM events")
            else:
                await bridge.flush()
                assert await core.run_once()
                assert await bridge.pump()
                assert len(models) == 1
                assert bridge.attempts() == {"UNKNOWN": 1}
            assert sends == []
            assert cumulative.status(core.store, core.settings, core.clock())["used"][
                period][dimension] == 0
    run(scenario())


def test_final_send_checks_current_pause_and_spends_local_reservation(core, tmp_path):
    async def scenario():
        async with native_quota(core, tmp_path) as (bridge, _, sends, models):
            await native_stage(core, bridge)
            async def pause(request):
                if request.url.path == "/send_private_msg":
                    with core.store.transaction():
                        cumulative.pause(core.store, core.settings.paid_model, core.clock())
            bridge.protocol.event_hooks["request"].append(pause)
            assert await bridge.pump()
            assert len(models) == 1 and sends == []
            assert sum(bridge.attempts().values()) == 1
            assert sum(row["kind"] == "RESERVED"
                       for row in bridge.runtime.verify(active=False)) == 1
            assert view(core)["paused"]
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    run(scenario())


def test_private_pause_keeps_read_status_available_after_approval_removal(core, tmp_path):
    cfg, plan = fixture(core, tmp_path)

    async def scenario():
        async with live_clients(core, cfg) as (client, _, models, sends):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client)
                response = await client.post("/internal/v1/cumulative/pause", json={
                    "account_ref": "b" * 64, "policy_sha256": view(core)["policy_sha256"],
                    "evidence_sha256": "a" * 64,
                })
                assert response.status_code == 200 and response.json()["paused"]
                core.settings.paid_model = None
                core.settings.live_text_window = None
                status = await client.get("/internal/v1/cumulative/status")
                assert status.status_code == 200 and status.json()["paused"]
                assert "journal_path" not in json.dumps(status.json())
                stopped = await client.post("/internal/v1/live-text/stop", json={
                    "window_sha256": plan.sha256,
                })
                assert stopped.status_code == 200 and stopped.json()["stopped"]
                assert models == sends == []

    run(scenario())


def test_missing_spool_projection_cannot_reset_cumulative_inputs(core, tmp_path):
    async def scenario():
        async with native_quota(core, tmp_path) as (bridge, _, sends, models):
            await native_stage(core, bridge)
            prior = view(core)["operations"]
            with bridge.db:
                bridge.db.execute("DELETE FROM window_inputs")
            with pytest.raises(ValueError, match="OWNER_JOURNAL_RECONCILIATION_REQUIRED"):
                bridge.runtime.verify()
            assert len(models) == 1 and sends == []
            assert view(core)["operations"] == prior
    run(scenario())


def test_stop_requires_new_policy_for_successor(core, tmp_path):
    from tests.support.live_text_handoff import (
        configure,
        open_successor,
        reference,
        stop_and_successor,
    )

    first = configure(core, tmp_path)
    scope = policy(core)

    def update(approval, scope):
        updated = approval.model_copy(update={
            "cumulative": scope, "core_config_sha256": live_text.config_sha256(core.settings),
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        })
        core.settings.live_text_window = updated
        return updated

    first = update(first, scope)
    live_text.open_window(core.store, core.settings, core.clock, reference(first))
    _, new = stop_and_successor(core, tmp_path)
    assert view(core)["paused"]
    core.settings.paid_model = core.settings.paid_model.model_copy(update={"cumulative": scope})
    new = update(new, scope)
    with pytest.raises(CoreError, match="CUMULATIVE_PAUSED"):
        live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    renewed = policy(core)
    new = update(new, renewed)
    prepared = live_text.prepare_handoff(core.store, core.settings, core.clock, reference(new))
    assert open_successor(core, new, prepared)["approved"]
    assert len(view(core)["policies"]) == 2 and not view(core)["paused"]
    live_text.stop_window(core.store, reference(first))
    assert not view(core)["paused"]


def test_policy_contract_cannot_disagree_between_paid_and_window(core, tmp_path):
    cfg, plan = fixture(core, tmp_path)
    live = core.settings.live_text_window.model_dump(mode="json")
    live.pop("cumulative")
    core.settings.live_text_window = LiveTextWindowApproval.model_validate(live)
    with pytest.raises(CoreError, match="LIVE_TEXT_PAID_CONTRACT_CHANGED"):
        live_text.open_window(core.store, core.settings, core.clock,
                              live_text.WindowReference(window_sha256=plan.sha256))


@pytest.mark.parametrize("legacy", [False, True])
def test_real_handoff_preserves_complete_input_send_and_model_history(core, tmp_path, legacy):
    if legacy:
        archived_account(core, tmp_path)
        original_usage = core.store.rows("SELECT * FROM model_usage")
        with core.store.transaction():
            cumulative.activate(core.store, core.settings, core.clock())
        before = view(core)
        assert len(before["operations"]) == 2 and len(before["models"]) == 1
        policy(core, legacy_adoption_evidence_sha256=None, legacy_window_inventory=None)
        with core.store.transaction():
            cumulative.activate(core.store, core.settings, core.clock())
        after = view(core)
        assert after["operations"] == before["operations"]
        assert after["models"] == before["models"]
        assert core.store.rows("SELECT * FROM model_usage") == original_usage
        assert len(after["policies"]) == 2
        return

    limits = {"account": {"owner_inputs": 2, "send_attempts": 2, "model_attempts": 2}}
    async def scenario():
        async with native_quota(core, tmp_path, **limits) as (bridge, _, sends, models):
            await native_stage(core, bridge)
            assert await bridge.pump()
            prefix = bridge.runtime.verify()
            original_usage = core.store.rows("SELECT * FROM model_usage")
            await renew_native(core, bridge, 2, **limits)
            await bridge.capture(2)
            capture = json.loads(bridge.db.execute(
                "SELECT capture_json FROM window_inputs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0])
            response = await bridge.client.post(
                "/internal/v1/owner-authority/inputs/approve", json=capture)
            assert response.status_code == 200, json.dumps({"response": response.text,
                "occurred": capture["occurred_at"], "received": capture["received_at"],
                "revision": capture["authority"]["revision"],
                "grants": core.store.rows(
                    "SELECT revision,created_at FROM owner_authority_revisions")})
            await bridge.flush()
            assert await core.run_once()
            assert await bridge.pump(), json.dumps(core.store.rows("SELECT * FROM jobs"))
            assert bridge.runtime.verify()[:len(prefix)] == prefix
            totals = cumulative.status(core.store, core.settings, core.clock())["used"]["account"]
            assert all(totals[key] == 2
                       for key in ("owner_inputs", "send_attempts", "model_attempts"))
            assert len(view(core)["operations"]) == 4
            for row in original_usage:
                assert core.store.one('SELECT * FROM model_usage WHERE request_id=%s',
                                      (row["request_id"],)) == row
            await bridge.capture(3)
            with pytest.raises(httpx.HTTPStatusError):
                await bridge.flush()
            assert len(models) == len(sends) == 2
    run(scenario())


def test_forgotten_content_and_old_backup_keep_latest_cumulative_responsibility(core, tmp_path):
    from cryptography.fernet import Fernet

    from companion import owner_authority
    from companion.backup import backup, restore
    from companion.core import Core
    from tests.conftest import FakeModel

    key = Fernet.generate_key()
    archive, destination = tmp_path / "old.enc", tmp_path / "restored.db"
    async def scenario():
        async with native_quota(core, tmp_path) as (bridge, _, sends, models):
            await asyncio.to_thread(backup, core.settings.database, archive, key)
            await native_stage(core, bridge)
            assert await bridge.pump()
            event_id = core.store.one("SELECT event_id FROM events")["event_id"]
            before = cumulative.status(core.store, core.settings, core.clock())["used"]
            with core.store.transaction():
                core.ledger.expire_events(core.store, [event_id], core.clock())
            assert cumulative.status(core.store, core.settings, core.clock())["used"] == before
            await asyncio.to_thread(restore, archive, destination, key,
                                    core.settings.erasure_ledger)
            settings = core.settings.model_copy(update={"database": str(destination)})
            with Core(settings, FakeModel(), core.clock) as restored:
                assert restored.store.one('SELECT 1 FROM runtime_meta WHERE key=%s',
                                          ("erased_event:" + event_id,))
                assert not cumulative.status(restored.store, settings, core.clock())["available"]
                assert owner_authority.status(
                    restored.store, settings, core.clock)["state"] == "OFF"
                assert not await restored.run_once("synthetic-recovery")
                assert restored.model.calls == []
            assert len(models) == len(sends) == 1
    run(scenario())


def test_legacy_window_timestamp_cannot_be_changed_without_its_original_signature(core, tmp_path):
    scope, path = archived_account(core, tmp_path)
    inventory = scope.legacy_window_inventory.model_dump(mode="json")
    inventory["reservations"][0]["at"] = "2026-10-01T00:00:00Z"
    changed = scope.model_copy(update={
        "legacy_window_inventory": cumulative.LegacyWindowInventory.model_validate(inventory)})
    original_journal = Path(core.settings.paid_model.journal_path).read_bytes()
    original_reservations = path.read_bytes()
    with pytest.raises(CoreError, match="CUMULATIVE_LEGACY_WINDOW_PROOF_REQUIRED"):
        cumulative.verify_legacy_inventory(core.store, core.settings, changed)
    assert Path(core.settings.paid_model.journal_path).read_bytes() == original_journal
    assert path.read_bytes() == original_reservations
    cumulative.verify_legacy_inventory(core.store, core.settings, scope)


def test_third_window_inherits_original_legacy_adoption_without_repeating_inventory(core, tmp_path):
    archived_account(core, tmp_path)
    original_usage = core.store.rows("SELECT * FROM model_usage")
    with core.store.transaction():
        cumulative.activate(core.store, core.settings, core.clock())
    original = view(core)
    for _ in range(2):
        renewed = policy(core, legacy_adoption_evidence_sha256=None, legacy_window_inventory=None)
        assert renewed.legacy_window_inventory is None
        with core.store.transaction():
            cumulative.activate(core.store, core.settings, core.clock())
        assert view(core)["models"] == original["models"]
        assert view(core)["operations"] == original["operations"]
    assert len(view(core)["policies"]) == 3
    assert len(view(core)["operations"]) == 2
    assert core.store.rows("SELECT * FROM model_usage") == original_usage
    assert core.model.calls == []


def test_final_live_and_cumulative_fence_share_one_clock_sample(core, tmp_path):
    cfg, plan = fixture(core, tmp_path)

    async def scenario():
        async with live_clients(core, cfg) as (client, _, models, sends):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client)
                calls = []

                def sample():
                    calls.append(1)
                    return core.clock() if len(calls) == 1 else plan.end.isoformat()

                live_text.dispatch_fence(core.settings, sample, plan.end.isoformat(),
                    plan.end.isoformat(), quota_state=view(core))
                assert calls == [1]
                with pytest.raises(CoreError, match="LIVE_TEXT_APPROVAL_EXPIRED"):
                    live_text.dispatch_fence(core.settings, lambda: plan.end.isoformat(),
                        plan.end.isoformat(), plan.end.isoformat(), quota_state=view(core))
                state = view(core)
                core.clock.advance(-1)
                with pytest.raises(CoreError, match="CUMULATIVE_DISPATCH_FENCED"):
                    live_text.dispatch_fence(core.settings, core.clock, plan.end.isoformat(),
                        plan.end.isoformat(), quota_state=state)
                assert models == sends == []

    run(scenario())
