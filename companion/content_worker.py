import asyncio
import random

from companion import runtime_config, shutdown
from companion.content import ContentService
from companion.observation import ObservationService
from companion.runtime import Runtime
from companion.worker_schedule import ClaimBusy, run_native_unit
from companion.worker_wakeup import Wakeup


async def content_worker(settings):
    with Runtime(settings) as runtime:
        runtime.store.configuration_worker = "content-worker"
        observations = ObservationService(runtime.store, settings, runtime.clock)
        content = ContentService(runtime.store, settings, observations, clock=runtime.clock)
        with Wakeup(runtime.store, settings, "content-worker", runtime.clock) as wake:
            while not shutdown.requested():
                if not await runtime_config.worker_boundary(runtime.store, settings, runtime.clock):
                    break
                scan = wake.before_scan()
                try:
                    result = await run_native_unit(content.run_batch)
                except ClaimBusy:
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                    continue
                if not result.progressed:
                    await wake.wait(scan, result)
