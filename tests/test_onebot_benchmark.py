import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.spool import Spool
from scripts.benchmark_qq_beta import summarize
from tests.conftest import run
from tests.support.onebot import provider
from tests.support.onebot_delivery import core_client
from tests.support.onebot_events import config, normalized, raw_event


def test_100_offline_messages_measure_fsync_ingest_and_fifo_while_tools_are_busy(
    core,
    tmp_path,
    monkeypatch,
):
    records, sends = [], []
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    monkeypatch.setattr(
        "companion.telemetry.print",
        # Slow-lock reports go to stderr and are not stage metrics; a busy runner emits them.
        lambda line, file=None, **kw: file is None and records.append(json.loads(line)),
        raising=False,
    )
    metrics = Metrics("OFFLINE_FIXTURE", records.append)
    cfg = config(data_dir=str(tmp_path / "bridge"), delivery_enabled=True)

    async def scenario():
        # A deliberately unavailable optional executor never enters the ordinary chat path.
        cancelled = asyncio.Event()
        tool_task = asyncio.create_task(cancelled.wait())
        try:
            with Spool(cfg) as spool:
                async with (
                    core_client(core) as http,
                    httpx.AsyncClient(
                        base_url="http://onebot",
                        transport=httpx.MockTransport(lambda r: provider(r, sends)),
                    ) as protocol,
                ):
                    for number in range(1, 101):
                        received_ns = time.perf_counter_ns()
                        payload = normalized(raw_event(number, f"合成文字 {number} 🙂"))
                        assert spool.put(payload)
                        metrics.start(payload, received_ns)
                        metrics.emit(
                            "t_spool_commit",
                            payload["trace_id"],
                            elapsed_ms=(time.perf_counter_ns() - received_ns) / 1e6,
                        )
                        accepted = await spool.flush_one(http)
                        metrics.accepted(accepted)
                        assert await core.run_once("offline-benchmark")
                        assert await deliver_tick(
                            http, OneBotClient(cfg, protocol), spool, metrics=metrics
                        )
                        assert not spool.put(payload)
                        metrics.emit("duplicate_suppressed", payload["trace_id"])
                        assert not tool_task.done()
                    assert not await core.run_once("offline-benchmark")
        finally:
            tool_task.cancel()
            await asyncio.gather(tool_task, return_exceptions=True)

    run(scenario())
    assert len(sends) == 100
    for number, body in enumerate(sends, 1):
        assert body["message"][0]["data"]["text"].endswith(f"合成文字 {number} 🙂")
    assert len(core.model.calls) == 100
    assert all(scenario == "chat" for _, scenario in core.model.calls)
    assert core.store.one("SELECT COUNT(*) AS n FROM events")["n"] == 100
    assert core.store.one("SELECT COUNT(*) AS n FROM outbox WHERE state='SENT'")["n"] == 100
    states = {
        row["state"]: row["n"]
        for row in core.store.rows("SELECT state,COUNT(*) AS n FROM outbox GROUP BY state")
    }
    report = summarize(records, "OFFLINE_FIXTURE", delivery_states=states)
    assert report["unknown_delivery_count"] == 0
    assert report["latency"]["t_core_ingest_ack"]["samples"] == 100
    assert report["real_qq_private_scenarios_accepted"] == 0
    assert report["latency_real_p95_ms"] is None
    if directory := os.environ.get("P5_EVIDENCE_DIR"):
        destination = Path(directory).resolve()
        assert destination.is_relative_to(Path(__file__).resolve().parents[1])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "offline-100.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in records),
            encoding="utf-8",
        )
        (destination / "offline-delivery-states.json").write_text(
            json.dumps(states), encoding="utf-8"
        )


def test_summary_separates_evidence_and_uses_nearest_rank():
    rows = [
        {
            "schema_version": 1,
            "evidence_type": "OFFLINE_FIXTURE",
            "stage": "t_core_ingest_ack",
            "elapsed_ms": value,
        }
        for value in range(1, 101)
    ]
    report = summarize(rows, "OFFLINE_FIXTURE")
    assert report["latency"]["t_core_ingest_ack"] == {
        "samples": 100,
        "p50_ms": 50,
        "p95_ms": 95,
        "max_ms": 100,
    }
    assert summarize(rows, "LIVE_ONEBOT")["latency"] == {}
    assert summarize(rows, "OFFLINE_FIXTURE")["unknown_delivery_count"] is None
    with pytest.raises(ValueError, match="UNRECOGNIZED"):
        summarize([{"schema_version": 1, "private_text": "never emit me"}], "OFFLINE_FIXTURE")
