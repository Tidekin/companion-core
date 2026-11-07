"""Run a finite synchronous operation with its own PostgreSQL session.

Only materialized values cross this boundary. Callers retain their original
transactions, authority checks and idempotency; no Core bootstrap is performed.
"""

import asyncio

from anyio import CancelScope, CapacityLimiter, to_thread

from companion.store import Store


class ApiStore:
    def __init__(self, database, state_directory):
        self.database, self.state_directory = database, state_directory
        self.limiter = CapacityLimiter(4)

    async def run(self, operation):
        def execute():
            store = Store(self.database, self.state_directory)
            try:
                return operation(store)
            finally:
                store.close()

        pending = asyncio.create_task(to_thread.run_sync(
            execute, abandon_on_cancel=False, limiter=self.limiter,
        ))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # As with Wakeup, join the submitted call before releasing its owner.
            # Shield also joins on repeated asyncio/ASGI shutdown cancellation.
            with CancelScope(shield=True):
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not pending.cancelled():
                    pending.exception()  # Retrieve late failure; keep original cancellation.
            raise
