"""Stored owner-shared content used by readers, uploads and memory references."""


def content_item_by_id(store, item_id):
    return store.one(
        "SELECT item_id, owner_event_id, kind, slot, source_event_id, target_url, "
        "read_request_id, state, media_sha256, fingerprint, captured_privacy_epoch, "
        "reserved_bytes, usage_request_id, output_event_id, lease_token, lease_until, deadline, "
        "error_code, redacted, created_at, started_at, updated_at, attachment_json FROM "
        "content_items WHERE item_id = %s",
        (item_id,),
    )
