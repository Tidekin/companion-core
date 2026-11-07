"""Consumed native memory evidence fences provider I/O after asynchronous hooks."""

import asyncio
import json

import httpx
import pytest

from companion.store import after
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.native_recall_entry import native_recall, returned


@pytest.mark.parametrize("phase", ["followup_recall", "answer"])
@pytest.mark.parametrize("expiry", ["fact", "source"])
def test_expiry_during_request_hook_never_reaches_provider(core, tmp_path, phase, expiry):
    hooks = []

    async def hook(request):
        results = returned(json.loads(request.content))
        stage = "initial_recall" if not results else (
            "followup_recall" if len(results) == 1 else "answer")
        hooks.append(stage)
        if stage == phase:
            await asyncio.sleep(0)
            core.clock.advance(2)

    def provider(request, payload, number):
        body = answer("项目验收代号是青竹七号。") if number == 3 else answer(calls=[
            call(f"recall-{number}", "memory_recall", {"query": "*"})])
        return httpx.Response(200, json=body)

    async def scenario():
        async with native_recall(core, tmp_path, paid={"per_request_calls": 3},
                                 respond=provider, request_hook=hook) as value:
            if expiry == "fact":
                core.store.execute('UPDATE memories SET valid_to=%s WHERE memory_id=%s',
                                   (after(core.clock(), 1), value.memory_id))
            else:
                retained = after(core.clock(), -86400 * core.settings.raw_event_retention_days + 1)
                core.store.execute('UPDATE events SET ingested_at=%s WHERE event_id=%s',
                                   (retained, value.source["event_id"]))
            source = await value.capture()
            assert await value.worker.run_once("native-memory-expiry")
            assert phase in hooks
            assert len(value.calls) == (1 if phase == "followup_recall" else 2)
            assert core.store.one('SELECT state FROM jobs WHERE event_id=%s',
                                 (source["event_id"],)) == {"state": "CANCELLED"}
            assert not core.store.rows("SELECT * FROM outbox") and not value.sends
            # The blocked transport costs zero; earlier real provider responses stay settled.
            rows = core.store.rows(
                'SELECT state,billed_micro_usd FROM model_usage ORDER '
                'BY record_sequence'
            )
            assert len(rows) == len(value.calls) + 1
            assert all(row["state"] == "SETTLED" for row in rows)
            assert rows[-1]["billed_micro_usd"] == 0
            assert all(row["billed_micro_usd"] > 0 for row in rows[:-1])
            assert not await value.worker.run_once("no-expired-memory-replay")
    run(scenario())
