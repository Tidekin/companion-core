from companion import mood_appraisals
from companion.contracts import InteractionSignal
from tests.conftest import ingest


def appraise(core, number, text, reason="OWNER_AFFECTION"):
    """A retained owner source and settled extraction, through the real record boundary."""
    core.settings.mood_enabled = True
    key = ingest(core, number, text)["event_id"]
    request_id = f"appraisal_{number}"
    core.store.insert(
        "model_usage", usage_id=f"usage_{request_id}", request_id=request_id,
        scenario="memory_extract", provider="synthetic", model="synthetic", state="SETTLED",
        input_tokens=100, output_tokens=20, occurred_at=core.clock(),
    )
    signals = mood_appraisals.validate(core.store, [
        InteractionSignal(event_id=key, reason=reason, quote=text),
    ], {key})
    with core.store.transaction():
        mood_appraisals.record(core.store, core.settings, signals, request_id, core.clock())
    return key
