"""Synthetic retained v1 custody for original revocation and read-only validators.

This follows tests.test_legacy_owner_input.archived and the schema/record seeding in
tests.test_native_image_migration. These fixtures do not execute retired approvals
or invoke a model/provider.
The signed capture, terminal rows and fees below are explicit historical fixtures,
not evidence of a new execution. The real HTTP Core opens its own Store.
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import live_text, paid_budget
from companion.core import Core
from companion.privacy import epoch
from companion.qq_rich import input_digest
from companion.store import after, encode, uid
from companion_onebot_bridge import journal
from companion_onebot_bridge.capture import write_evidence
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow
from tests.support.core_http import real_core_http
from tests.support.onebot_events import raw_event
from tests.test_onebot_capture import pinned_plan
from tests.test_private_companion import companion_options


def settled_usage(core, approval, job, *, scenario):
    """Retained settled accounting uses the existing budget journal grammar."""
    request = uid("model")
    contract = paid_budget.sha(paid_budget.snapshot(core.settings))
    path = str(Path(core.settings.paid_model.journal_path).resolve())
    core.store.execute("INSERT OR IGNORE INTO model_budget_windows VALUES (?,?)",
                       (approval.window_id, contract))
    core.store.insert("model_usage", usage_id=uid("usage"), request_id=request,
        job_id=job["job_id"], scenario=scenario, provider=core.settings.provider,
        model=core.settings.model_name, input_tokens=100, output_tokens=20,
        billed_micro_usd=140, is_estimate=0, pricing_known=1, state="SETTLED",
        occurred_at=core.clock())
    core.store.insert("model_admissions", request_id=request, logical_request_id=job["job_id"],
        attempt_id=uid("attempt"), attempt_number=1, window_id=approval.window_id,
        contract_sha256=contract, payload_sha256=paid_budget.sha({"archived_job": job["job_id"]}),
        snapshot_json=encode(paid_budget.snapshot(core.settings)), reserved_micro_usd=200,
        input_bound=100, output_bound=20, journal_path=path,
        journal_id=approval.budget_journal_id, lease_token=uid("model_lease"),
        lease_until=after(core.clock(), -1), io_started_at=core.clock())
    for kind, state, amount in (("reserve", "RESERVED", 200), ("settle", "SETTLED", 140)):
        paid_budget.journal(core.store, core.settings.paid_model, dict(kind=kind,
            request_id=request, state=state, minimum_micro_usd=amount,
            contract_sha256=contract, **({"io_started_at": core.clock()} if kind == "settle"
                                       else {})), allow_settlement=True)
    return request


def seed_input(core, window, *, failure, number=1, text="今天想聊聊茶", message=None):
    """Populate one already admitted historical input; no ingest or approval endpoint."""
    cfg, plan, now = window.plan.config, window.plan, core.clock()
    raw = raw_event(number, text=text, time=int(core.clock.now.timestamp()),
                    **({"message": message} if message is not None else {}))
    frame = encode(raw)
    event = normalize(frame, cfg, now=core.clock.now.timestamp(),
                      rich_input_scope="references_v1" if failure == "image" else "none",
                      image_scope="owner_image_v1" if failure == "image" else "none")
    conversation = core.store.one("SELECT conversation_id FROM conversations WHERE platform='qq' "
        "AND platform_session_key=?", (cfg.origin,))
    conversation_id = conversation["conversation_id"] if conversation else uid("conversation")
    if not conversation:
        core.store.insert("conversations", conversation_id=conversation_id, platform="qq",
            platform_session_key=cfg.origin, scope="private", owner_actor_id="owner",
            created_at=now, reply_revision=number)
    else:
        core.store.execute("UPDATE conversations SET reply_revision=? WHERE conversation_id=?",
                           (number, conversation_id))
    core.store.insert("events", event_id=event["event_id"], schema_version=1, source="qq.onebot",
        source_event_id=event["source_event_id"], conversation_id=conversation_id, actor_id="owner",
        event_type=event["event_type"], occurred_at=event["occurred_at"], ingested_at=now,
        privacy_scope="private", body_json=encode(event["payload"]), trace_id=event["trace_id"],
        fingerprint=paid_budget.sha(event), reply_revision=number)
    state = "FAILED" if failure in {"budget", "memory"} else "CANCELLED"
    code = {"budget": "MONTHLY_BUDGET_EXHAUSTED", "memory": "MEMORY_REVIEW_INVALID"}.get(
        failure, "LIVE_WINDOW_STOPPED")
    job_id = uid("job")
    core.store.insert("jobs", job_id=job_id, event_id=event["event_id"], kind="chat", state=state,
        idempotency_key=job_id, attempts=1, last_error_code=code, created_at=now, updated_at=now)
    core.store.insert("job_sources", job_id=job_id, event_id=event["event_id"])
    job = core.store.one("SELECT j.*,e.conversation_id,e.trace_id FROM jobs j JOIN events e "
                         "USING(event_id) WHERE job_id=?", (job_id,))
    approval = core.settings.live_text_window
    capture = dict(kind="authenticated_onebot_ws", window_sha256=plan.sha256,
        event_id=event["event_id"], source_event_id=event["source_event_id"],
        input_sha256=input_digest(event["payload"]),
        frame_sha256=hashlib.sha256(frame.encode()).hexdigest(),
        identity_sha256=plan.value["identity_sha256"], config_sha256=plan.value["config_sha256"],
        protocol_epoch=cfg.protocol_epoch, received_at=core.clock.now.isoformat(),
        occurred_at=event["occurred_at"])
    evidence = {"receipt": dict(capture), "frame": frame, "peer": ["127.0.0.1", 3001],
        "endpoint": cfg.onebot_ws_url, "protocol": "OneBot-v11/SnowLuma/2.0.3-node",
        "bearer_accepted": True}
    capture["evidence_sha256"] = journal.digest(evidence)
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(cfg.capture_signing_key))
    capture["signature"] = key.sign(
        live_text.CAPTURE_SIGNATURE_DOMAIN + encode(capture).encode()).hex()
    assert write_evidence(window, evidence) == capture["evidence_sha256"]
    approval_value = {"window_sha256": plan.sha256, "event_id": event["event_id"],
        "input_sha256": capture["input_sha256"], "capture": capture, "evidence_sha256": "a" * 64}
    saved = live_text.InputApproval.model_validate(approval_value).model_dump(mode="json")
    core.store.insert("runtime_meta", key=live_text.INPUT + event["event_id"],
        value=encode({**saved, "privacy_epoch": epoch(core.store)}))
    approval_hash = journal.digest({key: value for key, value in approval_value.items()
        if key != "capture"} | {"capture_sha256": journal.digest(capture)})
    window.db.execute("INSERT INTO window_inputs VALUES(?,?,?,'APPROVED',?,?)",
        (event["event_id"], capture["input_sha256"], approval_hash, encode(capture), plan.sha256))
    window.db.execute("UPDATE test_window SET fence=?", (number,))
    for kind, data in (("CAPTURE_STARTED", {"fence": number}),
        ("CAPTURED", {"event_hash": journal.digest(event["event_id"]),
        "capture_sha256": journal.digest(capture), "frame_sha256": capture["frame_sha256"]}),
        ("INPUT_APPROVED", {"event_hash": journal.digest(event["event_id"]),
                             "approval_sha256": approval_hash})):
        append_history(window, kind, data)
    if failure in {"memory", "image"}:
        scenario = "memory_review" if failure == "memory" else "content_video"
        request = settled_usage(core, approval, job, scenario=scenario)
        if failure == "image":
            core.store.insert("content_items", item_id=uid("content"),
                owner_event_id=event["event_id"],
                kind="qq_image", slot=0, state="UNKNOWN", captured_privacy_epoch=epoch(core.store),
                usage_request_id=request, deadline=now, created_at=now, updated_at=now)
    if failure == "memory":
        core.store.audit("memory.recall_invalid", job_id, event["trace_id"], now)
    if state == "FAILED":
        core.store.audit("job.failed", job_id, event["trace_id"], now, code=code, state=state)
    return job


def append_history(window, kind, data):
    head = journal.append(window.plan.journal, kind, data, at=window.clock().isoformat(),
                          expected_head=window.row()["journal_head"])
    with window.db:
        window.db.execute("UPDATE test_window SET journal_head=?", (head,))


@asynccontextmanager
async def historical_input(core, tmp_path, *, failure="budget", scope_options=None, message=None,
                           terminal=None, stopped=True, rich_input_scope="none"):
    with Core(core.settings, clock=core.clock) as api_core:
        assert api_core.store is not core.store
        async with real_core_http(api_core) as port:
            cfg, plan = pinned_plan(core, tmp_path, 3001, core_port=port,
                live=True, approved=True, scope_options=scope_options or companion_options(core),
                rich_input_scope="references_v1" if failure == "image" else rich_input_scope)
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                spool.db.executescript(
                    "CREATE TABLE test_window(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                    "plan_hash TEXT NOT NULL,plan_json TEXT NOT NULL,journal_head TEXT NOT NULL,"
                    "state TEXT NOT NULL,fence INTEGER NOT NULL,initial_counts TEXT NOT NULL,"
                    "initial_inbox_rowid INTEGER NOT NULL,reason TEXT);"
                    "CREATE TABLE window_inputs(event_id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,"
                    "approval_hash TEXT NOT NULL,state TEXT NOT NULL,capture_json TEXT,"
                    "plan_hash TEXT NOT NULL);"
                    "CREATE TABLE window_attempts(message_id TEXT PRIMARY KEY,"
                    "event_id TEXT NOT NULL,"
                    "fence INTEGER NOT NULL,state TEXT NOT NULL,text_hash TEXT NOT NULL,"
                    "approval_hash TEXT NOT NULL,provider_ack_hash TEXT,item_hash TEXT NOT NULL,"
                    "plan_hash TEXT NOT NULL);"
                    "CREATE TABLE window_history(plan_hash TEXT PRIMARY KEY,"
                    "record_json TEXT NOT NULL,"
                    "retirement_head TEXT NOT NULL,successor_hash TEXT NOT NULL);")
                head = journal.append(plan.journal, "OPEN", {"plan_sha256": plan.sha256},
                    at=core.clock.now.isoformat(), expected_head="0" * 64, create=True)
                state = "STOPPED" if stopped else "OPEN"
                spool.db.execute("INSERT INTO test_window VALUES(1,?,?,?,?,0,'{}',0,?)",
                    (plan.sha256, encode(plan.value), head, state,
                     "OWNER_STOP" if stopped else None))
                spool.db.commit()
                approval = core.settings.live_text_window
                paid_budget.journal(core.store, core.settings.paid_model, dict(kind="window_open",
                    window_id=approval.window_id, plan_sha256=plan.sha256, handoff_sha256=None))
                job = seed_input(core, window, failure=failure, message=message)
                if terminal == "SUPERSEDED":
                    seed_input(core, window, failure="cancelled", number=2)
                elif terminal == "RECALLED":
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
                if stopped:
                    paid_budget.journal(core.store, core.settings.paid_model,
                        dict(kind="window_stop", window_id=approval.window_id,
                             plan_sha256=plan.sha256), allow_settlement=True)
                budget_path, erasures = await asyncio.gather(
                    asyncio.to_thread(Path(core.settings.paid_model.journal_path).resolve),
                    asyncio.to_thread(Path(core.settings.erasure_ledger).read_bytes))
                binding = dict(approval_sha256=paid_budget.sha(approval.model_dump(mode="json")),
                    privacy_epoch=epoch(core.store), plan_sha256=plan.sha256,
                    window_id=approval.window_id, budget_journal_id=approval.budget_journal_id,
                    budget_journal_path=str(budget_path),
                    erasure_ledger_sha256=hashlib.sha256(erasures).hexdigest())
                for name, value in ((live_text.BINDING, binding),
                    (live_text.HISTORY + plan.sha256, {"state": state, "binding": binding,
                        "approval": approval.model_dump(mode="json")})):
                    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                                       (name, encode(value) if isinstance(value, dict) else value))
                if stopped:
                    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                                       (live_text.STOP + plan.sha256, "OWNER_STOP"))
                    append_history(window, "STOP", {"reason": "OWNER_STOP"})
                    append_history(window, "CORE_STOP_CONFIRMED", {})
                    journal.stop_marker(window.stop_path)
                assert not core.store.rows("PRAGMA foreign_key_check")
                window.verify(active=False)
                async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                    yield window, client, job
