"""Native history validation retains its fences while batching one write transaction."""

import json
from contextlib import nullcontext
from pathlib import Path

import httpx
import psycopg
import pytest

from companion import authority_custody, owner_authority, owner_input, paid_budget
from companion.contracts import CoreError
from companion.store import after, encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer, worker_for
from tests.support.native_consumer_entry import environment
from tests.support.native_history_delivery import sdk_step


async def native_history(core, tmp_path, check, *, turns=3, independent=False):
    async with environment(core, tmp_path, purposes=("chat", "history"), expires_in=30,
                           core_options={"max_recent_messages": 30},
                           paid_options={"daily_calls": 30}) as (
        entry, sends,
    ):
        def provider(request):
            return httpx.Response(200, json=answer("合成答复。"))

        async with worker_for(core, provider) as worker:
            for number in range(1, turns + 1):
                source = await entry.admit(number, "合成历史输入。")
                job_id = entry.accepted[source["event_id"]]["job_id"]
                assert await worker.run_once("history-transaction", job_id=job_id)
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                core.clock.advance(1)
            assert len(sends) == turns
            source = await entry.admit(turns + 1, "合成当前输入。")
            job = core.store.one((
                'SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
                'USING(event_id) WHERE event_id=%s'
            ), (source["event_id"],))
            expected = owner_input.history_sources(core.store, core.settings, core.clock, job)
            assert len(expected) == turns
            check(job, expected)
            if independent:
                job_id, wire = await sdk_step(core, tmp_path, source["event_id"], turns + 1)
                assert sum(message["role"] == "assistant" for message in wire["messages"]) == turns
                row = core.store.one((
                    'SELECT snapshots_json FROM agent_conversations WHERE '
                    'job_id=%s'
                ), (job_id,))
                history = next(item["proof"] for item in json.loads(row["snapshots_json"])
                               if item["kind"] == "history")
                assert history == owner_input.history_proof(expected)
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                assert len(sends) == turns + 1
                assert len(core.store.rows("SELECT 1 FROM model_usage WHERE state='SETTLED'")) == (
                    turns + 1)


@pytest.mark.parametrize("transactional", [True, False, "read_transaction"])
def test_history_reuses_custody_inside_one_owned_write_transaction(
    core, tmp_path, monkeypatch, transactional,
):
    def check(job, expected):
        calls, recalls = [], []
        native = authority_custody.check
        native_recall = owner_input.input_recall.require_not_recalled

        def custody(*args, **kwargs):
            calls.append(1)
            return native(*args, **kwargs)

        def recall(*args, **kwargs):
            recalls.append(1)
            return native_recall(*args, **kwargs)

        monkeypatch.setattr(authority_custody, "check", custody)
        monkeypatch.setattr(owner_input.input_recall, "require_not_recalled", recall)
        if transactional == "read_transaction":
            core.store.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                actual = owner_input.history_sources(core.store, core.settings, core.clock, job)
            finally:
                core.store.execute("ROLLBACK")
            # An unowned native transaction retains each source's live check,
            # plus the complete batch's initial and final custody checks.
            assert len(calls) == len(expected) + 2
        elif transactional:
            with core.store.transaction():
                actual = owner_input.history_sources(core.store, core.settings, core.clock, job)
            # READ COMMITTED also rechecks each source, unlike the owned snapshot.
            assert len(calls) == len(expected) + 2
        else:
            actual = owner_input.history_sources(core.store, core.settings, core.clock, job)
            assert len(calls) == 2
        assert actual == expected and len(recalls) == 3
        assert not core.store.in_transaction

    run(native_history(core, tmp_path, check))


@pytest.mark.parametrize("boundary", ["stop", "marker", "journal", "erasure", "expiry"])
@pytest.mark.parametrize("transactional", [True, False])
def test_transaction_batch_rejects_live_boundary_changes_before_returning(
    core, tmp_path, monkeypatch, boundary, transactional,
):
    def check(job, expected):
        original = owner_input.input_recall.require_not_recalled
        changed, saved = [], {}
        # An unowned batch reads one snapshot, so the marker comes from another commit.
        external = not transactional and boundary == "marker"
        other = (psycopg.connect(core.settings.database.get_secret_value(), autocommit=True)
                 if external else None)

        def recall(*args, **kwargs):
            original(*args, **kwargs)
            if changed:
                return
            changed.append(1)
            if boundary == "stop":
                core.settings.owner_runtime.mode = "OFF"
            elif boundary == "marker":
                (other or core.store).execute("UPDATE runtime_meta SET value=%s WHERE key=%s",
                                              (encode("OFF"), owner_authority.MODE))
            elif boundary == "expiry":
                core.clock.advance(31)
            else:
                path = Path(core.settings.paid_model.journal_path if boundary == "journal"
                            else core.settings.erasure_ledger)
                saved[path] = path.read_bytes()
                path.write_bytes(saved[path] + b"\n")

        monkeypatch.setattr(owner_input.input_recall, "require_not_recalled", recall)
        try:
            if external:
                # The snapshot answers for the state it began on; the next batch rejects.
                assert owner_input.history_sources(
                    core.store, core.settings, core.clock, job) == expected
                assert changed
            with pytest.raises(CoreError):
                with core.store.transaction() if transactional else nullcontext():
                    owner_input.history_sources(core.store, core.settings, core.clock, job)
            assert changed
        finally:
            for path, content in saved.items():
                path.write_bytes(content)
            if other is not None:
                other.close()

    run(native_history(core, tmp_path, check))


@pytest.mark.parametrize("mutation", ["signature", "redaction", "redaction_first", "future_send"])
@pytest.mark.parametrize("transactional", [True, False])
def test_transaction_history_still_checks_every_selected_source(
    core, tmp_path, monkeypatch, mutation, transactional,
):
    def check(job, expected):
        original = owner_input.input_recall.require_not_recalled
        target = expected[0 if mutation == "redaction_first" else 1]["event_id"]
        changed = []
        proof = owner_input.history_proof(expected)

        # A caller's write transaction sees its own change; an unowned batch reads one
        # snapshot, so the change comes from another connection's commit.
        other = None if transactional else psycopg.connect(
            core.settings.database.get_secret_value(), autocommit=True)
        execute = core.store.execute if transactional else other.execute

        def recall(*args, **kwargs):
            original(*args, **kwargs)
            if changed:
                return
            changed.append(1)
            if mutation in {"redaction", "redaction_first"}:
                execute("UPDATE events SET redacted=1 WHERE event_id=%s", (target,))
            elif mutation == "future_send":
                execute("UPDATE outbox SET sent_at=%s WHERE origin_event_id=%s",
                        (after(core.clock(), 100), target))
            else:
                key = owner_input.INPUT + target
                row = core.store.one('SELECT value FROM runtime_meta WHERE key=%s', (key,))
                value = json.loads(row["value"])
                value["capture"]["signature"] = "0" * 128
                execute("UPDATE runtime_meta SET value=%s WHERE key=%s", (encode(value), key))

        monkeypatch.setattr(owner_input.input_recall, "require_not_recalled", recall)
        try:
            if not transactional:
                # The snapshot answers for the state it began on; the commit is ordered
                # after this batch and the next batch rejects it.
                owner_input.check_history(core.store, core.settings, core.clock, job, proof)
                assert changed
            with pytest.raises(CoreError):
                with core.store.transaction() if transactional else nullcontext():
                    owner_input.check_history(core.store, core.settings, core.clock, job, proof)
        finally:
            if other is not None:
                other.close()
        assert changed

    run(native_history(core, tmp_path, check))


def test_full_history_batches_without_the_journal_byte_cache(core, tmp_path, monkeypatch):
    def check(job, expected):
        monkeypatch.setattr(paid_budget, "CACHE_MAX_BYTES", 1)
        calls, per_turn_reads, source_reads = [], [], []
        native = authority_custody.check
        native_one = core.store.one
        native_rows = core.store.rows

        def custody(*args, **kwargs):
            calls.append(1)
            return native(*args, **kwargs)

        def one(statement, parameters=()):
            if statement.startswith("SELECT sent_at FROM outbox") or (
                statement.startswith("SELECT 1 FROM runtime_meta")
                and parameters and str(parameters[0]).startswith(owner_input.INPUT)
            ):
                per_turn_reads.append(statement)
            return native_one(statement, parameters)

        def rows(statement, parameters=()):
            if "AS capture_receipt" in statement and "AS source_jobs" in statement:
                source_reads.append(len(parameters[0]))
            return native_rows(statement, parameters)

        monkeypatch.setattr(authority_custody, "check", custody)
        monkeypatch.setattr(core.store, "one", one)
        monkeypatch.setattr(core.store, "rows", rows)
        assert owner_input.history_sources(core.store, core.settings, core.clock, job) == expected
        assert len(calls) == 2
        assert per_turn_reads == []
        assert source_reads == [len(expected)]
        assert not core.store.in_transaction

    run(native_history(core, tmp_path, check, turns=15))


@pytest.mark.parametrize("max_messages", [0, 3])
def test_native_history_obeys_the_existing_recent_message_setting(core, tmp_path, max_messages):
    def check(job, expected):
        core.settings.max_recent_messages = max_messages
        count = max_messages // 2
        assert owner_input.history_sources(core.store, core.settings, core.clock, job) == (
            expected[-count:] if count else [])

    run(native_history(core, tmp_path, check))


def test_native_history_survives_real_http_and_an_independent_worker(core, tmp_path):
    run(native_history(core, tmp_path, lambda job, expected: None, independent=True))


def test_native_history_is_rechecked_after_the_model_await(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "history")) as (entry, sends):
            requests = []

            async def provider(request):
                requests.append(json.loads(request.content))
                assert not worker.store.in_transaction
                if len(requests) == 2:
                    # A separate live connection can change custody during model I/O.
                    core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s',
                                       (first["event_id"],))
                return httpx.Response(200, json=answer("合成答复。"))

            async with worker_for(core, provider) as worker:
                first = await entry.admit(1, "合成历史输入。")
                assert await worker.run_once("history-before-await")
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                core.clock.advance(1)
                current = await entry.admit(2, "合成当前输入。")
                assert await worker.run_once("history-after-await")
                assert not await worker.run_once("history-no-replay")
            assert len(requests) == 2 and len(sends) == 1
            assert any(message["role"] == "assistant" for message in requests[1]["messages"])
            assert core.store.one('SELECT state FROM jobs WHERE event_id=%s',
                                  (current["event_id"],))["state"] == "CANCELLED"
            assert not core.store.one('SELECT 1 FROM outbox WHERE origin_event_id=%s',
                                      (current["event_id"],))
            assert len(core.store.rows("SELECT 1 FROM model_usage WHERE state='SETTLED'")) == 2

    run(scenario())
