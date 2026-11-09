"""Offline completion of the expired-claim UNKNOWN accounting projection."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

from pydantic import Field

from companion import model_carry, owner_authority, paid_budget
from companion.contracts import CoreError, StrictModel
from companion.model_carry_controller import VerifiedExecutors
from companion.store import encode
from companion_delivery_protocol import timestamp

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ACTION = "model.expired_settlement_reconciled"


class Request(StrictModel):
    version: Annotated[int, Field(strict=True, ge=1, le=1)]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    request_id: Annotated[str, Field(pattern=r"^model_[a-f0-9]{32}$")]
    journal_id: Annotated[str, Field(pattern=r"^budget_[a-f0-9]{32}$")]
    expected_head_sha256: Hash
    admission_sha256: Hash
    usage_sha256: Hash
    authority_sha256: Hash
    authorization_sha256: Hash
    executors: Annotated[list[dict], Field(min_length=2, max_length=100)]


def load(path, expected):
    raw = Path(path).read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected:
        raise CoreError("MODEL_EXPIRED_RECONCILIATION_INVALID", 400)
    return Request.model_validate_json(raw)


def reconcile(store, settings, request, executors, now, *, commit=False):
    """Keep usage/admission bytes; complete one exact native settlement, never a result.

    The CLI holds the original worker locks and the existing deployment controller
    proves every model executor, including the API, is permanently stopped.
    """
    if not isinstance(executors, VerifiedExecutors) or executors.value != request.executors:
        raise CoreError("OWNER_MODEL_CARRY_NOT_QUIESCENT", 409)
    try:
        with store.transaction(rollback=not commit):
            from companion.postgres import BUDGET_LEDGER, ERASURE_LEDGER

            store.ledger_locks(((BUDGET_LEDGER, False), (ERASURE_LEDGER, True)))
            admission = store.one("SELECT * FROM model_admissions WHERE request_id=%s",
                                  (request.request_id,))
            usage = store.one("SELECT * FROM model_usage WHERE request_id=%s",
                              (request.request_id,))
            head = owner_authority._latest(store)
            job = (store.one("SELECT * FROM jobs WHERE job_id=%s", (usage["job_id"],))
                   if usage else None)
            if (not model_carry.active(settings)
                or not admission or not usage or not head or not job
                or paid_budget.sha(admission) != request.admission_sha256
                or paid_budget.sha(usage) != request.usage_sha256
                or paid_budget.sha(owner_authority.reference(head).as_dict())
                != request.authority_sha256
                or head["restore_generation"] != owner_authority._meta(
                    store, owner_authority.GENERATION)
                or json.loads(admission["authority_json"])
                != owner_authority.reference(head).as_dict()
                or usage["state"] != "UNKNOWN" or usage["is_estimate"] != 1
                or usage["billed_micro_usd"] != admission["reserved_micro_usd"]
                or admission["journal_id"] != request.journal_id
                or paid_budget.sha(json.loads(admission["snapshot_json"]))
                != admission["contract_sha256"]
                or job["state"] != "FAILED" or job["kind"] not in {"chat", "activity"}
                or job["last_error_code"] != "WORKER_LEASE_EXPIRED"
                or not job["lease_until"] or timestamp(job["lease_until"]) > timestamp(now)
                or store.one("SELECT 1 FROM runtime_meta WHERE key IN "
                             "('model_budget_restore_required','cloud_budget_restore_required')")):
                raise ValueError("original expired claim or accounting evidence changed")
            path = Path(settings.paid_model.journal_path)
            raw = path.read_bytes()
            rows, _ = paid_budget._journal_records(raw)
            if not rows or rows[0].get("journal_id") != request.journal_id:
                raise ValueError("original independent journal required")
            request_sha = paid_budget.sha(request.model_dump(mode="json"))
            prior_audit = store.one("SELECT safe_detail_json FROM audit_log WHERE action=%s "
                                   "AND entity_id=%s", (ACTION, request.request_id))
            record = {"kind": "settle", "state": "UNKNOWN", "request_id": request.request_id,
                      "minimum_micro_usd": usage["billed_micro_usd"],
                      "contract_sha256": admission["contract_sha256"],
                      "io_started_at": admission["io_started_at"],
                      "evidence_sha256": request_sha}
            candidate = {**record, "previous": request.expected_head_sha256}
            candidate["sha256"] = paid_budget.sha(candidate)
            closed_at = max(request.executors, key=lambda item: timestamp(
                item["finished_at"]))["finished_at"]
            if timestamp(closed_at) > timestamp(now):
                raise ValueError("executor exit cannot be in the future")
            close = {"kind": model_carry.CLOSE, "version": 1, **model_carry._tuple(admission),
                     "settlement_sha256": candidate["sha256"], "closed_at": closed_at}
            closure = {**close, "previous": candidate["sha256"]}
            closure["sha256"] = paid_budget.sha(closure)
            cutoffs = [i for i, row in enumerate(rows)
                       if row["sha256"] == request.expected_head_sha256]
            if len(cutoffs) != 1:
                raise ValueError("original reviewed journal prefix changed")
            cutoff = cutoffs[0]
            tail = rows[cutoff + 1:]
            if tail not in ([], [candidate], [candidate, closure]):
                raise ValueError("unexpected journal suffix or conflicting recovery")
            completed = len(tail) == 2
            if prior_audit and (not completed or json.loads(
                prior_audit["safe_detail_json"]).get("request_sha256") != request_sha):
                raise ValueError("reconciliation operation conflict")
            prior = [r for r in rows[:cutoff + 1] if r.get("request_id") == request.request_id]
            original = prior[-1] if prior else None
            marker = store.one("SELECT value FROM runtime_meta WHERE key='model_budget_blocked'")
            if (not original or original.get("kind") not in {"reserve", "io_started"}
                or original.get("state") != "RESERVED"
                or original.get("minimum_micro_usd") != usage["billed_micro_usd"]
                or original.get("contract_sha256") != admission["contract_sha256"]
                or original.get("io_started_at") != admission["io_started_at"]
                or marker != {"value": "1"} and not (completed and marker is None)):
                raise ValueError("only the pinned RESERVED/UNKNOWN divergence is eligible")
            custody = SimpleNamespace(journal_path=path)
            proposed = raw + b"".join((encode(row) + "\n").encode()
                                       for row in [candidate, closure][len(tail):])
            # Run the unchanged complete validator over the proposed exact bytes.
            # No journal append, SQL projection or authorization result is mocked.
            view = paid_budget._journal(store, custody, allow_settlement=True,
                                         initialize=False, raw=proposed,
                                         model_control_replay=closure["sha256"])
            if view["blocked_request_ids"]:
                raise ValueError("unrelated independent accounting block")
            for frozen in store.rows("SELECT snapshot_json,contract_sha256 FROM model_admissions"):
                if (paid_budget.sha(json.loads(frozen["snapshot_json"]))
                    != frozen["contract_sha256"]):
                    raise ValueError("unrelated frozen accounting contract changed")
            result = {"state": "RECONCILED_OFFLINE" if commit else "PREVIEWED_OFFLINE",
                      "request_id": request.request_id, "request_sha256": request_sha,
                      "settlement_sha256": candidate["sha256"],
                      "execution_closure_sha256": closure["sha256"],
                      "retained_micro_usd": usage["billed_micro_usd"],
                      "usage_state": "UNKNOWN", "provider_calls": 0, "qq_sends": 0}
            if not commit:
                return result
            if not tail:
                assert path.read_bytes() == raw, "MODEL_JOURNAL_CHANGED_DURING_RECONCILIATION"
                paid_budget.journal(store, custody, record, allow_settlement=True, initialize=False)
            if not completed:
                paid_budget.journal(store, custody, close, allow_settlement=True, initialize=False)
            else:
                # Reuse native control projection recovery after fsync-before-commit.
                paid_budget.journal(store, custody, allow_settlement=True, initialize=False,
                                    model_control_replay=closure["sha256"])
                store.execute("INSERT INTO runtime_meta(key,value) VALUES(%s,%s) "
                              "ON CONFLICT(key) DO NOTHING",
                              ("model_control:" + closure["sha256"], closure["sha256"]))
            view = paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
            if view["head_sha256"] != closure["sha256"] or view["blocked_request_ids"]:
                raise ValueError("completed accounting custody changed")
            store.execute("DELETE FROM runtime_meta WHERE key='model_budget_blocked' AND value='1'")
            paid_budget.forget_views(store)
            paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
            if not prior_audit:
                store.audit(ACTION, request.request_id, request.operation_id, now,
                            request_sha256=request_sha, settlement_sha256=candidate["sha256"],
                            execution_closure_sha256=closure["sha256"],
                            authorization_sha256=request.authorization_sha256,
                            retired_marker="model_budget_blocked=1",
                            retained_micro_usd=usage["billed_micro_usd"])
            return result
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise CoreError("MODEL_EXPIRED_RECONCILIATION_REJECTED", 409) from error
