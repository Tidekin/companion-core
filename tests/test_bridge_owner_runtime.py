import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge import journal
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from companion_onebot_bridge.spool import Spool


@pytest.fixture
def config(tmp_path):
    return BridgeConfig(owner_qq_id="12345", bot_qq_id="67890", bridge_token="b" * 32,
        onebot_token="o" * 32, protocol_epoch="epoch1234", data_dir=str(tmp_path),
        capture_signing_key="c" * 64, owner_authority={"version": 2,
            "grant_id": "grant_" + "a" * 32, "revision": 1, "policy_sha256": "d" * 64,
            "restore_generation": "initial"})


def test_owner_stop_survives_restart_and_refuses_new_authority(config):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        runtime.stop()
        runtime.stop()
        assert len(runtime.verify(active=False)) == 2
        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
            runtime.verify()
    with Spool(config) as spool:
        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
            OwnerRuntime(spool).verify()
    changed = replace(config, owner_authority=config.owner_authority | {"revision": 2})
    with Spool(changed) as spool:
        with pytest.raises(ValueError, match="OWNER_AUTHORITY_BINDING_CHANGED"):
            OwnerRuntime(spool)


def test_native_stop_command_fences_active_process_and_status_is_read_only(config):
    from companion_onebot_bridge.owner_runtime import command

    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        before = runtime.row()["journal_head"]
        assert command(config, SimpleNamespace(command="owner-status"))["state"] == "ACTIVE"
        result = command(config, SimpleNamespace(command="owner-stop"))
        assert result["state"] == "STOP_REQUESTED" and not result["core_stop_confirmed"]
        assert runtime.row()["journal_head"] == before
        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
            runtime.verify()
        runtime.stop()
    assert command(config, SimpleNamespace(command="owner-status"))["state"] == "STOPPED"


def test_daemon_requires_native_authority_before_network(config):
    from companion_onebot_bridge.app import run

    with pytest.raises(ValueError, match="OWNER_AUTHORITY_REQUIRED"):
        asyncio.run(run(replace(config, owner_authority=None)))


@pytest.mark.parametrize("operation", ["window-open", "window-handoff", "window-run",
    "window-ingest", "window-capture-once", "window-approve-input", "window-send-once"])
def test_retired_window_cli_cannot_execute(config, operation):
    from companion_onebot_bridge.window import command

    with pytest.raises(ValueError, match="LEGACY_WINDOW_EXECUTION_RETIRED"):
        asyncio.run(command(config, SimpleNamespace(command=operation)))


def test_journal_ahead_of_projection_blocks_restart(config):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        journal.append(runtime.path, "OWNER_STOPPED", {"authority": runtime.authority},
                       at=runtime.clock().isoformat(), expected_head=runtime.row()["journal_head"])
        with pytest.raises(ValueError, match="OWNER_JOURNAL_RECONCILIATION_REQUIRED"):
            OwnerRuntime(spool)


def test_legacy_custody_is_not_implicitly_authorized(config):
    with Spool(config) as spool:
        spool.db.execute("CREATE TABLE test_window(evidence TEXT)")
        spool.db.execute("INSERT INTO test_window VALUES('retained')")
        with pytest.raises(ValueError, match="OWNER_CUSTODY_CUTOVER_REQUIRED"):
            OwnerRuntime(spool)
        assert spool.db.execute("SELECT evidence FROM test_window").fetchone()[0] == "retained"
        assert not (spool.directory / "owner-journal.jsonl").exists()


def test_stop_during_core_check_blocks_continuation(config):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)

        def respond(request):
            assert request.url.path == "/internal/v1/owner-authority/check"
            assert json.loads(request.content) == {"authority": runtime.authority,
                                                   "purpose": "chat"}
            runtime.stop()
            return httpx.Response(200, json={"approved": True, "authority": runtime.authority,
                "purpose": "chat", "expires_at": None,
                "capability_policies": {},
                "budget_journal_id": "budget_" + "e" * 32,
                "capture": {"public_key": config.capture_public_key,
                    "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
                    "protocol_epoch": config.protocol_epoch}})

        async def run():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                                        transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    await runtime.check(client)

        asyncio.run(run())


@pytest.mark.parametrize("purpose,policies,accepted", [
    ("chat", {}, True), ("voice", {"voice": "a" * 64}, True),
    ("voice", {}, False), ("voice", {"image": "a" * 64}, False),
    ("chat", {"unknown": "a" * 64}, False),
    ("chat", {"voice": "A" * 64}, False), ("chat", [], False),
])
def test_current_capability_metadata_has_a_closed_shape(config, purpose, policies, accepted):
    from companion_onebot_bridge.owner_runtime import check_receipt

    value = {"approved": True, "authority": config.owner_authority,
        "purpose": purpose, "expires_at": None, "capability_policies": policies,
        "budget_journal_id": "budget_" + "e" * 32,
        "capture": {"public_key": config.capture_public_key,
            "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
            "protocol_epoch": config.protocol_epoch}}
    if accepted:
        assert check_receipt(config, config.owner_authority, purpose,
                             value, datetime.now(UTC)) == value
    else:
        with pytest.raises(ValueError, match="OWNER_AUTHORITY_CHECK_REJECTED"):
            check_receipt(config, config.owner_authority, purpose, value, datetime.now(UTC))


def test_v1_journal_codec_and_projection_head_are_preserved(tmp_path):
    path = tmp_path / "custody.jsonl"
    first = journal.append(path, "OPEN", {"version": 1}, at="2026-10-25T00:00:00+00:00",
                           expected_head="0" * 64, create=True)
    second = journal.append(path, "STOP", {}, at="2026-10-25T00:01:00+00:00",
                            expected_head=first)
    rows, head = journal.read(path)
    assert head == second
    assert rows[1]["previous"] == first
    with pytest.raises(ValueError, match="WINDOW_JOURNAL_RECONCILIATION_REQUIRED"):
        journal.append(path, "OPEN", {}, at="unused", expected_head=first)
    assert journal.read(path) == (rows, head)


def test_capture_signature_survives_restart_without_a_plaintext_archive(config):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from companion_delivery_protocol.authority import capture_signing_payload
    from companion_onebot_bridge.event_normalizer import normalize
    from tests.support.onebot_events import raw_event

    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        received = runtime.clock()
        frame = json.dumps(raw_event(text="private input", time=int(received.timestamp()),
            self_id=67890, user_id=12345, sender={"user_id": 12345}))
        event = normalize(frame, config, now=received.timestamp())
        assert runtime.persist_capture(event, frame, received_at=received, peer=("127.0.0.1", 3001))
        assert not runtime.persist_capture(event, frame, received_at=received,
                                           peer=("127.0.0.1", 3001))
        receipt = json.loads(spool.db.execute(
            "SELECT capture_json FROM window_inputs").fetchone()[0])
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(config.capture_public_key)).verify(
            bytes.fromhex(receipt["signature"]), capture_signing_payload(unsigned))
        assert "private input" not in runtime.path.read_text("utf-8")
        assert "private input" not in json.dumps(receipt)
        with spool.db:
            spool.db.execute("UPDATE inbox SET state='accepted',body=NULL")
        spool.remove_capture_text(event["event_id"])
    with Spool(config) as spool:
        OwnerRuntime(spool).verify()
        assert spool.db.execute("SELECT body FROM inbox").fetchone()[0] is None


@pytest.mark.parametrize("mutation", ["input_hash", "approval_hash", "body"])
def test_capture_projection_mutation_blocks_upload(config, mutation):
    from companion_onebot_bridge.event_normalizer import normalize
    from tests.support.onebot_events import raw_event

    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        received = runtime.clock()
        frame = json.dumps(raw_event(text="original", time=int(received.timestamp()),
            self_id=67890, user_id=12345, sender={"user_id": 12345}))
        event = normalize(frame, config, now=received.timestamp())
        runtime.persist_capture(event, frame, received_at=received, peer=("127.0.0.1", 3001))
        if mutation == "body":
            event["payload"]["text"] = "changed"
            spool.db.execute("UPDATE inbox SET body=?", (json.dumps(event),))
        else:
            spool.db.execute(f"UPDATE window_inputs SET {mutation}=?", ("f" * 64,))
        with pytest.raises(ValueError, match="OWNER_CAPTURE_PROJECTION_CHANGED"):
            asyncio.run(runtime.flush_one(None))
        assert spool.db.execute("SELECT state FROM inbox").fetchone()[0] == "pending"


@pytest.mark.parametrize("interrupt", [False, True])
def test_checked_dispatch_rechecks_stop_and_retains_uncertainty(config, interrupt):
    from companion_onebot_bridge.network import CheckedTransport, PrivateDestination

    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        expiry = (runtime.clock() + timedelta(minutes=1)).isoformat()
        item = {"authority": runtime.authority, "message_id": "msg_" + "a" * 32,
            "lease_token": "lease_" + "b" * 32, "lease_until": expiry,
            "origin_event_id": "evt_" + "c" * 64, "action_type": "qq.private.reply",
            "content": {"text": "hello"}, "reply_revision": 1}
        calls = []

        def core(request):
            assert request.url.path == "/internal/v1/owner-authority/send-check"
            assert json.loads(request.content)["content_sha256"] == journal.digest(item["content"])
            if interrupt:
                runtime.stop()
            return httpx.Response(200, json={"approved": True, "lease_until": expiry})

        def onebot(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        async def run():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                                        transport=httpx.MockTransport(core)) as client:
                guard = OwnerSendGuard(runtime, client)
                guard.reserve(item)
                spool.start_delivery(item)
                transport = CheckedTransport(httpx.MockTransport(onebot), guard.before_dispatch)
                async with httpx.AsyncClient(transport=transport) as protocol:
                    request = protocol.build_request("POST", "http://127.0.0.1:3000/send_private_msg",
                        headers={"Host": "snowluma:3000", "Authorization": "Bearer " + "o" * 32},
                        json={"user_id": 12345, "message": [
                            {"type": "text", "data": {"text": "hello"}}], "auto_escape": True})
                    request.extensions.update(sni_hostname="snowluma",
                        companion_private_destination=PrivateDestination(
                            "snowluma:3000", "http", "snowluma", 3000, "127.0.0.1"))
                    if interrupt:
                        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                            await protocol.send(request)
                    else:
                        await protocol.send(request)
                        assert guard.dispatched
                guard.complete(item, None)

        asyncio.run(run())
        assert calls == ([] if interrupt else ["/send_private_msg"])
        assert spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == "UNKNOWN"
        assert json.loads(spool.db.execute("SELECT body FROM results").fetchone()[0])[
            "result"] == "DELIVERY_UNCERTAIN"
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        runtime.verify(active=False)
        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
            OwnerSendGuard(runtime, None).reserve(item)


def _replays(runtime, monkeypatch):
    calls = []
    original = runtime._replay

    def counted(row):
        calls.append(row)
        return original(row)

    monkeypatch.setattr(runtime, "_replay", counted)
    return calls


def test_unchanged_custody_reuses_the_last_full_verification(config, monkeypatch):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        calls = _replays(runtime, monkeypatch)
        # Construction already completed one full verification of this custody.
        first = runtime.verify()
        second = runtime.verify()
        assert calls == []
        assert first == second and first is not second
        first.append("caller-owned list")
        assert runtime.verify() == second


def test_any_spool_write_or_journal_append_forces_a_full_replay(config, monkeypatch):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        calls = _replays(runtime, monkeypatch)
        runtime.verify()
        assert calls == []
        spool.db.execute("CREATE TABLE IF NOT EXISTS verify_cache_probe(x)")
        runtime.verify()
        runtime.verify()
        assert len(calls) == 1
        with spool.db:
            runtime.append("OWNER_STOPPED", {"authority": runtime.authority})
            spool.db.execute("UPDATE owner_runtime SET state='STOPPED' WHERE id=1")
        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
            runtime.verify()
        assert runtime.verify(active=False)[-1]["kind"] == "OWNER_STOPPED"


@pytest.mark.parametrize("change", ["journal_tamper", "other_connection", "stop_marker"])
def test_cached_proof_never_hides_out_of_band_changes(config, change):
    import sqlite3

    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        runtime.verify()
        if change == "journal_tamper":
            raw = runtime.path.read_bytes()
            runtime.path.write_bytes(raw.replace(b'"OWNER_BOUND"', b'"OWNER_BOUNX"'))
            expected = "OWNER_JOURNAL_RECONCILIATION_REQUIRED|INVALID_WINDOW_JOURNAL"
        elif change == "other_connection":
            with sqlite3.connect(spool.directory / "spool.sqlite3") as other:
                other.execute("UPDATE owner_runtime SET journal_head=? WHERE id=1", ("e" * 64,))
            expected = "OWNER_JOURNAL_RECONCILIATION_REQUIRED"
        else:
            journal.stop_marker(runtime.stop_path)
            expected = "OWNER_RUNTIME_STOPPED"
        with pytest.raises(ValueError, match=expected):
            runtime.verify()
