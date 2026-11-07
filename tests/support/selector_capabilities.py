import asyncio
import json
import os
import sys

from tests.support.evaluation_process import process_config


async def child(core, tmp_path, target, number):
    config, output = tmp_path / f"capability-{number}.json", tmp_path / f"result-{number}.json"
    process_config(core, config)
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.selector_capability_process", str(config), core.clock(), str(output), target,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, error.decode(errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["pid"] != os.getpid()
    return value
