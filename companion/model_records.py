"""Persisted evidence for one model request in the caller's database session."""


def admission_by_request_id(store, request_id):
    return store.one(
        "SELECT model_admissions.request_id AS request_id, "
        "model_admissions.logical_request_id AS logical_request_id, "
        "model_admissions.attempt_id AS attempt_id, model_admissions.attempt_number AS "
        "attempt_number, model_admissions.window_id AS window_id, "
        "model_admissions.contract_sha256 AS contract_sha256, model_admissions.payload_sha256 "
        "AS payload_sha256, model_admissions.snapshot_json AS snapshot_json, "
        "model_admissions.reserved_micro_usd AS reserved_micro_usd, "
        "model_admissions.input_bound AS input_bound, model_admissions.output_bound AS "
        "output_bound, model_admissions.lease_token AS lease_token, "
        "model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS "
        "io_started_at, model_admissions.journal_path AS journal_path, "
        "model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS "
        "measurement_json, model_admissions.authority_json AS authority_json FROM "
        "model_admissions AS model_admissions WHERE model_admissions.request_id = "
        "%s",
        (request_id,),
    )


def usage_by_request_id(store, request_id):
    return store.one(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.request_id = %s",
        (request_id,),
    )
