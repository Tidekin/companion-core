"""Continuous private replies inside one explicitly approved, expiring window.

Spool, authenticated capture, Core admissions and delivery fences stay authoritative.
No model or storage implementation is imported from Core.
"""

import asyncio
import hashlib
import json
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from websockets.exceptions import ConnectionClosed

from companion_meme_protocol import OFFER_QUERY_SECONDS

from . import account_actions, capture, pokes, reply_segments
from .event_normalizer import decode, identifier, normalize, normalize_recall
from .metrics import emit
from .native_actions import approval_hash
from .network import checked_http_client, websocket
from .onebot_client import OneBotClient
from .outbound import deliver_tick
from .spool import encode
from .window import (
    SendOperation,
    WindowOneBot,
    check_protocol_request,
    delivery_origin,
    digest,
    seal_protocol_content,
)

MAX_BUFFERED_INPUTS = 32
REPLY_WAIT_SECONDS = 180
SEND_PROGRESS_SECONDS = 180
BUFFER_DOMAIN = b"companion-core/automatic-owner-buffer/v1\x00"
RECALL_DOMAIN = b"companion-core/onebot-owner-recall/v1\x00"
RECALL_FIELDS = {"kind", "window_sha256", "recall_id", "target_source_event_id",
                 "target_event_id", "protocol_epoch", "identity_sha256", "config_sha256",
                 "occurred_at", "received_at", "frame_sha256", "evidence_sha256", "signature"}


def interruptible(window):
    scope = window.plan.value.get("live_text", {})
    return scope.get("automatic_reply") is True and scope.get("companion_scope") == (
        "owner_private_v1")


def verify_recall(window, receipt):
    if not isinstance(receipt, dict) or set(receipt) != RECALL_FIELDS:
        raise ValueError("AUTHENTICATED_RECALL_REQUIRED")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(
            window.plan.config.capture_public_key,
        )).verify(bytes.fromhex(receipt["signature"]), RECALL_DOMAIN + encode(
            {key: value for key, value in receipt.items() if key != "signature"}).encode())
        occurred, received = (datetime.fromisoformat(receipt[key]) for key in (
            "occurred_at", "received_at"))
        source = receipt["target_source_event_id"]
        message_id = source.removeprefix(
            f"{window.plan.config.bot_qq_id}:{window.plan.config.origin}:"
            f"epoch:{window.plan.config.protocol_epoch}:")
        value = normalize_recall(encode({"time": int(occurred.timestamp()),
            "self_id": window.plan.config.bot_qq_id, "post_type": "notice",
            "notice_type": "friend_recall", "user_id": window.plan.config.owner_qq_id,
            "message_id": message_id}), window.plan.config, now=received.timestamp())
        if (value is None or any(receipt[key] != item for key, item in value.items())
            or receipt["kind"] != "authenticated_onebot_friend_recall"
            or receipt["window_sha256"] != window.plan.sha256
            or receipt["protocol_epoch"] != window.plan.config.protocol_epoch
            or receipt["identity_sha256"] != window.plan.value["identity_sha256"]
            or receipt["config_sha256"] != window.plan.value["config_sha256"]
            or not window.plan.start <= occurred <= received < window.plan.end
            or (received - occurred).total_seconds() > capture.MAX_AGE_SECONDS):
            raise ValueError("AUTHENTICATED_RECALL_REQUIRED")
    except (InvalidSignature, ValueError, TypeError, OverflowError):
        raise ValueError("AUTHENTICATED_RECALL_REQUIRED") from None
    return receipt


def recalled_input(window, event):
    """A notice only suppresses reference I/O after matching authenticated inbound identity."""
    if not window.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'")\
            .fetchone():
        return False
    provider_id = event["source_event_id"].rsplit(":", 1)[-1]
    if window.db.execute("SELECT 1 FROM results WHERE json_extract(body,'$.provider_message_id')=?",
                         (provider_id,)).fetchone():
        return False
    for row in window.db.execute(
        "SELECT record_json FROM automatic_recalls WHERE target_event_id=? AND window_sha256=?",
        (event["event_id"], window.plan.sha256),
    ):
        receipt = verify_recall(window, json.loads(row[0]))
        if (receipt["target_source_event_id"] == event["source_event_id"]
            and datetime.fromisoformat(event["occurred_at"]) <= datetime.fromisoformat(
                receipt["occurred_at"])):
            return True
    return False


async def flush_recalls(window, client):
    """Repeat authenticated control delivery; never discard an unacknowledged revocation."""
    if not window.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'",
    ).fetchone():
        return False
    changed = False
    for row in window.db.execute("SELECT * FROM automatic_recalls WHERE window_sha256=? "
                                 "AND state='PENDING' ORDER BY rowid",
                                 (window.plan.sha256,)).fetchall():
        receipt = verify_recall(window, json.loads(row["record_json"]))
        response = await window.core_post(client, "/internal/v1/live-text/inputs/recall", receipt)
        if (not isinstance(response, dict)
            or response.get("recall_id") != receipt["recall_id"]
            or response.get("target_event_id") != receipt["target_event_id"]
            or response.get("state") not in {"PENDING_TARGET", "RECALLED"}
            or type(response.get("duplicate")) is not bool):
            raise ValueError("INVALID_CORE_RECALL_ACK")
        head = window.append("RECALL_ACCEPTED", {"recall_id": receipt["recall_id"],
                                                 "core_receipt": response})
        with window.db:
            window.db.execute("UPDATE automatic_recalls SET state='ACCEPTED',response_json=? "
                               "WHERE recall_id=?", (encode(response), receipt["recall_id"]))
            window.db.execute("UPDATE test_window SET journal_head=?", (head,))
        if window.plan.config.meme_enabled and response["state"] == "RECALLED":
            window.memes.cancel(receipt["target_event_id"])
        changed = True
    return changed


def require_scope(window):
    if (window.plan.mode != "live_text"
        or window.plan.value["live_text"].get("automatic_reply") is not True):
        raise ValueError("AUTOMATIC_OWNER_REPLY_APPROVAL_REQUIRED")
    window.verify(active=False, dispatch=True)
    if window.row()["state"] == "STOPPED":
        raise ValueError("WINDOW_STOPPED")
    if not window.plan.start <= window.clock() < window.plan.end:
        raise ValueError("WINDOW_OUTSIDE_INTERVAL")


def evidence(window, kind, value):
    return digest({"kind": "automatic_owner_private_" + kind,
                   "window_sha256": window.plan.sha256,
                   "owner_approval_sha256": window.plan.value["live_text"]["approval_sha256"],
                   "material": value})


@dataclass(frozen=True)
class ObservedFrame:
    frame: str | bytes
    received_at: datetime
    peer: tuple
    window_sha256: str
    event_id: str
    signature: str

    def material(self):
        return {"frame": self.frame, "received_at": self.received_at.isoformat(),
                "peer": list(self.peer), "window_sha256": self.window_sha256,
                "event_id": self.event_id}

    def verify(self, window):
        if self.window_sha256 != window.plan.sha256:
            raise ValueError("AUTOMATIC_BUFFER_SCOPE_CHANGED")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(
                window.plan.config.capture_public_key,
            )).verify(bytes.fromhex(self.signature),
                      BUFFER_DOMAIN + encode(self.material()).encode())
        except (InvalidSignature, ValueError):
            raise ValueError("AUTHENTICATED_BUFFER_SIGNATURE_REQUIRED") from None
        _, event = capture.input_event(window, self.frame, self.received_at)
        if event["event_id"] != self.event_id:
            raise ValueError("AUTOMATIC_BUFFER_EVENT_CHANGED")


def observed_record(window, record):
    value = json.loads(record)
    if value.get("kind") == pokes.KIND:
        observed = pokes.ObservedPoke(value)
        observed.verify(window)
        return observed
    if set(value) != {"frame", "received_at", "peer", "window_sha256", "event_id", "signature"}:
        raise ValueError("AUTOMATIC_BUFFER_CHANGED")
    observed = ObservedFrame(value["frame"], datetime.fromisoformat(value["received_at"]),
                             tuple(value["peer"]), value["window_sha256"], value["event_id"],
                             value["signature"])
    observed.verify(window)
    return observed


def pending_input(window, current_event_id):
    """An explicit cancellation cause must be an authenticated, still queued owner turn."""
    if not interruptible(window) or not window.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='automatic_received'",
    ).fetchone():
        return None
    for row in window.db.execute(
        "SELECT * FROM automatic_received WHERE window_sha256=? AND event_id<>? ORDER BY rowid",
        (window.plan.sha256, current_event_id),
    ):
        observed = observed_record(window, row["record_json"])
        if observed.event_id != row["event_id"]:
            raise ValueError("AUTOMATIC_BUFFER_EVENT_CHANGED")
        if isinstance(observed, pokes.ObservedPoke):
            continue
        previous = window.db.execute("SELECT fingerprint FROM inbox WHERE key=?",
                                      (observed.event_id,)).fetchone()
        if previous:
            raw = json.loads(observed.frame)
            if previous[0] != digest({"message": raw["message"], "time": raw["time"]}):
                raise ValueError("SOURCE_EVENT_CONFLICT")
            continue
        return observed.event_id
    return None


class OwnerSource:
    """Signed receive records stay in the same Spool across model work and restarts."""

    def __init__(self, window, protocol, pause):
        self.window, self.protocol, self.pause = window, protocol, pause
        self.available = asyncio.Event()
        self.task = None
        window.db.execute(
            "CREATE TABLE IF NOT EXISTS automatic_received ("
            "event_id TEXT PRIMARY KEY,window_sha256 TEXT NOT NULL,record_json TEXT NOT NULL)"
        )
        window.db.execute(
            "CREATE TABLE IF NOT EXISTS automatic_recalls (recall_id TEXT PRIMARY KEY,"
            "window_sha256 TEXT NOT NULL,target_event_id TEXT NOT NULL,record_json TEXT NOT NULL,"
            "state TEXT NOT NULL,response_json TEXT)"
        )
        window.db.commit()
        columns = [row[1] for row in window.db.execute("PRAGMA table_info(automatic_received)")]
        if columns != ["event_id", "window_sha256", "record_json"]:
            raise ValueError("AUTOMATIC_BUFFER_SCHEMA_CHANGED")
        columns = [row[1] for row in window.db.execute("PRAGMA table_info(automatic_recalls)")]
        if columns != ["recall_id", "window_sha256", "target_event_id", "record_json", "state",
                       "response_json"]:
            raise ValueError("AUTOMATIC_RECALL_SCHEMA_CHANGED")

    def put_recall(self, frame, received_at, peer):
        window = self.window
        value = normalize_recall(frame, window.plan.config, now=received_at.timestamp())
        if value is None:
            return
        if window.db.execute("SELECT 1 FROM automatic_recalls WHERE recall_id=?",
                             (value["recall_id"],)).fetchone():
            return
        if window.db.execute("SELECT COUNT(*) FROM automatic_recalls WHERE state='PENDING'")\
                .fetchone()[0] >= min(MAX_BUFFERED_INPUTS, window.plan.config.max_pending):
            raise ValueError("AUTOMATIC_RECALL_QUEUE_FULL")
        data = frame.encode() if isinstance(frame, str) else frame
        receipt = {"kind": "authenticated_onebot_friend_recall",
                   "window_sha256": window.plan.sha256, **value,
                   "protocol_epoch": window.plan.config.protocol_epoch,
                   "identity_sha256": window.plan.value["identity_sha256"],
                   "config_sha256": window.plan.value["config_sha256"],
                   "received_at": received_at.isoformat(),
                   "frame_sha256": hashlib.sha256(data).hexdigest()}
        receipt["evidence_sha256"] = digest({"receipt": dict(receipt), "peer": list(peer),
            "endpoint": window.plan.config.onebot_ws_url, "bearer_accepted": True})
        receipt["signature"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
            window.plan.config.capture_signing_key)).sign(
                RECALL_DOMAIN + encode(receipt).encode()).hex()
        verify_recall(window, receipt)
        window.spool.capacity(len(encode(receipt).encode()) * 3)
        head = window.append("RECALL_RECEIVED", {"recall_id": receipt["recall_id"],
                                                 "receipt_sha256": digest(receipt)})
        with window.db:
            window.db.execute("INSERT INTO automatic_recalls VALUES(?,?,?,?,'PENDING',NULL)",
                (receipt["recall_id"], window.plan.sha256, receipt["target_event_id"],
                 encode(receipt)))
            window.db.execute("UPDATE test_window SET journal_head=?", (head,))
        self.available.set()

    def put(self, frame, received_at, peer, event_id):
        window = self.window
        frame = frame.decode("utf-8") if isinstance(frame, bytes) else frame
        persisted = window.db.execute("SELECT fingerprint FROM inbox WHERE key=?",
                                       (event_id,)).fetchone()
        if persisted:
            raw = json.loads(frame)
            if persisted[0] != digest({"message": raw["message"], "time": raw["time"]}):
                raise ValueError("SOURCE_EVENT_CONFLICT")
            return
        value = {"frame": frame, "received_at": received_at.isoformat(), "peer": list(peer),
                 "window_sha256": window.plan.sha256, "event_id": event_id}
        previous = window.db.execute(
            "SELECT record_json FROM automatic_received WHERE event_id=?", (event_id,),
        ).fetchone()
        if previous:
            old = json.loads(previous[0])
            before, current = json.loads(old["frame"]), json.loads(frame)
            if (any(before[key] != current[key] for key in ("message", "time"))
                or old["window_sha256"] != window.plan.sha256):
                raise ValueError("SOURCE_EVENT_CONFLICT")
            return
        count = window.db.execute("SELECT COUNT(*) FROM automatic_received").fetchone()[0]
        if count >= min(MAX_BUFFERED_INPUTS, window.plan.config.max_pending):
            raise ValueError("AUTOMATIC_RECEIVE_QUEUE_FULL")
        window.spool.capacity(len(frame.encode()) * 3)
        value["signature"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
            window.plan.config.capture_signing_key,
        )).sign(BUFFER_DOMAIN + encode(value).encode()).hex()
        with window.db:
            window.db.execute("INSERT INTO automatic_received VALUES (?,?,?)",
                              (event_id, window.plan.sha256, encode(value)))
        self.available.set()

    def first(self):
        row = self.window.db.execute(
            "SELECT * FROM automatic_received ORDER BY rowid LIMIT 1",
        ).fetchone()
        if row is None:
            return None
        return observed_record(self.window, row["record_json"])

    def consume(self, observed):
        observed.verify(self.window)
        with self.window.db:
            self.window.db.execute("DELETE FROM automatic_received WHERE event_id=? "
                                   "AND window_sha256=?",
                                   (observed.event_id, observed.window_sha256))

    async def __aenter__(self):
        self.task = asyncio.create_task(self.run())
        return self

    async def __aexit__(self, *_):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def probe(self):
        window = self.window

        async def boundary(request):
            require_scope(window)
            check_protocol_request(window, request)
            content = request.content
            if request.url.path not in {"/get_login_info", "/get_status", "/get_version_info"} or (
                decode(content, 64) != {}
            ):
                raise ValueError("CAPTURE_PROBE_CHANGED")
            seal_protocol_content(request, content)

        async with checked_http_client(self.protocol, boundary) as scoped:
            if not (await OneBotClient(window.plan.config, scoped).probe())["connection_ready"]:
                raise ValueError("ONEBOT_NOT_READY")

    async def run(self):
        window, delay = self.window, 0.25
        while not self.pause.is_set():
            require_scope(window)
            try:
                await self.probe()
                connector = await websocket(
                    window.plan.config,
                    synthetic=window.plan.value["execution"] == "local_synthetic",
                    check=lambda: require_scope(window),
                )
                async with connector as ws:
                    parsed = urlsplit(window.plan.config.onebot_ws_url)
                    if (ws.remote_address[0] != connector.companion_private_destination
                        or ws.request.path != "/" or ws.request.headers["Host"] != parsed.netloc
                        or ws.request.headers["Authorization"]
                        != "Bearer " + window.plan.config.onebot_token):
                        raise ValueError("AUTHENTICATED_WS_BOUNDARY_CHANGED")
                    emit("automatic_source_connected")
                    wall_start, monotonic_start = window.clock(), time.monotonic()
                    delay = 0.25
                    while not self.pause.is_set():
                        try:
                            frame = await asyncio.wait_for(ws.recv(), 1)
                        except TimeoutError:
                            require_scope(window)
                            continue
                        received_at = window.clock()
                        require_scope(window)
                        if abs((received_at - wall_start).total_seconds() - (
                            time.monotonic() - monotonic_start
                        )) > capture.MAX_CLOCK_DRIFT_SECONDS:
                            raise ValueError("CAPTURE_CLOCK_DRIFT")
                        raw = decode(frame, window.plan.config.max_frame_bytes, max_depth=20)
                        if identifier(raw.get("self_id")) != window.plan.config.bot_qq_id:
                            raise ValueError("INVALID_CAPTURE_ACCOUNT")
                        if raw.get("post_type") == "meta_event":
                            capture.validate_meta(window, raw)
                            continue
                        if raw.get("post_type") == "notice":
                            if raw.get("notice_type") == "friend_recall" and interruptible(window):
                                self.put_recall(frame, received_at, ws.remote_address)
                            elif (raw.get("notice_type") == "notify"
                                  and raw.get("sub_type") == "poke"):
                                pokes.put(self, frame, received_at, ws.remote_address)
                            elif (raw.get("notice_type") == "notify"
                                and raw.get("sub_type") == "input_status"
                                and identifier(raw.get("user_id"))
                                == window.plan.config.owner_qq_id):
                                capture.validate_input_status(window, raw)
                            continue
                        scope = window.plan.value["live_text"]
                        event = normalize(
                            frame, window.plan.config, now=received_at.timestamp(),
                            rich_input_scope=scope.get("rich_input_scope", "none"),
                            image_scope=scope.get("image_scope", "none"),
                            file_scope=scope.get("file_scope", "none"),
                        )
                        if event is None:
                            continue
                        if (event["payload"]["text"].strip().startswith("/")
                            and scope.get("companion_scope") != "owner_private_v1"):
                            emit("automatic_command_not_in_scope")
                            continue
                        capture.input_event(window, frame, received_at)
                        self.put(frame, received_at, ws.remote_address, event["event_id"])
            except (ConnectionClosed, OSError, httpx.TransportError):
                emit("automatic_source_reconnecting")
                try:
                    await asyncio.wait_for(self.pause.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(30, delay * 2)

    def check(self):
        require_scope(self.window)
        if self.task.done():
            self.task.result()
            if not self.pause.is_set():
                raise ValueError("AUTOMATIC_SOURCE_STOPPED")

    async def next(self, idle=None):
        while True:
            self.check()
            if self.pause.is_set():
                return None
            if observed := self.first():
                return observed
            if idle is not None:
                await idle()
            self.available.clear()
            try:
                await asyncio.wait_for(self.available.wait(), 1)
            except TimeoutError:
                pass


async def capture_observed(window, client, protocol, observed):
    require_scope(window)
    observed.verify(window)
    if observed.window_sha256 != window.plan.sha256 or window.busy:
        raise ValueError("AUTOMATIC_CAPTURE_SCOPE_CHANGED")
    if isinstance(observed, pokes.ObservedPoke):
        return await pokes.receive(window, client, observed)
    window.busy = True
    try:
        capture.capacity(window)
        await window.core_counts(client)
        capture.capacity(window)
        fence = window.row()["fence"] + 1
        head = window.append("CAPTURE_STARTED", {"fence": fence})
        with window.db:
            window.db.execute("UPDATE test_window SET state='CAPTURING',fence=?,journal_head=?",
                              (fence, head))
        return await capture.persist_capture(window, client, protocol, observed.frame,
                                             observed.received_at, observed.peer, fence)
    finally:
        window.busy = False


class AutomaticSend(SendOperation):
    def __init__(self, window, client):
        super().__init__(window, client, None)

    def reserve(self, item):
        require_scope(self.window)
        value = {"window_sha256": self.window.plan.sha256,
                 "event_id": delivery_origin(item), "message_id": item["message_id"],
                 **approval_hash(item)}
        self.approval = {**value, "evidence_sha256": evidence(self.window, "reply", value)}
        super().reserve(item)


async def core_status(window, client):
    content = bytearray()
    async with window.core_http(client) as scoped:
        async with scoped.stream("GET", "/internal/v1/ops/status") as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError("INVALID_CORE_COUNTS")
    return decode(bytes(content), 65536)


def segment_pacing_pending(window):
    """Defer leasing while the local ACK interval exceeds Core's earlier result clock."""
    window.verify()
    policy = reply_segments.scope(window.plan)
    if not policy or not policy["interval_ms"]:
        return False
    row = window.db.execute(
        "SELECT * FROM window_attempts WHERE plan_hash=? ORDER BY fence DESC LIMIT 1",
        (window.plan.sha256,),
    ).fetchone()
    if not row or row["state"] != "ACK":
        return False
    incoming = window.db.execute(
        "SELECT state FROM window_inputs WHERE event_id=? AND plan_hash=?",
        (row["event_id"], window.plan.sha256),
    ).fetchone()
    if incoming and incoming["state"] in {"SUPERSEDED", "RECALLED", "CANCELLED"}:
        return False
    records, _ = window.read_journal()
    reservation = next((record for record in reversed(records) if record["kind"] == "RESERVED"
                        and record["data"].get("fence") == row["fence"]), None)
    terminal = next((record for record in reversed(records)
                     if record["kind"] in {"IO_STARTED", "ACK", "UNKNOWN"}
                     and record["data"].get("fence") == row["fence"]), None)
    if (not reservation or not terminal or terminal["kind"] != "ACK"
        or reservation["data"].get("event_hash") != digest(row["event_id"])
        or reservation["data"].get("message_hash") != digest(row["message_id"])
        or reservation["data"].get("item_sha256") != row["item_hash"]):
        raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
    segment = reservation["data"].get("reply_segment")
    if not segment or segment["index"] >= segment["count"] - 1:
        return False
    if segment["interval_ms"] != policy["interval_ms"]:
        raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
    return window.clock() < datetime.fromisoformat(terminal["at"]) + timedelta(
        milliseconds=policy["interval_ms"])


async def pump_private(window, client, protocol, *, status=None):
    status = status if status is not None else await core_status(window, client)
    if any(row["state"] == "PENDING" and row["count"] for row in status["deliveries"]):
        await window.core_counts(client)
        if segment_pacing_pending(window):
            return
        operation = AutomaticSend(window, client)
        sender = WindowOneBot(operation, protocol)
        async with window.core_http(client) as scoped, sender.delegate.client:
            sent = await deliver_tick(scoped, sender, window.spool, guard=operation)
        if not sent and account_actions.enabled(window):
            operation = AutomaticSend(window, client)
            sender = WindowOneBot(operation, protocol)
            async with window.core_http(client) as scoped, sender.delegate.client:
                await deliver_tick(scoped, sender, window.spool, guard=operation,
                                   delivery_lane="qq_account")


async def finish_input(window, client, protocol, source, event_id):
    deadline = time.monotonic() + REPLY_WAIT_SECONDS
    sending = False
    acknowledged = window.db.execute(
        "SELECT COUNT(*) FROM window_attempts WHERE event_id=? AND state='ACK'", (event_id,),
    ).fetchone()[0]
    interval = (reply_segments.scope(window.plan) or {}).get("interval_ms", 0) / 1000
    while window.db.execute("SELECT state FROM window_inputs WHERE event_id=?",
                            (event_id,)).fetchone()[0] != "ACK":
        source.check()
        window.verify()
        status = await core_status(window, client)
        if any(row.get("unknown_usage_requests", 0) for row in status.get("usage", [])):
            raise ValueError("AUTOMATIC_MODEL_RECONCILIATION_REQUIRED")
        jobs = {row["state"]: row["count"] for row in status["jobs"]}
        deliveries = {row["state"]: row["count"] for row in status["deliveries"]}
        if not any(jobs.get(state, 0) for state in ("QUEUED", "RUNNING", "RETRY_WAIT")):
            if not deliveries.get("PENDING", 0):
                raise ValueError("AUTOMATIC_REPLY_NOT_CREATED")
            if not sending:
                if time.monotonic() >= deadline:
                    raise ValueError("AUTOMATIC_REPLY_TIMEOUT")
                sending = True
                deadline = time.monotonic() + SEND_PROGRESS_SECONDS + interval
            await pump_private(window, client, protocol, status=status)
            if window.row()["state"] == "STOPPED":
                await window.stop_core(client)
                return
            current = window.db.execute(
                "SELECT COUNT(*) FROM window_attempts WHERE event_id=? AND state='ACK'",
                (event_id,),
            ).fetchone()[0]
            if current > acknowledged:
                acknowledged = current
                deadline = time.monotonic() + SEND_PROGRESS_SECONDS + interval
        if time.monotonic() >= deadline:
            raise ValueError("AUTOMATIC_REPLY_TIMEOUT")
        await asyncio.sleep(max(0.1, window.plan.config.poll_seconds))
    emit("automatic_input_delivered", event_id=event_id)


async def approve_row(window, client, row):
    value = {"window_sha256": window.plan.sha256, "event_id": row["event_id"],
             "input_sha256": row["input_hash"],
             "capture_sha256": digest(json.loads(row["capture_json"]))}
    return await window.approve_captured(client, {
        **value, "evidence_sha256": evidence(window, "input", value),
    }, defer_media=True)


class MemePump:
    """Use idle time for bounded discovery; owner input interrupts public I/O."""

    def __init__(self, window, client):
        self.window, self.client, self.task = window, client, None
        self.query_task = None
        self.query_event = None
        self.query_tasks = set()
        self.enabled = window.plan.config.meme_enabled
        self.submitted_event = None
        self.next_run = 0
        self.runtime = window.memes if self.enabled else None

    def interrupt(self):
        self._interrupt_background()
        for task in self.query_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    def _interrupt_background(self):
        if self.task is not None and not self.task.done() and not self.task.cancelling():
            self.task.cancel()

    def admitted(self, event_id):
        if not self.enabled or not self.runtime.semantics_enabled:
            return
        if event_id == self.query_event:
            return
        row = self.window.db.execute(
            "SELECT w.* FROM window_inputs w JOIN inbox i ON i.key=w.event_id "
            "WHERE w.event_id=? AND w.plan_hash=? AND i.state='accepted'",
            (event_id, self.window.plan.sha256),
        ).fetchone()
        if row is None or row["state"] in {"ACK", "CANCELLED", "SUPERSEDED", "RECALLED"}:
            return
        if self.window.db.execute(
            "SELECT 1 FROM meme_runtime_semantic_offers WHERE event_id=?", (event_id,),
        ).fetchone():
            return
        self.interrupt()
        self.query_event = event_id
        self.query_task = asyncio.create_task(self._query(row))
        self.query_tasks.add(self.query_task)
        self.query_task.add_done_callback(self.query_tasks.discard)

    async def _query(self, row):
        try:
            # The shared budget finishes before Core's bounded candidate window.
            async with asyncio.timeout(OFFER_QUERY_SECONDS):
                self.runtime._admitted_check(row["event_id"])
                state = await self.window.input_state(self.client, row)
                await self.runtime.semantic_offer(
                    self.client, row["event_id"], reply_revision=state["reply_revision"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            emit("meme_semantic_offer", status="DEFERRED")

    async def execute(self, row):
        try:
            runtime = self.runtime
            if row is not None and row["event_id"] != self.submitted_event:
                offer = runtime.missing_offer(row["event_id"])
                if offer:
                    state = await self.window.input_state(self.client, row)
                    result = await runtime.submit_missing(self.client, row["event_id"], offer,
                                                         reply_revision=state["reply_revision"])
                    if result.get("status") != "DEFERRED":
                        self.submitted_event = row["event_id"]
                else:
                    self.submitted_event = row["event_id"]
            result = await runtime.run_once(self.client)
            if result.get("status") not in {"IDLE", "DISABLED", "BUSY"}:
                emit("meme_discovery", status=result["status"])
        except asyncio.CancelledError:
            raise
        except Exception:
            # Discovery cannot prevent an otherwise authorized text reply.
            emit("meme_discovery", status="DEFERRED")

    def tick(self, *, idle):
        if not self.enabled:
            return
        if self.runtime.semantics_enabled:
            latest = self.window.db.execute(
                "SELECT event_id FROM window_inputs WHERE plan_hash=? ORDER BY rowid DESC LIMIT 1",
                (self.window.plan.sha256,),
            ).fetchone()
            if latest:
                self.admitted(latest[0])
            if self.query_task is not None and self.query_task.done():
                if not self.query_task.cancelled():
                    self.query_task.result()
                self.query_task = None
        if self.task is not None:
            if not self.task.done():
                if not idle:
                    self._interrupt_background()
                return
            if not self.task.cancelled():
                self.task.result()
            self.task = None
            self.next_run = time.monotonic() + 5
        if not idle or time.monotonic() < self.next_run:
            return
        if self.query_task is not None and not self.query_task.done():
            return
        row = self.window.db.execute(
            "SELECT * FROM window_inputs WHERE plan_hash=? ORDER BY rowid DESC LIMIT 1",
            (self.window.plan.sha256,),
        ).fetchone()
        if row is not None and row["state"] != "ACK":
            return
        self.task = asyncio.create_task(self.execute(row))

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def close(self):
        self.interrupt()
        tasks = [*self.query_tasks, *([self.task] if self.task is not None else [])]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def run_interleaved(window, client, protocol, source, pause, media, memes, profiles):
    """Admit received owner turns before waiting for generation or the next reply bubble."""
    watches = {}
    interval = (reply_segments.scope(window.plan) or {}).get("interval_ms", 0) / 1000
    while True:
        source.check()
        if pause.is_set():
            await profiles.close()
            await memes.close()
            if media.task is not None:
                await media.task
            emit("automatic_service_paused")
            return window.status()
        window.verify()
        if pending_input(window, "") is not None or window.db.execute(
            "SELECT 1 FROM automatic_recalls WHERE window_sha256=? AND state='PENDING' LIMIT 1",
            (window.plan.sha256,),
        ).fetchone():
            profiles.interrupt()
            memes.interrupt()
        await flush_recalls(window, client)
        await pokes.flush(window, client)
        rows = window.db.execute(
            "SELECT * FROM window_inputs WHERE plan_hash=? AND state "
            "NOT IN ('ACK','CANCELLED','SUPERSEDED','RECALLED') ORDER BY rowid",
            (window.plan.sha256,),
        ).fetchall()
        captured = next((row for row in rows if row["state"] == "CAPTURED"), None)
        if captured is not None:
            await approve_row(window, client, captured)
            continue
        if any(row["state"] != "APPROVED" for row in rows):
            raise ValueError("AUTOMATIC_INPUT_RECONCILIATION_REQUIRED")
        if window.spool.pending_count:
            async with window.core_http(client) as scoped:
                accepted = await window.spool.flush_one(scoped)
            if accepted:
                memes.admitted(accepted["event_id"])
            continue
        if observed := source.first():
            await capture_observed(window, client, protocol, observed)
            source.consume(observed)
            continue
        media.tick()
        status = await core_status(window, client)
        if any(row.get("unknown_usage_requests", 0) for row in status.get("usage", [])):
            raise ValueError("AUTOMATIC_MODEL_RECONCILIATION_REQUIRED")
        for row in rows:
            value = await window.input_state(client, row)
            if value["state"] in {"SUPERSEDED", "RECALLED"}:
                from .pending_media import cancel

                cancel(window, row["event_id"], value)
                if memes.enabled:
                    window.memes.cancel(row["event_id"])
                watches.pop(row["event_id"], None)
                if not (value["unsettled_model"] or value["unsettled_delivery"]):
                    window.retire_input(row, value)
                    emit("automatic_input_retired", event_id=row["event_id"], state=value["state"])
                continue
            if value["state"] == "FAILED":
                raise ValueError("AUTOMATIC_REPLY_NOT_CREATED")
            acknowledged = window.db.execute(
                "SELECT COUNT(*) FROM window_attempts WHERE event_id=? AND state='ACK'",
                (row["event_id"],),
            ).fetchone()[0]
            watch = watches.setdefault(row["event_id"], {
                "deadline": time.monotonic() + REPLY_WAIT_SECONDS,
                "sending": False, "acknowledged": acknowledged,
            })
            if media.pending(row["event_id"]) or (media.task is not None and not media.task.done()):
                # The media request has its own Core absolute deadline and bounded
                # HTTP timeout; it may also be settling the shared provider slot.
                watch["deadline"] = time.monotonic() + REPLY_WAIT_SECONDS
            if ((value["state"] == "REPLIED" and not watch["sending"])
                or acknowledged > watch["acknowledged"]):
                watch.update(sending=True, acknowledged=acknowledged,
                             deadline=time.monotonic() + SEND_PROGRESS_SECONDS + interval)
            if time.monotonic() >= watch["deadline"]:
                raise ValueError("AUTOMATIC_REPLY_TIMEOUT")
        # Re-enter the inbox between individual sends. A reply's pacing interval
        # never holds admission of a later input or a recall behind the old ACK.
        foreground = bool(rows or any(row["count"] for row in status["deliveries"]
                                     if row["state"] in {"PENDING", "LEASED", "SENDING"}))
        media_running = media.task is not None and not media.task.done()
        profiles.tick(idle=not foreground and not media_running and not memes.running)
        memes.tick(idle=not foreground and not media_running and not profiles.running)
        if not media_running and not memes.running and not profiles.running:
            await pump_private(window, client, protocol, status=status)
        if window.row()["state"] == "STOPPED":
            await window.stop_core(client)
            await flush_recalls(window, client)
            return window.status()
        for event_id in list(watches):
            row = window.db.execute("SELECT state FROM window_inputs WHERE event_id=?",
                                     (event_id,)).fetchone()
            if row["state"] == "ACK":
                watches.pop(event_id)
                emit("automatic_input_delivered", event_id=event_id)
        source.available.clear()
        if source.first() is not None:
            continue
        try:
            await asyncio.wait_for(source.available.wait(),
                                   max(0.1, window.plan.config.poll_seconds) if watches else 1)
        except TimeoutError:
            pass


async def run(window, client, protocol, *, pause=None, image_transport=None, image_resolver=None,
              file_transport=None, file_resolver=None, voice_transport=None, voice_resolver=None,
              profile_transport=None, profile_resolver=None):
    """Resume known work. UNKNOWN or partial irreversible steps stay stopped."""
    if (window.plan.mode != "live_text"
        or window.plan.value["live_text"].get("automatic_reply") is not True):
        raise ValueError("AUTOMATIC_OWNER_REPLY_APPROVAL_REQUIRED")
    pause = pause or asyncio.Event()
    media = None
    memes = None
    profiles = None
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, pause.set)
            installed.append(sig)
        except NotImplementedError:
            pass
    try:
        await pokes.flush(window, client)
        if window.row()["state"] == "STOPPED":
            await window.stop_core(client)
            await flush_recalls(window, client)
            return window.status()
        require_scope(window)
        window.verify()
        async with OwnerSource(window, protocol, pause) as source:
            emit("automatic_window_running", window_id=window.plan.value["window_id"],
                 ends_at=window.plan.value["ends_at"])
            if interruptible(window):
                from .pending_media import MediaPump
                from .profile_probe import ProfilePump

                media = MediaPump(window, client, image_transport=image_transport,
                    image_resolver=image_resolver, protocol=protocol,
                    file_transport=file_transport, file_resolver=file_resolver,
                    voice_transport=voice_transport, voice_resolver=voice_resolver)
                memes = MemePump(window, client)
                profiles = ProfilePump(window, client, protocol, transport=profile_transport,
                    **({"resolver": profile_resolver} if profile_resolver is not None else {}))
                return await run_interleaved(window, client, protocol, source, pause, media,
                                             memes, profiles)
            while True:
                require_scope(window)
                rows = window.db.execute(
                    "SELECT * FROM window_inputs WHERE plan_hash=? AND state!='ACK' ORDER BY rowid",
                    (window.plan.sha256,),
                ).fetchall()
                if len(rows) > 1:
                    raise ValueError("AUTOMATIC_INPUT_RECONCILIATION_REQUIRED")
                if not rows:
                    async def idle():
                        if (window.plan.value["live_text"].get("companion_scope")
                            == "owner_private_v1"):
                            await pump_private(window, client, protocol)

                    observed = await source.next(idle)
                    if observed is None:
                        emit("automatic_service_paused")
                        return window.status()
                    result = await capture_observed(window, client, protocol, observed)
                    source.consume(observed)
                    if "input_approval" not in result:
                        continue
                    rows = window.db.execute(
                        "SELECT * FROM window_inputs WHERE event_id=?",
                        (result["input_approval"]["event_id"],),
                    ).fetchall()
                row = rows[0]
                if row["state"] == "CAPTURED":
                    value = {"window_sha256": window.plan.sha256, "event_id": row["event_id"],
                             "input_sha256": row["input_hash"],
                             "capture_sha256": digest(json.loads(row["capture_json"]))}
                    await window.approve_captured(client, {
                        **value, "evidence_sha256": evidence(window, "input", value),
                    })
                elif row["state"] == "APPROVED":
                    # Upload is idempotent. Never repeat image download/inference or send I/O.
                    async with window.core_http(client) as scoped:
                        await window.spool.flush_one(scoped)
                else:
                    raise ValueError("AUTOMATIC_INPUT_RECONCILIATION_REQUIRED")
                await finish_input(window, client, protocol, source, row["event_id"])
                if window.row()["state"] == "STOPPED":
                    return window.status()
    except BaseException as error:
        reason = ("WINDOW_EXPIRED" if window.clock() >= window.plan.end else
                  "OWNER_STOP" if window.stop_path.exists() else "AUTOMATIC_RUN_STOPPED")
        await window.stop_live(client, reason)
        if isinstance(error, ValueError) and str(error) in {
            "WINDOW_STOPPED", "WINDOW_OUTSIDE_INTERVAL",
        }:
            return window.status()
        raise
    finally:
        if profiles is not None:
            await profiles.close()
        if memes is not None:
            await memes.close()
        if media is not None:
            await media.close()
        for sig in installed:
            asyncio.get_running_loop().remove_signal_handler(sig)
