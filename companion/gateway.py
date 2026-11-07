"""Native Passthrough consumer: client authority, custody, quota and bounded receipts."""

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError

from companion import budget, cumulative, paid_budget
from companion.contracts import CoreError
from companion.gateway_admission import (
    client_binding,
    config,
    model_alias,
    require_passthrough,
    require_reference,
    restore_generation,
)
from companion.gateway_contract import ChatRequest, ClientGrant, RequestContext, route_intent
from companion.model import GenerationOptions, ModelClient
from companion.store import after, encode, uid, utcnow


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


def budget_reference(store, settings, now):
    approval = paid_budget.contract(settings, now)
    with store.transaction():
        paid_budget.journal(store, approval)
        if approval.cumulative is not None:
            cumulative.activate(store, settings, now)
        journal = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
    return {"budget_journal_id": journal["value"],
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(settings))}


def put_client(store, settings, grant, now):
    """Local owner management only. The returned high-entropy Key is never persisted."""
    grant = ClientGrant.model_validate(grant)
    require_passthrough(grant)
    _cipher(settings)
    if grant.expires_at <= datetime.fromisoformat(now):
        raise CoreError("GATEWAY_GRANT_EXPIRED", 400)
    reference = budget_reference(store, settings, now)
    if any(getattr(grant.budget, name) != value for name, value in reference.items()):
        raise CoreError("GATEWAY_BUDGET_REFERENCE_CHANGED", 403)
    token = "gwk_" + secrets.token_urlsafe(32)
    with store.transaction():
        old = store.one('SELECT grant_json FROM gateway_clients WHERE client_id = %s',
                        (grant.client_id,))
        if old and grant.revision <= ClientGrant.model_validate_json(old["grant_json"]).revision:
            raise CoreError("GATEWAY_GRANT_REVISION_REQUIRED", 409)
        store.execute('INSERT INTO gateway_clients VALUES (%s, %s, %s, %s, 0, %s, %s, %s) ON '
            'CONFLICT(client_id) DO UPDATE SET key_sha256 = excluded.key_sha256, key_id = '
            'excluded.key_id, grant_json = excluded.grant_json, revoked = 0, updated_at = '
            'excluded.updated_at, restore_generation = excluded.restore_generation, journal_path = '
            'excluded.journal_path', (grant.client_id, key_digest(token),
                      grant.key_id, encode(grant.model_dump(mode="json")), now,
                      restore_generation(store),
                      str(Path(settings.paid_model.journal_path).resolve())))
        row = store.one('SELECT gateway_clients.client_id AS client_id, gateway_clients.key_sha256 '
            'AS key_sha256, gateway_clients.key_id AS key_id, gateway_clients.grant_json AS '
            'grant_json, gateway_clients.revoked AS revoked, gateway_clients.updated_at AS '
            'updated_at, gateway_clients.restore_generation AS restore_generation, '
            'gateway_clients.journal_path AS journal_path FROM gateway_clients AS gateway_clients '
            'WHERE gateway_clients.client_id = %s', (grant.client_id,))
        paid_budget.journal(store, settings.paid_model,
                            {"kind": "gateway_client", "client": client_binding(row)})
    return token


def revoke_client(store, client_id, now):
    with store.transaction():
        store.execute('UPDATE gateway_clients SET revoked = 1, updated_at = %s WHERE client_id = '
            '%s',
                      (now, client_id))
        row = store.one('SELECT gateway_clients.client_id AS client_id, gateway_clients.key_sha256 '
            'AS key_sha256, gateway_clients.key_id AS key_id, gateway_clients.grant_json AS '
            'grant_json, gateway_clients.revoked AS revoked, gateway_clients.updated_at AS '
            'updated_at, gateway_clients.restore_generation AS restore_generation, '
            'gateway_clients.journal_path AS journal_path FROM gateway_clients AS gateway_clients '
            'WHERE gateway_clients.client_id = %s', (client_id,))
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
    row = store.one('SELECT gateway_clients.client_id AS client_id, gateway_clients.key_sha256 AS '
        'key_sha256, gateway_clients.key_id AS key_id, gateway_clients.grant_json AS grant_json, '
        'gateway_clients.revoked AS revoked, gateway_clients.updated_at AS updated_at, '
        'gateway_clients.restore_generation AS restore_generation, gateway_clients.journal_path AS '
        'journal_path FROM gateway_clients AS gateway_clients WHERE gateway_clients.key_sha256 = '
        '%s', (digest,))
    if not row or row["revoked"] or not hmac.compare_digest(digest, row["key_sha256"]):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    if row["restore_generation"] != restore_generation(store):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    paid_budget.journal(store, SimpleNamespace(journal_path=row["journal_path"]),
                        allow_settlement=True, initialize=False)
    try:
        grant = ClientGrant.model_validate_json(row["grant_json"])
    except ValidationError:
        raise CoreError("GATEWAY_GRANT_INVALID", 503) from None
    require_passthrough(grant)
    if grant.client_id != row["client_id"] or grant.key_id != row["key_id"]:
        raise CoreError("GATEWAY_GRANT_INVALID", 503)
    if grant.expires_at <= datetime.fromisoformat(now):
        raise CoreError("GATEWAY_INVALID_KEY", 401)
    return grant


def submit(store, settings, grant, body, context, idempotency_key, now):
    current = config()
    require_passthrough(grant)
    require_reference(store, settings, grant, now)
    if body.model not in grant.model_aliases:
        raise CoreError("GATEWAY_MODEL_NOT_ALLOWED", 404)
    if settings.provider == "deepseek" and any(m.role == "developer" for m in body.messages):
        raise CoreError("GATEWAY_ROLE_NOT_SUPPORTED", 400)
    try:
        selected = model_alias(current, body.model)
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
        existing = store.one('SELECT gateway_requests.request_id AS request_id, '
            'gateway_requests.client_id AS client_id, gateway_requests.model_request_id AS '
            'model_request_id, gateway_requests.idempotency_sha256 AS idempotency_sha256, '
            'gateway_requests.body_sha256 AS body_sha256, gateway_requests.frozen_json AS '
            'frozen_json, gateway_requests.request_cipher AS request_cipher, '
            'gateway_requests.response_cipher AS response_cipher, gateway_requests.state AS state, '
            'gateway_requests.created_at AS created_at, gateway_requests.expires_at AS expires_at, '
            'gateway_requests.deadline AS deadline, gateway_requests.lease_token AS lease_token, '
            'gateway_requests.lease_until AS lease_until, gateway_requests.error_code AS '
            'error_code, gateway_requests.error_status AS error_status FROM gateway_requests AS '
            'gateway_requests WHERE gateway_requests.client_id = %s AND '
            'gateway_requests.idempotency_sha256 = %s', (grant.client_id, identity))
        if existing:
            saved = json.loads(existing["frozen_json"])
            if saved["policy"] != policy:
                raise CoreError("GATEWAY_IDEMPOTENCY_CONFLICT", 409)
            return existing["request_id"]
        pending = store.one('SELECT COUNT(*) AS n FROM gateway_requests WHERE client_id = %s AND '
            "state IN ('QUEUED', 'RUNNING')", (grant.client_id,))["n"]
        total = store.one("SELECT COUNT(*) AS n FROM gateway_requests WHERE state IN ('QUEUED', "
            "'RUNNING')")["n"]
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


def expire(store, settings, now):
    """Neither an expired wait nor a dead worker grants a second paid attempt."""
    with nullcontext() if store.in_write_transaction else store.transaction():
        for row in store.rows('SELECT g.request_id AS request_id, g.client_id AS client_id, '
            'g.model_request_id AS model_request_id, g.idempotency_sha256 AS idempotency_sha256, '
            'g.body_sha256 AS body_sha256, g.frozen_json AS frozen_json, g.request_cipher AS '
            'request_cipher, g.response_cipher AS response_cipher, g.state AS state, g.created_at '
            'AS created_at, g.expires_at AS expires_at, g.deadline AS deadline, g.lease_token AS '
            'lease_token, g.lease_until AS lease_until, g.error_code AS error_code, g.error_status '
            "AS error_status FROM gateway_requests AS g WHERE (g.state IN ('QUEUED', 'RUNNING') OR "
            'EXISTS(SELECT 1 AS "1" FROM model_usage AS m WHERE m.request_id = g.model_request_id '
            "AND m.state = 'RESERVED')) AND (g.deadline <= %s OR g.lease_until <= %s)",
                              (now, now)):
            usage = store.one('SELECT state FROM model_usage WHERE request_id = %s',
                              (row["model_request_id"],))
            if usage and usage["state"] == "RESERVED":
                budget.settle(store, settings, row["model_request_id"])
            store.execute('UPDATE gateway_requests SET state = %s, error_code = %s, request_cipher '
                '= NULL, lease_token = NULL, lease_until = NULL, error_status = %s WHERE '
                'request_id = %s', (
                          "EXPIRED" if row["state"] == "EXPIRED" else
                          "UNKNOWN" if usage and usage["state"] != "SETTLED" else "FAILED",
                          "GATEWAY_RESULT_UNAVAILABLE" if usage and usage["state"] == "SETTLED"
                          else "GATEWAY_OUTCOME_UNKNOWN" if usage else "GATEWAY_REQUEST_EXPIRED",
                          503 if usage else 409, row["request_id"]))
        store.execute('UPDATE gateway_requests SET request_cipher = NULL, response_cipher = NULL, '
            "state = 'EXPIRED' WHERE expires_at <= %s", (now,))


def receipt(store, settings, client_id, request_id, now):
    # The journal is fsynced before its SQL projection commits. Share the native
    # writer barrier so receipt polling cannot observe that temporary mismatch.
    with nullcontext() if store.in_write_transaction else store.transaction():
        return _receipt_locked(store, settings, client_id, request_id, now)


def _receipt_locked(store, settings, client_id, request_id, now):
    expire(store, settings, now)
    row = store.one('SELECT gateway_requests.request_id AS request_id, gateway_requests.client_id '
        'AS client_id, gateway_requests.model_request_id AS model_request_id, '
        'gateway_requests.idempotency_sha256 AS idempotency_sha256, gateway_requests.body_sha256 '
        'AS body_sha256, gateway_requests.frozen_json AS frozen_json, '
        'gateway_requests.request_cipher AS request_cipher, gateway_requests.response_cipher AS '
        'response_cipher, gateway_requests.state AS state, gateway_requests.created_at AS '
        'created_at, gateway_requests.expires_at AS expires_at, gateway_requests.deadline AS '
        'deadline, gateway_requests.lease_token AS lease_token, gateway_requests.lease_until AS '
        'lease_until, gateway_requests.error_code AS error_code, gateway_requests.error_status AS '
        'error_status FROM gateway_requests AS gateway_requests WHERE gateway_requests.request_id '
        '= %s AND gateway_requests.client_id = %s',
                    (request_id, client_id))
    if row is None:
        raise CoreError("GATEWAY_REQUEST_NOT_FOUND", 404)
    completion, accounting = None, None
    usage = store.one('SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS '
        'request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, '
        'model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS '
        'input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, '
        'model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS '
        'billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS '
        'occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM '
        'model_usage AS model_usage WHERE model_usage.request_id = %s', (row["model_request_id"],))
    if usage:
        admission = store.one('SELECT journal_path FROM model_admissions WHERE request_id = %s',
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
        row = store.one('SELECT gateway_requests.request_id AS request_id, '
            'gateway_requests.client_id AS client_id, gateway_requests.model_request_id AS '
            'model_request_id, gateway_requests.idempotency_sha256 AS idempotency_sha256, '
            'gateway_requests.body_sha256 AS body_sha256, gateway_requests.frozen_json AS '
            'frozen_json, gateway_requests.request_cipher AS request_cipher, '
            'gateway_requests.response_cipher AS response_cipher, gateway_requests.state AS state, '
            'gateway_requests.created_at AS created_at, gateway_requests.expires_at AS expires_at, '
            'gateway_requests.deadline AS deadline, gateway_requests.lease_token AS lease_token, '
            'gateway_requests.lease_until AS lease_until, gateway_requests.error_code AS '
            'error_code, gateway_requests.error_status AS error_status FROM gateway_requests AS '
            "gateway_requests WHERE gateway_requests.state = 'QUEUED' ORDER BY "
            'gateway_requests.created_at NULLS FIRST, gateway_requests.request_id NULLS FIRST '
            'LIMIT 1')
        if row is None:
            return False
        lease = uid("gateway_lease")
        deadline = min(row["deadline"], after(clock(), settings.model_timeout("gateway") + 10))
        store.execute("UPDATE gateway_requests SET state = 'RUNNING', lease_token = %s, "
            'lease_until = %s WHERE request_id = %s', (lease, deadline, row["request_id"]))
    response, error, error_status = None, None, None
    try:
        body = ChatRequest.model_validate(_decrypt(settings, row["request_cipher"]))
        policy = json.loads(row["frozen_json"])["policy"]
        if paid_budget.sha({"body": body.model_dump(mode="json"), "context": policy["context"]
                            }) != row["body_sha256"]:
            raise CoreError("GATEWAY_REQUEST_BINDING_CHANGED", 503)
        intent = route_intent(ClientGrant.model_validate(policy["grant"]),
                              model_alias(config(), body.model), body,
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
        usage = store.one('SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS '
            'request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, '
            'model_usage.provider AS provider, model_usage.model AS model, '
            'model_usage.input_tokens AS input_tokens, model_usage.cached_input_tokens AS '
            'cached_input_tokens, model_usage.output_tokens AS output_tokens, '
            'model_usage.billed_micro_usd AS billed_micro_usd, model_usage.is_estimate AS '
            'is_estimate, model_usage.occurred_at AS occurred_at, model_usage.state AS state, '
            'model_usage.pricing_known AS pricing_known FROM model_usage AS model_usage WHERE '
            'model_usage.request_id = %s',
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
        store.execute('UPDATE gateway_requests SET state = %s, response_cipher = %s, '
            'request_cipher = NULL, error_code = %s, error_status = %s, lease_token = NULL, '
            "lease_until = NULL WHERE request_id = %s AND state = 'RUNNING' AND lease_token = %s",
                      (state, encrypted, error, error_status, row["request_id"], lease))
    return True


def restore(store):
    """A backup cannot resurrect client credentials, bodies or queued execution."""
    if store.one('SELECT 1 FROM gateway_clients LIMIT 1') is None:
        return
    store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO '
        'UPDATE SET value = excluded.value, record_sequence = DEFAULT',
                  ("gateway_restore_generation", uid("restore")))
    store.execute('UPDATE gateway_requests SET request_cipher = NULL, response_cipher = NULL, '
        "state = 'UNKNOWN', lease_token = NULL, lease_until = NULL, error_code = "
        "'GATEWAY_RESTORED_REQUIRES_REVIEW', error_status = 503")
