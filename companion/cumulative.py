"""Cumulative admission in the original Budget Journal, never a second wallet.

Account ownership is an owner-approved anonymous assertion. These are local
liability limits, not a verified provider account balance or external hard cap.
"""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from companion.contracts import CoreError
from companion.store import encode

SHA = r"^[a-f0-9]{64}$"
PROJECTION = "model_budget_cumulative"
DIMENSIONS = ("micro_usd", "model_attempts", "input_tokens", "output_tokens",
              "owner_inputs", "send_attempts")
POLICY_RECORDS = {"quota_enroll", "quota_renew"}
CONTROL_RECORDS = POLICY_RECORDS | {
    "quota_pause", "quota_operation", "quota_send_result", "quota_send_resolution",
}
LEGACY_DOMAIN = b"companion-core/cumulative-window-inventory/v1\x00"


class ModelCaps(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    micro_usd: int = Field(strict=True, ge=0, le=2**63 - 1)
    model_attempts: int = Field(strict=True, ge=0, le=2**31 - 1)
    input_tokens: int = Field(strict=True, ge=0, le=2**63 - 1)
    output_tokens: int = Field(strict=True, ge=0, le=2**63 - 1)


class PeriodCaps(ModelCaps):
    owner_inputs: int = Field(strict=True, ge=0, le=2**31 - 1)
    send_attempts: int = Field(strict=True, ge=0, le=2**31 - 1)


class LegacyWindowReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["input", "send"]
    source_sha256: str = Field(pattern=SHA)
    window_id: str = Field(min_length=1, max_length=128)
    at: AwareDatetime
    record_sha256: str = Field(pattern=SHA)
    journal_head_sha256: str = Field(pattern=SHA)


class LegacyWindowInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cumulative_window_inventory_v1"]
    identity_sha256: str = Field(pattern=SHA)
    budget_journal_id: str = Field(pattern=r"^budget_[a-f0-9]{32}$")
    reservations: list[LegacyWindowReservation] = Field(max_length=20000)
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class CumulativeApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    account_ref: str = Field(pattern=SHA)
    policy_id: str = Field(pattern=r"^quota_[a-f0-9]{32}$")
    approval_sha256: str = Field(pattern=SHA)
    account_evidence_sha256: str = Field(pattern=SHA)
    credential_sha256: str = Field(pattern=SHA)
    key_ownership_sha256: str = Field(pattern=SHA)
    provider: str = Field(min_length=1, max_length=64)
    currency: Literal["USD"]
    timezone: str
    owner_identity_sha256: str = Field(pattern=SHA)
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    expected_journal_head_sha256: str = Field(pattern=SHA)
    predecessor_policy_sha256: str | None = Field(default=None, pattern=SHA)
    legacy_liabilities_sha256: str | None = Field(default=None, pattern=SHA)
    legacy_adoption_evidence_sha256: str | None = Field(default=None, pattern=SHA)
    legacy_window_inventory: LegacyWindowInventory | None = None
    request: ModelCaps
    window: PeriodCaps
    day: PeriodCaps
    month: PeriodCaps
    account: PeriodCaps
    concurrency: int = Field(strict=True, ge=1, le=32)

    @model_validator(mode="after")
    def interval(self):
        ZoneInfo(self.timezone)
        if self.starts_at >= self.expires_at:
            raise ValueError("INVALID_CUMULATIVE_INTERVAL")
        if self.predecessor_policy_sha256 is None and self.legacy_liabilities_sha256 is None:
            raise ValueError("EXACT_LEGACY_LIABILITIES_REQUIRED")
        return self


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ref: str = Field(pattern=SHA)
    policy_sha256: str = Field(pattern=SHA)
    evidence_sha256: str = Field(pattern=SHA)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def instant(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("aware cumulative time required")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def periods(at, timezone):
    local = datetime.fromisoformat(at).astimezone(ZoneInfo(timezone))
    return local.date().isoformat(), local.strftime("%Y-%m")


def credential(settings, account_ref):
    return hmac.new(settings.model_key.get_secret_value().encode(),
        encode(["companion/account-key/v1", account_ref, settings.provider]).encode(),
        hashlib.sha256).hexdigest()


def empty_state():
    return {"policy": None, "policy_sha256": None, "policies": {}, "models": {},
            "operations": {}, "clock": None, "paused": False, "reason": None,
            "send_resolutions": {}}


def binding(state, path, head=None):
    if state["policy"] is None:
        return None
    return {"account_ref": state["policy"]["account_ref"],
            "policy_sha256": state["policy_sha256"], "journal_path": str(Path(path).resolve()),
            **({"control_head_sha256": head} if head else {})}


def _liability(value):
    if set(value) != {"at", "window_id", "logical_request_id", "state", *DIMENSIONS}:
        raise ValueError("invalid cumulative liability")
    if value["at"] != instant(value["at"]) or value["state"] not in {
        "RESERVED", "UNKNOWN", "SETTLED", "ADMITTED", "SENT", "FAILED", "DELIVERY_UNCERTAIN",
    } or any(type(value[name]) is not int or value[name] < 0 for name in DIMENSIONS):
        raise ValueError("invalid cumulative liability value")
    if not isinstance(value["window_id"], str) or not isinstance(value["logical_request_id"], str):
        raise ValueError("invalid cumulative source")
    return dict(value)


def blocking_pause(state):
    """Cost uncertainty is advisory; owner and delivery controls remain hard fences."""
    return state["paused"] and state["reason"] not in {
        "UNKNOWN_MODEL_LIABILITY", "LOCAL_LIMIT_EXCEEDED",
    }


def fold(state, row):
    """Pure projection of versioned records in the existing hash-chained journal."""
    kind = row["kind"]
    if kind == "quota_send_resolution":
        from companion.delivery_reconciliation import fold_record

        fold_record(state, row)
        return
    if kind in POLICY_RECORDS:
        expected = {"kind", "version", "at", "policy", "policy_sha256", "previous"}
        if kind == "quota_enroll":
            expected.add("legacy")
        if set(row) != expected or row["version"] != 1:
            raise ValueError("invalid cumulative policy record")
        policy = CumulativeApproval.model_validate(row["policy"]).model_dump(mode="json")
        if policy != row["policy"] or digest(policy) != row["policy_sha256"]:
            raise ValueError("cumulative policy hash")
        at = instant(row["at"])
        if at != row["at"] or state["clock"] and at < state["clock"]:
            raise ValueError("cumulative policy clock")
        if row["previous"] != policy["expected_journal_head_sha256"]:
            raise ValueError("cumulative policy cutoff")
        if kind == "quota_enroll":
            if state["policy"] or policy["predecessor_policy_sha256"] is not None:
                raise ValueError("cumulative account already bound")
            legacy = row["legacy"]
            if set(legacy) != {"models", "operations"} or digest(legacy) != policy[
                "legacy_liabilities_sha256"
            ]:
                raise ValueError("legacy cumulative liabilities")
            if (legacy["models"] or legacy["operations"]) and not policy[
                "legacy_adoption_evidence_sha256"
            ]:
                raise ValueError("legacy account attribution missing")
            for category in ("models", "operations"):
                if not isinstance(legacy[category], dict) or len(legacy[category]) > 10000:
                    raise ValueError("bounded legacy liabilities required")
                state[category] = {
                    key: _liability(value) for key, value in legacy[category].items()
                }
                if any(value["state"] not in {"SETTLED", "ADMITTED", "SENT", "FAILED"}
                       for value in state[category].values()):
                    raise ValueError("unresolved legacy liability")
                if any(value["at"] > at for value in state[category].values()):
                    raise ValueError("legacy responsibility is ahead of the clock")
        else:
            previous = state["policy"]
            if not previous or policy["predecessor_policy_sha256"] != state["policy_sha256"]:
                raise ValueError("cumulative renewal predecessor")
            if any(policy[key] != previous[key] for key in (
                "account_ref", "provider", "currency", "timezone", "owner_identity_sha256",
            )) or policy["policy_id"] in {p["policy_id"] for p in state["policies"].values()}:
                raise ValueError("cumulative account rebind or policy reuse")
            if policy["approval_sha256"] in {
                p["approval_sha256"] for p in state["policies"].values()
            }:
                raise ValueError("new cumulative approval evidence required")
            if policy["credential_sha256"] != previous["credential_sha256"] and (
                policy["key_ownership_sha256"] == previous["key_ownership_sha256"]
            ):
                raise ValueError("key rotation attribution required")
            if any(v["state"] in {"RESERVED", "DELIVERY_UNCERTAIN"}
                   for v in [*state["models"].values(), *state["operations"].values()]):
                raise ValueError("unresolved responsibility cannot renew")
        state.update(policy=policy, policy_sha256=row["policy_sha256"], clock=at,
                     paused=False, reason=None)
        state["policies"][row["policy_sha256"]] = policy
        return
    if kind in {"quota_pause", "quota_operation", "quota_send_result"}:
        if not state["policy"] or row.get("version") != 1 or row.get("account_ref") != (
            state["policy"]["account_ref"]
        ):
            raise ValueError("cumulative operation account")
        if kind == "quota_pause":
            if set(row) != {"kind", "version", "account_ref", "at", "observed_at", "reason",
                            "evidence_sha256", "previous"}:
                raise ValueError("invalid cumulative pause")
            if row["reason"] not in {"OWNER_PAUSE", "WINDOW_STOP", "CLOCK_ROLLBACK", "EXPIRED",
                                     "IDENTITY_CHANGED", "LOCAL_LIMIT_EXCEEDED"}:
                raise ValueError("invalid cumulative pause reason")
            instant(row["observed_at"])
            state.update(paused=True, reason=row["reason"])
        elif kind == "quota_operation":
            if set(row) != {"kind", "version", "account_ref", "at", "policy_sha256", "key",
                            "source_sha256", "liability", "previous"} or row[
                "policy_sha256"
            ] != state["policy_sha256"] or blocking_pause(state):
                raise ValueError("invalid cumulative operation")
            liability = _liability(row["liability"])
            if (not re.fullmatch(r"(?:input|send):[a-f0-9]{64}", row["key"])
                or not re.fullmatch(SHA, row["source_sha256"])
                or liability["at"] > row["at"]
                or row["key"] in state["operations"]):
                raise ValueError("cumulative operation replay")
            dimension = "owner_inputs" if row["key"].startswith("input:") else "send_attempts"
            if (liability[dimension] != 1
                or liability["state"] != ("ADMITTED" if dimension == "owner_inputs" else "RESERVED")
                or any(liability[k] for k in DIMENSIONS if k != dimension)):
                raise ValueError("invalid cumulative operation dimensions")
            state["operations"][row["key"]] = liability | {"source_sha256": row["source_sha256"]}
        else:
            if set(row) != {"kind", "version", "account_ref", "at", "key", "state", "previous"}:
                raise ValueError("invalid cumulative send result")
            prior = state["operations"].get(row["key"])
            if not prior or not row["key"].startswith("send:") or row["state"] not in {
                "SENT", "FAILED", "DELIVERY_UNCERTAIN",
            }:
                raise ValueError("unbound cumulative send result")
            if prior["state"] == "DELIVERY_UNCERTAIN" and row["state"] != "DELIVERY_UNCERTAIN":
                raise ValueError("uncertain send needs independent reconciliation")
            if prior["state"] in {"OWNER_SENT", "OWNER_CLOSED"}:
                raise ValueError("owner decision cannot be replaced by a late send result")
            prior["state"] = row["state"]
            if row["state"] == "DELIVERY_UNCERTAIN":
                state.update(paused=True, reason="DELIVERY_UNCERTAIN")
        at = instant(row["at"])
        if at != row["at"] or state["clock"] and at < state["clock"]:
            raise ValueError("cumulative clock moved backwards")
        state["clock"] = at
        return
    if not state["policy"]:
        if row.get("quota"):
            raise ValueError("cumulative account not enrolled")
        return
    if kind in {"reserve", "io_started", "settle", "blocked"}:
        key, quota = row["request_id"], row.get("quota")
        prior = state["models"].get(key)
        if quota is None:
            # A previously adopted legacy request may receive a late discrepancy.
            if not prior or kind not in {"settle", "blocked"}:
                raise ValueError("cumulative metadata cannot be removed")
            quota = dict(prior)
        else:
            quota = _liability(quota)
        if quota["micro_usd"] != row["minimum_micro_usd"] or quota["state"] != row["state"]:
            if row.get("quota") is not None:
                raise ValueError("cumulative model amount/state mismatch")
            quota.update(micro_usd=row["minimum_micro_usd"], state=row["state"])
        if quota["model_attempts"] != 1 or quota["owner_inputs"] or quota["send_attempts"]:
            raise ValueError("invalid model attempt dimensions")
        if kind == "reserve" and (prior or blocking_pause(state)):
            raise ValueError("cumulative model reservation replay or pause")
        if prior:
            if any(prior[k] != quota[k] for k in ("at", "window_id", "logical_request_id")):
                raise ValueError("model liability period changed")
            if prior["state"] == "UNKNOWN" and (
                quota["state"] != "UNKNOWN" or any(quota[k] < prior[k] for k in DIMENSIONS)
            ):
                raise ValueError("unknown liability cannot be refunded")
        if kind in {"reserve", "io_started"}:
            checked_at = instant(row.get("io_started_at") or quota["at"])
            if checked_at < state["clock"]:
                raise ValueError("cumulative model clock moved backwards")
            state["clock"] = checked_at
        state["models"][key] = quota
        if (row["state"] == "UNKNOWN" or kind == "blocked") and not blocking_pause(state):
            state.update(paused=True, reason="UNKNOWN_MODEL_LIABILITY")


def legacy_liabilities(store, provider, inventory=None):
    """Exact, bounded import inventory. Nothing is silently assigned to another account."""
    models, operations = {}, {}
    rows = store.rows('SELECT m.usage_id AS usage_id, m.request_id AS request_id, m.job_id AS '
        'job_id, m.scenario AS scenario, m.provider AS provider, m.model AS model, m.input_tokens '
        'AS input_tokens, m.cached_input_tokens AS cached_input_tokens, m.output_tokens AS '
        'output_tokens, m.billed_micro_usd AS billed_micro_usd, m.is_estimate AS is_estimate, '
        'm.occurred_at AS occurred_at, m.state AS state, m.pricing_known AS pricing_known, '
        'a.window_id AS window_id, a.logical_request_id AS logical_request_id FROM model_usage AS '
        'm LEFT JOIN model_admissions AS a ON m.request_id = a.request_id ORDER BY m.request_id '
        'NULLS FIRST')
    if len(rows) > 10000:
        raise CoreError("CUMULATIVE_LEGACY_INVENTORY_TOO_LARGE", 409)
    for row in rows:
        if (row["state"] != "SETTLED" or row["provider"] != provider
            or row["is_estimate"] not in {0, 1}):
            raise CoreError("CUMULATIVE_LEGACY_ATTRIBUTION_REQUIRED", 409)
        models[row["request_id"]] = liability(row["occurred_at"], row["window_id"] or "legacy",
            row["logical_request_id"] or row["request_id"], "SETTLED",
            micro_usd=row["billed_micro_usd"], model_attempts=1,
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"])
    inventory_items, records = {}, set()
    for item in inventory.reservations if inventory else []:
        if item.record_sha256 in records:
            raise CoreError("CUMULATIVE_LEGACY_WINDOW_ATTRIBUTION_REQUIRED", 409)
        records.add(item.record_sha256)
        inventory_items.setdefault((item.kind, item.source_sha256), []).append(item)
    approved = {}
    for row in store.rows('SELECT key, value FROM runtime_meta WHERE key LIKE '
        "'live_text_input:%%'"):
        item = json.loads(row["value"])
        history = store.one('SELECT value FROM runtime_meta WHERE key = %s',
                            ("live_text_window:" + item["window_sha256"],))
        if not history:
            raise CoreError("CUMULATIVE_LEGACY_INPUT_ATTRIBUTION_REQUIRED", 409)
        window = json.loads(history["value"])["binding"]["window_id"]
        attempts = inventory_items.pop(("input", digest(item["event_id"])), [])
        if len(attempts) != 1 or attempts[0].window_id != window:
            raise CoreError("CUMULATIVE_LEGACY_INPUT_TIMING_REQUIRED", 409)
        approved[item["event_id"]] = window
        operations["input:" + digest(item["event_id"])] = liability(
            attempts[0].at, window, item["event_id"], "ADMITTED", owner_inputs=1)
    for row in store.rows('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
        'origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS '
        'action_type, outbox.content_json AS content_json, outbox.content_sha256 AS '
        'content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, '
        'outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, '
        'outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, '
        'outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS '
        'last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, '
        'outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS '
        'next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS '
        'segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS '
        'reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, '
        'outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, '
        'outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.attempt_count '
        '> 0 ORDER BY outbox.message_id NULLS FIRST'):
        if (row["state"] not in {"SENT", "FAILED", "CANCELLED"}
            or row["origin_event_id"] not in approved):
            raise CoreError("CUMULATIVE_LEGACY_SEND_ATTRIBUTION_REQUIRED", 409)
        source = digest(row["message_id"])
        attempts = inventory_items.pop(("send", source), [])
        if len(attempts) != row["attempt_count"] or any(
            item.window_id != approved[row["origin_event_id"]] for item in attempts
        ):
            raise CoreError("CUMULATIVE_LEGACY_SEND_TIMING_REQUIRED", 409)
        for item in attempts:
            operations["legacy_send:" + source + ":" + item.record_sha256] = liability(
                item.at, item.window_id, row["origin_event_id"],
                "SENT" if row["state"] == "SENT" else "FAILED", send_attempts=1)
    if inventory_items:
        raise CoreError("CUMULATIVE_LEGACY_WINDOW_ATTRIBUTION_REQUIRED", 409)
    return {"models": models, "operations": operations}


def verify_legacy_inventory(store, settings, scope):
    inventory = scope.legacy_window_inventory
    if inventory is None:
        return
    live = settings.live_text_window
    journal_id = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
    if (not live or not live.capture_public_key or not journal_id
        or inventory.budget_journal_id != journal_id["value"]
        or inventory.identity_sha256 != scope.owner_identity_sha256):
        raise CoreError("CUMULATIVE_LEGACY_WINDOW_PROOF_REQUIRED", 403)
    value = inventory.model_dump(mode="json", exclude={"signature"})
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(live.capture_public_key)).verify(
            bytes.fromhex(inventory.signature), LEGACY_DOMAIN + encode(value).encode())
    except (ValueError, InvalidSignature):
        raise CoreError("CUMULATIVE_LEGACY_WINDOW_PROOF_REQUIRED", 403) from None


def liability(at, window, logical, state, **values):
    return {"at": instant(at), "window_id": window, "logical_request_id": logical,
            "state": state, **dict.fromkeys(DIMENSIONS, 0), **values}


def _scope(settings):
    return settings.paid_model.cumulative if settings.paid_model else None


def _identity(settings, scope):
    if (scope.provider != settings.provider or scope.currency != "USD"
        or scope.timezone != settings.timezone
        or scope.credential_sha256 != credential(settings, scope.account_ref)
        or scope.owner_identity_sha256 != digest([settings.owner_qq_id, settings.bot_qq_id])):
        raise CoreError("CUMULATIVE_ACCOUNT_BINDING_CHANGED", 403)


def activate(store, settings, now):
    """Append only an explicitly loaded policy at its exact reviewed journal cutoff."""
    from companion import paid_budget

    scope = _scope(settings)
    custody = paid_budget.journal(store, settings.paid_model, allow_settlement=True)
    state = custody["cumulative"]
    if scope is None:
        if state["policy"]:
            raise CoreError("CUMULATIVE_SCOPE_REQUIRED", 403)
        return
    _identity(settings, scope)
    value = scope.model_dump(mode="json")
    policy_sha = digest(value)
    if state["policy_sha256"] == policy_sha:
        return
    at = instant(now)
    if not instant(scope.starts_at) <= at < instant(scope.expires_at):
        raise CoreError("CUMULATIVE_POLICY_EXPIRED", 403)
    if (scope.expected_journal_head_sha256 != custody["head_sha256"]
        or scope.predecessor_policy_sha256 != state["policy_sha256"]
        or state["clock"] and at < state["clock"]
        or custody["lifecycle"] and custody["lifecycle"]["state"] != "STOPPED"):
        raise CoreError("CUMULATIVE_EXPLICIT_RENEWAL_REQUIRED", 409)
    record = {"kind": "quota_renew" if state["policy"] else "quota_enroll", "version": 1,
              "at": at, "policy": value, "policy_sha256": policy_sha}
    if not state["policy"]:
        verify_legacy_inventory(store, settings, scope)
        legacy = legacy_liabilities(store, scope.provider, scope.legacy_window_inventory)
        if digest(legacy) != scope.legacy_liabilities_sha256:
            raise CoreError("CUMULATIVE_LEGACY_INVENTORY_CHANGED", 409)
        record["legacy"] = legacy
    paid_budget.journal(store, settings.paid_model, record, allow_settlement=True)


def pause(store, custody, now, reason="OWNER_PAUSE", evidence_sha256=None):
    from companion import paid_budget

    state = paid_budget.journal(store, custody, allow_settlement=True)["cumulative"]
    if not state["policy"] or blocking_pause(state):
        return
    observed = instant(now) if now is not None else state["clock"]
    paid_budget.journal(store, custody, {"kind": "quota_pause", "version": 1,
        "account_ref": state["policy"]["account_ref"], "at": max(observed, state["clock"]),
        "observed_at": observed, "reason": reason, "evidence_sha256": evidence_sha256},
        allow_settlement=True)


def check(store, settings, now, *, custody=None):
    from companion import paid_budget

    scope = _scope(settings)
    if scope is None and custody is None and not store.one(
        'SELECT 1 FROM runtime_meta WHERE key = %s', (PROJECTION,),
    ):
        return empty_state()
    view = custody
    if view is None:
        approval = settings.paid_model
        if approval is None:
            projection = store.one('SELECT value FROM runtime_meta WHERE key = %s', (PROJECTION,))
            approval = SimpleNamespace(journal_path=json.loads(projection["value"])["journal_path"])
        view = paid_budget.journal(store, approval, allow_settlement=True)
    state = view["cumulative"]
    if scope is None and not state["policy"]:
        return state
    if scope is None or state["policy_sha256"] != digest(scope.model_dump(mode="json")):
        raise CoreError("CUMULATIVE_SCOPE_REQUIRED", 403)
    try:
        _identity(settings, scope)
    except CoreError:
        pause(store, settings.paid_model, now, "IDENTITY_CHANGED")
        raise
    at = instant(now)
    if state["clock"] and at < state["clock"]:
        pause(store, settings.paid_model, now, "CLOCK_ROLLBACK")
        raise CoreError("CUMULATIVE_CLOCK_ROLLBACK", 403)
    if not instant(scope.starts_at) <= at < instant(scope.expires_at):
        pause(store, settings.paid_model, now, "EXPIRED")
        raise CoreError("CUMULATIVE_POLICY_EXPIRED", 403)
    if blocking_pause(state):
        raise CoreError("CUMULATIVE_PAUSED", 403)
    return state


def preflight(store, settings, clock):
    """Record expiry/rollback before a narrower window validator can return early."""
    from companion import paid_budget

    if not store.in_transaction:
        return store.read_or_write(lambda: preflight(store, settings, clock))
    if not store.one('SELECT 1 FROM runtime_meta WHERE key = %s', (PROJECTION,)):
        return
    now = clock() if callable(clock) else clock
    projection = store.one('SELECT value FROM runtime_meta WHERE key = %s', (PROJECTION,))
    custody = SimpleNamespace(journal_path=json.loads(projection["value"])["journal_path"])
    view = paid_budget.journal(store, custody, allow_settlement=True)
    scope = _scope(settings)
    if scope and digest(scope.model_dump(mode="json")) != view["cumulative"]["policy_sha256"]:
        # Explicit new approvals still pass activate() at the reviewed cutoff.
        return
    check(store, settings, now, custody=view)
    paid = settings.paid_model
    # Cumulative scopes reject open-ended approvals at configuration time.
    if paid and not instant(paid.starts_at) <= instant(now) < instant(paid.expires_at):
        pause(store, custody, now, "EXPIRED")
        raise CoreError("CUMULATIVE_WINDOW_EXPIRED", 403)


def violations(state, item, *, replacing=None):
    policy = state["policy"]
    if policy is None:
        return []
    day, month = periods(item["at"], policy["timezone"])
    existing = {**{"model:" + k: v for k, v in state["models"].items()}, **state["operations"]}
    values = [v for k, v in existing.items() if k != replacing]
    values.append(item)
    result = []
    for name in ("request", "window", "day", "month", "account"):
        selected = [v for v in values if (
            name == "account" or name == "window" and v["window_id"] == item["window_id"]
            or name == "request" and v["logical_request_id"] == item["logical_request_id"]
            or name == "day" and periods(v["at"], policy["timezone"])[0] == day
            or name == "month" and periods(v["at"], policy["timezone"])[1] == month
        )]
        for dimension, cap in policy[name].items():
            if dimension != "micro_usd" and sum(v[dimension] for v in selected) > cap:
                result.append(name.upper() + "_" + dimension.upper())
    concurrent = sum(v["model_attempts"] for v in values if v["state"] == "RESERVED")
    if concurrent > policy["concurrency"]:
        result.append("CONCURRENCY")
    return result


def admit_model(store, settings, request_id, logical, at, amount, input_tokens, output_tokens):
    state = check(store, settings, at)
    if not state["policy"]:
        return None
    item = liability(at, settings.paid_model.window_id, logical, "RESERVED", micro_usd=amount,
                     model_attempts=1, input_tokens=input_tokens, output_tokens=output_tokens)
    if violations(state, item):
        raise CoreError("CUMULATIVE_LIMIT_EXHAUSTED", 429)
    if request_id in state["models"]:
        raise CoreError("MODEL_REQUEST_ALREADY_RESERVED", 409)
    return item


def admission_metadata(store, admission, *, amount=None, state=None, tokens=None):
    from companion import paid_budget

    if not store.one('SELECT 1 FROM runtime_meta WHERE key = %s', (PROJECTION,)):
        return None
    custody = SimpleNamespace(journal_path=admission["journal_path"])
    view = paid_budget.journal(store, custody, allow_settlement=True)["cumulative"]
    previous = view["models"].get(admission["request_id"])
    if previous is None:
        return None
    value = dict(previous)
    if amount is not None:
        value.update(micro_usd=amount, state=state)
    if tokens is not None:
        for name, count in zip(("input_tokens", "output_tokens"), tokens, strict=True):
            value[name] = max(value[name], count) if state == "UNKNOWN" else count
    return value


def final_fence(settings, state, now, request_id=None):
    scope = _scope(settings)
    if not state["policy"]:
        return
    at = instant(now)
    if (scope is None or digest(scope.model_dump(mode="json")) != state["policy_sha256"]
        or blocking_pause(state) or at < state["clock"]
        or not instant(scope.starts_at) <= at < instant(scope.expires_at)):
        raise CoreError("CUMULATIVE_DISPATCH_FENCED", 403)
    if request_id and periods(state["models"][request_id]["at"], scope.timezone) != periods(
        at, scope.timezone,
    ):
        raise CoreError("CUMULATIVE_DISPATCH_PERIOD_CHANGED", 403)


def admit_operation(store, settings, now, kind, source_id, source_sha256, *, at=None, logical=None):
    """Conservative, idempotent shadow of the existing input/outbox responsibility."""
    from companion import paid_budget

    state = check(store, settings, now)
    if not state["policy"]:
        return
    if kind not in {"input", "send"}:
        raise ValueError("invalid cumulative operation")
    key = kind + ":" + digest(source_id)
    previous = state["operations"].get(key)
    if previous:
        if (previous.get("source_sha256") != source_sha256
            or previous["window_id"] != settings.paid_model.window_id):
            raise CoreError("CUMULATIVE_OPERATION_CONFLICT", 409)
        return
    item = liability(at or now, settings.paid_model.window_id, logical or source_id,
        "ADMITTED" if kind == "input" else "RESERVED",
        **{"owner_inputs" if kind == "input" else "send_attempts": 1})
    if item["at"] > instant(now) or violations(state, item):
        raise CoreError("CUMULATIVE_LIMIT_EXHAUSTED", 429)
    paid_budget.journal(store, settings.paid_model, {
        "kind": "quota_operation", "version": 1, "at": instant(now),
        "account_ref": state["policy"]["account_ref"], "policy_sha256": state["policy_sha256"],
        "key": key, "source_sha256": source_sha256, "liability": item,
    })


def send_result(store, message_id, result, now):
    from companion import paid_budget

    projection = store.one('SELECT value FROM runtime_meta WHERE key = %s', (PROJECTION,))
    if not projection:
        return
    custody = SimpleNamespace(journal_path=json.loads(projection["value"])["journal_path"])
    state = paid_budget.journal(store, custody, allow_settlement=True)["cumulative"]
    key = "send:" + digest(message_id)
    previous = state["operations"].get(key)
    if not previous or previous["state"] == result:
        return
    paid_budget.journal(store, custody, {
        "kind": "quota_send_result", "version": 1,
        "account_ref": state["policy"]["account_ref"], "at": max(instant(now), state["clock"]),
        "key": key, "state": result,
    }, allow_settlement=True)


def status(store, settings, now):
    from companion import paid_budget

    projection = store.one('SELECT value FROM runtime_meta WHERE key = %s', (PROJECTION,))
    if not projection:
        return {"enrolled": False}
    try:
        custody = SimpleNamespace(journal_path=json.loads(projection["value"])["journal_path"])
        state = paid_budget.journal(store, custody, allow_settlement=True,
                                    initialize=False)["cumulative"]
    except (CoreError, ValueError, TypeError, KeyError) as error:
        return {"enrolled": True, "available": False,
                "code": getattr(error, "code", "MODEL_BUDGET_RECONCILIATION_REQUIRED")}
    policy = state["policy"]
    values = [*state["models"].values(), *state["operations"].values()]
    day, month = periods(instant(now), policy["timezone"])
    window = settings.paid_model.window_id if settings.paid_model else None
    totals = {}
    for name in ("window", "day", "month", "account"):
        selected = [v for v in values if (
            name == "account" or name == "window" and v["window_id"] == window
            or name == "day" and periods(v["at"], policy["timezone"])[0] == day
            or name == "month" and periods(v["at"], policy["timezone"])[1] == month)]
        totals[name] = {dimension: sum(v[dimension] for v in selected) for dimension in DIMENSIONS}
    return {"enrolled": True, "available": True, "account_ref": policy["account_ref"],
            "policy_sha256": state["policy_sha256"], "paused": blocking_pause(state),
            "warnings": ["MODEL_COST_UNKNOWN"] if any(
                value["state"] == "UNKNOWN" for value in state["models"].values()) else [],
            "reason": state["reason"], "expires_at": policy["expires_at"],
            "timezone": policy["timezone"], "currency": policy["currency"], "used": totals,
            "limits": {name: policy[name] for name in totals}}


def owner_pause(store, settings, clock, request):
    from companion import paid_budget

    with store.transaction():
        projection = store.one('SELECT value FROM runtime_meta WHERE key = %s', (PROJECTION,))
        if not projection:
            raise CoreError("CUMULATIVE_ACCOUNT_NOT_ENROLLED", 409)
        custody = SimpleNamespace(journal_path=json.loads(projection["value"])["journal_path"])
        state = paid_budget.journal(store, custody, allow_settlement=True)["cumulative"]
        if (request.account_ref != state["policy"]["account_ref"]
            or request.policy_sha256 != state["policy_sha256"]):
            raise CoreError("CUMULATIVE_ACCOUNT_BINDING_CHANGED", 409)
        pause(store, custody, clock(), evidence_sha256=request.evidence_sha256)
    return status(store, settings, clock())
