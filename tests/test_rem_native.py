"""REM uses signed owner inputs, original jobs and accounting, with fake transport only."""

import asyncio
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from companion import rem
from companion.backup import backup, restore
from companion.core import Core
from companion.model import ModelClient
from tests.conftest import run
from tests.postgres_fixture import logical_state
from tests.support.core_http import real_core_http
from tests.support.evaluation_process import process_config
from tests.support.rem_native import FACT, prepared, response, usage

DREAM = {"decision": "DREAM", "title": "纸上的花园", "text": "虚构：配色化成了纸上的花园。",
         "questions": ["冷暖配色可以如何组合？"]}


def test_rem_explicit_off_and_empty_material_are_quiet(core, tmp_path):
    assert rem.schedule_due(core) is None

    async def scenario():
        async with prepared(core, tmp_path, seed=False):
            assert rem.schedule_due(core) is None
            assert core.store.rows("SELECT * FROM jobs WHERE kind='rem'") == []
            assert usage(core) == []
    run(scenario())


@pytest.mark.parametrize("draft", [DREAM, {"decision": "SKIP"}])
def test_rem_native_success_is_private_fiction_with_one_settlement(core, tmp_path, draft):
    calls = []
    core.settings.control_token = SecretStr("synthetic-rem-control-token")

    def provider(request):
        calls.append(json.loads(request.content))
        return response(draft)

    async def scenario():
        async with prepared(core, tmp_path):
            before = {table: core.store.rows(f"SELECT * FROM {table}") for table in (
                "memories", "observations", "diary_entries", "goals", "outbox")}
            job = rem.schedule_due(core)
            assert job is not None
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once(job_id=job)
                    assert not await worker.run_once(job_id=job)
                    record = rem.records(worker)[0]
            before_read = logical_state(core.store)
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
            ) as client:
                observed = await client.post("/internal/v2/cognition",
                    json={"kind": "rem", "record_id": record["rem_id"]},
                    headers={"Authorization": "Bearer synthetic-rem-control-token"})
            assert observed.status_code == 200, observed.text
            assert observed.headers["cache-control"] == "no-store"
            items = observed.json()["items"]
            assert len(items) == 1 and items[0]["id"] == record["rem_id"]
            item = items[0]
            assert item["kind"] == "FICTION" and item["decision"] == draft["decision"]
            assert item["questions"] == {"state": "UNVERIFIED",
                                         "count": len(draft.get("questions", []))}
            assert item["usage"] == record["usage"]
            assert item["source_available"] and item["audience"] == "owner_only"
            assert "title" not in item and "text" not in item
            assert all(secret not in observed.text for secret in (
                FACT, DREAM["title"], DREAM["text"], *DREAM["questions"]))
            assert logical_state(core.store) == before_read
            assert record["state"] == "SUCCEEDED"
            assert record["kind"] == "FICTION" and record["decision"] == draft["decision"]
            assert all(q["state"] == "UNVERIFIED" for q in record["questions"])
            assert record["source_event_ids"] and record["source_memory_ids"]
            assert len(calls) == 1 and "tools" not in calls[0]
            assert len(usage(core)) == 1 and usage(core)[0]["state"] == "SETTLED"
            assert usage(core)[0]["billed_micro_usd"] > 0
            assert rem.schedule_due(core) is None
            assert before == {table: core.store.rows(f"SELECT * FROM {table}") for table in before}
    run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "invalid", "withdraw"])
def test_rem_failure_never_replays_and_keeps_accounting(core, tmp_path, failure):
    calls = []

    def provider(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic uncertain response", request=request)
        if failure == "withdraw":
            mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
            core.memory.decide(mid, "forget", now=core.clock())
        return response({"invalid": True} if failure == "invalid" else DREAM)

    async def scenario():
        async with prepared(core, tmp_path):
            job = rem.schedule_due(core)
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once(job_id=job)
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as restarted:
                    assert not await restarted.run_once(job_id=job)
                    assert rem.schedule_due(restarted) is None
                    record = rem.records(restarted)[0]
            assert len(calls) == 1
            assert record["state"] in {"FAILED", "CANCELLED"}
            assert record["text"] == "" and record["questions"] == []
            assert len(usage(core)) == 1
            assert usage(core)[0]["state"] == ("UNKNOWN" if failure == "timeout" else "SETTLED")
            assert usage(core)[0]["billed_micro_usd"] > 0
            if failure == "withdraw":
                assert record["redacted"] and not record["source_available"]
    run(scenario())


@pytest.mark.parametrize("config", [
    {"max_input_tokens": 128}, {}, {},
])


def test_rem_input_limit_stays_hard_and_money_caps_are_advisory(core, tmp_path, config):
    calls = []

    def provider(request):
        calls.append(request)
        return response(DREAM)

    async def scenario():
        async with prepared(core, tmp_path, **config):
            job = rem.schedule_due(core)
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once(job_id=job)
            if "max_input_tokens" in config:
                assert not calls and not usage(core)
                assert rem.records(core)[0]["state"] == "FAILED"
            else:
                assert len(calls) == 1
                assert usage(core)[0]["state"] == "SETTLED"
                assert rem.records(core)[0]["state"] == "COMMITTED"
    run(scenario())


@pytest.mark.parametrize("crash", [False, True])
def test_rem_independent_process_restart_preserves_single_admission(core, tmp_path, crash):
    async def child(job, name, *, terminate=False):
        config = tmp_path / (name + ".json")
        log = tmp_path / (name + ".jsonl")
        process_config(core, config)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "tests.rem_process", "--config", str(config),
            "--now", core.clock(), "--job", job, "--log", str(log),
            *(["--crash"] if terminate else []), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(45):
                output, error = await process.communicate()
            assert process.returncode == (23 if terminate else 0), (output, error)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return log.read_text("utf-8").splitlines() if log.exists() else []

    async def scenario():
        async with prepared(core, tmp_path):
            job = rem.schedule_due(core)
            assert len(await child(job, "first", terminate=crash)) == 1
            if crash:
                assert usage(core)[0]["state"] == "RESERVED"
                core.clock.advance(core.settings.lease_seconds + 1)
            assert await child(job, "restart") == []
            assert len(usage(core)) == 1
            assert usage(core)[0]["state"] == ("UNKNOWN" if crash else "SETTLED")
            assert usage(core)[0]["billed_micro_usd"] > 0
            record = rem.records(core)[0]
            assert record["state"] == ("CANCELLED" if crash else "SUCCEEDED")
            assert rem.schedule_due(core) is None
    run(scenario())


def test_rem_retention_erases_only_derived_record_and_replays_after_restore(
    empty_database, core, tmp_path,
):
    async def scenario():
        async with prepared(core, tmp_path, record_retention_days=1):
            job = rem.schedule_due(core)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response(DREAM)),
            ) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once(job_id=job)
            sources = rem.records(core)[0]["source_event_ids"]
            archive = tmp_path / "before.enc"
            restored_state = tmp_path / "restored-state"
            key = Fernet.generate_key()
            backup(core.settings.database, archive, key)
            settled = usage(core)
            core.clock.advance(2 * 86400)
            core.expire_raw_events()
            restore(archive, empty_database, key, core.settings.erasure_ledger,
                    state_directory=restored_state)
            cfg = core.settings.model_copy(update={
                "database": empty_database, "state_directory": str(restored_state),
            })
            with Core(cfg, core.model, core.clock) as restored:
                for instance in (core, restored):
                    record = rem.records(instance)[0]
                    assert record["redacted"] and not record["text"]
                    assert record["source_event_ids"] == []
                    assert instance.store.one("SELECT content,status FROM memories") == {
                        "content": FACT, "status": "CONFIRMED"}
                    assert all(instance.store.one('SELECT redacted FROM events WHERE event_id=%s',
                               (source,))["redacted"] == 0 for source in sources)
                    assert usage(instance) == settled
    run(scenario())


def test_rem_signed_input_at_last_request_hook_prevents_provider_io(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        return response(DREAM)

    async def scenario():
        async with prepared(core, tmp_path) as (_, admit):
            job = rem.schedule_due(core)

            async def owner_arrives(request):
                await admit(2, "先回答我的问题", ingest_event=False)

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                event_hooks={"request": [owner_arrives]}) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once(job_id=job)
            assert calls == []
            assert len(usage(core)) == 1
            assert usage(core)[0]["state"] == "SETTLED"
            assert usage(core)[0]["billed_micro_usd"] == 0
            assert core.store.one((
                "SELECT 1 FROM audit_log WHERE action='model.not_dispatched' "
                'AND entity_id=%s'
            ), (usage(core)[0]["request_id"],))
            assert rem.records(core)[0]["text"] == ""
    run(scenario())


def test_rem_invalid_result_does_not_prevent_independent_diary(core, tmp_path):
    calls = []

    def provider(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if "memories" in payload:
            calls.append("rem")
            return response({"invalid": True})
        calls.append("diary")
        source = payload["sources"][0]
        return response({"schema_version": 1, "local_date": payload["local_date"],
            "title": "配色记录", "worth_saving": True, "sections": [{
                "kind": "observation", "text": source["text"], "evidence": [{
                    "source_id": source["source_id"], "exact_quote": source["text"]}]}]})

    async def scenario():
        async with prepared(core, tmp_path, seed=False, diary=True) as (_, admit):
            await admit(1, "今天讨论了青禾项目的配色。")
            await admit(2, "/记住这个 " + FACT)
            assert await core.run_once()
            day = datetime.fromisoformat(core.clock()).astimezone(
                ZoneInfo(core.settings.timezone)).date()
            job = rem.schedule_due(core)
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once(job_id=job)
                    assert rem.records(worker)[0]["state"] == "FAILED"
                    core.clock.advance(86400)
                    assert worker.diary.schedule_day(day)
                    assert await worker.diary.generate_once("independent-diary")
                    assert worker.diary.get_day(day, core.settings.owner_qq_id) is not None
            assert calls == ["rem", "diary"]
            assert all(row["state"] == "SETTLED" for row in core.store.rows(
                "SELECT state FROM model_usage"))
    run(scenario())
