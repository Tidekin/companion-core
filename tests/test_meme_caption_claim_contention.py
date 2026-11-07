"""Caption claims yield before I/O while retaining the original durable custody."""

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from companion.meme_core import MemeCaptionService
from companion.worker_lock import single_worker
from companion.worker_schedule import ClaimBusy
from tests.conftest import run
from tests.support.evaluation_process import process_config
from tests.test_meme_core import body, caption_environment

ROOT = Path(__file__).resolve().parents[1]


def custody(core):
    tables = (
        "events", "jobs", "job_sources", "meme_captions", "meme_assets",
        "meme_asset_sources", "meme_discovery_reservations", "model_usage",
        "model_admissions", "outbox", "owner_authority_revisions", "privacy_erasure_ledger",
    )
    paths = (core.settings.paid_model.journal_path, core.settings.erasure_ledger)
    return {
        "rows": {table: core.store.rows("SELECT * FROM " + table) for table in tables},
        "journals": {str(path): Path(path).read_bytes() if Path(path).exists() else None
                     for path in paths},
    }


@pytest.mark.parametrize("queued", [False, True])
def test_busy_caption_claim_preserves_idle_or_queued_custody(core, tmp_path, monkeypatch, queued):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            if queued:
                created = await client.post("/internal/v1/memes/captions",
                                            json=body(core, event, item, now))
                assert created.status_code == 200, created.text
            calls = []

            def model():
                calls.append(1)
                raise AssertionError("A contended claim must not access the provider")

            service = MemeCaptionService(core.store, core.settings, model, core.clock)
            core.store.db.execute("PRAGMA busy_timeout=1")
            before = custody(core)
            with closing(sqlite3.connect(core.settings.database, isolation_level=None)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                try:
                    with pytest.raises(ClaimBusy) as caught:
                        await service.run_once("contended-caption")
                    assert caught.value.__cause__.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
                    assert not core.store.db.in_transaction
                    assert custody(core) == before
                    assert calls == []
                finally:
                    writer.rollback()

    run(scenario())


def test_caption_claim_propagates_real_readonly_error(core):
    core.settings.meme_semantics_enabled = True
    core.store.db.execute("PRAGMA query_only=ON")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            run(core.memes.run_once("readonly-caption"))
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_READONLY
        assert not core.store.db.in_transaction
        assert not core.model.calls
    finally:
        core.store.db.execute("PRAGMA query_only=OFF")


@pytest.mark.parametrize("stop_while_busy", [False, True])
def test_http_caption_and_independent_worker_yield_or_stop_before_io(
    core, tmp_path, monkeypatch, stop_while_busy,
):
    async def scenario():
        async with caption_environment(core, tmp_path, monkeypatch) as values:
            event, _, client, _, _, now, item = values
            created = await client.post("/internal/v1/memes/captions",
                                        json=body(core, event, item, now))
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]
            config = tmp_path / "caption-worker.json"
            process_config(core, config)
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "tests.support.meme_caption_claim_worker",
                str(config), core.clock(), "stop" if stop_while_busy else "resume",
                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT),
                               "PYTHONDONTWRITEBYTECODE": "1", "COMPANION_TELEMETRY": "0"},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def phase(name):
                while line := await asyncio.wait_for(child.stdout.readline(), 45):
                    record = json.loads(line)
                    if record.get("phase") == name:
                        return record
                error = await child.stderr.read()
                raise AssertionError("worker exited before " + name + ": " + error.decode())

            try:
                ready = await phase("ready")
                assert ready["pid"] != os.getpid()
                before = custody(core)
                with closing(sqlite3.connect(core.settings.database,
                                             isolation_level=None)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    try:
                        child.stdin.write(b"continue\n")
                        await child.stdin.drain()
                        busy = await phase("busy")
                        assert busy["provider_calls"] == 0 and not busy["in_transaction"]
                        assert custody(core) == before
                    finally:
                        writer.rollback()
                finished = await phase("finished")
                output, error = await asyncio.wait_for(child.communicate(), 45)
                assert child.returncode == 0, (output + error).decode(errors="replace")
                assert finished["pid"] == ready["pid"] and finished["busy_claims"] >= 1
                assert finished["provider_calls"] == int(not stop_while_busy), core.store.rows(
                    "SELECT kind,state,last_error_code FROM jobs")
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            with single_worker(core.settings.database):
                pass  # The actual CLI's process lock was released normally.
            if stop_while_busy:
                assert custody(core) == before
            else:
                assert core.store.one("SELECT state,attempts FROM jobs WHERE job_id=?",
                                      (job_id,)) == {"state": "SUCCEEDED", "attempts": 1}
                assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
                assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())
