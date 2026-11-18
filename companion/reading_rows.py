"""Stored request and attempt evidence for exploration and reading."""


def exploration_by_request_id(store, request_id):
    return store.one(
        "SELECT request_id, request_key, adapter_id, operation, url, goal_step_json, state, "
        "lease_token, lease_until, captured_privacy_epoch, reserved_bytes, used_bytes, "
        "result_sha256, last_error_code, created_at, started_at, updated_at, redacted, "
        "reserved_requests, used_requests, source_grant_id, source_grant_epoch, authority_json, "
        "query, discovery_mode FROM exploration_requests WHERE request_id = %s",
        (request_id,),
    )


def attempt_by_request_id(store, request_id):
    return store.one(
        "SELECT request_id, adapter_id, lease_token, lease_until, context_json, "
        "allowed_hosts_json, original_url_sha256, source_grant_json, state, result_sha256, "
        "result_event_ids_json, observed_requests, observed_bytes, created_at, updated_at FROM "
        "reading_attempts WHERE request_id = %s",
        (request_id,),
    )
