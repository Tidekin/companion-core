"""Optional initial context selection at the existing SDK request/checkpoint seam.

Only trusted Core groups are removable. This module neither executes tools nor
owns accounting, source authority, scheduling, model loops or persistence.
"""

import json
import re
from copy import deepcopy
from dataclasses import replace

from companion.agent_messages import request_wire, to_wire_messages
from companion.context_selection import BACKGROUND, OLD_HISTORY, TOPIC, drop_order
from companion.contracts import CoreError
from companion.paid_budget import sha
from companion.privacy import epoch
from companion.store import encode
from companion.token_measurement import measure_request

KINDS = {"rules", "current", "reference", "history", "media"}
REASONS = {"SOFT_LIMIT", "INPUT_LIMIT", "CONTEXT_WINDOW_LIMIT"}
MAX_GROUPS = 96
MAX_BYTES = 65536


def pressure(settings, measured, *, soft_limit=None, scenario="chat"):
    """Shared optional-selection target; hard admission still checks every request."""
    approval = settings.paid_model
    if approval and measured.reserved_input_tokens > approval.max_input_tokens:
        return "INPUT_LIMIT"
    from companion.video_input import SCENARIOS

    route = approval.video_input if approval and scenario in SCENARIOS else approval
    if (
        route
        and route.max_context_tokens is not None
        and measured.reserved_input_tokens + measured.output_allowance > route.max_context_tokens
    ):
        return "CONTEXT_WINDOW_LIMIT"
    limit = settings.context_soft_tokens if soft_limit is None else soft_limit
    return "SOFT_LIMIT" if measured.reserved_input_tokens > limit else None


def _proofs(snapshots):
    return [item for item in snapshots if item["kind"] != "context_plan"]


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _integer(value, maximum=MAX_GROUPS):
    return type(value) is int and 0 <= value <= maximum


def _ids(values):
    return (
        isinstance(values, list)
        and len(values) <= MAX_GROUPS
        and all(
            isinstance(value, str)
            and re.fullmatch(r"g[0-9]{1,2}", value)
            and int(value[1:]) < MAX_GROUPS
            for value in values
        )
        and len(set(values)) == len(values)
    )


def _record(record, proofs, version):
    if not isinstance(record, dict) or set(record) != {
        "payload_sha256",
        "measurement_sha256",
        "source_proof_count",
        "source_proofs_sha256",
        "selected",
        "dropped",
        "request_id",
        "state",
    }:
        raise ValueError
    count = record["source_proof_count"]
    if (
        not _integer(count, len(proofs))
        or record["source_proofs_sha256"] != sha(proofs[:count])
        or not all(_digest(record[key]) for key in ("payload_sha256", "measurement_sha256"))
        or not _ids(record["selected"])
        or record["state"] not in {"PLANNED", "ADMITTED", "NOT_ADMITTED"}
        or not isinstance(record["dropped"], list)
        or len(record["dropped"]) > MAX_GROUPS
    ):
        raise ValueError
    dropped = []
    for item in record["dropped"]:
        if (
            not isinstance(item, dict)
            or set(item) != ({"group_id", "kind", "reason"}
                             | ({"priority"} if version == 2 else set()))
            or version == 2 and not _integer(item.get("priority"), TOPIC)
            or item["kind"] not in {"reference", "history"}
            or item["reason"] not in REASONS
        ):
            raise ValueError
        dropped.append(item["group_id"])
    if not _ids(dropped) or set(dropped) & set(record["selected"]):
        raise ValueError
    request = record["request_id"]
    if record["state"] == "ADMITTED":
        if not isinstance(request, str) or not 1 <= len(request) <= 128:
            raise ValueError
    elif request is not None:
        raise ValueError


def validate(snapshots, job, current_epoch):
    """Validate current and persisted v1 layouts; unknown versions grant no pruning."""
    try:
        entries = [item for item in snapshots if item["kind"] == "context_plan"]
        if not entries:
            return None
        if len(entries) != 1 or len(encode(entries[0]).encode()) > MAX_BYTES:
            raise ValueError
        entry = entries[0]
        if set(entry) != {"kind", "metadata"} or set(entry["metadata"]) != {"core_context"}:
            raise ValueError
        plan = entry["metadata"]["core_context"]
        if not isinstance(plan, dict):
            raise ValueError
        if type(plan.get("version")) is not int:
            raise ValueError
        if plan["version"] not in {1, 2}:
            return None
        if set(plan) != ({
            "version",
            "job_id",
            "privacy_epoch",
            "authority_sha256",
            "initial_body_sha256",
            "body_sha256",
            "prefix_count",
            "initial_source_proof_count",
            "initial_source_proofs_sha256",
            "groups",
            "requests",
            "pending",
        } | ({"assembly_dropped"} if plan["version"] == 2 else set())):
            raise ValueError
        if plan["version"] == 2:
            if (not isinstance(plan["assembly_dropped"], list)
                or len(plan["assembly_dropped"]) > MAX_GROUPS):
                raise ValueError
            indices = []
            for item in plan["assembly_dropped"]:
                if (not isinstance(item, dict)
                    or set(item) != {"index", "kind", "priority", "reason"}
                    or not _integer(item["index"], MAX_GROUPS - 1)
                    or item["kind"] not in {"reference", "history"}
                    or not _integer(item["priority"], TOPIC)
                    or item["reason"] not in REASONS):
                    raise ValueError
                indices.append(item["index"])
            if len(set(indices)) != len(indices):
                raise ValueError
        proofs = _proofs(snapshots)
        count = plan["initial_source_proof_count"]
        if (
            plan["job_id"] != job["job_id"]
            or type(plan["privacy_epoch"]) is not int
            # This is the layout's capture generation, not a replacement for
            # original source revalidation. Unrelated erasure may advance it.
            or not 0 <= plan["privacy_epoch"] <= current_epoch
            or plan["authority_sha256"] != sha(json.loads(job["authority_json"]))
            or not _integer(count, len(proofs))
            or plan["initial_source_proofs_sha256"] != sha(proofs[:count])
            or not _digest(plan["initial_body_sha256"])
            or not _digest(plan["body_sha256"])
            or not isinstance(plan["groups"], list)
            or not 1 <= len(plan["groups"]) <= MAX_GROUPS
            or not _integer(plan["prefix_count"], 512)
        ):
            raise ValueError
        offset, ids = 0, []
        for group in plan["groups"]:
            if (
                not isinstance(group, dict)
                or set(group)
                != ({"group_id", "kind", "wire_start", "wire_count", "messages_sha256", "required"}
                    | ({"priority"} if plan["version"] == 2 else set()))
                or plan["version"] == 2 and not _integer(group.get("priority"), TOPIC)
                or group["kind"] not in KINDS
                or type(group["required"]) is not bool
                or not _integer(group["wire_start"], 512)
                or group["wire_start"] != offset
                or not _integer(group["wire_count"], 512)
                or not group["wire_count"]
                or not _digest(group["messages_sha256"])
                or not group["required"]
                and group["kind"] not in {"reference", "history"}
            ):
                raise ValueError
            ids.append(group["group_id"])
            offset += group["wire_count"]
        if (
            not _ids(ids)
            or offset != plan["prefix_count"]
            or not {"rules", "current"} <= {group["kind"] for group in plan["groups"]}
            or not isinstance(plan["requests"], list)
            or len(plan["requests"]) > 10
        ):
            raise ValueError
        for record in plan["requests"]:
            _record(record, proofs, plan["version"])
            if record["state"] != "ADMITTED":
                raise ValueError
        request_ids = [record["request_id"] for record in plan["requests"]]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError
        if plan["pending"] is not None:
            _record(plan["pending"], proofs, plan["version"])
            if plan["pending"]["state"] == "ADMITTED":
                raise ValueError
        latest = plan["pending"] or (plan["requests"][-1] if plan["requests"] else None)
        if latest is not None and latest["selected"] != ids:
            raise ValueError
        return plan
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        raise CoreError("CONTEXT_PLAN_INVALID", 409) from error


def check(core, job, snapshots):
    return check_bound(core.store, job, snapshots, epoch(core.store))


def check_bound(store, job, snapshots, current_epoch):
    """Read-only shape/admission binding; never replaces source revalidation."""
    plan = validate(snapshots, job, current_epoch)
    if plan is not None:
        for record in plan["requests"]:
            row = store.one(
                "SELECT a.*,m.job_id FROM model_admissions a "
                "JOIN model_usage m USING(request_id) WHERE request_id=?",
                (record["request_id"],),
            )
            try:
                valid = (
                    row
                    and row["job_id"] == job["job_id"]
                    and row["payload_sha256"] == record["payload_sha256"]
                    and sha(json.loads(row["measurement_json"])) == record["measurement_sha256"]
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise CoreError("CONTEXT_PLAN_INVALID", 409)
    return plan


def initialize(core, job, messages, blocks, snapshots, *, assembly_dropped=()):
    """Called by the trusted Core assembler after attachments and original source pinning."""
    if any(item["kind"] == "context_plan" for item in snapshots):
        raise CoreError("CONTEXT_PLAN_INVALID", 409)
    groups, offset = [], 0
    for kind, block, required, priority in blocks:
        if not block:
            continue
        span = messages[offset : offset + len(block)]
        if len(span) != len(block) or any(
            left is not right for left, right in zip(span, block, strict=True)
        ):
            return  # An inexact initial layout grants no selection rights.
        # Custody already binds every attached image. Preserve its entire group.
        required = required or any(isinstance(message["content"], list) for message in block)
        groups.append(
            {
                "group_id": "g" + str(len(groups)),
                "kind": kind,
                "wire_start": offset,
                "wire_count": len(block),
                "messages_sha256": sha(block),
                "required": required,
                "priority": priority,
            }
        )
        offset += len(block)
    if offset != len(messages):
        return
    proofs = _proofs(snapshots)
    plan = {
        "version": 2,
        "assembly_dropped": deepcopy(list(assembly_dropped)),
        "job_id": job["job_id"],
        "privacy_epoch": epoch(core.store),
        "authority_sha256": sha(json.loads(job["authority_json"])),
        "initial_body_sha256": sha(messages),
        "body_sha256": sha(messages),
        "prefix_count": len(messages),
        "initial_source_proof_count": len(proofs),
        "initial_source_proofs_sha256": sha(proofs),
        "groups": groups,
        "requests": [],
        "pending": None,
    }
    entry = {"kind": "context_plan", "metadata": {"core_context": plan}}
    if len(groups) > MAX_GROUPS or offset > 512 or len(encode(entry).encode()) > MAX_BYTES:
        return  # Oversized layouts retain the full input and original hard admission.
    snapshots.append(entry)
    check(core, job, snapshots)


def _filter(messages, removed):
    """Delete selected wire spans using public SDK types, retaining response/usage objects."""
    from pydantic_ai.messages import ModelRequest

    selected, offset = [], 0
    for message in messages:
        if isinstance(message, ModelRequest):
            parts = []
            for part in message.parts:
                width = len(to_wire_messages([replace(message, parts=[part])]))
                if not set(range(offset, offset + width)) & removed:
                    parts.append(part)
                offset += width
            if parts or message.instructions or message is messages[-1]:
                selected.append(
                    replace(message, parts=parts) if len(parts) != len(message.parts) else message
                )
        else:
            if offset in removed and (message.metadata or {}).get("core_request_id"):
                raise CoreError("CONTEXT_PLAN_CHANGED", 409)
            if offset not in removed:
                selected.append(message)
            offset += len(to_wire_messages([message]))
    return selected


def plan_request(core, job, snapshots, request_context):
    plan = check(core, job, snapshots)
    if plan is None:
        return request_context.messages
    messages = list(request_context.messages)
    body = to_wire_messages(messages)
    prefix = body[: plan["prefix_count"]]
    if sha(prefix) != plan["body_sha256"]:
        raise CoreError("CONTEXT_PLAN_CHANGED", 409)
    for group in plan["groups"]:
        start, width = group["wire_start"], group["wire_count"]
        if sha(prefix[start : start + width]) != group["messages_sha256"]:
            raise CoreError("CONTEXT_PLAN_CHANGED", 409)
    pending = plan["pending"]
    dropped = deepcopy(pending["dropped"]) if pending and pending["state"] == "NOT_ADMITTED" else []
    parameters, removed = request_context.model_request_parameters, set()
    groups = list(plan["groups"])

    # Keep semantic claims and tool call/result pairing. Under pressure, repeated
    # inspection metadata in native memory results is cheaper to remove than the
    # original conversation's statement, quotation and attribution as a whole.
    original = measure_request(core.settings, request_wire(messages, parameters), "chat",
        tools=[*parameters.function_tools, *parameters.output_tools])
    if pressure(core.settings, original):
        messages = _compact_memory_tools(messages)
    base_messages = messages

    def measure():
        return measure_request(
            core.settings,
            request_wire(messages, parameters),
            "chat",
            tools=[*parameters.function_tools, *parameters.output_tools],
        )

    measured = measure()
    optional = drop_order([{**group, "priority": group.get("priority", (
        OLD_HISTORY if group["kind"] == "history" else BACKGROUND))} for group in groups])
    for group in optional:
        reason = pressure(core.settings, measured)
        if reason is None:
            break
        dropped.append({"group_id": group["group_id"], "kind": group["kind"], "reason": reason,
                        **({"priority": group["priority"]} if plan["version"] == 2 else {})})
        removed.update(range(group["wire_start"], group["wire_start"] + group["wire_count"]))
        messages = _filter(base_messages, removed)
        groups = [item for item in groups if item["group_id"] != group["group_id"]]
        measured = measure()
    offset = 0
    for group in groups:
        group["wire_start"] = offset
        offset += group["wire_count"]
    plan["groups"], plan["prefix_count"] = groups, offset
    plan["body_sha256"] = sha(to_wire_messages(messages)[:offset])
    proofs = _proofs(snapshots)
    plan["pending"] = {
        "payload_sha256": measured.payload_sha256,
        "measurement_sha256": sha(measured.receipt()),
        "source_proof_count": len(proofs),
        "source_proofs_sha256": sha(proofs),
        "selected": [group["group_id"] for group in groups],
        "dropped": dropped,
        "request_id": None,
        "state": "PLANNED",
    }
    check(core, job, snapshots)
    return messages


def _compact_memory_tools(messages):
    """Presentation only: every fact, source ID, version and attribution survives.

    Full provenance stays in the native store and source snapshots. This performs
    no semantic relevance inference and never removes a call or return part.
    """
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    selected = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            selected.append(message)
            continue
        parts = []
        for part in message.parts:
            if not isinstance(part, ToolReturnPart) or part.tool_name != "memory_recall":
                parts.append(part)
                continue
            value = part.content
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    parts.append(part)
                    continue
            if not isinstance(value, dict):
                parts.append(part)
                continue
            value = deepcopy(value)
            for field in ("facts", "conflicts"):
                for fact in value.get(field, []):
                    if not isinstance(fact, dict):
                        continue
                    evidence = fact.pop("evidence", None)
                    if isinstance(evidence, list):
                        # IDs already occur in evidence_ids; preserve source dates/type.
                        fact["source_times"] = [{k: row[k] for k in (
                            "event_id", "source", "occurred_at")
                                                 if k in row} for row in evidence]
                    fact.pop("updated_at", None)
            parts.append(replace(part, content=value))
        selected.append(replace(message, parts=parts))
    return selected


def check_request(core, job, snapshots, wire, tools):
    plan = check(core, job, snapshots)
    if plan is not None:
        measured = measure_request(core.settings, wire, "chat", tools=tools)
        pending = plan["pending"]
        if (
            pending is None
            or pending["payload_sha256"] != measured.payload_sha256
            or pending["measurement_sha256"] != sha(measured.receipt())
        ):
            raise CoreError("CONTEXT_PLAN_CHANGED", 409)


def admitted(core, job, snapshots, request_id):
    plan = check(core, job, snapshots)
    if plan is not None and plan["pending"] is not None:
        record = deepcopy(plan["pending"])
        record.update(request_id=request_id, state="ADMITTED")
        plan["requests"].append(record)
        plan["pending"] = None
        check(core, job, snapshots)


def not_admitted(core, job, snapshots):
    plan = check(core, job, snapshots)
    if plan is not None and plan["pending"] is not None:
        plan["pending"]["state"] = "NOT_ADMITTED"
