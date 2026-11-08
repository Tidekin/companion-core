"""Bounded, source-checked views over the existing Memory store; no second memory store."""

import hashlib
import json
from datetime import datetime

from companion import retrieval_work
from companion.contracts import CoreError
from companion.privacy import epoch, redact, sensitive
from companion.store import after, encode

MEMORY_FIELDS = (
    "memory_id", "subject", "kind", "content", "status", "needs_review",
    "supersedes_memory_id", "valid_from", "valid_to", "created_at", "updated_at",
)
MEMORY_COLUMNS = (
    "memory_id", "subject", "kind", "content", "status", "origin", "confidence",
    "importance", "privacy_scope", "supersedes_memory_id", "created_at", "updated_at",
    "valid_from", "valid_to", "last_reviewed_at", "fact_key", "needs_review",
)
EVENT_COLUMNS = (
    "event_id", "schema_version", "source", "source_event_id", "conversation_id", "actor_id",
    "event_type", "occurred_at", "ingested_at", "privacy_scope", "body_json", "trace_id",
    "idempotency_key", "fingerprint", "redacted", "memory_processed", "reply_revision",
)


def read_version(memory):
    # Reuse the same Ledger/DB custody check as the real private context boundary.
    from companion.live_text import _ledger_custody

    return epoch(memory.store), _ledger_custody(memory.store, memory.settings)


def check_version(memory, version):
    if read_version(memory) != version:
        raise CoreError("PRIVACY_CHANGED", 409)


class MemorySources:
    """Positive owner evidence requires every upstream source to remain available."""

    def __init__(self, memory, now=None, excluded=(), *, bounded=False, work=None):
        self._memory = memory
        self.store, self.settings, self.now = memory.store, memory.settings, now
        self.ledger = memory.ledger
        self.excluded = set(excluded)
        self.bounded = bounded
        self.work = work or (retrieval_work.Work() if bounded else None)
        self._source_ids, self._source_bytes = set(), 0
        self._cache_scope = None
        self._loaded, self._edges, self._evidence = {}, {}, {}
        self._references, self._current, self._blocked = {}, {}, {}
        self._reference_values = {}
        self.cutoff = (datetime.fromisoformat(after(
            now, -86400 * self.settings.raw_event_retention_days,
        )) if now else None)

    def _snapshot_cache(self):
        scope = self.store.frozen_scope
        if scope is None or scope != self._cache_scope:
            for cache in (self._loaded, self._edges, self._evidence,
                          self._references, self._current, self._blocked, self._reference_values):
                cache.clear()
            self._cache_scope = scope
        return scope is not None

    def prefetch(self, memory_ids):
        """Batch proof roots and bodies only inside the current immutable DB snapshot."""
        if not self.bounded or not self._snapshot_cache() or not memory_ids:
            return
        memory_ids = [key for key in memory_ids if key not in self._evidence]
        if not memory_ids:
            return
        from companion.memory_references import MEMORY

        rows = self.work.rows(self.store,
            "SELECT m.memory_id,r.key IS NOT NULL AS reference,ARRAY(SELECT v.event_id "
            "FROM memory_evidence v WHERE v.memory_id=m.memory_id AND v.polarity=1 "
            "ORDER BY v.created_at,v.event_id LIMIT %s) AS roots FROM memories m "
            "LEFT JOIN runtime_meta r ON r.key=%s||m.memory_id "
            "WHERE m.memory_id=ANY(%s::text[])",
            (retrieval_work.EVIDENCE_ROOTS + 1, MEMORY, list(memory_ids)), limit=len(memory_ids))
        positions = {key: index for index, key in enumerate(memory_ids)}
        rows.sort(key=lambda row: positions[row["memory_id"]])
        for row in rows:
            self._references[row["memory_id"]] = row["reference"]
            self._evidence[row["memory_id"]] = [{"event_id": key} for key in row["roots"]]
        keys = list(dict.fromkeys(key for row in rows
            if len(row["roots"]) <= retrieval_work.EVIDENCE_ROOTS for key in row["roots"]))
        self._load_events(keys)

    def _load_events(self, keys):
        """Read finite source bodies in a batch after admitting their measured sizes."""
        from companion.retrieval_work import LIMITS, SOURCE_NODES, WorkLimit

        keys = [key for key in keys if key not in self._loaded and key not in self._blocked]
        if not keys:
            return
        headers = self._rows("SELECT s.event_id,COALESCE(octet_length(e.body_json),0) AS size "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS s(event_id,position) "
            "LEFT JOIN events e USING(event_id) ORDER BY s.position",
            (keys[:SOURCE_NODES],), limit=SOURCE_NODES)
        for key in keys[SOURCE_NODES:]:
            self._blocked[key] = "source_nodes"
            self.work.mark("source_nodes")
        admitted = []
        for header in headers:
            key, size = header["event_id"], header["size"]
            if len(self._source_ids | {key}) > SOURCE_NODES or not self.work.take("source_nodes"):
                self._blocked[key] = "source_nodes"
            elif (self._source_bytes + size > LIMITS["text_bytes"]
                  or not self.work.take("text_bytes", size)):
                self._blocked[key] = "source_text_bytes"
            else:
                self._source_ids.add(key)
                self._source_bytes += size
                admitted.append(key)
            if key in self._blocked:
                self.work.mark(self._blocked[key])
        if admitted:
            rows = self._rows("SELECT s.event_id AS source_id,"
                + ",".join("e." + field for field in EVENT_COLUMNS)
                + ",c.scope,c.owner_actor_id,r.key AS erased FROM unnest(%s::text[]) s(event_id) "
                "LEFT JOIN events e USING(event_id) "
                "LEFT JOIN conversations c USING(conversation_id) "
                "LEFT JOIN runtime_meta r ON r.key='erased_event:'||s.event_id",
                (admitted,), limit=len(admitted))
            self._loaded.update((row["source_id"], row) for row in rows)
        for key in keys:
            if key not in self._loaded and key not in self._blocked:
                raise WorkLimit("source_nodes")

    def _rows(self, statement, parameters=(), *, limit):
        if self.bounded:
            return self.work.source_rows(self.store, statement, parameters, limit=limit)
        return self.store.rows(statement, parameters)

    def _one(self, statement, parameters=()):
        rows = self._rows(statement, parameters, limit=1)
        return rows[0] if rows else None

    def _lineage(self, event_id):
        """Follow native edges and the exact legacy correction relation used by erasure."""
        if self.bounded:
            def read():
                try:
                    return self._bounded_lineage(event_id)
                except retrieval_work.WorkLimit as error:
                    return error  # A work limit is not a snapshot-write retry.

            rows = self.store.read_or_write(read)
            if isinstance(rows, retrieval_work.WorkLimit):
                raise rows
            return rows
        lineage = ('SELECT value COLLATE "C" FROM json_array_elements_text(%s::json) AS ids(value) '
                   "UNION SELECT d.source_event_id FROM event_dependencies d "
                   "JOIN sources s ON d.event_id=s.event_id")
        pending, found = {event_id}, {}
        while pending:
            rows = self.store.rows(
                "WITH RECURSIVE sources(event_id) AS (" + lineage + ") "
                "SELECT s.event_id AS source_id," + ",".join("e." + key for key in EVENT_COLUMNS)
                + ",c.scope,c.owner_actor_id,r.key AS erased "
                "FROM sources s LEFT JOIN events e USING(event_id) "
                "LEFT JOIN conversations c USING(conversation_id) "
                "LEFT JOIN runtime_meta r ON r.key='erased_event:'||s.event_id",
                (encode(sorted(pending)),),
            )
            fresh = [row for row in rows if row["source_id"] not in found]
            found.update((row["source_id"], row) for row in fresh)
            pending = set()
            for row in fresh:
                if row["source"] != "owner.decision" or row["event_type"] != "memory.corrected":
                    continue
                candidates = self.store.rows(
                    'SELECT DISTINCT e.event_id FROM memory_evidence AS copy JOIN memories AS m '
                    'USING (memory_id) JOIN memory_evidence AS v USING (memory_id) JOIN events AS '
                    'e ON e.event_id = v.event_id WHERE copy.event_id = %s AND NOT '
                    "m.supersedes_memory_id IS NULL AND e.source = 'qq.onebot'",
                    (row["event_id"],),
                )
                for candidate in candidates:
                    source = candidate["event_id"]
                    # Sharing a memory is insufficient: the Ledger checks the exact
                    # correction command, prior memory ID and replacement content.
                    if source not in found and row["event_id"] in self.ledger.retention_events(
                        self.store, [source],
                    ):
                        pending.add(source)
        return found.values()

    def _bounded_lineage(self, event_id):
        """Read complete indexed adjacency, or refuse the candidate; never cut its proof."""
        from companion.retrieval_work import EVIDENCE_ROOTS, LIMITS, SOURCE_NODES, WorkLimit

        def refuse(reason):
            if self.work:
                self.work.mark(reason)
            raise WorkLimit(reason)

        cached = self._snapshot_cache()
        pending, found = [event_id], {}
        loaded = self._loaded if cached else {}

        def read(key):
            if key in loaded:
                return loaded[key]
            if cached:
                self._load_events([key])
                if key in self._blocked:
                    refuse(self._blocked[key])
                return loaded[key]
            self._source_ids.add(key)
            if len(self._source_ids) > SOURCE_NODES:
                refuse("source_nodes")
            if self.work and not self.work.take("source_nodes"):
                refuse("source_nodes")
            size = self._one('SELECT OCTET_LENGTH(body_json) AS size FROM events WHERE '
                'event_id = %s', (key,))
            size = size["size"] if size else 0
            self._source_bytes += size
            if self._source_bytes > LIMITS["text_bytes"] or (
                self.work and not self.work.take("text_bytes", size)
            ):
                refuse("source_text_bytes")
            row = self._one(
                'SELECT s.event_id AS source_id, e.event_id AS event_id, e.schema_version AS '
                'schema_version, e.source AS source, e.source_event_id AS source_event_id, '
                'e.conversation_id AS conversation_id, e.actor_id AS actor_id, e.event_type AS '
                'event_type, e.occurred_at AS occurred_at, e.ingested_at AS ingested_at, '
                'e.privacy_scope AS privacy_scope, e.body_json AS body_json, e.trace_id AS '
                'trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS fingerprint, '
                'e.redacted AS redacted, e.memory_processed AS memory_processed, e.reply_revision '
                'AS reply_revision, c.scope AS scope, c.owner_actor_id AS owner_actor_id, r.key AS '
                'erased FROM (SELECT %s AS event_id) AS s LEFT JOIN events AS e ON s.event_id = '
                'e.event_id LEFT JOIN conversations AS c ON e.conversation_id = c.conversation_id '
                "LEFT JOIN runtime_meta AS r ON r.key = 'erased_event:' || s.event_id", (key,))
            loaded[key] = row
            return row

        while pending:
            key = pending.pop()
            if key in found:
                continue
            row = read(key)
            found[key] = row
            edges = self._edges.get(key) if cached else None
            if edges is None:
                edges = self._rows('SELECT source_event_id FROM event_dependencies WHERE '
                    'event_id = %s LIMIT %s', (key, SOURCE_NODES + 1), limit=SOURCE_NODES + 1)
                if cached:
                    self._edges[key] = edges
            if len(edges) > SOURCE_NODES:
                refuse("source_nodes")
            pending.extend(edge["source_event_id"] for edge in edges)
            if row["source"] != "owner.decision" or row["event_type"] != "memory.corrected":
                continue
            memories = self._rows(
                'SELECT DISTINCT m.memory_id, m.supersedes_memory_id, m.content FROM '
                'memory_evidence AS v JOIN memories AS m USING (memory_id) WHERE v.event_id = %s '
                'AND NOT m.supersedes_memory_id IS NULL LIMIT %s', (key, EVIDENCE_ROOTS + 1),
                limit=EVIDENCE_ROOTS + 1)
            if len(memories) > EVIDENCE_ROOTS:
                refuse("evidence_roots")
            for memory in memories:
                candidates = self._rows(
                    'SELECT DISTINCT e.event_id FROM memory_evidence AS v JOIN events AS e ON '
                    "e.event_id = v.event_id WHERE v.memory_id = %s AND e.source = 'qq.onebot' "
                    'LIMIT %s', (memory["memory_id"], EVIDENCE_ROOTS + 1), limit=EVIDENCE_ROOTS + 1)
                if len(candidates) > EVIDENCE_ROOTS:
                    refuse("evidence_roots")
                for candidate in candidates:
                    source = candidate["event_id"]
                    from companion.privacy import legacy_correction_matches

                    if source not in found and legacy_correction_matches(
                        memory, read(source)["body_json"],
                    ):
                        pending.append(source)
        return found.values()

    def lineage(self, event_id):
        """Live private provenance for owner facts and non-owner reference sources."""
        rows = list(self._lineage(event_id))
        for row in rows:
            if (row["source_id"] in self.excluded or row["event_id"] is None
                or row["redacted"] or row["erased"] or row["privacy_scope"] != "private"
                or row["scope"] != "private" or row["owner_actor_id"] != "owner"):
                return []
            if self.now and (
                datetime.fromisoformat(row["ingested_at"]) < self.cutoff
                or datetime.fromisoformat(row["occurred_at"]) > datetime.fromisoformat(self.now)
                or datetime.fromisoformat(row["ingested_at"]) > datetime.fromisoformat(self.now)
            ):
                return []
        return rows

    def event(self, event_id, *, require_text=True):
        """Only owner-authored roots can establish a memory fact."""
        root = next((row for row in self.lineage(event_id) if row["event_id"] == event_id), None)
        if not root or root["actor_id"] != "owner":
            return None
        body = json.loads(root["body_json"])
        if (not isinstance(body, dict) or not isinstance(body.get("text"), str)
            or (require_text and not body["text"].strip())):
            return None
        return root

    def evidence(self, memory_id):
        from companion.memory_references import MEMORY

        cached = self._snapshot_cache() if self.bounded else False
        reference = self._references.get(memory_id) if cached else None
        if reference is None:
            reference = bool(self._one('SELECT 1 FROM runtime_meta WHERE key = '
                '%s', (MEMORY + memory_id,)))
            if cached:
                self._references[memory_id] = reference
        rows = self._evidence.get(memory_id) if cached else None
        if rows is None:
            rows = self._rows(
            "SELECT event_id FROM memory_evidence WHERE memory_id=%s AND polarity=1 "
            "ORDER BY created_at,event_id" + (" LIMIT %s" if self.bounded else ""),
            (memory_id, retrieval_work.EVIDENCE_ROOTS + 1) if self.bounded else (memory_id,),
            limit=retrieval_work.EVIDENCE_ROOTS + 1)
            if cached:
                self._evidence[memory_id] = rows
        if self.bounded and len(rows) > retrieval_work.EVIDENCE_ROOTS:
            if self.work:
                self.work.mark("evidence_roots")
            raise retrieval_work.WorkLimit("evidence_roots")
        return [event for row in rows
                if (event := self.event(row["event_id"], require_text=not reference))]

    def current(self, row):
        cached = self._snapshot_cache() if self.bounded else False
        key = encode(row)
        if cached and key in self._current:
            return self._current[key]
        try:
            self.reference(row)
        except retrieval_work.WorkLimit:
            raise
        except CoreError:
            return False
        now = datetime.fromisoformat(self.now) if self.now else None
        result = (now is None or (
            (not row["valid_from"] or datetime.fromisoformat(row["valid_from"]) <= now)
            and (not row["valid_to"] or now < datetime.fromisoformat(row["valid_to"]))))
        if cached:
            self._current[key] = result
        return result

    def reference(self, row):
        from companion import memory_references

        cached = self._snapshot_cache() if self.bounded else False
        if cached and row["origin"] != "event_observed" and self._references.get(
            row["memory_id"]) is False:
            return None
        key = encode(row)
        if cached and key in self._reference_values:
            return self._reference_values[key]
        value = memory_references.for_memory(self._memory, row, self.now, work=self.work)
        if cached:
            self._reference_values[key] = value
        return value


class MemoryContext:
    """Integration seam; callers still authorize context scope and review relevance."""

    def __init__(self, memory):
        self.memory, self.store = memory, memory.store

    def _begin(self, actor):
        self.memory.require_owner(actor)
        return read_version(self.memory)

    @staticmethod
    def _fact(row, sources):
        return {**{key: row[key] for key in MEMORY_FIELDS},
                "origin": row["origin"],
                "evidence_ids": [source["event_id"] for source in sources],
                "evidence": [{key: source[key] for key in (
            "event_id", "source", "occurred_at", "ingested_at",
        )} for source in sources[-8:]], "evidence_truncated": len(sources) > 8}

    def pin(self, now, *, rows=(), event_ids=(), historical=False, work=None, source_view=None):
        return self.store.read_or_write(lambda: self._pin(
            now, rows=rows, event_ids=event_ids, historical=historical,
            work=work, source_view=source_view))

    def _pin(self, now, *, rows, event_ids, historical, work, source_view):
        """Freeze full fact/evidence identity for a local await, never a display projection.

        The proof contains all positive roots and actual ancestors, including legacy
        correction sources. Callers persist consumed IDs in job_sources, not new edges
        between independent owner inputs. This transient proof does not grant access.
        """
        # Source-only pins are also used by independently authorized expression /
        # exploration consumers. They cannot read facts; Core checks their purpose.
        version = self._begin("owner") if rows else read_version(self.memory)
        if source_view is not None and (
            source_view.store is not self.store or not self.store.in_read_snapshot
            or source_view._cache_scope != self.store.frozen_scope or source_view.now != now
        ):
            raise RuntimeError("MEMORY_SOURCE_SNAPSHOT_REQUIRED")
        sources = source_view or MemorySources(self.memory, now, bounded=True, work=work)
        sources.prefetch([row["memory_id"] for row in rows])
        roots, memories = set(event_ids), []
        for expected in rows:
            row = sources._one('SELECT memories.memory_id AS memory_id, memories.subject AS '
                'subject, memories.kind AS kind, memories.content AS content, memories.status AS '
                'status, memories.origin AS origin, memories.confidence AS confidence, '
                'memories.importance AS importance, memories.privacy_scope AS privacy_scope, '
                'memories.supersedes_memory_id AS supersedes_memory_id, memories.created_at AS '
                'created_at, memories.updated_at AS updated_at, memories.valid_from AS valid_from, '
                'memories.valid_to AS valid_to, memories.last_reviewed_at AS last_reviewed_at, '
                'memories.fact_key AS fact_key, memories.needs_review AS needs_review FROM '
                'memories AS memories WHERE memories.memory_id = %s',
                                 (expected["memory_id"],))
            if (not row or row["privacy_scope"] != "private" or row["confidence"] <= 0
                or row["status"] not in (
                    {"CONFIRMED", "HYPOTHESIS", "SUPERSEDED"} if historical
                    else {"CONFIRMED", "HYPOTHESIS"}
                ) or (not historical and not sources.current(row))
                or any(row[key] != expected[key] for key in MEMORY_FIELDS)):
                raise CoreError("PRIVACY_CHANGED", 409)
            ids = sorted(item["event_id"] for item in sources.evidence(row["memory_id"]))
            if not ids or ids != sorted(expected["evidence_ids"]):
                raise CoreError("PRIVACY_CHANGED", 409)
            memories.append({**{key: row[key] for key in MEMORY_FIELDS}, "evidence_ids": ids})
            roots.update(ids)
            reference = sources.reference(row)
            if reference:
                roots.update(reference["proof"]["source_event_ids"])
        fingerprints = {}
        for event_id in sorted(roots):
            lineage = sources.lineage(event_id)
            if not lineage:
                raise CoreError("PRIVACY_CHANGED", 409)
            for source in lineage:
                material = {key: source[key] for key in (
                    "event_id", "source_event_id", "source", "event_type", "conversation_id",
                    "actor_id", "privacy_scope", "occurred_at", "ingested_at", "body_json",
                    "redacted", "scope", "owner_actor_id",
                )}
                fingerprints[source["event_id"]] = hashlib.sha256(
                    encode(material).encode(),
                ).hexdigest()
        check_version(self.memory, version)
        return {"rows": memories, "event_ids": sorted(event_ids), "historical": historical,
                "source_event_ids": sorted(fingerprints), "sources": fingerprints}

    def revalidate(self, proof, now, *, work=None):
        actual = self.pin(now, rows=proof["rows"], event_ids=proof["event_ids"],
                          historical=proof["historical"], work=work)
        if actual != proof:
            raise CoreError("PRIVACY_CHANGED", 409)

    def _limit(self):
        return getattr(self.memory.settings, "memory_recall_limit", 6)

    def recall(self, query, now, *, actor="owner", memory_ids=None, project_query=None, trace=None):
        """`memory_recall_limit` facts plus at most two unresolved conflicts; no model call.

        NO_RECORD means this eligible scope has no hit. Index/custody errors propagate
        rather than misrepresenting unavailable data as an empty personal history.
        """
        with retrieval_work.scope():
            return self._recall(query, now, actor=actor, memory_ids=memory_ids,
                                project_query=project_query, trace=trace)

    def _recall(self, query, now, *, actor, memory_ids, project_query, trace):
        version = self._begin(actor)
        if not isinstance(query, str) or not query.strip():
            raise CoreError("INVALID_MEMORY_QUERY", 400)
        rows = self.memory.search(query, actor=actor, memory_ids=memory_ids,
                                  limit=self._limit() + 2, now=now,
                                  trace=trace)
        return self._recall_result(rows, version, query, now, memory_ids, project_query, trace)

    async def recall_async(self, query, now, *, actor="owner", memory_ids=None,
                           project_query=None, source_check=None, clock=None, trace=None):
        with retrieval_work.scope():
            return await self._recall_async(query, now, actor=actor, memory_ids=memory_ids,
                project_query=project_query, source_check=source_check, clock=clock, trace=trace)

    async def _recall_async(self, query, now, *, actor, memory_ids, project_query,
                             source_check, clock, trace):
        version = self._begin(actor)
        if not isinstance(query, str) or not query.strip():
            raise CoreError("INVALID_MEMORY_QUERY", 400)
        rows = await self.memory.search_async(
            query, actor=actor, memory_ids=memory_ids, limit=self._limit() + 2, now=now,
            source_check=source_check, clock=clock, trace=trace,
        )
        return self._recall_result(rows, version, query, clock() if clock else now,
                                   memory_ids, project_query, trace)

    def _recall_result(self, rows, version, query, now, memory_ids, project_query, trace=None):
        if not rows and project_query is not None:
            rows = self.memory.project_recall_candidates(
                project_query, memory_ids=memory_ids, limit=6, now=now,
            )
        work = retrieval_work.current()
        sources = MemorySources(self.memory, now, bounded=True, work=work)
        facts, conflicts = [], []
        for row in rows:
            try:
                evidence = sources.evidence(row["memory_id"])
            except retrieval_work.WorkLimit as error:
                work.mark(error.reason)
                break
            if not evidence:
                continue
            item = self._fact(row, evidence)
            from companion import memory_references

            try:
                reference = memory_references.for_memory(self.memory, row, now, work=work)
            except retrieval_work.WorkLimit as error:
                work.mark(error.reason)
                break
            if reference:
                item["reference"] = {"kind": reference["kind"],
                    "reference_id": reference["reference_id"],
                    "source_event_ids": reference["proof"]["source_event_ids"],
                    "owner_confirmed": row["origin"] == "user_confirmed"}
            (conflicts if row["needs_review"] else facts).append(item)
        check_version(self.memory, version)
        facts = facts[:self._limit()]
        selected = {row["memory_id"] for row in [*facts, *conflicts[:2]]}
        if trace is not None:
            ranks = trace["selected"]
            trace.update({"search_selected_count": len(ranks),
                "conflicts_truncated": len(conflicts) > 2,
                "selected": {mid: ranks.get(mid, {"project_fallback": True})
                             for mid in sorted(selected)}})
            trace["work"] = work.receipt()
        coverage = work.receipt()
        return {"state": "FOUND" if facts else "NEEDS_REVIEW" if conflicts else (
                    "COVERAGE_LIMITED" if coverage["coverage_limited"] else "NO_RECORD"),
                **({"retrieval_coverage": coverage, "note": retrieval_work.NOTE}
                   if coverage["coverage_limited"] else {}),
                "privacy_epoch": version[0], "as_of": now, "facts": facts,
                "conflicts": conflicts[:2], "conflicts_truncated": len(conflicts) > 2,
                "rows": [row for row in rows if row["memory_id"] in selected],
                "requires_relevance_review": bool(rows)}

    def continuation(self, conversation_id, now, *, actor="owner", event_ids=None,
                     current_event_ids=(), used_memory_ids=(), source_check=None):
        with retrieval_work.scope() as work:
            try:
                return self._continuation(conversation_id, now, actor=actor, event_ids=event_ids,
                    current_event_ids=current_event_ids, used_memory_ids=used_memory_ids,
                    source_check=source_check)
            except retrieval_work.WorkLimit as error:
                work.mark(error.reason)
                return {"privacy_epoch": epoch(self.store), "as_of": now, "items": [],
                        "truncated": True, "retrieval_coverage": work.receipt(),
                        "note": retrieval_work.NOTE}

    def _continuation(self, conversation_id, now, *, actor, event_ids,
                      current_event_ids, used_memory_ids, source_check):
        """Three Hot/Warm episode excerpts within the caller's authorized source scope.

        These are attributed observations, never current facts or proof of a sent reply.
        Context integration must explicitly opt in with its authorized conversation/sources.
        """
        from companion.diary import is_management_command
        from companion.memory_projections import MemoryProjections, identifiers

        version = self._begin(actor)
        work = retrieval_work.current()
        sources = MemorySources(self.memory, now, bounded=True, work=work)
        allowed = set(event_ids) if event_ids is not None else None
        current = set(identifiers(current_event_ids))
        used = identifiers(used_memory_ids)
        recalled = set()
        for memory in self.store.rows(
            'SELECT memories.memory_id AS memory_id, memories.subject AS subject, memories.kind AS '
            'kind, memories.content AS content, memories.status AS status, memories.origin AS '
            'origin, memories.confidence AS confidence, memories.importance AS importance, '
            'memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS '
            'supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS '
            'updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, '
            'memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, '
            'memories.needs_review AS needs_review FROM memories AS memories WHERE '
            'memories.memory_id IN (SELECT parsed.value COLLATE "C" AS _col_0 FROM '
            'JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) AND memories.status IN '
            "('CONFIRMED', 'HYPOTHESIS') AND memories.confidence > 0 AND memories.privacy_scope = "
            "'private'", (encode(used),),
        ):
            if sources.current(memory):
                recalled.update(item["event_id"] for item in sources.evidence(memory["memory_id"]))
        projection = MemoryProjections(self.memory, work=work)
        rows, truncated = projection.continuation_candidates(
            conversation_id, now, current | recalled,
        )
        items = []
        for row in rows:
            ids = [link["event_id"] for link in self.store.rows(
                'SELECT event_id FROM episode_events WHERE episode_id = %s ORDER BY event_id NULLS '
                'FIRST LIMIT 21',
                (row["episode_id"],),
            )]
            if not 1 <= len(ids) <= 20 or (allowed is not None and not set(ids) <= allowed):
                continue
            events = [sources.event(key, require_text=False) for key in ids]
            if any(event is None or event["conversation_id"] != conversation_id
                   for event in events):
                continue
            if source_check is not None and not all(source_check(key) for key in ids):
                continue
            goals, _ = projection._goals(ids, sources, allowed)
            temperature, reasons = projection._temperature(
                events, goals, current, now, bool(set(ids) & recalled),
            )
            if temperature == "Cold":
                continue
            excerpts = []
            for event in sorted(events, key=lambda e: (
                e["occurred_at"], e["ingested_at"], e["event_id"],
            )):
                body = json.loads(event["body_json"])
                text = body["text"]
                if (event["source"] != "qq.onebot" or event["event_type"] not in {
                        "message.received", "message.media_received"}
                    or not text.strip()
                    or body.get("control_command") or body.get("diary_command")
                    or is_management_command(text) or sensitive(text)
                    or redact(text, self.memory.settings) != text):
                    continue
                excerpts.append({"event_id": event["event_id"],
                                 "occurred_at": event["occurred_at"], "excerpt": text[:200]})
            if excerpts:
                item = {"episode_id": row["episode_id"], "kind": "owner_observation",
                        "temperature": temperature, "attention_reasons": reasons,
                        "source_event_ids": ids,
                        "excerpts": excerpts[-3:], "truncated": len(excerpts) > 3}
                summary = self.memory.episode_summary(row["episode_id"], ids, now)
                if summary is not None:
                    item["summary"] = summary
                items.append(item)
        items.sort(key=lambda item: item["temperature"] != "Hot")
        check_version(self.memory, version)
        return {"privacy_epoch": version[0], "as_of": now, "items": items[:3],
                "truncated": truncated or len(items) > 3}

    def history(self, memory_id, now, *, actor="owner", memory_ids=None):
        """Owner-inspectable revision chain, including superseded facts with live support.

        Recorded/corrected timestamps are not guessed real-world validity dates.
        A supplied allowlist bounds every member, not just the initially selected ID.
        """
        with retrieval_work.scope():
            return self._history(memory_id, now, actor=actor, memory_ids=memory_ids)

    def _history(self, memory_id, now, *, actor, memory_ids):
        version = self._begin(actor)
        if memory_ids is not None and memory_id not in memory_ids:
            return {"privacy_epoch": version[0], "as_of": now,
                    "revisions": [], "truncated": False}
        # The native replacement edge establishes the attribute relation even if
        # historical inference used different key spellings for that attribute.
        work = retrieval_work.current()

        def nominate(sql, params):
            try:
                return work.rows(self.store, sql, params, limit=21)
            except retrieval_work.WorkLimit:
                return []

        rows = nominate(
            "WITH RECURSIVE relations(older,newer) AS ("
            "SELECT supersedes_memory_id,memory_id FROM memories "
            "WHERE supersedes_memory_id IS NOT NULL UNION "
            'SELECT a.entity_id,(a.safe_detail_json::json->>\'by\') COLLATE "C" FROM audit_log a '
            "JOIN memories old ON old.memory_id=a.entity_id "
            "JOIN memories new ON new.memory_id=(a.safe_detail_json::json->>'by') "
            "WHERE a.action='memory.replaced' AND a.created_at<=%s "
            "AND old.subject=new.subject "
            "AND old.kind<>'episode' AND new.kind<>'episode'), "
            "edges(source,target) AS (SELECT older,newer FROM relations UNION "
            "SELECT newer,older FROM relations), "
            'family(memory_id) AS (SELECT %s::text COLLATE "C" UNION '
            "SELECT e.target FROM edges e JOIN family f ON e.source=f.memory_id), "
            "descendants(root,node) AS (SELECT memory_id,memory_id FROM family UNION "
            "SELECT d.root,e.newer FROM descendants d JOIN relations e ON e.older=d.node) "
            "SELECT m.memory_id,octet_length(m.content) AS text_bytes "
            "FROM memories m JOIN family f USING(memory_id) "
            "JOIN (SELECT root,COUNT(*) AS later_count FROM descendants GROUP BY root) rank "
            "ON rank.root=m.memory_id "
            "WHERE m.privacy_scope='private' AND m.confidence>0 "
            "AND m.status IN ('CONFIRMED','HYPOTHESIS','SUPERSEDED') "
            "ORDER BY rank.later_count,m.created_at DESC,m.memory_id LIMIT 21", (now, memory_id),
        )
        sources = MemorySources(self.memory, now, bounded=True, work=work)
        items = []
        for header in rows[:20]:
            if memory_ids is not None and header["memory_id"] not in memory_ids:
                continue
            if not work.take("candidates") or not work.take("text_bytes", header["text_bytes"]):
                break
            row = self.store.one('SELECT memories.memory_id AS memory_id, memories.subject AS '
                'subject, memories.kind AS kind, memories.content AS content, memories.status AS '
                'status, memories.origin AS origin, memories.confidence AS confidence, '
                'memories.importance AS importance, memories.privacy_scope AS privacy_scope, '
                'memories.supersedes_memory_id AS supersedes_memory_id, memories.created_at AS '
                'created_at, memories.updated_at AS updated_at, memories.valid_from AS valid_from, '
                'memories.valid_to AS valid_to, memories.last_reviewed_at AS last_reviewed_at, '
                'memories.fact_key AS fact_key, memories.needs_review AS needs_review FROM '
                'memories AS memories WHERE memories.memory_id = %s AND '
                "OCTET_LENGTH(memories.content) = %s AND memories.privacy_scope = 'private' AND "
                "memories.confidence > 0 AND memories.status IN ('CONFIRMED', 'HYPOTHESIS', "
                "'SUPERSEDED')",
                                 (header["memory_id"], header["text_bytes"]))
            try:
                if row and (evidence := sources.evidence(row["memory_id"])):
                    items.append(self._fact(row, evidence))
            except retrieval_work.WorkLimit as error:
                work.mark(error.reason)
                break
        check_version(self.memory, version)
        coverage = work.receipt()
        return {"privacy_epoch": version[0], "as_of": now, "revisions": items,
                "truncated": len(rows) > 20 or coverage["coverage_limited"],
                **({"retrieval_coverage": coverage, "note": retrieval_work.NOTE}
                   if coverage["coverage_limited"] else {})}

    def invalidation_plan(self, event_ids, now, *, actor="owner"):
        """Read-only withdrawal plan; integration owns the Ledger-first mutation/replay.

        Recompute inside that transaction. In particular, event_batch currently erases
        every supported fact and is NOT an implementation of this plan's retain branch.
        """
        version = self._begin(actor)
        if (not isinstance(event_ids, (list, tuple, set)) or not 1 <= len(event_ids) <= 256
            or any(not isinstance(key, str) or not key for key in event_ids)):
            raise CoreError("INVALID_MEMORY_SOURCES", 400)
        # Includes legacy correction copies whose explicit links predate event_dependencies.
        affected = self.memory.ledger.retention_events(self.store, event_ids)
        sources = MemorySources(self.memory, now, excluded=affected)
        rows = self.store.rows(
            'SELECT DISTINCT m.memory_id FROM memories AS m JOIN memory_evidence AS v USING '
            '(memory_id) WHERE v.event_id IN (SELECT value COLLATE "C" FROM '
            'JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) AND m.status IN '
            "('CONFIRMED', 'HYPOTHESIS', 'SUPERSEDED')", (encode(sorted(affected)),),
        )
        retained, withdrawn = [], []
        for row in rows:
            if evidence := sources.evidence(row["memory_id"]):
                retained.append({"memory_id": row["memory_id"],
                                 "evidence_ids": [item["event_id"] for item in evidence]})
            else:
                withdrawn.append(row["memory_id"])
        episodes = self.store.rows(
            'SELECT DISTINCT episode_id FROM episode_events WHERE event_id IN (SELECT value '
            'COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) ORDER '
            'BY episode_id NULLS FIRST',
            (encode(sorted(affected)),),
        )
        check_version(self.memory, version)
        return {"privacy_epoch": version[0], "source_event_ids": sorted(affected),
                "retain": retained, "withdraw": sorted(withdrawn),
                "recompute_episode_ids": [row["episode_id"] for row in episodes]}
