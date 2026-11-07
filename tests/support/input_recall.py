from companion.contracts import InboundEvent
from companion.input_recall import canonical_source, recall_input
from companion.store import after
from tests.support.onebot_events import config, normalized, raw_event


def source(number=42, **changes):
    return canonical_source(
        **{
            "owner_qq_id": "10001",
            "bot_qq_id": "10002",
            "protocol_epoch": config().protocol_epoch,
            "provider_message_id": str(number),
            **changes,
        }
    )


def incoming(core, number=42, text="我喜欢绿茶", *, event_id=None):
    value = normalized(raw_event(number, text))
    if event_id is not None:
        value["event_id"] = event_id
    event = InboundEvent.model_validate(value)
    core.ingest(event, event.event_id)
    return event.event_id


def recall(core, number=42, *, verified=True):
    return recall_input(
        core.store,
        core.ledger,
        source(number),
        core.clock(),
        retention_cutoff=after(core.clock(), -86400 * 30),
        verified=verified,
        occurred_at=core.clock(),
        event_id=normalized(raw_event(number))["event_id"] if verified else None,
    )
