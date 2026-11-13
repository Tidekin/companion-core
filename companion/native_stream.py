"""Worker-owned public previews; final Outbox and accounting keep their original gates."""

import json
import time

from pydantic_ai.messages import (
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from companion import job_queue, owner_delivery, reply_quote, transport
from companion.agent_runtime import check_conversation
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import after, encode

# A full conversation check costs about a second on production history. Between
# full checks a preview write is fenced instead: the same authority record, privacy
# epoch, live lease and an untouched preview row (any withdraw bumps its version).
PREVIEW_CHECK_SECONDS = 2.0


class Publisher:
    def __init__(self, core, job, snapshots):
        self.core, self.job, self.snapshots = core, job, snapshots
        self.request_id = None
        self.text = ""
        self.saved = ""
        self.last_flush = 0.0
        self.fence = None

    async def events(self, context, events):
        async for event in events:
            text = None
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                text = event.part.content
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                text = event.delta.content_delta
            elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                self.flush(force=True)
            if text is None:
                continue  # Thinking, tools and raw arguments are never public previews.
            row = self.core.store.one(
                "SELECT request_id FROM agent_conversations WHERE job_id=%s", (self.job["job_id"],))
            if row is None or not row["request_id"]:
                raise CoreError("NATIVE_STREAM_REQUEST_REQUIRED", 409)
            if row["request_id"] != self.request_id:
                self.request_id = row["request_id"]
                self.text = self.saved = ""
                self.last_flush = 0.0
                self.fence = None
            self.text += text
            self.flush()
        self.flush(force=True)

    def flush(self, *, force=False):
        if self.text == self.saved:
            return
        if len(self.text) > 32000:
            raise CoreError("MODEL_INVALID_RESPONSE", 502)
        now = time.monotonic()
        if not force and self.saved and now - self.last_flush < 0.1:
            return
        if not force and self._fenced_flush(now):
            return
        store = self.core.store
        with store.transaction():
            check_conversation(self.core, self.job, self.snapshots,
                               current_request=self.request_id)
            version = self._write(store)
            if version is None:
                raise CoreError("NATIVE_STREAM_REQUEST_CHANGED", 409)
            self.fence = {"checked_at": now, "version": version, "epoch": epoch(store),
                          "authority": self._authority(store)}
        self.saved, self.last_flush = self.text, now

    @staticmethod
    def _authority(store):
        from companion import owner_authority

        row = owner_authority._latest(store)
        return row["record_sha256"] if row else None

    def _write(self, store, *, expected_version=None):
        at = self.core.clock()
        job_queue.renew(store, self.job, after(at, self.core.settings.lease_seconds), now=at)
        value = {"text": self.text, "state": "GENERATING", "request_id": self.request_id,
                 "lease_token": self.job["lease_token"], "privacy_epoch": epoch(store)}
        row = store.one(
            "UPDATE agent_conversations SET preview_json=%s,preview_version=preview_version+1 "
            "WHERE job_id=%s AND request_id=%s AND (%s::bigint IS NULL OR preview_version=%s) "
            "RETURNING preview_version",
            (encode(value), self.job["job_id"], self.request_id,
             expected_version, expected_version))
        return row["preview_version"] if row else None

    def _fenced_flush(self, now):
        """Write between full checks only while nothing a full check guards has moved."""
        fence = self.fence
        if fence is None or now - fence["checked_at"] >= PREVIEW_CHECK_SECONDS:
            return False
        store = self.core.store
        with store.transaction():
            if (epoch(store) != fence["epoch"]
                    or self._authority(store) != fence["authority"]):
                return False
            version = self._write(store, expected_version=fence["version"])
            if version is None:
                # Withdrawn or replaced since the last full check: take the full path.
                return False
        fence["version"] = version
        self.saved, self.last_flush = self.text, now
        return True


def finish(store, job):
    if transport.native_text(store, job["event_id"]):
        store.execute("UPDATE agent_conversations SET preview_json=NULL,"
                      "preview_version=preview_version+1 WHERE job_id=%s "
                      "AND preview_json IS NOT NULL",
                      (job["job_id"],))


def withdraw(store):
    """Join an original authority stop/revision/restore transaction."""
    store.execute("UPDATE agent_conversations SET preview_json=NULL,"
                  "preview_version=preview_version+1 WHERE preview_json IS NOT NULL")


def interrupt(core, job):
    if not transport.native_text(core.store, job["event_id"]):
        return
    row = core.store.one("SELECT preview_json FROM agent_conversations WHERE job_id=%s",
                         (job["job_id"],))
    if not row or not row["preview_json"]:
        return
    value = json.loads(row["preview_json"])
    try:
        saved = core.store.one("SELECT snapshots_json FROM agent_conversations WHERE job_id=%s",
                               (job["job_id"],))
        check_conversation(core, job, json.loads(saved["snapshots_json"]),
                           current_request=value["request_id"])
        value["state"] = "INTERRUPTED"
    except CoreError:
        value = None
    core.store.execute("UPDATE agent_conversations SET preview_json=%s,"
                       "preview_version=preview_version+1 WHERE job_id=%s",
                       (encode(value) if value else None, job["job_id"]))


def project(context, event_id, *, redacted=False):
    row = context.store.one(
        "SELECT c.preview_json,c.preview_version,j.job_id FROM agent_conversations c "
        "JOIN jobs j USING(job_id) WHERE j.event_id=%s AND j.kind='chat'", (event_id,))
    if not row or not row["preview_version"]:
        return None
    result = {"version": row["preview_version"], "state": "WITHDRAWN", "text": ""}
    if redacted:
        return result
    job = owner_delivery._job(context.store, row["job_id"])
    if job["state"] == "SUCCEEDED":
        return {**result, "state": "COMPLETE"}
    if not row["preview_json"] or job["state"] == "CANCELLED":
        return result
    try:
        value = json.loads(row["preview_json"])
        if (not isinstance(value, dict) or not isinstance(value.get("text"), str)
                or len(value["text"]) > 32000
                or value.get("privacy_epoch") != epoch(context.store)):
            return result
        running = job["state"] == "RUNNING"
        if running and value.get("lease_token") != job["lease_token"]:
            return result
        owner_delivery._sources(context.store, context.settings, context.clock, job,
                                executing=running, preview=True)
        text = reply_quote.visible_preview(value["text"])
        if running:
            return {**result, "text": text, "state": "GENERATING"}
        if value.get("state") == "INTERRUPTED":
            return {**result, "text": text, "state": "INTERRUPTED"}
        return {**result, "state": "INTERRUPTED" if job["state"] == "FAILED" else "WAITING"}
    except (CoreError, ValueError, TypeError, KeyError):
        return result  # A failed current proof can never expose the previous plaintext.
