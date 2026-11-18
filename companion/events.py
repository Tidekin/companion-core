"""Reads of the stored event, before caller-specific authority and source checks."""


def event_by_id(store, event_id):
    return store.one(
        "SELECT events.event_id AS event_id, events.schema_version AS "
        "schema_version, events.source AS source, events.source_event_id AS source_event_id, "
        "events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type "
        "AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, "
        "events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS "
        "trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, "
        "events.redacted AS redacted, events.memory_processed AS memory_processed, "
        "events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = "
        "%s",
        (event_id,),
    )
