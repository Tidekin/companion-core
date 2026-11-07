import asyncio
import json
import sys

from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.spool import encode
from tests.support.evaluation_process import process_config
from tests.support.onebot_events import raw_event


async def worker(core, tmp_path, candidate):
    config, output = tmp_path / "worker.json", tmp_path / "result.json"
    process_config(core, config)
    child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.native_meme_host_process", str(config), core.clock(), candidate, str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(40):
            _, error = await child.communicate()
        assert child.returncode == 0, error.decode()
        report = json.loads(output.read_text("utf-8"))
        assert report["worked"] and len(report["calls"]) == 1, report
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


def capture(owner, number=1):
    frame = encode(raw_event(number, time=int(owner.clock().timestamp()), text="太棒了，成功了"))
    event = normalize(frame, owner.config, now=owner.clock().timestamp())
    owner.persist_capture(event, frame, received_at=owner.clock(), peer=("127.0.0.1", 3001))
    return event
