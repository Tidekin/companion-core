"""Validate frozen Gateway grants, request bindings and native spend limits."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from companion import cumulative, paid_budget
from companion.contracts import CoreError
from companion.gateway_config import GatewayConfig
from companion.gateway_contract import ClientGrant


def config():
    try:
        current = GatewayConfig.load()
    except (OSError, ValueError):
        raise CoreError("GATEWAY_CONFIGURATION_INVALID", 503) from None
    if not current.enabled:
        raise CoreError("GATEWAY_DISABLED", 503)
    return current


def restore_generation(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key = 'gateway_restore_generation'")
    return row["value"] if row else "initial"


def client_binding(row):
    return {name: row[name] for name in ("client_id", "key_sha256", "key_id", "revoked",
                                        "restore_generation", "journal_path")} | {
        "grant_sha256": paid_budget.sha(json.loads(row["grant_json"]))}


def validate_clients(store, clients, record=None):
    expected = dict(clients)
    if record and record.get("kind") == "gateway_client":
        proof = record["client"]
        expected[proof["client_id"]] = proof
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='gateway_clients' AND relkind IN "
        "('r','p')"):
        if expected:
            raise ValueError("missing gateway authority projection")
        return expected
    actual = {row["client_id"]: client_binding(row) for row in store.rows(
        'SELECT gateway_clients.client_id AS client_id, gateway_clients.key_sha256 AS key_sha256, '
        'gateway_clients.key_id AS key_id, gateway_clients.grant_json AS grant_json, '
        'gateway_clients.revoked AS revoked, gateway_clients.updated_at AS updated_at, '
        'gateway_clients.restore_generation AS restore_generation, gateway_clients.journal_path AS '
        'journal_path FROM gateway_clients AS gateway_clients')}
    if expected != actual:
        raise ValueError("gateway authority behind journal or DB")
    return expected


def require_passthrough(grant):
    if (grant.default_mode != "passthrough" or len(grant.modes) != 1
        or grant.modes[0].mode != "passthrough" or grant.owner_authority is not None):
        raise CoreError("GATEWAY_MODE_NOT_IMPLEMENTED", 403)


def require_reference(store, settings, grant, now):
    with nullcontext() if store.in_write_transaction else store.transaction():
        return _reference_locked(store, settings, grant, now)


def _reference_locked(store, settings, grant, now):
    cumulative.preflight(store, settings, now)
    paid_budget.contract(settings, now)
    if store.one("SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked', "
        "'model_budget_restore_required', 'cloud_budget_restore_required')"):
        raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
    paid_budget.journal(store, settings.paid_model, initialize=False)
    journal = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
    if (journal is None or grant.budget.budget_journal_id != journal["value"]
        or grant.budget.paid_contract_sha256 != paid_budget.sha(paid_budget.snapshot(settings))):
        raise CoreError("GATEWAY_BUDGET_REFERENCE_CHANGED", 403)


def model_alias(current, name):
    found = next((item for item in current.aliases if item.model_alias == name), None)
    if found is None:
        raise CoreError("GATEWAY_MODEL_NOT_ALLOWED", 404)
    return found


def binding(store, model_request_id):
    row = store.one('SELECT gateway_requests.request_id AS request_id, gateway_requests.client_id '
        'AS client_id, gateway_requests.model_request_id AS model_request_id, '
        'gateway_requests.idempotency_sha256 AS idempotency_sha256, gateway_requests.body_sha256 '
        'AS body_sha256, gateway_requests.frozen_json AS frozen_json, '
        'gateway_requests.request_cipher AS request_cipher, gateway_requests.response_cipher AS '
        'response_cipher, gateway_requests.state AS state, gateway_requests.created_at AS '
        'created_at, gateway_requests.expires_at AS expires_at, gateway_requests.deadline AS '
        'deadline, gateway_requests.lease_token AS lease_token, gateway_requests.lease_until AS '
        'lease_until, gateway_requests.error_code AS error_code, gateway_requests.error_status AS '
        'error_status FROM gateway_requests AS gateway_requests WHERE '
        'gateway_requests.model_request_id = %s', (model_request_id,))
    if row is None:
        raise CoreError("GATEWAY_REQUEST_BINDING_REQUIRED", 403)
    frozen = json.loads(row["frozen_json"])
    expected = {name: row[name] for name in ("client_id", "request_id", "created_at", "expires_at",
                                            "deadline", "idempotency_sha256")}
    if (any(frozen[name] != value for name, value in expected.items())
        or frozen["policy"]["grant"]["client_id"] != row["client_id"]
        or frozen["policy"]["request_sha256"] != row["body_sha256"]):
        raise CoreError("GATEWAY_REQUEST_BINDING_CHANGED", 503)
    return {"version": 1, "client_id": row["client_id"], "request_id": row["request_id"],
            "model_request_id": model_request_id, "frozen_sha256": paid_budget.sha(frozen)}


def validate_projection(store, bindings, record=None):
    """Budget Journal checks this projection, including on cached journal reads."""
    expected = dict(bindings)
    if record and "gateway" in record:
        if record["kind"] != "reserve" or record["request_id"] in expected:
            raise ValueError("duplicate gateway financial attribution")
        expected[record["request_id"]] = record["gateway"]
    rows = store.rows("SELECT request_id FROM model_usage WHERE scenario = 'gateway'")
    actual = {row["request_id"]: binding(store, row["request_id"]) for row in rows}
    if actual != expected:
        raise ValueError("gateway attribution behind journal or DB")
    return expected


def _quota(store, settings, grant, now, *, adding=0, estimate=0):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat(
        timespec="microseconds")
    daily = store.one('SELECT COUNT(*) AS calls, COALESCE(CAST(SUM(m.billed_micro_usd) AS BIGINT), '
        '0) AS amount FROM model_usage AS m JOIN gateway_requests AS g ON g.model_request_id = '
        'm.request_id WHERE g.client_id = %s AND m.occurred_at >= %s', (grant.client_id, day))
    running = store.one('SELECT COUNT(*) AS n FROM model_usage AS m JOIN gateway_requests AS g ON '
        "g.model_request_id = m.request_id WHERE g.client_id = %s AND m.state IN ('RESERVED', "
        "'UNKNOWN')", (grant.client_id,))["n"]
    if (daily["calls"] + adding > grant.budget.daily_calls
        or daily["amount"] + estimate > grant.budget.daily_micro_usd
        or running + adding > grant.budget.concurrency):
        raise CoreError("GATEWAY_CLIENT_BUDGET_EXHAUSTED", 429)


def check_model(store, settings, clock, model_request_id):
    current = config()
    row = store.one('SELECT gateway_requests.request_id AS request_id, gateway_requests.client_id '
        'AS client_id, gateway_requests.model_request_id AS model_request_id, '
        'gateway_requests.idempotency_sha256 AS idempotency_sha256, gateway_requests.body_sha256 '
        'AS body_sha256, gateway_requests.frozen_json AS frozen_json, '
        'gateway_requests.request_cipher AS request_cipher, gateway_requests.response_cipher AS '
        'response_cipher, gateway_requests.state AS state, gateway_requests.created_at AS '
        'created_at, gateway_requests.expires_at AS expires_at, gateway_requests.deadline AS '
        'deadline, gateway_requests.lease_token AS lease_token, gateway_requests.lease_until AS '
        'lease_until, gateway_requests.error_code AS error_code, gateway_requests.error_status AS '
        'error_status FROM gateway_requests AS gateway_requests WHERE '
        'gateway_requests.model_request_id = %s', (model_request_id,))
    if row is None or row["state"] != "RUNNING" or not row["lease_token"]:
        raise CoreError("GATEWAY_REQUEST_FENCED", 409)
    binding(store, model_request_id)
    frozen = json.loads(row["frozen_json"])["policy"]
    grant = ClientGrant.model_validate(frozen["grant"])
    client = store.one('SELECT gateway_clients.client_id AS client_id, gateway_clients.key_sha256 '
        'AS key_sha256, gateway_clients.key_id AS key_id, gateway_clients.grant_json AS '
        'grant_json, gateway_clients.revoked AS revoked, gateway_clients.updated_at AS updated_at, '
        'gateway_clients.restore_generation AS restore_generation, gateway_clients.journal_path AS '
        'journal_path FROM gateway_clients AS gateway_clients WHERE gateway_clients.client_id = '
        '%s', (row["client_id"],))
    if client is None or client["revoked"] or json.loads(client["grant_json"]) != frozen["grant"]:
        raise CoreError("GATEWAY_GRANT_CHANGED", 403)
    if client["restore_generation"] != restore_generation(store):
        raise CoreError("GATEWAY_GRANT_CHANGED", 403)
    require_passthrough(grant)
    if model_alias(current, frozen["alias"]["model_alias"]).model_dump() != frozen["alias"]:
        raise CoreError("GATEWAY_ALIAS_CHANGED", 403)
    now = clock()
    require_reference(store, settings, grant, now)
    deadline = min(datetime.fromisoformat(row["deadline"]),
                   datetime.fromisoformat(row["lease_until"]), grant.expires_at)
    if deadline <= datetime.fromisoformat(clock()):
        raise CoreError("GATEWAY_REQUEST_EXPIRED", 409)
    _quota(store, settings, grant, now)
    return {"lease_until": deadline.isoformat(timespec="microseconds")}


def admit(store, settings, clock, model_request_id, estimate, input_tokens):
    check_model(store, settings, clock, model_request_id)
    row = store.one('SELECT frozen_json FROM gateway_requests WHERE model_request_id = %s',
                    (model_request_id,))
    grant = ClientGrant.model_validate(json.loads(row["frozen_json"])["policy"]["grant"])
    if (estimate > grant.budget.max_request_micro_usd or estimate <= 0
        or input_tokens > grant.budget.max_input_tokens):
        raise CoreError("GATEWAY_REQUEST_BUDGET_EXHAUSTED", 429)
    _quota(store, settings, grant, clock(), adding=1, estimate=estimate)


def actual_limits(store, usage, actual):
    row = store.one('SELECT frozen_json, client_id FROM gateway_requests WHERE model_request_id = '
        '%s',
                    (usage["request_id"],))
    grant = ClientGrant.model_validate(json.loads(row["frozen_json"])["policy"]["grant"])
    local = datetime.fromisoformat(usage["occurred_at"]).astimezone(ZoneInfo(
        json.loads(store.one('SELECT snapshot_json FROM model_admissions WHERE request_id = %s',
                            (usage["request_id"],))["snapshot_json"])["timezone"]))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day.astimezone(UTC)
    end = (day + timedelta(days=1)).astimezone(UTC).isoformat(timespec="microseconds")
    total = store.one('SELECT COALESCE(CAST(SUM(m.billed_micro_usd) AS BIGINT), 0) AS amount FROM '
        'model_usage AS m JOIN gateway_requests AS g ON g.model_request_id = m.request_id WHERE '
        'g.client_id = %s AND m.occurred_at >= %s AND m.occurred_at < %s',
        (row["client_id"], start.isoformat(timespec="microseconds"), end))["amount"]
    result = []
    if actual > grant.budget.max_request_micro_usd:
        result.append("GATEWAY_REQUEST_AMOUNT")
    if total + actual - usage["billed_micro_usd"] > grant.budget.daily_micro_usd:
        result.append("GATEWAY_DAILY_AMOUNT")
    return result
