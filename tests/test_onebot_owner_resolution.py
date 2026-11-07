"""Stopped native Bridge custody retains original send evidence across owner decisions."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

import companion_delivery_protocol as wire
from companion import delivery_reconciliation as reconciliation
from companion import owner_authority
from companion.poke_protocol import PokePolicy
from companion.reply_segments import SegmentedReplyConfig
from companion_onebot_bridge import journal, owner_handoff
from companion_onebot_bridge import owner_native_resolution as owner_resolution
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.native_continuous_support import answer, frame, host
from tests.native_owner_recovery_support import CONTROL_TOKEN, stop_host
from tests.support.onebot_owner_resolution import (
    assert_custody_counts,
    original_rows,
    reopen,
    successor,
)
from tests.support.poke import native_frame


@asynccontextmanager
async def uncertain(core, tmp_path, monkeypatch, *, crash=False, kind="poke", segmented=False):
    sent = []
    policy = (SegmentedReplyConfig(max_segments=2, min_chars=4, interval_ms=0, mode="semantic")
              if segmented else None)
    text = "第一条核对前已发送。\n第二条发送响应丢失。" if segmented else "合成待核对文字回复"

    async def source(ws):
        timestamp = int(core.clock.now.timestamp())
        message = (native_frame(timestamp) if kind == "poke"
                   else {**json.loads(frame()), "time": timestamp})
        await ws.send(json.dumps(message))
        await ws.wait_closed()

    def lost(request):
        if request.url.path == ("/friend_poke" if kind == "poke" else "/send_private_msg"):
            sent.append(request)
            if segmented and len(sent) == 1:
                return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                                "data": {"message_id": 777}})
            if crash:
                raise asyncio.CancelledError("synthetic process interruption")
            raise httpx.ReadTimeout("synthetic lost native response")

    async with host(core, tmp_path, source, policy=policy, protocol_reply=lost,
        model_reply=lambda _: answer('{"kind":"poke"}' if kind == "poke" else text),
        core_options={"control_token": SecretStr(CONTROL_TOKEN),
                      "poke_policy": PokePolicy(enabled=True, allow_native=True,
                                                coalesce_seconds=0)},
        purposes=("chat", "poke") if kind == "poke" else ("chat",)) as (active, _, _, calls):
        # The signed receipt and Core source-age checks share this fixture clock.
        monkeypatch.setattr("companion_onebot_bridge.app.datetime",
                            SimpleNamespace(now=lambda _tz: core.clock.now))
        grant = owner_authority.current(core.store, core.settings, core.clock).policy
        await active.capture()
        await active.flush()
        assert await core.run_once("owner-resolution-synthetic")
        for _ in range(2 if segmented else 1):
            if crash:
                with pytest.raises(asyncio.CancelledError):
                    await active.pump()
            else:
                assert await active.pump()
        await stop_host(active)
        attempt = owner_handoff._rows(active.db,
            "SELECT * FROM window_attempts ORDER BY fence DESC LIMIT 1")[0]
        result = owner_handoff._rows(active.db, "SELECT * FROM results WHERE message_id=?",
                                      (attempt["message_id"],))[0]
        assert len(calls) == 1
        yield SimpleNamespace(active=active, runtime=active.runtime, client=active.client,
            sent=sent, message_id=attempt["message_id"], attempt=attempt, result=result,
            core=core, cfg=active.config, policy=grant, monkeypatch=monkeypatch)


def decide(env, outcome):
    return reconciliation.resolve(env.core.store, env.core.settings, env.message_id, outcome,
        env.core.clock(), operation_id="owner-native-resolution",
        operation_sha256=wire.digest([env.message_id, outcome]))


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_owner_resolution_preserves_unknown_and_counts_and_allows_inventory(
    core, tmp_path, monkeypatch, outcome,
):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch) as env:
            with pytest.raises(RuntimeError, match="BRIDGE_ALREADY_RUNNING"):
                Spool(env.cfg, reconciliation_only=True)
            with pytest.raises(ValueError, match="DELIVERY_QUIESCENT_PROCESS_REQUIRED"):
                await owner_resolution.prepare(env.runtime, env.client, env.message_id)
            window = reopen(env)
            with window.spool:
                with pytest.raises(ValueError, match="OWNER_INPUT_TERMINAL_REQUIRED"):
                    await owner_handoff.prepare(window, env.client)
                first = await owner_resolution.prepare(window, env.client, env.message_id)
                assert first["state"] == "READY" and first["paused"]
                assert await owner_resolution.prepare(window, env.client, env.message_id) == first
                assert (await owner_resolution.reconcile(window, env.client, env.message_id))[
                    "state"] == "AWAITING_OWNER"
                receipt = decide(env, outcome)["receipt"]
                result = await owner_resolution.reconcile(window, env.client, env.message_id)
                assert result["state"] == "OWNER_RESOLVED" and result["outcome"] == outcome
                assert result["receipt_sha256"] == receipt["receipt_sha256"]
                assert await owner_resolution.reconcile(window, env.client, env.message_id) == (
                    result)
                assert original_rows(window, "window_attempts")[0] == (
                    env.attempt)
                assert original_rows(window, "results")[0] == env.result
                assert window.row()["state"] == "STOPPED" and window.stop_path.is_file()
                await owner_handoff.prepare(window, env.client)
                assert_custody_counts(window)
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    window.verify()
                assert not await window.spool.flush_result(env.client)
                assert len(env.sent) == 1
                row = core.store.one("SELECT * FROM outbox")
                assert row["state"] == outcome and row["provider_message_id"] is None
                assert row["native_ack_json"] is None and row["attempt_count"] == 1

    run(scenario())


def test_prepare_lost_response_reuses_the_exact_signed_proof(core, tmp_path, monkeypatch):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch) as env:
            window = reopen(env)
            with window.spool:
                async def lost(response):
                    if response.request.url.path.endswith("/reconciliation/prepare"):
                        raise httpx.ReadError("synthetic lost preparation response")

                env.client.event_hooks["response"].append(lost)
                with pytest.raises(httpx.ReadError):
                    await owner_resolution.prepare(window, env.client, env.message_id)
                original = dict(owner_resolution.rows(window)[0])
                env.client.event_hooks["response"].remove(lost)
                core.clock.advance(1)
                assert (await owner_resolution.prepare(window, env.client, env.message_id))[
                    "quiescence_sha256"] == wire.digest(json.loads(original["proof_json"]))
                assert dict(owner_resolution.rows(window)[0]) == original
                assert len([r for r in journal.read(window.path)[0]
                            if r["kind"] == "DELIVERY_QUIESCED"]) == 1

    run(scenario())


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_text_reply_retains_original_revision_and_never_fabricates_input_ack(
    core, tmp_path, monkeypatch, outcome,
):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch, kind="text") as env:
            window = reopen(env)
            with window.spool:
                await owner_resolution.prepare(window, env.client, env.message_id)
                proof = json.loads(owner_resolution.rows(window)[0]["proof_json"])
                assert proof["action_type"] == "qq.private.reply"
                assert proof["payload_kind"] == "reply_sha256"
                assert proof["reply_revision"] == 1
                assert proof["payload_sha256"] == wire.digest("合成待核对文字回复")
                decide(env, outcome)
                await owner_resolution.reconcile(window, env.client, env.message_id)
                assert original_rows(window, "window_attempts")[0] == (
                    env.attempt)
                assert original_rows(window, "results")[0] == env.result
                assert window.db.execute("SELECT state FROM window_inputs").fetchone()[0] == (
                    "CAPTURED")
                await owner_handoff.prepare(window, env.client)
                assert sum(r["kind"] == "OWNER_INPUT_TERMINAL" for r in
                           window.verify(active=False)) == 1
                assert_custody_counts(window)
                assert len(env.sent) == 1

    run(scenario())


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
@pytest.mark.parametrize("kind", ["poke", "text"])
def test_owner_resolution_allows_actual_core_handoff_without_reopening(
    core, tmp_path, monkeypatch, outcome, kind,
):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch, kind=kind) as env:
            window = reopen(env)
            with window.spool:
                await owner_resolution.prepare(window, env.client, env.message_id)
                decide(env, outcome)
                await owner_resolution.reconcile(window, env.client, env.message_id)
                config = await successor(env, window)
                window.spool.close()
                with Spool(config) as spool:
                    new = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    new.verify()
                    assert original_rows(new, "window_attempts")[0] == env.attempt
                    assert original_rows(new, "results")[0] == env.result
                    assert core.store.one("SELECT state FROM outbox")["state"] == outcome
                    assert len(env.sent) == 1
                    assert not new.stop_path.exists()
                    assert window.stop_path.read_bytes() == b"OWNER_STOP\n"


    run(scenario())


@pytest.mark.parametrize("tamper", [False, True])
def test_input_closure_binds_prior_segment_provider_receipt(core, tmp_path, monkeypatch, tamper):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch, kind="text", segmented=True) as env:
            window = reopen(env)
            with window.spool:
                prior = owner_handoff._rows(window.db,
                    "SELECT r.* FROM results r JOIN window_attempts a "
                    "USING(message_id) WHERE a.state='ACK'")[0]
                assert len(env.sent) == 2
                await owner_resolution.prepare(window, env.client, env.message_id)
                receipt = decide(env, "CANCELLED")["receipt"]
                assert receipt["input_closure"]["terminal"]
                assert len(receipt["input_closure"]["deliveries"]) == 2
                await owner_resolution.reconcile(window, env.client, env.message_id)
                if tamper:
                    wrong = json.loads(prior["body"]) | {"provider_message_id": "888"}
                    with window.db:
                        window.db.execute("UPDATE results SET body=? WHERE message_id=?",
                                          (wire.canonical(wrong).decode(), prior["message_id"]))
                    with pytest.raises(ValueError, match="OWNER_HANDOFF_RECONCILIATION_REQUIRED"):
                        await owner_handoff.prepare(window, env.client)
                    with window.db:
                        window.db.execute("UPDATE results SET body=? WHERE message_id=?",
                                          (prior["body"], prior["message_id"]))
                await owner_resolution.reconcile(window, env.client, env.message_id)
                assert_custody_counts(window, sends=2)
                config = await successor(env, window)
                window.spool.close()
                with Spool(config) as spool:
                    OwnerRuntime(spool, clock=lambda: core.clock.now).verify()
                    assert len(env.sent) == 2

    run(scenario())


def test_crashed_send_requires_expired_lease_and_retains_pending_original_result(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch, crash=True) as env:
            assert env.attempt["state"] == "IO_STARTED" and env.result["state"] == "pending"
            window = reopen(env)
            with window.spool:
                with pytest.raises(httpx.HTTPStatusError) as denied:
                    await owner_resolution.prepare(window, env.client, env.message_id)
                assert denied.value.response.status_code == 409
                core.clock.advance(601)
                assert (await owner_resolution.prepare(window, env.client, env.message_id))[
                    "state"] == "READY"
                decide(env, "CANCELLED")
                await owner_resolution.reconcile(window, env.client, env.message_id)
                assert original_rows(window, "results")[0] == env.result
                assert original_rows(window, "window_attempts")[0] == (
                    env.attempt)
                assert not await window.spool.flush_result(env.client)
                assert len(env.sent) == 1
                await owner_handoff.prepare(window, env.client)
                assert_custody_counts(window)

    run(scenario())


@pytest.mark.parametrize("damage", ["attempt", "result", "proof", "receipt", "missing_projection",
                                    "tail", "receipt_rollback", "extra_projection"])
def test_owner_resolution_projection_cannot_hide_original_evidence_changes(
    core, tmp_path, monkeypatch, damage,
):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch) as env:
            window = reopen(env)
            with window.spool:
                await owner_resolution.prepare(window, env.client, env.message_id)
                decide(env, "CANCELLED")
                await owner_resolution.reconcile(window, env.client, env.message_id)
                with window.db:
                    if damage == "attempt":
                        window.db.execute("UPDATE window_attempts SET state='ACK'")
                    elif damage == "result":
                        window.db.execute("UPDATE results SET body='{}'")
                    elif damage == "proof":
                        window.db.execute("UPDATE window_delivery_resolutions SET proof_json='{}'")
                    elif damage == "receipt":
                        window.db.execute(
                            "UPDATE window_delivery_resolutions SET receipt_json='{}'")
                    elif damage == "missing_projection":
                        window.db.execute("DELETE FROM window_delivery_resolutions")
                    elif damage == "receipt_rollback":
                        window.db.execute("UPDATE window_delivery_resolutions SET "
                                          "receipt_json=NULL,receipt_record_sha256=NULL")
                    elif damage == "extra_projection":
                        row = owner_resolution.rows(window)[0]
                        window.db.execute("INSERT INTO window_delivery_resolutions VALUES "
                            "(?,?,?,?,?)", ("msg_" + "f" * 32, row["plan_hash"], row["proof_json"],
                                           row["receipt_json"], row["receipt_record_sha256"]))
                    else:
                        records, _ = journal.read(window.path)
                        assert records[-1]["kind"] == "OWNER_RECONCILED"
                        window.path.write_text("".join(
                            wire.canonical(record).decode() + "\n" for record in records[:-1]),
                            encoding="utf-8")
                        window.db.execute("UPDATE owner_runtime SET journal_head=?",
                                          (wire.digest(records[-2]),))
                with pytest.raises(ValueError, match="(?:DELIVERY_|OWNER_)"):
                    window.verify(active=False)
                with pytest.raises(ValueError, match="(?:DELIVERY_|OWNER_)"):
                    await window.spool.flush_result(env.client)

    run(scenario())


def test_quiescence_requires_original_stop_marker_and_cannot_send(core, tmp_path, monkeypatch):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch) as env:
            window = reopen(env)
            with window.spool:
                with pytest.raises(ValueError, match="DELIVERY_QUIESCENT_PROCESS_REQUIRED"):
                    window.spool.start_delivery({})
                window.stop_path.write_bytes(b"changed\n")
                with pytest.raises(ValueError, match="DELIVERY_QUIESCENT_PROCESS_REQUIRED"):
                    await owner_resolution.prepare(window, env.client, env.message_id)
                assert not owner_resolution.rows(window)

    run(scenario())


def test_reconciliation_cli_uses_fresh_process_lock_and_real_core_http(core, tmp_path, monkeypatch):
    async def scenario():
        async with uncertain(core, tmp_path, monkeypatch) as env:
            config = tmp_path / "bridge.toml"
            config.write_text("\n".join(f"{key} = {json.dumps(value)}"
                for key, value in asdict(env.cfg).items()
                if value is not None and key != "owner_authority") + "\n[owner_authority]\n"
                + "\n".join(f"{key} = {json.dumps(value)}"
                            for key, value in env.cfg.owner_authority.items()), "utf-8")
            if os.name != "nt":
                config.chmod(0o600)

            async def cli(command):
                result = await asyncio.to_thread(subprocess.run, [sys.executable,
                    "-m", "companion_onebot_bridge", "--config", str(config), command,
                    "--message-id", env.message_id], capture_output=True, text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=15)
                return result.returncode, json.loads(result.stdout)

            code, result = await cli("owner-delivery-prepare")
            assert code == 1 and result["state"] == "STOPPED"
            assert len(env.sent) == 1
            env.runtime.spool.close()
            core.clock.now = datetime.now(UTC) + timedelta(seconds=30)
            code, result = await cli("owner-delivery-prepare")
            assert code == 0 and result["state"] == "READY" and result["paused"]
            code, result = await cli("owner-delivery-reconcile")
            assert code == 0 and result["state"] == "AWAITING_OWNER"
            # Receipt and subprocess clocks must remain causally ordered.
            core.clock.now = datetime.now(UTC)
            decide(env, "CANCELLED")
            code, result = await cli("owner-delivery-reconcile")
            assert code == 0 and result["state"] == "OWNER_RESOLVED" and result["paused"]
            assert len(env.sent) == 1

    run(scenario())
