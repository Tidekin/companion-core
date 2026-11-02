from companion import runtime_config, shutdown
from companion.executive import Executive
from companion.runtime import Runtime
from companion.worker_schedule import run_native_unit
from companion.worker_wakeup import Wakeup


async def executive_worker(settings):
    with Runtime(settings) as runtime:
        runtime.store.configuration_worker = "executive-worker"
        executive = Executive(runtime.store, settings, runtime.clock)
        with Wakeup(runtime.store, settings, "executive-worker", runtime.clock) as wake:
            while not shutdown.requested():
                if not await runtime_config.worker_boundary(runtime.store, settings, runtime.clock):
                    break
                scan = wake.before_scan()
                result = await run_native_unit(executive.run_one)
                if not result.progressed:
                    await wake.wait(scan, result)
