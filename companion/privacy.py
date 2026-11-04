import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from companion.commands import command_parts
from companion.contracts import CoreError
from companion.store import encode, uid

SECRET_PATTERNS = [
    r"(?i)\b(?:sk-|ghp_|gho_)[a-z0-9_-]{12,}",
    r"(?i)\bBearer\s+[^\s\"'<>]+",
    r"(?i)\b(?:api[_-]?key|password|passwd|token|cookie|skey|p_skey)\s*[:=]\s*[^\s,;]+",
    r"(?:密码|密钥)\s*[:：=]\s*[^\s,，;；]+",
]


def redact(text, settings):
    for secret in (
        settings.bridge_token.get_secret_value(),
        settings.model_key.get_secret_value(),
        settings.video_model_key.get_secret_value(),
        settings.diary_admin_token.get_secret_value(),
        settings.control_token.get_secret_value(),
        settings.content.vision.key.get_secret_value(),
        *(adapter.token.get_secret_value() for adapter in settings.observation_adapters.values()),
        *(endpoint.token.get_secret_value() for endpoint in settings.executive.endpoints.values()),
        *(
            endpoint.key.get_secret_value()
            for endpoint in (settings.media.stt, settings.media.tts, settings.media.vision)
        ),
    ):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text)
    return text


_SECRETS = r"REDACTED|密码|密钥|身份证|银行卡|住址"
_INFERRED = r"诊断|病历|抑郁|性取向|宗教|政治立场|他患|她患|系统规则|system prompt|ignore previous"


def memory_sensitive(text):
    """Memory keeps attributed third-party facts and quoted lines; secrets stay excluded."""
    return bool(re.search(_SECRETS + "|" + _INFERRED, text, re.I))


def memory_summary_sensitive(text, source):
    """Allow the generic card noun in an explicit bank-card opening event only."""
    if re.search(r"银行.{0,12}办.{0,6}卡", source) and not re.search(r"\d", text):
        text = re.sub(r"银行卡(?=$|[，。！？、；])", "", text)
    return memory_sensitive(text)


def sensitive(text):
    return bool(
        re.search(
            r"REDACTED|密码|密钥|身份证|银行卡|住址|诊断|病历|抑郁|性取向|宗教|政治立场|"
            r"我朋友|我同事|他患|她患|帖子|网页|系统规则|system prompt|ignore previous|^>",
            text,
            re.I | re.M,
        )
    )


def epoch(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key='privacy_epoch'")
    return int(row["value"]) if row else 0


def advance_epoch(store):
    store.execute(
        "INSERT OR REPLACE INTO runtime_meta VALUES ('privacy_epoch',?)",
        (str(epoch(store) + 1),),
    )


def diary_copies(store, dates, before=None):
    """Resolve explicit diary commands, including records from before date metadata existed."""
    result = set()
    for row in store.rows(
        "SELECT event_id,body_json FROM events WHERE redacted=0 "
        "AND json_extract(body_json,'$.diary_command')=1 "
        "AND (? IS NULL OR ingested_at<=?)",
        (before, before),
    ):
        body = json.loads(row["body_json"])
        text = body.get("text", "")
        command, arguments = command_parts(text)
        # This marked-only lookup must still erase readbacks from pre-slash backups.
        parts = (command + " " + arguments if command else text).split(maxsplit=2)
        if not parts:
            continue
        if parts[0] == "日记列表" or (parts[0] == "读日记" and len(parts) == 1):
            result.add(row["event_id"])
        elif parts[0] in {"读日记", "修改日记"} and len(parts) >= 2:
            try:
                command_day = date.fromisoformat(parts[1]).isoformat()
            except ValueError:
                continue
            if any(
                command_day == day and body.get("diary_timezone", timezone) == timezone
                for day, timezone in dates
            ):
                result.add(row["event_id"])
    return result


def source_grant_closure(store, event_ids, memory_ids=()):
    """Follow explicit derivation, including owner corrections of attributed memories."""
    from companion.observation import dependent_events

    event_ids, memory_ids = set(event_ids), set(memory_ids)
    relatives = store.rows("SELECT memory_id,supersedes_memory_id FROM memories")
    evidence = store.rows(
        "SELECT m.memory_id,m.event_id,e.source,e.event_type FROM memory_evidence m "
        "JOIN events e USING(event_id)"
    )
    while True:
        before = len(event_ids), len(memory_ids)
        event_ids = dependent_events(store, event_ids)
        for row in evidence:
            if row["event_id"] in event_ids:
                memory_ids.add(row["memory_id"])
        for row in relatives:
            if row["supersedes_memory_id"] in memory_ids:
                memory_ids.add(row["memory_id"])
        for row in evidence:
            if (
                row["memory_id"] in memory_ids
                and row["source"] == "owner.decision" and row["event_type"] == "memory.corrected"
            ):
                event_ids.add(row["event_id"])
        if before == (len(event_ids), len(memory_ids)):
            return event_ids, memory_ids


class ErasureLedger:
    def __init__(self, path):
        self.path = Path(path)

    def initialize(self, store):
        existing = store.one("SELECT value FROM runtime_meta WHERE key='ledger_id'")
        if not self.path.exists():
            if existing:
                raise CoreError("ERASURE_LEDGER_MISSING", 503)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(encode({"ledger_id": uid("ledger")}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        lines = self.read()
        ledger_id = lines[0]["ledger_id"]
        if existing and existing["value"] != ledger_id:
            raise CoreError("ERASURE_LEDGER_MISMATCH", 503)
        store.execute("INSERT OR IGNORE INTO runtime_meta VALUES ('ledger_id',?)", (ledger_id,))
        self.replay(store)

    def read(self):
        try:
            lines = [json.loads(line) for line in self.path.read_text("utf-8").splitlines()]
            if not lines or not isinstance(lines[0].get("ledger_id"), str):
                raise ValueError("missing header")
            for seq, record in enumerate(lines[1:], 1):
                if (
                    record["seq"] != seq
                    or not isinstance(record["event_ids"], list)
                    or not isinstance(record["memory_ids"], list)
                    or not all(
                        isinstance(value, str)
                        for value in record["event_ids"] + record["memory_ids"]
                    )
                ):
                    raise ValueError("invalid sequence")
                if record.get("target_type", "memory") not in {
                    "memory", "event_batch", "raw_retention", "diary", "source_grant",
                    "input_recall", "affect_reset", "mood_reset",
                }:
                    raise ValueError("unknown erasure target")
                if record.get("target_type") == "affect_reset":
                    from companion.affect import validate_reset_record

                    validate_reset_record(record)
                if record.get("target_type") == "mood_reset":
                    from companion.mood import validate_reset_record as validate_mood_reset

                    validate_mood_reset(record)
                if record.get("target_type") == "input_recall":
                    from companion.input_recall import marker_key, validate_source

                    source = validate_source(record["recall_source"])
                    target = marker_key(source["source_event_id"])
                    state = record["state"]
                    stamp = datetime.fromisoformat(record["occurred_at"])
                    if (state not in {"PENDING_TARGET", "RECALLED"}
                        or record["memory_id"] != target + ":" + state
                        or stamp.utcoffset() is None
                        or stamp > datetime.fromisoformat(record["at"])
                        or (state == "PENDING_TARGET" and (
                            record["event_ids"] or record["memory_ids"] or record.get("event_id")
                        ))
                        or (state == "RECALLED" and (
                            record.get("event_id") != "evt_" + target.removeprefix("input_recall:")
                            or record["event_id"] not in record["event_ids"]
                        ))):
                        raise ValueError("invalid input recall target")
                if record.get("target_type") == "raw_retention" or (
                    record.get("target_type") == "input_recall" and "retention_cutoff" in record
                ):
                    cutoff = datetime.fromisoformat(record["retention_cutoff"])
                    if cutoff.utcoffset() is None or cutoff > datetime.fromisoformat(record["at"]):
                        raise ValueError("invalid retention cutoff")
                if record.get("target_type") == "source_grant" and (
                    not re.fullmatch(r"grant_[0-9a-f]{32}", record.get("source_grant_id", ""))
                    or record["source_grant_id"] != record["memory_id"]
                ):
                    raise ValueError("invalid source grant")
                if record.get("target_type") == "diary":
                    if date.fromisoformat(record["diary_day"]).isoformat() != record["diary_day"]:
                        raise ValueError("invalid diary date")
                    ZoneInfo(record["diary_timezone"])
            return lines
        except (ValueError, KeyError, OSError, TypeError) as error:
            raise CoreError("ERASURE_LEDGER_INVALID", 503) from error

    def append(self, store, memory_id, now):
        # Caller holds a DB write transaction, serializing ledger appends across API workers.
        rows = store.rows("SELECT memory_id,event_id FROM memory_evidence")
        memory_ids, event_ids = {memory_id}, set()
        relatives = store.rows("SELECT memory_id,supersedes_memory_id FROM memories")
        while True:
            previous = (len(memory_ids), len(event_ids))
            for row in relatives:
                if row["memory_id"] in memory_ids or row["supersedes_memory_id"] in memory_ids:
                    memory_ids.add(row["memory_id"])
                    if row["supersedes_memory_id"]:
                        memory_ids.add(row["supersedes_memory_id"])
            for row in rows:
                if row["memory_id"] in memory_ids:
                    event_ids.add(row["event_id"])
                if row["event_id"] in event_ids:
                    memory_ids.add(row["memory_id"])
            if previous == (len(memory_ids), len(event_ids)):
                break
        record = {
            "seq": len(self.read()),
            "erasure_id": uid("erase"),
            "memory_id": memory_id,
            "memory_ids": sorted(memory_ids),
            "event_ids": sorted(event_ids),
            "at": now,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encode(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.apply(store, record)

    def reset_affect(self, store, request_id, now):
        from companion.affect import apply_feedback_reset, reset_record, reset_target

        if not store.db.in_transaction:
            raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")
        target = reset_target(request_id)
        lines = self.read()
        record = next((item for item in lines[1:] if
            item.get("target_type") == "affect_reset" and item["memory_id"] == target), None)
        if record is None:
            record = reset_record(len(lines), request_id, now)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encode(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            store.audit("affect.reset", record["erasure_id"], "owner", now)
        result = apply_feedback_reset(store, record)
        self.record_complete(store, record)
        return result

    def reset_mood(self, store, request_id, now):
        from companion.mood import apply_reset, reset_record, reset_target

        if not store.db.in_transaction:
            raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")
        target = reset_target(request_id)
        lines = self.read()
        record = next((item for item in lines[1:] if
            item.get("target_type") == "mood_reset" and item["memory_id"] == target), None)
        if record is None:
            record = reset_record(len(lines), request_id, now)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encode(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            store.audit("mood.reset", record["erasure_id"], "owner", now)
        result = apply_reset(store, record)
        self.record_complete(store, record)
        return result

    def expire_events(self, store, event_ids, now, *, retention_cutoff=None):
        record = {
            "seq": len(self.read()),
            "erasure_id": uid("erase"),
            "memory_id": uid("retention"),
            "memory_ids": [],
            "event_ids": event_ids,
            "at": now,
            "target_type": "raw_retention" if retention_cutoff is not None else "event_batch",
        }
        if retention_cutoff is not None:
            record["retention_cutoff"] = retention_cutoff
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encode(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.apply(store, record)

    def forget_diary(self, store, day, timezone, now):
        target = "companion:" + timezone + ":" + day
        existing = store.one(
            "SELECT erasure_id FROM privacy_erasure_ledger "
            "WHERE target_type='diary' AND target_id=?",
            (target,),
        )
        if existing:
            record = next(
                row for row in self.read()[1:] if row["erasure_id"] == existing["erasure_id"]
            )
            if diary_copies(store, {(day, timezone)}, before=record["at"]):
                self.apply(store, record)
            return
        copies = diary_copies(store, {(day, timezone)})
        record = {
            "seq": len(self.read()),
            "erasure_id": uid("erase"),
            "memory_id": target,
            "memory_ids": [],
            "event_ids": sorted(copies),
            "at": now,
            "target_type": "diary",
            "diary_day": day,
            "diary_timezone": timezone,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encode(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.apply(store, record)

    def forget_source_grant(self, store, grant_id, now):
        events, memories = source_grant_closure(store, [row["event_id"] for row in store.rows(
            "SELECT event_id FROM source_grant_events WHERE grant_id=?", (grant_id,))])
        record = {
            "seq": len(self.read()), "erasure_id": uid("erase"),
            "memory_id": grant_id, "memory_ids": sorted(memories), "event_ids": sorted(events),
            "at": now, "target_type": "source_grant", "source_grant_id": grant_id,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(encode(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.apply(store, record)

    def recall_input(
        self, store, source, now, *, retention_cutoff=None, verified=False, occurred_at=None,
        event_id=None,
    ):
        from companion.input_recall import marker_key, matching_events, validate_source

        if not store.db.in_transaction:
            raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")
        validate_source(source)
        if retention_cutoff is not None:
            cutoff, stamp = datetime.fromisoformat(retention_cutoff), datetime.fromisoformat(now)
            if cutoff.utcoffset() is None or stamp.utcoffset() is None or cutoff > stamp:
                raise ValueError("INVALID_RECALL_RETENTION_CUTOFF")
        target, records = marker_key(source["source_event_id"]), self.read()
        if type(verified) is not bool:
            raise CoreError("INPUT_RECALL_VERIFICATION_REQUIRED", 403)
        if verified and event_id != "evt_" + target.removeprefix("input_recall:"):
            raise CoreError("INPUT_RECALL_TARGET_MISMATCH", 409)
        state = "RECALLED" if verified else "PENDING_TARGET"
        prior = [item for item in records[1:] if item.get("target_type") == "input_recall"
                 and item["recall_source"] == source]
        pending = next((item for item in prior if item["state"] == "PENDING_TARGET"), None)
        occurred_at = occurred_at or (pending["occurred_at"] if pending else now)
        stamp, received = datetime.fromisoformat(occurred_at), datetime.fromisoformat(now)
        if stamp.utcoffset() is None or received.utcoffset() is None or stamp > received:
            raise CoreError("INPUT_RECALL_TIME_INVALID", 400)
        if verified and pending and stamp != datetime.fromisoformat(pending["occurred_at"]):
            raise CoreError("INPUT_RECALL_TIME_CONFLICT", 409)
        # A crash can leave a fsynced record without its DB projection. Deduplicate
        # against the authority, not just privacy_erasure_ledger's unique target.
        record = next((item for item in prior if item["state"] == "RECALLED"), None)
        if record is None and not verified:
            record = pending
        duplicate = record is not None
        if record is None:
            roots = matching_events(store, source)
            if verified:
                existing = store.one("SELECT occurred_at FROM events WHERE event_id=?", (event_id,))
                if (roots and roots != {event_id}) or (existing and (
                    event_id not in roots or datetime.fromisoformat(existing["occurred_at"]) > stamp
                )):
                    raise CoreError("INPUT_RECALL_TARGET_MISMATCH", 409)
            events = self.retention_events(store, [event_id]) if verified else set()
            memories = {row["memory_id"] for row in store.rows(
                "SELECT memory_id FROM memory_evidence WHERE event_id IN "
                "(SELECT value FROM json_each(?))", (encode(sorted(events)),),
            )}
            record = {
                "seq": len(records), "erasure_id": uid("erase"), "memory_id": target + ":" + state,
                "memory_ids": sorted(memories), "event_ids": sorted(events), "at": now,
                "target_type": "input_recall", "recall_source": source,
                "state": state, "occurred_at": occurred_at,
                "event_id": event_id if verified else None,
            }
            if retention_cutoff is not None:
                record["retention_cutoff"] = retention_cutoff
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encode(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        applied = store.one("SELECT 1 FROM privacy_erasure_ledger WHERE erasure_id=?",
                            (record["erasure_id"],))
        if not applied or self.remaining_input_recall(store, record):
            self.apply_input_recall(store, record, repair=bool(applied))
        prefix = "erased_event:"
        events = sorted({row["key"][len(prefix):] for row in store.rows(
            "SELECT key FROM runtime_meta WHERE value=? AND substr(key,1,?)=?",
            (record["erasure_id"], len(prefix), prefix),
        )} | set(record["event_ids"]))
        return {"state": record["state"], "source_event_id": source["source_event_id"],
                "event_ids": events, "erasure_id": record["erasure_id"], "duplicate": duplicate}

    @staticmethod
    def erase_diary_projections(store, record):
        # Compatibility entry point; the diary module owns projection cleanup.
        from companion.diary import erase_diary_projections

        return erase_diary_projections(store, record)

    def replay(self, store):
        from companion.erasure import CONTRACT_KEY, contract_signature

        lines = self.read()
        applied = store.one(
            "SELECT MAX(restore_min_replay_version) AS n FROM privacy_erasure_ledger"
        )
        if (applied["n"] or 0) > len(lines) - 1:
            raise CoreError("ERASURE_LEDGER_TRUNCATED", 503)
        # Loading cleanup owners may import the SDK on a cold worker. Discover
        # the code-only contract before taking SQLite's single writer lock.
        signature = contract_signature()
        with store.transaction():
            previous = store.one("SELECT value FROM runtime_meta WHERE key=?", (CONTRACT_KEY,))
            repair_contracts = not previous or previous["value"] != signature
            for record in lines[1:]:
                if record.get("target_type") in {"affect_reset", "mood_reset"}:
                    # Rebuild the durable reset barrier even from an older backup.
                    self.apply(store, record)
                    continue
                # Rebuild this projection even for pre-upgrade COMPLETE records.
                # A backup may predate the event itself, so UPDATE events is insufficient.
                for event_id in record["event_ids"]:
                    store.execute("INSERT OR IGNORE INTO runtime_meta VALUES (?,?)",
                                  ("erased_event:" + event_id, record["erasure_id"]))
                applied = store.one(
                    "SELECT 1 FROM privacy_erasure_ledger WHERE erasure_id=?",
                    (record["erasure_id"],),
                )
                # Older releases marked deletion complete before resolving date aliases.
                # Recompute remaining copies even in snapshots taken after that deletion.
                remaining_diary_copies = record.get("target_type") == "diary" and diary_copies(
                    store, {(record["diary_day"], record["diary_timezone"])}, before=record["at"]
                )
                remaining_grant = False
                if record.get("target_type") == "source_grant":
                    from companion.source_grants import grant_tombstone

                    if applied and record["event_ids"] and not record["memory_ids"] and store.one(
                        "SELECT 1 FROM memories p JOIN memories c "
                        "ON c.supersedes_memory_id=p.memory_id WHERE p.status='FORGOTTEN' "
                        "AND (c.status<>'FORGOTTEN' OR c.content<>'') LIMIT 1"
                    ):
                        # Old COMPLETE records deleted the root-to-memory evidence without
                        # recording memory IDs. Preserve data for isolated owner review;
                        # neither restored publication nor inferred grant attribution is safe.
                        raise CoreError("SOURCE_GRANT_LEGACY_ERASURE_REVIEW_REQUIRED", 503)
                    remaining_grant = not store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                        (grant_tombstone(record["source_grant_id"]),)) or store.one(
                        "SELECT 1 FROM source_grant_events g JOIN events e USING(event_id) "
                        "WHERE g.grant_id=? AND e.redacted=0", (record["source_grant_id"],))
                    remaining_grant = remaining_grant or store.one(
                        "SELECT 1 FROM events WHERE redacted=0 AND event_id IN "
                        "(SELECT value FROM json_each(?))", (encode(record["event_ids"]),),
                    ) or store.one(
                        "SELECT 1 FROM memories WHERE memory_id IN "
                        "(SELECT value FROM json_each(?)) AND (content<>'' OR status<>'FORGOTTEN')",
                        (encode(record["memory_ids"]),),
                    )
                remaining_recall = record.get("target_type") == "input_recall" and (
                    self.remaining_input_recall(store, record)
                )
                if not applied or remaining_diary_copies or remaining_grant or remaining_recall:
                    if applied and record.get("target_type") == "input_recall":
                        self.apply_input_recall(store, record, repair=True)
                        continue
                    self.apply(store, record)
                elif repair_contracts:
                    self.repair_derivatives(store, record)
            # Publish only after every owner handler succeeded. A failure leaves
            # the durable Ledger unchanged and the entire replay transaction undone.
            if repair_contracts:
                store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                              (CONTRACT_KEY, signature))

    def repair_derivatives(self, store, record):
        """Upgrade COMPLETE projections once, using recorded source identities only."""
        from companion.diary import erase_diary_projections
        from companion.erasure import ErasureScope, erase_derivatives

        if not store.db.in_transaction:
            raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")

        if record.get("target_type") == "input_recall":
            self.apply_input_recall(store, record, repair=True)
            return

        if record.get("target_type") in {"affect_reset", "mood_reset"}:
            self.apply(store, record)
            return

        # Older apply() expanded dependencies only in memory, then deleted those
        # links. Its per-event tombstone still names this exact erasure, so use
        # that explicit attribution rather than guessing from content or dates.
        prefix = "erased_event:"
        recorded = {row["key"][len(prefix):] for row in store.rows(
            "SELECT key FROM runtime_meta WHERE value=? AND substr(key,1,?)=? "
            "AND length(key)>?", (record["erasure_id"], len(prefix), prefix, len(prefix)),
        )}
        roots = set(record["event_ids"]) | recorded
        if record.get("target_type") == "raw_retention":
            event_ids, memory_ids = self.retention_events(store, roots), set(record["memory_ids"])
        else:
            event_ids, memory_ids = source_grant_closure(store, roots, record["memory_ids"])
        scoped_record = {**record, "event_ids": sorted(event_ids),
                         "memory_ids": sorted(memory_ids)}
        copies = erase_diary_projections(store, scoped_record, conservative=False)
        if record.get("target_type") == "raw_retention":
            self.expire_memory_evidence(store, scoped_record)
        else:
            self.erase_memories(store, memory_ids, record["at"])
        event_ids |= copies
        erase_derivatives(store, ErasureScope(frozenset(event_ids), False))
        self.erase_events(store, record, event_ids)
        # COMPLETE repair has no proof that later unrelated summaries/replies came
        # from the forgotten fact. Never repeat the broad new-erasure fallback.
        self.clear_event_replies(store, event_ids)
        advance_epoch(store)

    def apply(self, store, record):
        from companion.erasure import ErasureScope, erase_derivatives
        from companion.observation import dependent_events

        if not store.db.in_transaction:
            raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")

        if record.get("target_type") == "input_recall":
            self.apply_input_recall(store, record)
            return

        if record.get("target_type") == "mood_reset":
            from companion.mood import apply_reset

            apply_reset(store, record)
            self.record_complete(store, record)
            return

        if record.get("target_type") == "affect_reset":
            from companion.affect import apply_feedback_reset

            apply_feedback_reset(store, record)
            self.record_complete(store, record)
            return

        if record.get("target_type") == "source_grant":
            from companion.reading import erase_reading
            from companion.source_grants import cancel_grant_reads, grant_tombstone

            grant_id = record["source_grant_id"]
            store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                          (grant_tombstone(grant_id), record["erasure_id"]))
            store.execute("UPDATE source_grants SET revoked_at=COALESCE(revoked_at,?),"
                          "forgotten_at=? WHERE grant_id=?", (record["at"], record["at"], grant_id))
            cancel_grant_reads(store, grant_id, record["at"])
            erase_reading(store, ())
            # Re-resolve the explicit relationship in the snapshot; never infer it from URLs.
            roots = set(record["event_ids"]) | {row["event_id"] for row in store.rows(
                "SELECT event_id FROM source_grant_events WHERE grant_id=?", (grant_id,))}
            events, memories = source_grant_closure(store, roots, record["memory_ids"])
            record = {**record, "event_ids": sorted(events), "memory_ids": sorted(memories)}
            if not events and not memories:
                advance_epoch(store)
                self.record_complete(store, record)
                return

        event_ids = (self.retention_events(store, record["event_ids"])
                     if record.get("target_type") == "raw_retention"
                     else dependent_events(store, record["event_ids"]))
        memory_ids = set(record["memory_ids"])
        for event_id in event_ids:
            memory_ids.update(
                row["memory_id"]
                for row in store.rows(
                    "SELECT memory_id FROM memory_evidence WHERE event_id=?", (event_id,)
                )
            )
        record = {**record, "event_ids": sorted(event_ids), "memory_ids": sorted(memory_ids)}
        copies = self.erase_diary_projections(store, record)
        if record.get("target_type") == "raw_retention":
            self.expire_memory_evidence(store, record)
        else:
            self.erase_memories(store, record["memory_ids"], record["at"])
        event_ids = set(record["event_ids"]) | copies
        erase_derivatives(store, ErasureScope(frozenset(event_ids),
                                             record.get("target_type") != "diary"))
        self.erase_events(store, record, event_ids)
        if record.get("target_type") == "diary":
            # Diary commands never enter chat context, memory extraction, or diary sources.
            self.clear_event_replies(store, event_ids)
        else:
            # No complete source lineage exists for these paraphrases. Keep the
            # existing conservative cleanup on every new raw-fact erasure.
            store.execute("DELETE FROM conversation_summaries")
            store.execute("UPDATE episodes SET title='',summary='',status='REDACTED'")
            store.execute("DELETE FROM trait_evidence")
            store.execute("DELETE FROM traits")
            store.execute(self.clear_reply_sql())
        advance_epoch(store)
        self.record_complete(store, record)

    @staticmethod
    def recall_support(store, memory_id, record):
        """A remaining positive source must have no erased, missing or expired ancestor."""
        events = encode(record["event_ids"])
        sources = store.rows(
            "SELECT e.event_id FROM memory_evidence v JOIN events e USING(event_id) "
            "JOIN conversations c USING(conversation_id) WHERE v.memory_id=? AND v.polarity=1 "
            "AND e.actor_id='owner' AND e.privacy_scope='private' AND c.scope='private' "
            "AND c.owner_actor_id='owner'", (memory_id,),
        )
        for source in sources:
            invalid = store.one(
                "WITH RECURSIVE sources(event_id) AS (SELECT ? UNION "
                "SELECT d.source_event_id FROM event_dependencies d "
                "JOIN sources s ON d.event_id=s.event_id) "
                "SELECT 1 FROM sources s LEFT JOIN events e USING(event_id) "
                "WHERE e.event_id IS NULL OR e.redacted=1 "
                "OR e.event_id IN (SELECT value FROM json_each(?)) "
                "OR (? IS NOT NULL AND e.ingested_at<?) "
                "OR EXISTS (SELECT 1 FROM runtime_meta r WHERE r.key='erased_event:'||s.event_id) "
                "LIMIT 1", (source["event_id"], events, record.get("retention_cutoff"),
                             record.get("retention_cutoff")),
            )
            if not invalid:
                return True
        return False

    def remaining_input_recall(self, store, record):
        from companion.input_recall import is_recalled, marker_key

        if record["state"] == "PENDING_TARGET":
            return not store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                                 (marker_key(record["recall_source"]["source_event_id"]),))
        if not is_recalled(store, record["recall_source"]["source_event_id"]):
            return True
        events = set(record["event_ids"])
        if store.one("SELECT 1 FROM events WHERE redacted=0 AND event_id IN "
                     "(SELECT value FROM json_each(?)) LIMIT 1", (encode(sorted(events)),)):
            return True
        if store.one("SELECT 1 FROM memory_evidence WHERE event_id IN "
                     "(SELECT value FROM json_each(?)) LIMIT 1", (encode(sorted(events)),)):
            return True
        return any(not self.recall_support(store, row["memory_id"], record)
                   for row in store.rows(
                       "SELECT memory_id FROM memories WHERE content<>'' AND memory_id IN "
                       "(SELECT value FROM json_each(?))", (encode(record["memory_ids"]),),
                   ))

    def apply_input_recall(self, store, record, *, repair=False):
        from companion.diary import erase_diary_projections
        from companion.erasure import ErasureScope, erase_derivatives
        from companion.input_recall import project_marker

        if not store.db.in_transaction:
            raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")
        project_marker(store, record)
        if record["state"] == "PENDING_TARGET":
            self.record_complete(store, record)
            return
        prefix = "erased_event:"
        recorded = {row["key"][len(prefix):] for row in store.rows(
            "SELECT key FROM runtime_meta WHERE value=? AND substr(key,1,?)=?",
            (record["erasure_id"], len(prefix), prefix),
        )}
        events = self.retention_events(store, set(record["event_ids"]) | recorded)
        memories = set(record["memory_ids"]) | {row["memory_id"] for row in store.rows(
            "SELECT memory_id FROM memory_evidence WHERE event_id IN "
            "(SELECT value FROM json_each(?))", (encode(sorted(events)),),
        )}
        scoped = {**record, "event_ids": sorted(events), "memory_ids": sorted(memories)}
        # A verified before-input notice has no content or work to revoke. Keep
        # the event/source tombstones without invalidating unrelated work.
        present = store.one("SELECT 1 FROM events WHERE event_id IN "
                            "(SELECT value FROM json_each(?)) LIMIT 1", (encode(sorted(events)),))
        if not present and not memories:
            self.erase_events(store, record, events)
            self.record_complete(store, record)
            return
        copies = erase_diary_projections(store, scoped, conservative=not repair)
        store.execute("DELETE FROM memory_evidence WHERE event_id IN "
                      "(SELECT value FROM json_each(?))", (encode(sorted(events)),))
        for memory_id in memories:
            store.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
            if not self.recall_support(store, memory_id, scoped):
                # Source withdrawal is not an explicit request to forget a fact's
                # family. Preserve every separately supported successor/fact.
                self.erase_memories(store, [memory_id], record["at"])
        events |= copies
        erase_derivatives(store, ErasureScope(frozenset(events), not repair))
        self.erase_events(store, record, events)
        self.clear_recalled_replies(store, events, record["at"], repair=repair)
        if not repair:
            # Historical replies/summaries lack complete source lineage. Keep
            # the existing conservative fallback until their owners supply it.
            store.execute("DELETE FROM conversation_summaries")
            store.execute("UPDATE episodes SET title='',summary='',status='REDACTED'")
            store.execute("DELETE FROM trait_evidence")
            store.execute("DELETE FROM traits")
        advance_epoch(store)
        self.record_complete(store, record)

    def clear_recalled_replies(self, store, event_ids, at, *, repair=False):
        """Invalidate replies from consumed sources without erasing their owner inputs."""
        if not store.one("SELECT 1 FROM sqlite_master WHERE name='job_sources' AND type='table'"):
            if repair:
                self.clear_event_replies(store, event_ids)
            else:
                store.execute(self.clear_reply_sql())
            return
        events = encode(sorted(event_ids))
        jobs = store.rows(
            "WITH RECURSIVE origins(event_id) AS (SELECT value FROM json_each(?) "
            "UNION SELECT event_id FROM jobs WHERE ? AND context_tracked=0 "
            "AND event_id IS NOT NULL UNION SELECT origin_event_id FROM outbox o "
            "WHERE ? AND origin_event_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM jobs j WHERE j.event_id=o.origin_event_id) "
            "UNION SELECT j.event_id FROM jobs j JOIN job_sources s USING(job_id) "
            "JOIN origins r ON r.event_id=s.event_id WHERE j.event_id IS NOT NULL) "
            "SELECT j.job_id,j.event_id FROM jobs j JOIN origins r USING(event_id)",
            (events, int(not repair), int(not repair)),
        )
        job_ids = encode([row["job_id"] for row in jobs])
        origins = encode(sorted(set(event_ids) | {row["event_id"] for row in jobs
                                                  if row["event_id"]}))
        store.execute(
            "UPDATE jobs SET state='CANCELLED',lease_token=NULL,updated_at=?,"
            "last_error_code='REPLY_SOURCE_RECALLED' WHERE job_id IN "
            "(SELECT value FROM json_each(?)) AND state IN ('QUEUED','RUNNING','RETRY_WAIT')",
            (at, job_ids),
        )
        store.execute(self.clear_reply_sql() + " WHERE origin_event_id IN "
                      "(SELECT value FROM json_each(?)) OR (? AND NOT EXISTS "
                      "(SELECT 1 FROM jobs j WHERE j.event_id=outbox.origin_event_id))",
                      (origins, int(not repair)))

    @staticmethod
    def retention_events(store, event_ids):
        """Expire derived originals too, including legacy QQ correction copies."""
        from companion.observation import dependent_events

        event_ids = set(event_ids)
        while True:
            previous = set(event_ids)
            event_ids = dependent_events(store, event_ids)
            # Older corrections predate event_dependencies but retained this
            # explicit source-to-successor relation. It is not a second source.
            for row in store.rows(
                "SELECT m.memory_id,m.supersedes_memory_id,m.content,e.body_json "
                "FROM memories m JOIN memory_evidence v USING(memory_id) "
                "JOIN events e USING(event_id) WHERE m.supersedes_memory_id IS NOT NULL "
                "AND e.source='qq.onebot' AND e.event_id IN (SELECT value FROM json_each(?))",
                (encode(sorted(event_ids)),),
            ):
                command, arguments = command_parts(json.loads(row["body_json"]).get("text", ""))
                if command == "纠正这一条" and arguments.split(maxsplit=1) == [
                    row["supersedes_memory_id"], row["content"],
                ]:
                    event_ids.update(item["event_id"] for item in store.rows(
                        "SELECT e.event_id FROM memory_evidence v JOIN events e USING(event_id) "
                        "WHERE v.memory_id=? AND e.source='owner.decision' "
                        "AND e.event_type='memory.corrected'", (row["memory_id"],),
                    ))
            if event_ids == previous:
                return event_ids

    @staticmethod
    def expire_memory_evidence(store, record):
        """Keep facts with independent live support; quarantine unsupported facts."""
        from companion.observation import expired_sources

        events = encode(record["event_ids"])
        affected = {row["memory_id"] for row in store.rows(
            "SELECT memory_id FROM memory_evidence WHERE event_id IN "
            "(SELECT value FROM json_each(?))", (events,),
        )} | set(record["memory_ids"])
        store.execute("DELETE FROM memory_evidence WHERE event_id IN "
                      "(SELECT value FROM json_each(?))", (events,))
        for memory_id in affected:
            sources = [row["event_id"] for row in store.rows(
                "SELECT e.event_id FROM memory_evidence v JOIN events e USING(event_id) "
                "JOIN conversations c USING(conversation_id) WHERE v.memory_id=? "
                "AND v.polarity=1 AND e.redacted=0 AND e.actor_id='owner' "
                "AND e.privacy_scope='private' AND c.scope='private' AND c.owner_actor_id='owner' "
                "AND NOT EXISTS (SELECT 1 FROM runtime_meta r "
                "WHERE r.key='erased_event:'||e.event_id)",
                (memory_id,),
            )]
            valid = any(not expired_sources(store, [source], record["retention_cutoff"])
                        for source in sources)
            store.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
            if not valid:
                store.execute(
                    "UPDATE memories SET confidence=0,needs_review=1,updated_at=?,"
                    "status=CASE WHEN status IN ('CONFIRMED','HYPOTHESIS') "
                    "THEN 'HYPOTHESIS' ELSE status END WHERE memory_id=? AND status<>'FORGOTTEN'",
                    (record["at"], memory_id),
                )

    @staticmethod
    def erase_memories(store, memory_ids, at):
        for memory_id in memory_ids:
            store.execute(
                "UPDATE memories SET content='',status='FORGOTTEN',fact_key='',"
                "confidence=0,updated_at=? WHERE memory_id=?",
                (at, memory_id),
            )
            store.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
            store.execute("DELETE FROM memory_evidence WHERE memory_id=?", (memory_id,))

    @staticmethod
    def erase_events(store, record, event_ids):
        for event_id in event_ids:
            store.execute("INSERT OR IGNORE INTO runtime_meta VALUES (?,?)",
                          ("erased_event:" + event_id, record["erasure_id"]))
            store.execute(
                "UPDATE events SET body_json='{}',redacted=1,memory_processed=1 WHERE event_id=?",
                (event_id,),
            )
            store.execute(
                "UPDATE jobs SET state='CANCELLED',lease_token=NULL,"
                "last_error_code='PRIVACY_CHANGED' "
                "WHERE event_id=? AND state IN ('QUEUED','RUNNING','RETRY_WAIT')",
                (event_id,),
            )
    @staticmethod
    def clear_reply_sql():
        return (
            "UPDATE outbox SET content_json='{}',content_sha256='',"
            "state=CASE WHEN state='SENDING' THEN 'DELIVERY_UNCERTAIN' "
            "WHEN state IN ('PENDING','LEASED','RETRY_WAIT') THEN 'CANCELLED' ELSE state END"
        )

    def clear_event_replies(self, store, event_ids):
        for event_id in event_ids:
            store.execute(self.clear_reply_sql() + " WHERE origin_event_id=?", (event_id,))

    @staticmethod
    def record_complete(store, record):
        store.execute(
            "INSERT OR IGNORE INTO privacy_erasure_ledger VALUES (?,?,?,?,?,?)",
            (
                record["erasure_id"],
                record.get("target_type", "memory"),
                record["memory_id"],
                record["at"],
                record["seq"],
                "COMPLETE",
            ),
        )
