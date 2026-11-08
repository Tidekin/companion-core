"""One atomic CNY ceiling over native USD charges, with frozen conversion snapshots.

Amounts are configured planning estimates, not provider-guaranteed upper bounds.
Native model_usage/tool_tasks remain the accounting authority. These small attached
snapshots preserve conversion, liability and task identity across policy changes.
"""

import hashlib
import json
import re
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError, field_validator

from companion.contracts import CoreError, StrictModel
from companion.postgres import BUDGET_LEDGER
from companion.store import encode, utcnow


class CloudBudgetConfig(StrictModel):
    daily_micro_cny: int = Field(default=20_000_000, strict=True, ge=0, le=10**12)
    video_per_item_micro_cny: int = Field(default=1_000_000, strict=True, ge=0, le=10**12)
    asr_per_item_micro_cny: int = Field(default=500_000, strict=True, ge=0, le=10**12)
    usd_to_cny_micro: int = Field(default=8_000_000, strict=True, ge=1, le=100_000_000)
    fx_source: str = Field(default="operator_planning_estimate", min_length=1, max_length=256)
    fx_validity_seconds: int = Field(default=30 * 86400, strict=True, ge=60, le=366 * 86400)

    @field_validator("fx_source")
    @classmethod
    def source_label(cls, value):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("a nonblank FX source or planning-estimate label is required")
        return value.strip()


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise CoreError("CLOUD_BUDGET_TIME_REQUIRED", 400)
    return result.astimezone(UTC)


def _sha(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


@contextmanager
def _transaction(store):
    with nullcontext() if store.in_transaction else store.transaction():
        store.ledger_lock(BUDGET_LEDGER, shared=store.in_read_snapshot)
        yield


def _config(settings):
    value = getattr(settings, "cloud_budget", None) or CloudBudgetConfig()
    return (
        value if isinstance(value, CloudBudgetConfig) else CloudBudgetConfig.model_validate(value)
    )


def _view(row):
    return {
        "version": row["version"],
        "config": json.loads(row["config_json"]),
        "config_sha256": row["config_sha256"],
        "timezone": row["timezone"],
        "effective_at": row["effective_at"],
        "fx_valid_from": row["fx_valid_from"],
        "fx_valid_until": row["fx_valid_until"],
        "cost_basis": "configured_planning_estimates_not_provider_upper_bounds",
    }


def _insert_policy(store, config, version, timezone, now, *, fx_window=None):
    config = config.model_dump()
    starts = _time(now)
    valid_from, valid_until = fx_window or (
        starts.isoformat(timespec="microseconds"),
        (starts + timedelta(seconds=config["fx_validity_seconds"])).isoformat(
            timespec="microseconds"
        ),
    )
    store.insert(
        "cloud_budget_policies",
        version=version,
        config_json=encode(config),
        config_sha256=_sha(config),
        timezone=timezone,
        effective_at=starts.isoformat(timespec="microseconds"),
        fx_valid_from=valid_from,
        fx_valid_until=valid_until,
    )
    return store.one(
        "SELECT cloud_budget_policies.version AS version, "
        "cloud_budget_policies.config_json AS config_json, cloud_budget_policies.config_sha256 AS "
        "config_sha256, cloud_budget_policies.timezone AS timezone, "
        "cloud_budget_policies.effective_at AS effective_at, cloud_budget_policies.fx_valid_from "
        "AS fx_valid_from, cloud_budget_policies.fx_valid_until AS fx_valid_until FROM "
        "cloud_budget_policies AS cloud_budget_policies WHERE cloud_budget_policies.version = "
        "%s",
        (version,),
    )


def _policy(store, settings, now):
    row = store.one(
        "SELECT cloud_budget_policies.version AS version, "
        "cloud_budget_policies.config_json AS config_json, cloud_budget_policies.config_sha256 AS "
        "config_sha256, cloud_budget_policies.timezone AS timezone, "
        "cloud_budget_policies.effective_at AS effective_at, cloud_budget_policies.fx_valid_from "
        "AS fx_valid_from, cloud_budget_policies.fx_valid_until AS fx_valid_until FROM "
        "cloud_budget_policies AS cloud_budget_policies ORDER BY cloud_budget_policies.version "
        "DESC NULLS LAST LIMIT 1"
    )
    if row is None:
        # Bind the bookkeeping timezone once. Changing Settings cannot move costs
        # into another day or silently replace a persisted operational policy.
        ZoneInfo(settings.timezone)
        row = _insert_policy(store, _config(settings), 1, settings.timezone, now)
        store.audit(
            "cloud_budget.bound",
            "1",
            "cloud-budget",
            now,
            config_sha256=row["config_sha256"],
            fx_basis="configured_estimate",
        )
    return row


def _cny(amount, snapshot):
    return (amount * snapshot["config"]["usd_to_cny_micro"] + 999999) // 1000000


def _day(now, timezone):
    return _time(now).astimezone(ZoneInfo(timezone)).date().isoformat()


def _native(store, kind, reference_id):
    if kind == "model":
        row = store.one(
            "SELECT m.usage_id AS usage_id, m.request_id AS request_id, m.job_id AS job_id, "
            "m.scenario AS scenario, m.provider AS provider, m.model AS model, m.input_tokens AS "
            "input_tokens, m.cached_input_tokens AS cached_input_tokens, m.output_tokens AS "
            "output_tokens, m.billed_micro_usd AS billed_micro_usd, m.is_estimate AS is_estimate, "
            "m.occurred_at AS occurred_at, m.state AS state, m.pricing_known AS pricing_known, "
            "a.logical_request_id AS logical_request_id, a.reserved_micro_usd AS "
            "reserved_micro_usd, a.io_started_at AS io_started_at, a.snapshot_json AS "
            "snapshot_json FROM model_usage AS m LEFT JOIN model_admissions AS a ON m.request_id = "
            "a.request_id WHERE m.request_id = %s",
            (reference_id,),
        )
        if not row:
            return None
        task_kind = (
            "video" if row["scenario"] in {"content_video", "content_video_preview"} else "model"
        )
        task_id = row["logical_request_id"] or row["job_id"] or row["request_id"]
        if row["scenario"] == "media":
            media = store.one(
                "SELECT media_requests.request_id AS request_id, "
                "media_requests.session_id AS session_id, media_requests.request_key AS "
                "request_key, media_requests.fingerprint AS fingerprint, media_requests.operation "
                "AS operation, media_requests.state AS state, media_requests.reserved_bytes AS "
                "reserved_bytes, media_requests.usage_request_id AS usage_request_id, "
                "media_requests.event_id AS event_id, media_requests.created_at AS created_at FROM "
                "media_requests AS media_requests WHERE media_requests.usage_request_id = %s",
                (reference_id,),
            )
            if media and media["operation"] == "audio":
                task_kind, task_id = "asr", "media-session:" + media["session_id"]
        return {
            "amount": row["billed_micro_usd"],
            "state": row["state"],
            "reserved": row["reserved_micro_usd"] or row["billed_micro_usd"],
            "occurred_at": row["occurred_at"],
            "scenario": row["scenario"],
            "dispatched_at": row["io_started_at"],
            "task_kind": task_kind,
            "task_id": task_id,
            "pricing_known": bool(row["pricing_known"]),
        }
    row = store.one(
        "SELECT tool_tasks.task_id AS task_id, tool_tasks.idempotency_key AS "
        "idempotency_key, tool_tasks.request_sha256 AS request_sha256, tool_tasks.scope_sha256 AS "
        "scope_sha256, tool_tasks.executor_id AS executor_id, tool_tasks.capability AS capability, "
        "tool_tasks.scope_json AS scope_json, tool_tasks.risk_level AS risk_level, "
        "tool_tasks.state AS state, tool_tasks.captured_privacy_epoch AS captured_privacy_epoch, "
        "tool_tasks.lease_token AS lease_token, tool_tasks.lease_until AS lease_until, "
        "tool_tasks.reserved_micro_usd AS reserved_micro_usd, tool_tasks.billed_micro_usd AS "
        "billed_micro_usd, tool_tasks.dispatched_at AS dispatched_at, tool_tasks.result_event_id "
        "AS result_event_id, tool_tasks.last_error_code AS last_error_code, tool_tasks.created_at "
        "AS created_at, tool_tasks.updated_at AS updated_at, tool_tasks.redacted AS redacted FROM "
        "tool_tasks AS tool_tasks WHERE tool_tasks.task_id = %s",
        (reference_id,),
    )
    if not row or not row["dispatched_at"]:
        return None
    state = (
        "UNKNOWN"
        if row["state"] == "UNKNOWN"
        else "RESERVED"
        if row["state"] in {"LEASED", "RUNNING"}
        else "SETTLED"
    )
    if row["state"] == "CANCELLED":
        # Privacy erasure cancels completed and in-flight work alike. Only the
        # native validated completion or proof of no dispatch closes liability.
        completed = store.one(
            (
                (
                    'SELECT 1 FROM audit_log WHERE entity_id=%s AND '
                    "(action='tool.cancelled_before_dispatch' OR "
                    "(action='tool.finished' AND (CASE WHEN "
                    'safe_detail_json IS JSON THEN '
                    "(safe_detail_json::json#>>'{status}') END) IN "
                    "('SUCCEEDED','FAILED'))) LIMIT 1"
                )
            ),
            (reference_id,),
        )
        state = "SETTLED" if completed else "UNKNOWN"
    return {
        "amount": row["billed_micro_usd"],
        "state": state,
        "reserved": row["reserved_micro_usd"],
        "occurred_at": row["dispatched_at"],
        "dispatched_at": row["dispatched_at"],
        "scenario": "tool",
        "task_kind": "tool",
        "task_id": row["task_id"],
        "pricing_known": True,
    }


def _record(
    store,
    policy_row,
    kind,
    reference_id,
    amount,
    now,
    *,
    task_kind,
    task_id,
    pricing=None,
    legacy=False,
    state="RESERVED",
    reserved=None,
    dispatched_at=None,
):
    snapshot = {
        **_view(policy_row),
        "pricing": pricing,
        "conversion_provenance": "first_binding_baseline" if legacy else "reservation",
    }
    store.insert(
        "cloud_cost_snapshots",
        reference_type=kind,
        reference_id=reference_id,
        policy_version=policy_row["version"],
        task_kind=task_kind,
        task_id=task_id,
        local_day=_day(dispatched_at or now, policy_row["timezone"]),
        occurred_at=now,
        dispatched_at=dispatched_at,
        snapshot_json=encode(snapshot),
        reserved_micro_usd=amount if reserved is None else reserved,
        accounted_micro_usd=amount,
        amount_micro_cny=_cny(amount, snapshot),
        state=state,
        legacy=int(legacy),
        updated_at=now,
    )


def _native_settlement_receipt(store, reference_id, amount):
    """Recognize the existing independently validated video correction, not a DB flip.

    No generic ASR/tool/provider settlement protocol exists yet. Their UNKNOWNs need
    that native evidence path before this layer can safely release a carried hold.
    """
    from companion import paid_budget

    admission = store.one(
        "SELECT journal_path FROM model_admissions WHERE request_id = %s", (reference_id,)
    )
    audit = store.one(
        "SELECT safe_detail_json FROM audit_log WHERE action = "
        "'model.video_rate_correction' AND entity_id = %s",
        (reference_id,),
    )
    if not admission or not audit:
        return None
    try:
        details = json.loads(audit["safe_detail_json"])
        if (
            set(details) != {"receipt_sha256", "retained_micro_usd"}
            or details["retained_micro_usd"] != amount
        ):
            return None
        records = [
            json.loads(line)
            for line in Path(admission["journal_path"]).read_text("utf-8").splitlines()
        ]
        previous = "0" * 64
        receipt = None
        for record in records:
            material = {key: value for key, value in record.items() if key != "sha256"}
            if material["previous"] != previous or paid_budget.sha(material) != record["sha256"]:
                return None
            previous = record["sha256"]
            if (
                record.get("kind") == "video_rate_correction"
                and record["request_id"] == reference_id
            ):
                frozen = {key: value for key, value in material.items() if key != "previous"}
                if (
                    record["state"] == "SETTLED"
                    and record["minimum_micro_usd"] == amount
                    and paid_budget.sha(frozen) == details["receipt_sha256"]
                ):
                    receipt = details["receipt_sha256"]
        # Native validation checks original observed tokens, approval, journal
        # custody and exact correction binding. The independently read chain must
        # have the same validated head, including under concurrent file changes.
        custody = SimpleNamespace(journal_path=admission["journal_path"])
        validated = paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
        return receipt if validated["head_sha256"] == previous else None
    except (CoreError, OSError, ValueError, TypeError, KeyError):
        return None


def _liability(row, native, receipt=None):
    amount, state = native["amount"], native["state"]
    if receipt:
        return max(amount, row["accounted_micro_usd"]), state
    if state == "UNKNOWN" or row["state"] == "UNKNOWN":
        return max(
            amount, native["reserved"], row["accounted_micro_usd"], row["reserved_micro_usd"]
        ), "UNKNOWN"
    if state == "RESERVED":
        amount = max(amount, native["reserved"], row["reserved_micro_usd"])
    if row["state"] == "SETTLED" or state == "RESERVED":
        amount = max(amount, row["accounted_micro_usd"])
    return amount, state


def sync_reference(store, reference_type, reference_id):
    """Refresh a native charge in its caller's transaction, using only its old snapshot."""
    if not store.in_transaction:
        raise RuntimeError("CLOUD_BUDGET_TRANSACTION_REQUIRED")
    store.ledger_lock(BUDGET_LEDGER)
    row = store.one(
        "SELECT cloud_cost_snapshots.reference_type AS reference_type, "
        "cloud_cost_snapshots.reference_id AS reference_id, cloud_cost_snapshots.policy_version AS "
        "policy_version, cloud_cost_snapshots.task_kind AS task_kind, cloud_cost_snapshots.task_id "
        "AS task_id, cloud_cost_snapshots.local_day AS local_day, cloud_cost_snapshots.occurred_at "
        "AS occurred_at, cloud_cost_snapshots.dispatched_at AS dispatched_at, "
        "cloud_cost_snapshots.snapshot_json AS snapshot_json, "
        "cloud_cost_snapshots.reserved_micro_usd AS reserved_micro_usd, "
        "cloud_cost_snapshots.accounted_micro_usd AS accounted_micro_usd, "
        "cloud_cost_snapshots.amount_micro_cny AS amount_micro_cny, cloud_cost_snapshots.state AS "
        "state, cloud_cost_snapshots.legacy AS legacy, cloud_cost_snapshots.reconciliation_sha256 "
        "AS reconciliation_sha256, cloud_cost_snapshots.updated_at AS updated_at FROM "
        "cloud_cost_snapshots AS cloud_cost_snapshots WHERE cloud_cost_snapshots.reference_type = "
        "%s AND cloud_cost_snapshots.reference_id = %s",
        (reference_type, reference_id),
    )
    if row is None:
        return  # Pre-migration in-flight usage is captured by the next binding/sync.
    native = _native(store, reference_type, reference_id)
    if native is None:
        return  # Deleting a native projection cannot release an already booked liability.
    receipt = (
        _native_settlement_receipt(store, reference_id, native["amount"])
        if reference_type == "model" and row["state"] == "UNKNOWN" and native["state"] == "SETTLED"
        else None
    )
    amount, state = _liability(row, native, receipt)
    snapshot = json.loads(row["snapshot_json"])
    converted = _cny(amount, snapshot)
    # New tool snapshots mark the actual adapter boundary. Native begin() also
    # records a timestamp, but that alone does not prove an attempt was made.
    dispatched = (
        row["dispatched_at"]
        if reference_type == "tool" and not row["legacy"]
        else row["dispatched_at"] or native["dispatched_at"]
    )
    day = _day(dispatched or row["occurred_at"], snapshot["timezone"])
    reconciled = receipt or row["reconciliation_sha256"]
    if (amount, converted, state, reconciled, dispatched, day) == (
        row["accounted_micro_usd"],
        row["amount_micro_cny"],
        row["state"],
        row["reconciliation_sha256"],
        row["dispatched_at"],
        row["local_day"],
    ):
        return
    store.execute(
        "UPDATE cloud_cost_snapshots SET accounted_micro_usd = %s, amount_micro_cny = %s, state = "
        "%s, reconciliation_sha256 = COALESCE(%s, reconciliation_sha256), dispatched_at = %s, "
        "local_day = %s WHERE reference_type = %s AND reference_id = %s",
        (amount, converted, state, receipt, dispatched, day, reference_type, reference_id),
    )
    if receipt:
        store.audit(
            "cloud_budget.native_settled",
            reference_id,
            "cloud-budget",
            utcnow(),
            receipt_sha256=receipt,
            retained_micro_cny=converted,
        )


def _sync(store, settings, now):
    current = _policy(store, settings, now)
    # Tool UNKNOWN is sticky; CANCELLED needs the same native completion proof
    # as _native(). A cancellation alone is neither a new charge nor settlement.
    changed = store.rows(
        (
            'SELECT c.reference_type,c.reference_id FROM '
            'cloud_cost_snapshots c JOIN model_usage m ON '
            "c.reference_type='model' AND "
            'c.reference_id=m.request_id WHERE '
            'm.billed_micro_usd<>c.accounted_micro_usd OR '
            'm.state<>c.state UNION SELECT c.reference_type,'
            'c.reference_id FROM cloud_cost_snapshots c JOIN '
            "tool_tasks t ON c.reference_type='tool' AND "
            'c.reference_id=t.task_id WHERE '
            't.billed_micro_usd<>c.accounted_micro_usd OR '
            "(c.state='UNKNOWN' AND "
            't.reserved_micro_usd>c.accounted_micro_usd) OR '
            '(c.legacy=1 AND c.dispatched_at IS NULL AND '
            "t.dispatched_at IS NOT NULL) OR (c.state<>'UNKNOWN' "
            "AND (CASE WHEN t.state='UNKNOWN' OR "
            "(t.state='CANCELLED' AND NOT EXISTS (SELECT 1 FROM "
            'audit_log a WHERE a.entity_id=t.task_id AND '
            "(a.action='tool.cancelled_before_dispatch' OR "
            "(a.action='tool.finished' AND (CASE WHEN "
            'a.safe_detail_json IS JSON THEN '
            "(a.safe_detail_json::json#>>'{status}') END) IN "
            "('SUCCEEDED','FAILED'))))) THEN 'UNKNOWN' WHEN t.state "
            "IN ('LEASED','RUNNING') THEN 'RESERVED' ELSE 'SETTLED' "
            'END)<>c.state)'
        ),
    )
    for row in changed:
        sync_reference(store, row["reference_type"], row["reference_id"])
    missing = [
        ("model", row["request_id"])
        for row in store.rows(
            "SELECT request_id FROM model_usage AS m WHERE NOT EXISTS(SELECT 1 FROM "
            "cloud_cost_snapshots AS c WHERE c.reference_type = 'model' AND c.reference_id = "
            "m.request_id)",
        )
    ]
    missing += [
        ("tool", row["task_id"])
        for row in store.rows(
            (
                'SELECT task_id FROM tool_tasks AS t WHERE NOT '
                'dispatched_at IS NULL AND NOT EXISTS(SELECT 1 FROM '
                'cloud_cost_snapshots AS c WHERE c.reference_type = '
                "'tool' AND c.reference_id = t.task_id)"
            ),
        )
    ]
    for kind, reference_id in missing:
        native = _native(store, kind, reference_id)
        amount = (
            max(native["amount"], native["reserved"])
            if native["state"] in {"RESERVED", "UNKNOWN"}
            else native["amount"]
        )
        _record(
            store,
            current,
            kind,
            reference_id,
            amount,
            native["occurred_at"],
            task_kind=native["task_kind"],
            task_id=native["task_id"],
            legacy=True,
            state=native["state"],
            reserved=native["reserved"],
            dispatched_at=native["dispatched_at"],
            pricing={"pricing_known": native["pricing_known"]},
        )
    return current


def sync(store, settings, now=None):
    with _transaction(store):
        return _view(_sync(store, settings, now or utcnow()))


def policy(store, settings, now=None):
    """Return the persisted policy; first binding also snapshots existing liabilities."""
    return sync(store, settings, now)


def configure(
    store,
    settings,
    request,
    *,
    expected_version=None,
    idempotency_key,
    now,
    owner_check=None,
    clock=None,
):
    """Apply {expected_version, changes}, or a full typed configuration, once."""
    changes = None
    if isinstance(request, dict) and "changes" in request:
        if (
            set(request) != {"expected_version", "changes"}
            or not isinstance(request["changes"], dict)
            or not request["changes"]
            or (expected_version is not None and expected_version != request["expected_version"])
        ):
            raise CoreError("CLOUD_BUDGET_CHANGE_INVALID", 400)
        expected_version, changes = request["expected_version"], request["changes"]
        if set(changes) - CloudBudgetConfig.model_fields.keys():
            raise CoreError("CLOUD_BUDGET_CONFIG_INVALID", 400)
        material = {"changes": changes, "expected_version": expected_version}
    else:
        try:
            config = (
                request
                if isinstance(request, CloudBudgetConfig)
                else CloudBudgetConfig.model_validate(request)
            )
        except ValidationError as error:
            raise CoreError("CLOUD_BUDGET_CONFIG_INVALID", 400) from error
        material = {"config": config.model_dump(), "expected_version": expected_version}
    if (
        type(expected_version) is not int
        or expected_version < 0
        or not isinstance(idempotency_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idempotency_key)
    ):
        raise CoreError("CLOUD_BUDGET_CHANGE_INVALID", 400)
    fingerprint = _sha(material)
    with _transaction(store):
        if owner_check is not None:
            owner_check()
        if clock is not None:
            now = clock()
        receipt = store.one(
            "SELECT cloud_budget_changes.idempotency_key AS idempotency_key, "
            "cloud_budget_changes.request_sha256 AS request_sha256, cloud_budget_changes.version "
            "AS version, cloud_budget_changes.created_at AS created_at FROM cloud_budget_changes "
            "AS cloud_budget_changes WHERE cloud_budget_changes.idempotency_key = %s",
            (idempotency_key,),
        )
        if receipt:
            if receipt["request_sha256"] != fingerprint:
                raise CoreError("IDEMPOTENCY_CONFLICT", 409)
            return _view(
                store.one(
                    (
                        'SELECT cloud_budget_policies.version AS version, '
                        'cloud_budget_policies.config_json AS config_json, '
                        'cloud_budget_policies.config_sha256 AS config_sha256, '
                        'cloud_budget_policies.timezone AS timezone, '
                        'cloud_budget_policies.effective_at AS effective_at, '
                        'cloud_budget_policies.fx_valid_from AS fx_valid_from, '
                        'cloud_budget_policies.fx_valid_until AS fx_valid_until '
                        'FROM cloud_budget_policies AS cloud_budget_policies '
                        'WHERE cloud_budget_policies.version = %s'
                    ),
                    (receipt["version"],),
                )
            )
        # Capture history under the previous FX before writing the new policy.
        current = _sync(store, settings, now)
        if current["version"] != expected_version:
            raise CoreError("CLOUD_BUDGET_VERSION_CONFLICT", 409)
        if _time(now) < _time(current["effective_at"]):
            raise CoreError("CLOUD_BUDGET_TIME_REVERSED", 409)
        if changes is not None:
            try:
                config = CloudBudgetConfig.model_validate(
                    {**json.loads(current["config_json"]), **changes}
                )
            except ValidationError as error:
                raise CoreError("CLOUD_BUDGET_CONFIG_INVALID", 400) from error
        renew_fx = changes is None or bool(
            set(changes)
            & {
                "usd_to_cny_micro",
                "fx_source",
                "fx_validity_seconds",
            }
        )
        changed = _insert_policy(
            store,
            config,
            current["version"] + 1,
            current["timezone"],
            now,
            fx_window=None if renew_fx else (current["fx_valid_from"], current["fx_valid_until"]),
        )
        store.insert(
            "cloud_budget_changes",
            idempotency_key=idempotency_key,
            request_sha256=fingerprint,
            version=changed["version"],
            created_at=now,
        )
        store.audit(
            "cloud_budget.configured",
            str(changed["version"]),
            idempotency_key,
            now,
            previous_version=current["version"],
            config_sha256=changed["config_sha256"],
        )
        return _view(changed)


def _task_cap(config, kind):
    return config.get(
        {"video": "video_per_item_micro_cny", "asr": "asr_per_item_micro_cny"}.get(kind, "")
    )


def _unpriced_unknown(store):
    return store.one(
        (
            'SELECT COUNT(*) n FROM cloud_cost_snapshots WHERE '
            "state='UNKNOWN' AND amount_micro_cny=0 AND "
            "reference_type='model' AND COALESCE((CASE WHEN "
            'snapshot_json IS JSON THEN CASE '
            "(snapshot_json::json#>>'{pricing,pricing_known}') WHEN "
            "'true' THEN 1 WHEN 'false' THEN 0 WHEN '1' THEN 1 WHEN "
            "'0' THEN 0 END END),0)=0"
        ),
    )["n"]


def _restore_required(store):
    return bool(store.one("SELECT 1 FROM runtime_meta WHERE key = 'cloud_budget_restore_required'"))


def mark_dispatched(store, reference_type, reference_id, now):
    """Fix the actual dispatch day once, immediately before the native adapter I/O."""
    if not store.in_transaction:
        raise RuntimeError("CLOUD_BUDGET_TRANSACTION_REQUIRED")
    store.ledger_lock(BUDGET_LEDGER)
    row = store.one(
        "SELECT cloud_cost_snapshots.reference_type AS reference_type, "
        "cloud_cost_snapshots.reference_id AS reference_id, cloud_cost_snapshots.policy_version AS "
        "policy_version, cloud_cost_snapshots.task_kind AS task_kind, cloud_cost_snapshots.task_id "
        "AS task_id, cloud_cost_snapshots.local_day AS local_day, cloud_cost_snapshots.occurred_at "
        "AS occurred_at, cloud_cost_snapshots.dispatched_at AS dispatched_at, "
        "cloud_cost_snapshots.snapshot_json AS snapshot_json, "
        "cloud_cost_snapshots.reserved_micro_usd AS reserved_micro_usd, "
        "cloud_cost_snapshots.accounted_micro_usd AS accounted_micro_usd, "
        "cloud_cost_snapshots.amount_micro_cny AS amount_micro_cny, cloud_cost_snapshots.state AS "
        "state, cloud_cost_snapshots.legacy AS legacy, cloud_cost_snapshots.reconciliation_sha256 "
        "AS reconciliation_sha256, cloud_cost_snapshots.updated_at AS updated_at FROM "
        "cloud_cost_snapshots AS cloud_cost_snapshots WHERE cloud_cost_snapshots.reference_type = "
        "%s AND cloud_cost_snapshots.reference_id = %s",
        (reference_type, reference_id),
    )
    if row is None:
        raise CoreError("CLOUD_BUDGET_RESERVATION_REQUIRED", 503)
    if row["state"] != "RESERVED" or row["dispatched_at"] is not None:
        raise CoreError("CLOUD_BUDGET_ALREADY_DISPATCHED", 409)
    snapshot = json.loads(row["snapshot_json"])
    store.execute(
        "UPDATE cloud_cost_snapshots SET dispatched_at = %s, local_day = %s WHERE "
        "reference_type = %s AND reference_id = %s",
        (now, _day(now, snapshot["timezone"]), reference_type, reference_id),
    )


def reserve(
    store,
    settings,
    *,
    reference_type,
    reference_id,
    estimated_micro_usd,
    now,
    task_kind=None,
    task_id=None,
    pricing=None,
):
    """Keep the native charge and cloud reservation under one transaction and ledger lock."""
    if not store.in_transaction:
        raise RuntimeError("CLOUD_BUDGET_TRANSACTION_REQUIRED")
    store.ledger_lock(BUDGET_LEDGER)
    if (
        reference_type not in {"model", "tool"}
        or not isinstance(reference_id, str)
        or not 1 <= len(reference_id) <= 256
        or type(estimated_micro_usd) is not int
        or not 0 <= estimated_micro_usd <= 10**12
    ):
        raise CoreError("CLOUD_BUDGET_RESERVATION_INVALID", 400)
    task_kind = task_kind or reference_type
    task_id = task_id or reference_id
    if (
        task_kind not in {"model", "tool", "video", "asr"}
        or not isinstance(task_id, str)
        or not 1 <= len(task_id) <= 512
    ):
        raise CoreError("CLOUD_BUDGET_TASK_INVALID", 400)
    if store.one(
        "SELECT 1 FROM cloud_cost_snapshots WHERE reference_type = %s AND reference_id = %s",
        (reference_type, reference_id),
    ):
        raise CoreError("CLOUD_BUDGET_DUPLICATE_CHARGE", 409)
    current = _policy(store, settings, now)
    view = _view(current)
    converted = _cny(estimated_micro_usd, view)
    day = _day(now, current["timezone"])
    _record(
        store,
        current,
        reference_type,
        reference_id,
        estimated_micro_usd,
        now,
        task_kind=task_kind,
        task_id=task_id,
        pricing=pricing,
    )
    return {
        "reference_type": reference_type,
        "reference_id": reference_id,
        "policy_version": current["version"],
        "reserved_micro_cny": converted,
        "local_day": day,
    }


def model_pricing(store, request_id, fallback):
    """Use captured USD rates for non-paid/mock native settlement too."""
    row = store.one(
        "SELECT snapshot_json FROM cloud_cost_snapshots WHERE reference_type = 'model' "
        "AND reference_id = %s",
        (request_id,),
    )
    rates = json.loads(row["snapshot_json"]).get("pricing") if row else None
    keys = ("pricing_known", "input_rate", "cached_input_rate", "output_rate")
    return (
        SimpleNamespace(**{key: rates[key] for key in keys})
        if rates and all(key in rates for key in keys)
        else fallback
    )



def capacity_blocks(store, settings, now):
    """Cost estimates and unknown charges are warnings, never capability gates."""
    return []


def summary(store, settings, now=None):
    now = now or utcnow()
    with _transaction(store):
        current = _sync(store, settings, now)
        view = _view(current)
        day = _day(now, current["timezone"])
        rows = store.rows(
            (
                "SELECT state,COUNT(*) n,SUM(amount_micro_cny) amount "
                "FROM cloud_cost_snapshots WHERE local_day=%s OR state "
                "IN ('RESERVED','UNKNOWN') GROUP BY state"
            ),
            (day,),
        )
        states = {row["state"]: row for row in rows}
        amounts = {
            name: int(states.get(name, {}).get("amount", 0))
            for name in ("RESERVED", "SETTLED", "UNKNOWN")
        }
        used = sum(amounts.values())
        carried = store.one(
            (
                "SELECT COALESCE(SUM(amount_micro_cny),0) n FROM "
                "cloud_cost_snapshots WHERE local_day<>%s AND state IN "
                "('RESERVED','UNKNOWN')"
            ),
            (day,),
        )["n"]
        carried = int(carried)
        unpriced = _unpriced_unknown(store)
        restore_required = _restore_required(store)
        task_overruns = store.one(
            (
                'SELECT COUNT(*) n FROM (SELECT task_kind,task_id FROM '
                "cloud_cost_snapshots WHERE task_kind IN ('video',"
                "'asr') GROUP BY task_kind,task_id HAVING "
                "SUM(amount_micro_cny)>MIN(CASE task_kind WHEN 'video' "
                'THEN (CASE WHEN snapshot_json IS JSON THEN '
                "((snapshot_json::json#>>'{config,"
                "video_per_item_micro_cny}'))::numeric END) ELSE (CASE "
                'WHEN snapshot_json IS JSON THEN '
                "((snapshot_json::json#>>'{config,"
                "asr_per_item_micro_cny}'))::numeric END) END))"
            ),
        )["n"]
        return {
            **view,
            "currency": "CNY",
            "local_day": day,
            "used_micro_cny": used,
            "reserved_micro_cny": amounts["RESERVED"],
            "settled_micro_cny": amounts["SETTLED"],
            "unknown_micro_cny": amounts["UNKNOWN"],
            "remaining_micro_cny": (
                0 if restore_required else max(0, view["config"]["daily_micro_cny"] - used)
            ),
            "over_limit": used > view["config"]["daily_micro_cny"],
            "carried_micro_cny": carried,
            "unpriced_unknown_count": unpriced,
            "restore_reconciliation_required": restore_required,
            "reconciliation_required": restore_required,
            "reconciliation_reasons": (
                ["CLOUD_BUDGET_RESTORE_RECONCILIATION_REQUIRED"] if restore_required else []
            ),
            "restored_history_may_be_incomplete": restore_required,
            "task_over_limit_count": task_overruns,
            "admission_blocked": restore_required
            or bool(unpriced)
            or used >= view["config"]["daily_micro_cny"]
            or _time(now) >= _time(current["fx_valid_until"]),
            "fx_expired": _time(now) >= _time(current["fx_valid_until"]),
            "charge_count": sum(row["n"] for row in rows),
        }
