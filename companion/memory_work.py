"""Native memory extraction/review with one durable shared checkpoint.

Core's batch entry and Nightly's single-admission entry share these operations.
The original runtime_meta owns pending work; a nightly run records only native
references in steps_json. Both use the original leases, facts and erasure closure.
"""

import json
from contextlib import contextmanager, nullcontext
from datetime import date, datetime

from companion import (
    budget,
    live_text,
    memory_enrichment,
    memory_recovery,
    memory_references,
    mood_appraisals,
    owner_authority,
    private_life,
    reply_revision,
)
from companion.contracts import Candidate, CoreError, EpisodeSummary, Extraction, MemoryReview
from companion.diary import day_bounds
from companion.memory_authority import source_allowed
from companion.model import EXTRACT_RULES, REVIEW_RULES
from companion.privacy import epoch
from companion.store import after, encode, uid
from companion.worker_schedule import UnitResult

CHECKPOINT_KEY = "memory_extraction_checkpoint"
TERMINAL_PHASES = {"COMPLETE", "SKIPPED", "FAILED", "UNKNOWN", "CANCELLED"}


class MemoryReviewError(CoreError):
    """Settled review feedback, consumed only by the original bounded checkpoint."""

    def __init__(self, reason, issues=(), *, candidates=None):
        self.feedback = {"reason": reason, "issues": list(issues)}
        if candidates is not None:
            self.feedback["candidates"] = [item.model_dump(mode="json") for item in candidates]
        self.reextract = bool(issues)
        super().__init__("MEMORY_REVIEW_DISAGREEMENT" if issues else "MEMORY_REVIEW_INVALID", 502)


def _repair_matches(original, candidate):
    quote = original["evidence_quote"]
    return (
        original["evidence_event_id"] == candidate.evidence_event_id
        and original.get("evidence_reference_id") == candidate.evidence_reference_id
        and original["subject"] == candidate.subject
        and original["kind"] == candidate.kind
        and (quote in candidate.evidence_quote or candidate.evidence_quote in quote)
    )


def _repair_positions(originals, values):
    """Identify one distinct current candidate for each retained attribute."""
    remaining = {i: Candidate.model_validate(value) for i, value in enumerate(values)}
    required = set()
    for original in originals:
        related = [i for i, item in remaining.items() if _repair_matches(original, item)]
        matches = [i for i in related if original["content"] == remaining[i].content]
        if not matches:
            matches = [
                i
                for i in related
                if original["fact_key"].casefold() == remaining[i].fact_key.casefold()
            ]
        if not matches:
            # A paraphrase and a renamed key are fine with a unique source anchor.
            matches = [
                i
                for i in related
                if sum(_repair_matches(other, remaining[i]) for other in originals) == 1
            ]
        if len(matches) != 1:
            return None
        required.add(matches[0])
        del remaining[matches[0]]
    return required


def checkpoint_state(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key = %s", (CHECKPOINT_KEY,))
    return json.loads(row["value"]) if row else None


def erase_memory_work(store, event_ids):
    """Registered native erasure/replay cleanup; never touches accounting or leases."""
    rows = store.rows(
        ("SELECT key,value FROM runtime_meta WHERE key=%s OR starts_with(key,%s)"),
        (CHECKPOINT_KEY, memory_recovery.PREFIX),
    )
    for row in rows:
        try:
            record = json.loads(row["value"])
            ids = set(record.get("source_event_ids", [])) | set(
                record["batch"].get("event_ids", [])
            )
            affected = bool(ids & set(event_ids))
        except (ValueError, TypeError, KeyError, AttributeError):
            affected = True  # An uninterpretable payload cannot prove independence.
        if affected:
            store.execute("DELETE FROM runtime_meta WHERE key = %s", (row["key"],))


def short_refs(data):
    """Replace long event/reference IDs in the extractor input with e0/r0, and repeated
    episode groups with g0. Returns (payload, mapping); the model answers in short refs
    and from_refs() restores the real IDs before any validation."""
    refs, groups, mapping = {}, {}, {}
    for index, item in enumerate(data):
        refs[item["event_id"]] = f"e{index}"
        mapping[f"e{index}"] = item["event_id"]
    events = []
    for item in data:
        group = tuple(item["episode_source_event_ids"])
        key = None
        if group:
            if group not in groups:
                groups[group] = f"g{len(groups)}"
                mapping[groups[group]] = list(group)
            key = groups[group]
        references = []
        for reference in item["references"]:
            ref = f"r{sum(name.startswith('r') for name in mapping)}"
            mapping[ref] = reference["reference_id"]
            references.append(
                {"ref": ref, **{k: v for k, v in reference.items() if k != "reference_id"}}
            )
        events.append(
            {
                "ref": refs[item["event_id"]],
                "occurred_at": item["occurred_at"],
                "group": key,
                "text": item["text"],
                "references": references,
            }
        )
    payload = {
        "groups": {ref: [refs.get(e, e) for e in group] for group, ref in groups.items()},
        "events": events,
    }
    return payload, mapping


def from_refs(value, mapping):
    """Real IDs for the short refs an extraction used; unknown values stay unchanged
    and fail the usual evidence checks."""

    def one(ref):
        target = mapping.get(ref, ref)
        return target if isinstance(target, str) else ref

    for item in value.get("memories", []) if isinstance(value, dict) else []:
        if isinstance(item, dict):
            for key in ("evidence_event_id", "evidence_reference_id"):
                if isinstance(item.get(key), str):
                    item[key] = one(item[key])
    for item in value.get("episode_summaries", []) if isinstance(value, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("source_event_ids"), list):
            ids = []
            for ref in item["source_event_ids"]:
                target = mapping.get(ref, ref) if isinstance(ref, str) else ref
                ids += target if isinstance(target, list) else [target]
            item["source_event_ids"] = ids
    for item in value.get("interaction_signals", []) if isinstance(value, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("event_id"), str):
            item["event_id"] = one(item["event_id"])
    return value


def repair_json(text):
    """Close brackets the model left open (e.g. a source list missing its ']').

    Only bracket structure outside strings is touched; nothing is invented, and the
    result still passes the strict schema and every evidence check. None when the
    text is not repairable this way."""
    stack, out, in_string, escaped = [], [], False, False
    pairs = {"{": "}", "[": "]"}
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}]":
            if char not in stack:
                return None
            while stack and stack[-1] != char:
                out.append(stack.pop())
            stack.pop()
        out.append(char)
    if in_string:
        return None
    out.extend(reversed(stack))
    try:
        return json.loads("".join(out))
    except ValueError:
        return None


def parse_extraction(text, mapping):
    """(Extraction, repaired) from model text in short refs."""
    try:
        value, repaired = json.loads(text), False
    except ValueError:
        value, repaired = repair_json(text), True
        if value is None:
            raise
    return Extraction.model_validate(from_refs(value, mapping)), repaired


def _settled(store, request_id):
    usage = store.one("SELECT state FROM model_usage WHERE request_id = %s", (request_id,))
    if not usage or usage["state"] != "SETTLED":
        raise CoreError("MODEL_USAGE_UNRESOLVED", 409)


async def prepare_memories(
    self,
    candidates,
    confirmed=False,
    job_id=None,
    logical_request_id=None,
    request_id=None,
    review_feedback=None,
    repair_candidates=(),
):
    """Plan semantic matches without holding a transaction across model I/O."""
    authority = (
        owner_authority.current(self.store, self.settings, self.clock, purpose="memory")
        if owner_authority.required(self.store, self.settings)
        else None
    )
    if authority is None and private_life.strict(self.store, self.settings):
        raise CoreError("LIVE_TEXT_MEMORY_READ_ONLY", 403)
    s, cfg = self.store, self.settings
    candidates = [
        candidate
        for candidate in candidates
        if self.memory.validate_candidate(candidate, confirmed, now=self.clock()) is not None
        and source_allowed(
            s, cfg, self.clock, candidate.evidence_event_id, "memory", allow_commands=True
        )
    ]
    required = _repair_positions(repair_candidates, candidates) if repair_candidates else set()
    if required is None:
        raise MemoryReviewError("repair_incomplete", [{"reason": "repair_candidate_missing"}])
    if not candidates:
        return [], []
    job = (
        s.one(
            (
                "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, "
                "jobs.kind AS kind, jobs.state AS state, jobs.idempotency_key"
                " AS idempotency_key, jobs.attempts AS attempts, "
                "jobs.max_attempts AS max_attempts, jobs.lease_owner AS "
                "lease_owner, jobs.lease_until AS lease_until, "
                "jobs.next_attempt_at AS next_attempt_at, "
                "jobs.last_error_code AS last_error_code, jobs.created_at AS "
                "created_at, jobs.updated_at AS updated_at, jobs.lease_token "
                "AS lease_token, jobs.context_tracked AS context_tracked, "
                "jobs.authority_json AS authority_json FROM jobs AS jobs "
                "WHERE jobs.job_id = %s"
            ),
            (job_id,),
        )
        if job_id
        else None
    )
    if job:
        reply_revision.remember_sources(s, job, {row.evidence_event_id for row in candidates})
    existing = {}
    candidate_proof = self.memory_context.pin(
        self.clock(),
        event_ids={row.evidence_event_id for row in candidates},
    )

    def check_candidates():
        if authority:
            owner_authority.current(
                s, cfg, self.clock, purpose="memory", authority=authority.authority
            )
        if job:
            reply_revision.check_job(s, job)
        self.memory_context.revalidate(candidate_proof, self.clock())
        if not all(
            source_allowed(
                s, cfg, self.clock, item.evidence_event_id, "memory", allow_commands=True
            )
            for item in candidates
        ):
            raise CoreError("PRIVACY_CHANGED", 409)

    for candidate in candidates:
        for row in await self.memory.search_async(
            candidate.content,
            subjects=[candidate.subject],
            limit=6,
            now=self.clock(),
            source_check=check_candidates,
            clock=self.clock,
        ):
            if (row["origin"] == "event_observed") == bool(candidate.evidence_reference_id):
                existing[row["memory_id"]] = row
    catalog = {f"m{i}": row for i, row in enumerate(existing.values())}
    if len(candidates) == 1:
        item = candidates[0]
        same = next((row for row in catalog.values() if row["content"] == item.content), None)
        # A recorded positive link permits replay of the same fact and source.
        # A new reference, update or outstanding repair still needs review.
        replay = (
            same
            and item.evidence_event_id in same["evidence_ids"]
            and item.kind == same["kind"]
            and item.fact_key == same["fact_key"]
            and not item.evidence_reference_id
            and not item.update
            and not review_feedback
            and not required
        )
        if replay or (confirmed and (same or not catalog)):
            return candidates, [
                {"same": same["memory_id"] if same else None, "conflicts": [], "replaces": []}
            ]
    from companion.memory_context import MemorySources

    sources = MemorySources(self.memory, self.clock())
    quote_chars = min(2000, cfg.context_soft_tokens)

    def excerpt(text, quote=""):
        nonlocal quote_chars
        start = max(0, text.find(quote) - 100) if quote else 0
        value = text[start : start + min(300, quote_chars)]
        quote_chars -= len(value)
        return value

    def quotes(memory_id):
        result = []
        for event in sources.evidence(memory_id)[:2]:
            text = excerpt(json.loads(event["body_json"]).get("text", ""))
            if text:
                result.append(text)
        return result

    reviewed_candidates = []
    for i, row in enumerate(candidates):
        if row.evidence_reference_id:
            text = memory_references.read(self.memory, row.evidence_reference_id, self.clock())[
                "text"
            ]
        else:
            event = s.one(
                "SELECT body_json FROM events WHERE event_id = %s", (row.evidence_event_id,)
            )
            text = json.loads(event["body_json"]).get("text", "")
        reviewed_candidates.append(
            {
                "ref": f"c{i}",
                "subject": row.subject,
                "kind": row.kind,
                "content": row.content,
                "reference": bool(row.evidence_reference_id),
                "quote": row.evidence_quote,
                "speaker": row.speaker,
                "update": row.update,
                "source_excerpt": excerpt(text, row.evidence_quote),
            }
        )
    payload = {
        "existing": [
            {
                "ref": ref,
                "subject": row["subject"],
                "kind": row["kind"],
                "content": row["content"],
                "reference": row["origin"] == "event_observed",
                "quotes": quotes(row["memory_id"]),
            }
            for ref, row in catalog.items()
        ],
        "candidates": reviewed_candidates,
    }
    if review_feedback:
        payload["review_feedback"] = review_feedback
    messages = [
        {"role": "system", "content": REVIEW_RULES},
        {"role": "user", "content": encode(payload)},
    ]
    proof = self.memory_context.pin(
        self.clock(),
        rows=list(catalog.values()),
        event_ids={row.evidence_event_id for row in candidates},
    )
    if job:
        reply_revision.remember_sources(s, job, proof["source_event_ids"])
    request_id = budget.reserve(
        s,
        cfg,
        job_id,
        "memory_review",
        messages,
        self.clock(),
        logical_request_id=logical_request_id,
        request_id=request_id,
        clock=self.clock,
    )

    def check_review():
        self.memory_context.revalidate(proof, self.clock())
        check_candidates()

    response = await budget.complete(
        self.model,
        self.store,
        self.settings,
        request_id,
        messages,
        "memory_review",
        self.clock,
        source_check=check_review,
    )
    _settled(s, request_id)
    # Preserve the existing write-reconciliation failure contract. A real input
    # recall already cancelled the tracked job; a concurrent explicit forget
    # remains PRIVACY_CHANGED and cannot commit the old review result.
    self.memory_context.revalidate(proof, self.clock())
    check_candidates()
    if job:
        reply_revision.check_job(s, job)
    validation_reason = "schema"
    disagreements, retained = [], []
    try:
        value = json.loads(response.text)
        # Only lossless single-reference presentations are repairable. The full strict schema
        # and the catalog/subject/origin checks below still validate every reference.
        if isinstance(value, dict) and isinstance(value.get("relations"), list):
            for relation in value["relations"]:
                if not isinstance(relation, dict):
                    continue
                if (
                    isinstance(relation.get("same"), list)
                    and len(relation["same"]) == 1
                    and isinstance(relation["same"][0], str)
                ):
                    relation["same"] = relation["same"][0]
                for field in ("conflicts", "replaces"):
                    if isinstance(relation.get(field), list):
                        relation[field] = [
                            item["ref"] if isinstance(item, dict) and set(item) == {"ref"} else item
                            for item in relation[field]
                        ]
        review = MemoryReview.model_validate(value)
        if len(review.relations) != len(candidates):
            validation_reason = "relation_count"
            raise ValueError("relation count mismatch")
        resolutions, supported, positions = [], [], {}
        for i, relation in enumerate(review.relations):
            unsupported_replacement = relation.claims_replacement and not relation.update_authorized
            if unsupported_replacement:
                reason = "unsupported_replacement_claim"
            elif bool(candidates[i].update) != relation.update_authorized:
                reason = "update_marker_mismatch"
            elif relation.supported:
                reason = None
            elif i in required:
                reason = "repair_candidate_rejected"
            else:
                s.audit(
                    "memory.candidate_rejected",
                    candidates[i].evidence_event_id,
                    "memory",
                    self.clock(),
                    reason="UNSUPPORTED_MEMORY_CLAIM",
                )
                continue
            retained.append(candidates[i])
            if reason:
                disagreements.append(
                    {
                        "candidate": f"c{len(retained) - 1}",
                        "reason": reason,
                        "expected_update": "replaces_previous"
                        if relation.update_authorized
                        else None,
                    }
                )
                continue
            refs = ([relation.same] if relation.same else []) + relation.conflicts
            if len(refs) != len(set(refs)):
                validation_reason = "duplicate_relation"
                raise ValueError("duplicate relation")
            if (
                len(set(relation.replaces)) != len(relation.replaces)
                or not set(relation.replaces) <= set(relation.conflicts)
                or relation.replaces
                and not relation.update_authorized
            ):
                validation_reason = "invalid_replacement"
                raise ValueError("invalid replacement relation")
            allowed = {
                **catalog,
                **{
                    f"c{j}": {
                        "subject": candidates[j].subject,
                        "origin": "event_observed"
                        if candidates[j].evidence_reference_id
                        else "model_inferred",
                    }
                    for j in positions
                },
            }
            if any(
                ref not in allowed
                or allowed[ref]["subject"] != candidates[i].subject
                or (allowed[ref]["origin"] == "event_observed")
                != bool(candidates[i].evidence_reference_id)
                for ref in refs
            ):
                validation_reason = "invalid_reference"
                raise ValueError("invalid memory reference")
            if relation.update_authorized and relation.conflicts and not relation.replaces:
                validation_reason = "missing_replacement_target"
                raise ValueError(
                    "authorized update cannot leave every conflicting target current"
                )

            def resolve(ref):
                return (
                    catalog[ref]["memory_id"]
                    if ref in catalog
                    else f"c{positions[int(ref[1:])]}"
                    if ref
                    else None
                )

            positions[i] = len(supported)
            supported.append(candidates[i])
            resolutions.append(
                {
                    "same": resolve(relation.same) if relation.same else None,
                    "conflicts": [resolve(ref) for ref in relation.conflicts],
                    "replaces": [resolve(ref) for ref in relation.replaces],
                }
            )
    except ValueError as error:
        s.audit(
            "memory.review_invalid",
            request_id,
            "memory",
            self.clock(),
            reason=validation_reason,
        )
        raise MemoryReviewError(validation_reason) from error
    if disagreements:
        s.audit(
            "memory.review_invalid",
            request_id,
            "memory",
            self.clock(),
            reason="update_disagreement",
        )
        raise MemoryReviewError("update_disagreement", disagreements, candidates=retained)
    return supported, resolutions


def extraction_sources(core, local_day):
    """The original bounded native eligibility query, shared by both entrypoints."""
    cfg, now = core.settings, core.clock()
    day_filter = " AND e.occurred_at>=%s AND e.occurred_at<%s " if local_day else ""
    bounds = day_bounds(local_day, cfg.timezone) if local_day else ()
    authority_filter, authority_params = "", ()
    if owner_authority.required(core.store, cfg):
        view = owner_authority.current(core.store, cfg, core.clock, purpose="memory")
        # Scope nomination precedes LIMIT: an earlier revision without a history
        # grant must not permanently hide eligible current-revision work.
        authority_filter = (
            " AND (EXISTS (SELECT 1 FROM runtime_meta r WHERE "
            "r.key='owner_input:'||e.event_id AND ((CASE WHEN r.value IS "
            "JSON THEN (r.value::json->'authority')::text END)=%s OR (%s "
            "AND e.occurred_at>=%s))) OR EXISTS (SELECT 1 FROM "
            "runtime_meta r WHERE r.key='live_text_input:'||e.event_id)) "
        )
        authority_params = (
            encode(view.authority.as_dict()),
            "history" in view.policy.purposes,
            view.policy.history.not_before.isoformat() if view.policy.history.not_before else now,
        )
    rows = core.store.rows(
        (
            "SELECT "
            "e.event_id,e.schema_version,e.source,e.source_event_id,e.conversation_id,e.actor_id,e.event_type,e.occurred_at,e.ingested_at,e.privacy_scope,e.body_json,e.trace_id,e.idempotency_key,e.fingerprint,e.redacted,e.memory_processed,e.reply_revision"
            " FROM events e JOIN jobs j ON j.event_id=e.event_id WHERE "
            "e.memory_processed=0 AND e.redacted=0 AND e.actor_id='owner'"
            " AND e.source='qq.onebot' AND j.kind='chat' AND "
            "(j.state='SUCCEEDED' OR (j.state='CANCELLED' AND "
            "j.last_error_code=%s)) AND NOT EXISTS (SELECT 1 FROM "
            "runtime_meta WHERE key='erased_event:'||e.event_id) AND "
            "e.ingested_at>=%s AND e.ingested_at<=%s AND "
            "e.occurred_at<=%s "
        )
        + day_filter
        + authority_filter
        + " ORDER BY e.record_sequence LIMIT 20",
        (
            reply_revision.SUPERSEDED,
            after(now, -cfg.raw_event_retention_days * 86400),
            after(now, -cfg.extraction_idle_seconds),
            now,
            *bounds,
            *authority_params,
        ),
    )
    return [
        row
        for row in rows
        if source_allowed(core.store, cfg, core.clock, row["event_id"], "memory")
    ]


def _input_deadline(core, local_day):
    events = core.store.rows(
        (
            "SELECT e.ingested_at, e.occurred_at FROM events AS e JOIN "
            "jobs AS j USING (event_id) WHERE e.memory_processed = 0 AND "
            "e.redacted = 0 AND e.actor_id = 'owner' AND e.source = "
            "'qq.onebot' AND j.kind = 'chat' AND (j.state = 'SUCCEEDED' "
            "OR (j.state = 'CANCELLED' AND j.last_error_code = %s)) AND "
            "e.ingested_at > %s ORDER BY e.ingested_at NULLS FIRST LIMIT "
            "20"
        ),
        (reply_revision.SUPERSEDED, after(core.clock(), -core.settings.extraction_idle_seconds)),
    )
    bounds = day_bounds(local_day, core.settings.timezone) if local_day else None
    times = [
        after(event["ingested_at"], core.settings.extraction_idle_seconds)
        for event in events
        if bounds is None or bounds[0] <= event["occurred_at"] < bounds[1]
    ]
    return datetime.fromisoformat(min(times)) if times else None


@contextmanager
def _extraction_lease_row(store, now):
    # A unique upsert also locks the first claim, when there is no row to SELECT
    # FOR UPDATE yet. Keep an existing owner's value; the caller rechecks it.
    placeholder = encode({"token": uid("memory_lease"), "until": now})
    row = store.one(
        "INSERT INTO runtime_meta(key,value) VALUES ('memory_extraction_lease',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=runtime_meta.value RETURNING value",
        (placeholder,),
    )
    yield None if row["value"] == placeholder else row
    # Empty/not-due work leaves no newly created lease behind. Exceptions roll
    # the owning transaction back, including a newly inserted placeholder.
    store.execute("DELETE FROM runtime_meta WHERE key='memory_extraction_lease' "
                  "AND value=%s", (placeholder,))


async def extract_step(core, local_day=None, *, on_checkpoint=None):
    """One shared persistent batch for standalone and natural-day extraction.

    The existing global native lease serializes a phase; runtime_meta carries the
    source-linked checkpoint across phases. The optional callback only joins a
    natural-day coordinator checkpoint to the same transaction, never owns a copy.
    """
    authority = None
    if owner_authority.required(core.store, core.settings):
        try:
            authority = owner_authority.current(
                core.store, core.settings, core.clock, purpose="memory"
            )
        except CoreError:
            return UnitResult(False)
    if (
        authority is None
        and private_life.strict(core.store, core.settings)
        or not core.settings.memory_enabled
        or not core.settings.pricing_known
    ):
        return UnitResult(False)
    from companion.erasure import cleanup_contracts

    if not any(contract.name == "memory-work" for contract in cleanup_contracts()):
        raise CoreError("MEMORY_ERASURE_CONTRACT_REQUIRED", 503)
    s, now = core.store, core.clock()
    lease = encode({"token": uid("memory_lease"), "until": after(now, core.settings.lease_seconds)})
    with s.transaction(), _extraction_lease_row(s, now) as old:
        if authority:
            if private_life.busy(s, now) or budget.chat_pending(s, now):
                return UnitResult(False)
        elif private_life.enabled(core.settings):
            live_text.guard(s, core.settings, core.clock)
            if private_life.busy(s, now) or private_life.owner_waiting(s, core.settings, now):
                return UnitResult(False)
        if old and json.loads(old["value"])["until"] > now:
            return UnitResult(False, datetime.fromisoformat(json.loads(old["value"])["until"]))
        row = s.one("SELECT value FROM runtime_meta WHERE key = %s", (CHECKPOINT_KEY,))
        record = json.loads(row["value"]) if row else None
        expected = row["value"] if row else None
        if record and record["batch"].get("phase") not in TERMINAL_PHASES:
            changed_authority = (
                authority and record["batch"].get("authority") != authority.authority.as_dict()
            )
            if record["timezone"] != core.settings.timezone or changed_authority:
                batch = record["batch"]
                usage = s.one(
                    "SELECT state FROM model_usage WHERE request_id = %s",
                    (batch.get("request_id"),),
                )
                unknown = usage and usage["state"] in {"RESERVED", "UNKNOWN"}
                batch.update(
                    phase="UNKNOWN" if unknown else "CANCELLED",
                    error="OWNER_AUTHORITY_REVISION_CHANGED"
                    if changed_authority
                    else "MEMORY_TIMEZONE_CHANGED",
                )
                if unknown:
                    budget.mark_reservation_unknown(s, batch["request_id"])
                    for key in batch.get("event_ids", []):
                        s.execute(
                            "UPDATE events SET memory_processed = 1 WHERE event_id = %s", (key,)
                        )
                batch.pop("candidates", None)
                batch.pop("interaction_signals", None)
                batch.pop("episode_summaries", None)
                batch.pop("proof", None)
                batch.pop("review_feedback", None)
                batch.pop("repair_required", None)
                batch.pop("repair_candidates", None)
                memory_recovery.finish(s, record)
                record["next_attempt_at"] = None
                s.execute(
                    "UPDATE runtime_meta SET value = %s WHERE key = %s",
                    (encode(record), CHECKPOINT_KEY),
                )
                return UnitResult(True)
            if local_day is not None and record["local_day"] != local_day.isoformat():
                return UnitResult(False, datetime.fromisoformat(after(now, 60)))
            due = record.get("next_attempt_at")
            if due and due > now:
                return UnitResult(False, datetime.fromisoformat(due))
        else:
            if len(extraction_sources(core, local_day)) < core.settings.extraction_min_events:
                return UnitResult(False, _input_deadline(core, local_day))
            record = {
                "batch_id": uid("memory_batch"),
                "timezone": core.settings.timezone,
                "local_day": local_day.isoformat() if local_day else None,
                "batch": {"authority": authority.authority.as_dict()} if authority else {},
                "source_event_ids": [],
                "next_attempt_at": None,
            }
            expected = encode(record)
            s.execute(
                (
                    "INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON "
                    "CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "record_sequence = DEFAULT"
                ),
                (CHECKPOINT_KEY, expected),
            )
        if old and record["batch"].get("request_id"):
            budget.mark_reservation_unknown(s, record["batch"]["request_id"])
        s.execute(
            (
                "INSERT INTO runtime_meta (key, value) VALUES "
                "('memory_extraction_lease', %s) ON CONFLICT(key) DO UPDATE "
                "SET value = excluded.value, record_sequence = DEFAULT"
            ),
            (lease,),
        )

    def persist():
        nonlocal expected
        with nullcontext() if s.in_transaction else s.transaction():
            current = s.one("SELECT value FROM runtime_meta WHERE key = %s", (CHECKPOINT_KEY,))
            if not current or current["value"] != expected:
                raise CoreError("PRIVACY_CHANGED", 409)
            batch = record["batch"]
            record["source_event_ids"] = sorted(
                set(record["source_event_ids"])
                | set(batch.get("event_ids", []))
                | set(batch.get("proof", {}).get("source_event_ids", []))
            )
            if batch.get("phase") in TERMINAL_PHASES:
                memory_recovery.retain(core, record)
                memory_recovery.finish(s, record)
                if batch["phase"] in {"FAILED", "UNKNOWN"}:
                    # A terminal failed/uncertain batch is exhausted work, not a fact.
                    # Its usage remains charged; neither lane may silently replay it.
                    for key in batch.get("event_ids", []):
                        s.execute(
                            "UPDATE events SET memory_processed = 1 WHERE event_id = %s", (key,)
                        )
                batch.pop("candidates", None)
                batch.pop("interaction_signals", None)
                batch.pop("episode_summaries", None)
                batch.pop("proof", None)
                batch.pop("review_feedback", None)
                batch.pop("repair_required", None)
                batch.pop("repair_candidates", None)
                if batch.get("request_id") and not s.one(
                    "SELECT 1 FROM model_usage WHERE request_id = %s",
                    (batch["request_id"],),
                ):
                    batch.pop("request_id", None)
            value = encode(record)
            s.execute("UPDATE runtime_meta SET value = %s WHERE key = %s", (value, CHECKPOINT_KEY))
            if on_checkpoint:
                on_checkpoint(record)
            expected = value

    try:
        target_day = date.fromisoformat(record["local_day"]) if record["local_day"] else None
        result = await extraction_step(core, target_day, lease, record["batch"], persist)
        if not result.progressed and record["batch"].get("phase") not in TERMINAL_PHASES:
            record["next_attempt_at"] = after(core.clock(), 60)
            persist()
            return UnitResult(False, datetime.fromisoformat(record["next_attempt_at"]))
        record["next_attempt_at"] = None
        persist()
        return UnitResult(True) if record["batch"].get("phase") in TERMINAL_PHASES else result
    except CoreError as error:
        if error.code not in {"PRIVACY_CHANGED", "MEMORY_STALE_LEASE", "ERASURE_REPLAY_REQUIRED"}:
            raise
        if error.code == "PRIVACY_CHANGED":
            s.execute(
                "DELETE FROM runtime_meta WHERE key = %s AND value = %s", (CHECKPOINT_KEY, expected)
            )
        return UnitResult(False)
    finally:
        s.execute(
            "DELETE FROM runtime_meta WHERE key = 'memory_extraction_lease' AND value = %s",
            (lease,),
        )


async def extract_once(core, local_day=None):
    """Batch convenience over the same persistent single-admission implementation."""
    for _ in range(3):
        try:
            result = await extract_step(core, local_day)
        except CoreError as error:
            if error.code in {"PRIVACY_CHANGED", "MEMORY_STALE_LEASE", "ERASURE_REPLAY_REQUIRED"}:
                return False
            raise
        if not result.progressed:
            return False
        record = checkpoint_state(core.store)
        if record and record["batch"].get("phase") in TERMINAL_PHASES:
            return record["batch"]["phase"] == "COMPLETE"
    return False


def _validate_batch(core, lease, batch):
    if owner_authority.required(core.store, core.settings):
        owner_authority.current(
            core.store,
            core.settings,
            core.clock,
            purpose="memory",
            authority=batch.get("authority"),
        )
        if budget.chat_pending(core.store, core.clock()):
            raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
    if not core._owns_memory_lease(lease):
        raise CoreError("MEMORY_STALE_LEASE", 409)
    if (
        not owner_authority.required(core.store, core.settings)
        and private_life.enabled(core.settings)
        and private_life.owner_waiting(core.store, core.settings, core.clock())
    ):
        raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
    core.memory_context.revalidate(batch["proof"], core.clock())
    for reference_id in batch.get("reference_ids", []):
        memory_references.read(core.memory, reference_id, core.clock())
    if not all(
        source_allowed(core.store, core.settings, core.clock, key, "memory")
        for key in batch["event_ids"]
    ):
        raise CoreError("PRIVACY_CHANGED", 409)
    if batch.get("phase_epoch") != epoch(core.store):
        raise CoreError("PRIVACY_CHANGED", 409)


def _recover_phase(core, batch, persist):
    """Unknown admissions stop; settled lost output uses the original bounded retry."""
    request_id = batch.get("request_id")
    usage = core.store.one("SELECT state FROM model_usage WHERE request_id = %s", (request_id,))
    if usage is None:
        # Intent persisted before reserve: no reservation means no dispatched call.
        batch["phase"] = "REVIEW" if batch["phase"] == "REVIEWING" else "EXTRACT"
        batch.pop("request_id", None)
    elif usage["state"] == "SETTLED":
        previous = "REVIEW" if batch["phase"] == "REVIEWING" else "EXTRACT"
        if previous == "REVIEW":
            batch["review_attempts"] = batch.get("review_attempts", 0) + 1
            attempts = batch["review_attempts"]
        else:
            row = core.store.one(
                "SELECT value FROM runtime_meta WHERE key = %s", (batch["attempt_key"],)
            )
            attempts = int(row["value"]) if row else 0
        batch["phase"] = "FAILED" if attempts >= core.settings.max_attempts else previous
        batch["error"] = "MEMORY_RESULT_NOT_CHECKPOINTED"
        if batch["phase"] != "FAILED":
            batch.pop("request_id", None)
    else:
        if usage["state"] == "RESERVED":
            budget.mark_reservation_unknown(core.store, request_id)
        batch["phase"] = "UNKNOWN" if usage["state"] in {"UNKNOWN", "RESERVED"} else "FAILED"
        batch["error"] = "MEMORY_RESULT_NOT_CHECKPOINTED"
        batch.pop("candidates", None)
        batch.pop("interaction_signals", None)
    persist()
    return UnitResult(True)


async def extraction_step(core, local_day, lease, batch, persist):
    """One extraction admission OR one conflict-review admission and native commit."""
    s, cfg, now = core.store, core.settings, core.clock()
    if batch.get("phase") in {"EXTRACTING", "REVIEWING"}:
        return _recover_phase(core, batch, persist)
    if batch.get("phase") in {"COMPLETE", "SKIPPED", "FAILED", "UNKNOWN"}:
        return UnitResult(False)
    if not batch.get("phase"):
        events = extraction_sources(core, local_day)
        if len(events) < cfg.extraction_min_events:
            batch["phase"] = "SKIPPED"
            persist()
            return UnitResult(True)
        ids = [event["event_id"] for event in events]
        batch.update(
            phase="EXTRACT",
            event_ids=ids,
            attempt_key="extract_attempts:" + ids[0],
            proof=core.memory_context.pin(now, event_ids=set(ids)),
        )
    events = [
        s.one(
            (
                "SELECT events.event_id AS event_id, events.schema_version AS"
                " schema_version, events.source AS source, "
                "events.source_event_id AS source_event_id, "
                "events.conversation_id AS conversation_id, events.actor_id "
                "AS actor_id, events.event_type AS event_type, "
                "events.occurred_at AS occurred_at, events.ingested_at AS "
                "ingested_at, events.privacy_scope AS privacy_scope, "
                "events.body_json AS body_json, events.trace_id AS trace_id, "
                "events.idempotency_key AS idempotency_key, "
                "events.fingerprint AS fingerprint, events.redacted AS "
                "redacted, events.memory_processed AS memory_processed, "
                "events.reply_revision AS reply_revision FROM events AS "
                "events WHERE events.event_id = %s"
            ),
            (key,),
        )
        for key in batch["event_ids"]
    ]
    request_id = None
    attempts = s.one("SELECT value FROM runtime_meta WHERE key = %s", (batch["attempt_key"],))
    attempts = int(attempts["value"]) if attempts else 0
    previous_phase = batch["phase"]
    batch["phase_epoch"] = epoch(s)
    try:
        _validate_batch(core, lease, batch)
        if any(event is None for event in events):
            raise CoreError("PRIVACY_CHANGED", 409)
        if all(event["memory_processed"] for event in events):
            batch.update(phase="COMPLETE", independently_processed=True)
            batch.pop("candidates", None)
            batch.pop("interaction_signals", None)
            persist()
            return UnitResult(True)
        # Persist a preallocated identity before any reservation or dispatch. The
        # caller's source-linked run owns this intent even if the process dies.
        request_id = uid("request")
        batch.update(
            phase="EXTRACTING" if previous_phase == "EXTRACT" else "REVIEWING",
            request_id=request_id,
        )
        persist()
        if previous_phase == "EXTRACT":
            groups = memory_enrichment.summary_groups(core.memory, events, core.clock())
            data = [
                {
                    "event_id": event["event_id"],
                    "conversation_id": event["conversation_id"],
                    "occurred_at": event["occurred_at"],
                    "episode_source_event_ids": next(
                        (group for group in groups if event["event_id"] in group), []
                    ),
                    "text": json.loads(event["body_json"]).get("text", ""),
                    "references": memory_references.inputs(core, event),
                }
                for event in events
            ]
            # Share the existing bounded context allowance across the entire batch.
            # Receipt/proof payloads stay local; the extractor receives source labels
            # and an explicitly marked text excerpt, never native credentials.
            remaining = cfg.content.max_context_chars
            for item in data:
                kept = []
                for reference in item["references"]:
                    if remaining <= 0:
                        break
                    text = reference["text"][:remaining]
                    remaining -= len(text)
                    kept.append(
                        {
                            "reference_id": reference["reference_id"],
                            "kind": reference["kind"],
                            "text": text,
                            "excerpt": len(text) < len(reference["text"]),
                        }
                    )
                item["references"] = kept
            payload, ref_map = short_refs(data)
            if batch.get("repair_required"):
                payload["review_feedback"] = batch["review_feedback"]
            batch["ref_map"] = ref_map
            messages = [
                {"role": "system", "content": EXTRACT_RULES},
                {"role": "user", "content": encode(payload)},
            ]
            batch["reference_ids"] = [
                ref["reference_id"] for event in data for ref in event["references"]
            ]
            batch["proof"] = core.memory_context.pin(
                core.clock(),
                event_ids={
                    *batch["event_ids"],
                    *(
                        source
                        for reference_id in batch["reference_ids"]
                        for source in memory_references.read(
                            core.memory, reference_id, core.clock()
                        )["proof"]["source_event_ids"]
                    ),
                },
            )
            persist()
            budget.reserve(
                s,
                cfg,
                None,
                "memory_extract",
                messages,
                now,
                request_id=request_id,
                logical_request_id=batch["attempt_key"],
                clock=core.clock,
            )
            attempts += 1
            s.execute(
                (
                    "INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON "
                    "CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "record_sequence = DEFAULT"
                ),
                (batch["attempt_key"], str(attempts)),
            )
            response = await budget.complete(
                core.model,
                s,
                cfg,
                request_id,
                messages,
                "memory_extract",
                core.clock,
                source_check=lambda: _validate_batch(core, lease, batch),
            )
            _settled(s, request_id)
            if response.finish_reason == "length":
                # The JSON is cut off and the batch will be retried; keep the evidence.
                s.audit(
                    "memory.extract_truncated",
                    request_id,
                    "memory",
                    now,
                    output_tokens=response.output_tokens,
                    max_tokens=cfg.output_tokens("memory_extract"),
                    events=len(batch["event_ids"]),
                )
            extraction, repaired = parse_extraction(response.text, batch.get("ref_map", {}))
            if repaired:
                s.audit("memory.output_repaired", events[0]["event_id"], "memory", now)
            with s.transaction():
                _validate_batch(core, lease, batch)
                _settled(s, request_id)
                rejected = {}
                signals = mood_appraisals.validate(
                    s, extraction.interaction_signals, set(batch["event_ids"]), rejected
                )
                # Content-free funnel: did the model answer the field, and what survived.
                s.audit(
                    "mood.appraisals_extracted",
                    request_id,
                    "memory",
                    now,
                    field_present="interaction_signals" in extraction.model_fields_set,
                    finish_reason=response.finish_reason,
                    returned=len(extraction.interaction_signals),
                    kept=len(signals),
                    rejected=rejected,
                    reasons={
                        reason: sum(item["reason"] == reason for item in signals)
                        for reason in sorted({item["reason"] for item in signals})
                    },
                )
                mood_appraisals.record(s, cfg, signals, request_id, core.clock())
            memory_enrichment.validate_summaries(
                core.memory, events, extraction.episode_summaries, core.clock()
            )
            candidates = []
            shown_references = {
                ref["reference_id"]: ref["text"] for item in data for ref in item["references"]
            }
            for candidate in extraction.memories:
                if candidate.evidence_event_id not in batch["event_ids"]:
                    s.audit(
                        "memory.candidate_rejected",
                        events[0]["event_id"],
                        "memory",
                        now,
                        reason="MISSING_BATCH_EVIDENCE",
                    )
                    continue
                if candidate.evidence_reference_id and candidate.evidence_quote not in (
                    shown_references.get(candidate.evidence_reference_id, "")
                ):
                    s.audit(
                        "memory.candidate_rejected",
                        candidate.evidence_event_id,
                        "memory",
                        now,
                        reason="REFERENCE_QUOTE_MISMATCH",
                    )
                    continue
                try:
                    rejection = {}
                    if (
                        core.memory.validate_candidate(
                            candidate, now=core.clock(), rejection=rejection
                        )
                        is not None
                    ):
                        candidates.append(candidate.model_dump(mode="json"))
                    else:
                        s.audit(
                            "memory.candidate_rejected",
                            candidate.evidence_event_id,
                            "memory",
                            now,
                            **rejection,
                        )
                except CoreError as error:
                    s.audit(
                        "memory.candidate_rejected",
                        candidate.evidence_event_id,
                        "memory",
                        now,
                        reason=error.code,
                    )
            if (
                batch.get("repair_candidates")
                and _repair_positions(batch["repair_candidates"], candidates) is None
            ):
                raise MemoryReviewError(
                    "repair_incomplete", [{"reason": "repair_candidate_missing"}]
                )
            batch.update(
                phase="REVIEW",
                candidates=candidates,
                episode_summaries=[
                    item.model_dump(mode="json") for item in extraction.episode_summaries
                ],
                extraction_request_id=request_id,
            )
            batch.pop("repair_required", None)
            batch.pop("review_feedback", None)
            batch.pop("request_id", None)
            persist()
            return UnitResult(True)
        # A pre-upgrade REVIEW checkpoint may still hold validated, uncommitted signals.
        if batch.get("interaction_signals"):
            with s.transaction():
                _validate_batch(core, lease, batch)
                extraction_id = batch["extraction_request_id"]
                _settled(s, extraction_id)
                signals = [
                    item
                    for item in batch["interaction_signals"]
                    if item["event_id"] in batch["event_ids"]
                    and mood_appraisals.still_valid(s, {**item, "request_id": extraction_id})
                ]
                mood_appraisals.record(s, cfg, signals, extraction_id, core.clock())
                batch.pop("interaction_signals")
                persist()
        candidates = [Candidate.model_validate(value) for value in batch["candidates"]]
        candidates, resolutions = await prepare_memories(
            core,
            candidates,
            logical_request_id=batch["attempt_key"],
            request_id=request_id,
            review_feedback=batch.get("review_feedback"),
            repair_candidates=batch.get("repair_candidates", ()),
        )
        with s.transaction(control_write=True):
            _validate_batch(core, lease, batch)
            at = core.clock()
            memory_ids = core.add_memories(candidates, resolutions, at)
            episode_ids = []
            for conv_id in dict.fromkeys(event["conversation_id"] for event in events):
                episode_id = core.memory.episodes(
                    [event for event in events if event["conversation_id"] == conv_id],
                    at,
                )
                if episode_id:
                    episode_ids.append(episode_id)
            summaries = [
                EpisodeSummary.model_validate(value) for value in batch.get("episode_summaries", [])
            ]
            memory_enrichment.record(
                core.memory,
                candidates,
                memory_ids,
                events,
                summaries,
                batch["extraction_request_id"],
                at,
                reference_ids=batch.get("reference_ids", []),
            )
            s.execute("DELETE FROM runtime_meta WHERE key = %s", (batch["attempt_key"],))
            batch.update(
                phase="COMPLETE", committed=True, memory_ids=memory_ids, episode_ids=episode_ids
            )
            batch.pop("candidates", None)
            batch.pop("interaction_signals", None)
            batch.pop("episode_summaries", None)
            batch.pop("proof", None)
            persist()
        return UnitResult(True)
    except Exception as error:
        code = error.code if isinstance(error, CoreError) else "MEMORY_INVALID_OUTPUT"
        if code in {"PRIVACY_CHANGED", "MEMORY_STALE_LEASE", "ERASURE_REPLAY_REQUIRED"}:
            raise
        usage = s.one("SELECT state FROM model_usage WHERE request_id = %s", (request_id,))
        unsent = request_id is not None and budget.not_dispatched(s, request_id)
        if previous_phase == "REVIEW" and usage and not unsent:
            batch["review_attempts"] = batch.get("review_attempts", 0) + 1
        unknown = usage and usage["state"] in {"RESERVED", "UNKNOWN"}
        blocked = budget.capacity_wait(s, code, request_id=request_id) or code in {
            "BACKGROUND_YIELD_TO_CHAT",
            "DAILY_BUDGET_EXHAUSTED",
            "MONTHLY_BUDGET_EXHAUSTED",
            "UNKNOWN_PRICING",
        }
        with s.transaction():
            if isinstance(error, MemoryReviewError):
                batch["review_feedback"] = error.feedback
                if error.reextract:
                    batch["repair_required"] = True
                    if "candidates" in error.feedback:
                        batch["repair_candidates"] = error.feedback["candidates"]
                    else:
                        # An incomplete extraction still owes the last reviewed set.
                        error.feedback["candidates"] = batch["repair_candidates"]
            if previous_phase == "EXTRACT" and unsent:
                attempts = max(0, attempts - 1)
                s.execute(
                    (
                        "INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON "
                        "CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "record_sequence = DEFAULT"
                    ),
                    (batch["attempt_key"], str(attempts)),
                )
            if unknown:
                batch.update(phase="UNKNOWN", error=code)
                batch.pop("candidates", None)
                batch.pop("interaction_signals", None)
            elif not blocked and max(attempts, batch.get("review_attempts", 0)) >= cfg.max_attempts:
                for event in events:
                    s.execute(
                        "UPDATE events SET memory_processed = 1 WHERE event_id = %s",
                        (event["event_id"],),
                    )
                s.execute("DELETE FROM runtime_meta WHERE key = %s", (batch["attempt_key"],))
                batch.update(phase="FAILED", error=code)
            else:
                batch.update(
                    phase="EXTRACT" if batch.get("repair_required") else previous_phase, error=code
                )
                batch.pop("request_id", None)
            if not blocked:
                s.audit(
                    "memory.batch_failed", events[0]["event_id"], "memory", now, attempts=attempts
                )
            persist()
        return UnitResult(False)
