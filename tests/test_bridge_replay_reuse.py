"""Owner replay reuses only work whose exact inputs are unchanged."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from companion_onebot_bridge import journal, owner_handoff, profile_probe
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.bridge_journal_cache import chain, write
from tests.support.bridge_owner_handoff import core_handler
from tests.support.bridge_owner_runtime import config as config


@pytest.fixture(autouse=True)
def empty_cache():
    journal._clear()
    yield
    journal._clear()


def counted_decode(monkeypatch):
    calls = []
    original = journal.decode

    def decode(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(journal, "decode", decode)
    return calls


def test_an_append_decodes_only_the_new_lines_and_equals_a_full_read(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    raw, _, head = chain({"value": 1}, {"value": 2})
    write(path, raw)
    decodes = counted_decode(monkeypatch)
    journal.read(path)
    journal.append(path, "NEXT", {"value": 3}, at="later", expected_head=head)
    extended = journal.read(path)
    assert len(decodes) == 3  # the two original lines, then only the appended one
    journal._clear()
    assert journal.read(path) == extended and len(extended[0]) == 3


@pytest.mark.parametrize("change", ["prefix", "seq", "previous"])
def test_a_changed_prefix_or_broken_tail_is_never_extended(tmp_path, change):
    path = tmp_path / "journal.jsonl"
    raw, _, head = chain({"value": "old"})
    write(path, raw)
    journal.read(path)
    tail = {"seq": 2, "previous": head, "at": "later", "kind": "NEXT", "data": {}}
    if change == "seq":
        tail["seq"] = 3
    elif change == "previous":
        tail["previous"] = "f" * 64
    grown = (raw.replace(b'"old"', b'"new"') if change == "prefix" else raw) + (
        journal.encode(tail) + "\n").encode()
    path.write_bytes(grown)
    with pytest.raises(ValueError, match="INVALID_WINDOW_JOURNAL"):
        journal.read(path)
    assert journal._previous is None


def test_a_prefix_without_a_final_line_feed_is_read_in_full(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    raw, _, head = chain({"value": 1})
    write(path, raw.replace(b"\n", b"\r"))
    journal.read(path)
    tail = {"seq": 2, "previous": head, "at": "later", "kind": "NEXT", "data": {}}
    path.write_bytes(raw.replace(b"\n", b"\r") + (journal.encode(tail) + "\r").encode())
    decodes = counted_decode(monkeypatch)
    records, _ = journal.read(path)
    assert len(records) == 2 and len(decodes) == 2


def profile(at):
    return {"profiles": [{"field": "nickname", "observed_at": at.isoformat(),
                          "value_sha256": "a" * 64, "receipt_sha256": "b" * 64}]}


def test_each_profile_record_shape_is_checked_once_per_runtime(config, monkeypatch):
    calls = []
    original = profile_probe.validate_record

    def validate(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(profile_probe, "validate_record", validate)
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        for _ in range(2):
            with runtime.db:
                runtime.append("PROFILE_PROBE_ACCEPTED", profile(runtime.clock()))
            runtime.verify()
        assert len(calls) == 2
        # A new runtime trusts nothing from the old one.
        assert len(OwnerRuntime(spool).verify()) == 3 and len(calls) == 4


def handed_off(config):
    with Spool(config) as spool:
        OwnerRuntime(spool).stop()
    successor = config.owner_authority | {"revision": 3, "policy_sha256": "e" * 64}
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))

        async def run():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(core_handler(config, successor))) as client:
                await owner_handoff.prepare(runtime, client)
                await owner_handoff.handoff(runtime, client, successor)
        asyncio.run(run())
    return replace(config, owner_authority=successor)


def test_reused_handoff_history_still_checks_new_historical_table_rows(config):
    successor = handed_off(config)
    with Spool(successor) as spool:
        runtime = OwnerRuntime(spool)
        runtime.verify()
        assert runtime._handoff_verified is not None
        spool.db.execute("CREATE TABLE automatic_media(event_id TEXT,plan_hash TEXT,state TEXT)")
        spool.db.execute("INSERT INTO automatic_media VALUES('event',?,'DONE')",
                         (journal.digest(config.owner_authority),))
        spool.db.commit()
        with pytest.raises(ValueError):
            runtime.verify()


def test_daily_appends_reuse_history_but_a_new_stop_replays_it(config, monkeypatch):
    successor = handed_off(config)
    with Spool(successor) as spool:
        runtime = OwnerRuntime(spool)
        runtime.verify()
        replayed = []
        original = owner_handoff._verify

        def verify(*args, **kwargs):
            replayed.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner_handoff, "_verify", verify)
        for _ in range(2):
            with runtime.db:
                runtime.append("PROFILE_PROBE_STARTED", {"authority": runtime.authority})
            runtime.verify()
        from companion_onebot_bridge.event_normalizer import normalize
        from companion_onebot_bridge.spool import encode
        from tests.support.onebot_events import raw_event

        now = runtime.clock()
        raw = encode(raw_event(2, text="current source", time=int(now.timestamp()),
            self_id=int(successor.bot_qq_id), user_id=int(successor.owner_qq_id),
            sender={"user_id": int(successor.owner_qq_id)}))
        event = normalize(raw, successor, now=now.timestamp())
        runtime.persist_capture(event, raw, received_at=now, peer=("127.0.0.1", 1))
        runtime.verify()
        assert replayed == []
        runtime.stop()
        runtime.verify(active=False)
        assert replayed


@pytest.mark.parametrize("damage", ["journal", "capture", "inbox"])
def test_historical_proof_changes_invalidate_warm_history(config, damage):
    from tests.support.bridge_owner_carry_handoff import (
        capture,
        client,
        handler,
        prepare,
        proof,
        stopped,
    )

    source = capture(config)
    retained = proof(source)
    runtime = stopped(config)
    successor = config.owner_authority | {"revision": 3, "policy_sha256": "c" * 64}

    async def handoff():
        async with client(handler(config, retained)) as core:
            await prepare(runtime, core)
            await owner_handoff.handoff(runtime, core, successor)

    with runtime.spool:
        asyncio.run(handoff())
    with Spool(replace(config, owner_authority=successor)) as spool:
        runtime = OwnerRuntime(spool)
        records = runtime.verify()
        assert runtime._handoff_verified is not None
        if damage == "journal":
            # Re-chain the edited prefix, so the cache must reject changed evidence
            # even when the generic codec accepts the resulting chain.
            original = next(r for r in records if r["kind"] == "OWNER_INPUT_CARRIED")
            original["data"]["carry_receipt"]["retained_micro_usd"] += 1
            previous = "0" * 64
            for record in records:
                record["previous"] = previous
                previous = journal.digest(record)
            runtime.path.write_text("".join(json.dumps(r) + "\n" for r in records),
                                    encoding="utf8")
            spool.db.execute("UPDATE owner_runtime SET journal_head=?", (previous,))
        elif damage == "capture":
            spool.db.execute("UPDATE window_inputs SET capture_json='{}'")
        else:
            # Attempts/rows are still readable, but the handoff's sealed inbox
            # custody has changed. This is not an authority/current-send decision.
            spool.db.execute("UPDATE inbox SET state='changed'")
        spool.db.commit()
        with pytest.raises((ValueError, KeyError)):
            runtime.verify()


@pytest.mark.parametrize("damage", ["result_body", "result_state", "missing_result"])
def test_historical_delivery_receipt_is_rechecked_after_cache_hit(config, damage):
    from companion_onebot_bridge.owner_send import OwnerSendGuard

    item = {"authority": config.owner_authority, "message_id": "msg_" + "a" * 32,
        "lease_token": "lease_" + "b" * 32,
        "lease_until": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "origin_event_id": "evt_" + "c" * 64, "action_type": "qq.private.proactive",
        "content": {"text": "synthetic acknowledgement"}}
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        guard = OwnerSendGuard(runtime, None)
        guard.reserve(item)
        spool.start_delivery(item)
        with spool.db:
            runtime.append("IO_STARTED", {"message_id": item["message_id"],
                                         "provider_ack_hash": None})
            spool.db.execute("UPDATE window_attempts SET state='IO_STARTED'")
        guard.dispatched = True
        guard.complete(item, "fixture-provider-id")
        spool.finish_delivery(item["message_id"], "fixture-provider-id")
        with spool.db:
            spool.db.execute("UPDATE results SET state='accepted'")
    successor = handed_off(config)
    with Spool(successor) as spool:
        runtime = OwnerRuntime(spool)
        runtime.verify()
        assert runtime._handoff_verified is not None
        with spool.db:
            if damage == "result_body":
                value = json.loads(spool.db.execute("SELECT body FROM results").fetchone()[0])
                value["provider_message_id"] = "different"
                spool.db.execute("UPDATE results SET body=?", (json.dumps(value),))
            elif damage == "result_state":
                spool.db.execute("UPDATE results SET state='pending'")
            else:
                spool.db.execute("DELETE FROM results")
        with pytest.raises(ValueError, match="OWNER_HANDOFF_RECONCILIATION_REQUIRED"):
            runtime.verify()
