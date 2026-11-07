"""Local execution closure and explicitly approved retained UNKNOWN responsibility.

These control records share the original budget journal. They never settle or
refund a model attempt and never authorize replaying its original input or tool.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from companion import paid_budget
from companion.contracts import CoreError
from companion.store import encode
from companion_delivery_protocol import digest, hash_value, identifier, timestamp
from companion_delivery_protocol import model_carry as wire

CLOSE = "model_local_close"
CARRY = "model_unknown_carry"
RENEW = "model_approval_renew"
KINDS = {CLOSE, CARRY, RENEW}
CLOSE_FIELDS = {
    "kind",
    "version",
    "request_id",
    "authority",
    "contract_sha256",
    "payload_sha256",
    "lease_sha256",
    "io_started_at",
    "settlement_sha256",
    "closed_at",
    "previous",
}
CARRY_FIELDS = {
    "kind",
    "version",
    "operation_id",
    "authorization_sha256",
    "manifest",
    "carry_sha256",
    "successor_policy_sha256",
    "successor_paid_contract_sha256",
    "successor_approval_sha256",
    "concurrency_basis",
    "concurrency",
    "closed_executors",
    "at",
    "request_sha256",
    "request_fingerprint_sha256",
    "previous",
}


def active(settings):
    return bool(settings.paid_model and settings.paid_model.concurrency_basis == wire.MODE)


def supported(store, settings):
    # Cumulative v2 is a separate explicitly reviewed capability. Never bypass v1.
    if settings.paid_model.cumulative is not None or store.one(
        "SELECT 1 FROM runtime_meta WHERE key = 'model_budget_cumulative'"
    ):
        raise CoreError("MODEL_CARRY_CUMULATIVE_UNSUPPORTED", 503)


def _tuple(admission):
    return {
        "request_id": admission["request_id"],
        "authority": json.loads(admission["authority_json"]),
        "contract_sha256": admission["contract_sha256"],
        "payload_sha256": admission["payload_sha256"],
        "lease_sha256": digest(admission["lease_token"]),
        "io_started_at": admission["io_started_at"],
    }


def _validate_close(store, row, latest):
    admission = store.one('SELECT model_admissions.request_id AS request_id, '
        'model_admissions.logical_request_id AS logical_request_id, model_admissions.attempt_id AS '
        'attempt_id, model_admissions.attempt_number AS attempt_number, model_admissions.window_id '
        'AS window_id, model_admissions.contract_sha256 AS contract_sha256, '
        'model_admissions.payload_sha256 AS payload_sha256, model_admissions.snapshot_json AS '
        'snapshot_json, model_admissions.reserved_micro_usd AS reserved_micro_usd, '
        'model_admissions.input_bound AS input_bound, model_admissions.output_bound AS '
        'output_bound, model_admissions.lease_token AS lease_token, model_admissions.lease_until '
        'AS lease_until, model_admissions.io_started_at AS io_started_at, '
        'model_admissions.journal_path AS journal_path, model_admissions.journal_id AS journal_id, '
        'model_admissions.measurement_json AS measurement_json, model_admissions.authority_json AS '
        'authority_json FROM model_admissions AS model_admissions WHERE '
        'model_admissions.request_id = %s', (row["request_id"],))
    financial = latest.get(row["request_id"])
    if (
        not admission
        or not financial
        or financial["kind"] != "settle"
        or financial["state"] != "UNKNOWN"
        or row["settlement_sha256"] != paid_budget.sha(financial)
        or any(row[k] != v for k, v in _tuple(admission).items())
        or not (_unstarted(admission, financial) if not admission["io_started_at"]
                else timestamp(row["closed_at"]) >= timestamp(admission["io_started_at"]))
    ):
        raise ValueError("model local closure binding")
    if json.loads(admission["snapshot_json"]).get("concurrency_basis") != wire.MODE:
        raise ValueError("local closure requires explicit new approval")


def _unstarted(admission, financial):
    # Provider I/O is journaled (`io_started`) before any HTTP request; the settle record
    # copies the admission's io_started_at. Both absent proves nothing was sent.
    return (admission["io_started_at"] is None and "io_started_at" in financial
            and financial["io_started_at"] is None)


def close_unstarted(store, settings, request_id, now, *, commit):
    """Close one UNKNOWN whose original journal proves provider I/O never began.

    Background yield and similar refusals before `io_started` used to settle such an
    attempt UNKNOWN. It still occupies the single execution slot and blocks every
    resume. The closure frees the slot only: the outcome stays UNKNOWN at its retained
    amount, every budget keeps counting it, and its logical request can never retry.
    Offline under STOP, with the worker locks held by the caller.
    """
    from companion import owner_authority

    if not active(settings):
        raise CoreError("MODEL_LOCAL_CLOSE_REQUIRES_V2", 409)
    supported(store, settings)
    with store.transaction():
        head = owner_authority._latest(store)
        if not head or head["state"] != "STOPPED":
            raise CoreError("OWNER_AUTHORITY_STOP_REQUIRED", 409)
        admission = store.one('SELECT model_admissions.request_id AS request_id, '
            'model_admissions.logical_request_id AS logical_request_id, '
            'model_admissions.attempt_id AS attempt_id, model_admissions.attempt_number AS '
            'attempt_number, model_admissions.window_id AS window_id, '
            'model_admissions.contract_sha256 AS contract_sha256, model_admissions.payload_sha256 '
            'AS payload_sha256, model_admissions.snapshot_json AS snapshot_json, '
            'model_admissions.reserved_micro_usd AS reserved_micro_usd, '
            'model_admissions.input_bound AS input_bound, model_admissions.output_bound AS '
            'output_bound, model_admissions.lease_token AS lease_token, '
            'model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS '
            'io_started_at, model_admissions.journal_path AS journal_path, '
            'model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS '
            'measurement_json, model_admissions.authority_json AS authority_json FROM '
            'model_admissions AS model_admissions WHERE model_admissions.request_id = '
            '%s', (request_id,))
        usage = store.one('SELECT state FROM model_usage WHERE request_id = %s', (request_id,))
        if not admission or not usage or usage["state"] != "UNKNOWN":
            raise CoreError("MODEL_UNSTARTED_CLOSE_NOT_ELIGIBLE", 409)
        custody = SimpleNamespace(journal_path=admission["journal_path"])
        view = paid_budget.journal(store, custody, allow_settlement=True)
        financial = view["model_latest"].get(request_id)
        raw = Path(admission["journal_path"]).read_bytes()
        started = any(
            record.get("kind") == "io_started" and record.get("request_id") == request_id
            for record in map(json.loads, filter(None, raw.splitlines()))
        )
        if (started or not financial or financial["kind"] != "settle"
                or financial["state"] != "UNKNOWN" or not _unstarted(admission, financial)
                or request_id in view["blocked_request_ids"]
                or request_id in view["model_closures"]):
            raise CoreError("MODEL_UNSTARTED_CLOSE_NOT_ELIGIBLE", 409)
        if json.loads(admission["snapshot_json"]).get("concurrency_basis") != wire.MODE:
            raise CoreError("MODEL_LOCAL_CLOSE_REQUIRES_V2", 409)
        if store.one(
            "SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked', "
            "'model_budget_restore_required', 'cloud_budget_restore_required')"
        ):
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        record = {
            "kind": CLOSE,
            "version": 1,
            **_tuple(admission),
            "settlement_sha256": paid_budget.sha(financial),
            "closed_at": now,
        }
        if commit:
            paid_budget.journal(store, custody, record, allow_settlement=True)
    return {"request_id": request_id, "committed": commit,
            "retained_micro_usd": financial["minimum_micro_usd"], "record": record}


def fold(store, row, record_sha, journal_id, authorities, latest, blocked, closures, carries):
    """Validate control evidence at its original journal cutoff, never as a charge."""
    if type(row.get("version")) is not int or row["version"] != 1:
        raise ValueError("model control record version")
    if row["kind"] == RENEW:
        from companion import model_renewal

        return model_renewal.fold(
            store, row, record_sha, journal_id, authorities, latest, blocked, closures, carries,
        )
    if row["kind"] == CLOSE:
        if set(row) != CLOSE_FIELDS or row["request_id"] in blocked:
            raise ValueError("invalid model local closure")
        _validate_close(store, row, latest)
        if row["request_id"] in closures:
            raise ValueError("duplicate model local closure")
        closures[row["request_id"]] = record_sha
        return
    if set(row) != CARRY_FIELDS or not hash_value(row["authorization_sha256"]):
        raise ValueError("invalid model carry record")
    manifest = row["manifest"]
    if (
        digest(manifest) != row["carry_sha256"]
        or row["carry_sha256"] in carries
        or manifest["budget_journal_id"] != journal_id
        or blocked
        or not authorities
        or authorities[-1]["state"] != "STOPPED"
        or authorities[-1]["record_sha256"] != manifest["core_stop_sha256"]
        or manifest["closed_executors_sha256"] != digest(row["closed_executors"])
    ):
        raise ValueError("model carry custody")
    unknown = {k for k, v in latest.items() if v["state"] == "UNKNOWN"}
    items = manifest["unknowns"]
    if (
        not 1 <= len(items) <= 100
        or [i["request_id"] for i in items] != sorted(unknown)
        or any(v["state"] == "RESERVED" for v in latest.values())
    ):
        raise ValueError("model carry incomplete inventory")
    for item in items:
        admission = store.one(
            'SELECT model_admissions.request_id AS request_id, model_admissions.logical_request_id '
            'AS logical_request_id, model_admissions.attempt_id AS attempt_id, '
            'model_admissions.attempt_number AS attempt_number, model_admissions.window_id AS '
            'window_id, model_admissions.contract_sha256 AS contract_sha256, '
            'model_admissions.payload_sha256 AS payload_sha256, model_admissions.snapshot_json AS '
            'snapshot_json, model_admissions.reserved_micro_usd AS reserved_micro_usd, '
            'model_admissions.input_bound AS input_bound, model_admissions.output_bound AS '
            'output_bound, model_admissions.lease_token AS lease_token, '
            'model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS '
            'io_started_at, model_admissions.journal_path AS journal_path, '
            'model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS '
            'measurement_json, model_admissions.authority_json AS authority_json FROM '
            'model_admissions AS model_admissions WHERE model_admissions.request_id = '
            '%s', (item["request_id"],)
        )
        financial = latest[item["request_id"]]
        if (
            not admission
            or item["settlement_sha256"] != paid_budget.sha(financial)
            or financial["kind"] != "settle"
            or item["minimum_micro_usd"] != financial["minimum_micro_usd"]
            or item["reserved_micro_usd"] != admission["reserved_micro_usd"]
            or any(item[k] != v for k, v in _tuple(admission).items())
            or item["authority"] != manifest["authority"]
        ):
            raise ValueError("model carry original admission")
        closures[item["request_id"]] = record_sha
    if (
        row["concurrency_basis"] != wire.MODE
        or type(row["concurrency"]) is not int
        or not 1 <= row["concurrency"] <= 10
        or any(
            not hash_value(row[k])
            for k in (
                "successor_policy_sha256",
                "successor_paid_contract_sha256",
                "successor_approval_sha256",
            )
        )
    ):
        raise ValueError("model carry successor approval")
    carries[row["carry_sha256"]] = {**row, "record_sha256": record_sha}


def validate_projection(store, controls, *, replay=None):
    rows = store.rows("SELECT key, value FROM runtime_meta WHERE key LIKE 'model_control:%%'")
    saved = {row["key"].removeprefix("model_control:"): row["value"] for row in rows}
    if (
        any(key != value for key, value in saved.items())
        or set(saved) - set(controls)
        or set(controls) - set(saved) - ({replay} if replay else set())
    ):
        raise ValueError("model control DB behind journal")


def close_owned(store, settings, request_id, dispatch, now):
    """Only this committed admission owner may close its own unwound transport."""
    if not active(settings) or not dispatch.get("owned") or not dispatch.get("transport_returned"):
        return
    supported(store, settings)
    with store.transaction():
        admission = store.one('SELECT model_admissions.request_id AS request_id, '
            'model_admissions.logical_request_id AS logical_request_id, '
            'model_admissions.attempt_id AS attempt_id, model_admissions.attempt_number AS '
            'attempt_number, model_admissions.window_id AS window_id, '
            'model_admissions.contract_sha256 AS contract_sha256, model_admissions.payload_sha256 '
            'AS payload_sha256, model_admissions.snapshot_json AS snapshot_json, '
            'model_admissions.reserved_micro_usd AS reserved_micro_usd, '
            'model_admissions.input_bound AS input_bound, model_admissions.output_bound AS '
            'output_bound, model_admissions.lease_token AS lease_token, '
            'model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS '
            'io_started_at, model_admissions.journal_path AS journal_path, '
            'model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS '
            'measurement_json, model_admissions.authority_json AS authority_json FROM '
            'model_admissions AS model_admissions WHERE model_admissions.request_id = '
            '%s', (request_id,))
        usage = store.one('SELECT state FROM model_usage WHERE request_id = %s', (request_id,))
        if not admission or not usage or usage["state"] != "UNKNOWN":
            return
        if dispatch.get("owner_tuple") != _tuple(admission) or admission[
            "io_started_at"
        ] != dispatch.get("io_started_at"):
            raise CoreError("MODEL_LOCAL_CLOSE_OWNER_CHANGED", 503)
        custody = SimpleNamespace(journal_path=admission["journal_path"])
        view = paid_budget.journal(store, custody, allow_settlement=True)
        if view["blocked_request_ids"] or request_id in view["model_closures"]:
            return
        if store.one(
            "SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked', "
            "'model_budget_restore_required', 'cloud_budget_restore_required')"
        ):
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        record = {
            "kind": CLOSE,
            "version": 1,
            **_tuple(admission),
            "settlement_sha256": paid_budget.sha(view["model_latest"][request_id]),
            "closed_at": now,
        }
        paid_budget.journal(store, custody, record, allow_settlement=True)


def _work(store, settings, authority, model_requests=None):
    from companion import owner_input_state
    from companion.owner_authority_protocol import OwnerCapture

    result = {}
    for row in store.rows("SELECT value FROM runtime_meta WHERE key LIKE 'owner_input:%%'"):
        value = json.loads(row["value"])
        capture = value.get("capture")
        if not capture or capture["authority"] != authority:
            continue
        models = []
        state = owner_input_state.read(
            store, settings, OwnerCapture.model_validate(capture), model_requests=models
        )
        if state["unsettled_work"] or state["unsettled_delivery"]:
            raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
        result[capture["event_id"]] = state
        if model_requests is not None:
            model_requests[capture["event_id"]] = models
    return result


def _quiescent(store, view):
    if view["blocked_request_ids"] or store.one(
        "SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked', "
        "'model_budget_restore_required', 'cloud_budget_restore_required')"
    ):
        raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
    for table, states in (
        ("jobs", "'SUCCEEDED','FAILED','CANCELLED'"),
        ("outbox", "'SENT','FAILED','CANCELLED'"),
        ("tool_tasks", "'SUCCEEDED','FAILED','CANCELLED'"),
    ):
        if store.one(f"SELECT 1 FROM {table} WHERE state NOT IN ({states}) LIMIT 1"):
            raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
    if store.one("SELECT 1 FROM model_usage WHERE state = 'RESERVED'"):
        raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)


def manifest(store, settings, authority, successor_window, witness, view):
    from companion import live_text, owner_authority, owner_stop_receipt

    supported(store, settings)
    stop = owner_stop_receipt.read(store, settings, authority)
    if store.one('SELECT 1 FROM model_budget_windows WHERE window_id = %s', (successor_window,)):
        raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
    _quiescent(store, view)
    model_requests = {}
    work = _work(store, settings, authority, model_requests)
    unknowns, inputs = [], {}
    rows = store.rows(
        'SELECT a.request_id AS request_id, a.logical_request_id AS logical_request_id, '
        'a.attempt_id AS attempt_id, a.attempt_number AS attempt_number, a.window_id AS window_id, '
        'a.contract_sha256 AS contract_sha256, a.payload_sha256 AS payload_sha256, a.snapshot_json '
        'AS snapshot_json, a.reserved_micro_usd AS reserved_micro_usd, a.input_bound AS '
        'input_bound, a.output_bound AS output_bound, a.lease_token AS lease_token, a.lease_until '
        'AS lease_until, a.io_started_at AS io_started_at, a.journal_path AS journal_path, '
        'a.journal_id AS journal_id, a.measurement_json AS measurement_json, a.authority_json AS '
        'authority_json, m.job_id AS job_id, m.billed_micro_usd AS billed_micro_usd FROM '
        'model_admissions AS a JOIN model_usage AS m ON a.request_id = m.request_id WHERE m.state '
        "= 'UNKNOWN' ORDER BY a.request_id NULLS FIRST LIMIT 101"
    )
    total_unknown = store.one("SELECT COUNT(*) AS n FROM model_usage WHERE state = 'UNKNOWN'")["n"]
    if not rows or len(rows) > 100 or len(rows) != total_unknown:
        raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
    original = owner_authority.evidence(store, settings, authority)
    if original.policy.unknown_carry_sha256 is not None:
        from companion import model_renewal

        return model_renewal.manifest(
            store, settings, authority, successor_window, witness, view, original, stop, work,
        )
    for row in rows:
        financial = view["model_latest"].get(row["request_id"])
        if (
            not financial
            or financial["kind"] != "settle"
            or financial["state"] != "UNKNOWN"
            or json.loads(row["authority_json"]) != authority
            or row["contract_sha256"] != original.policy.paid_contract_sha256
            or not row["io_started_at"]
        ):
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        job = store.one('SELECT event_id FROM jobs WHERE job_id = %s', (row["job_id"],))
        if not job or job["event_id"] not in work:
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        event = job["event_id"]
        state = work[event]
        if state["terminal"] or not state["unsettled_model"]:
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        for source, source_models in model_requests.items():
            if row["request_id"] not in {item["request_id"] for item in source_models}:
                continue
            state = work[source]
            inputs.setdefault(
                source,
                {
                    "event_id": source,
                    "input_sha256": state["input_sha256"],
                    "work_sha256": state["work_sha256"],
                    "request_ids": [],
                },
            )
            inputs[source]["request_ids"].append(row["request_id"])
        tools = store.rows(
            'SELECT agent_tool_calls.job_id AS job_id, agent_tool_calls.tool_call_id AS '
            'tool_call_id, agent_tool_calls.tool_name AS tool_name, agent_tool_calls.args_sha256 '
            'AS args_sha256, agent_tool_calls.model_request_id AS model_request_id, '
            'agent_tool_calls.state AS state, agent_tool_calls.result_json AS result_json, '
            'agent_tool_calls.metadata_json AS metadata_json, agent_tool_calls.request_id AS '
            'request_id, agent_tool_calls.created_at AS created_at, agent_tool_calls.updated_at AS '
            'updated_at, agent_tool_calls.voice_job_id AS voice_job_id FROM agent_tool_calls AS '
            'agent_tool_calls WHERE agent_tool_calls.job_id = %s AND agent_tool_calls.state = '
            "'STARTED'", (row["job_id"],)
        )
        for tool in tools:
            metadata = json.loads(tool["metadata_json"] or "{}")
            if (
                tool["tool_name"] != "understand_video"
                or metadata.get("kind") != "qq_animation"
                or metadata.get("request_id") != row["request_id"]
            ):
                raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
        unknowns.append(
            {
                **_tuple(row),
                "logical_request_id": row["logical_request_id"],
                "reserved_micro_usd": row["reserved_micro_usd"],
                # Carry binds the original settlement; later accounting receipts
                # independently retain any larger booked responsibility.
                "minimum_micro_usd": financial["minimum_micro_usd"],
                "settlement_sha256": paid_budget.sha(financial),
                "tool_sha256": digest(tools),
            }
        )
    windows = sorted(
        {
            store.one(
                'SELECT window_id FROM model_admissions WHERE request_id = '
                '%s', (item["request_id"],)
            )["window_id"]
            for item in unknowns
        }
    )
    for carry in view["model_carries"].values():
        if set(windows) & {carry["manifest"]["successor_window_id"]}:
            windows = sorted(set(windows) | set(carry["manifest"]["budget_scope_window_ids"]))
    live_text._ledger_custody(store, settings)
    return {
        "authority": authority,
        "core_stop_sha256": stop["core_stop_sha256"],
        "budget_journal_id": original.journal_id,
        "successor_window_id": successor_window,
        "budget_scope_window_ids": windows,
        "unknowns": unknowns,
        "inputs": [inputs[key] for key in sorted(inputs)],
        "closed_executors_sha256": digest(witness),
        "budget_baseline_sha256": digest(
            {
                k: v
                for k, v in view["model_latest"].items()
                if store.one('SELECT window_id FROM model_admissions WHERE request_id = %s', (k,))[
                    "window_id"
                ]
                in windows
            }
        ),
    }


def _same_approval(old, new, *, renewal_at=None):
    allowed = {
        "window_id",
        "approval_sha256",
        "concurrency_basis",
        "credential_tag",
        "video_credential_tag",
        "concurrency",
    }
    if renewal_at is not None:
        # Renewal may happen before or after the old expiry: liabilities are fenced
        # by STOP, quiescence and the exact UNKNOWN inventory, not by the clock.
        # A missing expiry or rate validity is open-ended.
        def until(value, key):
            return None if value.get(key) is None else timestamp(value[key])

        now = timestamp(renewal_at)
        old_end, new_end = until(old, "expires_at"), until(new, "expires_at")
        old_rates, new_rates = until(old, "rates_valid_until"), until(new, "rates_valid_until")
        if ((new_end is not None and (
                new_end <= now or (old_end is not None and new_end <= old_end)
                or new_rates is None or new_rates < new_end))
            or (new_rates is not None and (
                new_rates <= now or (old_rates is not None and new_rates < old_rates)))):
            raise CoreError("OWNER_MODEL_RENEWAL_EXPIRY_REQUIRED", 409)
        allowed.update({"expires_at", "rates_valid_until"})
        # The per-call output ceiling may only rise. Every money amount, call count and
        # per-attempt/request cap stays identical, so no liability bound can grow.
        if new["max_output_tokens"] < old["max_output_tokens"]:
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        allowed.add("max_output_tokens")
    if (
        new.get("concurrency_basis") != wire.MODE
        or not 1 <= old["concurrency"] <= 10
        or not 1 <= new["concurrency"] <= 10
        or old["window_id"] == new["window_id"]
        or old["approval_sha256"] == new["approval_sha256"]
        or {k: v for k, v in old.items() if k not in allowed}
        != {k: v for k, v in new.items() if k not in allowed}
    ):
        raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)


def prepare(store, settings, request, verified_executors, now, *, request_sha256=None):
    """Offline operation; the deployment controller, not HTTP input, verifies exits."""
    from companion import owner_authority
    from companion.model_carry_controller import VerifiedExecutors
    from companion.owner_authority_protocol import OwnerGrantPolicy

    if not isinstance(verified_executors, VerifiedExecutors):
        raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
    supported(store, settings)
    paid_budget.contract(settings, now)
    fields = {
        "version",
        "operation_id",
        "authorization_sha256",
        "expected_head_sha256",
        "authority",
        "successor_window_id",
        "manifest_sha256",
        "successor_policy",
        "executors",
    }
    renewal = isinstance(request, dict) and request.get("version") == 2
    if renewal:
        fields.add("previous_paid_contract")
    kind = RENEW if renewal else CARRY
    if (
        not isinstance(request, dict)
        or set(request) != fields
        or type(request["version"]) is not int
        or request["version"] not in {1, 2}
        or not hash_value(request["authorization_sha256"])
        or not hash_value(request["expected_head_sha256"])
        or not hash_value(request["manifest_sha256"])
        or not identifier(request["operation_id"], r"[A-Za-z0-9_-]{8,128}")
    ):
        raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
    if verified_executors.value != request["executors"]:
        raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
    reviewed_sha = request_sha256 or digest(request)
    if not hash_value(reviewed_sha):
        raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
    with store.transaction():
        # Only the same reviewed offline request may complete fsync-before-commit
        # projection recovery. Normal readers always fail closed on this mismatch.
        journal_path = settings.paid_model.journal_path
        from pathlib import Path

        authority_row = owner_authority._latest(store)
        if (
            not authority_row
            or Path(journal_path).resolve() != Path(authority_row["journal_path"]).resolve()
        ):
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        raw_records = [
            json.loads(line) for line in Path(journal_path).read_text("utf-8").splitlines()
        ]
        previous = [
            row
            for row in raw_records
            if row.get("kind") in {CARRY, RENEW}
            and row.get("operation_id") == request["operation_id"]
        ]
        if previous:
            if (
                len(previous) != 1
                or previous[0]["request_sha256"] != reviewed_sha
                or previous[0]["request_fingerprint_sha256"] != digest(request)
            ):
                raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
            candidate = previous[0]
            projected = store.one(
                'SELECT value FROM runtime_meta WHERE key = %s',
                ("model_control:" + candidate["sha256"],),
            )
            if not projected:
                paid_budget.journal(
                    store,
                    SimpleNamespace(journal_path=journal_path),
                    allow_settlement=True,
                    model_control_replay=candidate["sha256"],
                )
                with Path(journal_path).open("r+b") as stream:
                    os.fsync(stream.fileno())
                store.execute(
                    'INSERT INTO runtime_meta VALUES (%s, %s)',
                    ("model_control:" + candidate["sha256"], candidate["sha256"]),
                )
        view = owner_authority.custody(store, settings, settlement=True)
        for saved in view["model_carries"].values():
            if saved["operation_id"] == request["operation_id"]:
                if saved["carry_sha256"] != request["manifest_sha256"]:
                    raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
                return _receipt(saved)
        if view["head_sha256"] != request["expected_head_sha256"]:
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        value = manifest(
            store,
            settings,
            request["authority"],
            request["successor_window_id"],
            verified_executors.value,
            view,
        )
        if digest(value) != request["manifest_sha256"]:
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        if renewal != ("predecessor_carry_sha256" in value):
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        policy = OwnerGrantPolicy.model_validate(request["successor_policy"])
        original = owner_authority.evidence(store, settings, request["authority"])
        old_policy = original.policy.model_dump(mode="json")
        new_policy = policy.model_dump(mode="json")
        allowed = {"authorization_sha256", "paid_contract_sha256", "unknown_carry_sha256"}
        if renewal:
            from companion import model_renewal

            model_renewal.check_policy(store, settings, original.policy, policy, now,
                retained_unknowns={item["request_id"] for item in value["unknowns"]})
            allowed.update({"expires_at", "purposes", "life_shadow"})
        if (
            policy.unknown_carry_sha256 != digest(value)
            or policy.authorization_sha256 != request["authorization_sha256"]
            or policy.authorization_sha256 == original.policy.authorization_sha256
            or policy.paid_contract_sha256 != paid_budget.sha(paid_budget.snapshot(settings))
            or {k: v for k, v in old_policy.items() if k not in allowed}
            != {k: v for k, v in new_policy.items() if k not in allowed}
        ):
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        old = request["previous_paid_contract"] if renewal else json.loads(
            store.one(
                'SELECT snapshot_json FROM model_admissions WHERE request_id = %s',
                (value["unknowns"][0]["request_id"],),
            )["snapshot_json"]
        )
        new = paid_budget.snapshot(settings)
        if renewal and paid_budget.sha(old) != original.policy.paid_contract_sha256:
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        _same_approval(old, new, renewal_at=now if renewal else None)
        if new["window_id"] != request["successor_window_id"]:
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        # Recompute the original per-window credential tags using the current key.
        previous_paid = settings.paid_model
        try:
            settings.paid_model = previous_paid.model_copy(update={"window_id": old["window_id"]})
            tags = paid_budget.snapshot(settings)
        finally:
            settings.paid_model = previous_paid
        if any(old.get(k) != tags.get(k) for k in ("credential_tag", "video_credential_tag")):
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        record = {
            "kind": kind,
            "version": 1,
            "operation_id": request["operation_id"],
            "request_sha256": reviewed_sha,
            "request_fingerprint_sha256": digest(request),
            "authorization_sha256": request["authorization_sha256"],
            "manifest": value,
            "carry_sha256": digest(value),
            "successor_policy_sha256": digest(new_policy),
            "successor_paid_contract_sha256": paid_budget.sha(new),
            "successor_approval_sha256": settings.paid_model.approval_sha256,
            "concurrency_basis": wire.MODE,
            "concurrency": settings.paid_model.concurrency,
            "closed_executors": verified_executors.value,
            "at": now,
        }
        paid_budget.journal(
            store,
            SimpleNamespace(journal_path=original.journal_path),
            record,
            allow_settlement=True,
        )
        saved = owner_authority.custody(store, settings, settlement=True)["model_carries"][
            digest(value)
        ]
        return _receipt(saved)


def _receipt(saved):
    value = saved["manifest"]
    if saved["kind"] == RENEW:
        # The CLI summary retains the whole financial scope. Authenticated input
        # handoff reads a separate receipt for this authority's closed inputs.
        return {
            "kind": "owner_model_approval_renewal_v1", "version": 1, "state": "PREPARED",
            "authority": value["authority"], "carry_sha256": saved["carry_sha256"],
            "record_sha256": saved["record_sha256"],
            "predecessor_carry_sha256": value["predecessor_carry_sha256"],
            "successor_policy_sha256": saved["successor_policy_sha256"],
            "successor_window_id": value["successor_window_id"],
            "retained_micro_usd": sum(i["minimum_micro_usd"] for i in value["unknowns"]),
        }
    return _input_receipt(saved)


def _carry_fields(saved):
    value = saved["manifest"]
    requests = sorted({key for item in value["inputs"] for key in item["request_ids"]})
    return {
        "kind": wire.KIND,
        "version": 1,
        "state": "PREPARED",
        **{
            k: value[k]
            for k in (
                "authority",
                "core_stop_sha256",
                "budget_journal_id",
                "successor_window_id",
                "inputs",
                "closed_executors_sha256",
            )
        },
        "carry_sha256": saved["carry_sha256"],
        "record_sha256": saved["record_sha256"],
        "successor_policy_sha256": saved["successor_policy_sha256"],
        "request_ids": requests,
        "retained_micro_usd": sum(i["minimum_micro_usd"] for i in value["unknowns"]
                                 if i["request_id"] in requests),
    }


def _input_receipt(saved):
    result = _carry_fields(saved)
    result["receipt_sha256"] = digest(result)
    return wire.receipt(result)


def read(store, settings, authority, carry_sha, successor_policy_sha):
    from companion import owner_authority

    with store.transaction():
        supported(store, settings)
        view = owner_authority.custody(store, settings, settlement=True)
        saved = view["model_carries"].get(carry_sha)
        if not saved:
            raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
        if (saved["kind"] == RENEW and authority == saved["manifest"]["authority"]
                and not saved["manifest"]["inputs"]):
            raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
        if authority != saved["manifest"]["authority"]:
            if authority["policy_sha256"] != successor_policy_sha:
                raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
            stopped = _continuation(store, settings, authority, saved, view)
            return _local_receipt(store, settings, authority, saved, stopped, view)
        if saved["successor_policy_sha256"] != successor_policy_sha:
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        result = _input_receipt(saved)
        head = owner_authority._latest(store)
        if (
            result["authority"] != authority
            or result["successor_policy_sha256"] != successor_policy_sha
            or not head
            or head["grant_id"] != authority["grant_id"]
            or head["restore_generation"] != authority["restore_generation"]
            or owner_authority._meta(store, owner_authority.GENERATION)
            != authority["restore_generation"]
            or not (
                head["state"] == "STOPPED"
                and head["revision"] == authority["revision"] + 1
                or head["state"] == "ACTIVE"
                and head["revision"] == authority["revision"] + 2
                and head["policy_sha256"] == successor_policy_sha
            )
        ):
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        return result


def _local_receipt(store, settings, authority, saved, stopped, view):
    models = {}
    work = _work(store, settings, authority, models)
    inputs, requests = [], set()
    for event, state in sorted(work.items()):
        ids = [row["request_id"] for row in models[event] if row["state"] == "UNKNOWN"]
        if not ids:
            continue
        if state["terminal"] or not state["unsettled_model"]:
            raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
        for request_id in ids:
            closed_sha = view["model_closures"].get(request_id)
            closed = view["model_controls"].get(closed_sha)
            if not closed or closed["kind"] != CLOSE or closed["authority"] != authority:
                raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
        inputs.append(
            {
                "event_id": event,
                "input_sha256": state["input_sha256"],
                "work_sha256": state["work_sha256"],
                "request_ids": ids,
            }
        )
        requests.update(ids)
    current_unknown = store.rows(
        'SELECT a.request_id, a.authority_json FROM model_admissions AS a JOIN model_usage AS m '
        "USING (request_id) WHERE m.state = 'UNKNOWN'"
    )
    if not requests or requests != {
        row["request_id"]
        for row in current_unknown
        if json.loads(row["authority_json"]) == authority
    }:
        raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
    closures = [
        {"request_id": request_id, "record_sha256": view["model_closures"][request_id]}
        for request_id in sorted(requests)
    ]
    result = {
        **_carry_fields(saved),
        "kind": wire.LOCAL_KIND,
        "version": 2,
        "authority": authority,
        "successor_policy_sha256": authority["policy_sha256"],
        "origin_authority": saved["manifest"]["authority"],
        "core_stop_sha256": stopped["record_sha256"],
        "inputs": inputs,
        "request_ids": sorted(requests),
        "retained_micro_usd": sum(view["model_latest"][k]["minimum_micro_usd"] for k in requests),
        "local_closures": closures,
        "closed_executors_sha256": digest(closures),
    }
    result["receipt_sha256"] = digest({k: v for k, v in result.items() if k != "receipt_sha256"})
    return wire.receipt(result)


def purpose_revision(old, new):
    """One private-purpose addition or an existing voice/meme configuration revision."""
    if old == new:
        return
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    for capability in ("voice", "meme"):
        fingerprint = new.capability_policies.get(capability)
        if (capability in old.purposes and hash_value(fingerprint)
            and fingerprint != old.capability_policies.get(capability)
            and old.authorization_sha256 != new.authorization_sha256
            and after == {**before, "authorization_sha256": new.authorization_sha256,
                          "capability_policies": {**old.capability_policies,
                                                  capability: fingerprint}}):
            return
    if (len(new.purposes) != len(old.purposes) + 1
        or new.purposes[:-1] != old.purposes
        or new.purposes[-1] not in {"evaluation", "rem"}
        or old.authorization_sha256 == new.authorization_sha256):
        raise ValueError("explicit private purpose addition required")
    expected = {**before, "purposes": new.purposes,
                "authorization_sha256": new.authorization_sha256}
    if new.purposes[-1] == "rem":
        fingerprint = new.capability_policies.get("rem")
        if ("memory" not in old.purposes or old.memory_scope != "owner_private"
            or "rem" in old.capability_policies or not hash_value(fingerprint)):
            raise ValueError("REM requires existing private Memory and its exact policy")
        expected["capability_policies"] = {**old.capability_policies, "rem": fingerprint}
    if after != expected:
        raise ValueError("unrelated policy changes are not allowed")


def authority_chain(store, saved, revisions):
    """Validate retained authority evidence without resurrecting erased policies.

    Authority records attest each accepted activation. Erasure retains their
    hashes; fresh activation separately requires the current policy body.
    """
    from companion.owner_authority_protocol import OwnerGrantPolicy

    origin = saved["manifest"]["authority"]
    first = origin["revision"] + 2
    current_rows = {row["revision"]: row for row in store.rows(
        'SELECT owner_authority_revisions.grant_id AS grant_id, owner_authority_revisions.revision '
        'AS revision, owner_authority_revisions.operation_id AS operation_id, '
        'owner_authority_revisions.request_sha256 AS request_sha256, '
        'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
        'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
        'owner_authority_revisions.policy_redacted AS policy_redacted, '
        'owner_authority_revisions.restore_generation AS restore_generation, '
        'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
        'owner_authority_revisions.record_sha256 AS record_sha256, '
        'owner_authority_revisions.journal_id AS journal_id, '
        'owner_authority_revisions.journal_path AS journal_path, '
        'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
        'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
        'owner_authority_revisions.revision BETWEEN %s AND %s',
        (origin["grant_id"], first, first + len(revisions) - 1),
    )} if revisions else {}
    policies = {}
    previous, previous_policy = None, None
    for index, row in enumerate(revisions):
        current = current_rows.get(row["revision"])
        if (not current or row["grant_id"] != origin["grant_id"]
            or row["restore_generation"] != origin["restore_generation"]
            or row["revision"] != origin["revision"] + 2 + index
            or row["state"] != ("ACTIVE" if index % 2 == 0 else "STOPPED")
            or any(current[k] != row[k] for k in (
                "record_sha256", "policy_sha256", "state", "predecessor_sha256"))
            or not index and row["policy_sha256"] != saved["successor_policy_sha256"]
            or previous and row["predecessor_sha256"] != previous["record_sha256"]
            or index % 2 and row["policy_sha256"] != previous["policy_sha256"]):
            raise ValueError("model carry authority chain changed")
        policy = None
        if current["policy_redacted"]:
            if current["policy_json"] != "{}":
                raise ValueError("invalid erased authority")
        else:
            text = current["policy_json"]
            if text not in policies:
                policy = OwnerGrantPolicy.model_validate_json(text)
                policies[text] = policy, digest(policy.model_dump(mode="json"))
            policy, policy_sha256 = policies[text]
            if (policy_sha256 != row["policy_sha256"]
                or policy.unknown_carry_sha256 != saved["carry_sha256"]
                or policy.paid_contract_sha256 != saved["successor_paid_contract_sha256"]):
                raise ValueError("model carry authority policy changed")
        if previous and row["policy_sha256"] != previous["policy_sha256"]:
            if policy is not None and previous_policy is not None:
                purpose_revision(previous_policy, policy)
            elif policy is not None or previous_policy is not None:
                raise ValueError("incomplete erased authority transition")
        previous, previous_policy = row, policy


def _continuation(store, settings, authority, saved, view, *, approved_contract_sha256=None):
    """Reuse one paid approval across verified STOP/ACTIVE authority revisions."""
    from companion import live_text, owner_authority

    origin = saved["manifest"]["authority"]
    original = owner_authority.evidence(store, settings, authority)
    head = owner_authority._latest(store)
    stopped = store.one(
        'SELECT owner_authority_revisions.grant_id AS grant_id, owner_authority_revisions.revision '
        'AS revision, owner_authority_revisions.operation_id AS operation_id, '
        'owner_authority_revisions.request_sha256 AS request_sha256, '
        'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
        'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
        'owner_authority_revisions.policy_redacted AS policy_redacted, '
        'owner_authority_revisions.restore_generation AS restore_generation, '
        'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
        'owner_authority_revisions.record_sha256 AS record_sha256, '
        'owner_authority_revisions.journal_id AS journal_id, '
        'owner_authority_revisions.journal_path AS journal_path, '
        'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
        'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
        'owner_authority_revisions.revision = %s',
        (authority["grant_id"], authority["revision"] + 1),
    )
    revisions = store.rows(
        'SELECT owner_authority_revisions.grant_id AS grant_id, owner_authority_revisions.revision '
        'AS revision, owner_authority_revisions.operation_id AS operation_id, '
        'owner_authority_revisions.request_sha256 AS request_sha256, '
        'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
        'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
        'owner_authority_revisions.policy_redacted AS policy_redacted, '
        'owner_authority_revisions.restore_generation AS restore_generation, '
        'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
        'owner_authority_revisions.record_sha256 AS record_sha256, '
        'owner_authority_revisions.journal_id AS journal_id, '
        'owner_authority_revisions.journal_path AS journal_path, '
        'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
        'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
        'owner_authority_revisions.revision BETWEEN %s AND %s ORDER BY '
        'owner_authority_revisions.revision NULLS FIRST',
        (origin["grant_id"], origin["revision"] + 2, authority["revision"]),
    )
    if (
        authority["grant_id"] != origin["grant_id"]
        or authority["restore_generation"] != origin["restore_generation"]
        or authority["revision"] < origin["revision"] + 2
        or not revisions
        or len(revisions) != authority["revision"] - origin["revision"] - 1
        or any(
            row["restore_generation"] != origin["restore_generation"]
            or row["state"] != ("ACTIVE" if index % 2 == 0 else "STOPPED")
            for index, row in enumerate(revisions)
        )
        or not stopped
        or stopped["state"] != "STOPPED"
        or stopped["predecessor_sha256"] != revisions[-1]["record_sha256"]
        or stopped["policy_sha256"] != authority["policy_sha256"]
        or stopped["restore_generation"] != origin["restore_generation"]
        or owner_authority._meta(store, owner_authority.GENERATION) != origin["restore_generation"]
        or not head
        or not (
            head["record_sha256"] == stopped["record_sha256"]
            or head["state"] == "ACTIVE"
            and head["revision"] == authority["revision"] + 2
            and head["grant_id"] == origin["grant_id"]
            and head["restore_generation"] == origin["restore_generation"]
            and head["policy_sha256"] == authority["policy_sha256"]
            and head["predecessor_sha256"] == stopped["record_sha256"]
        )
        or (approved_contract_sha256 or paid_budget.sha(paid_budget.snapshot(settings)))
        != saved["successor_paid_contract_sha256"]
        or original.policy.unknown_carry_sha256 != saved["carry_sha256"]
    ):
        raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
    try:
        authority_chain(store, saved, revisions)
    except (ValueError, TypeError, KeyError) as error:
        raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409) from error
    _quiescent(store, view)
    unknown = store.rows("SELECT request_id FROM model_usage WHERE state = 'UNKNOWN'")
    if any(row["request_id"] not in view["model_closures"] for row in unknown):
        raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
    live_text._ledger_custody(store, settings)
    return stopped


def activation(store, settings, policy):
    from companion import owner_authority

    supported(store, settings)
    view = owner_authority.custody(store, settings, settlement=True)
    saved = view["model_carries"].get(policy.unknown_carry_sha256)
    if not saved:
        raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
    head = owner_authority._latest(store)
    if head and head["record_sha256"] != saved["manifest"]["core_stop_sha256"]:
        if head["state"] != "STOPPED":
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        previous = store.one(
            'SELECT owner_authority_revisions.grant_id AS grant_id, '
            'owner_authority_revisions.revision AS revision, '
            'owner_authority_revisions.operation_id AS operation_id, '
            'owner_authority_revisions.request_sha256 AS request_sha256, '
            'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
            'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
            'owner_authority_revisions.policy_redacted AS policy_redacted, '
            'owner_authority_revisions.restore_generation AS restore_generation, '
            'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
            'owner_authority_revisions.record_sha256 AS record_sha256, '
            'owner_authority_revisions.journal_id AS journal_id, '
            'owner_authority_revisions.journal_path AS journal_path, '
            'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
            'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
            'owner_authority_revisions.revision = %s',
            (head["grant_id"], head["revision"] - 1),
        )
        if not previous:
            raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)
        authority = owner_authority.reference(previous).as_dict()
        _continuation(store, settings, authority, saved, view)
        work = _work(store, settings, authority)
        old = owner_authority.evidence(store, settings, authority).policy
        try:
            purpose_revision(old, policy)
        except (ValueError, TypeError, KeyError) as error:
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409) from error
        if old != policy and any(not state["terminal"] for state in work.values()):
            raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
        if old != policy and store.one(
            'SELECT 1 FROM model_admissions AS a JOIN model_usage AS u USING (request_id) WHERE '
            "u.state = 'UNKNOWN' AND a.authority_json = %s", (encode(authority),)
        ):
            raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
        return
    if saved["successor_policy_sha256"] != digest(policy.model_dump(mode="json")):
        raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
    value = manifest(
        store,
        settings,
        saved["manifest"]["authority"],
        settings.paid_model.window_id,
        saved["closed_executors"],
        view,
    )
    if (
        head["record_sha256"] != saved["manifest"]["core_stop_sha256"]
        or value != saved["manifest"]
        or paid_budget.sha(paid_budget.snapshot(settings))
        != saved["successor_paid_contract_sha256"]
    ):
        raise CoreError("OWNER_MODEL_CARRY_CHANGED", 409)


def scope_windows(view, window_id):
    result = {window_id}
    for saved in view["model_carries"].values():
        if saved["manifest"]["successor_window_id"] == window_id:
            result.update(saved["manifest"]["budget_scope_window_ids"])
    return sorted(result)
