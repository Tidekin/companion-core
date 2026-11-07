from companion.contracts import LeaseRequest
from companion.transport import lease_deliveries


def lease(core):
    return lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="test"), core.clock()
    )["items"]
