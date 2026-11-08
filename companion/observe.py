"""Bounded observation evidence, not a heartbeat, queue, or health authority."""

import asyncio
import hashlib
import json
import math
import os
import re
import ssl
import time
import uuid
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from companion import worker_loop
from companion.store import encode

HTTP_TOTAL_SECONDS = 5
BLOCKER_CODES = frozenset({
    "DISK_RESOURCE_UNAVAILABLE", "DATABASE_MISSING", "MIGRATION_REQUIRED",
    "SCHEMA_HEAD_UNAVAILABLE",
    "DATABASE_INTEGRITY_FAILED", "DATABASE_SNAPSHOT_UNAVAILABLE",
    "OWNER_IDENTITY_UNINITIALIZED_OR_MISMATCH", "ERASURE_IDENTITY_UNINITIALIZED",
    "ERASURE_LEDGER_RECONCILIATION_REQUIRED", "ERASURE_REPLAY_REQUIRED",
    "ERASURE_LEDGER_UNAVAILABLE_OR_TOO_LARGE", "BUDGET_JOURNAL_RECONCILIATION_REQUIRED",
    "BUDGET_JOURNAL_UNAVAILABLE_OR_TOO_LARGE", "PAID_APPROVAL_REQUIRED",
    "MODEL_BUDGET_BLOCKED", "MODEL_BUDGET_RESTORE_REQUIRED", "LIVE_TEXT_STOPPED",
    "MODEL_USAGE_UNSETTLED", "DELIVERY_UNCERTAIN", "EXPIRED_LEASES", "EMBEDDING_CACHE_MISSING",
})
WARNING_CODES = frozenset({"MODEL_COST_UNKNOWN", "MODEL_BUDGET_BLOCKED",
                           "MODEL_BUDGET_RESTORE_REQUIRED"})


def _integer(value, *, signed=False):
    if type(value) is not int or not (-(2**63 - 1) if signed else 0) <= value < 2**63:
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    return value


def _queue(value, active, terminal):
    states = value["states"]
    if not isinstance(states, dict) or set(states) - set(active + terminal):
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    states = {state: _integer(count) for state, count in states.items()}
    count, expired = _integer(value["active"]), _integer(value["expired_leases"])
    age = value["oldest_age_seconds"]
    if (count != sum(states.get(state, 0) for state in active) or expired > count
        or (age is None) != (count == 0)
        or (age is not None and (type(age) not in {int, float} or not math.isfinite(age)
                                 or not 0 <= age < 2**63))):
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    return {"states": states, "active": count, "expired_leases": expired,
            "oldest_age_seconds": age}


def _resource(value):
    if not isinstance(value, dict):
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    result = {}
    for key in ("required", "exists", "model_loaded"):
        if key in value:
            result[key] = _boolean(value[key])
    for key in ("bytes", "records"):
        if key in value:
            result[key] = (None if key == "bytes" and value[key] is None
                           else _integer(value[key]))
    if "checked" in value:
        checked = value["checked"]
        if checked is not False and checked not in {"HEADER_ONLY", "FULL_LEDGER", "FULL_JOURNAL"}:
            raise ValueError("OBSERVATION_RESPONSE_INVALID")
        result["checked"] = checked
    return result


def _safe_operations(raw, observed):
    """Project this endpoint's small aggregate contract; never retain remote objects."""
    codes = raw["blockers"]
    if (type(raw["schema_version"]) is not int or raw["schema_version"] != 1
        or raw["read_only"] is not True or raw["mode"] not in {"demo", "live"}
        or not isinstance(codes, list) or len(codes) > 64
        or any(type(code) is not str or code not in BLOCKER_CODES for code in codes)
        or raw["worker_progress"].get("state") != "UNOBSERVED"
        or raw["worker_progress"].get("reason") != "BUSINESS_PROGRESS_NOT_INFERRED"):
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    result = {"schema_version": 1, "read_only": True, "mode": raw["mode"],
              "observed_at": observed.astimezone(UTC).isoformat(), "blockers": list(codes),
              "worker_progress": {"state": "UNOBSERVED",
                                  "reason": "BUSINESS_PROGRESS_NOT_INFERRED"}}
    warnings = raw.get("warnings", [])
    if (type(warnings) is not list or len(warnings) > len(WARNING_CODES)
        or any(type(code) is not str or code not in WARNING_CODES for code in warnings)):
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    result["warnings"] = list(warnings)
    result["worker_loop"] = worker_loop.project_public(raw["worker_loop"], observed)
    database = raw["database"]
    if not {"exists", "bytes"} <= database.keys():
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    result["database"] = _resource(database)
    if "wal_bytes" in database:
        result["database"]["wal_bytes"] = (
            None if database["wal_bytes"] is None else _integer(database["wal_bytes"])
        )
    if "capabilities_present" in database:
        result["database"]["capabilities_present"] = _boolean(database["capabilities_present"])
    if "integrity" in database:
        if database["integrity"] not in {"NOT_CHECKED", "OK", "FAILED"}:
            raise ValueError("OBSERVATION_RESPONSE_INVALID")
        result["database"]["integrity"] = database["integrity"]
    resources = raw["resources"]
    if not codes and not {
        "erasure_ledger", "budget_journal", "embedding_cache",
    } <= resources.keys():
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    free = resources["disk_free_bytes"]
    result["resources"] = {"disk_free_bytes": None if free is None else _integer(free)}
    for key in ("erasure_ledger", "budget_journal", "embedding_cache"):
        if key in resources:
            result["resources"][key] = _resource(resources[key])
    for key, active, terminal in (
        ("jobs", ("QUEUED", "RUNNING", "RETRY_WAIT"), ("SUCCEEDED", "FAILED", "CANCELLED")),
        ("deliveries", ("PENDING", "LEASED", "SENDING", "RETRY_WAIT", "DELIVERY_UNCERTAIN"),
         ("SENT", "FAILED", "CANCELLED")),
    ):
        if key in raw:
            result[key] = _queue(raw[key], active, terminal)
        elif not codes:
            raise ValueError("OBSERVATION_RESPONSE_INVALID")
    if "budget" in raw:
        budget = raw["budget"]
        result["budget"] = {key: _integer(budget[key]) for key in (
            "unsettled_calls", "unsettled_micro_usd", "unknown_calls",
        )}
        for key in ("pricing_configured", "paid_approval_configured"):
            result["budget"][key] = _boolean(budget[key])
        calibration = budget["calibration"]
        safe = {key: _integer(calibration[key]) for key in (
            "calibrated_calls", "input_estimated_tokens", "input_actual_tokens",
            "output_actual_tokens", "cached_input_tokens", "reserved_micro_usd",
            "usage_priced_micro_usd", "anomaly_calls",
        )}
        for key in ("input_error_tokens", "cost_error_micro_usd"):
            safe[key] = _integer(calibration[key], signed=True)
        currency = "USD" if safe["calibrated_calls"] else None
        if (calibration["currency"] != currency
            or calibration["billing_basis"] != "configured_rates_not_invoice"
            or safe["input_error_tokens"] != (
                safe["input_actual_tokens"] - safe["input_estimated_tokens"]
            ) or safe["cost_error_micro_usd"] != (
                safe["usage_priced_micro_usd"] - safe["reserved_micro_usd"]
            ) or result["budget"]["unknown_calls"] > result["budget"]["unsettled_calls"]):
            raise ValueError("OBSERVATION_RESPONSE_INVALID")
        result["budget"]["calibration"] = safe | {
            "currency": currency, "billing_basis": "configured_rates_not_invoice",
        }
    elif not codes:
        raise ValueError("OBSERVATION_RESPONSE_INVALID")
    return result


def http_collector(settings, url, *, allow_external=False, authorization_sha256=None):
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("OBSERVATION_ORIGIN_REQUIRED")
    try:
        address = ip_address(parsed.hostname)
        if "%" in parsed.hostname:
            raise ValueError("scoped address")
    except ValueError:
        raise ValueError("OBSERVATION_IP_LITERAL_REQUIRED") from None
    if not allow_external and not address.is_loopback:
        raise ValueError("OBSERVATION_LOOPBACK_REQUIRED")
    if allow_external and not re.fullmatch(r"[a-f0-9]{64}", authorization_sha256 or ""):
        raise ValueError("OBSERVATION_EXTERNAL_AUTHORIZATION_REQUIRED")

    # HTTPS uses the normal trust store and IP SAN verification. HTTP never uses TLS.
    tls = (ssl.create_default_context() if parsed.scheme == "https"
           else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    async def receive():
        deadline = time.monotonic() + HTTP_TOTAL_SECONDS
        target = httpx.URL(url.rstrip("/") + "/internal/v1/ops/status?summary_only=true")
        async with asyncio.timeout(HTTP_TOTAL_SECONDS), httpx.AsyncClient(
            timeout=HTTP_TOTAL_SECONDS, trust_env=False, follow_redirects=False, verify=tls,
        ) as client:
            async with client.stream("GET", target, headers={
                "Authorization": "Bearer " + settings.bridge_token.get_secret_value(),
                "Cache-Control": "no-cache",
            }) as response:
                if response.status_code != 200:
                    raise ValueError("OBSERVATION_HTTP_STATUS")
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 131072:
                        raise ValueError("OBSERVATION_RESPONSE_TOO_LARGE")
        result = json.loads(body)
        operations, identity = result["operations"], result["observation"]["api_instance"]
        observed = datetime.fromisoformat(operations["observed_at"])
        completed = datetime.now(UTC)
        if (not re.fullmatch(r"[a-f0-9]{32}", identity) or observed.tzinfo is None
            or observed < completed - timedelta(seconds=5)
            or observed > completed + timedelta(seconds=5) or time.monotonic() >= deadline):
            raise ValueError("OBSERVATION_RESPONSE_INVALID")
        projected = _safe_operations(operations, observed)
        if time.monotonic() >= deadline:
            raise ValueError("OBSERVATION_DEADLINE")
        return projected, identity

    def collect():
        return asyncio.run(receive())

    return collect


class _LoopActivity:
    def __init__(self):
        self.prior = self.start = None
        self.segment = self.intervals = 0
        self.seconds = self.last_segment_seconds = 0

    def sample(self, snapshot, mono, discontinuities):
        reasons = [code for code in discontinuities if code != "OBSERVED_BLOCKERS"]
        current = None
        if not reasons:
            try:
                value = snapshot.get("worker_loop", {"state": "UNOBSERVED"})
                if value["state"] == "UNOBSERVED":
                    reasons.append("NO_LOOP_RECORD")
                elif value["state"] == "INVALID":
                    reasons.append("LOOP_RECORD_INVALID")
                else:
                    current = worker_loop.project_public(
                        value, datetime.fromisoformat(snapshot["observed_at"]),
                    )
                    if current["state"] == "WAITING":
                        reasons.append("LOOP_WAITING_UNVERIFIED")
                        current = None
                    elif current["state"] == "STALE":
                        reasons.append("LOOP_RECORD_STALE")
                        current = None
            except (ValueError, TypeError, KeyError, OverflowError):
                reasons.append("LOOP_RECORD_INVALID")
        if current is not None:
            if self.prior is None:
                reasons.append("LOOP_BASELINE_REQUIRED")
            else:
                prior = self.prior[0]
                if current["session"] != prior["session"]:
                    reasons.append("LOOP_SESSION_CHANGED")
                elif current["sequence"] < prior["sequence"]:
                    reasons.append("LOOP_SEQUENCE_REGRESSED")
                elif current["sequence"] == prior["sequence"]:
                    reasons.append("LOOP_SEQUENCE_UNCHANGED")
                elif current["completed_at"] <= prior["completed_at"]:
                    reasons.append("LOOP_COMPLETION_TIME_NOT_ADVANCED")
        interval = 0
        if reasons:
            self.start = None
            self.last_segment_seconds = 0
        else:
            if self.start is None:
                self.segment += 1
                self.start = self.prior[1]
            interval = (mono - self.prior[1]) / 1e9
            self.intervals += 1
            self.seconds += interval
            self.last_segment_seconds = (mono - self.start) / 1e9
        self.prior = (current, mono) if current is not None else None
        return {"state": "UNPROVEN" if reasons else "ADVANCED",
                "segment": None if reasons else self.segment,
                "reasons": reasons, "interval_seconds": interval}

    def summary(self):
        return {"advancing_intervals": self.intervals,
                "advancing_interval_seconds": self.seconds,
                "last_segment_observed_seconds": self.last_segment_seconds}


def observe(collect, output, *, samples=3, interval_seconds=5, max_gap_seconds=None,
            evidence_type="local_synthetic", authorization_sha256=None, source="local_database"):
    if (type(samples) is not int or not 1 <= samples <= 100000
        or not math.isfinite(interval_seconds) or not 0 < interval_seconds <= 3600
        or evidence_type not in {"local_synthetic", "authorized_runtime"}):
        raise ValueError("OBSERVATION_BOUNDS_INVALID")
    max_gap_seconds = (interval_seconds * 1.5 + 0.1 if max_gap_seconds is None
                       else max_gap_seconds)
    if not math.isfinite(max_gap_seconds) or max_gap_seconds < interval_seconds:
        raise ValueError("OBSERVATION_GAP_INVALID")
    if evidence_type == "authorized_runtime" and not re.fullmatch(
        r"[a-f0-9]{64}", authorization_sha256 or "",
    ):
        raise ValueError("OBSERVATION_RUNTIME_AUTHORIZATION_REQUIRED")
    if authorization_sha256 is not None and not re.fullmatch(
        r"[a-f0-9]{64}", authorization_sha256,
    ):
        raise ValueError("OBSERVATION_AUTHORIZATION_REFERENCE_INVALID")
    run_id, segment, prior, segment_start = uuid.uuid4().hex, 0, None, None
    completed, failures, discontinuities = 0, 0, 0
    loop_activity = _LoopActivity()
    path = Path(output)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        hasher = hashlib.sha256()

        def write(record):
            line = encode(record) + "\n"
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
            hasher.update(line.encode())

        write({"kind": "observation_start", "schema_version": 1, "run_id": run_id,
               "evidence_type": evidence_type, "source": source,
               "authorization_sha256": authorization_sha256,
               "started_at": datetime.now(UTC).isoformat(),
               "interval_seconds": interval_seconds, "max_gap_seconds": max_gap_seconds,
               "worker_health_proven": False})
        for sequence in range(samples):
            wall, mono = datetime.now(UTC), time.monotonic_ns()
            gap = None if prior is None else (mono - prior[1]) / 1e9
            reasons = []
            if gap is not None and (gap < 0 or gap > max_gap_seconds):
                reasons.append("SAMPLE_GAP")
            if prior is not None and abs((wall - prior[0]).total_seconds() - gap) > 2:
                reasons.append("WALL_CLOCK_DISCONTINUITY")
            try:
                snapshot, instance = collect()
                collection_seconds = (time.monotonic_ns() - mono) / 1e9
                if collection_seconds > max_gap_seconds:
                    reasons.append("COLLECTION_GAP")
                if prior is not None and prior[2] is not None and instance is not None and (
                    prior[2] != instance
                ):
                    reasons.append("API_INSTANCE_CHANGED")
                if snapshot["blockers"]:
                    reasons.append("OBSERVED_BLOCKERS")
                error = None
            except Exception:
                snapshot, instance, error = None, None, "SAMPLE_FAILED"
                reasons.append(error)
                failures += 1
            if reasons:
                discontinuities += 1
                segment_start = None
            elif segment_start is None:
                segment += 1
                segment_start = mono
            write({"kind": "observation_sample", "run_id": run_id, "sequence": sequence,
                   "observed_at": wall.isoformat(), "monotonic_ns": mono,
                   "collection_seconds": (time.monotonic_ns() - mono) / 1e9,
                   "interval_seconds": gap, "api_instance": instance,
                   "restart_detection": "AVAILABLE" if instance else "UNAVAILABLE",
                   "segment": segment if segment_start is not None else None,
                   "discontinuities": reasons, "error": error, "snapshot": snapshot,
                   "worker_loop_activity": loop_activity.sample(snapshot, mono, reasons)})
            completed += 1
            prior = wall, mono, instance
            if sequence + 1 < samples:
                # Long collections count toward the next interval, never compress gaps.
                time.sleep(max(0, interval_seconds - (time.monotonic_ns() - mono) / 1e9))
        summary = {"kind": "observation_end", "run_id": run_id, "samples": completed,
                   "failures": failures, "discontinuities": discontinuities,
                   "clear_segment_observed_seconds": (
                       (prior[1] - segment_start) / 1e9 if segment_start is not None else 0
                   ), "worker_health_proven": False,
                   "worker_loop_activity": loop_activity.summary(),
                   "ended_at": datetime.now(UTC).isoformat(),
                   "file_sha256_before_summary": hasher.hexdigest()}
        write(summary)
    return summary
