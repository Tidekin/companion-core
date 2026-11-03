import asyncio
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from companion.commands import command_parts
from companion.contracts import CoreError
from companion.memory_context import MemoryContext, MemorySources, check_version, read_version
from companion.privacy import (
    advance_epoch,
    memory_sensitive,
    memory_summary_sensitive,
    redact,
)
from companion.retrieval import SemanticIndex, fuse
from companion.store import encode, uid

RELAYED = "memory_relayed_update:"


class Memory:
    def __init__(self, store, settings, ledger):
        self.store, self.settings, self.ledger = store, settings, ledger
        self.index = SemanticIndex(store, settings)

    def require_owner(self, actor):
        if actor != "owner" or not self.settings.memory_enabled:
            raise CoreError("MEMORY_ACCESS_DENIED", 403)

    def project_recall_candidates(self, query, *, memory_ids=None, limit=6, now=None):
        """Nominate a bounded project catalog for an explicit retrospective request.

        A generic request can name the subject without resembling any fact's content.
        These are only candidates: Core must still pay for and apply relevance review.
        """
        clauses = re.split(r"[，,。！？!?；;\n]", query)
        # Negation must target recall or memory use, not accuracy/format constraints.
        if any(re.search(
            r"(?:不要|不用|无需|不必|不想|不需要|不希望|别|停止|禁止).{0,6}(?:回忆|回顾)|"
            r"(?:不|别|不要|不用|无需|不必).{0,4}(?:谈|提|聊|说出|检索|调用|使用)"
            r".{0,8}(?:记忆|项目|事实|约定|设定)|"
            r"\b(?:do not|don't|never|no need|stop|avoid)\b.{0,15}\b(?:recall|remember)\b|"
            r"\b(?:do not|don't|never|no need|stop|avoid)\b.{0,15}\buse\b.{0,15}\bmemory\b",
            clause, re.IGNORECASE,
        ) for clause in clauses):
            return []
        # A project statement beside an unrelated question is not a recall request.
        for clause in clauses:
            if (not re.search(r"项目|\bproject\b", clause, re.IGNORECASE)
                or re.search(r"(?:未|没|尚未|还没|未经)(?:有)?(?:确认|确定)", clause)):
                continue
            direct = re.search(
                r"你.{0,4}记得|记得.{0,40}(?:什么|哪些|吗)|(?:回忆|回顾)一下|"
                r"^(?:请|帮我|麻烦你)?(?:回忆|回顾)|记(?:住|下|录)(?:了)?(?:哪些|什么)|"
                r"^(?:please )?(?:recall|recollect)\b|\b(?:do you|what.*)\s+remember\b",
                clause, re.IGNORECASE,
            )
            recorded = re.search(
                r"(?:之前|以前|先前|上次|曾经|已经|已).{0,12}"
                r"(?:确认|确定|约定|记住|记下|记录|说过)|"
                r"(?:我|我们).{0,4}让你.{0,4}(?:记住|记下|记录)(?:的|过)|"
                r"\b(?:previously|already|earlier)\s+(?:confirmed|agreed|recorded)\b",
                clause, re.IGNORECASE,
            )
            question = re.search(
                r"什么|哪些|哪条|哪个|吗|(?:说说|列出|复述|讲讲|总结)|"
                r"\b(?:what|which|list|tell|remind)\b", clause, re.IGNORECASE,
            )
            if direct or (recorded and question):
                break
        else:
            return []
        # Reuse native eligibility, subject filtering, evidence and approval limits.
        return [row for row in self.search(
            "*", subjects=("project",), limit=limit, memory_ids=memory_ids, now=now,
        ) if row["status"] == "CONFIRMED" and not row["needs_review"]]

    def search(self, query, actor="owner", subjects=(), limit=6, *, memory_ids=None, now=None,
               explain=None, trace=None):
        """`explain`, when a dict, receives each returned memory's ranking evidence."""
        version, sources, rows, lexical = self._prepare_search(
            query, actor, subjects, limit, memory_ids=memory_ids, now=now,
        )
        similarities = self.index.scores(query, rows) if query != "*" and rows else {}
        return self._finish_search(query, rows, lexical, similarities, limit, version, sources,
                                   explain=explain, trace=trace)

    async def search_async(self, query, actor="owner", subjects=(), limit=6, *,
                           memory_ids=None, now=None, source_check=None, clock=None,
                           explain=None, trace=None):
        if source_check:
            source_check()
        version, sources, rows, lexical = self._prepare_search(
            query, actor, subjects, limit, memory_ids=memory_ids, now=now,
        )
        similarities = {}
        if query != "*" and rows:
            context = MemoryContext(self)
            proof = context.pin(now, rows=[
                {**row, "evidence_ids": [e["event_id"] for e in sources.evidence(row["memory_id"])]}
                for row in rows
            ])

            def revalidate():
                if source_check:
                    source_check()
                check_version(self, version)
                context.revalidate(proof, clock() if clock else now)
                for row in rows:
                    if self.store.one("SELECT * FROM memories WHERE memory_id=?",
                                      (row["memory_id"],)) != row:
                        raise CoreError("PRIVACY_CHANGED", 409)

            similarities = await self.index.scores_async(query, rows, revalidate=revalidate)
        rerank_scores = None
        model, pool = self._rerank_pool(
            query, self._fused_order(query, rows, lexical, similarities)[0], limit)
        if pool:
            from companion import reranker

            # CPU-bound cross-encoder stays off the event loop; nothing touches the Store there.
            try:
                values = await asyncio.wait_for(asyncio.to_thread(
                    reranker.scores, model, query, [row["content"] for row in pool]),
                    getattr(self.settings, "recall_compute_timeout_seconds", 15))
            except TimeoutError:
                # Keep the fused order; a sync rescore here would block the loop instead.
                values = [-index for index in range(len(pool))]
            rerank_scores = dict(zip((row["memory_id"] for row in pool), values, strict=True))
        if clock:
            sources = MemorySources(self, clock())
        return self._finish_search(query, rows, lexical, similarities, limit, version, sources,
                                   explain=explain, trace=trace, rerank_scores=rerank_scores)

    def _prepare_search(self, query, actor="owner", subjects=(), limit=6, *,
                        memory_ids=None, now=None):
        self.require_owner(actor)
        s = self.store
        version = read_version(self)
        sources = MemorySources(self, now)
        if memory_ids is not None and not memory_ids:
            return version, sources, [], []
        allowed_sql = ""
        if memory_ids is not None:
            allowed_sql = " AND memory_id IN (" + ",".join("?" for _ in memory_ids) + ")"
        # Missing evidence must never remain confidently retrievable.
        s.execute(
            "UPDATE memories SET confidence=0,needs_review=1,status='HYPOTHESIS' "
            "WHERE status IN ('CONFIRMED','HYPOTHESIS') AND NOT EXISTS "
            "(SELECT 1 FROM memory_evidence m JOIN events e ON e.event_id=m.event_id "
            "WHERE m.memory_id=memories.memory_id AND e.redacted=0)" + allowed_sql,
            tuple(memory_ids or ()),
        )
        base = (
            "SELECT m.* FROM memories m WHERE m.status IN ('CONFIRMED','HYPOTHESIS') "
            "AND m.privacy_scope='private' AND m.confidence>0 AND EXISTS "
            "(SELECT 1 FROM memory_evidence v JOIN events e ON e.event_id=v.event_id "
            "WHERE v.memory_id=m.memory_id AND v.polarity=1 "
            "AND e.redacted=0 AND e.actor_id='owner' AND e.privacy_scope='private') "
        )
        params = []
        if memory_ids is not None:
            base += "AND m.memory_id IN (" + ",".join("?" for _ in memory_ids) + ") "
            params.extend(memory_ids)
        if subjects:
            base += "AND m.subject IN (" + ",".join("?" for _ in subjects) + ") "
            params.extend(subjects)
        base += "ORDER BY m.needs_review,m.status='CONFIRMED' DESC,m.updated_at DESC"
        rows = [row for row in s.rows(base, params)
                if sources.current(row) and sources.evidence(row["memory_id"])]
        from companion.memory_enrichment import refresh_importance

        for row in rows:
            row["importance"] = refresh_importance(self, row, now)
        if query == "*":
            rows.sort(key=lambda row: (not row["needs_review"], row["status"] == "CONFIRMED",
                                      row["importance"], row["updated_at"]), reverse=True)
        lexical = []
        if query != "*" and rows:
            allowed = {row["memory_id"] for row in rows}
            if len(query) < 3:
                lexical = [row["memory_id"] for row in rows if query in row["content"]]
            else:
                terms = re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", query)
                grams = []
                for term in terms:
                    if re.search(r"[\u4e00-\u9fff]", term):
                        grams.extend(term[i : i + 3] for i in range(len(term) - 2))
                    else:
                        grams.append(term)
                grams = [term for term in dict.fromkeys(grams) if len(term) >= 3][:40]
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in grams)
                lexical = (
                    [
                        row["memory_id"]
                        for row in s.rows(
                            "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ? "
                            + ("AND memory_id IN (" + ",".join("?" for _ in allowed) + ") "
                               if memory_ids is not None else "")
                            + "ORDER BY bm25(memories_fts)",
                            (match, *allowed) if memory_ids is not None else (match,),
                        )
                        if row["memory_id"] in allowed
                    ]
                    if match
                    else []
                )
        return version, sources, rows, lexical

    def _rerank_pool(self, query, ordered, limit):
        """The fused-order rows a configured cross-encoder would rescore, and its model."""
        from companion import reranker

        model = reranker.directory(self.settings) if getattr(
            self.settings, "memory_rerank", False) else None
        if query == "*" or not model or len(ordered) < 2:
            return None, []
        return model, ordered[:limit * 2]

    def _finish_search(self, query, rows, lexical, similarities, limit, version, sources, *,
                       explain=None, trace=None, rerank_scores=None):
        """`rerank_scores` (memory_id -> score) lets async callers rescore off the loop."""
        eligible, lexical_raw = len(rows), len(lexical)
        rows, ranks, lexical_count, semantic_count = self._fused_order(
            query, rows, lexical, similarities)
        matched = len(rows)
        model, pool = self._rerank_pool(query, rows, limit)
        if pool:
            if rerank_scores is None or any(row["memory_id"] not in rerank_scores
                                            for row in pool):
                from companion import reranker

                rerank_scores = dict(zip((row["memory_id"] for row in pool), reranker.scores(
                    model, query, [row["content"] for row in pool]), strict=True))
            rows = sorted(pool, key=lambda row: -rerank_scores[row["memory_id"]]) + rows[
                limit * 2:]
        return self._complete_search(rows, ranks, eligible, lexical_raw, lexical_count,
                                     semantic_count, matched, limit, version, sources,
                                     explain=explain, trace=trace)

    def _fused_order(self, query, rows, lexical, similarities):
        ranks = {}
        lexical_count = semantic_count = 0
        if query != "*" and rows:
            exact = {row["memory_id"] for row in rows if query in row["content"]}
            # Generic shared phrases (e.g. "most liked") are not enough to change the topic.
            # Top lexical ranks survive the floor: short Chinese facts embed weakly.
            keep = getattr(self.settings, "lexical_keep_rank", 0)
            lexical = [
                key
                for rank, key in enumerate(lexical)
                if key in exact or rank < keep
                or similarities[key] >= self.settings.semantic_min_score
            ]
            semantic = sorted(
                (
                    key
                    for key, score in similarities.items()
                    if score >= self.settings.semantic_min_score
                ),
                key=lambda key: similarities[key],
                reverse=True,
            )
            scores = fuse(lexical, semantic)
            lexical_count, semantic_count = len(lexical), len(semantic)
            ranks = {key: {"exact": key in exact,
                           "lexical_rank": lexical.index(key) + 1 if key in lexical else None,
                           "semantic_rank": semantic.index(key) + 1 if key in semantic else None,
                           "similarity": round(similarities[key], 4),
                           "fused_score": round(scores[key], 5)} for key in scores}
            rows = sorted(
                (row for row in rows if row["memory_id"] in scores),
                key=lambda row: (
                    query in row["content"],
                    scores[row["memory_id"]],
                    similarities[row["memory_id"]],
                    row["importance"],
                ),
                reverse=True,
            )
        return rows, ranks, lexical_count, semantic_count

    def _complete_search(self, rows, ranks, eligible, lexical_raw, lexical_count,
                         semantic_count, matched, limit, version, sources, *,
                         explain=None, trace=None):
        s = self.store
        rows = rows[:limit]
        check_version(self, version)
        current = []
        for row in rows:
            # Embedding may yield to a concurrent writer. Never publish its stale snapshot.
            if s.one("SELECT * FROM memories WHERE memory_id=?", (row["memory_id"],)) != row:
                raise CoreError("PRIVACY_CHANGED", 409)
            row["evidence_ids"] = [e["event_id"] for e in sources.evidence(row["memory_id"])]
            if row["evidence_ids"]:
                current.append(row)
        check_version(self, version)
        if explain is not None:
            explain.update({row["memory_id"]: ranks.get(row["memory_id"], {}) for row in current})
        if trace is not None:
            trace.update({"eligible_candidates": eligible, "lexical_matches": lexical_raw,
                "accepted_lexical": lexical_count, "accepted_semantic": semantic_count,
                "matched_candidates": matched, "limit": limit,
                "selected": {row["memory_id"]: ranks.get(row["memory_id"], {})
                             for row in current}})
        return current

    def validate_candidate(self, candidate, confirmed=False, *, now=None, rejection=None):
        def reject(reason):
            if rejection is not None:
                rejection["reason"] = reason
            return None

        event = MemorySources(self, now).event(candidate.evidence_event_id, require_text=False)
        if not event:
            raise CoreError("MISSING_MEMORY_EVIDENCE")
        body = json.loads(event["body_json"])
        text = body.get("text", "")
        reference = None
        if candidate.evidence_reference_id:
            from companion import memory_references

            if confirmed or candidate.subject == "self":
                raise CoreError("UNSUPPORTED_MEMORY_EVIDENCE")
            reference = memory_references.candidate(self, candidate, now)
            text = reference["text"]
        if not text.strip() or candidate.evidence_quote not in text:
            raise CoreError("UNSUPPORTED_MEMORY_EVIDENCE")
        if not confirmed and self.store.one(
            "SELECT 1 FROM memories newer JOIN memories older "
            "ON newer.supersedes_memory_id=older.memory_id "
            "WHERE older.subject=? AND older.content=? AND newer.content<>? "
            "AND newer.origin='user_confirmed' AND newer.status IN ('CONFIRMED','SUPERSEDED') "
            "AND newer.created_at>=? LIMIT 1",
            (candidate.subject, candidate.content, candidate.content, event["ingested_at"]),
        ):
            # A delayed extraction of an old value cannot undo the owner's later correction.
            return reject("OWNER_CORRECTION_PRECEDES_EXTRACTION")
        # Check both source and summary; only the generic card noun has a narrow exception.
        if not confirmed and memory_sensitive(text):
            return reject("SENSITIVE_SOURCE")
        if not confirmed and memory_summary_sensitive(candidate.content, text):
            return reject("SENSITIVE_SUMMARY")
        if candidate.subject == "self" and not re.search(r"你(?:最近|经常|总是|这几次|刚才)", text):
            return reject("UNSUPPORTED_SELF_OBSERVATION")
        content = redact(candidate.content, self.settings)
        if "[REDACTED]" in content:
            return reject("REDACTED_SUMMARY")
        if reference:
            content = memory_references.attributed(candidate, reference)
        return event, content

    def confirm(self, memory_id, now):
        self.store.execute(
            "UPDATE memories SET status='CONFIRMED',confidence=1,"
            "origin='user_confirmed',updated_at=? WHERE memory_id=?",
            (now, memory_id),
        )
        advance_epoch(self.store)

    def add(self, candidate, now, confirmed=False, resolution=None):
        validated = self.validate_candidate(candidate, confirmed, now=now)
        if validated is None:
            return None
        event, content = validated
        s = self.store
        reference = candidate.evidence_reference_id is not None
        sources = MemorySources(self, now)
        same = next((row for row in s.rows(
            "SELECT * FROM memories WHERE content=? AND subject=? AND status IN "
            "('HYPOTHESIS','CONFIRMED')",
            (content, candidate.subject),
        ) if sources.current(row) and (row["origin"] == "event_observed") == reference), None)
        conflicts = []
        if resolution is not None:
            for memory_id in [resolution.get("same"), *resolution.get("conflicts", [])]:
                if memory_id is None:
                    continue
                row = s.one(
                    "SELECT * FROM memories WHERE memory_id=? AND subject=? "
                    "AND status IN ('HYPOTHESIS','CONFIRMED') AND confidence>0",
                    (memory_id, candidate.subject),
                )
                if not row or not sources.current(row):
                    raise CoreError("MEMORY_MATCH_STALE", 409)
                if (row["origin"] == "event_observed") != reference:
                    raise CoreError("MEMORY_MATCH_STALE", 409)
                if memory_id == resolution.get("same"):
                    same = row
                else:
                    conflicts.append(row)
        elif not same and candidate.kind != "episode":
            # An episode is one occurrence, not an attribute value: it never conflicts by key.
            conflicts = [row for row in s.rows(
                "SELECT * FROM memories WHERE subject=? AND fact_key=? AND kind<>'episode' "
                "AND status IN ('HYPOTHESIS','CONFIRMED') AND content<>?",
                (candidate.subject, candidate.fact_key, content),
            ) if sources.current(row) and (row["origin"] == "event_observed") == reference]
        replaced, relayed = [], False
        if (candidate.update == "replaces_previous" and candidate.kind != "episode" and not same
                and (candidate.speaker == "owner" or candidate.relayed_by_owner)):
            # The owner's own update ends the older values' validity (kept for history,
            # not deleted), across origins. A third party's update the owner only relayed
            # never does: both stay current and are linked, so the reader sees the later
            # relayed claim with its source. Owner-confirmed corrections stay untouched.
            relayed = candidate.speaker != "owner"
            # Reviewed relations identify the attribute before any mutation. A coarse
            # or differently spelled candidate key must not replace unrelated facts.
            targets = ([row for row in conflicts if row["memory_id"] in resolution["replaces"]]
                       if resolution is not None else s.rows(
                "SELECT * FROM memories WHERE subject=? AND fact_key=? AND kind<>'episode' "
                "AND status IN ('HYPOTHESIS','CONFIRMED') AND confidence>0 AND content<>?",
                (candidate.subject, candidate.fact_key, content)))
            replaced = [row for row in targets if sources.current(row) and not (
                row["origin"] == "user_confirmed" and row["status"] == "CONFIRMED")]
            if replaced:
                candidate = candidate.model_copy(update={"fact_key": replaced[0]["fact_key"]})
            ended = {row["memory_id"] for row in replaced}
            conflicts = [row for row in conflicts if row["memory_id"] not in ended]
        if same:
            memory_id = same["memory_id"]
            if confirmed:
                self.confirm(memory_id, now)
        else:
            memory_id = uid("mem")
            s.insert(
                "memories",
                memory_id=memory_id,
                subject=candidate.subject,
                kind=candidate.kind,
                content=content,
                status="CONFIRMED" if confirmed else "HYPOTHESIS",
                origin="event_observed" if reference else (
                    "user_confirmed" if confirmed else "model_inferred"),
                confidence=1.0 if confirmed else 0.5,
                importance=0.5,
                privacy_scope="private",
                created_at=now,
                updated_at=now,
                fact_key=(candidate.fact_key if replaced else
                          conflicts[0]["fact_key"] if conflicts else candidate.fact_key),
                needs_review=int(bool(conflicts)),
            )
        if conflicts:
            for row in [*conflicts, same or {"memory_id": memory_id}]:
                if (not confirmed and row.get("supersedes_memory_id")
                    and row.get("origin") == "user_confirmed"
                    and row.get("status") == "CONFIRMED"):
                    # Inference can raise a pending conflict; only the owner changes a correction.
                    continue
                s.execute(
                    "UPDATE memories SET needs_review=1 WHERE memory_id=?", (row["memory_id"],)
                )
            s.audit("memory.conflict", memory_id, event["trace_id"], now)
        for row in replaced:
            if relayed:
                s.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                          (RELAYED + row["memory_id"], encode({"newer": memory_id})))
                s.audit("memory.relayed_update", row["memory_id"], event["trace_id"], now,
                        by=memory_id)
                continue
            s.execute("UPDATE memories SET valid_to=?,updated_at=? WHERE memory_id=? "
                      "AND (valid_to IS NULL OR valid_to>?)",
                      (event["occurred_at"], now, row["memory_id"], event["occurred_at"]))
            s.audit("memory.replaced", row["memory_id"], event["trace_id"], now,
                    by=memory_id)
        s.execute(
            "INSERT OR IGNORE INTO memory_evidence VALUES (?,?,1,?)",
            (memory_id, event["event_id"], now),
        )
        if candidate.subject == "self":
            key = s.one("SELECT fact_key FROM memories WHERE memory_id=?", (memory_id,))["fact_key"]
            self.observe_trait(key, event, now)
        if reference:
            from companion import memory_references

            memory_references.record(self, memory_id, candidate, now)
        advance_epoch(s)
        return memory_id

    def with_relayed_updates(self, rows, now):
        """Add the other side of each relayed-update link; returns (rows, relations).

        Linked rows pass the same currency and evidence checks as search results, so a
        forgotten, superseded or erased memory never re-enters through a link.
        """
        s, sources = self.store, MemorySources(self, now)
        ids = {row["memory_id"] for row in rows}
        links = [(row["key"][len(RELAYED):], json.loads(row["value"])["newer"])
                 for row in s.rows("SELECT key,value FROM runtime_meta WHERE key LIKE ?",
                                   (RELAYED + "%",))]
        out, relations = list(rows), {}
        for older, newer in links:
            if not ({older, newer} & ids):
                continue
            pair = {}
            for memory_id in (older, newer):
                row = next((item for item in out if item["memory_id"] == memory_id), None)
                if row is None:
                    row = s.one("SELECT * FROM memories WHERE memory_id=? AND status IN "
                                "('HYPOTHESIS','CONFIRMED') AND confidence>0", (memory_id,))
                    evidence = row and sources.current(row) and [
                        e["event_id"] for e in sources.evidence(memory_id)]
                    if not evidence:
                        break
                    row["evidence_ids"] = evidence
                    out.append(row)
                pair[memory_id] = row
            else:
                relations.setdefault(older, {})["later_relayed_update"] = pair[newer]["content"]
                relations.setdefault(newer, {})["relayed_update_of"] = pair[older]["content"]
        return out, relations

    def observe_trait(self, topic, event, now):
        s = self.store
        trait = s.one("SELECT * FROM traits WHERE category='style' AND topic=?", (topic,))
        trait_id = trait["trait_id"] if trait else uid("trait")
        if not trait:
            s.insert(
                "traits",
                trait_id=trait_id,
                category="style",
                topic=topic,
                strength=0.2,
                confidence=0.3,
                status="EXPLORING",
                updated_at=now,
            )
        s.execute(
            "INSERT OR IGNORE INTO trait_evidence VALUES (?,?,1,0.5,?)",
            (trait_id, event["event_id"], event["occurred_at"]),
        )
        evidence = s.rows(
            "SELECT e.conversation_id,e.occurred_at FROM trait_evidence t "
            "JOIN events e ON e.event_id=t.event_id WHERE t.trait_id=? "
            "AND e.redacted=0 AND e.actor_id='owner'",
            (trait_id,),
        )
        sessions, days = set(), set()
        for item in evidence:
            dt = datetime.fromisoformat(item["occurred_at"]).astimezone(
                ZoneInfo(self.settings.timezone)
            )
            sessions.add((item["conversation_id"], int(dt.timestamp()) // 1800))
            days.add(dt.date())
        conflict = s.one(
            "SELECT 1 FROM memories WHERE subject='self' AND fact_key=? AND needs_review=1",
            (topic,),
        )
        state = "EXPLORING"
        if len(sessions) >= self.settings.emerging_min_sessions:
            state = "EMERGING"
        if state == "EMERGING" and len(days) >= self.settings.stable_min_days and not conflict:
            state = "STABLE"
        if conflict:
            state = "FADING"
        s.execute(
            "UPDATE traits SET status=?,independent_sessions=?,independent_days=?,"
            "strength=?,confidence=?,last_evidence_at=?,updated_at=?,version=version+1 "
            "WHERE trait_id=?",
            (
                state,
                len(sessions),
                len(days),
                min(0.9, len(days) / 10),
                min(0.9, 0.3 + len(sessions) / 20),
                now,
                now,
                trait_id,
            ),
        )

    def decide_from_console(self, memory_id, decision, now, corrected=None, *, operation_id,
                            origin, owner_check):
        """Called only by the owner Console; receipt and native decision commit together."""
        s = self.store
        with nullcontext() if s.db.in_transaction else s.transaction():
            # Reuse the same session/control credential/CSRF/origin check inside the write
            # transaction. The bridge-token memory API never calls this entry point.
            owner_check()
            if not self.settings.console.enabled or origin != self.settings.console.public_origin:
                raise CoreError("CONSOLE_ORIGIN_REQUIRED", 403)
            if decision != "correct" and corrected is not None:
                raise CoreError("CORRECTION_NOT_APPLICABLE", 400)
            request_hash = hashlib.sha256(encode([
                "POST", "/v1/console/memories/" + memory_id + "/decision",
                {"decision": decision, "corrected_content": corrected},
            ]).encode()).hexdigest()
            operation = s.one("SELECT * FROM console_operations WHERE request_id=?",
                              (operation_id,))
            if (not operation or operation["state"] != "STARTED"
                or operation["request_sha256"] != request_hash):
                raise CoreError("CONSOLE_OPERATION_RECONCILIATION_REQUIRED", 409)
            before = s.one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
            if not before:
                raise CoreError("MEMORY_NOT_FOUND", 404)
            roots = {e["event_id"] for e in s.rows(
                "SELECT event_id FROM memory_evidence WHERE memory_id=?", (memory_id,),
            )}
            result = self.decide(memory_id, decision, now, corrected)
            if decision == "forget":
                return result
            current = s.one("SELECT * FROM memories WHERE memory_id=?", (result["memory_id"],))
            roots.update(e["event_id"] for e in s.rows(
                "SELECT event_id FROM memory_evidence WHERE memory_id=?", (result["memory_id"],),
            ))
            evidence = s.one("SELECT * FROM events WHERE event_id IN "
                             "(SELECT value FROM json_each(?)) ORDER BY event_id LIMIT 1",
                             (encode(sorted(roots)),))
            if not evidence:
                raise CoreError("MISSING_MEMORY_EVIDENCE", 409)
            event_id = uid("evt")
            body = {
                "text": current["content"], "channel": "owner_console", "origin": origin,
                "operation_id": operation_id, "request_sha256": request_hash,
                "memory_id": memory_id, "result_memory_id": current["memory_id"],
                "decision": decision,
                "before_sha256": hashlib.sha256(encode(before).encode()).hexdigest(),
                "after_sha256": hashlib.sha256(encode(current).encode()).hexdigest(),
                "content_sha256": hashlib.sha256(current["content"].encode()).hexdigest(),
                "source_event_ids": sorted(roots),
            }
            s.insert("events", event_id=event_id, schema_version=1, source="owner.decision",
                     source_event_id="console-memory:" + operation_id,
                     conversation_id=evidence["conversation_id"], actor_id="owner",
                     event_type="memory.console_decided", occurred_at=now, ingested_at=now,
                     privacy_scope="private", body_json=encode(body), trace_id=event_id,
                     memory_processed=1)
            s.insert("memory_evidence", memory_id=current["memory_id"], event_id=event_id,
                     polarity=1, created_at=now)
            for source_id in sorted(roots):
                s.insert("event_dependencies", event_id=event_id, source_event_id=source_id)
            s.audit("memory.owner_decision", memory_id, event_id, now, decision=decision,
                    source_event_id=event_id, channel="owner_console",
                    receipt_sha256=hashlib.sha256(encode(body).encode()).hexdigest())
            return result

    def decide(self, memory_id, decision, now, corrected=None, *, evidence_event_id=None):
        self.require_owner("owner")
        s = self.store
        with nullcontext() if s.db.in_transaction else s.transaction():
            old = s.one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
            if not old:
                raise CoreError("MEMORY_NOT_FOUND", 404)
            if evidence_event_id is not None:
                source = s.one("SELECT * FROM events WHERE event_id=?", (evidence_event_id,))
                operation = {
                    "confirm": "确认这一条",
                    "correct": "纠正这一条",
                    "forget": "忘记这一条",
                }
                command, arguments = command_parts(
                    json.loads(source["body_json"]).get("text", "") if source else ""
                )
                expected = [memory_id, corrected] if decision == "correct" else [memory_id]
                if (
                    not source
                    or source["redacted"]
                    or source["actor_id"] != "owner"
                    or source["privacy_scope"] != "private"
                    or source["source"] != "qq.onebot"
                    or source["event_type"] != "message.received"
                    or command != operation.get(decision)
                    or arguments.split(maxsplit=1) != expected
                ):
                    raise CoreError("MEMORY_DECISION_SOURCE_INVALID", 403)
                if decision == "correct":
                    # The successor and this source link commit together. Recover
                    # the same result after a crash before Outbox/job completion.
                    previous = s.one(
                        "SELECT m.memory_id,m.status,m.content FROM memories m "
                        "JOIN memory_evidence v USING(memory_id) "
                        "WHERE m.supersedes_memory_id=? AND v.event_id=? AND v.polarity=1",
                        (memory_id, evidence_event_id),
                    )
                    if previous:
                        if previous["status"] != "CONFIRMED" or previous["content"] != corrected:
                            raise CoreError("MEMORY_NOT_ACTIVE")
                        return {"memory_id": previous["memory_id"], "state": "CONFIRMED",
                                "supersedes": memory_id}
                s.audit(
                    "memory.owner_decision",
                    memory_id,
                    source["trace_id"],
                    now,
                    decision=decision,
                    source_event_id=evidence_event_id,
                )
            if decision == "forget":
                if old["status"] != "FORGOTTEN":
                    self.ledger.append(s, memory_id, now)
                return {"memory_id": memory_id, "state": "FORGOTTEN"}
            if old["status"] not in {"HYPOTHESIS", "CONFIRMED"}:
                raise CoreError("MEMORY_NOT_ACTIVE")
            if decision == "confirm":
                self.confirm(memory_id, now)
                return {"memory_id": memory_id, "state": "CONFIRMED"}
            if not corrected or not corrected.strip():
                raise CoreError("CORRECTION_REQUIRED", 400)
            if redact(corrected, self.settings) != corrected:
                raise CoreError("CREDENTIAL_MEMORY_DENIED", 400)
            new_id = uid("mem")
            s.insert(
                "memory_revisions",
                revision_id=uid("rev"),
                memory_id=memory_id,
                revision_reason="owner_correction",
                prior_status=old["status"],
                prior_content=old["content"],
                changed_by="owner",
                created_at=now,
            )
            s.execute(
                "UPDATE memories SET status='SUPERSEDED',updated_at=? WHERE memory_id=?",
                (now, memory_id),
            )
            s.insert(
                "memories",
                memory_id=new_id,
                subject=old["subject"],
                kind=old["kind"],
                content=corrected,
                status="CONFIRMED",
                origin="user_confirmed",
                confidence=1.0,
                importance=0.5,
                privacy_scope="private",
                supersedes_memory_id=memory_id,
                created_at=now,
                updated_at=now,
                fact_key=old["fact_key"],
            )
            # Correction is itself an authenticated owner event; original evidence is retained.
            evidence = s.one(
                "SELECT e.* FROM memory_evidence m JOIN events e ON e.event_id=m.event_id "
                "WHERE m.memory_id=? LIMIT 1",
                (memory_id,),
            )
            if not evidence:
                raise CoreError("MISSING_MEMORY_EVIDENCE")
            event_id = uid("evt")
            s.insert(
                "events",
                event_id=event_id,
                schema_version=1,
                source="owner.decision",
                source_event_id=event_id,
                conversation_id=evidence["conversation_id"],
                actor_id="owner",
                event_type="memory.corrected",
                occurred_at=now,
                ingested_at=now,
                privacy_scope="private",
                body_json=encode({"text": corrected}),
                trace_id=event_id,
                memory_processed=1,
            )
            s.insert(
                "memory_evidence", memory_id=new_id, event_id=event_id, polarity=1, created_at=now
            )
            if evidence_event_id is not None:
                # The QQ correction contains the new fact too. The same Ledger family must
                # erase that original, as well as the derived owner.decision event.
                s.insert(
                    "memory_evidence",
                    memory_id=new_id,
                    event_id=evidence_event_id,
                    polarity=1,
                    created_at=now,
                )
                s.insert("event_dependencies", event_id=event_id,
                         source_event_id=evidence_event_id)
            # Invalidate only derived text that could restate the corrected attribute: what
            # covers evidence of any value of this fact key, or quotes the old value.
            sources = json.dumps(sorted({row["event_id"] for row in s.rows(
                "SELECT DISTINCT v.event_id FROM memory_evidence v JOIN memories m "
                "USING(memory_id) WHERE m.subject=? AND m.fact_key=? AND v.event_id<>?",
                (old["subject"], old["fact_key"], event_id))}))
            s.execute(
                "DELETE FROM conversation_summaries WHERE instr(summary_text,?)>0 OR EXISTS ("
                "SELECT 1 FROM events f JOIN events l ON l.event_id=last_event_id "
                "JOIN events e ON e.conversation_id=f.conversation_id "
                "AND e.rowid BETWEEN f.rowid AND l.rowid WHERE f.event_id=first_event_id "
                "AND e.event_id IN (SELECT value FROM json_each(?)))",
                (old["content"], sources))
            s.execute(
                "UPDATE episodes SET summary='',status='REDACTED' WHERE instr(summary,?)>0 "
                "OR instr(title,?)>0 OR episode_id IN (SELECT episode_id FROM episode_events "
                "WHERE event_id IN (SELECT value FROM json_each(?)))",
                (old["content"], old["content"], sources))
            s.execute(
                "DELETE FROM trait_evidence WHERE trait_id IN "
                "(SELECT trait_id FROM traits WHERE topic=?)",
                (old["fact_key"],),
            )
            s.execute(
                "UPDATE traits SET status='FADING',confidence=0 WHERE topic=?", (old["fact_key"],)
            )
            advance_epoch(s)
            return {"memory_id": new_id, "state": "CONFIRMED", "supersedes": memory_id}

    def episodes(self, events, now, *, mark_processed=True):
        """Encode one bounded extraction batch atomically and idempotently."""
        s = self.store
        with nullcontext() if s.db.in_transaction else s.transaction():
            return self._encode_episode(events, now, mark_processed=mark_processed)

    def episode_summary(self, episode_id, source_event_ids, now):
        from companion.memory_enrichment import summary

        return summary(self, episode_id, source_event_ids, now)

    def episode_coverage(self, event_ids, now):
        """Reuse only complete, still-private native episodes; never trust old summary text."""
        version = read_version(self)
        if not 1 <= len(event_ids) <= 20:
            raise CoreError("INVALID_EPISODE_SOURCES", 400)
        rows = self.store.rows(
            "SELECT DISTINCT ep.episode_id,ep.conversation_id FROM episodes ep "
            "JOIN episode_events ee USING(episode_id) WHERE ep.status='ACTIVE' "
            "AND ep.privacy_scope='private' AND ee.event_id IN "
            "(SELECT value FROM json_each(?)) ORDER BY ep.episode_id LIMIT 101",
            (encode(sorted(set(event_ids))),),
        )
        if len(rows) > 100:
            raise CoreError("EPISODE_COVERAGE_LIMIT", 409)
        sources, result = MemorySources(self, now), []
        for row in rows:
            ids = [link["event_id"] for link in self.store.rows(
                "SELECT event_id FROM episode_events WHERE episode_id=? ORDER BY event_id LIMIT 21",
                (row["episode_id"],),
            )]
            if not 1 <= len(ids) <= 20:
                continue
            evidence = [sources.event(key, require_text=False) for key in ids]
            if all(event is not None and event["conversation_id"] == row["conversation_id"]
                   for event in evidence):
                result.append({"episode_id": row["episode_id"], "source_event_ids": ids})
        check_version(self, version)
        return result

    def _encode_episode(self, events, now, *, mark_processed=True):
        s = self.store
        version = read_version(self)
        if not 1 <= len(events) <= 20 or len({e["event_id"] for e in events}) != len(events):
            raise CoreError("INVALID_EPISODE_SOURCES", 400)
        sources = MemorySources(self, now)
        refreshed = []
        for event in events:
            current = sources.event(event["event_id"], require_text=False)
            if (not current or any(current[key] != event[key] for key in (
                    "conversation_id", "source", "event_type", "occurred_at", "ingested_at",
                ))
                or any(json.loads(current["body_json"]).get(key)
                       != json.loads(event["body_json"]).get(key) for key in ("text", "media"))
                or current["source"] != "qq.onebot"
                or current["event_type"] not in {"message.received", "message.media_received"}):
                raise CoreError("EPISODE_SOURCE_CHANGED", 409)
            refreshed.append(current)
        events = sorted(refreshed, key=lambda e: (
            e["occurred_at"], e["ingested_at"], e["event_id"],
        ))
        if len({e["conversation_id"] for e in events}) != 1:
            raise CoreError("INVALID_EPISODE_SOURCES", 400)
        # An authentic image/voice/file with no owner text has no extractive summary.
        # Complete it in the same batch transaction; it must not roll back sibling facts
        # or become a permanently retried extraction job. It is not positive text evidence.
        if mark_processed:
            for event in events:
                s.execute("UPDATE events SET memory_processed=1 WHERE event_id=?",
                          (event["event_id"],))
        events = [event for event in events if json.loads(event["body_json"])["text"].strip()]
        if not events:
            check_version(self, version)
            return None
        episode_id = "episode_" + hashlib.sha256(encode(
            sorted(e["event_id"] for e in events),
        ).encode()).hexdigest()[:32]
        if s.one("SELECT 1 FROM episodes WHERE episode_id=?", (episode_id,)):
            check_version(self, version)
            return episode_id
        covered = {key for row in self.episode_coverage([e["event_id"] for e in events], now)
                   for key in row["source_event_ids"]}
        if covered:
            events = [event for event in events if event["event_id"] not in covered]
            if not events:
                check_version(self, version)
                return None
            episode_id = "episode_" + hashlib.sha256(encode(
                sorted(e["event_id"] for e in events),
            ).encode()).hexdigest()[:32]
            if s.one("SELECT 1 FROM episodes WHERE episode_id=?", (episode_id,)):
                check_version(self, version)
                return episode_id
        excerpts = [json.loads(e["body_json"]).get("text", "")[:100] for e in events]
        summary = "资料摘录（不可信观察）：\n" + "\n".join(excerpts)
        s.insert(
            "episodes",
            episode_id=episode_id,
            title="QQ 对话片段",
            summary=summary,
            conversation_id=events[0]["conversation_id"],
            privacy_scope="private",
            started_at=events[0]["occurred_at"],
            ended_at=events[-1]["occurred_at"],
            status="ACTIVE",
            extractor_version="v2-extractive",
            created_at=now,
        )
        for event in events:
            s.insert("episode_events", episode_id=episode_id, event_id=event["event_id"])
        s.insert(
            "conversation_summaries",
            summary_id=uid("summary"),
            conversation_id=events[0]["conversation_id"],
            first_event_id=events[0]["event_id"],
            last_event_id=events[-1]["event_id"],
            summary_text=summary,
            summarizer_version="v2-extractive",
            created_at=now,
        )
        check_version(self, version)
        return episode_id
