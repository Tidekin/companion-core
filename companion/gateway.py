"""Native Passthrough consumer: client authority, custody, quota and bounded receipts."""

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError

from companion import budget, cumulative, paid_budget
from companion.contracts import CoreError
from companion.gateway_config import GatewayConfig
from companion.gateway_contract import ChatRequest, ClientGrant, RequestContext, route_intent
from companion.model import GenerationOptions, ModelClient
from companion.store import after, encode, uid, utcnow


def config():
    try:
        current = GatewayConfig.load()
    except (OSError, ValueError):
        raise CoreError("GATEWAY_CONFIGURATION_INVALID", 503) from None
    if not current.enabled:
        raise CoreError("GATEWAY_DISABLED", 503)
    return current


def _cipher(settings):
    token = settings.control_token.get_secret_value()
    if len(token) < 24:
        raise CoreError("GATEWAY_OWNER_CREDENTIAL_REQUIRED", 503)
    key = hashlib.sha256(("companion/gateway/body/v1:" + token).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(settings, value):
    return _cipher(settings).encrypt(encode(value).encode()).decode()


def _decrypt(settings, value):
    try:
        return json.loads(_cipher(settings).decrypt(value.encode()))
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise CoreError("GATEWAY_RECEIPT_UNAVAILABLE", 503) from None


def key_digest(token):
    return hashlib.sha256(("companion/gateway/key/v1:" + token).encode()).hexdigest()


def _generation(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key='gateway_restore_generation'")
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
    if not store.one("SELECT 1 FROM sqlite_master WHERE name='gateway_clients'"):
        if expected:
            raise ValueError("missing gateway authority projection")
        return expected
    actual = {row["client_id"]: client_binding(row) for row in store.rows(
        "SELECT * FROM gateway_clients")}
    if expected != actual:
        raise ValueError("gateway authority behind journal or DB")
    return expected


def _passthrough(grant):
    if (grant.default_mode != "passthrough" or len(grant.modes) != 1
        or grant.modes[0].mode != "passthrough" or grant.owner_authority is not None):
        raise CoreError("GATEWAY_MODE_NOT_IMPLEMENTED", 403)


def budget_reference(store, settings, now):
    approval = paid_budget.contract(settings, now)
    with store.transaction():
        paid_budget.journal(store, approval)
        if approval.cumulative is not None:
            cumulative.activate(store, settings, now)
        journal = store.one("SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'")
    return {"budget_journal_id": journal["value"],
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(settings))}


def put_client(store, settings, grant, now):
    """Local owner management only. The returned high-entropy Key is never persisted."""
    grant = ClientGrant.model_validate(grant)
    _passthrough(grant)
    _cipher(settings)
    if grant.expires_at <= datetime.fromisoformat(now):
        raise CoreError("GATEWAY_GRANT_EXPIRED", 400)
    reference = budget_reference(store, settings, now)
    if any(getattr(grant.budget, name) != value for name, value in reference.items()):
        raise CoreError("GATEWAY_BUDGET_REFERENCE_CHANGED", 403)
    token = "gwk_" + secrets.token_urlsafe(32)
    with store.transaction():
        old = store.one("SELECT grant_json FROM gateway_clients WHERE client_id=?",
                        (grant.client_id,))
        if old and grant.revision <= ClientGrant.model_validate_json(old["grant_json"]).revision:
            raise CoreError("GATEWAY_GRANT_REVISION_REQUIRED", 409)
        store.execute("INSERT INTO gateway_clients VALUES (?,?,?,?,0,?,?,?) "
                      "ON CONFLICT(client_id) DO UPDATE SET key_sha256=excluded.key_sha256,"
                      "key_id=excluded.key_id,grant_json=excluded.grant_json,revoked=0,"
                      "updated_at=excluded.updated_at,restore_generation=excluded.restore_generation,"
                      "journal_path=excluded.journal_path", (grant.client_id, key_digest(token),
                      grant.key_id, encode(grant.model_dump(mode="json")), now, _generation(store),
                      str(Path(settings.paid_model.journal_path).resolve())))
        row = store.one("SELECT * FROM gateway_clients WHERE client_id=?", (grant.client_id,))
        paid_budget.journal(store, settings.paid_model,
                            {"kind": "gateway_client", "client": client_binding(row)})
    return token


def revoke_client(store, client_id, now):
    with store.transaction():
        store.execute("UPDATE gateway_clients SET revoked=1,updated_at=? WHERE client_id=?",
                      (now, client_id))
        row = store.one("SELECT * FROM gateway_clients WHERE client_id=?", (client_id,))
        if row:
            paid_budget.journal(store, SimpleNamespace(journal_path=row["journal_path"]),
                {"kind": "gateway_client", "client": client_binding(row)}, allow_settlement=True,
                initialize=False)


def authenticate(store, settings, token, now):
    with nullcontext() if store.in_write_transaction else store.transaction():
        return _authenticate_locked(store, settings, token, now)


def _authenticate_locked(store, settings, token, now):
    config()
    if (not isinstance(token, str) or not token.isascii() or not token.startswith("gwk_")
        or not 32 <= len(token) <= 128):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    # Dedicated keys cannot be substituted by any Core control or provider credential.
    for secret in (settings.bridge_token, settings.control_token, settings.model_key,
                   settings.video_model_key):
        if hmac.compare_digest(token.encode(), secret.get_secret_value().encode()):
            raise CoreError("GATEWAY_INVALID_KEY", 401)
    digest = key_digest(token)
    row = store.one("SELECT * FROM gateway_clients WHERE key_sha256=?", (digest,))
    if not row or row["revoked"] or not hmac.compare_digest(digest, row["key_sha256"]):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    if row["restore_generation"] != _generation(store):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    paid_budget.journal(store, SimpleNamespace(journal_path=row["journal_path"]),
                        allow_settlement=True, initialize=False)
    try:
        grant = ClientGrant.model_validate_json(row["grant_json"])
    except ValidationError:
        raise CoreError("GATEWAY_GRANT_INVALID", 503) from None
    _passthrough(grant)
    if grant.client_id != row["client_id"] or grant.key_id != row["key_id"]:
        raise CoreError("GATEWAY_GRANT_INVALID", 503)
    if grant.expires_at <= datetime.fromisoformat(now):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    return grant


def _reference(store, settings, grant, now):
    with nullcontext() if store.in_write_transaction else store.transaction():
        return _reference_locked(store, settings, grant, now)


def _reference_locked(store, settings, grant, now):
    cumulative.preflight(store, settings, now)
    paid_budget.contract(settings, now)
    if store.one("SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked',"
                 "'model_budget_restore_required','cloud_budget_restore_required')"):
        raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
    paid_budget.journal(store, settings.paid_model, initialize=False)
    journal = store.one("SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'")
    if (journal is None or grant.budget.budget_journal_id != journal["value"]
        or grant.budget.paid_contract_sha256 != paid_budget.sha(paid_budget.snapshot(settings))):
        raise CoreError("GATEWAY_BUDGET_REFERENCE_CHANGED", 403)


def _alias(current, name):
    found = next((item for item in current.aliases if item.model_alias == name), None)
    if found is None:
        raise CoreError("GATEWAY_MODEL_NOT_ALLOWED", 404)
    return found


def submit(store, settings, grant, body, context, idempotency_key, now):
    current = config()
    _passthrough(grant)
    _reference(store, settings, grant, now)
    if body.model not in grant.model_aliases:
        raise CoreError("GATEWAY_MODEL_NOT_ALLOWED", 404)
    if settings.provider == "deepseek" and any(m.role == "developer" for m in body.messages):
        raise CoreError("GATEWAY_ROLE_NOT_SUPPORTED", 400)
    try:
        selected = _alias(current, body.model)
        route_intent(grant, selected, body, context)
    except ValueError as error:
        raise CoreError(str(error), 429 if str(error) == "GATEWAY_OUTPUT_LIMIT" else 403) from None
    if idempotency_key is not None and (not 8 <= len(idempotency_key) <= 128
                                      or not idempotency_key.isascii()):
        raise CoreError("GATEWAY_IDEMPOTENCY_KEY_INVALID", 400)
    request_hash = paid_budget.sha({"body": body.model_dump(mode="json"),
                                  "context": context.model_dump(mode="json")})
    identity = paid_budget.sha(idempotency_key) if idempotency_key is not None else uid("intent")
    policy = {"grant": grant.model_dump(mode="json"), "alias": selected.model_dump(),
              "context": context.model_dump(mode="json"), "request_sha256": request_hash}
    with store.transaction():
        existing = store.one("SELECT * FROM gateway_requests WHERE client_id=? "
                             "AND idempotency_sha256=?", (grant.client_id, identity))
        if existing:
            saved = json.loads(existing["frozen_json"])
            if saved["policy"] != policy:
                raise CoreError("GATEWAY_IDEMPOTENCY_CONFLICT", 409)
            return existing["request_id"]
        pending = store.one("SELECT COUNT(*) n FROM gateway_requests WHERE client_id=? "
                            "AND state IN ('QUEUED','RUNNING')", (grant.client_id,))["n"]
        total = store.one("SELECT COUNT(*) n FROM gateway_requests WHERE state IN "
                         "('QUEUED','RUNNING')")["n"]
        if pending >= current.pending_per_client or total >= 256:
            raise CoreError("GATEWAY_QUEUE_FULL", 429)
        request_id = uid("gateway")
        expires = after(now, current.receipt_seconds)
        deadline = min(expires, after(now, current.execution_seconds),
                       grant.expires_at.astimezone(UTC).isoformat(timespec="microseconds"))
        frozen = {"policy": policy, "request_id": request_id, "client_id": grant.client_id,
                  "created_at": now, "expires_at": expires, "deadline": deadline,
                  "idempotency_sha256": identity}
        store.insert("gateway_requests", request_id=request_id, client_id=grant.client_id,
            model_request_id=uid("model"), idempotency_sha256=identity,
            body_sha256=request_hash, frozen_json=encode(frozen),
            request_cipher=_encrypt(settings, body.model_dump(mode="json")), state="QUEUED",
            created_at=now, expires_at=expires, deadline=deadline)
        return request_id


def binding(store, model_request_id):
    row = store.one("SELECT * FROM gateway_requests WHERE model_request_id=?", (model_request_id,))
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
    rows = store.rows("SELECT request_id FROM model_usage WHERE scenario='gateway'")
    actual = {row["request_id"]: binding(store, row["request_id"]) for row in rows}
    if actual != expected:
        raise ValueError("gateway attribution behind journal or DB")
    return expected


def _quota(store, settings, grant, now, *, adding=0, estimate=0):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat(
        timespec="microseconds")
    daily = store.one("SELECT COUNT(*) calls,COALESCE(SUM(m.billed_micro_usd),0) amount "
        "FROM model_usage m JOIN gateway_requests g ON g.model_request_id=m.request_id "
        "WHERE g.client_id=? AND m.occurred_at>=?", (grant.client_id, day))
    running = store.one("SELECT COUNT(*) n FROM model_usage m JOIN gateway_requests g "
        "ON g.model_request_id=m.request_id WHERE g.client_id=? "
        "AND m.state IN ('RESERVED','UNKNOWN')", (grant.client_id,))["n"]
    if (daily["calls"] + adding > grant.budget.daily_calls
        or daily["amount"] + estimate > grant.budget.daily_micro_usd
        or running + adding > grant.budget.concurrency):
        raise CoreError("GATEWAY_CLIENT_BUDGET_EXHAUSTED", 429)


def check_model(store, settings, clock, model_request_id):
    current = config()
    row = store.one("SELECT * FROM gateway_requests WHERE model_request_id=?", (model_request_id,))
    if row is None or row["state"] != "RUNNING" or not row["lease_token"]:
        raise CoreError("GATEWAY_REQUEST_FENCED", 409)
    binding(store, model_request_id)
    frozen = json.loads(row["frozen_json"])["policy"]
    grant = ClientGrant.model_validate(frozen["grant"])
    client = store.one("SELECT * FROM gateway_clients WHERE client_id=?", (row["client_id"],))
    if client is None or client["revoked"] or json.loads(client["grant_json"]) != frozen["grant"]:
        raise CoreError("GATEWAY_GRANT_CHANGED", 403)
    if client["restore_generation"] != _generation(store):
        raise CoreError("GATEWAY_GRANT_CHANGED", 403)
    _passthrough(grant)
    if _alias(current, frozen["alias"]["model_alias"]).model_dump() != frozen["alias"]:
        raise CoreError("GATEWAY_ALIAS_CHANGED", 403)
    now = clock()
    _reference(store, settings, grant, now)
    deadline = min(datetime.fromisoformat(row["deadline"]),
                   datetime.fromisoformat(row["lease_until"]), grant.expires_at)
    if deadline <= datetime.fromisoformat(clock()):
        raise CoreError("GATEWAY_REQUEST_EXPIRED", 409)
    _quota(store, settings, grant, now)
    return {"lease_until": deadline.isoformat(timespec="microseconds")}


def admit(store, settings, clock, model_request_id, estimate, input_tokens):
    check_model(store, settings, clock, model_request_id)
    row = store.one("SELECT frozen_json FROM gateway_requests WHERE model_request_id=?",
                    (model_request_id,))
    grant = ClientGrant.model_validate(json.loads(row["frozen_json"])["policy"]["grant"])
    if (estimate > grant.budget.max_request_micro_usd or estimate <= 0
        or input_tokens > grant.budget.max_input_tokens):
        raise CoreError("GATEWAY_REQUEST_BUDGET_EXHAUSTED", 429)
    _quota(store, settings, grant, clock(), adding=1, estimate=estimate)


def actual_limits(store, usage, actual):
    row = store.one("SELECT frozen_json,client_id FROM gateway_requests WHERE model_request_id=?",
                    (usage["request_id"],))
    grant = ClientGrant.model_validate(json.loads(row["frozen_json"])["policy"]["grant"])
    local = datetime.fromisoformat(usage["occurred_at"]).astimezone(ZoneInfo(
        json.loads(store.one("SELECT snapshot_json FROM model_admissions WHERE request_id=?",
                            (usage["request_id"],))["snapshot_json"])["timezone"]))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day.astimezone(UTC)
    end = (day + timedelta(days=1)).astimezone(UTC).isoformat(timespec="microseconds")
    total = store.one("SELECT COALESCE(SUM(m.billed_micro_usd),0) amount FROM model_usage m "
        "JOIN gateway_requests g ON g.model_request_id=m.request_id WHERE g.client_id=? "
        "AND m.occurred_at>=? AND m.occurred_at<?",
        (row["client_id"], start.isoformat(timespec="microseconds"), end))["amount"]
    result = []
    if actual > grant.budget.max_request_micro_usd:
        result.append("GATEWAY_REQUEST_AMOUNT")
    if total + actual - usage["billed_micro_usd"] > grant.budget.daily_micro_usd:
        result.append("GATEWAY_DAILY_AMOUNT")
    return result


def expire(store, settings, now):
    """Neither an expired wait nor a dead worker grants a second paid attempt."""
    with nullcontext() if store.in_write_transaction else store.transaction():
        for row in store.rows("SELECT g.* FROM gateway_requests g WHERE (g.state IN "
                              "('QUEUED','RUNNING') OR EXISTS (SELECT 1 FROM model_usage m "
                              "WHERE m.request_id=g.model_request_id AND m.state='RESERVED')) "
                              "AND (g.deadline<=? OR g.lease_until<=?)",
                              (now, now)):
            usage = store.one("SELECT state FROM model_usage WHERE request_id=?",
                              (row["model_request_id"],))
            if usage and usage["state"] == "RESERVED":
                budget.settle(store, settings, row["model_request_id"])
            store.execute("UPDATE gateway_requests SET state=?,error_code=?,request_cipher=NULL,"
                          "lease_token=NULL,"
                          "lease_until=NULL,error_status=? WHERE request_id=?", (
                          "EXPIRED" if row["state"] == "EXPIRED" else
                          "UNKNOWN" if usage and usage["state"] != "SETTLED" else "FAILED",
                          "GATEWAY_RESULT_UNAVAILABLE" if usage and usage["state"] == "SETTLED"
                          else "GATEWAY_OUTCOME_UNKNOWN" if usage else "GATEWAY_REQUEST_EXPIRED",
                          503 if usage else 409, row["request_id"]))
        store.execute("UPDATE gateway_requests SET request_cipher=NULL,response_cipher=NULL,"
                      "state='EXPIRED' WHERE expires_at<=?", (now,))


def receipt(store, settings, client_id, request_id, now):
    # The journal is fsynced before its SQL projection commits. Share the native
    # writer barrier so receipt polling cannot observe that temporary mismatch.
    with nullcontext() if store.in_write_transaction else store.transaction():
        return _receipt_locked(store, settings, client_id, request_id, now)


def _receipt_locked(store, settings, client_id, request_id, now):
    expire(store, settings, now)
    row = store.one("SELECT * FROM gateway_requests WHERE request_id=? AND client_id=?",
                    (request_id, client_id))
    if row is None:
        raise CoreError("GATEWAY_REQUEST_NOT_FOUND", 404)
    completion, accounting = None, None
    usage = store.one("SELECT * FROM model_usage WHERE request_id=?", (row["model_request_id"],))
    if usage:
        admission = store.one("SELECT journal_path FROM model_admissions WHERE request_id=?",
                              (row["model_request_id"],))
        if admission is None:
            raise CoreError("GATEWAY_ACCOUNTING_UNAVAILABLE", 503)
        paid_budget.journal(store, SimpleNamespace(journal_path=admission["journal_path"]),
                            allow_settlement=True, initialize=False)
        accounting = {"state": usage["state"], "micro_usd": usage["billed_micro_usd"],
                      "currency": "USD", "basis": "configured_rates_not_invoice"}
    if row["state"] == "SUCCEEDED" and row["response_cipher"]:
        if usage is None or usage["state"] != "SETTLED":
            raise CoreError("GATEWAY_ACCOUNTING_UNAVAILABLE", 503)
        completion = _decrypt(settings, row["response_cipher"])
        if completion.get("id") != "chatcmpl-" + row["request_id"]:
            raise CoreError("GATEWAY_RECEIPT_UNAVAILABLE", 503)
    return {"id": row["request_id"], "object": "gateway.request", "status": row["state"],
            "created": int(datetime.fromisoformat(row["created_at"]).timestamp()),
            "expires_at": row["expires_at"], "error_code": row["error_code"],
            "error_status": row["error_status"],
            "completion": completion, "accounting": accounting}


async def run_once(store, settings, model, clock=utcnow):
    config()
    if settings.mode != "live" or not isinstance(model, ModelClient):
        raise CoreError("GATEWAY_NATIVE_MODEL_REQUIRED", 503)
    expire(store, settings, clock())
    with store.transaction():
        row = store.one("SELECT * FROM gateway_requests WHERE state='QUEUED' "
                        "ORDER BY created_at,request_id LIMIT 1")
        if row is None:
            return False
        lease = uid("gateway_lease")
        deadline = min(row["deadline"], after(clock(), settings.model_timeout("gateway") + 10))
        store.execute("UPDATE gateway_requests SET state='RUNNING',lease_token=?,lease_until=? "
                      "WHERE request_id=?", (lease, deadline, row["request_id"]))
    response, error, error_status = None, None, None
    try:
        body = ChatRequest.model_validate(_decrypt(settings, row["request_cipher"]))
        policy = json.loads(row["frozen_json"])["policy"]
        if paid_budget.sha({"body": body.model_dump(mode="json"), "context": policy["context"]
                            }) != row["body_sha256"]:
            raise CoreError("GATEWAY_REQUEST_BINDING_CHANGED", 503)
        intent = route_intent(ClientGrant.model_validate(policy["grant"]),
                              _alias(config(), body.model), body,
                              RequestContext.model_validate(policy["context"]))
        generation = GenerationOptions(intent.max_output_tokens, body.temperature)
        messages = [message.model_dump() for message in body.messages]
        budget.reserve(store, settings, None, "gateway", messages, clock(),
            request_id=row["model_request_id"], logical_request_id=row["request_id"],
            clock=clock, generation=generation)
        response = await budget.complete(model, store, settings, row["model_request_id"],
                                         messages, "gateway", clock, generation=generation)
    except (CoreError, ValueError) as failure:
        error = failure.code if isinstance(failure, CoreError) else "GATEWAY_REQUEST_INVALID"
        error_status = failure.status if isinstance(failure, CoreError) else 400
    with store.transaction():
        usage = store.one("SELECT * FROM model_usage WHERE request_id=?",
                          (row["model_request_id"],))
        state, encrypted = "FAILED", None
        if usage and usage["state"] != "SETTLED":
            state, error = "UNKNOWN", "GATEWAY_OUTCOME_UNKNOWN"
            error_status = 503
        elif response is not None and error is None and row["expires_at"] > clock():
            completion = {"id": "chatcmpl-" + row["request_id"], "object": "chat.completion",
                "created": int(datetime.fromisoformat(row["created_at"]).timestamp()),
                "model": body.model, "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": response.text},
                    "finish_reason": response.finish_reason}], "usage": {
                    "prompt_tokens": response.input_tokens,
                    "completion_tokens": response.output_tokens,
                    "total_tokens": response.input_tokens + response.output_tokens}}
            state, encrypted = "SUCCEEDED", _encrypt(settings, completion)
        store.execute("UPDATE gateway_requests SET state=?,response_cipher=?,request_cipher=NULL,"
                      "error_code=?,error_status=?,lease_token=NULL,lease_until=NULL "
                      "WHERE request_id=? AND state='RUNNING' AND lease_token=?",
                      (state, encrypted, error, error_status, row["request_id"], lease))
    return True


def restore(store):
    """A backup cannot resurrect client credentials, bodies or queued execution."""
    if store.one("SELECT 1 FROM gateway_clients LIMIT 1") is None:
        return
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                  ("gateway_restore_generation", uid("restore")))
    store.execute("UPDATE gateway_requests SET request_cipher=NULL,response_cipher=NULL,"
                  "state='UNKNOWN',lease_token=NULL,lease_until=NULL,"
                  "error_code='GATEWAY_RESTORED_REQUIRES_REVIEW',error_status=503")
