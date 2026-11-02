"""Native host scheduling for the existing bounded Meme retrieval/discovery workers."""

import asyncio
import json
import time

import companion_delivery_protocol as wire

from . import journal, pending_media
from .meme_runtime import failure_diagnostic, infer_intent
from .metrics import emit
from .onebot_client import bounded_post
from .owner_handoff import terminal
from .rich_input import input_digest


class MemePump:
    def __init__(self, runtime, client):
        self.owner, self.client = runtime, client
        self.runtime = runtime.memes if runtime.config.meme_enabled \
            and runtime.config.meme_catalog_owner == "bridge" else None
        self.task = self.query_task = None
        self.query_tasks = set()
        self.query_event = self.submitted_event = None
        self.discovery_intent = None
        self.next_run = 0

    def latest(self):
        row = self.owner.db.execute(
            "SELECT w.event_id,w.capture_json,i.state FROM window_inputs w "
            "JOIN inbox i ON i.key=w.event_id WHERE w.plan_hash=? "
            "AND json_extract(w.capture_json,'$.kind') IN "
            "('authenticated_onebot_ws','onebot_history_backfill') "
            "ORDER BY w.rowid DESC LIMIT 1", (journal.digest(self.owner.authority),)).fetchone()
        return row if row and row[2] == "accepted" else None

    async def state(self, row):
        capture = json.loads(row[1])
        self.runtime._admitted_check(row[0])
        value = await bounded_post(self.client,
                                    "/internal/v1/owner-authority/inputs/state", capture)
        fields = {"authority", "event_id", "input_sha256", "reply_revision", "terminal", "state",
                  "unsettled_work", "unsettled_model", "unsettled_delivery", "work_sha256"}
        if (not isinstance(value, dict) or set(value) != fields
            or any(value[k] != capture[k] for k in ("authority", "event_id", "input_sha256"))
            or type(value["reply_revision"]) is not int or value["reply_revision"] < 1
            or any(type(value[k]) is not bool for k in (
                "terminal", "unsettled_work", "unsettled_model", "unsettled_delivery"))
            or value["state"] not in {"PENDING", "COMPLETE", "CANCELLED", "RECALLED"}
            or not wire.hash_value(value["work_sha256"])):
            raise ValueError("MEME_INPUT_STATE_INVALID")
        if value["terminal"]:
            terminal(value, capture)
        self.runtime._admitted_check(row[0])
        return value

    def interrupt(self):
        for task in [self.task, *self.query_tasks]:
            if task is not None and not task.done() and not task.cancelling():
                task.cancel()

    def admitted(self, event_id, *, payload=None):
        if self.runtime is None or event_id == self.query_event:
            return
        row = self.latest()
        if row is None or row[0] != event_id:
            return
        intent = None
        if payload is not None:
            capture = json.loads(row[1])
            if (payload.get("event_id") != event_id
                or input_digest(payload["payload"]) != capture["input_sha256"]):
                raise ValueError("MEME_INPUT_INVALID")
            intent = infer_intent(payload["payload"].get("text"))
        self.interrupt()
        self.query_event = event_id
        # Accepted inbox text has already been erased. Keep only this enum in RAM,
        # so optional retrieval failure cannot strand an otherwise empty catalog.
        self.discovery_intent = intent
        if self.runtime.semantics_enabled:
            self.query_task = asyncio.create_task(self.query(row))
            self.query_tasks.add(self.query_task)
            self.query_task.add_done_callback(self.query_tasks.discard)

    async def query(self, row):
        try:
            async with asyncio.timeout(1.5):
                state = await self.state(row)
                await self.runtime.semantic_offer(self.client, row[0],
                                                    reply_revision=state["reply_revision"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            emit("meme_semantic_offer", status="DEFERRED", **failure_diagnostic(error))

    def discovery_offer(self, event_id):
        """A discovery hint only; never creates a current-turn offer receipt."""
        offer = self.runtime.missing_offer(event_id)
        if (offer is None and event_id == self.query_event and self.discovery_intent is not None
            and self.runtime.semantics_enabled and not self.owner.db.execute(
                "SELECT 1 FROM meme_assets WHERE active=1 LIMIT 1").fetchone()
            and not self.owner.db.execute(
                "SELECT 1 FROM meme_runtime_semantic_offers WHERE event_id=? "
                "AND query_binding IS NOT NULL AND json_type(offer_json)='null'",
                (event_id,)).fetchone()):
            self.runtime._admitted_check(event_id)
            return {"intent": self.discovery_intent, "candidates": []}
        return offer

    async def execute(self, row):
        try:
            state = await self.state(row)
            if (row[0] != self.submitted_event and state["terminal"]
                and state["state"] == "COMPLETE"):
                offer = self.discovery_offer(row[0])
                result = await self.runtime.submit_missing(self.client, row[0], offer,
                    reply_revision=state["reply_revision"]) if offer else {}
                if result.get("status") != "DEFERRED":
                    self.submitted_event = row[0]
            result = await self.runtime.run_once(self.client)
            if result.get("status") not in {"IDLE", "DISABLED", "BUSY"}:
                emit("meme_discovery", status=result["status"])
        except asyncio.CancelledError:
            raise
        except Exception:
            emit("meme_discovery", status="DEFERRED")
        finally:
            self.next_run = time.monotonic() + 5

    def tick(self, *, idle):
        if self.runtime is None:
            return
        self.owner.verify()
        row = self.latest()
        pending = self.owner.db.execute("SELECT 1 FROM inbox WHERE state='pending' LIMIT 1"
                                       ).fetchone()
        if pending or row is None:
            self.interrupt()
            return
        self.admitted(row[0])
        if self.task is not None:
            if not self.task.done():
                if not idle and not self.task.cancelling():
                    self.task.cancel()
                return
            if not self.task.cancelled():
                self.task.result()
            self.task = None
        if (not idle or time.monotonic() < self.next_run
            or self.query_task is not None and not self.query_task.done()):
            return
        if pending_media.exists(self.owner) and self.owner.db.execute(
            "SELECT 1 FROM automatic_media WHERE state NOT IN ('DONE','CANCELLED') LIMIT 1"
        ).fetchone():
            return
        self.task = asyncio.create_task(self.execute(row))

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def close(self):
        self.interrupt()
        await asyncio.gather(*self.query_tasks, *([self.task] if self.task else []),
                             return_exceptions=True)
        self.discovery_intent = None
