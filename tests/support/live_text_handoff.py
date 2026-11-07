import hashlib
from datetime import timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import live_text, paid_budget
from companion.config import LiveTextWindowApproval
from companion.privacy import epoch
from companion.store import encode
from tests.support.paid_model import approved_live

KEY = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("e" * 64))


def configure(core, tmp_path, *, successor=False, handoff_changes=None):
    old = core.settings.live_text_window
    core.settings.max_recent_messages = 0
    paid = approved_live(
        core,
        tmp_path,
        window_id="win_" + ("2" if successor else "1") * 32,
        concurrency=1,
        per_request_calls=1,
        window_calls=5,
    )
    with core.store.transaction():
        custody = paid_budget.journal(core.store, paid, allow_settlement=successor)
    handoff = None
    if successor:
        handoff = {
            "predecessor_plan_sha256": old.plan_sha256,
            "predecessor_window_id": old.window_id,
            "budget_journal_head_sha256": custody["head_sha256"],
            "bridge_journal_head_sha256": "b" * 64,
            "identity_sha256": paid_budget.sha(
                [core.settings.owner_qq_id, core.settings.bot_qq_id]
            ),
            "protocol_epoch": "SYNTHETIC_ORIGINAL_EPOCH",
            "privacy_epoch": epoch(core.store),
            "erasure_ledger_sha256": hashlib.sha256(
                Path(core.settings.erasure_ledger).read_bytes()
            ).hexdigest(),
            "evidence_sha256": "d" * 64,
            **(handoff_changes or {}),
        }
    core.settings.live_text_window = LiveTextWindowApproval(
        live_approved=True,
        window_id=paid.window_id,
        plan_sha256=("2" if successor else "1") * 64,
        starts_at=core.clock.now - timedelta(seconds=60),
        ends_at=core.clock.now + timedelta(seconds=600),
        core_config_sha256=live_text.config_sha256(core.settings),
        core_code_sha256=live_text.code_sha256(),
        bridge_code_sha256="b" * 64,
        candidate_sha="c" * 40,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        budget_journal_id=core.store.one(
            "SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'"
        )["value"],
        custodian_sha256="c" * 64,
        approval_sha256="d" * 64,
        capture_public_key=KEY.public_key().public_bytes_raw().hex(),
        handoff=handoff,
    )
    return core.settings.live_text_window


def reference(approval):
    return live_text.WindowReference(window_sha256=approval.plan_sha256)


def journal_bytes(core):
    return Path(core.settings.paid_model.journal_path).read_bytes()


def initial(core, tmp_path):
    approval = configure(core, tmp_path)
    assert live_text.open_window(core.store, core.settings, core.clock, reference(approval))[
        "approved"
    ]
    return approval


def stop_and_successor(core, tmp_path, **changes):
    old = core.settings.live_text_window
    live_text.stop_window(core.store, reference(old))
    new = configure(core, tmp_path, successor=True, **changes)
    return old, new


def bridge_receipt(approval, prepared, **changes):
    value = {
        "kind": "stopped_window_handoff",
        "window_sha256": approval.plan_sha256,
        "predecessor_window_sha256": approval.handoff.predecessor_plan_sha256,
        "handoff_sha256": paid_budget.sha(approval.handoff.model_dump(mode="json")),
        "core_receipt_sha256": paid_budget.sha(prepared),
        "bridge_journal_head_sha256": "f" * 64,
        **changes,
    }
    signature = KEY.sign(live_text.HANDOFF_SIGNATURE_DOMAIN + encode(value).encode()).hex()
    return {**value, "signature": signature}


def open_successor(core, new, prepared):
    body = live_text.OpenWindow(
        window_sha256=new.plan_sha256, handoff_receipt=bridge_receipt(new, prepared)
    )
    return live_text.open_window(core.store, core.settings, core.clock, body)
