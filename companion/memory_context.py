"""Bounded, source-checked views over the existing Memory store; no second memory store."""

import hashlib
import json
from datetime import datetime

from companion.contracts import CoreError
from companion.privacy import epoch, redact, sensitive
from companion.store import after, encode

MEMORY_FIELDS = (
    "memory_id", "subject", "kind", "content", "status", "needs_review",
    "supersedes_memory_id", "valid_from", "valid_to", "created_at", "updated_at",
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

    def __init__(self, memory, now=None, excluded=()):
        self._memory = memory
        self.store, self.settings, self.now = memory.store, memory.settings, now
        self.ledger = memory.ledger
        self.excluded = set(excluded)
        self.cutoff = (datetime.fromisoformat(after(
            now, -86400 * self.settings.raw_event_retention_days,
        )) if now else None)

    def _lineage(self, event_id):
        """Follow native edges and the exact legacy correction relation used by erasure."""
        lineage = "SELECT value FROM json_each(?)"
        if self.store.one("SELECT 1 FROM sqlite_master WHERE name='event_dependencies'"):
            lineage += (" UNION SELECT d.source_event_id FROM event_dependencies d "
                        "JOIN sources s ON d.event_id=s.event_id")
        pending, found = {event_id}, {}
        while pending:
            rows = self.store.rows(
                "WITH RECURSIVE sources(event_id) AS (" + lineage + ") "
                "SELECT s.event_id AS source_id,e.*,c.scope,c.owner_actor_id,r.key AS erased "
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
                    "SELECT DISTINCT e.event_id FROM memory_evidence copy "
                    "JOIN memories m USING(memory_id) JOIN memory_evidence v USING(memory_id) "
                    "JOIN events e ON e.event_id=v.event_id WHERE copy.event_id=? "
                    "AND m.supersedes_memory_id IS NOT NULL AND e.source='qq.onebot'",
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

        reference = self.store.one("SELECT 1 FROM runtime_meta WHERE key=?", (MEMORY + memory_id,))
        return [event for row in self.store.rows(
            "SELECT event_id FROM memory_evidence WHERE memory_id=? AND polarity=1 "
            "ORDER BY created_at,event_id", (memory_id,),
        ) if (event := self.event(row["event_id"], require_text=not reference))]

    def current(self, row):
        from companion import memory_references

        try:
            memory_references.for_memory(self._memory, row, self.now)
        except CoreError:
            return False
        if not self.now:
            return True
        now = datetime.fromisoformat(self.now)
        return ((not row["valid_from"] or datetime.fromisoformat(row["valid_from"]) <= now)
                and (not row["valid_to"] or now < datetime.fromisoformat(row["valid_to"])))


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

    def pin(self, now, *, rows=(), event_ids=(), historical=False):
        """Freeze full fact/evidence identity for a local await, never a display projection.

        The proof contains all positive roots and actual ancestors, including legacy
        correction sources. Callers persist consumed IDs in job_sources, not new edges
        between independent owner inputs. This transient proof does not grant access.
        """
        # Source-only pins are also used by independently authorized expression /
        # exploration consumers. They cannot read facts; Core checks their purpose.
        version = self._begin("owner") if rows else read_version(self.memory)
        sources = MemorySources(self.memory, now)
        roots, memories = set(event_ids), []
        for expected in rows:
            row = self.store.one("SELECT * FROM memories WHERE memory_id=?",
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
            from companion import memory_references

            reference = memory_references.for_memory(self.memory, row, now)
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

    def revalidate(self, proof, now):
        actual = self.pin(now, rows=proof["rows"], event_ids=proof["event_ids"],
                          historical=proof["historical"])
        if actual != proof:
            raise CoreError("PRIVACY_CHANGED", 409)

    def _limit(self):
        return getattr(self.memory.settings, "memory_recall_limit", 6)

    def recall(self, query, now, *, actor="owner", memory_ids=None, project_query=None, trace=None):
        """`memory_recall_limit` facts plus at most two unresolved conflicts; no model call.

        NO_RECORD means this eligible scope has no hit. Index/custody errors propagate
        rather than misrepresenting unavailable data as an empty personal history.
        """
        version = self._begin(actor)
        if not isinstance(query, str) or not query.strip():
            raise CoreError("INVALID_MEMORY_QUERY", 400)
        rows = self.memory.search(query, actor=actor, memory_ids=memory_ids,
                                  limit=self._limit() + 2, now=now,
                                  trace=trace)
        return self._recall_result(rows, version, query, now, memory_ids, project_query, trace)

    async def recall_async(self, query, now, *, actor="owner", memory_ids=None,
                           project_query=None, source_check=None, clock=None, trace=None):
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
        sources = MemorySources(self.memory, now)
        facts, conflicts = [], []
        for row in rows:
            evidence = sources.evidence(row["memory_id"])
            if not evidence:
                continue
            item = self._fact(row, evidence)
            from companion import memory_references

            reference = memory_references.for_memory(self.memory, row, now)
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
        return {"state": "FOUND" if facts else "NEEDS_REVIEW" if conflicts else "NO_RECORD",
                "privacy_epoch": version[0], "as_of": now, "facts": facts,
                "conflicts": conflicts[:2], "conflicts_truncated": len(conflicts) > 2,
                "rows": [row for row in rows if row["memory_id"] in selected],
                "requires_relevance_review": bool(rows)}

    def continuation(self, conversation_id, now, *, actor="owner", event_ids=None,
                     current_event_ids=(), used_memory_ids=(), source_check=None):
        """Three Hot/Warm episode excerpts within the caller's authorized source scope.

        These are attributed observations, never current facts or proof of a sent reply.
        Context integration must explicitly opt in with its authorized conversation/sources.
        """
        from companion.diary import is_management_command
        from companion.memory_projections import MemoryProjections, identifiers

        version = self._begin(actor)
        sources = MemorySources(self.memory, now)
        allowed = set(event_ids) if event_ids is not None else None
        current = set(identifiers(current_event_ids))
        used = identifiers(used_memory_ids)
        recalled = set()
        for memory in self.store.rows(
            "SELECT * FROM memories WHERE memory_id IN (SELECT value FROM json_each(?)) "
            "AND status IN ('CONFIRMED','HYPOTHESIS') AND confidence>0 "
            "AND privacy_scope='private'", (encode(used),),
        ):
            if sources.current(memory):
                recalled.update(item["event_id"] for item in sources.evidence(memory["memory_id"]))
        projection = MemoryProjections(self.memory)
        rows, truncated = projection.continuation_candidates(
            conversation_id, now, current | recalled,
        )
        items = []
        for row in rows:
            ids = [link["event_id"] for link in self.store.rows(
                "SELECT event_id FROM episode_events WHERE episode_id=? ORDER BY event_id LIMIT 21",
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
        version = self._begin(actor)
        if memory_ids is not None and memory_id not in memory_ids:
            return {"privacy_epoch": version[0], "as_of": now,
                    "revisions": [], "truncated": False}
        # The native replacement edge establishes the attribute relation even if
        # historical inference used different key spellings for that attribute.
        rows = self.store.rows(
            "WITH RECURSIVE edges(older,newer) AS ("
            "SELECT supersedes_memory_id,memory_id FROM memories "
            "WHERE supersedes_memory_id IS NOT NULL UNION "
            "SELECT a.entity_id,json_extract(a.safe_detail_json,'$.by') FROM audit_log a "
            "JOIN memories old ON old.memory_id=a.entity_id "
            "JOIN memories new ON new.memory_id=json_extract(a.safe_detail_json,'$.by') "
            "WHERE a.action='memory.replaced' AND a.created_at<=? "
            "AND old.subject=new.subject "
            "AND old.kind<>'episode' AND new.kind<>'episode'), "
            "family(memory_id) AS (SELECT ? UNION "
            "SELECT e.newer FROM edges e JOIN family f ON e.older=f.memory_id UNION "
            "SELECT e.older FROM edges e JOIN family f ON e.newer=f.memory_id), "
            "descendants(root,node) AS (SELECT memory_id,memory_id FROM family UNION "
            "SELECT d.root,e.newer FROM descendants d JOIN edges e ON e.older=d.node) "
            "SELECT m.* FROM memories m JOIN family f USING(memory_id) "
            "JOIN (SELECT root,COUNT(*) AS later_count FROM descendants GROUP BY root) rank "
            "ON rank.root=m.memory_id "
            "WHERE m.privacy_scope='private' AND m.confidence>0 "
            "AND m.status IN ('CONFIRMED','HYPOTHESIS','SUPERSEDED') "
            "ORDER BY rank.later_count,m.created_at DESC,m.memory_id LIMIT 21", (now, memory_id),
        )
        sources, items = MemorySources(self.memory, now), []
        for row in rows[:20]:
            if memory_ids is not None and row["memory_id"] not in memory_ids:
                continue
            if evidence := sources.evidence(row["memory_id"]):
                items.append(self._fact(row, evidence))
        check_version(self.memory, version)
        return {"privacy_epoch": version[0], "as_of": now, "revisions": items,
                "truncated": len(rows) > 20}

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
            "SELECT DISTINCT m.memory_id FROM memories m JOIN memory_evidence v USING(memory_id) "
            "WHERE v.event_id IN (SELECT value FROM json_each(?)) "
            "AND m.status IN ('CONFIRMED','HYPOTHESIS','SUPERSEDED')", (encode(sorted(affected)),),
        )
        retained, withdrawn = [], []
        for row in rows:
            if evidence := sources.evidence(row["memory_id"]):
                retained.append({"memory_id": row["memory_id"],
                                 "evidence_ids": [item["event_id"] for item in evidence]})
            else:
                withdrawn.append(row["memory_id"])
        episodes = self.store.rows(
            "SELECT DISTINCT episode_id FROM episode_events "
            "WHERE event_id IN (SELECT value FROM json_each(?)) ORDER BY episode_id",
            (encode(sorted(affected)),),
        )
        check_version(self.memory, version)
        return {"privacy_epoch": version[0], "source_event_ids": sorted(affected),
                "retain": retained, "withdraw": sorted(withdrawn),
                "recompute_episode_ids": [row["episode_id"] for row in episodes]}
