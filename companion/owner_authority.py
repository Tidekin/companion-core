"""Owner revisions over native Core custody; permission never substitutes for a lease."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from companion.contracts import CoreError
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode, uid
from companion_delivery_protocol.authority import AuthorityRef

CURRENT = "owner_authority_current"
MODE = "owner_authority_mode"
GENERATION = "owner_authority_restore_generation"
RECORD_KIND = "authority_revision"
RECORD_FIELDS = {"kind", "version", "grant_id", "revision", "operation_id", "request_sha256",
                 "state", "policy_sha256", "restore_generation", "predecessor_sha256", "at",
                 "previous"}


def _sha(value):
    from companion.paid_budget import sha

    return sha(value)


def _meta(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    return row["value"] if row else None


def _put(store, key, value):
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (key, value))


def _latest(store):
    pointer = _meta(store, CURRENT)
    return store.one(
        "SELECT * FROM owner_authority_revisions WHERE record_sha256=?", (pointer,)
    ) if pointer else None


def required(store, settings):
    """Live execution and persisted authority custody cannot fall back to mock admission."""
    return bool(settings.mode == "live" or settings.owner_runtime.mode != "OFF"
                or _meta(store, CURRENT))


def reference(row):
    return AuthorityRef(version=2, **{k: row[k] for k in (
        "grant_id", "revision", "policy_sha256", "restore_generation",
    )})


@dataclass(frozen=True)
class OwnerAuthorityView:
    authority: AuthorityRef
    policy: OwnerGrantPolicy
    journal_id: str
    journal_path: str


def check_response(view, purpose):
    """Serialize an already checked current view; this does not grant permission."""
    return {"approved": True, "authority": view.authority.as_dict(), "purpose": purpose,
            "capability_policies": dict(view.policy.capability_policies),
            "budget_journal_id": view.journal_id,
            "capture": view.policy.capture.model_dump(mode="json") if view.policy.capture else None,
            "expires_at": view.policy.expires_at.isoformat() if view.policy.expires_at else None}


def fold(records, row, digest):
    """Fold only versioned native journal evidence; a DB snapshot cannot invent it."""
    if set(row) != RECORD_FIELDS or row["kind"] != RECORD_KIND or row["version"] != 2:
        raise ValueError("invalid authority record")
    AuthorityRef.from_dict({"version": 2, **{k: row[k] for k in (
        "grant_id", "revision", "policy_sha256", "restore_generation",
    )}})
    prior = records[-1] if records else None
    if (row["state"] not in {"ACTIVE", "STOPPED"}
        or row["predecessor_sha256"] != (prior["record_sha256"] if prior else None)
        or row["revision"] != (prior["revision"] + 1 if prior else 1)
        or prior and row["grant_id"] != prior["grant_id"]
        or any(r["operation_id"] == row["operation_id"] for r in records)
        or datetime.fromisoformat(row["at"]).tzinfo is None
        or prior and datetime.fromisoformat(row["at"]) < datetime.fromisoformat(prior["at"])):
        raise ValueError("authority revision chain")
    records.append({**row, "record_sha256": digest})


def validate_projection(store, records, journal_id, journal_path):
    exists = store.one("SELECT 1 FROM sqlite_master WHERE name='owner_authority_revisions'")
    rows = store.rows("SELECT * FROM owner_authority_revisions ORDER BY revision") if exists else []
    if len(rows) != len(records):
        raise ValueError("authority DB behind journal")
    for saved, record in zip(rows, records, strict=True):
        if any(saved[key] != record[key] for key in (
            "grant_id", "revision", "operation_id", "request_sha256", "state", "policy_sha256",
            "restore_generation", "predecessor_sha256", "record_sha256",
        )) or saved["created_at"] != record["at"] or (
            saved["journal_id"] != journal_id
            or Path(saved["journal_path"]).resolve() != Path(journal_path).resolve()
        ):
            raise ValueError("authority projection mismatch")
        if not saved["policy_redacted"] and _sha(json.loads(saved["policy_json"])) != (
            saved["policy_sha256"]
        ):
            raise ValueError("authority policy mismatch")
        if saved["policy_redacted"] and saved["policy_json"] != "{}":
            raise ValueError("invalid erased authority")
    expected = records[-1]["record_sha256"] if records else None
    if _meta(store, CURRENT) != expected:
        raise ValueError("authority pointer behind journal")


def custody(store, settings, *, settlement=False):
    from companion import paid_budget

    row = _latest(store)
    approval = settings.paid_model
    if row:
        approval = SimpleNamespace(journal_path=row["journal_path"])
    if approval is None:
        raise CoreError("OWNER_AUTHORITY_BUDGET_REQUIRED", 503)
    return paid_budget.journal(store, approval, allow_settlement=settlement)


def current(store, settings, clock, *, purpose=None, authority=None):
    """Current policy plus original journal custody; never returns a synthetic window."""
    from companion.live_text import _ledger_custody

    if settings.owner_runtime.mode != "ENABLED" or _meta(store, MODE) != "ENABLED":
        raise CoreError("OWNER_AUTHORITY_DISABLED", 403)
    row = _latest(store)
    if (not row or row["state"] != "ACTIVE" or row["policy_redacted"]
        or row["restore_generation"] != _meta(store, GENERATION)):
        raise CoreError("OWNER_AUTHORITY_REQUIRED", 403)
    ref = reference(row)
    if authority is not None:
        try:
            supplied = AuthorityRef.from_dict(authority)
        except (ValueError, TypeError):
            raise CoreError("OWNER_AUTHORITY_REFERENCE_INVALID", 403) from None
        if supplied != ref:
            raise CoreError("OWNER_AUTHORITY_REVISION_CHANGED", 409)
    policy = OwnerGrantPolicy.model_validate_json(row["policy_json"])
    if problem := policy_problem(settings, policy):
        raise problem
    if purpose is not None and purpose not in policy.purposes:
        raise CoreError("OWNER_AUTHORITY_PURPOSE_REQUIRED", 403)
    custody(store, settings)
    _ledger_custody(store, settings)
    if policy.expires_at is not None and datetime.fromisoformat(clock()) >= policy.expires_at:
        raise CoreError("OWNER_AUTHORITY_EXPIRED", 403)
    return OwnerAuthorityView(ref, policy, row["journal_id"], row["journal_path"])


def evidence(store, settings, authority):
    """Original policy for proof verification only; never grants execution permission."""
    try:
        ref = AuthorityRef.from_dict(authority)
    except (ValueError, TypeError):
        raise CoreError("OWNER_AUTHORITY_REFERENCE_INVALID", 400) from None
    row = store.one("SELECT * FROM owner_authority_revisions WHERE grant_id=? AND revision=?",
                    (ref.grant_id, ref.revision))
    if (not row or reference(row) != ref or row["state"] != "ACTIVE"
        or row["policy_redacted"]):
        raise CoreError("OWNER_AUTHORITY_EVIDENCE_REQUIRED", 403)
    custody(store, settings, settlement=True)
    return OwnerAuthorityView(ref, OwnerGrantPolicy.model_validate_json(row["policy_json"]),
                              row["journal_id"], row["journal_path"])


def bind_job(store, settings, clock, job_id, *, purpose):
    """Pin a native job once, in its admission transaction; revisions never rebind it."""
    if not store.db.in_transaction:
        raise RuntimeError("authority binding requires the job admission transaction")
    view = current(store, settings, clock, purpose=purpose)
    row = store.one("SELECT authority_json,state FROM jobs WHERE job_id=?", (job_id,))
    if not row or row["state"] != "QUEUED":
        raise CoreError("OWNER_AUTHORITY_JOB_ADMISSION_REQUIRED", 409)
    saved = json.loads(row["authority_json"])
    if saved and saved != view.authority.as_dict():
        raise CoreError("OWNER_AUTHORITY_REVISION_CHANGED", 409)
    store.execute("UPDATE jobs SET authority_json=? WHERE job_id=?",
                  (encode(view.authority.as_dict()), job_id))
    return view


def check_job(store, settings, clock, job, *, purpose, executing=False):
    """Re-read native state and permission; a durable grant is never a native lease."""
    row = store.one("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],))
    try:
        ref = AuthorityRef.from_dict(json.loads(row["authority_json"])) if row else None
    except (ValueError, TypeError):
        ref = None
    if ref is None:
        raise CoreError("OWNER_AUTHORITY_JOB_BINDING_REQUIRED", 403)
    view = current(store, settings, clock, purpose=purpose, authority=ref)
    if executing and (
        row["state"] != "RUNNING" or not row["lease_token"]
        or row["lease_token"] != job["lease_token"]
        or not row["lease_until"]
        or datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(clock())
    ):
        raise CoreError("JOB_LEASE_LOST", 409)
    return view


def model_purpose(store, scenario, job_id):
    from companion import notices

    if scenario == "activity":
        job = store.one("SELECT j.*,e.body_json FROM jobs j JOIN events e USING(event_id) "
                        "WHERE job_id=?", (job_id,)) if job_id else None
        return "life" if notices.shadow(job) else (
            "goals" if notices.required(store, job_id) else "proactive")
    purpose = {"chat": "chat", "memory_extract": "memory", "memory_review": "memory",
               "diary": "diary", "reflection": "reflection", "qq_poke": "poke",
               "social_autonomy": "social", "content_vision": "image",
               "content_video": "content", "content_video_preview": "bili_video",
               "meme_caption": "meme", "evaluation": "evaluation", "rem": "rem",
               "watch_review": "exploration", "style_distill": "expression",
               "jargon": "memory"}.get(scenario)
    if purpose is None:
        raise CoreError("OWNER_AUTHORITY_MODEL_PURPOSE_REQUIRED", 403)
    return purpose


def owner_waiting(store, settings, now, *, authority=None):
    """Native accepted captures preempt background work before event ingestion too."""
    from companion.budget import chat_pending

    if chat_pending(store, now):
        return True
    ref = authority or current(store, settings, lambda: now).authority.as_dict()
    return bool(store.one(
        "SELECT 1 FROM runtime_meta r LEFT JOIN events e "
        "ON e.event_id=json_extract(r.value,'$.event_id') "
        "WHERE r.key LIKE 'owner_input:%' AND e.event_id IS NULL "
        "AND json_extract(r.value,'$.authority')=? "
        "AND NOT EXISTS(SELECT 1 FROM runtime_meta erased "
        "WHERE erased.key='erased_event:' || json_extract(r.value,'$.event_id')) LIMIT 1",
        (encode(ref),),
    ))


def admit_model(store, settings, clock, job_id, scenario, *, yield_to_chat=True):
    """Check the model's independent purpose and its native task before budget admission.

    Background work yields to a waiting owner at admission and at the HTTP boundary,
    where the model client can prove the request was never sent. The intermediate
    recheck (`yield_to_chat=False`) has no such proof; yielding there left calls UNKNOWN.
    """
    from companion import owner_input, reply_revision
    from companion.budget import is_background

    purpose = model_purpose(store, scenario, job_id)
    view = current(store, settings, clock, purpose=purpose)
    background = is_background(scenario, job_id)
    if background and yield_to_chat and owner_waiting(
            store, settings, clock(), authority=view.authority.as_dict()):
        raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
    if job_id is not None:
        job = store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if job is None:
            raise CoreError("OWNER_AUTHORITY_JOB_BINDING_REQUIRED", 403)
        check_job(store, settings, clock, job, purpose=purpose,
                  executing=scenario not in {"content_vision", "content_video",
                                             "content_video_preview"})
        reply_revision.check_job(store, job)
        reply_revision.check_sources(store, job_id)
        if job["kind"] == "chat":
            owner_input.source(store, settings, view, job["event_id"], clock(),
                               allow_commands=True)
        allowed = {"chat": {"chat", "memory_review", "content_vision", "content_video",
                             "content_video_preview"},
                   "activity": {"activity", "memory_review"}, "poke": {"qq_poke"},
                   "meme_caption": {"meme_caption"}, "social_plan": {"social_autonomy"},
                   "evaluation": {"evaluation"}, "rem": {"rem"}}
        if scenario not in allowed.get(job["kind"], set()):
            raise CoreError("COMPANION_MODEL_SCOPE_REQUIRED", 403)
    elif scenario not in {"memory_extract", "memory_review", "diary", "reflection",
                          "social_autonomy", "watch_review", "style_distill", "jargon"}:
        raise CoreError("COMPANION_BACKGROUND_SCOPE_REQUIRED", 403)
    return view


def check_model(store, settings, clock, request_id, *, yield_to_chat=True):
    """Recheck a reserved admission; historical references never renew an execution lease."""
    row = store.one("SELECT a.*,m.job_id,m.scenario,m.state FROM model_admissions a "
                    "JOIN model_usage m USING(request_id) WHERE request_id=?", (request_id,))
    if not row or row["state"] != "RESERVED":
        raise CoreError("OWNER_AUTHORITY_MODEL_ADMISSION_REQUIRED", 403)
    view = current(store, settings, clock, purpose=model_purpose(
        store, row["scenario"], row["job_id"]), authority=json.loads(row["authority_json"]))
    admit_model(store, settings, clock, row["job_id"], row["scenario"],
                yield_to_chat=yield_to_chat)
    deadlines = [datetime.fromisoformat(row["lease_until"])]
    if view.policy.expires_at:
        deadlines.append(view.policy.expires_at)
    if row["job_id"] and row["scenario"] not in {
        "content_vision", "content_video", "content_video_preview",
    }:
        job = store.one("SELECT lease_until FROM jobs WHERE job_id=?", (row["job_id"],))
        deadlines.append(datetime.fromisoformat(job["lease_until"]))
    deadline = min(deadlines)
    if deadline <= datetime.fromisoformat(clock()):
        raise CoreError("JOB_LEASE_LOST", 409)
    return {"lease_until": deadline.isoformat()}


def preview(store, settings, clock, request):
    from companion import paid_budget

    if settings.owner_runtime.mode != "ENABLED":
        raise CoreError("OWNER_AUTHORITY_DISABLED", 403)
    policy = request.policy
    _check_policy(settings, policy)
    if len(encode(policy.model_dump(mode="json")).encode("utf-8")) > 65536:
        raise CoreError("OWNER_AUTHORITY_POLICY_TOO_LARGE", 400)
    if policy.owner_qq_id != settings.owner_qq_id:
        raise CoreError("OWNER_AUTHORITY_IDENTITY_CHANGED", 403)
    paid_budget.contract(settings, clock())
    if policy.paid_contract_sha256 != _sha(paid_budget.snapshot(settings)):
        raise CoreError("OWNER_AUTHORITY_PAID_POLICY_CHANGED", 409)
    if policy.expires_at is not None and policy.expires_at <= datetime.fromisoformat(clock()):
        raise CoreError("OWNER_AUTHORITY_EXPIRED", 403)
    previous = _latest(store)
    if request.expected_revision != (previous["revision"] if previous else 0):
        raise CoreError("OWNER_AUTHORITY_REVISION_CHANGED", 409)
    # Permission-only revisions use the same paid/carry approval and must prove
    # stopped custody at preview as well as in the activation transaction.
    if policy.unknown_carry_sha256 is not None:
        from companion import model_carry

        view = custody(store, settings, settlement=True)
        saved = view["model_carries"].get(policy.unknown_carry_sha256)
        if saved and saved["successor_policy_sha256"] != _sha(policy.model_dump(mode="json")):
            model_carry.activation(store, settings, policy)
    value = {"policy": policy.model_dump(mode="json"),
             "expected_revision": request.expected_revision,
             "predecessor_sha256": previous["record_sha256"] if previous else None,
             "restore_generation": _meta(store, GENERATION)}
    return {"preview_sha256": _sha(value), **value}


def _operation(store, operation_id, request_hash):
    row = store.one("SELECT * FROM owner_authority_revisions WHERE operation_id=?", (operation_id,))
    if row and row["request_sha256"] != request_hash:
        raise CoreError("OWNER_AUTHORITY_OPERATION_CONFLICT", 409)
    return row


def _reply(row):
    return {"authority": reference(row).as_dict(), "state": row["state"],
            "record_sha256": row["record_sha256"], "admission_fenced": row["state"] != "ACTIVE",
            "quiescence_proven": False}


def capability_hashes(settings):
    """Reuse each domain's concrete policy fingerprint, never a programmable rule engine."""
    from companion.meme_service import policy_hash as meme_policy_hash

    paid = settings.paid_model
    return {
        "image": _sha(paid.image_input.model_dump(mode="json"))
        if paid and paid.image_input else None,
        "meme": meme_policy_hash(settings),
        "file": _sha(settings.private_files.model_dump(mode="json"))
        if settings.private_files.enabled else None,
        "poke": _sha(settings.poke_policy.model_dump(mode="json"))
        if settings.poke_policy.enabled else None,
        "voice": settings.voice.authority_sha256() if settings.voice.enabled else None,
        "account_actions": settings.social_autonomy.authorization_sha256
        if settings.social_autonomy.enabled else None,
        "qzone_publication": _sha(settings.social_autonomy.model_dump(mode="json"))
        if settings.social_autonomy.enabled and settings.social_autonomy.qzone_enabled
        and settings.social_autonomy.qzone_public_enabled else None,
        "bili_video": _sha(settings.bili_video.model_dump(mode="json"))
        if settings.bili_video.enabled else None,
        "rem": _sha(settings.rem.model_dump(mode="json")) if settings.rem.enabled else None,
    }


def policy_problem(settings, policy):
    """The error `current()` would raise for this policy under this code and config, or None.

    Release planning uses it to tell whether new code keeps the stored policy valid.
    """
    from companion import paid_budget

    try:
        _check_policy(settings, policy)
        if settings.paid_model is None or policy.paid_contract_sha256 != _sha(
            paid_budget.snapshot(settings)
        ):
            raise CoreError("OWNER_AUTHORITY_PAID_POLICY_CHANGED", 409)
        if policy.owner_qq_id != settings.owner_qq_id:
            raise CoreError("OWNER_AUTHORITY_IDENTITY_CHANGED", 403)
    except CoreError as error:
        return error
    return None


def stored_policy_problem(store, settings):
    """Configuration compatibility for display; this never authorizes execution."""
    row = _latest(store)
    if row is None or row["policy_redacted"]:
        return None
    try:
        policy = OwnerGrantPolicy.model_validate_json(row["policy_json"])
    except ValueError:
        return CoreError("OWNER_AUTHORITY_POLICY_INVALID", 409)
    return policy_problem(settings, policy)


def _check_policy(settings, policy):
    if policy.capture and policy.capture.identity_sha256 != _sha(
        [settings.owner_qq_id, settings.bot_qq_id]
    ):
        raise CoreError("OWNER_AUTHORITY_CAPTURE_IDENTITY_CHANGED", 403)
    hashes = capability_hashes(settings)
    if any(hashes[key] != value for key, value in policy.capability_policies.items()):
        raise CoreError("OWNER_AUTHORITY_CAPABILITY_CHANGED", 403)
    if policy.source_retention_days > settings.raw_event_retention_days:
        raise CoreError("OWNER_AUTHORITY_RETENTION_SCOPE_REQUIRED", 403)


def erase_sources(store, event_ids):
    """Existing erasure transaction scrubs referenced policies without rewriting finance."""
    if not event_ids or not store.one(
        "SELECT 1 FROM sqlite_master WHERE name='owner_authority_revisions'"
    ):
        return
    for row in store.rows("SELECT * FROM owner_authority_revisions WHERE policy_redacted=0"):
        policy = json.loads(row["policy_json"])
        shadow = policy.get("life_shadow") or {}
        if set(shadow.get("source_event_ids", ())) & set(event_ids):
            store.execute("UPDATE owner_authority_revisions SET policy_json='{}',policy_redacted=1 "
                          "WHERE grant_id=? AND revision=?", (row["grant_id"], row["revision"]))
            if _meta(store, CURRENT) == row["record_sha256"]:
                _put(store, MODE, "OFF")


def _append(store, settings, clock, operation, request_hash, policy, state, previous):
    from companion import paid_budget

    authority = {"grant_id": previous["grant_id"] if previous else uid("grant"),
                 "revision": previous["revision"] + 1 if previous else 1,
                 "policy_sha256": _sha(policy), "restore_generation": _meta(store, GENERATION)}
    approval = (SimpleNamespace(journal_path=previous["journal_path"]) if previous
                else settings.paid_model)
    record = {"kind": RECORD_KIND, "version": 2, **authority,
              "operation_id": operation, "request_sha256": request_hash,
              "state": state, "predecessor_sha256": previous["record_sha256"] if previous else None,
              "at": clock()}
    result = paid_budget.journal(store, approval, record, allow_settlement=state == "STOPPED")
    store.insert("owner_authority_revisions", **authority, operation_id=operation,
                 request_sha256=request_hash, state=state, policy_json=encode(policy),
                 predecessor_sha256=record["predecessor_sha256"],
                 record_sha256=result["head_sha256"],
                 journal_path=str(Path(approval.journal_path).resolve()),
                 journal_id=_meta(store, "model_budget_journal_id"), created_at=record["at"])
    _put(store, CURRENT, result["head_sha256"])
    _put(store, MODE, "ENABLED" if state == "ACTIVE" else "OFF")
    store.audit("owner_authority." + state.lower(), authority["grant_id"], operation,
                record["at"], revision=authority["revision"],
                policy_sha256=authority["policy_sha256"])
    return _latest(store)


def activate(store, settings, clock, request):
    from companion import cumulative, live_text

    with store.transaction():
        request_hash = _sha(request.model_dump(mode="json"))
        if existing := _operation(store, request.operation_id, request_hash):
            custody(store, settings, settlement=True)
            return _reply(existing)
        expected = preview(store, settings, clock, request)
        if request.preview_sha256 != expected["preview_sha256"]:
            raise CoreError("OWNER_AUTHORITY_PREVIEW_CHANGED", 409)
        if store.one("SELECT 1 FROM runtime_meta WHERE key IN "
                     "('cloud_budget_restore_required','model_budget_restore_required')"):
            raise CoreError("OWNER_AUTHORITY_RESTORE_RECONCILIATION_REQUIRED", 503)
        # Preserve the native quiescence inventory: neither UNKNOWN nor a pending send is idle.
        from companion import model_carry

        if model_carry.active(settings) and request.policy.unknown_carry_sha256 is not None:
            model_carry.activation(store, settings, request.policy)
        else:
            if request.policy.unknown_carry_sha256 is not None:
                raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
            if custody(store, settings, settlement=True)["model_carries"]:
                raise CoreError("OWNER_MODEL_CARRY_REQUIRED", 403)
            live_text._idle(store)
        old = live_text._meta(store, live_text.BINDING)
        if old and not live_text.stopped(store, old["plan_sha256"]):
            raise CoreError("OWNER_AUTHORITY_LEGACY_STOP_REQUIRED", 409)
        if settings.paid_model.cumulative or _meta(store, cumulative.PROJECTION):
            # The existing quota approval pins its pre-append journal cutoff.
            # Accept it before appending the authority revision, as native open did.
            cumulative.activate(store, settings, clock())
            cumulative.check(store, settings, clock())
        previous = _latest(store)
        row = _append(store, settings, clock, request.operation_id, request_hash,
                      request.policy.model_dump(mode="json"), "ACTIVE", previous)
        return _reply(row)


def stop(store, settings, clock, request):
    from companion import private_life

    with store.transaction():
        request_hash = _sha(request.model_dump(mode="json"))
        if existing := _operation(store, request.operation_id, request_hash):
            custody(store, settings, settlement=True)
            return _reply(existing)
        previous = _latest(store)
        if previous is None or reference(previous) != AuthorityRef.from_dict(request.authority):
            raise CoreError("OWNER_AUTHORITY_REVISION_CHANGED", 409)
        row = _append(store, settings, clock, request.operation_id, request_hash,
                      json.loads(previous["policy_json"]), "STOPPED", previous)
        private_life.stop_work(store, clock())
        return _reply(row)


def restore(store):
    """Called only by controlled backup restore, after newest erasure replay."""
    _put(store, GENERATION, uid("restore"))
    _put(store, MODE, "OFF")


def status(store, settings, clock):
    try:
        view = current(store, settings, clock)
        return {"state": "ACTIVE", "authority": view.authority.as_dict()}
    except CoreError as error:
        return {"state": "OFF", "code": error.code}
