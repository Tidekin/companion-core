"""The sole autonomous selector; native domains still own work and permission."""

import asyncio

import httpx

from companion import owner_authority, runtime_config, shutdown
from companion.autonomous_selector import native_selector
from companion.core import Core
from companion.model import ModelClient
from companion.store import uid
from companion.worker_schedule import UnitResult
from companion.worker_wakeup import Wakeup


async def life_worker(settings):
    async with httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False) as client:
        core = Core(settings, ModelClient(settings, client))
        worker_id = uid("life_worker")
        core.store.configuration_worker = "life-worker"
        try:
            selector = native_selector(core, worker_id)
            # Preserve the former Diary worker's startup custody recovery under
            # the single Life worker lock, including orphaned reservations.
            core.store.execute(
                "UPDATE model_usage SET state='UNKNOWN' WHERE scenario='diary' AND state='RESERVED'"
                + (
                    (
                        " AND request_id IN (SELECT request_id FROM "
                        "model_admissions WHERE lease_until<=%s)"
                    )
                    if owner_authority.required(core.store, settings)
                    else ""
                ),
                (core.clock(),) if owner_authority.required(core.store, settings) else (),
            )
            with Wakeup(core.store, settings, "life-worker", core.clock) as wake:
                scan, deadline = wake.before_scan(), None
                while not shutdown.requested():
                    if not await runtime_config.worker_boundary(core.store, settings, core.clock):
                        break
                    selected = await selector.step()
                    hint = selected.result.next_deadline
                    if hint is not None and hint >= scan.started_at:
                        deadline = min(deadline, hint) if deadline else hint
                    if selected.result.progressed:
                        scan, deadline = wake.before_scan(), None
                    elif not selected.more:
                        await wake.wait(
                            scan,
                            UnitResult(False, deadline),
                            selector.next_blocked_maintenance
                            if selected.blocked
                            else selector.next_maintenance,
                        )
                        selector.resume()
                        scan, deadline = wake.before_scan(), None
                    # A native step can complete synchronously. Give cancellation
                    # and newly arrived input a boundary before the next selection.
                    await asyncio.sleep(0)
        finally:
            core.close()
