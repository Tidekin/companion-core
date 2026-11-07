"""Native mood sources reuse authority only inside a Core-owned read snapshot."""

import json
from contextlib import nullcontext
from pathlib import Path

import httpx
import psycopg
import pytest

from companion import owner_authority, owner_input, paid_budget
from companion.contracts import CoreError
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer, worker_for
from tests.support.native_consumer_entry import environment


async def native_mood(core, tmp_path, check):
    async with environment(core, tmp_path, purposes=("chat", "expression", "history"),
                           expires_in=120, core_options={"mood_enabled": True},
                           paid_options={"daily_calls": 30}) as (entry, sends):
        def provider(request):
            return httpx.Response(200, json=answer("合成答复。"))

        async with worker_for(core, provider) as worker:
            for number in range(1, 7):
                source = await entry.admit(number,
                    "一起看看这个问题" if number % 2 else "这个回答很有帮助")
                job_id = entry.accepted[source["event_id"]]["job_id"]
                assert await worker.run_once("mood-transaction", job_id=job_id)
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                core.clock.advance(1)
            assert len(sends) == 6
            expected = core.mood.snapshot()
            assert len(expected["signals"]) == 3
            check(expected)


@pytest.mark.parametrize("transactional", [True, False, "read_transaction"])
def test_mood_batches_authority_but_validates_every_source(core, tmp_path, monkeypatch,
                                                        transactional):
    def check(expected):
        calls, recalls = [], []
        native = paid_budget.journal
        native_recall = owner_input.input_recall.require_not_recalled

        def journal(*args, **kwargs):
            calls.append(1)
            return native(*args, **kwargs)

        def recall(*args, **kwargs):
            recalls.append(1)
            return native_recall(*args, **kwargs)

        monkeypatch.setattr(paid_budget, "journal", journal)
        monkeypatch.setattr(owner_input.input_recall, "require_not_recalled", recall)
        if transactional == "read_transaction":
            core.store.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                actual = core.mood.snapshot()
            finally:
                core.store.execute("ROLLBACK")
        else:
            with core.store.transaction() if transactional else nullcontext():
                actual = core.mood.snapshot()
        if transactional:
            # Two independent authority checks per source outside a Core read snapshot.
            assert len(calls) == 3 + 2 * len(recalls)
        else:
            assert len(calls) <= 4
        assert len(recalls) == 6
        assert actual == expected
        assert not core.store.in_transaction

    run(native_mood(core, tmp_path, check))


@pytest.mark.parametrize("boundary", ["stop", "marker", "journal", "erasure", "expiry",
                                      "redaction", "signature"])
@pytest.mark.parametrize("transactional", [True, False])
def test_mood_batch_rejects_changes_before_publishing(core, tmp_path, monkeypatch,
                                                   boundary, transactional):
    def check(expected):
        native = owner_input.input_recall.require_not_recalled
        changed, saved = [], {}
        # A caller's write transaction sees its own DB change; an unowned batch reads one
        # snapshot, so the DB change comes from another connection's commit.
        external = not transactional and boundary in {"marker", "redaction", "signature"}
        other = (psycopg.connect(core.settings.database.get_secret_value(), autocommit=True)
                 if external else None)
        execute = other.execute if external else core.store.execute

        def recall(*args, **kwargs):
            native(*args, **kwargs)
            if changed:
                return
            changed.append(1)
            if boundary == "stop":
                core.settings.owner_runtime.mode = "OFF"
            elif boundary == "marker":
                execute("UPDATE runtime_meta SET value=%s WHERE key=%s",
                        (encode("OFF"), owner_authority.MODE))
            elif boundary == "expiry":
                core.clock.advance(121)
            elif boundary in {"journal", "erasure"}:
                path = Path(core.settings.paid_model.journal_path if boundary == "journal"
                            else core.settings.erasure_ledger)
                saved[path] = path.read_bytes()
                path.write_bytes(saved[path] + b"\n")
            elif boundary == "redaction":
                execute("UPDATE events SET redacted=1 WHERE event_id=%s",
                        (expected["signals"][0]["event_id"],))
            else:
                key = owner_input.INPUT + expected["signals"][0]["event_id"]
                value = json.loads(core.store.one(
                    'SELECT value FROM runtime_meta WHERE key=%s', (key,))["value"])
                value["capture"]["signature"] = "0" * 128
                execute("UPDATE runtime_meta SET value=%s WHERE key=%s", (encode(value), key))

        monkeypatch.setattr(owner_input.input_recall, "require_not_recalled", recall)
        try:
            if external:
                # The snapshot answers for the state it began on; the next batch rejects.
                core.mood.revalidate(expected)
                assert changed
            with pytest.raises(CoreError):
                with core.store.transaction() if transactional else nullcontext():
                    core.mood.revalidate(expected)
            assert changed
        finally:
            for path, content in saved.items():
                path.write_bytes(content)
            if other is not None:
                other.close()

    run(native_mood(core, tmp_path, check))


@pytest.mark.parametrize("change", ["journal", "redaction"])
def test_mood_rechecks_mutable_batch_at_publication(core, tmp_path, monkeypatch, change):
    def check(expected):
        appraisals = core.mood._appraisals
        journal = Path(core.settings.paid_model.journal_path)
        original = journal.read_bytes()

        def collect(*args, **kwargs):
            signals = appraisals(*args, **kwargs)
            if change == "journal":
                journal.write_bytes(original + b"\n")
            else:
                core.store.execute(
                    "UPDATE events SET redacted=1 WHERE event_id=%s",
                    (expected["signals"][0]["event_id"],),
                )
            return signals

        monkeypatch.setattr(core.mood, "_appraisals", collect)
        try:
            with pytest.raises(CoreError), core.store.transaction():
                core.mood.snapshot()
        finally:
            journal.write_bytes(original)

    run(native_mood(core, tmp_path, check))
