from companion.growth import ExpressionLearning
from tests.conftest import ingest


def snapshot(core):
    return ExpressionLearning(core.store, core.settings, core.clock).snapshot(
        core.settings.owner_qq_id,
    )


def feedback_days(core, count=7):
    for index in range(count):
        if index:
            core.clock.advance(86400)
        ingest(core, index + 1, "简短一点")
