"""Native authority guard for the common leased-item dispatch lifecycle."""

import base64
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

from companion_delivery_protocol.authority import validate_authority_ref

from . import account_actions, journal, owner_notices, reply_segments
from .event_normalizer import decode
from .network import checked_http_client, private_request_matches, seal_protocol_content
from .onebot_client import OneBotClient, bounded_post


class OwnerSendGuard:
    def __init__(self, runtime, core_client):
        self.runtime, self.client = runtime, core_client
        self.config, self.db = runtime.config, runtime.db
        self.authority = dict(runtime.authority)
        self.current_item = None
        self.dispatched = False
        self.prepared_message = self.prepared_voice = self.prepared_account = None
        self.prepared_native = None
        self.item_hash = None
        self.delivery_binding = None
        self.lease_until = None
        self.meme_prepared = self.meme_sending = False
        self.message_hash = None
        self.segment_not_before = None

    def _segment_deadline(self, *, event_id=None):
        records = self.runtime.verify()
        prior = self.db.execute("SELECT message_id,event_id,state FROM window_attempts "
            "WHERE plan_hash=? ORDER BY fence DESC LIMIT 1",
            (journal.digest(self.authority),)).fetchone()
        if not prior or prior[2] != "ACK" or event_id is not None and event_id != prior[1]:
            return None
        latest = self.db.execute("SELECT event_id FROM window_inputs WHERE plan_hash=? "
            "AND json_extract(capture_json,'$.kind') IN "
            "('authenticated_onebot_ws','onebot_history_backfill') "
            "ORDER BY rowid DESC LIMIT 1", (journal.digest(self.authority),)).fetchone()
        if latest and latest[0] != prior[1]:
            return None
        if (self.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'").fetchone()
            and self.db.execute("SELECT 1 FROM automatic_recalls WHERE target_event_id=? LIMIT 1",
                                (prior[1],)).fetchone()):
            return None
        reserved = next(record for record in reversed(records) if record["kind"] == "RESERVED"
                        and record["data"]["message_id"] == prior[0])
        ack = next(record for record in reversed(records) if record["kind"] == "ACK"
                   and record["data"]["message_id"] == prior[0])
        if "reply_segment" not in reserved["data"]:
            # Pre-migration ACKs lack a manifest; the established maximum interval
            # bounds this conservative wait without inventing a success receipt.
            interval = 3000
        else:
            segment = reserved["data"]["reply_segment"]
            if segment is None or segment["index"] >= segment["count"] - 1:
                return None
            interval = segment["interval_ms"]
        return datetime.fromisoformat(ack["at"]) + timedelta(milliseconds=interval)

    def waiting_for_segment(self):
        deadline = self._segment_deadline()
        self.segment_not_before = (deadline if deadline is not None
                                   and self.runtime.clock() < deadline else None)
        return self.segment_not_before is not None

    def reserve(self, item):
        from .owner_native_resolution import binding

        self.runtime.verify()
        if self.current_item is not None or validate_authority_ref(
            item.get("authority")
        ) != self.authority:
            raise ValueError("OWNER_DELIVERY_BINDING_CHANGED")
        if self.db.execute("SELECT 1 FROM window_attempts WHERE message_id=?",
                           (item["message_id"],)).fetchone():
            raise ValueError("DELIVERY_ALREADY_ATTEMPTED")
        lease_until = datetime.fromisoformat(item["lease_until"])
        if lease_until.utcoffset() is None or self.runtime.clock() >= lease_until:
            raise ValueError("OWNER_DELIVERY_LEASE_LOST")
        event_id = (item["content"]["plan_id"] if account_actions.is_account(item)
                    else reply_segments.event_id(item))
        self.segment_not_before = self._segment_deadline(event_id=event_id)
        if self.segment_not_before is not None and self.runtime.clock() < self.segment_not_before:
            raise ValueError("REPLY_SEGMENT_INTERVAL_REQUIRED")
        segment = reply_segments.envelope(item) if not account_actions.is_account(item) else None
        value = {"message_id": item["message_id"], "event_id": event_id,
            "fence": self.db.execute("SELECT COALESCE(MAX(fence),0)+1 FROM window_attempts"
                                     ).fetchone()[0],
            "state": "RESERVED", "text_hash": journal.digest(item["content"]),
            "approval_hash": journal.digest(self.authority), "provider_ack_hash": None,
            "item_hash": journal.digest(item), "plan_hash": journal.digest(self.authority)}
        self.delivery_binding = binding(self, item)
        self.message_hash = journal.digest(self.prepared_message)
        with self.db:
            self.runtime.append("RESERVED", value | {"delivery_binding": self.delivery_binding,
                "reply_segment": reply_segments.journal_value(segment, item["content"]["text"])
                if segment is not None else None})
            self.db.execute("INSERT INTO window_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                            tuple(value.values()))
        self.current_item, self.item_hash, self.lease_until = item, value["item_hash"], lease_until

    def _local(self):
        self.runtime.verify()
        if self.current_item is None:
            raise ValueError("OWNER_DELIVERY_SEND_FENCED")
        if not account_actions.is_account(self.current_item) and owner_notices.recalled(
            self.runtime, reply_segments.event_id(self.current_item)
        ):
            raise ValueError("OWNER_INPUT_RECALLED")
        if self.pending_input():
            raise ValueError("OWNER_INPUT_PENDING")
        if self.segment_not_before is not None and self.runtime.clock() < self.segment_not_before:
            raise ValueError("REPLY_SEGMENT_INTERVAL_REQUIRED")
        # Journal reads above may block while STOP or lease expiry arrives.
        # Keep the immediate state fence after every read, immediately before I/O.
        if (self.runtime.row()["state"] != "ACTIVE" or self.runtime.stop_path.exists()
            or self.runtime.stop_path.is_symlink()):
            raise ValueError("OWNER_RUNTIME_STOPPED")
        if (self.current_item is None or self.item_hash != journal.digest(self.current_item)
            or self.dispatched or self.runtime.clock() >= self.lease_until
            or journal.digest(self.prepared_message) != self.message_hash):
            raise ValueError("OWNER_DELIVERY_SEND_FENCED")

    def pending_input(self):
        item = self.current_item
        if not item or not ("reply_revision" in item
            or item.get("delivery_key", "").startswith("poke-response:")
            or item.get("action_type") == "qq.private.proactive"
            or account_actions.is_account(item)):
            return None
        event_id = (item["content"]["plan_id"] if account_actions.is_account(item)
                    else reply_segments.event_id(item))
        row = self.db.execute("SELECT i.key FROM inbox i JOIN window_inputs w ON w.event_id=i.key "
            "WHERE i.state='pending' AND w.plan_hash=? AND i.key!=? "
            "AND json_extract(w.capture_json,'$.kind') IN "
            "('authenticated_onebot_ws','onebot_history_backfill') "
            "ORDER BY i.rowid LIMIT 1", (journal.digest(self.authority),
                                        event_id)).fetchone()
        return row[0] if row else None

    def _request(self, request):
        item = self.current_item
        if (request.method != "POST" or request.headers.get("Authorization")
            != "Bearer " + self.config.onebot_token
            or not private_request_matches(request, self.config.onebot_http_url,
                                           request.url.path)):
            raise ValueError("OWNER_PROTOCOL_REQUEST_CHANGED")
        if request.url.path in {"/get_login_info", "/get_status", "/get_version_info"}:
            if decode(request.content, 64) != {}:
                raise ValueError("OWNER_PROTOCOL_REQUEST_CHANGED")
            return False
        if account_actions.is_account(item):
            prepared = self.prepared_account
            expected = account_actions.prepare_owner(
                self.runtime, item, item["content"]["authorization_sha256"])
            if (prepared != expected or request.url.path != prepared.path
                or request.content != prepared.body):
                raise ValueError("OWNER_PROTOCOL_REQUEST_CHANGED")
            return True
        if item["action_type"] == "qq.private.poke":
            from .native_actions import prepare

            expected = prepare(item["content"], self.config.owner_qq_id)
            if (self.prepared_native != expected or request.url.path != expected.path
                or request.content != expected.body):
                raise ValueError("OWNER_PROTOCOL_REQUEST_CHANGED")
            return True
        if item["action_type"] not in {"qq.private.reply", "qq.private.proactive"}:
            raise ValueError("OWNER_NATIVE_PREPARATION_REQUIRED")
        message = (self.prepared_voice.message if self.prepared_voice is not None
                   else self.prepared_message or [
                       {"type": "text", "data": {"text": reply_segments.wire_text(item)}}])
        expected = {"user_id": int(self.config.owner_qq_id), "message": message,
                    "auto_escape": True}
        if request.url.path != "/send_private_msg" or decode(
            request.content, self.config.max_voice_lease_bytes, max_depth=20
        ) != expected:
            raise ValueError("OWNER_PROTOCOL_REQUEST_CHANGED")
        return True

    async def before_dispatch(self, request):
        self._local()
        sending = self._request(request)
        if not sending:
            await self.runtime.check(self.client)
            self._request(request)
            self._local()
            seal_protocol_content(request, request.content)
            return
        while await owner_notices.flush_one(self.runtime, self.client):
            pass
        item = self.current_item
        account = account_actions.is_account(item)
        check = (account_actions.send_check_owner(self.authority, item) if account else {
            "authority": self.authority, "message_id": item["message_id"],
            "event_id": reply_segments.event_id(item), "lease_token": item["lease_token"],
            "content_sha256": journal.digest(item["content"])})
        if "reply_revision" in item:
            check["reply_revision"] = item["reply_revision"]
        if self.prepared_voice is not None:
            check["voice"] = self.prepared_voice.check
            check["reply_revision"] = item["voice"]["binding"]["reply_revision"]
        if journal.digest(check) != self.delivery_binding["send_check_sha256"]:
            raise ValueError("OWNER_DELIVERY_BINDING_CHANGED")
        path = "/internal/v1/" + ("account-actions" if account else "owner-authority")
        result = await bounded_post(self.client, path + "/send-check", check)
        fields = {"approved", "lease_until"} | ({"content_sha256", "payload_sha256"}
                                               if account else set())
        if (set(result) != fields or result["approved"] is not True
            or account and any(result[k] != check[k]
                               for k in ("content_sha256", "payload_sha256"))):
            raise ValueError("OWNER_DELIVERY_SEND_FENCED")
        expiry = datetime.fromisoformat(result["lease_until"])
        if expiry.utcoffset() is None:
            raise ValueError("OWNER_DELIVERY_LEASE_LOST")
        self.lease_until = min(self.lease_until, expiry)
        if item["content"].get("image") is not None:
            await self.runtime.check(self.client, purpose="meme")
        if item["content"].get("generated_image") is not None:
            await self.runtime.check(self.client, purpose="expression")
        self._request(request)
        self._local()
        if self.meme_prepared:
            self.runtime.memes.mark_sending(item, check=self._local)
            self.meme_sending = True
        with self.db:
            self.runtime.append("IO_STARTED", {"message_id": item["message_id"],
                                                "provider_ack_hash": None})
            self.db.execute("UPDATE window_attempts SET state='IO_STARTED' WHERE message_id=?",
                            (item["message_id"],))
        self._local()
        seal_protocol_content(request, request.content)
        self.dispatched = True

    async def cancel_before_send(self, item):
        if (self.dispatched or item is not self.current_item
            or self.item_hash != journal.digest(item)):
            return False
        self.runtime.verify(active=False)
        attempt = self.db.execute("SELECT state FROM window_attempts WHERE message_id=?",
                                   (item["message_id"],)).fetchone()
        if not attempt or attempt[0] not in {"RESERVED", "IO_STARTED"}:
            return False
        try:
            while await owner_notices.flush_one(self.runtime, self.client):
                pass
        except httpx.HTTPError:
            return False
        body = {"authority": self.authority, "message_id": item["message_id"],
                "lease_token": item["lease_token"],
                "content_sha256": journal.digest(item["content"])}
        account = account_actions.is_account(item)
        if account:
            body = account_actions.send_check_owner(self.authority, item)
            body["reason"] = "BRIDGE_PREFLIGHT_FAILED"
        if "reply_revision" in item:
            body["reply_revision"] = item["reply_revision"]
        if pending := self.pending_input():
            if account:
                body["reason"] = "OWNER_INPUT_PENDING"
            else:
                body.update(cause="OWNER_INPUT_PENDING", pending_event_id=pending)
        try:
            path = "/internal/v1/" + ("account-actions" if account else "owner-authority")
            receipt = await bounded_post(self.client, path + "/cancel-before-send", body)
        except httpx.HTTPError:
            return False
        expected = {"cancelled": True, "message_id": item["message_id"],
                    "state": "CANCELLED", "send_attempt_retained": True}
        if account:
            expected = account_actions.cancelled_proof(receipt,
                message_id=item["message_id"], lease_token=item["lease_token"],
                content_sha256=body["content_sha256"], payload_sha256=body["payload_sha256"])
        if receipt != expected or self.dispatched or self.item_hash != journal.digest(item):
            raise ValueError("OWNER_CANCEL_RECEIPT_INVALID")
        self.runtime.verify(active=False)
        with self.db:
            self.runtime.append("NOT_SENT", {"message_id": item["message_id"],
                "provider_ack_hash": None, "core_receipt": receipt})
            self.db.execute("UPDATE window_attempts SET state='NOT_SENT' WHERE message_id=?",
                            (item["message_id"],))
            self.runtime.spool.cancel_delivery(item, receipt)
        if self.meme_prepared:
            self.runtime.memes.confirm_not_sent(item, self, receipt)
        self.runtime.verify(active=False)
        return True

    def complete(self, item, provider_id):
        from .native_actions import AccountAck, NativeAck

        if (item is not self.current_item or journal.digest(item) != self.item_hash
            or provider_id and not self.dispatched):
            raise ValueError("OWNER_DELIVERY_BINDING_CHANGED")
        state = "ACK" if provider_id else "UNKNOWN"
        ack = (provider_id.value(item) if isinstance(provider_id, AccountAck)
               else provider_id.value() if isinstance(provider_id, NativeAck) else provider_id)
        ack_hash = journal.digest(ack) if provider_id else None
        with self.db:
            self.runtime.append(state, {"message_id": item["message_id"],
                                        "provider_ack_hash": ack_hash})
            self.db.execute("UPDATE window_attempts SET state=?,provider_ack_hash=? "
                            "WHERE message_id=?", (state, ack_hash, item["message_id"]))
        if not provider_id:
            self.runtime.stop()


class OwnerOneBot:
    """Per-attempt protocol facade over the original checked HTTP transport."""

    companion = True
    account_scope = True
    voice_scope = True
    generated_image_scope = True
    probe_before_reservation = True

    def __init__(self, guard, protocol):
        self.guard, self.config = guard, guard.config
        if (str(protocol.base_url).rstrip("/") != self.config.onebot_http_url.rstrip("/")
            or protocol.headers.get("Authorization") != "Bearer " + self.config.onebot_token):
            raise ValueError("OWNER_PROTOCOL_ENDPOINT_CHANGED")
        self.protocol = protocol
        self.delegate = OneBotClient(replace(self.config, delivery_enabled=True),
                                     checked_http_client(protocol, guard.before_dispatch))

    async def probe(self):
        # Reuse the authority-bound health check without a leased-item guard.
        # The actual send retains before_dispatch's current-item/lease fences.
        return await self.guard.runtime.probe(self.guard.client, self.protocol)

    def release_prepared(self):
        prepared = self.guard.prepared_message
        # A quote targets one Core-resolved message and must never reach a later item.
        if self.config.meme_catalog_owner == "core" or (
            prepared and prepared[0].get("type") == "reply"
        ):
            self.guard.prepared_message = None

    async def prepare_delivery(self, item):
        if account_actions.is_account(item):
            permission = await self.guard.runtime.check(self.guard.client,
                                                        purpose="account_actions")
            await self.guard.runtime.check(self.guard.client, purpose="social")
            self.guard.prepared_account = account_actions.prepare_owner(self.guard.runtime,
                item, permission["capability_policies"]["account_actions"])
        elif item["action_type"] == "qq.private.poke":
            from .native_actions import prepare

            self.guard.runtime.verify()
            self.guard.prepared_native = prepare(item["content"], self.config.owner_qq_id)
        elif "voice" in item:
            from .voice_runtime import prepare_delivery

            permission = await self.guard.runtime.check(self.guard.client, purpose="voice")
            self.guard.prepared_voice = prepare_delivery(self.guard.runtime, item,
                voice_policy_sha256=permission["capability_policies"]["voice"])
        elif item["content"].get("generated_image") is not None:
            from .generated_image_http import read_image

            await self.guard.runtime.check(self.guard.client, purpose="expression")
            original = journal.digest(item)

            def fence():
                self.guard.runtime.verify()
                if journal.digest(item) != original:
                    raise ValueError("OWNER_DELIVERY_BINDING_CHANGED")

            message = reply_segments.quote(item)
            if not reply_segments.image_only(item):
                message.append({"type": "text", "data": {"text": reply_segments.wire_text(item)}})
            try:
                image = await read_image(self.guard.runtime, self.guard.client, item,
                                         check=fence)
            except httpx.HTTPStatusError as error:
                # Core no longer offers the picture (expired or erased): send the bound
                # text alone rather than holding the reply.
                if error.response.status_code != 410:
                    raise
                image = None
            if image is None and reply_segments.image_only(item):
                raise ValueError("GENERATED_IMAGE_UNAVAILABLE")
            if image is not None:
                message.append({"type": "image", "data": {
                    "file": "base64://" + base64.b64encode(image.data).decode("ascii")}})
            self.guard.prepared_message = message
        elif item["content"].get("image") is not None:
            await self.guard.runtime.check(self.guard.client, purpose="meme")
            original = journal.digest(item)

            def fence():
                self.guard.runtime.verify()
                if journal.digest(item) != original:
                    raise ValueError("OWNER_DELIVERY_BINDING_CHANGED")

            if self.config.meme_catalog_owner == "core":
                image = await self.guard.runtime.memes.prepare(self.guard.client, item, check=fence)
            else:
                image = self.guard.runtime.memes.prepare(item, check=fence)
            self.guard.meme_prepared = image is not None
            message = reply_segments.quote(item)
            if not reply_segments.image_only(item):
                message.append({"type": "text", "data": {"text": reply_segments.wire_text(item)}})
            if image is None:
                self.guard.runtime.memes.cancel(reply_segments.event_id(item))
                if reply_segments.image_only(item):
                    raise ValueError("MEME_SELECTED_ASSET_UNAVAILABLE")
            else:
                message.append({"type": "image", "data": {
                    "file": "base64://" + base64.b64encode(image.data).decode("ascii")}})
            self.guard.prepared_message = message
        elif quoted := reply_segments.quote(item):
            self.guard.prepared_message = quoted + [
                {"type": "text", "data": {"text": reply_segments.wire_text(item)}}]

    async def send_content(self, content):
        item = self.guard.current_item
        if content != item["content"]:
            raise ValueError("OWNER_DELIVERY_BINDING_CHANGED")
        if account_actions.is_account(item):
            return await self.delegate.send_account(self.guard.prepared_account, item)
        if item["action_type"] == "qq.private.poke":
            return await self.delegate.send_native(self.guard.prepared_native)
        if "voice" in item:
            if self.guard.prepared_voice is None:
                raise ValueError("OWNER_MEDIA_PREPARATION_REQUIRED")
            return await self.delegate.send_message(self.guard.prepared_voice.message)
        if "generated_image" in content:
            if self.guard.prepared_message is None:
                raise ValueError("OWNER_MEDIA_PREPARATION_REQUIRED")
            return await self.delegate.send_message(self.guard.prepared_message)
        if "image" in content:
            if self.guard.prepared_message is None:
                raise ValueError("OWNER_MEDIA_PREPARATION_REQUIRED")
            try:
                provider_id = await self.delegate.send_message(self.guard.prepared_message)
            except BaseException:
                if self.guard.meme_sending and self.guard.dispatched:
                    self.guard.runtime.memes.receipt(item, "UNKNOWN")
                raise
            if self.guard.meme_sending:
                self.guard.runtime.memes.receipt(item, "SENT" if provider_id else "UNKNOWN")
            return provider_id
        if "reply_to" in content:
            if self.guard.prepared_message is None:
                raise ValueError("OWNER_MEDIA_PREPARATION_REQUIRED")
            return await self.delegate.send_message(self.guard.prepared_message)
        return await self.delegate.send_text(reply_segments.wire_text(item))
