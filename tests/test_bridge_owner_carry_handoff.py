"""Stopped native custody carries model liability without declaring it terminal."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

import companion_delivery_protocol as wire
from companion_onebot_bridge import journal, owner_handoff
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool, encode
from tests.support.bridge_owner_carry_handoff import (
    capture,
    client,
    handler,
    prepare,
    proof,
    seal,
    state,
    stopped,
)
from tests.support.bridge_owner_resolution import reserve_uncertain
from tests.support.bridge_owner_runtime import config as config


def test_carry_is_distinct_from_terminal_and_preserves_original_sources(config):
    original = capture(config)
    second = capture(config, 2)
    retained = proof(original)
    runtime = stopped(config)
    calls = []
    with runtime.spool:
        prefix = runtime.verify(active=False)
        inputs = owner_handoff._rows(runtime.db, "SELECT * FROM window_inputs ORDER BY event_id")

        async def run():
            async with client(handler(config, retained, calls=calls,
                    states={second["event_id"]: state(second, terminal=True)})) as core:
                with pytest.raises(ValueError, match="OWNER_INPUT_TERMINAL_REQUIRED"):
                    await owner_handoff.prepare(runtime, core)
                result = await prepare(runtime, core)
                assert result["state"] == "CARRY_HANDOFF_PREPARED"
                head = runtime.row()["journal_head"]
                assert (await prepare(runtime, core))["journal_head"] == head
                successor = config.owner_authority | {"revision": 3, "policy_sha256": "c" * 64}
                assert (await owner_handoff.handoff(runtime, core, successor))["state"] == "ACTIVE"
                return successor
        successor = asyncio.run(run())
        records = runtime.verify(active=False) if runtime.authority == successor else journal.read(
            runtime.path)[0]
        assert records[:len(prefix)] == prefix
        carried = [r["data"] for r in records if r["kind"] == "OWNER_INPUT_CARRIED"]
        assert len(carried) == 1 and carried[0]["input_state"] == state(original)
        assert carried[0]["carry_receipt"] == retained
        assert [r["data"]["event_id"] for r in records if r["kind"] == "OWNER_INPUT_TERMINAL"] == [
            second["event_id"]]
        assert owner_handoff._rows(runtime.db,
                                   "SELECT * FROM window_inputs ORDER BY event_id") == inputs
        assert {tuple(row) for row in runtime.db.execute("SELECT state,body FROM inbox")} == {
            ("closed", None)}
        assert not runtime.db.execute("SELECT 1 FROM window_attempts").fetchone()
        old_stop = runtime.stop_path
    with Spool(replace(config, owner_authority=successor)) as spool:
        reopened = OwnerRuntime(spool)
        reopened.verify()
        assert old_stop.read_bytes() == b"OWNER_STOP\n" and not reopened.stop_path.exists()
        assert not asyncio.run(reopened.flush_one(None))
    with Spool(config) as spool:
        with pytest.raises(ValueError, match="OWNER_AUTHORITY_BINDING_CHANGED"):
            OwnerRuntime(spool)
    assert set(calls) == {"/internal/v1/owner-authority/" + path for path in (
        "stop-receipt", "model-carry-receipt", "inputs/state", "check")}


@pytest.mark.parametrize("field,value", [
    ("terminal", True), ("unsettled_model", False), ("unsettled_work", True),
    ("unsettled_delivery", True), ("work_sha256", "2" * 64), ("reply_revision", True),
])
def test_carry_refuses_changed_or_other_unsettled_work(config, field, value):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        before = runtime.row()["journal_head"]

        async def run():
            async with client(handler(config, proof(original),
                    states={original["event_id"]: state(original) | {field: value}})) as core:
                with pytest.raises(ValueError, match="OWNER_MODEL_CARRY_INPUT_CHANGED"):
                    await prepare(runtime, core)
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED" and runtime.row()["journal_head"] == before
        assert runtime.db.execute("SELECT state FROM inbox").fetchone()[0] == "pending"


@pytest.mark.parametrize("field,value", [
    ("authority", {"version": 2, "grant_id": "grant_" + "b" * 32, "revision": 1,
                   "policy_sha256": "d" * 64, "restore_generation": "initial"}),
    ("core_stop_sha256", "2" * 64), ("budget_journal_id", "budget_" + "2" * 32),
    ("carry_sha256", "2" * 64), ("successor_policy_sha256", "2" * 64),
    ("receipt_sha256", "2" * 64), ("version", True),
])
def test_carry_refuses_wrong_authenticated_lookup_binding(config, field, value):
    original = capture(config)
    bad = proof(original) | {field: value}
    if field != "receipt_sha256":
        bad = seal(bad)
    runtime = stopped(config)
    with runtime.spool:
        before = runtime.row()["journal_head"]

        async def run():
            async with client(handler(config, bad)) as core:
                with pytest.raises(ValueError, match="OWNER_MODEL_CARRY_"):
                    await prepare(runtime, core)
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED" and runtime.row()["journal_head"] == before


@pytest.mark.parametrize("path,nth", [
    ("stop-receipt", 1), ("model-carry-receipt", 1), ("inputs/state", 1),
    ("stop-receipt", 2), ("model-carry-receipt", 2),
])
def test_carry_rechecks_native_custody_after_every_await(config, path, nth):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        def mutate(actual, count, result):
            if actual.endswith("/" + path) and count == nth:
                with runtime.db:
                    runtime.db.execute("UPDATE inbox SET trace='synthetic changed trace'")
            return result

        async def run():
            async with client(handler(config, proof(original), mutate=mutate)) as core:
                with pytest.raises(ValueError, match="OWNER_HANDOFF_CUSTODY_CHANGED"):
                    await prepare(runtime, core)
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED" and runtime.stop_path.is_file()
        assert not any(r["kind"] == "OWNER_CARRY_HANDOFF_PREPARED"
                       for r in runtime.verify(active=False))


@pytest.mark.parametrize("path", ["stop-receipt", "model-carry-receipt"])
def test_changed_second_core_receipt_does_not_prepare(config, path):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        def mutate(actual, count, result):
            if actual.endswith("/" + path) and count == 2:
                result["core_stop_sha256"] = "2" * 64
                if path == "model-carry-receipt":
                    result = seal(result)
            return result

        async def run():
            async with client(handler(config, proof(original), mutate=mutate)) as core:
                with pytest.raises(ValueError, match="OWNER_HANDOFF_CUSTODY_CHANGED"):
                    await prepare(runtime, core)
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED"
        assert runtime.verify(active=False)[-1]["kind"] == "OWNER_INPUT_CARRIED"


@pytest.mark.parametrize("field,value", [
    ("revision", 5), ("grant_id", "grant_" + "2" * 32),
    ("restore_generation", "restored"), ("policy_sha256", "2" * 64),
])
def test_carry_cannot_rebind_to_a_different_successor(config, field, value):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        async def run():
            async with client(handler(config, proof(original))) as core:
                await prepare(runtime, core)
                before = runtime.row()["journal_head"]
                successor = config.owner_authority | {"revision": 3, "policy_sha256": "c" * 64}
                with pytest.raises(ValueError, match="OWNER_HANDOFF_BINDING_CHANGED"):
                    await owner_handoff.handoff(runtime, core, successor | {field: value})
                assert runtime.row()["journal_head"] == before
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED"


@pytest.mark.parametrize("interrupt", ["change", "lost", "cancel"])
def test_handoff_reauthenticates_carry_without_reopening_on_failure(config, interrupt):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        def mutate(path, count, result):
            if path.endswith("/model-carry-receipt") and count == 3:
                if interrupt == "lost":
                    raise httpx.ReadError("synthetic response loss")
                if interrupt == "cancel":
                    raise asyncio.CancelledError
                result = seal(result | {"record_sha256": "2" * 64})
            return result

        async def run():
            async with client(handler(config, proof(original), mutate=mutate)) as core:
                await prepare(runtime, core)
                before = runtime.row()["journal_head"]
                error = {"change": ValueError, "lost": httpx.ReadError,
                         "cancel": asyncio.CancelledError}[interrupt]
                with pytest.raises(error):
                    await owner_handoff.handoff(runtime, core,
                        config.owner_authority | {"revision": 3, "policy_sha256": "c" * 64})
                assert runtime.row()["journal_head"] == before
        asyncio.run(run())
        assert runtime.row()["state"] == "STOPPED" and runtime.stop_path.is_file()


def test_original_uncertain_delivery_still_requires_reconciliation(config):
    original = capture(config)
    reserve_uncertain(config)
    runtime = stopped(config)
    with runtime.spool:
        attempts = owner_handoff._rows(runtime.db, "SELECT * FROM window_attempts")
        results = owner_handoff._rows(runtime.db, "SELECT * FROM results")

        async def run():
            async with client(handler(config, proof(original))) as core:
                with pytest.raises(ValueError, match="OWNER_HANDOFF_RECONCILIATION_REQUIRED"):
                    await prepare(runtime, core)
        asyncio.run(run())
        assert owner_handoff._rows(runtime.db, "SELECT * FROM window_attempts") == attempts
        assert owner_handoff._rows(runtime.db, "SELECT * FROM results") == results
        assert runtime.row()["state"] == "STOPPED"


def test_carry_command_cannot_run_beside_original_process(config):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        with pytest.raises(RuntimeError, match="BRIDGE_ALREADY_RUNNING"):
            asyncio.run(owner_handoff.command(config, SimpleNamespace(
                command="owner-carry-handoff-prepare", carry_sha256=proof(original)["carry_sha256"],
                successor_policy_sha256="c" * 64)))


def test_later_ordinary_handoff_keeps_historical_carried_liability(config):
    original = capture(config)
    retained = proof(original)
    runtime = stopped(config)
    with runtime.spool:
        async def first():
            async with client(handler(config, retained)) as core:
                await prepare(runtime, core)
                return await owner_handoff.handoff(runtime, core,
                    config.owner_authority | {"revision": 3, "policy_sha256": "c" * 64})
        successor = asyncio.run(first())["authority"]
    next_config = replace(config, owner_authority=successor)
    runtime = stopped(next_config)
    with runtime.spool:
        prefix = runtime.verify(active=False)
        calls = []

        async def second():
            async with client(handler(next_config, retained, calls=calls)) as core:
                assert (await owner_handoff.prepare(runtime, core))["state"] == "HANDOFF_PREPARED"
                return await owner_handoff.handoff(runtime, core, successor | {"revision": 5})
        final = asyncio.run(second())["authority"]
    with Spool(replace(config, owner_authority=final)) as spool:
        new = OwnerRuntime(spool)
        records = new.verify()
        assert records[:len(prefix)] == prefix
        assert sum(r["kind"] == "OWNER_INPUT_CARRIED" for r in records) == 1
        assert not any(r["kind"] == "OWNER_INPUT_TERMINAL" for r in records)
        assert not asyncio.run(new.flush_one(None))
    assert not any(path.endswith(("/inputs/state", "/model-carry-receipt")) for path in calls)


@pytest.mark.parametrize("damage", ["carried_version", "prepared_version", "state", "receipt",
                                    "terminal_path", "missing_carried", "duplicate_carried"])
def test_journal_reader_distinguishes_both_paths_and_rejects_changed_evidence(config, damage):
    original = capture(config)
    runtime = stopped(config)
    with runtime.spool:
        async def run():
            async with client(handler(config, proof(original))) as core:
                await prepare(runtime, core)
        asyncio.run(run())
        records = runtime.verify(active=False)
        retained = next(r for r in records if r["kind"] == "OWNER_INPUT_CARRIED")
        prepared = records[-1]
        if damage == "carried_version":
            retained["data"]["version"] = 2
        elif damage == "prepared_version":
            prepared["data"]["version"] = 2
        elif damage == "state":
            retained["data"]["input_state"]["terminal"] = True
        elif damage == "receipt":
            prepared["data"]["carry_receipt"] = seal(
                prepared["data"]["carry_receipt"] | {"record_sha256": "2" * 64})
        elif damage == "terminal_path":
            prepared["kind"] = "OWNER_HANDOFF_PREPARED"
            prepared["data"] = {k: prepared["data"][k] for k in (
                "authority", "core_stop", "custody_sha256")}
        elif damage == "missing_carried":
            records.remove(retained)
        else:
            records.insert(-1, copy.deepcopy(retained))
        previous = "0" * 64
        for index, record in enumerate(records, 1):
            record.update(seq=index, previous=previous)
            previous = wire.digest(record)
        runtime.path.write_text("".join(encode(r) + "\n" for r in records),
                                encoding="utf-8", newline="\n")
        with runtime.db:
            runtime.db.execute("UPDATE owner_runtime SET journal_head=?", (previous,))
        with pytest.raises(ValueError, match="OWNER_(MODEL_CARRY|HANDOFF|INPUT_TERMINAL)"):
            runtime.verify(active=False)
        assert runtime.row()["state"] == "STOPPED"


@pytest.mark.parametrize("digest", [None, "short", "A" * 64])
def test_carry_requires_exact_digests_before_network(config, digest):
    capture(config)
    runtime = stopped(config)
    with runtime.spool:
        with pytest.raises(ValueError, match="OWNER_MODEL_CARRY_RECEIPT_INVALID"):
            asyncio.run(owner_handoff.prepare_carry(runtime, None, digest, "c" * 64))


def test_carry_manifest_cannot_add_an_uncaptured_input(config):
    original = capture(config)
    retained = proof(original)
    retained["inputs"][0]["event_id"] = "evt_" + "f" * 64
    runtime = stopped(config)
    with runtime.spool:
        before = runtime.row()["journal_head"]

        async def run():
            async with client(handler(config, seal(retained))) as core:
                with pytest.raises(ValueError, match="OWNER_MODEL_CARRY_INPUT_CHANGED"):
                    await prepare(runtime, core)
        asyncio.run(run())
        assert runtime.row()["journal_head"] == before
