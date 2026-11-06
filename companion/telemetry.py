"""Opt-in, content-free local stage timings; never a delivery or billing receipt."""

import json
import math
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from itertools import islice

SESSION = uuid.uuid4().hex
EVIDENCE_TYPES = {"OFFLINE_FIXTURE", "LIVE_ONEBOT", "PAID_MODEL"}
API_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"}
API_ERRORS = {None, "HTTPError", "OperationalError", "DatabaseError", "CancelledError",
              "UnhandledError"}
API_FIELDS = {"schema_version", "evidence_type", "session", "stage", "route", "method",
              "status", "elapsed_ms", "error_class", "response_complete"}
MAX_API_RECORD_BYTES = 4096
MAX_API_SAMPLES = 10000
STAGES = {
    "t_job_started",
    "t_context_ready",
    "t_model_request",
    "t_model_done",
    "t_model_failed",
    "t_job_done",
    "job_failed",
}
PHASES = {
    "context_guard", "context_restore", "context_tools", "context_build",
    "context_rules", "context_references", "context_memory", "context_history",
    "context_current", "context_fit", "context_planning",
    "agent_guard", "agent_setup", "request_guard", "request_plan",
    "request_checkpoint", "request_validate", "request_reserve", "request_sources",
    "dispatch_sources", "dispatch_scope", "dispatch_measure", "dispatch_journal",
    "dispatch_recheck",
    "transport_custody", "transport_payload", "transport_sources", "model_transport",
    "recall_memory", "recall_turns", "turn_recall_candidates", "turn_recall_rank",
    "context_turn_recall", "model_http",
}
STAGES.update(PHASES)


def splits(trace_id):
    """Consecutive local spans within one call; each mark covers time since the last."""
    enabled = trace_id is not None and api_enabled()
    last = [time.perf_counter_ns()] if enabled else [0]

    def mark(stage):
        if not enabled or stage not in PHASES:
            return
        now = time.perf_counter_ns()
        try:
            emit(stage, trace_id, elapsed_ms=(now - last[0]) / 1e6, at_ns=now)
        except Exception:
            pass  # Instrumentation must not change the operation's result.
        last[0] = now

    return mark


@contextmanager
def phase(stage, trace_id):
    """A local span only; a completed span is not proof that its operation succeeded."""
    if trace_id is None or stage not in PHASES or not api_enabled():
        yield
        return
    started = time.perf_counter_ns()
    try:
        yield
    finally:
        finished = time.perf_counter_ns()
        try:
            emit(stage, trace_id, elapsed_ms=(finished - started) / 1e6, at_ns=finished)
        except Exception:
            # Instrumentation must not replace the operation's result or exception.
            pass


def emit(
    stage, trace_id, *, elapsed_ms=None, budget_micro_usd=None, model_usage_state=None, at_ns=None
):
    if os.environ.get("COMPANION_TELEMETRY") != "1":
        return
    evidence = os.environ.get("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    if (
        stage not in STAGES
        or evidence not in EVIDENCE_TYPES
        or not re.fullmatch(r"trace_[a-f0-9]{32,64}", trace_id)
    ):
        return
    record = {
        "schema_version": 1,
        "evidence_type": evidence,
        "session": SESSION,
        "trace_id": trace_id,
        "stage": stage,
        "monotonic_ns": time.perf_counter_ns() if at_ns is None else at_ns,
    }
    if elapsed_ms is not None:
        record["elapsed_ms"] = round(max(0, elapsed_ms), 4)
    if budget_micro_usd is not None:
        record["budget_micro_usd"] = budget_micro_usd
    if model_usage_state in {"SETTLED", "UNKNOWN"}:
        record["model_usage_state"] = model_usage_state
    try:
        print(json.dumps(record), flush=True)
    except Exception:
        pass


LOCK_REPORT_MS = 500


def emit_lock(site, *, wait_ms, held_ms, outcome):
    """One selected write transaction: its call site, never what it wrote."""
    if not api_enabled() or (wait_ms + held_ms < LOCK_REPORT_MS
                            and os.environ.get("COMPANION_TELEMETRY_ALL_LOCKS") != "1"):
        return
    record = {
        "schema_version": 1,
        "evidence_type": os.environ.get("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE"),
        "session": SESSION,
        "stage": "db_write_lock",
        "site": site,
        "wait_ms": round(max(0, wait_ms), 1),
        "held_ms": round(max(0, held_ms), 1),
        "outcome": outcome,
    }
    try:
        # stderr: CLI commands print their receipts on stdout and parse them there.
        print(json.dumps(record), file=sys.stderr, flush=True)
    except Exception:
        pass  # Instrumentation must not change the transaction's result.


def api_enabled():
    return (os.environ.get("COMPANION_TELEMETRY") == "1"
            and os.environ.get("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
            in EVIDENCE_TYPES)


def emit_api(route, method, *, status, elapsed_ms, error_class, response_complete):
    """The caller supplies a registered route template, never a request path."""
    if not api_enabled():
        return
    record = {
        "schema_version": 1,
        "evidence_type": os.environ.get("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE"),
        "session": SESSION,
        "stage": "api_request",
        "route": route,
        "method": method,
        "status": status,
        "elapsed_ms": round(max(0, elapsed_ms), 4),
        "error_class": error_class,
        "response_complete": response_complete,
    }
    line = json.dumps(record)
    if len(line) + 1 > MAX_API_RECORD_BYTES:
        return
    try:
        print(line, flush=True)
    except Exception:
        # A closed/broken telemetry sink must not change a business response.
        pass


def validate_api_record(record):
    """Validate structure for count-only readers; this does not prove route registration.

    Route summaries must still use summarize_api with caller-supplied trusted
    templates. A count-only report must not echo this untrusted label.
    """
    if not isinstance(record, dict) or set(record) != API_FIELDS:
        raise ValueError("UNRECOGNIZED_API_METRIC_FIELDS")
    if (type(record["schema_version"]) is not int or record["schema_version"] != 1
        or record["stage"] != "api_request"
        or type(record["evidence_type"]) is not str or record["evidence_type"] not in EVIDENCE_TYPES
        or type(record["session"]) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", record["session"])
        or type(record["route"]) is not str
        or len(record["route"]) > 512
        or record["route"] != "UNMATCHED" and not re.fullmatch(
            r"/(?:(?:[A-Za-z0-9_.-]+|\{[A-Za-z_][A-Za-z0-9_]*\})(?:/|$))*", record["route"])
        or any(part in {".", ".."} for part in record["route"].split("/"))
        or type(record["method"]) is not str or record["method"] not in API_METHODS
        or record["status"] is not None and (
            type(record["status"]) is not int or not 100 <= record["status"] <= 599)
        or type(record["elapsed_ms"]) not in (int, float)
        or not 0 <= record["elapsed_ms"] < 2**63
        or type(record["error_class"]) not in (str, type(None))
        or record["error_class"] not in API_ERRORS
        or type(record["response_complete"]) is not bool
        or record["response_complete"] and record["status"] is None):
        raise ValueError("INVALID_API_METRIC")
    if len(json.dumps(record)) + 1 > MAX_API_RECORD_BYTES:
        raise ValueError("API_METRIC_TOO_LARGE")


def summarize_api(records, evidence_type, route_templates):
    """Summarize an already bounded log iterator; no file, DB or background task.

    Templates must come from the application's route registry. Scan at most
    MAX_API_SAMPLES input records; percentiles use the nearest-rank definition.
    """
    allowed = set(route_templates) | {"UNMATCHED"}
    groups = {}
    scanned, truncated = 0, False
    for index, record in enumerate(islice(records, MAX_API_SAMPLES + 1)):
        if index == MAX_API_SAMPLES:
            truncated = True
            break
        scanned += 1
        if (not isinstance(record, dict) or record.get("schema_version") != 1
            or not {"status", "error_class"} <= record.keys()
            or evidence_type not in EVIDENCE_TYPES
            or record.get("evidence_type") != evidence_type
            or record.get("stage") != "api_request"
            or type(record.get("route")) is not str or record["route"] not in allowed
            or type(record.get("method")) is not str or record["method"] not in API_METHODS
            or type(record.get("elapsed_ms")) not in (int, float)
            or not 0 <= record["elapsed_ms"] < 2**63
            or type(record.get("response_complete")) is not bool
            or record.get("status") is not None and (
                type(record["status"]) is not int or not 100 <= record["status"] <= 599)
            or type(record.get("error_class")) not in (str, type(None))
            or record.get("error_class") not in API_ERRORS):
            continue
        key = (record["route"], record["method"])
        durations, errors = groups.setdefault(key, ([], []))
        durations.append(record["elapsed_ms"])
        errors.append(bool(record["error_class"] or not record["response_complete"]
                           or record["status"] is None or record["status"] >= 400))
    rows = []
    for (route, method), (durations, errors) in sorted(groups.items()):
        durations.sort()
        rows.append({"route": route, "method": method, "count": len(durations),
                     "error_count": sum(errors), "error_rate": sum(errors) / len(errors),
                     "p50_ms": durations[math.ceil(len(durations) * 0.5) - 1],
                     "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1]})
    return {"evidence_type": evidence_type, "records_scanned": scanned,
            "truncated": truncated, "percentile_method": "nearest_rank", "routes": rows}
