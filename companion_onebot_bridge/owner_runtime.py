"""Owner authority projected into the existing exclusive Bridge Spool custody."""

import hashlib
import json
import re
import sqlite3
import stat
from datetime import UTC, datetime

from companion_delivery_protocol.authority import validate_authority_ref

from . import journal
from .onebot_client import bounded_post
from .spool import encode


def check_receipt(config, authority, purpose, result, now):
    known_policies = {"image", "file", "meme", "poke", "voice", "account_actions",
                      "bili_video", "rem", "qzone_publication"}
    policies = result.get("capability_policies") if isinstance(result, dict) else None
    expected_capture = {"public_key": config.capture_public_key,
        "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
        "protocol_epoch": config.protocol_epoch}
    if (not isinstance(result, dict)
        or set(result) != {"approved", "authority", "purpose", "expires_at",
                           "budget_journal_id", "capture", "capability_policies"}
        or not isinstance(policies, dict) or not policies.keys() <= known_policies
        or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
               for value in policies.values())
        or purpose in known_policies and purpose not in policies
        or result["approved"] is not True or result["authority"] != authority
        or result["purpose"] != purpose or result["capture"] != expected_capture
        or not isinstance(result["budget_journal_id"], str)
        or not re.fullmatch(r"budget_[a-f0-9]{32}", result["budget_journal_id"])):
        raise ValueError("OWNER_AUTHORITY_CHECK_REJECTED")
    if result["expires_at"] is not None:
        expiry = datetime.fromisoformat(result["expires_at"])
        if expiry.utcoffset() is None or now >= expiry:
            raise ValueError("OWNER_AUTHORITY_EXPIRED")
    return result


def command(config, args):
    import sqlite3
    from pathlib import Path

    from .spool import Spool

    authority = validate_authority_ref(config.owner_authority)
    directory = Path(config.data_dir)
    database = directory / "spool.sqlite3"
    if directory.is_symlink() or database.is_symlink() or not database.is_file():
        raise ValueError("OWNER_RUNTIME_NOT_FOUND")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        saved = db.execute("SELECT authority_json,state,journal_head FROM owner_runtime WHERE id=1"
                           ).fetchone()
        if saved is None or json.loads(saved[0]) != authority:
            raise ValueError("OWNER_AUTHORITY_BINDING_CHANGED")
        _, head = journal.read(directory / "owner-journal.jsonl")
        if saved[2] != head:
            raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
    marker = directory / ("owner-" + journal.digest(authority) + ".stop")
    if args.command == "owner-status":
        return {"state": "STOP_REQUESTED" if marker.exists() and saved[1] == "ACTIVE"
                else saved[1], "authority": authority, "journal_head": head}
    journal.stop_marker(marker)
    try:
        spool = Spool(config, reconciliation_only=True)
    except RuntimeError as error:
        if str(error) == "BRIDGE_ALREADY_RUNNING":
            return {"state": "STOP_REQUESTED", "authority": authority,
                    "core_stop_confirmed": False}
        raise
    with spool:
        runtime = OwnerRuntime(spool)
        runtime.stop()
        return {"state": "STOPPED", "authority": authority, "core_stop_confirmed": False}


class OwnerRuntime:
    def __init__(self, spool, *, clock=None, legacy=None):
        self.spool, self.db, self.config = spool, spool.db, spool.config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.authority = validate_authority_ref(self.config.owner_authority)
        self.path = spool.directory / "owner-journal.jsonl"
        self.stop_path = spool.directory / ("owner-" + journal.digest(self.authority) + ".stop")
        self.legacy = legacy
        # Chain hashes of PROFILE_PROBE_ACCEPTED records whose shape already validated.
        self._valid_profiles = set()
        # Old evidence must be inspected by explicit cutover before any v2 writer starts.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='test_window'").fetchone():
            if legacy is not None:
                from . import owner_cutover

                if owner_cutover.inspect(spool) != legacy:
                    raise ValueError("OWNER_LEGACY_CUSTODY_CHANGED")
            elif not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='owner_runtime'").fetchone():
                raise ValueError("OWNER_CUSTODY_CUTOVER_REQUIRED")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS owner_runtime("
            "id INTEGER PRIMARY KEY CHECK(id=1),authority_json TEXT NOT NULL,"
            "state TEXT NOT NULL,journal_head TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS window_attempts(message_id TEXT PRIMARY KEY,"
            "event_id TEXT NOT NULL,fence INTEGER NOT NULL,state TEXT NOT NULL,"
            "text_hash TEXT NOT NULL,approval_hash TEXT NOT NULL,provider_ack_hash TEXT,"
            "item_hash TEXT NOT NULL,plan_hash TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS window_inputs(event_id TEXT PRIMARY KEY,"
            "input_hash TEXT NOT NULL,approval_hash TEXT NOT NULL,state TEXT NOT NULL,"
            "capture_json TEXT,plan_hash TEXT NOT NULL)"
        )
        row = self.row()
        if row is None:
            if legacy is None and self.db.execute(
                "SELECT 1 FROM inbox UNION ALL SELECT 1 FROM results LIMIT 1").fetchone():
                raise ValueError("OWNER_CUSTODY_CUTOVER_REQUIRED")
            head = journal.append(self.path, "OWNER_BOUND", {"version": 2,
                "authority": self.authority, "capture_public_key": self.config.capture_public_key,
                **({"legacy_custody": legacy} if legacy is not None else {})},
                at=self.clock().isoformat(),
                expected_head="0" * 64, create=True)
            with self.db:
                self.db.execute("INSERT INTO owner_runtime VALUES(1,?,'ACTIVE',?)",
                                (encode(self.authority), head))
        self.verify(active=False)
        from companion_delivery_protocol.preflight import KIND

        for message_id, attempt, body, state in self.db.execute(
            "SELECT a.message_id,a.state,r.body,r.state FROM window_attempts a "
            "LEFT JOIN results r USING(message_id) WHERE a.plan_hash=? "
            "AND a.state IN ('RESERVED','IO_STARTED','UNKNOWN')", (journal.digest(self.authority),)
        ):
            if (attempt == "RESERVED" and state == "pending"
                and json.loads(body).get("result") == KIND):
                from . import owner_preflight

                owner_preflight.validate(self, message_id, json.loads(body))
                continue  # Only its signed cancellation RPC can be resumed.
            self.stop()
            break

    def row(self):
        row = self.db.execute(
            "SELECT authority_json,state,journal_head FROM owner_runtime WHERE id=1"
        ).fetchone()
        return None if row is None else dict(zip(("authority_json", "state", "journal_head"),
                                                row, strict=True))

    @property
    def memes(self):
        self.verify(active=False)
        if getattr(self, "_meme_retired", None) and self.config.meme_catalog_owner != "core":
            raise ValueError("MEME_CATALOG_RETIRED")
        if not hasattr(self, "_memes"):
            if self.config.meme_catalog_owner == "core":
                from .meme_transport import CoreMemeTransport

                self._memes = CoreMemeTransport(self)
            else:
                from .meme_runtime import MemeRuntime

                self._memes = MemeRuntime(self)
        return self._memes

    def _custody_key(self, row):
        """Exact inputs of the journal/database replay; any change forces a full verify.

        `total_changes` counts this connection's row writes (committed or not),
        `schema_version` its table changes, and `data_version` changes after any other
        connection commits, so the key is equal only when neither the journal bytes nor
        the spool database changed.
        """
        try:
            info = self.path.lstat()
        except OSError:
            return None
        if self.path.is_symlink() or not stat.S_ISREG(info.st_mode):
            return None
        try:
            versions = (self.db.execute("PRAGMA schema_version").fetchone()[0],
                        self.db.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.DatabaseError:
            # A caller-installed authorizer (e.g. the meme accounting drain) may deny
            # these reads; such callers always get the original full verification.
            return None
        return (self.path.read_bytes(), tuple(row.values()), self.db.total_changes, *versions)

    def verify(self, *, active=True):
        """Revalidate custody; an unchanged journal and database reuse the last full proof.

        The legacy cutover evidence reads other files and is always rechecked, as are
        the binding, the persisted state and the stop marker.
        """
        row = self.row()
        if row is None or json.loads(row["authority_json"]) != self.authority:
            raise ValueError("OWNER_AUTHORITY_BINDING_CHANGED")
        key = self._custody_key(row)
        cached = getattr(self, "_verified", None)
        if key is not None and cached is not None and cached[0] == key:
            records, state, references = cached[1], cached[2], cached[3]
        else:
            self._verified = None
            records, state, references = self._replay(row)
            if key is not None and self._custody_key(row) == key:
                self._verified = (key, records, state, references)
        if self.legacy is not None:
            from . import owner_cutover

            owner_cutover.verify(self, self.legacy, references)
        if row["state"] != state or references[-1] != self.authority:
            raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
        if active and (state != "ACTIVE" or self.spool.reconciliation_only
                       or self.stop_path.exists() or self.stop_path.is_symlink()):
            raise ValueError("OWNER_RUNTIME_STOPPED")
        # A new list; records are shared read-only values between unchanged proofs.
        return list(records)

    def _replay(self, row):
        records, head = journal.read(self.path)
        if (not records or head != row["journal_head"]
            or records[0]["kind"] != "OWNER_BOUND"
            or set(records[0]["data"]) not in (
                {"version", "authority", "capture_public_key"},
                {"version", "authority", "capture_public_key", "legacy_custody"})
            or records[0]["data"]["version"] != 2
            or records[0]["data"]["capture_public_key"] != self.config.capture_public_key):
            raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
        current = validate_authority_ref(records[0]["data"]["authority"])
        self.legacy = records[0]["data"].get("legacy_custody")
        if (self.db.execute("SELECT 1 FROM sqlite_master WHERE name='test_window'").fetchone()
            and self.legacy is None):
            raise ValueError("OWNER_CUSTODY_CUTOVER_REQUIRED")
        references = [current]
        state = "ACTIVE"
        attempts = {}
        bindings = {}
        inputs = {}
        retirement = None
        for index, record in enumerate(records[1:], start=1):
            if record["kind"] == "OWNER_STOPPED":
                if record["data"] != {"authority": current}:
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
                state = "STOPPED"
            elif record["kind"] == "OWNER_REBOUND":
                value = record["data"]
                successor = validate_authority_ref(value["authority"])
                if (state != "STOPPED" or value["previous_authority"] != current
                    or successor["grant_id"] != current["grant_id"]
                    # The exact prepared Core STOP (direct or unbound chain) is checked by
                    # owner_handoff.verify; here the parity keeps ACTIVE references.
                    or successor["revision"] <= current["revision"]
                    or (successor["revision"] - current["revision"]) % 2
                    or successor["restore_generation"] != current["restore_generation"]):
                    raise ValueError("OWNER_HANDOFF_BINDING_CHANGED")
                current = successor
                references.append(current)
                state = "ACTIVE"
            elif record["kind"] == "MEME_CATALOG_RETIRED":
                from .meme_isolation import validate_record

                if retirement is not None:
                    raise ValueError("MEME_RETIREMENT_CONFLICT")
                retirement = validate_record(record["data"], current, state)
            elif record["kind"] == "RESERVED":
                value = record["data"]
                if (state != "ACTIVE" or value["message_id"] in attempts
                    or value["plan_hash"] != journal.digest(current)):
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
                attempts[value["message_id"]] = {
                    key: item for key, item in value.items()
                    if key not in {"delivery_binding", "reply_segment"}}
                bindings[value["message_id"]] = value.get("delivery_binding", {})
            elif record["kind"] == "CAPTURED":
                value = record["data"]
                if value["event_id"] in inputs:
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
                inputs[value["event_id"]] = value["capture_sha256"]
            elif record["kind"] == "INPUT_HELD":
                if state != "ACTIVE":
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
            elif record["kind"] == "INPUT_HOLD_CLOSED":
                pass  # Checked against the held_inputs projection below.
            elif record["kind"] == "PROFILE_PROBE_STARTED":
                if state != "ACTIVE" or record["data"] != {"authority": current}:
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
            elif record["kind"] == "PROFILE_PROBE_ACCEPTED":
                from .profile_probe import validate_record

                # The chain hash commits to the record's bytes, so a record that already
                # passed this pure shape check needs no second one; live journals hold
                # thousands of these and re-checking them dominated every replay.
                own = records[index + 1]["previous"] if index + 1 < len(records) else head
                if own not in self._valid_profiles:
                    validate_record(record["data"])
                    self._valid_profiles.add(own)
            elif record["kind"] in {"RECALL_RECEIVED", "RECALL_ACCEPTED",
                                    "CORE_STOP_CONFIRMED", "DELIVERY_QUIESCED", "OWNER_RECONCILED",
                                    "OWNER_INPUT_TERMINAL", "OWNER_HANDOFF_PREPARED",
                                    "OWNER_INPUT_CARRIED", "OWNER_CARRY_HANDOFF_PREPARED",
                                    "MEDIA_QUEUED", "MEDIA_STATE"}:
                pass  # The original durable notice projection is checked below.
            elif record["kind"] in {"IO_STARTED", "ACK", "UNKNOWN", "NOT_SENT"}:
                value = record["data"]
                attempt = attempts.get(value["message_id"])
                if attempt is None or attempt["state"] not in {"RESERVED", "IO_STARTED"}:
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
                if ((record["kind"] == "ACK" and attempt["state"] != "IO_STARTED")
                    or (record["kind"] == "IO_STARTED"
                        and (attempt["state"] != "RESERVED" or state != "ACTIVE"))):
                    raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
                before_state = attempt["state"]
                attempt.update(state=record["kind"], provider_ack_hash=value["provider_ack_hash"])
                if record["kind"] == "NOT_SENT":
                    from companion_delivery_protocol import no_io

                    receipt = {"cancelled": True, "message_id": value["message_id"],
                               "state": "CANCELLED", "send_attempt_retained": True}
                    result = self.db.execute("SELECT body,state FROM results WHERE message_id=?",
                                              (value["message_id"],)).fetchone()
                    body = json.loads(result[0]) if result else {}
                    binding = bindings[value["message_id"]]
                    if value.get("core_receipt", {}).get("kind") == no_io.KIND:
                        receipt = no_io.receipt(value["core_receipt"])
                        proof = receipt["quiescence"]
                        if (before_state != "RESERVED" or proof["authority"] != current
                            or proof["message_id"] != value["message_id"]
                            or body != {"result": "NOT_SENT",
                                        "lease_sha256": binding["lease_sha256"],
                                        "core_receipt": receipt}):
                            raise ValueError("OWNER_NOT_SENT_PROOF_REQUIRED")
                    elif binding.get("payload_kind") == "account_payload_sha256":
                        from .account_actions import cancelled_proof

                        receipt = cancelled_proof(value.get("core_receipt"),
                            message_id=value["message_id"], lease_token=body.get("lease_token"),
                            content_sha256=binding["content_sha256"],
                            payload_sha256=binding["payload_sha256"])
                        if journal.digest(body.get("lease_token")) != binding["lease_sha256"]:
                            raise ValueError("OWNER_NOT_SENT_PROOF_REQUIRED")
                    if (value.get("core_receipt") != receipt
                        or not result or result[1] != "accepted"
                        or body.get("core_receipt") != receipt or body.get("result") != "NOT_SENT"
                        or value["provider_ack_hash"] is not None):
                        raise ValueError("OWNER_NOT_SENT_PROOF_REQUIRED")
            else:
                raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
        hashes = [journal.digest(ref) for ref in references]
        if retirement is not None:
            from .meme_isolation import verify_projection

            verify_projection(self.db, retirement)
        self._meme_retired = retirement
        condition = " WHERE plan_hash IN (" + ",".join("?" for _ in hashes) + ")"
        cursor = self.db.execute("SELECT * FROM window_attempts"
            + (condition if self.legacy else "") + " ORDER BY fence", hashes if self.legacy else ())
        names = [column[0] for column in cursor.description]
        actual = [dict(zip(names, row, strict=True)) for row in cursor]
        if actual != list(attempts.values()):
            raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
        actual_inputs = self._inputs(references)
        if actual_inputs != inputs:
            raise ValueError("OWNER_JOURNAL_RECONCILIATION_REQUIRED")
        from . import held_inputs, owner_notices

        owner_notices.verify(self, records, references=references)
        held_inputs.verify(self, records, references=references)
        from . import pending_media

        pending_media.verify_owner_history(self, records)
        from . import owner_native_resolution

        owner_native_resolution.verify(self, records)
        from . import owner_handoff

        owner_handoff.verify(self, records)
        return records, state, references

    def _inputs(self, references):
        from . import owner_pokes
        from .rich_input import input_digest

        inputs = {}
        hashes = [journal.digest(ref) for ref in references]
        condition = " WHERE w.plan_hash IN (" + ",".join("?" for _ in hashes) + ")"
        for row in self.db.execute(
            "SELECT w.event_id,w.input_hash,w.approval_hash,w.state,w.capture_json,w.plan_hash,"
            "i.body,i.state FROM window_inputs w LEFT JOIN inbox i ON i.key=w.event_id"
            + (condition if self.legacy else ""), hashes if self.legacy else (),
        ):
            event_id, input_hash, approval, state, capture, authority, body, inbox_state = row
            receipt = json.loads(capture)
            digest = journal.digest(receipt)
            source_hash = (journal.digest(receipt["notice"])
                           if receipt["kind"] == "authenticated_onebot_poke"
                           else receipt["input_sha256"])
            if (receipt["event_id"] != event_id or receipt["authority"] not in references
                or source_hash != input_hash or approval != digest
                or authority != journal.digest(receipt["authority"]) or state != "CAPTURED"
                or inbox_state is None or inbox_state == "pending" and body is None):
                raise ValueError("OWNER_CAPTURE_PROJECTION_CHANGED")
            if body is not None and not owner_pokes.check_body(receipt, body):
                event = json.loads(body)
                if (event["event_id"] != event_id
                    or event["source_event_id"] != receipt["source_event_id"]
                    or input_digest(event["payload"]) != input_hash):
                    raise ValueError("OWNER_CAPTURE_PROJECTION_CHANGED")
            inputs[event_id] = digest
        return inputs

    def append(self, kind, data):
        self.verify(active=False)
        head = journal.append(self.path, kind, data, at=self.clock().isoformat(),
                              expected_head=self.row()["journal_head"])
        # The caller commits this head with its native inbox/attempt projection.
        self.db.execute("UPDATE owner_runtime SET journal_head=? WHERE id=1", (head,))
        return head

    def stop(self):
        self.verify(active=False)
        journal.stop_marker(self.stop_path)
        if self.row()["state"] != "STOPPED":
            with self.db:
                self.append("OWNER_STOPPED", {"authority": self.authority})
                self.db.execute("UPDATE owner_runtime SET state='STOPPED' WHERE id=1")
        from . import held_inputs, pending_media

        held_inputs.close_all(self, "OWNER_STOPPED")
        pending_media.stop(self)

    async def check(self, core_client, *, purpose="chat"):
        self.verify()
        result = await bounded_post(core_client, "/internal/v1/owner-authority/check",
                                    {"authority": self.authority, "purpose": purpose})
        check_receipt(self.config, self.authority, purpose, result, self.clock())
        if self.legacy and result["budget_journal_id"] != self.legacy["budget_journal_id"]:
            raise ValueError("OWNER_LEGACY_BUDGET_CUSTODY_CHANGED")
        self.verify()
        return result

    async def probe(self, core_client, protocol):
        from .event_normalizer import decode
        from .network import checked_http_client, private_request_matches, seal_protocol_content
        from .onebot_client import OneBotClient

        async def boundary(request):
            def validate():
                if (request.method != "POST"
                    or request.url.path not in {
                        "/get_login_info", "/get_status", "/get_version_info"}
                    or not private_request_matches(request, self.config.onebot_http_url,
                                                   request.url.path)
                    or request.headers.get("Authorization") != "Bearer " + self.config.onebot_token
                    or decode(request.content, 64) != {}):
                    raise ValueError("OWNER_PROTOCOL_REQUEST_CHANGED")

            validate()
            await self.check(core_client)
            validate()
            seal_protocol_content(request, b"{}")

        result = await OneBotClient(self.config, checked_http_client(protocol, boundary)).probe()
        self.verify()
        return result

    def persist_capture(self, event, frame, *, received_at, peer, media=None, backfill=False):
        """Called only by the authenticated receive boundary after input preparation.

        `backfill` marks an owner message read back through the protocol's authenticated
        history API after a restart; the 30 s freshness bound is unchanged.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from companion_delivery_protocol.authority import capture_signing_payload

        from .event_normalizer import decode, identifier, message_identity
        from .rich_input import input_digest

        self.verify()
        raw = decode(frame, self.config.max_frame_bytes, max_depth=20)
        source, event_id = message_identity(self.config, raw["message_id"])
        if (raw.get("post_type") != "message" or raw.get("message_type") != "private"
            or raw.get("sub_type") != "friend" or "group_id" in raw
            or identifier(raw.get("self_id")) != self.config.bot_qq_id
            or identifier(raw.get("user_id")) != self.config.owner_qq_id
            or not isinstance(raw.get("sender"), dict) or "group_id" in raw["sender"]
            or identifier(raw["sender"].get("user_id")) != self.config.owner_qq_id
            or (event["source_event_id"], event["event_id"]) != (source, event_id)
            or event["conversation"]["platform_session_key"] != self.config.origin):
            raise ValueError("OWNER_CAPTURE_IDENTITY_CHANGED")
        occurred_at = datetime.fromisoformat(event["occurred_at"])
        if (not 0 <= (received_at - occurred_at).total_seconds() <= 30
            or (received_at - self.clock()).total_seconds() > 2
            or raw["message_id"] in (0, "0")):
            raise ValueError("OWNER_CAPTURE_TIME_CHANGED")
        fingerprint = journal.digest({"message": raw["message"], "time": raw["time"]})
        prior = self.db.execute("SELECT fingerprint FROM inbox WHERE key=?",
                                (event["event_id"],)).fetchone()
        if prior:
            if prior[0] != fingerprint:
                raise ValueError("SOURCE_EVENT_CONFLICT")
            return False
        frame_bytes = frame.encode() if isinstance(frame, str) else frame
        kind = "onebot_history_backfill" if backfill else "authenticated_onebot_ws"
        receipt = {"authority": self.authority, "kind": kind,
            "event_id": event["event_id"], "source_event_id": event["source_event_id"],
            "input_sha256": input_digest(event["payload"]),
            "frame_sha256": hashlib.sha256(frame_bytes).hexdigest(),
            "received_at": received_at.isoformat(), "occurred_at": event["occurred_at"],
            "protocol_epoch": self.config.protocol_epoch,
            "identity_sha256": journal.digest([self.config.owner_qq_id, self.config.bot_qq_id])}
        receipt["evidence_sha256"] = journal.digest({"receipt": receipt, "peer": list(peer),
            "endpoint": (self.config.onebot_http_url.rstrip("/") + "/get_friend_msg_history"
                         if backfill else self.config.onebot_ws_url), "bearer_accepted": True})
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.config.capture_signing_key))
        receipt["signature"] = key.sign(capture_signing_payload(receipt)).hex()
        body = encode(event)
        if self.spool.pending_count >= self.config.max_pending:
            raise ValueError("SPOOL_QUEUE_CAPACITY")
        self.spool.capacity(len(body.encode()) * 3 + len(encode(receipt)))
        self.verify()
        with self.db:
            self.append("CAPTURED", {"event_id": event["event_id"],
                                    "capture_sha256": journal.digest(receipt)})
            self.db.execute("INSERT INTO window_inputs VALUES(?,?,?,'CAPTURED',?,?)",
                (event["event_id"], receipt["input_sha256"], journal.digest(receipt),
                 encode(receipt), journal.digest(self.authority)))
            self.db.execute("INSERT INTO inbox(key,fingerprint,body,trace) VALUES(?,?,?,?)",
                (event["event_id"], fingerprint, body, event["trace_id"]))
            if media is not None:
                from . import pending_media

                pending_media.queue_owner(self, event, media, receipt, raw)
        return True

    async def flush_one(self, client):
        from . import owner_notices

        while await owner_notices.flush_one(self, client):
            pass
        self.verify()
        row = self.db.execute(
            "SELECT i.key,w.capture_json,i.body FROM inbox i JOIN window_inputs w "
            "ON w.event_id=i.key WHERE i.state='pending' AND w.plan_hash=? "
            "ORDER BY i.rowid LIMIT 1", (journal.digest(self.authority),)).fetchone()
        if row is None:
            return False
        receipt = json.loads(row[1])
        if receipt["kind"] == "authenticated_onebot_poke":
            from . import owner_pokes

            return await owner_pokes.flush(self, client, receipt)
        approval = receipt
        if self.config.meme_enabled and not self.config.meme_semantics_enabled:
            permission = await self.check(client)
            if "meme" in permission["capability_policies"]:
                await self.check(client, purpose="meme")
                await self.check(client, purpose="expression")
                offer = self.memes.prepare_offer(json.loads(row[2]), receipt)
                if offer is not None:
                    approval = {**receipt, "meme_offer": offer}
        response = await bounded_post(
            client, "/internal/v1/owner-authority/inputs/approve", approval)
        if (set(response) != {"approved", "duplicate"} or response["approved"] is not True
            or type(response["duplicate"]) is not bool):
            raise ValueError("OWNER_CAPTURE_APPROVAL_REJECTED")
        self.verify()
        return await self.spool.flush_one(client)
