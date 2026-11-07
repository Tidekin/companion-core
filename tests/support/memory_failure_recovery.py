from companion.memory_work import checkpoint_state
from tests.conftest import run
from tests.support.nightly_stages import staged


def failed_batch(core):
    model = staged(core)
    model.review = lambda data: '{"relations":"invalid"}'
    for _ in range(8):
        run(core.extract_step())
        record = checkpoint_state(core.store)
        if record["batch"].get("phase") == "FAILED":
            return model, record
        core.clock.advance(61)
    raise AssertionError("review did not exhaust its bounded attempts")
