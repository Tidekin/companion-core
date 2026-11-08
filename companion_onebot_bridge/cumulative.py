"""Local input/send limits folded from complete original Window Journal history.

Core owns model and account policy admission. The Bridge only understands the
HTTP approval contract and its existing Spool/Window delivery responsibilities.
"""

import hashlib
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .spool import encode

SHA = re.compile(r"[a-f0-9]{64}")
DIMENSIONS = {"model_attempts", "input_tokens", "output_tokens",
              "owner_inputs", "send_attempts"}
IDENTITY = ("account_ref", "provider", "currency", "timezone", "owner_identity_sha256")
LEGACY_DOMAIN = b"companion-core/cumulative-window-inventory/v1\x00"


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def timestamp(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("CUMULATIVE_AWARE_CLOCK_REQUIRED")
    return parsed.astimezone(UTC)


def periods(value, timezone):
    local = timestamp(value).astimezone(ZoneInfo(timezone))
    return local.date().isoformat(), local.strftime("%Y-%m")


def scope(plan):
    return plan.value.get("live_text", {}).get("cumulative")


def validate(value, config, start, end):
    fields = {"version", "account_ref", "policy_id", "approval_sha256", "account_evidence_sha256",
              "credential_sha256", "key_ownership_sha256", "provider", "currency", "timezone",
              "owner_identity_sha256", "starts_at", "expires_at", "expected_journal_head_sha256",
              "predecessor_policy_sha256", "legacy_liabilities_sha256",
              "legacy_adoption_evidence_sha256", "request", "window", "day", "month", "account",
              "concurrency", "legacy_window_inventory"}
    optional = {"predecessor_policy_sha256", "legacy_liabilities_sha256",
                "legacy_adoption_evidence_sha256"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int
        or value["version"] != 2 or value["currency"] != "USD"
        or not isinstance(value["policy_id"], str)
        or not re.fullmatch(r"quota_[a-f0-9]{32}", value["policy_id"])
        or not isinstance(value["provider"], str) or not 1 <= len(value["provider"]) <= 64
        or type(value["concurrency"]) is not int or not 1 <= value["concurrency"] <= 32):
        raise ValueError("INVALID_CUMULATIVE_SCOPE")
    for key in fields:
        if key.endswith("_sha256") or key == "account_ref":
            if value[key] is None and key in optional:
                continue
            if not isinstance(value[key], str) or not SHA.fullmatch(value[key]):
                raise ValueError("INVALID_CUMULATIVE_SCOPE")
    if (value["predecessor_policy_sha256"] is None and value["legacy_liabilities_sha256"] is None
        or value["owner_identity_sha256"] != digest([config.owner_qq_id, config.bot_qq_id])
        or not timestamp(value["starts_at"]) <= timestamp(start) < timestamp(end) <= timestamp(
            value["expires_at"])):
        raise ValueError("INVALID_CUMULATIVE_SCOPE")
    ZoneInfo(value["timezone"])
    for period in ("request", "window", "day", "month", "account"):
        caps = value[period]
        expected = DIMENSIONS - {"owner_inputs", "send_attempts"} if period == "request" else (
            DIMENSIONS)
        if (not isinstance(caps, dict) or set(caps) != expected
            or any(type(v) is not int or v < 0 or v > 2**63 - 1 for v in caps.values())):
            raise ValueError("INVALID_CUMULATIVE_SCOPE")


def fold(window, history):
    current = scope(window.plan)
    if not current:
        if any(scope(plan) for plan, _ in history):
            raise ValueError("CUMULATIVE_SCOPE_REQUIRED")
        return None
    items, clock = {}, None
    adoption = current["legacy_adoption_evidence_sha256"] or any(
        policy and policy["predecessor_policy_sha256"] is None
        and policy["legacy_adoption_evidence_sha256"]
        for policy in (scope(plan) for plan, _ in history))
    for plan, records in history:
        old = scope(plan)
        if old:
            if any(old[k] != current[k] for k in IDENTITY):
                raise ValueError("CUMULATIVE_ACCOUNT_BINDING_CHANGED")
        elif not adoption:
            raise ValueError("CUMULATIVE_LEGACY_ATTRIBUTION_REQUIRED")
        for record in records:
            at = timestamp(record["at"])
            clock = at if clock is None else max(clock, at)
            kind = record["kind"]
            if kind in {"CAPTURED", "INPUT"}:
                category, source = "owner_inputs", record["data"]["event_hash"]
            elif kind == "RESERVED":
                category, source = "send_attempts", record["data"]["message_hash"]
            else:
                continue
            key = category + ":" + source
            if not isinstance(source, str) or not SHA.fullmatch(source) or key in items:
                raise ValueError("CUMULATIVE_WINDOW_SOURCE_CONFLICT")
            items[key] = {"at": at, "plan_sha256": plan.sha256,
                          "window_id": plan.value["window_id"], "dimension": category}
    # The original Spool remains authoritative. Lost/extra projections are a stop,
    # never an opportunity to rebuild a smaller liability inventory.
    projected = {}
    for table, source, dimension in (("window_inputs", "event_id", "owner_inputs"),
                                     ("window_attempts", "message_id", "send_attempts")):
        for row in window.db.execute(f"SELECT {source},plan_hash FROM {table}"):
            projected[dimension + ":" + digest(row[source])] = row["plan_hash"]
    if projected != {k: v["plan_sha256"] for k, v in items.items()}:
        raise ValueError("CUMULATIVE_SPOOL_RECONCILIATION_REQUIRED")
    return {"items": items, "clock": clock, "policy": current}


def inspect(window):
    if not scope(window.plan):
        return None
    records, _ = window.read_journal()
    return fold(window, window.verify_history(records))


def check(window, state, now):
    if state is None:
        return
    policy = state["policy"]
    now = timestamp(now)
    if state["clock"] and now < state["clock"]:
        raise ValueError("CUMULATIVE_CLOCK_ROLLBACK")
    if not timestamp(policy["starts_at"]) <= now < timestamp(policy["expires_at"]):
        raise ValueError("CUMULATIVE_POLICY_EXPIRED")


def admit(window, dimension, source=None, *, amount=1):
    state = inspect(window)
    if state is None:
        return
    now = window.clock()
    check(window, state, now)
    if source and dimension + ":" + source in state["items"]:
        raise ValueError("CUMULATIVE_WINDOW_SOURCE_CONFLICT")
    policy = state["policy"]
    day, month = periods(now, policy["timezone"])
    for period in ("window", "day", "month", "account"):
        used = sum(1 for v in state["items"].values() if v["dimension"] == dimension and (
            period == "account" or period == "window" and v["window_id"] == (
                window.plan.value["window_id"])
            or period == "day" and periods(v["at"], policy["timezone"])[0] == day
            or period == "month" and periods(v["at"], policy["timezone"])[1] == month))
        if used + amount > policy[period][dimension]:
            raise ValueError("CUMULATIVE_" + period.upper() + "_" + dimension.upper() + "_CAP")


def dispatch(window, state, message_id, now):
    if state is None:
        return
    check(window, state, now)
    prior = state["items"].get("send_attempts:" + digest(message_id))
    if not prior or periods(prior["at"], state["policy"]["timezone"]) != periods(
        now, state["policy"]["timezone"],
    ):
        raise ValueError("CUMULATIVE_DISPATCH_PERIOD_CHANGED")


def legacy_inventory(window):
    """Signed anonymous timestamps from the full original journal chain, offline."""
    from .owner_resolution import unsettled_attempts, unsettled_inputs

    window.verify(active=False)
    if (window.row()["state"] != "STOPPED" or window.plan.mode != "live_text"
        or not window.plan.config.capture_signing_key
        or unsettled_attempts(window)
        or unsettled_inputs(window)):
        raise ValueError("CUMULATIVE_STOPPED_SETTLED_HISTORY_REQUIRED")
    records, _ = window.read_journal()
    reservations, projection = [], {}
    for plan, entries in window.verify_history(records):
        head = digest(entries[-1])
        for record in entries:
            if record["kind"] not in {"RESERVED", "INPUT", "CAPTURED"}:
                continue
            kind = "send" if record["kind"] == "RESERVED" else "input"
            source = record["data"]["message_hash" if kind == "send" else "event_hash"]
            if kind + source in projection:
                raise ValueError("CUMULATIVE_WINDOW_SOURCE_CONFLICT")
            projection[kind + source] = plan.sha256
            reservations.append({"kind": kind, "source_sha256": source,
                "window_id": plan.value["window_id"],
                "at": timestamp(record["at"]).isoformat().replace("+00:00", "Z"),
                "record_sha256": digest(record), "journal_head_sha256": head})
    expected = {}
    for table, column, kind in (("window_inputs", "event_id", "input"),
                                ("window_attempts", "message_id", "send")):
        expected.update({kind + digest(row[column]): row["plan_hash"] for row in window.db.execute(
            f"SELECT {column},plan_hash FROM {table}")})
    if projection != expected:
        raise ValueError("CUMULATIVE_SPOOL_RECONCILIATION_REQUIRED")
    value = {"kind": "cumulative_window_inventory_v1",
             "identity_sha256": window.plan.value["identity_sha256"],
             "budget_journal_id": window.plan.value["live_text"]["budget_journal_id"],
             "reservations": sorted(reservations, key=lambda v: (v["kind"], v["source_sha256"]))}
    signature = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
        window.plan.config.capture_signing_key)).sign(LEGACY_DOMAIN + encode(value).encode()).hex()
    return value | {"signature": signature}
