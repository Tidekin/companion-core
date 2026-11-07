from companion import owner_input
from tests.support.owner_input import captured, inbound


def chat(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    result = core.ingest(inbound(core, capture), "runtime-source")
    return capture, core.claim_job("owner-runtime-test", job_id=result["job_id"])
