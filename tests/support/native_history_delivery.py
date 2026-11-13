import asyncio
import json
import pickle
import sys
from pathlib import Path

from tests.sdk_reader_fixture import answer


async def sdk_step(core, tmp_path, event_id, index, reply="原始历史答复"):
    job = core.store.one('SELECT job_id FROM jobs WHERE event_id=%s', (event_id,))
    config, output = tmp_path / f"history-{index}.pickle", tmp_path / f"history-{index}.json"
    config.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
        [answer(reply)], "SUCCEEDED", str(output))))
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).parents[1] / "reader_sdk_worker.py"), str(config),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        logs, _ = await asyncio.wait_for(process.communicate(), 45)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, logs.decode(errors="replace")
    requests = json.loads(output.read_text(encoding="utf-8"))
    assert len(requests) == 1
    return job["job_id"], requests[0]
