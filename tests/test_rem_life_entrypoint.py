"""Signed HTTP material reaches REM via the original independent Life selector."""

import asyncio
import json
import sys

from companion import rem
from tests.conftest import run
from tests.support.evaluation_process import process_config
from tests.test_rem_native import prepared, usage


def test_signed_http_input_reaches_rem_through_independent_life_worker(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path):
            assert core.store.rows("SELECT * FROM rem_records") == []
            before = core.store.rows("SELECT * FROM outbox")
            config = tmp_path / "life-config.json"
            process_config(core, config)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "tests.rem_life_process", "--config", str(config),
                "--now", core.clock(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(45):
                    output, error = await process.communicate()
                assert process.returncode == 0, (output, error)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            result = json.loads(output.decode().splitlines()[-1])
            turns = [unit for unit in result["units"] if unit["lane"] == "rem"]
            assert turns == [
                {"lane": "rem", "admissions": 0, "progressed": True},
                {"lane": "rem", "admissions": 1, "progressed": True},
            ]
            assert len(result["calls"]) == 1
            record = rem.records(core)[0]
            assert record["state"] == "SUCCEEDED" and record["kind"] == "FICTION"
            assert record["decision"] == "SKIP"
            assert len(usage(core)) == 1 and usage(core)[0]["state"] == "SETTLED"
            assert core.store.rows("SELECT * FROM outbox") == before
    run(scenario())
