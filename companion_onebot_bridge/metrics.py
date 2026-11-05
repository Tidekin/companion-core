import asyncio
import json
import os
import re
import time
import uuid

import httpx

PROBE_STAGES = {"get_login_info": "t_onebot_probe_login", "get_status": "t_onebot_probe_status",
                "get_version_info": "t_onebot_probe_version"}

CORE_RPC_STAGES = {
    "/internal/v1/owner-authority/check": "t_core_owner_check",
    "/internal/v1/owner-authority/inputs/approve": "t_core_input_approve",
    "/internal/v1/inbox/events": "t_core_inbox",
    "/internal/v1/deliveries/lease": "t_core_delivery_lease",
    "/internal/v1/owner-authority/send-check": "t_core_send_check",
}
DELIVERY_RPC_STAGES = {"begin-send": "t_core_delivery_begin_send",
                       "result": "t_core_delivery_result"}

STAGES = {
    "t_onebot_received",
    "t_spool_commit",
    "t_core_ingest_ack",
    "t_outbox_lease",
    "t_begin_send",
    "t_onebot_send_ack",
    "delivery_uncertain",
    "duplicate_suppressed",
    "event_rejected",
    "ws_connected",
    "ws_disconnected",
    "core_unavailable",
    "send_unavailable",
    "media_unavailable",
    "t_bridge_send_ack",
    "t_reply_group_ack",
    "t_reply_segments_ack",
}

HTTP_STARTED = "companion_onebot_http_started_ns"


def probe_finished(name, started_ns, error=None):
    _request_finished(PROBE_STAGES[name], started_ns, error)


def send_finished(started_ns, error=None):
    """The send call includes its guard/preflight work and provider transport."""
    _request_finished("t_onebot_send_result", started_ns, error)


def http_finished(request, error=None):
    """One delegated OneBot HTTP attempt, including body reads, excluding its guard."""
    started = request.extensions.pop(HTTP_STARTED, None)
    if type(started) is int:
        _request_finished("onebot_http", started, error)


def core_rpc_finished(path, started_ns, error=None):
    """Select only fixed labels; dynamic delivery identifiers are never exported."""
    if not isinstance(path, str):
        return
    stage = CORE_RPC_STAGES.get(path)
    if stage is None and (match := re.fullmatch(
        r"/internal/v1/deliveries/msg_[a-f0-9]{32}/(begin-send|result)", path,
    )):
        stage = DELIVERY_RPC_STAGES[match[1]]
    if stage is not None:
        _request_finished(stage, started_ns, error)


def _request_finished(stage, started_ns, error=None):
    """Opt-in elapsed spans; no request, response or exception text is exported."""
    if os.environ.get("COMPANION_TELEMETRY") != "1":
        return
    if error is None:
        error_class = "none"
    elif isinstance(error, asyncio.CancelledError):
        error_class = "cancelled"
    elif isinstance(error, httpx.TimeoutException):
        error_class = "timeout"
    elif isinstance(error, httpx.HTTPStatusError):
        error_class = "http_status"
    elif isinstance(error, httpx.TransportError):
        error_class = "transport"
    elif isinstance(error, ValueError):
        error_class = "validation"
    else:
        error_class = "unexpected"
    evidence = os.environ.get("COMPANION_TELEMETRY_EVIDENCE", "LIVE_ONEBOT")
    if evidence not in {"LIVE_ONEBOT", "OFFLINE_FIXTURE"}:
        evidence = "LIVE_ONEBOT"
    finished = time.perf_counter_ns()
    record = {"schema_version": 1, "evidence_type": evidence, "stage": stage,
              "monotonic_ns": finished, "elapsed_ms": round(max(0, finished - started_ns) / 1e6, 4),
              "error_class": error_class}
    try:
        print(json.dumps(record), flush=True)
    except Exception:
        pass  # Telemetry cannot change delivery state or request a retry.


class Metrics:
    def __init__(self, evidence_type="LIVE_ONEBOT", sink=None):
        if evidence_type not in {"OFFLINE_FIXTURE", "LIVE_ONEBOT"}:
            raise ValueError("INVALID_EVIDENCE_TYPE")
        self.evidence_type = evidence_type
        self.sink = sink or (lambda record: print(json.dumps(record), flush=True))
        self.session = uuid.uuid4().hex
        self.received = {}
        self.first_acks = {}

    def emit(self, stage, trace=None, *, elapsed_ms=None, at_ns=None):
        if stage not in STAGES:
            raise ValueError("INVALID_METRIC_STAGE")
        record = {
            "schema_version": 1,
            "evidence_type": self.evidence_type,
            "session": self.session,
            "stage": stage,
            "monotonic_ns": time.perf_counter_ns() if at_ns is None else at_ns,
        }
        if trace and re.fullmatch(r"trace_[a-f0-9]{32,64}", trace):
            record["trace_id"] = trace
        if elapsed_ms is not None:
            record["elapsed_ms"] = round(max(0, elapsed_ms), 4)
        try:
            self.sink(record)
        except Exception:
            pass  # Telemetry cannot change delivery state or request a retry.

    def start(self, payload, received_ns):
        key = payload["event_id"]
        if key not in self.received:
            if len(self.received) >= 1000:
                oldest = next(iter(self.received))
                self.received.pop(oldest)
                self.first_acks.pop(oldest, None)
            self.received[key] = (received_ns, payload["trace_id"])
        self.emit("t_onebot_received", payload["trace_id"], at_ns=received_ns)

    def accepted(self, payload):
        previous = self.received.get(payload["event_id"])
        duration = (time.perf_counter_ns() - previous[0]) / 1e6 if previous else None
        self.emit("t_core_ingest_ack", payload["trace_id"], elapsed_ms=duration)

    def delivered(self, delivery_key, provider_id, ack_ns, *, segment=None):
        key = delivery_key.split(":")[1] if delivery_key.startswith("reply:") else ""
        previous = self.received.get(key)
        if previous and provider_id:
            if segment is None or segment["index"] == 0:
                self.first_acks[key] = ack_ns
                self.emit("t_bridge_send_ack", previous[1],
                          elapsed_ms=(ack_ns - previous[0]) / 1e6, at_ns=ack_ns)
            if segment is None or segment["index"] == segment["count"] - 1:
                self.emit("t_reply_group_ack", previous[1],
                          elapsed_ms=(ack_ns - previous[0]) / 1e6, at_ns=ack_ns)
                first = self.first_acks.get(key)
                if first is not None:
                    self.emit("t_reply_segments_ack", previous[1],
                              elapsed_ms=(ack_ns - first) / 1e6, at_ns=ack_ns)
            else:
                return
        self.received.pop(key, None)
        self.first_acks.pop(key, None)


def emit(kind, **values):
    print(json.dumps({"event": kind, **values}), flush=True)
