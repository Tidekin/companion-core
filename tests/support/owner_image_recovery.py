import asyncio
import json
import pickle
import sys
from pathlib import Path

from tests.sdk_reader_fixture import answer


async def run_worker(core, tmp_path, number, response):
    job = core.store.one("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1")
    config_path, output = tmp_path / f"recovery-{number}.pickle", tmp_path / f"wire-{number}.json"
    config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                                         [answer(response)], "SUCCEEDED", str(output))))
    child = await asyncio.create_subprocess_exec(sys.executable, "-B",
        str(Path(__file__).parents[1] / "reader_sdk_worker.py"), str(config_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        logs, _ = await asyncio.wait_for(child.communicate(), 45)
        assert child.returncode == 0, logs.decode(errors="replace")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    return json.loads(output.read_text(encoding="utf-8"))
