import asyncio
import json
import pickle
import sys
from pathlib import Path


async def sdk_step(core, tmp_path, job_id, replies, expected, name):
    config_path, output = tmp_path / (name + ".pickle"), tmp_path / (name + ".json")
    config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job_id,
                                         replies, expected, str(output))))
    process = await asyncio.create_subprocess_exec(sys.executable,
        str(Path(__file__).parents[1] / "reader_sdk_worker.py"), str(config_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        logs, _ = await asyncio.wait_for(process.communicate(), 45)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, logs.decode(errors="replace")
    return json.loads(output.read_text(encoding="utf-8"))
