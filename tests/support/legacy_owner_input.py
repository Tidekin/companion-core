import hashlib
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, paid_budget
from companion.config import LiveTextWindowApproval
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.private_life import event_digest
from companion.qq_rich import input_digest
from companion.store import after, encode, uid
from companion.transport import enqueue_reply_parts, reply_group, segment_envelopes
from tests.conftest import event
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live


def archived(core, tmp_path, *, private=False, segmented=False):
    """Seed original signed DB/journal bytes, as read from a pre-cutover archive.

    No live input guard is patched and no old writer is run as a current worker.
    The synthetic mock ingest only supplies the native historical event/job shape.
    """
    key = Ed25519PrivateKey.generate()
    options = dict(execution="approved_live")
    if private:
        options.update(automatic_reply=True, companion_scope="owner_private_v1",
            companion_approval_sha256="a" * 64, memory_scope="owner_private",
            memory_approval_sha256="b" * 64, history_scope="owner_private",
            history_approval_sha256="c" * 64, max_history_turns=5, max_history_utf8_bytes=8000)
    if segmented:
        options["segmented_reply"] = {"max_segments": 3, "interval_ms": 0}
        options["max_send_attempts"] = 5
    approved_live(core, tmp_path, window_id="win_" + "1" * 32)
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model)
    policy = LiveTextWindowApproval(
        live_approved=True, plan_sha256="e" * 64, window_id=core.settings.paid_model.window_id,
        starts_at=after(core.clock(), -60), ends_at=after(core.clock(), 30),
        core_config_sha256="a" * 64, core_code_sha256="b" * 64,
        bridge_code_sha256="c" * 64, candidate_sha="d" * 40,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        budget_journal_id=core.store.one(
            "SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'")["value"],
        custodian_sha256="f" * 64, approval_sha256="a" * 64,
        capture_public_key=key.public_key().public_bytes_raw().hex(), **options)
    core.settings.live_text_window = None
    core.settings.mode = "mock"
    message = event(core, text="Archived signed owner input",
                    origin="onebot-direct:FriendMessage:10001")
    source_id = (f"{core.settings.bot_qq_id}:{message.conversation.platform_session_key}"
                 ":epoch:old-epoch:1")
    message = message.model_copy(update={"source_event_id": source_id,
        "event_id": "evt_" + hashlib.sha256(source_id.encode()).hexdigest()})
    accepted = core.ingest(message, "archived-input")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted["job_id"],))
    job = core.store.one((
        'SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
        'USING(event_id) WHERE job_id=%s'
    ), (accepted["job_id"],))
    core.settings.mode = "live"
    captured = dict(kind="authenticated_onebot_ws", window_sha256=policy.plan_sha256,
        event_id=message.event_id, source_event_id=source_id,
        input_sha256=input_digest(message.payload.model_dump(mode="json")),
        frame_sha256="a" * 64, evidence_sha256="b" * 64,
        identity_sha256=paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id]),
        config_sha256="c" * 64, protocol_epoch="old-epoch",
        received_at=datetime.fromisoformat(core.clock()).isoformat(),
        occurred_at=message.occurred_at.isoformat())
    captured["signature"] = key.sign(
        b"companion-core/onebot-capture/v1\x00" + encode(captured).encode()).hex()
    binding = dict(approval_sha256=paid_budget.sha(policy.model_dump(mode="json")),
        privacy_epoch=0, plan_sha256=policy.plan_sha256, window_id=policy.window_id,
        budget_journal_id=policy.budget_journal_id,
        budget_journal_path=str(Path(core.settings.paid_model.journal_path).resolve()))
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model, dict(kind="window_open",
            window_id=policy.window_id, plan_sha256=policy.plan_sha256, handoff_sha256=None))
        core.store.insert("runtime_meta", key="live_text_input:" + message.event_id,
            value=encode(dict(window_sha256=policy.plan_sha256, event_id=message.event_id,
                              privacy_epoch=0, input_sha256=captured["input_sha256"],
                              capture=captured)))
        core.store.insert("runtime_meta", key="live_text_window:" + policy.plan_sha256,
            value=encode(dict(state="STOPPED", approval=policy.model_dump(mode="json"),
                              binding=binding)))
        core.store.insert("model_budget_windows", window_id=policy.window_id,
                          contract_sha256=policy.paid_contract_sha256)
        request = uid("request")
        payload = paid_budget.sha({"historical_payload": message.event_id})
        core.store.insert("model_usage", usage_id=uid("usage"), request_id=request,
            job_id=job["job_id"], scenario="chat", provider=core.settings.provider,
            model=core.settings.model_name, input_tokens=100, output_tokens=20,
            billed_micro_usd=140, is_estimate=0, pricing_known=1, state="SETTLED",
            occurred_at=core.clock())
        core.store.insert("model_admissions", request_id=request, logical_request_id=request,
            attempt_id=uid("attempt"), attempt_number=1, window_id=policy.window_id,
            contract_sha256=policy.paid_contract_sha256, payload_sha256=payload,
            snapshot_json=encode(paid_budget.snapshot(core.settings)), reserved_micro_usd=140,
            input_bound=100, output_bound=20, journal_path=binding["budget_journal_path"],
            journal_id=policy.budget_journal_id, lease_token=uid("oldlease"),
            lease_until=after(core.clock(), -1), io_started_at=core.clock())
        paid_budget.journal(core.store, core.settings.paid_model, dict(kind="settle",
            request_id=request, state="SETTLED", minimum_micro_usd=140,
            contract_sha256=policy.paid_contract_sha256, io_started_at=core.clock()))
        core.store.insert("runtime_meta", key="live_text_context:" + job["job_id"],
            value=encode(dict(plan_sha256=policy.plan_sha256, privacy_epoch=0,
                conversation_id=job["conversation_id"], event_id=message.event_id,
                sources=[], payload_sha256=payload)))
        core.store.insert("runtime_meta", key="private_life_model:" + request,
            value=encode(dict(plan_sha256=policy.plan_sha256, job_id=job["job_id"],
                              scenario="chat", payload_sha256=payload)))
        parts = ["Already sent.", "Unsent tail."] if segmented else ["Already sent."]
        mids = enqueue_reply_parts(core.store, job, parts, 0, core.clock())
        group = reply_group(core.store, mids[0])
        envelopes = segment_envelopes(group)
        original = core.store.one('SELECT * FROM events WHERE event_id=%s', (message.event_id,))
        for i, mid in enumerate(mids):
            row = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (mid,))
            core.store.execute((
                'UPDATE outbox SET state=%s,sent_at=%s,'
                'provider_message_id=%s WHERE message_id=%s'
            ), ("SENT" if i == 0 else "CANCELLED",
                core.clock() if i == 0 else None, "1234" if i == 0 else None, mid))
            proof = dict(message_id=mid, event_id=message.event_id,
                         plan_sha256=policy.plan_sha256, privacy_epoch=0)
            if private:
                proof.update(event_sha256=event_digest(original),
                    content_sha256=row["content_sha256"], delivery_key=row["delivery_key"],
                    action_type=row["action_type"], request_ids=[request])
            else:
                proof.update(job_id=job["job_id"], request_id=request,
                              reply_sha256=paid_budget.sha(parts[i]))
                if segmented:
                    proof["reply_segments"] = envelopes[mid]
            core.store.insert("runtime_meta", key=("private_life_delivery:" if private
                else "live_text_reply:") + mid, value=encode(proof))
        paid_budget.journal(core.store, core.settings.paid_model, dict(kind="window_stop",
            window_id=policy.window_id, plan_sha256=policy.plan_sha256), allow_settlement=True)
    core.clock.advance(60)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    current = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id,
        purposes=["chat", "history", "memory"], authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture=dict(public_key=key.public_key().public_bytes_raw().hex(),
            protocol_epoch="new-epoch", identity_sha256=captured["identity_sha256"]),
        history=dict(scope="owner_private", not_before=after(core.clock(), -3600),
                     max_turns=5, max_utf8_bytes=8000), memory_scope="owner_private")
    activate(core, current)
    view = owner_authority.current(core.store, core.settings, core.clock)
    return policy, view, job, mids, key
