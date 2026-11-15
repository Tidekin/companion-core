"""Fresh native worker; only the external model transport is simulated."""

import asyncio
import json
import os
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

from companion.autonomous_selector import native_selector
from tests.conftest import Clock
from tests.sdk_reader_fixture import Provider, worker_for


async def main():
    settings, now, job_id, responses, output = pickle.loads(
        await asyncio.to_thread(Path(sys.argv[1]).read_bytes)
    )
    assert settings.model_url == "https://synthetic.invalid/v1"
    clock = Clock()
    clock.now = now
    provider = Provider(*responses)
    async with worker_for(
        SimpleNamespace(settings=settings, clock=clock, store=None), provider
    ) as worker:
        if job_id:
            assert await worker.run_once("care-owner-chat", job_id=job_id)
        else:
            selector = native_selector(worker, "care-independent-selector")
            for _ in range(len(selector.lanes) * 4):
                result = await selector.step()
                if result.blocked or not result.more:
                    break
            else:
                raise AssertionError("care selector did not become idle")
        assert not provider.responses, worker.store.rows(
            "SELECT job_id,state,last_error_code FROM jobs"
        )
        result = {
            "pid": os.getpid(),
            "requests": provider.requests,
            "jobs": worker.store.rows("SELECT state,last_error_code FROM jobs"),
        }
    await asyncio.to_thread(
        Path(output).write_text, json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
