"""REM metadata over frozen native rows; fiction never becomes factual evidence."""

import json

from companion.cognition_read import STATES, measurement, symbol
from companion.contracts import CoreError

ERRORS = frozenset("REM_SOURCE_ERASED REM_INTERRUPTED REM_JOB_FENCED REM_POLICY_CHANGED "
                   "REM_ALREADY_ATTEMPTED REM_INPUT_LIMIT REM_INCOMPLETE_RESPONSE "
                   "REM_SENSITIVE_CONTENT REM_INVALID_RESULT BACKGROUND_YIELD_TO_CHAT "
                   "MODEL_USAGE_UNRESOLVED SCENARIO_BUDGET_DISABLED SCENARIO_CALL_LIMIT "
                   "DAILY_BUDGET_EXHAUSTED SCENARIO_MONTHLY_BUDGET_EXHAUSTED PRIVACY_CHANGED "
                   "OWNER_AUTHORITY_DISABLED OWNER_AUTHORITY_EXPIRED LIFE_DRAFT_CHANGED "
                   "LIFE_DRAFT_EXPIRED LIFE_SETTLED_DRAFT_REQUIRED LIFE_CONSUMPTION_PAUSED "
                   "LIFE_RECONCILIATION_REQUIRED".split())


def _ids(value, limit):
    result = json.loads(value)
    if (not isinstance(result, list) or len(result) > limit
        or any(not isinstance(key, str) or not 1 <= len(key) <= 160 for key in result)
        or len(set(result)) != len(result)):
        raise ValueError("invalid bounded native references")
    return result


def record(core, row, now):
    """Reuse REM's native MemoryContext proof and original job/scenario usage.

    The caller owns the request-wide privacy-version fence and owner HTTP auth.
    A missing/expired source hides private references, retaining accounting.
    This reader never invokes REM scheduling, execution, expiration or erasure.
    """
    job = core.store.one('SELECT state, last_error_code FROM jobs WHERE job_id = %s AND kind = '
        "'rem'",
                         (row["job_id"],))
    if job is None:
        return None
    available = not row["redacted"] and row["expires_at"] > now
    mids, eids, output = [], [], {}
    if available:
        try:
            mids = _ids(row["source_memory_ids_json"], 3)
            eids = _ids(row["source_event_ids_json"], 100)
            snapshot = json.loads(row["source_snapshot_json"])
            if "life" in snapshot:
                from companion import rem

                if mids or not eids:
                    raise ValueError("Life creation retains event sources, not new facts")
                rem.life_record(core.store, core.settings, lambda: now, row, snapshot)
            else:
                proof = snapshot["proof"]
                if (not mids or not eids or len(proof["rows"]) > 3
                    or set(mids) != {memory["memory_id"] for memory in proof["rows"]}
                    or set(eids) != set(proof["source_event_ids"])):
                    raise ValueError("source identities differ from original proof")
                core.memory_context.revalidate(proof, now)
            output = json.loads(row["output_json"])
            if not isinstance(output, dict):
                raise ValueError("invalid output object")
        except (CoreError, ValueError, KeyError, TypeError):
            available = False
    usage = core.store.one(
        'SELECT request_id, state, input_tokens, output_tokens, cached_input_tokens, '
        "billed_micro_usd FROM model_usage WHERE job_id = %s AND scenario = 'rem' ORDER BY "
        'record_sequence DESC NULLS LAST LIMIT 1',
        (row["job_id"],),
    )
    admission = core.store.one(
        'SELECT measurement_json FROM model_admissions WHERE request_id = '
        '%s', (usage["request_id"],),
    ) if usage else None
    questions = output.get("questions") if available else None
    count = len(questions) if isinstance(questions, list) and len(questions) <= 3 else None
    return {"id": row["rem_id"], "job_id": row["job_id"], "kind": "FICTION",
            "audience": "owner_only", "content_kind": "private_fiction",
            "state": symbol(job["state"], STATES), "source_available": available,
            "last_error_code": symbol(job["last_error_code"], ERRORS),
            "local_date": row["local_date"], "timezone": row["timezone"],
            "expires_at": row["expires_at"], "redacted": bool(row["redacted"]),
            "source_memory_ids": mids if available else [],
            "source_event_ids": eids if available else [],
            "decision": symbol(output.get("decision"), {"SKIP", "DREAM"})
            if available else "NOT_AVAILABLE",
            "questions": {"state": "UNVERIFIED", "count": count},
            "usage": usage, "usage_status": "RECORDED" if usage else "NOT_RECORDED",
            "measurement": measurement(admission and admission["measurement_json"])}
