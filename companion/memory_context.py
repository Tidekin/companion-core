"""Bounded, source-checked views over the existing Memory store; no second memory store."""

import hashlib
import json
import re
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


def read_version(memory, *, source_view=None):
    # Reuse the same Ledger/DB custody check as the real private context boundary.
    from companion.live_text import ERASURE_PROJECTION_SQL, _ledger_custody
    from companion.postgres import ERASURE_LEDGER

    store = memory.store
    if source_view is not None and source_view.store is not store:
        raise RuntimeError("MEMORY_SOURCE_SNAPSHOT_REQUIRED")
    if not store.in_transaction:
        return store.read_or_write(lambda: read_version(memory, source_view=source_view))
    store.ledger_lock(ERASURE_LEDGER, shared=True)
    cached = source_view is not None and source_view._snapshot_cache()
    metadata = source_view._custody if cached else {}
    if not metadata:
        projection = store.rows(
            "SELECT erasure.*,(SELECT value FROM runtime_meta WHERE key='privacy_epoch') "
            "AS privacy_epoch FROM (" + ERASURE_PROJECTION_SQL + ") erasure")
        value = projection[0]["privacy_epoch"]
        metadata.update(epoch=int(value) if value is not None else 0, projection=projection)
    # Only immutable DB rows are reused. Re-read and validate external bytes each time.
    digest = _ledger_custody(store, memory.settings, projection=metadata["projection"])
    return metadata["epoch"], digest


def check_version(memory, version, *, source_view=None):
    if read_version(memory, source_view=source_view) != version:
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
        self._facts, self._fact_blocked = {}, {}
        self._custody = {}
        self._source_end = None
        self.cutoff = (datetime.fromisoformat(after(
            now, -86400 * self.settings.raw_event_retention_days,
        )) if now else None)

    def _snapshot_cache(self):
        scope = self.store.frozen_scope
        if scope is None or scope != self._cache_scope:
            for cache in (self._loaded, self._edges, self._evidence,
                          self._references, self._current, self._blocked, self._reference_values,
                          self._facts, self._fact_blocked, self._custody):
                cache.clear()
            self._cache_scope = scope
            self._source_end = None
        return scope is not None

    def prefetch(self, memory_ids, *, max_bytes=None):
        """Batch proof roots and bodies only inside the current immutable DB snapshot."""
        if not self.bounded or not self._snapshot_cache() or not memory_ids:
            return
        if max_bytes is not None:
            self._source_end = min(self._source_end if self._source_end is not None
                                   else float("inf"),
                                   self._source_bytes + max_bytes)
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

    def _load_events(self, keys, *, loaded=None, edges=None, blocked=None):
        """Read finite source bodies in a batch after admitting their measured sizes."""
        from companion.retrieval_work import LIMITS, SOURCE_NODES, WorkLimit

        loaded = self._loaded if loaded is None else loaded
        edges = self._edges if edges is None else edges
        blocked = self._blocked if blocked is None else blocked
        keys = [key for key in keys if key not in loaded and key not in blocked]
        if not keys:
            return
        headers = self._rows("SELECT s.event_id,COALESCE(octet_length(e.body_json),0) AS size "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS s(event_id,position) "
            "LEFT JOIN events e USING(event_id) ORDER BY s.position",
            (keys[:SOURCE_NODES],), limit=SOURCE_NODES)
        for key in keys[SOURCE_NODES:]:
            blocked[key] = "source_nodes"
            self.work.mark("source_nodes")
        admitted = []
        for header in headers:
            key, size = header["event_id"], header["size"]
            if len(self._source_ids | {key}) > SOURCE_NODES or not self.work.take("source_nodes"):
                blocked[key] = "source_nodes"
            elif (self._source_bytes + size > min(
                    LIMITS["text_bytes"], self._source_end if self._source_end is not None
                    else LIMITS["text_bytes"])
                  or not self.work.take("text_bytes", size)):
                blocked[key] = "source_text_bytes"
            else:
                self._source_ids.add(key)
                self._source_bytes += size
                admitted.append((key, size))
            if key in blocked:
                self.work.mark(blocked[key])
        if admitted:
            rows = self._rows("SELECT s.event_id AS source_id,"
                + ",".join("e." + field for field in EVENT_COLUMNS)
                + ",c.scope,c.owner_actor_id,r.key AS erased,ARRAY(SELECT d.source_event_id "
                "FROM event_dependencies d WHERE d.event_id=s.event_id LIMIT "
                + str(SOURCE_NODES + 1) + ") AS source_edges "
                "FROM unnest(%s::text[],%s::bigint[]) s(event_id,text_bytes) "
                "LEFT JOIN events e ON e.event_id=s.event_id "
                "AND COALESCE(octet_length(e.body_json),0)=s.text_bytes "
                "LEFT JOIN conversations c USING(conversation_id) "
                "LEFT JOIN runtime_meta r ON r.key='erased_event:'||s.event_id",
                ([key for key, _ in admitted], [size for _, size in admitted]), limit=len(admitted))
            loaded.update((row["source_id"], row) for row in rows)
            edges.update((row["source_id"], [{"source_event_id": key}
                for key in row["source_edges"]]) for row in rows)
        for key in keys:
            if key not in loaded and key not in blocked:
                raise WorkLimit("source_nodes")

    def prefetch_facts(self, expected):
        """Re-read finite fact bodies once, retaining only this snapshot's values."""
        if not self.bounded or not self._snapshot_cache():
            return
        admitted = []
        for row in expected:
            key = row["memory_id"]
            if key in self._facts or key in self._fact_blocked:
                continue
            size = len(row["content"].encode())
            if self.work.take("text_bytes", size):
                admitted.append((key, size))
            else:
                self._fact_blocked[key] = "text_bytes"
        if admitted:
            rows = self._rows("SELECT " + ",".join("m." + key for key in MEMORY_COLUMNS)
                + " FROM memories m JOIN unnest(%s::text[],%s::bigint[]) "
                "AS expected(memory_id,text_bytes) USING(memory_id) "
                "WHERE octet_length(m.content)=expected.text_bytes",
                ([key for key, _ in admitted], [size for _, size in admitted]),
                limit=len(admitted))
            self._facts.update((key, None) for key, _ in admitted)
            self._facts.update((row["memory_id"], row) for row in rows)

    def fact(self, expected):
        if self.bounded and self._snapshot_cache():
            self.prefetch_facts([expected])
            if reason := self._fact_blocked.get(expected["memory_id"]):
                raise retrieval_work.WorkLimit(reason)
            return self._facts.get(expected["memory_id"])
        return self._one("SELECT " + ",".join(MEMORY_COLUMNS)
                         + " FROM memories WHERE memory_id=%s", (expected["memory_id"],))

    def _rows(self, statement, parameters=(), *, limit):
        if self.bounded:
            return self.work.source_rows(self.store, statement, parameters, limit=limit)
        return self.store.rows(statement, parameters)

    def _one(self, statement, parameters=()):
        rows = self._rows(statement, parameters, limit=1)
        return rows[0] if rows else None

    def _lineage(self, event_id):
        return self._lineages([event_id])

    def _lineages(self, event_ids):
        """Follow native edges and the exact legacy correction relation used by erasure."""
        if self.bounded:
            def read():
                try:
                    return self._bounded_lineages(event_ids)
                except retrieval_work.WorkLimit as error:
                    return error  # A work limit is not a snapshot-write retry.

            rows = self.store.read_or_write(read)
            if isinstance(rows, retrieval_work.WorkLimit):
                raise rows
            return rows
        lineage = ('SELECT value COLLATE "C" FROM json_array_elements_text(%s::json) AS ids(value) '
                   "UNION SELECT d.source_event_id FROM event_dependencies d "
                   "JOIN sources s ON d.event_id=s.event_id")
        pending, found = set(event_ids), {}
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

    def _bounded_lineages(self, event_ids):
        """Read complete indexed adjacency, or refuse the candidate; never cut its proof."""
        from companion.retrieval_work import EVIDENCE_ROOTS, LIMITS, SOURCE_NODES, WorkLimit

        def refuse(reason):
            if self.work:
                self.work.mark(reason)
            raise WorkLimit(reason)

        cached = self._snapshot_cache()
        pending, found = dict.fromkeys(event_ids), {}
        loaded = self._loaded if cached else {}
        local_edges, blocked = ({}, {}) if not cached else (self._edges, self._blocked)

        def read(key):
            if key in blocked:
                refuse(blocked[key])
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
            remaining = LIMITS["text_bytes"] - self._source_bytes
            if self.work:
                remaining = min(remaining, self.work.remaining("text_bytes"))
            row = self._one(
                'SELECT s.event_id AS source_id, e.event_id AS event_id, e.schema_version AS '
                'schema_version, e.source AS source, e.source_event_id AS source_event_id, '
                'e.conversation_id AS conversation_id, e.actor_id AS actor_id, e.event_type AS '
                'event_type, e.occurred_at AS occurred_at, e.ingested_at AS ingested_at, '
                'e.privacy_scope AS privacy_scope, OCTET_LENGTH(e.body_json) AS source_size, '
                'CASE WHEN OCTET_LENGTH(e.body_json)<=%s THEN e.body_json END AS body_json, '
                'e.trace_id AS '
                'trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS fingerprint, '
                'e.redacted AS redacted, e.memory_processed AS memory_processed, e.reply_revision '
                'AS reply_revision, c.scope AS scope, c.owner_actor_id AS owner_actor_id, r.key AS '
                'erased, ARRAY(SELECT d.source_event_id FROM event_dependencies d '
                'WHERE d.event_id=s.event_id LIMIT ' + str(SOURCE_NODES + 1) + ') AS source_edges '
                'FROM (SELECT %s AS event_id) AS s LEFT JOIN events AS e ON s.event_id = '
                'e.event_id LEFT JOIN conversations AS c ON e.conversation_id = c.conversation_id '
                "LEFT JOIN runtime_meta AS r ON r.key = 'erased_event:' || s.event_id",
                (remaining, key))
            size = row.pop("source_size") or 0
            if size > remaining or (self.work and not self.work.take("text_bytes", size)):
                refuse("source_text_bytes")
            self._source_bytes += size
            local_edges[key] = [{"source_event_id": value} for value in row.pop("source_edges")]
            loaded[key] = row
            return row

        while pending:
            fresh = [key for key in pending if key not in loaded and key not in blocked]
            if len(fresh) > 1:
                self._load_events(fresh, loaded=loaded, edges=local_edges, blocked=blocked)
            key, _ = pending.popitem()
            if key in found:
                continue
            row = read(key)
            found[key] = row
            edges = self._edges.get(key) if cached else local_edges.get(key)
            if edges is None:
                edges = self._rows('SELECT source_event_id FROM event_dependencies WHERE '
                    'event_id = %s LIMIT %s', (key, SOURCE_NODES + 1), limit=SOURCE_NODES + 1)
                if cached:
                    self._edges[key] = edges
            if len(edges) > SOURCE_NODES:
                refuse("source_nodes")
            pending.update((edge["source_event_id"], None) for edge in edges
                           if edge["source_event_id"] not in found)
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
                        pending[source] = None
        return found.values()

    def lineage(self, event_id):
        return self._checked_lineage(self._lineage(event_id))

    def lineages(self, event_ids):
        # Preserve specialized per-root scopes such as ConversationSources.
        if type(self)._lineage is not MemorySources._lineage:
            found = {}
            for event_id in event_ids:
                rows = self.lineage(event_id)
                if not rows:
                    return []
                found.update((row["source_id"], row) for row in rows)
            return list(found.values())
        return self._checked_lineage(self._lineages(event_ids))

    def _checked_lineage(self, values):
        """Live private provenance for owner facts and non-owner reference sources."""
        rows = list(values)
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

    def reply_source_check(self, settings, clock, authority):
        """Return a purpose-specific lineage check for this one prompt build."""
        from companion.memory_authority import sources_allowed

        source_view = MemorySources(self.memory, clock(), bounded=True)

        def available(ids, purpose):
            ids = tuple(dict.fromkeys(ids))
            if not ids:
                return True

            def check():
                try:
                    lineage = source_view.lineages(ids)
                except retrieval_work.WorkLimit as error:
                    if (work := retrieval_work.current()) is not None:
                        work.mark(error.reason)
                    return False
                # Group synchronous reads in one existing native snapshot. Only
                # SourceView's DB rows are reusable; each permission/file check is live.
                return bool(lineage) and (not authority or sources_allowed(
                    self.store, settings, clock,
                    (row["event_id"] for row in lineage if row["source"] == "qq.onebot"), purpose))

            return self.store.read_or_write(check)
        return available

    def add_reply_facts(self, selection, facts, settings, clock, *, chat, paused,
                        query="", allowed_ids=None):
        from companion import context_selection
        from companion.model import MEMORY_HEADING, RELATIONSHIP_HEADING

        hits, relations, standing, memory_proof = facts
        memory_references = []
        if standing:
            selection.add(RELATIONSHIP_HEADING,
                {"memories": [{k: row[k] for k in (
                    "subject", "kind", "content", "status", "needs_review", "origin",
                    "evidence_ids")}
                    for row in standing]},
                {event for row in standing for event in row["evidence_ids"]},
                priority=context_selection.TOPIC, required=True,
                proofs=[{"kind": "memory", "proof": self.pin(
                    clock(), rows=standing)}], module="memory")
        for rank, h in enumerate(hits):
            memory = {**{k: h[k] for k in (
                "memory_id", "subject", "kind", "content", "status", "needs_review",
                "evidence_ids")},
                **({"origin": h["origin"]} if h.get("origin") else {}),
                **relations.get(h["memory_id"], {}),
                **({("source_message_on" if h.get("origin") == "event_observed"
                     else "happened_on"): happened}
                   if chat and settings.chat_time_labels
                   and (happened := self._memory_happened_on(h, settings.timezone)) else {})}
            reference = selection.add(MEMORY_HEADING,
                {"memories": [memory], "self_observations": [], "memory_status": "available"},
                h["evidence_ids"], priority=context_selection.RELATED,
                required=rank < settings.memory_required_hits,
                proofs=[{"kind": "memory", "proof": memory_proof}], module="memory",
                fact=(h["subject"], memory["content"]))
            memory_references.append((reference, h))
        if chat and re.search(r"最初|最早|原来|之前|更正|改过|变更|顺序|依次|版本|"
                              r"截至|当时|当天|当前|现在|"
                              r"\b(?:earliest|previous|as of|revisions)\b", query, re.I):
            seen = set()
            for h in hits[:2]:
                if h["memory_id"] in seen:
                    continue
                chain = self.history(h["memory_id"], clock(), memory_ids=allowed_ids)
                revisions = chain["revisions"]
                seen.update(row["memory_id"] for row in revisions)
                if len(revisions) < 2 and not chain["truncated"]:
                    continue
                proof = self.pin(clock(), rows=revisions, historical=True)
                selection.add(MEMORY_HEADING, {"memory_history": chain},
                    proof["source_event_ids"], priority=context_selection.RELATED,
                    required=True, proofs=[{"kind": "memory", "proof": proof}], module="memory")
        if paused:
            selection.add(MEMORY_HEADING,
                {"memories": [], "self_observations": [],
                 "memory_status": "核对暂不可用，不能断言没有历史记录"}, (), required=True)
        return memory_references

    def _memory_happened_on(self, row, zone):
        """Source event dates of a memory, not its later extraction date."""
        from companion import time_context

        ids = row.get("evidence_ids") or []
        if not ids:
            return time_context.day(row["created_at"], zone) if row.get("created_at") else None
        days = sorted({time_context.day(item["occurred_at"], zone) for item in self.store.rows(
            "SELECT occurred_at FROM events WHERE event_id IN (" + ",".join('%s' for _ in ids)
            + ") AND occurred_at IS NOT NULL ORDER BY occurred_at", tuple(ids))},
            key=lambda value: [int(part) for part in value.replace("年", "-").replace(
                "月", "-").rstrip("日").split("-")])
        if not days:
            return None
        return days[0] if len(days) == 1 else days[0] + "至" + days[-1]

    def reply_facts(self, settings, clock, text, memories, *, allowed_ids, include_standing):
        """Select reply facts and pin the recalled rows after removing standing duplicates."""
        if memories is None:
            memories = self.recall(text or "*", clock(), memory_ids=allowed_ids,
                                   project_query=text)["rows"]
        hits, relations = self.memory.with_relayed_updates(memories, clock())
        standing = self.memory.standing_relationship(
            limit=settings.relationship_standing_limit,
            max_chars=settings.relationship_standing_chars, now=clock(),
        ) if include_standing else []
        standing_ids = {row["memory_id"] for row in standing}
        hits = [row for row in hits if row["memory_id"] not in standing_ids]

        def pinned_display():
            from companion import memory_references

            now = clock()
            sources = MemorySources(self.memory, now, bounded=True,
                                    work=retrieval_work.current())
            # Pin and display share immutable source material in this synchronous read.
            shared = sources if sources._snapshot_cache() else None
            proof = self.pin(now, rows=hits, work=sources.work, source_view=shared)
            overlays = dict(relations)
            for row in hits:
                if row["origin"] != "event_observed":
                    continue
                current = sources.fact(row)
                if not current or any(current[key] != row[key] for key in MEMORY_FIELDS):
                    raise CoreError("PRIVACY_CHANGED", 409)
                if reference := sources.reference(current):
                    overlays[row["memory_id"]] = {
                        **overlays.get(row["memory_id"], {}),
                        "content": memory_references.display_content(row, reference)}
            return hits, overlays, standing, proof

        def read():
            try:
                return pinned_display()
            except retrieval_work.WorkLimit as error:
                return error  # Optional exhaustion cannot trigger a write/fresh-budget retry.

        result = self.store.read_or_write(read)
        if isinstance(result, retrieval_work.WorkLimit):
            if (work := retrieval_work.current()) is not None:
                work.mark(result.reason)
                work.mark("final_evidence_unavailable")
            # No selected claim is usable without its final proof. Keep normal standing
            # facts on their existing validation path and expose limited coverage above.
            return [], {}, standing, {"rows": [], "event_ids": [], "historical": False,
                                      "source_event_ids": [], "sources": {}}
        return result

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
        if rows:
            self.memory.require_owner("owner")
        if source_view is not None and (
            source_view.store is not self.store or not self.store.in_read_snapshot
            or source_view._cache_scope != self.store.frozen_scope or source_view.now != now
        ):
            raise RuntimeError("MEMORY_SOURCE_SNAPSHOT_REQUIRED")
        sources = source_view or MemorySources(self.memory, now, bounded=True, work=work)
        version = read_version(self.memory, source_view=sources)
        sources.prefetch_facts(rows)
        sources.prefetch([row["memory_id"] for row in rows])
        roots, memories = set(event_ids), []
        for expected in rows:
            row = sources.fact(expected)
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
        lineage = sources.lineages(sorted(roots)) if roots else []
        if roots and not lineage:
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
        check_version(self.memory, version, source_view=sources)
        return {"rows": memories, "event_ids": sorted(event_ids), "historical": historical,
                "source_event_ids": sorted(fingerprints), "sources": fingerprints}

    def revalidate(self, proof, now, *, work=None, source_view=None):
        actual = self.pin(now, rows=proof["rows"], event_ids=proof["event_ids"],
                          historical=proof["historical"], work=work, source_view=source_view)
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
        return await self.memory.search_async(
            query, actor=actor, memory_ids=memory_ids, limit=self._limit() + 2, now=now,
            source_check=source_check, clock=clock, trace=trace,
            _consume=lambda rows, sources: self._recall_result(
                rows, version, query, sources.now, memory_ids, project_query, trace,
                source_view=sources),
        )

    def _recall_result(self, rows, version, query, now, memory_ids, project_query, trace=None, *,
                       source_view=None):
        if not self.store.in_transaction:
            return self.store.read_or_write(lambda: self._recall_result(
                rows, version, query, now, memory_ids, project_query, trace))
        if not rows and project_query is not None:
            rows = self.memory.project_recall_candidates(
                project_query, memory_ids=memory_ids, limit=6, now=now,
            )
        work = retrieval_work.current()
        sources = source_view or MemorySources(self.memory, now, bounded=True, work=work)
        try:
            sources.prefetch([row["memory_id"] for row in rows])
        except retrieval_work.WorkLimit as error:
            work.mark(error.reason)
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
            try:
                reference = sources.reference(row)
            except retrieval_work.WorkLimit as error:
                work.mark(error.reason)
                break
            if reference:
                from companion import memory_references

                item["content"] = memory_references.display_content(row, reference)
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

    def reply_continuation(self, settings, clock, now, authority, job, history, used_memory_ids, *,
                           reserved_ids, reserved_bytes):
        from companion import notices, owner_input, private_life, reply_revision

        s, cfg = self.store, settings
        if authority and not {"memory", "history"}.issubset(authority.policy.purposes):
            return None
        followup = (notices.reference(job) or {}).get("kind") == "followup"
        if (not cfg.memory_enabled or not (followup or job["kind"] == "chat"
            and reply_revision.owner_input(s, job["event_id"]))
            or reply_revision.control_job(s, job)):
            return None
        if authority:
            allowed = None
        elif private_life.strict(s, cfg):
            if cfg.live_text_window.history_scope != "approved_window":
                return None
            # These are already individually fingerprinted by the window authorizer.
            allowed = {row["event_id"] for row in history}
        else:
            if private_life.enabled(cfg):
                private_life.check_job(s, cfg, cfg.live_text_window, job)
                if cfg.live_text_window.history_scope != "owner_private":
                    return None
            allowed = None  # Check nominated sources individually below.

        def collect():
            source_check = finish = None
            if not authority and allowed is None:
                def source_check(event_id):
                    return bool(s.one(
                        (
                            'SELECT 1 FROM events e JOIN conversations c '
                            'USING(conversation_id) WHERE e.event_id=%s AND '
                            "e.conversation_id=%s AND e.actor_id='owner' AND "
                            "e.source='qq.onebot' AND e.privacy_scope='private' AND"
                            " c.scope='private' AND c.owner_actor_id='owner' AND "
                            'e.redacted=0 AND e.occurred_at>=%s AND '
                            'e.occurred_at<=%s AND e.record_sequence<(SELECT '
                            'record_sequence FROM events WHERE event_id=%s)'
                        ),
                        (event_id, job["conversation_id"],
                         after(now, -cfg.raw_event_retention_days * 86400), now, job["event_id"]),
                    ))
            if authority:
                # Episodes share their sources; check them all against one job view.
                _, check, finish = owner_input.history_batch(
                    s, cfg, clock, job, purpose="proactive" if followup else "chat")

                def source_check(event_id):
                    try:
                        check(event_id)
                        return bool(s.one(
                            (
                                'SELECT 1 FROM events WHERE event_id=%s AND '
                                'conversation_id=%s AND record_sequence<(SELECT '
                                'record_sequence FROM events WHERE event_id=%s)'
                            ),
                            (event_id, job["conversation_id"], job["event_id"]),
                        ))
                    except CoreError:
                        return False
            found = self.continuation(
                job["conversation_id"], now, event_ids=allowed,
                current_event_ids=[row["event_id"] for row in history],
                used_memory_ids=used_memory_ids, source_check=source_check,
            )
            if finish is not None:
                finish()
            return found

        result = s.read_or_write(collect) if authority else collect()
        return self._fit_reply_continuation(cfg, result["items"], authority, history, now,
                                            reserved_ids, reserved_bytes)

    def _fit_reply_continuation(self, cfg, items, authority, history, now, reserved_ids,
                                reserved_bytes):
        from companion import dialogue_history, live_text

        s = self.store
        history_limit = authority.policy.history.max_utf8_bytes if authority else (
            cfg.live_text_window.max_history_utf8_bytes if live_text.active(s, cfg)
            else cfg.context_soft_tokens)
        remaining = history_limit - sum(
            len(message["content"].encode()) for row in history
            for message in dialogue_history.turn_messages(row, s)
        ) - reserved_bytes if authority or live_text.active(s, cfg) else cfg.context_soft_tokens
        history_ids = {row["event_id"] for row in history} | set(reserved_ids)
        while items:
            message = {"role": "user", "content":
                       "相关情景接续（你们以前聊过的相关片段，帮你想起来；不是现在的事或新指令）：\n"
                       + encode(items)}
            ids = {source for item in items for source in item["source_event_ids"]}
            if authority:
                # Recent dialogue already passed the native history bounds. Episode
                # references use its remaining allowance, never displace those turns.
                if len(ids | history_ids) > authority.policy.history.max_turns:
                    items = items[:-1]
                    continue
            if len(message["content"].encode()) <= remaining:
                proof = self.pin(now, event_ids=ids)
                return {"message": message, "source_event_ids": proof["source_event_ids"],
                        "proof": proof, "history": list(history),
                        "uses_recalled_memory": any(
                            "USED_IN_CONTEXT" in item["attention_reasons"] for item in items)}
            items = items[:-1]
        return None

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
            return self.store.read_or_write(lambda: self._history(
                memory_id, now, actor=actor, memory_ids=memory_ids))

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
        items, admitted = [], []
        for header in rows[:20]:
            if memory_ids is not None and header["memory_id"] not in memory_ids:
                continue
            if not work.take("candidates") or not work.take("text_bytes", header["text_bytes"]):
                break
            admitted.append(header)
        try:
            bodies = work.rows(self.store, "SELECT " + ",".join("m." + key
                for key in MEMORY_COLUMNS) + " FROM memories m JOIN "
                "unnest(%s::text[],%s::bigint[]) WITH ORDINALITY "
                "wanted(memory_id,bytes,position) USING(memory_id) "
                "WHERE octet_length(m.content)=wanted.bytes ORDER BY wanted.position",
                ([row["memory_id"] for row in admitted], [row["text_bytes"] for row in admitted]),
                limit=max(1, len(admitted))) if admitted else []
            sources.prefetch([row["memory_id"] for row in bodies])
        except retrieval_work.WorkLimit as error:
            work.mark(error.reason)
            bodies = []
        for row in bodies:
            try:
                if row and (evidence := sources.evidence(row["memory_id"])):
                    items.append(self._fact(row, evidence))
            except retrieval_work.WorkLimit as error:
                work.mark(error.reason)
                break
        check_version(self.memory, version)
        coverage = work.receipt()
        return {"privacy_epoch": version[0], "as_of": now, "revisions": items,
                "order": "newest_to_oldest_by_replacement",
                "time_basis": "recorded timestamps; effective dates only when explicit in content",
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
