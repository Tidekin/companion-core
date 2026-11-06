"""Explicit renewal in the original carry journal; no settlement or input replay."""

import json
from datetime import datetime

from companion import model_carry, owner_authority, paid_budget
from companion.contracts import CoreError
from companion_delivery_protocol import digest, hash_value


def manifest(store, settings, authority, window, witness, view, original, stop, work):
    prior = view["model_carries"].get(original.policy.unknown_carry_sha256)
    if prior is None:
        raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
    stopped = model_carry._continuation(
        store,
        settings,
        authority,
        prior,
        view,
        approved_contract_sha256=original.policy.paid_contract_sha256,
    )
    unknown = {k for k, v in view["model_latest"].items() if v["state"] == "UNKNOWN"}
    retained = {item["request_id"]: item for item in prior["manifest"]["unknowns"]}
    inputs = []
    fresh = {key: store.one("SELECT * FROM model_admissions WHERE request_id=?", (key,))
             for key in unknown - retained.keys()}
    revisions = store.rows(
        "SELECT * FROM owner_authority_revisions WHERE grant_id=? "
        "AND revision BETWEEN ? AND ? AND state='ACTIVE' ORDER BY revision",
        (authority["grant_id"], prior["manifest"]["authority"]["revision"] + 2,
         authority["revision"]),
    )
    allowed = [owner_authority.reference(row).as_dict() for row in revisions]
    if any(not row or json.loads(row["authority_json"]) not in allowed
           or row["contract_sha256"] != prior["successor_paid_contract_sha256"]
           for row in fresh.values()):
        raise CoreError("OWNER_MODEL_RENEWAL_CURRENT_WORK_REQUIRED", 409)
    current_ids = {key for key, row in fresh.items()
                   if json.loads(row["authority_json"]) == authority}
    if current_ids:
        # Current inputs keep their original source-work proof. Earlier revisions
        # already crossed the verified STOP/ACTIVE chain with all work quiescent;
        # their closed liability is retained without transferring their inputs again.
        current = model_carry._local_receipt(store, settings, authority, prior, stopped, view)
        if set(current["request_ids"]) != current_ids:
            raise CoreError("OWNER_MODEL_RENEWAL_CURRENT_WORK_REQUIRED", 409)
        inputs = current["inputs"]
    for request_id, admission in fresh.items():
        usage = store.one("SELECT job_id FROM model_usage WHERE request_id=?", (request_id,))
        financial = view["model_latest"][request_id]
        retained[request_id] = {
            **model_carry._tuple(admission),
            "logical_request_id": admission["logical_request_id"],
            "reserved_micro_usd": admission["reserved_micro_usd"],
            "minimum_micro_usd": financial["minimum_micro_usd"],
            "settlement_sha256": paid_budget.sha(financial),
            "tool_sha256": digest(store.rows(
                "SELECT * FROM agent_tool_calls WHERE job_id=? AND state='STARTED'",
                (usage["job_id"],),
            )),
        }
    carried = {item["event_id"] for item in inputs}
    if unknown != retained.keys() or any(
        not state["terminal"] and event not in carried for event, state in work.items()
    ):
        raise CoreError("OWNER_MODEL_RENEWAL_CURRENT_WORK_REQUIRED", 409)
    windows = sorted(
        set(prior["manifest"]["budget_scope_window_ids"])
        | {prior["manifest"]["successor_window_id"]}
    )
    return {
        "authority": authority,
        "core_stop_sha256": stop["core_stop_sha256"],
        "budget_journal_id": original.journal_id,
        "successor_window_id": window,
        "budget_scope_window_ids": windows,
        "unknowns": [retained[key] for key in sorted(retained)],
        "inputs": inputs,
        "closed_executors_sha256": digest(witness),
        "predecessor_carry_sha256": prior["carry_sha256"],
        "budget_baseline_sha256": digest(
            {
                key: value
                for key, value in view["model_latest"].items()
                if store.one("SELECT window_id FROM model_admissions WHERE request_id=?", (key,))[
                    "window_id"
                ]
                in windows
            }
        ),
    }


def fold(store, row, record_sha, journal_id, authorities, latest, blocked, closures, carries):
    if set(row) != model_carry.CARRY_FIELDS or not hash_value(row["authorization_sha256"]):
        raise ValueError("model renewal record")
    value = row["manifest"]
    fields = {
        "authority",
        "core_stop_sha256",
        "budget_journal_id",
        "successor_window_id",
        "budget_scope_window_ids",
        "unknowns",
        "inputs",
        "closed_executors_sha256",
        "predecessor_carry_sha256",
        "budget_baseline_sha256",
    }
    prior = carries.get(value.get("predecessor_carry_sha256"))
    if (
        set(value) != fields
        or prior is None
        or digest(value) != row["carry_sha256"]
        or row["carry_sha256"] in carries
        or value["budget_journal_id"] != journal_id
        or blocked
        or not authorities
        or value["closed_executors_sha256"] != digest(row["closed_executors"])
        or value["budget_scope_window_ids"]
        != sorted(
            set(prior["manifest"]["budget_scope_window_ids"])
            | {prior["manifest"]["successor_window_id"]}
        )
        or value["successor_window_id"] in value["budget_scope_window_ids"]
    ):
        raise ValueError("model renewal custody")
    authority = value["authority"]
    origin = prior["manifest"]["authority"]
    revisions = [r for r in authorities if r["revision"] >= origin["revision"] + 2]
    if (
        authority["grant_id"] != origin["grant_id"]
        or authority["restore_generation"] != origin["restore_generation"]
        or len(revisions) != authority["revision"] - origin["revision"]
        or len(revisions) < 2
        or authority != owner_authority.reference(revisions[-2]).as_dict()
        or revisions[-1]["record_sha256"] != value["core_stop_sha256"]
        or revisions[-1]["revision"] != authority["revision"] + 1
        or any(
            r["state"] != ("ACTIVE" if index % 2 == 0 else "STOPPED")
            or r["restore_generation"] != origin["restore_generation"]
            for index, r in enumerate(revisions)
        )
    ):
        raise ValueError("model renewal authority chain")
    model_carry.authority_chain(store, prior, revisions)
    if sorted(k for k, v in latest.items() if v["state"] == "UNKNOWN") != [
        item["request_id"] for item in value["unknowns"]
    ] or any(v["state"] == "RESERVED" for v in latest.values()):
        raise ValueError("model renewal liability inventory")
    previous = {item["request_id"]: item for item in prior["manifest"]["unknowns"]}
    retained = {item["request_id"]: item for item in value["unknowns"]}
    if any(retained.get(key) != item for key, item in previous.items()):
        raise ValueError("model renewal predecessor liability changed")
    fresh = retained.keys() - previous.keys()
    current = {key for key in fresh if retained[key]["authority"] == authority}
    if current:
        proof = model_carry._input_receipt({**row, "record_sha256": record_sha})
        if proof["request_ids"] != sorted(current):
            raise ValueError("model renewal input inventory")
    elif value["inputs"] != []:
        raise ValueError("model renewal input inventory")
    for item in value["unknowns"]:
        financial = latest[item["request_id"]]
        if (
            item["request_id"] not in closures
            or financial["kind"] != "settle"
            or paid_budget.sha(financial) != item["settlement_sha256"]
            or financial["minimum_micro_usd"] != item["minimum_micro_usd"]
        ):
            raise ValueError("model renewal original liability")
        if item["request_id"] in fresh:
            admission = store.one(
                "SELECT * FROM model_admissions WHERE request_id=?", (item["request_id"],)
            )
            if (
                not admission
                or item["authority"] not in [owner_authority.reference(r).as_dict()
                                             for r in revisions if r["state"] == "ACTIVE"]
                or item["contract_sha256"] != prior["successor_paid_contract_sha256"]
                or admission["window_id"] != prior["manifest"]["successor_window_id"]
                or any(item[k] != v for k, v in model_carry._tuple(admission).items())
                or item["logical_request_id"] != admission["logical_request_id"]
                or item["reserved_micro_usd"] != admission["reserved_micro_usd"]
                or not hash_value(item["tool_sha256"])
            ):
                raise ValueError("model renewal current admission changed")
    windows = {item["request_id"]: item["window_id"] for item in store.rows(
        "SELECT request_id,window_id FROM model_admissions"
    )}
    if latest.keys() - windows.keys():
        raise ValueError("model renewal admission missing")
    baseline = {
        key: data
        for key, data in latest.items()
        if windows[key] in value["budget_scope_window_ids"]
    }
    if (
        digest(baseline) != value["budget_baseline_sha256"]
        or row["concurrency_basis"] != model_carry.wire.MODE
        or type(row["concurrency"]) is not int
        or not 1 <= row["concurrency"] <= 10
        or any(
            not hash_value(row[key])
            for key in (
                "successor_policy_sha256",
                "successor_paid_contract_sha256",
                "successor_approval_sha256",
            )
        )
    ):
        raise ValueError("model renewal approval")
    carries[row["carry_sha256"]] = {**row, "record_sha256": record_sha}


def _closed_context_models(store, capture, models, retained_unknowns):
    primary = store.rows(
        "SELECT job_id,authority_json FROM jobs WHERE event_id=? AND kind='chat' LIMIT 2",
        (capture.event_id,),
    )
    if len(primary) != 1 or json.loads(primary[0]["authority_json"]) != capture.authority:
        return False
    # The native manifest already proves these requests closed and explicitly retained.
    # Only a different chat's use of history is exempt; the source's own attempt is not.
    return all(
        item["state"] == "UNKNOWN" and item["request_id"] in retained_unknowns
        and store.one(
            "SELECT 1 FROM model_usage m JOIN jobs j USING(job_id) WHERE m.request_id=? "
            "AND j.job_id<>? AND j.kind='chat' AND j.event_id<>?",
            (item["request_id"], primary[0]["job_id"], capture.event_id),
        )
        for item in models if item["state"] != "SETTLED"
    )


def check_policy(store, settings, old, new, now, *, retained_unknowns):
    """Only expiry and explicitly bounded private Life consent may change."""
    from companion import owner_input_state
    from companion.owner_authority_protocol import OwnerCapture

    # The owner policy follows the paid approval exactly; both may be open-ended.
    expiry = settings.paid_model.expires_at
    if new.expires_at != expiry or (
        expiry is not None and old.expires_at is not None and expiry <= old.expires_at
    ):
        raise CoreError("OWNER_MODEL_RENEWAL_EXPIRY_REQUIRED", 409)
    old_purposes, new_purposes = set(old.purposes), set(new.purposes)
    if not old_purposes <= new_purposes or not new_purposes - old_purposes <= {"life"}:
        raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
    scope = new.life_shadow
    if scope is None:
        if old.life_shadow is not None or new_purposes != old_purposes:
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
        return
    if (
        "proactive" not in old_purposes
        or scope.authorization_sha256 != new.authorization_sha256
        or (expiry is not None and scope.expires_at > expiry)
        or scope.expires_at <= datetime.fromisoformat(now)
        or scope.purposes != ["PRIVATE_MESSAGE_DRAFT"]
        or scope.automatic_actions != ["qq.private.proactive"]
        or scope.qzone_draft_sources
    ):
        raise CoreError("OWNER_MODEL_RENEWAL_LIFE_SCOPE_REQUIRED", 409)
    if old.life_shadow is not None:
        before, after = (s.model_dump(mode="json") for s in (old.life_shadow, scope))
        allowed = {"authorization_sha256", "expires_at"}
        if (old.life_shadow.source_scope == "selected"
            and scope.source_scope == "owner_private_history"):
            # The new reviewed approval may reuse the unchanged independent
            # owner-history scope; conversation, destinations and item caps stay exact.
            allowed.update({"source_scope", "source_event_ids"})
        if {k: v for k, v in before.items() if k not in allowed} != {
            k: v for k, v in after.items() if k not in allowed
        }:
            raise CoreError("OWNER_MODEL_RENEWAL_LIFE_SCOPE_REQUIRED", 409)
    for event_id in scope.source_event_ids:
        current = owner_authority._latest(store)
        row = store.one(
            "SELECT e.*,c.owner_actor_id,c.scope FROM events e "
            "JOIN conversations c USING(conversation_id) WHERE event_id=?",
            (event_id,),
        )
        capture = store.one(
            "SELECT value FROM runtime_meta WHERE key=?", ("owner_input:" + event_id,)
        )
        captured = json.loads(capture["value"])["capture"] if capture else None
        history = old.history
        if (
            not row
            or row["redacted"]
            or row["privacy_scope"] != "private"
            or row["scope"] != "private"
            or row["owner_actor_id"] != "owner"
            or row["actor_id"] != "owner"
            or row["source"] != "qq.onebot"
            or row["conversation_id"] != scope.conversation_id
            or captured is None
            or any(
                captured["authority"][key] != current[key]
                for key in ("grant_id", "restore_generation")
            )
            or row["occurred_at"] > now
            or row["ingested_at"] > now
            or history.scope != "owner_private"
            or history.not_before is None
            or datetime.fromisoformat(row["occurred_at"]) < history.not_before
            or (
                datetime.fromisoformat(now) - datetime.fromisoformat(row["ingested_at"])
            ).total_seconds()
            >= min(old.source_retention_days, settings.raw_event_retention_days) * 86400
        ):
            raise CoreError("OWNER_MODEL_RENEWAL_LIFE_SOURCE_REQUIRED", 409)
        capture = OwnerCapture.model_validate(captured)
        models = []
        state = owner_input_state.read(store, settings, capture, model_requests=models)
        if (
            state["unsettled_work"] or state["unsettled_delivery"]
            or state["unsettled_model"]
            and not _closed_context_models(store, capture, models, retained_unknowns)
        ):
            raise CoreError("OWNER_MODEL_RENEWAL_LIFE_SOURCE_REQUIRED", 409)
