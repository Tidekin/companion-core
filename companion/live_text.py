"""Core-owned approval receipts for the existing bounded text/send path.

This module never delivers messages or estimates provider contracts. Paid admission
and its independent journal remain in budget/paid_budget; delivery remains Outbox.
"""

import hashlib
import hmac
import json
import re
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, field_validator, model_validator

import companion_delivery_protocol as delivery_wire
from companion import (
    cumulative,
    image_spool,
    input_recall,
    paid_budget,
    private_life,
    qq_files,
    qq_images,
)
from companion.contracts import CoreError, StrictModel
from companion.expression import MemeOffer
from companion.privacy import ErasureLedger, epoch
from companion.qq_rich import input_digest as _input_digest
from companion.store import after, encode, utcnow

BINDING = "live_text_window"
INPUT = "live_text_input:"
CONTEXT = "live_text_context:"
MEMORY = "live_text_memory:"
CAPTURE_SIGNATURE_DOMAIN = b"companion-core/onebot-capture/v1\x00"
HANDOFF_SIGNATURE_DOMAIN = b"companion-core/window-handoff/v1\x00"
HISTORY = "live_text_window:"
STOP = "live_text_stopped:"
LEGACY_STOP = "live_text_legacy_stop"


class WindowReference(StrictModel):
    window_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AbortHandoffReference(WindowReference):
    bridge_journal_head_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class HandoffReceipt(WindowReference):
    kind: Literal["stopped_window_handoff"]
    predecessor_window_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    handoff_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    core_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bridge_journal_head_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class OpenWindow(WindowReference):
    handoff_receipt: HandoffReceipt | None = None


class CaptureReceipt(WindowReference):
    kind: Literal["authenticated_onebot_ws"]
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    source_event_id: str = Field(min_length=1, max_length=512)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    frame_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    protocol_epoch: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    received_at: AwareDatetime
    occurred_at: AwareDatetime
    signature: str | None = Field(default=None, pattern=r"^[a-f0-9]{128}$")


class InputApproval(WindowReference):
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capture: CaptureReceipt | None = None
    meme_offer: MemeOffer | None = Field(default=None, exclude_if=lambda value: value is None)


class CancelledInputReference(WindowReference):
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")


class SendCheck(WindowReference):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_:-]{8,128}$")
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    reply_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        exclude_if=lambda value: value is None,
    )
    content_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        exclude_if=lambda value: value is None,
    )
    lease_token: str = Field(pattern=r"^lease_[a-f0-9]{32}$")
    reply_revision: int | None = Field(
        default=None, ge=0, strict=True, exclude_if=lambda value: value is None
    )
    voice: dict | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("voice")
    @classmethod
    def voice_binding(cls, value):
        return delivery_wire.validate_voice_check(value) if value is not None else None

    @model_validator(mode="after")
    def one_payload_digest(self):
        if (self.reply_sha256 is None) == (self.content_sha256 is None):
            raise ValueError("exactly one delivery payload digest is required")
        return self


def code_sha256():
    import companion_delivery_protocol
    import companion_meme_protocol
    import companion_meme_sources
    import companion_voice_node

    package = Path(__file__).parent
    shared = (
        companion_meme_protocol,
        companion_delivery_protocol,
        companion_voice_node,
        companion_meme_sources,
    )
    return paid_budget.sha(
        [
            [p.relative_to(package).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()]
            for p in sorted(package.rglob("*.py"))
        ]
        + [
            [
                module.__name__ + "/" + p.relative_to(Path(module.__file__).parent).as_posix(),
                hashlib.sha256(p.read_bytes()).hexdigest(),
            ]
            for module in shared
            for p in sorted(Path(module.__file__).parent.rglob("*.py"))
        ]
    )


def config_sha256(settings):
    value = settings.model_dump(mode="json", exclude={"live_text_window"})
    value["database"] = hashlib.sha256(settings.database.get_secret_value().encode()).hexdigest()
    value["model_credential"] = hashlib.sha256(
        settings.model_key.get_secret_value().encode()
    ).hexdigest()
    if settings.paid_model and settings.paid_model.video_input:
        value["video_credential"] = hashlib.sha256(
            settings.video_model_key.get_secret_value().encode()
        ).hexdigest()
    return hmac.new(
        settings.bridge_token.get_secret_value().encode(), encode(value).encode(), hashlib.sha256
    ).hexdigest()


def active(store, settings):
    return settings.live_text_window is not None or bool(
        store.one("SELECT 1 FROM runtime_meta WHERE key = %s", (BINDING,))
    )


def _meta(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key = %s", (key,))
    return json.loads(row["value"]) if row else None


def _put(store, key, value):
    store.execute(
        "INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO "
        "UPDATE SET value = excluded.value, record_sequence = DEFAULT",
        (key, encode(value)),
    )


def stopped(store, plan_sha256=None):
    binding = _meta(store, BINDING)
    plan_sha256 = plan_sha256 or (binding or {}).get("plan_sha256")
    if plan_sha256 and store.one(
        "SELECT 1 FROM runtime_meta WHERE key = %s", (STOP + plan_sha256,)
    ):
        return True
    legacy = store.one("SELECT value FROM runtime_meta WHERE key = 'live_text_stopped'")
    archived = _meta(store, LEGACY_STOP)
    return bool(
        legacy
        and (
            not archived
            or archived["value"] != legacy["value"]
            or archived["plan_sha256"] == plan_sha256
        )
    )


def record_failure(store, settings, code):
    approval = settings.live_text_window
    binding = _meta(store, BINDING)
    plan = approval.plan_sha256 if approval else (binding or {}).get("plan_sha256")
    if plan:
        store.execute(
            "INSERT INTO runtime_meta VALUES (%s, %s) ON CONFLICT DO NOTHING", (STOP + plan, code)
        )
    else:
        store.execute(
            "INSERT INTO runtime_meta VALUES ('live_text_stopped', %s) ON CONFLICT DO NOTHING",
            (code,),
        )


def guard(
    store,
    settings,
    clock,
    *,
    opening=False,
    current_request=None,
    current_delivery=None,
    preparing=False,
    input_only=False,
):
    # Read the clock after BEGIN IMMEDIATE, including any SQLite lock wait.
    with nullcontext() if store.in_transaction else store.transaction():
        if not opening and not preparing and callable(clock):
            cumulative.preflight(store, settings, clock)
        approval = settings.live_text_window
        if approval is None or approval.live_approved is not True or not callable(clock):
            raise CoreError("LIVE_TEXT_APPROVAL_REQUIRED", 503)
        if (
            settings.mode != "live"
            or approval.core_config_sha256 != config_sha256(settings)
            or approval.core_code_sha256 != code_sha256()
        ):
            raise CoreError("LIVE_TEXT_RUNTIME_CHANGED", 503)
        if settings.segmented_reply != approval.segmented_reply:
            raise CoreError("LIVE_TEXT_REPLY_SCOPE_CHANGED", 503)
        disabled = (
            "diary_enabled",
            "affect_enabled",
            "consolidation_enabled",
            "reflection_enabled",
            "goals_enabled",
            "wake_enabled",
            "exploration_enabled",
            "social_enabled",
            "expression_enabled",
            "meme_discovery_enabled",
            "proactive_enabled",
            "voice_enabled",
            "camera_enabled",
            "android_enabled",
        )
        images = approval.image_scope == "owner_image_v1"
        links = approval.link_scope == "owner_links_v1"
        files = approval.file_scope == "owner_file_v1"
        if settings.poke_policy.enabled != (approval.poke_scope == "owner_poke_v1"):
            raise CoreError("PRIVATE_POKE_SCOPE_REQUIRED", 403)
        if settings.poke_policy.enabled:
            from companion import poke

            poke.policy(settings, approval)
        if settings.voice.enabled != (approval.voice_scope == "owner_voice_v1") or (
            settings.voice.enabled
            and approval.voice_policy_sha256 != settings.voice.authority_sha256()
        ):
            raise CoreError("PRIVATE_VOICE_SCOPE_REQUIRED", 403)
        if settings.social_autonomy.enabled != (
            approval.account_actions_scope == "qq_social_v1"
        ) or (
            settings.social_autonomy.enabled
            and approval.account_actions_authorization_sha256
            != settings.social_autonomy.authorization_sha256
        ):
            raise CoreError("QQ_ACCOUNT_ACTION_SCOPE_REQUIRED", 403)
        if settings.bili_video.enabled != (
            approval.bili_video_scope == "bounded_video_media_v1"
        ) or (
            settings.bili_video.enabled
            and (
                approval.bili_video_policy_sha256
                != paid_budget.sha(settings.bili_video.model_dump(mode="json"))
                or not settings.paid_model
                or not settings.paid_model.image_input
                or not settings.paid_model.video_input
            )
        ):
            raise CoreError("BILI_VIDEO_SCOPE_REQUIRED", 403)
        if images:
            qq_images.policy(settings, approval)
        if files:
            qq_files.policy(settings, approval)
        from companion.sources import supports

        invalid_adapters = any(
            not (
                (images and name == settings.content.image_adapter and item.source == "qq_image")
                or (
                    files and name == settings.private_files.adapter_id and item.source == "qq_file"
                )
                or (links and supports(item.source, "get"))
            )
            for name, item in settings.observation_adapters.items()
        )
        if (
            (
                any(getattr(settings, name) for name in disabled)
                if not private_life.enabled(settings)
                else (
                    any(not getattr(settings, name + "_enabled") for name in private_life.FEATURES)
                    or any(
                        getattr(settings, name)
                        for name in ("voice_enabled", "camera_enabled", "android_enabled")
                    )
                )
            )
            or settings.content_enabled != (images or links or files)
            or settings.private_files.enabled != files
            or settings.memory_enabled != (approval.memory_scope != "none")
            or settings.executive.enabled
            or settings.media.enabled
            or invalid_adapters
            or ((links or files) and not settings.observation_enabled)
            or settings.max_recent_messages != approval.max_history_turns * 2
        ):
            raise CoreError("LIVE_TEXT_CLOSED_FEATURES_REQUIRED", 503)
        now = datetime.fromisoformat(clock())
        if not approval.starts_at <= now < approval.ends_at:
            if approval.cumulative:
                cumulative.pause(store, settings.paid_model, now, "EXPIRED")
            raise CoreError("LIVE_TEXT_APPROVAL_EXPIRED", 503)
        paid = paid_budget.contract(settings, clock())
        from companion.video_input import call_limit_matches

        if (
            paid.window_id != approval.window_id
            or paid.concurrency != 1
            or not call_limit_matches(
                settings,
                3 + int(paid.video_input is not None)
                if private_life.enabled(settings)
                else 1 + int(approval.memory_scope == "approved_facts") + int(images),
            )
            or paid.window_calls > approval.max_model_attempts
            or paid.starts_at > approval.starts_at
            or (paid.expires_at is not None and paid.expires_at < approval.ends_at)
            or paid_budget.sha(paid_budget.snapshot(settings)) != approval.paid_contract_sha256
            or paid.cumulative != approval.cumulative
        ):
            raise CoreError("LIVE_TEXT_PAID_CONTRACT_CHANGED", 503)
        if approval.memory_scope == "approved_facts":
            memory_sources(store, settings, clock, approval)
        if approval.memory_scope != "none" or images or links or files:
            ledger_sha256 = _ledger_custody(store, settings)
        journal = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
        if not journal or journal["value"] != approval.budget_journal_id:
            raise CoreError("LIVE_TEXT_BUDGET_JOURNAL_CHANGED", 503)
        custody = paid_budget.journal(store, paid, allow_settlement=preparing)
        if not opening and not preparing:
            cumulative.check(store, settings, clock(), custody=custody)
        if store.one(
            "SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked', "
            "'model_budget_restore_required')"
        ) or (not preparing and stopped(store, approval.plan_sha256)):
            raise CoreError("LIVE_TEXT_RECONCILIATION_REQUIRED", 503)
        if store.one(
            "SELECT 1 FROM model_usage AS m LEFT JOIN model_admissions AS a USING (request_id) "
            "WHERE m.state IN ('UNKNOWN', 'RESERVED') AND m.request_id <> %s AND NOT (%s AND "
            "m.state = 'RESERVED' AND a.lease_until > %s) LIMIT 1",
            (current_request or "", bool(private_life.enabled(settings)), clock()),
        ) or store.one(
            "SELECT 1 FROM outbox WHERE state = 'DELIVERY_UNCERTAIN' OR (state = 'SENDING' AND "
            "message_id <> %s AND NOT %s) LIMIT 1",
        (current_delivery or "", bool(input_only and private_life.enabled(settings))),
        ):
            raise CoreError("LIVE_TEXT_RECONCILIATION_REQUIRED", 503)
        binding = store.one("SELECT value FROM runtime_meta WHERE key = %s", (BINDING,))
        expected = {
            "approval_sha256": paid_budget.sha(approval.model_dump(mode="json")),
            "privacy_epoch": epoch(store),
            "plan_sha256": approval.plan_sha256,
            "window_id": approval.window_id,
            "budget_journal_id": approval.budget_journal_id,
            "budget_journal_path": str(Path(paid.journal_path).resolve()),
        }
        if approval.memory_scope != "none" or images or links:
            expected["erasure_ledger_sha256"] = ledger_sha256
        if approval.handoff:
            expected.update(
                identity_sha256=approval.handoff.identity_sha256,
                protocol_epoch=approval.handoff.protocol_epoch,
                handoff_sha256=paid_budget.sha(approval.handoff.model_dump(mode="json")),
            )
        if binding and private_life.enabled(settings) and not preparing:
            from companion.input_protocol import recall_ledger_extension

            saved = json.loads(binding["value"])
            stable = set(expected) - {"privacy_epoch", "erasure_ledger_sha256"}
            if all(saved.get(key) == expected[key] for key in stable) and (
                saved.get("privacy_epoch", -1) < expected["privacy_epoch"]
                or (
                    saved.get("privacy_epoch") == expected["privacy_epoch"]
                    and recall_ledger_extension(
                        store,
                        settings,
                        saved.get("erasure_ledger_sha256"),
                        expected.get("erasure_ledger_sha256"),
                    )
                )
            ):
                _put(store, BINDING, expected)
                history = _meta(store, HISTORY + approval.plan_sha256)
                if history:
                    history["binding"] = expected
                    _put(store, HISTORY + approval.plan_sha256, history)
                binding = {"value": encode(expected)}
        lifecycle = custody["lifecycle"]
        prepared = bool(
            lifecycle
            and lifecycle["state"] == "PREPARED"
            and opening
            and lifecycle["plan_sha256"] == approval.plan_sha256
            and approval.handoff
        )
        if prepared:
            predecessor = _meta(store, HISTORY + approval.handoff.predecessor_plan_sha256)
            if (
                not predecessor
                or predecessor["state"] != "RETIRED"
                or not binding
                or predecessor["binding"] != json.loads(binding["value"])
                or predecessor["successor_plan_sha256"] != approval.plan_sha256
            ):
                raise CoreError("LIVE_TEXT_HANDOFF_PREDECESSOR_CHANGED", 409)
        if (
            not preparing
            and lifecycle
            and (
                lifecycle["plan_sha256"] != approval.plan_sha256
                or (lifecycle["state"] != "OPEN" and not prepared)
            )
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_NOT_OPEN", 409)
        if not preparing and (
            (binding and json.loads(binding["value"]) != expected and not prepared)
            or (not binding and (not opening or lifecycle))
        ):
            raise CoreError("LIVE_TEXT_BINDING_OR_PRIVACY_CHANGED", 503)
        if not preparing and lifecycle:
            history = _meta(store, HISTORY + approval.plan_sha256)
            if not history or history["state"] != lifecycle["state"]:
                raise CoreError("LIVE_TEXT_LIFECYCLE_MISMATCH", 503)
        attempts = store.one(
            "SELECT COUNT(*) AS n FROM model_admissions WHERE window_id = %s", (paid.window_id,)
        )["n"]
        if attempts > approval.max_model_attempts:
            raise CoreError("LIVE_TEXT_MODEL_ATTEMPT_CAP", 429)
        return approval, expected, attempts


def status(store, settings, clock):
    if not active(store, settings):
        return {"approved": False}
    try:
        approval, binding, attempts = guard(store, settings, clock, input_only=True)
        return {
            "approved": True,
            "plan_sha256": approval.plan_sha256,
            "scope_sha256": paid_budget.sha(
                approval.model_dump(mode="json", exclude={"plan_sha256"})
            ),
            "privacy_epoch": binding["privacy_epoch"],
            "model_attempts": attempts,
        }
    except CoreError as error:
        binding = store.one("SELECT value FROM runtime_meta WHERE key = %s", (BINDING,))
        window_id = json.loads(binding["value"])["window_id"] if binding else ""
        attempts = store.one(
            "SELECT COUNT(*) AS n FROM model_admissions WHERE window_id = %s", (window_id,)
        )["n"]
        return {"approved": False, "code": error.code, "model_attempts": attempts}


def open_window(store, settings, clock, reference):
    with store.transaction():
        approval, binding, attempts = guard(store, settings, clock, opening=True)
        if reference.window_sha256 != approval.plan_sha256:
            raise CoreError("LIVE_TEXT_WINDOW_MISMATCH", 403)
        history = _meta(store, HISTORY + approval.plan_sha256)
        receipt = getattr(reference, "handoff_receipt", None)
        if approval.handoff:
            if not history or not receipt:
                raise CoreError("LIVE_TEXT_HANDOFF_RECEIPT_REQUIRED", 403)
            predecessor = _meta(store, HISTORY + approval.handoff.predecessor_plan_sha256)
            _handoff_custody(store, settings, approval, predecessor["binding"])
            _verify_handoff_receipt(approval, history, receipt)
            if history["state"] == "OPEN":
                if history["bridge_receipt_sha256"] != paid_budget.sha(
                    receipt.model_dump(mode="json")
                ):
                    raise CoreError("LIVE_TEXT_HANDOFF_RECEIPT_CHANGED", 409)
                return status(store, settings, clock)
        elif receipt:
            raise CoreError("LIVE_TEXT_UNEXPECTED_HANDOFF_RECEIPT", 403)
        if not history or history["state"] != "OPEN":
            _idle(store)
            if attempts:
                raise CoreError("LIVE_TEXT_INITIAL_WORK_MUST_BE_ZERO", 409)
            if approval.cumulative or store.one(
                "SELECT 1 FROM runtime_meta WHERE key = %s",
                (cumulative.PROJECTION,),
            ):
                cumulative.activate(store, settings, clock())
                cumulative.check(store, settings, clock())
            paid_budget.journal(
                store,
                settings.paid_model,
                {
                    "kind": "window_open",
                    "window_id": approval.window_id,
                    "plan_sha256": approval.plan_sha256,
                    "handoff_sha256": binding.get("handoff_sha256"),
                },
            )
            _put(store, BINDING, binding)
            history = {
                **(history or {}),
                "state": "OPEN",
                "binding": binding,
                "approval": approval.model_dump(mode="json"),
            }
            if receipt:
                history["bridge_receipt_sha256"] = paid_budget.sha(receipt.model_dump(mode="json"))
            _put(store, HISTORY + approval.plan_sha256, history)
    return status(store, settings, clock)


def _idle(store):
    if store.one("SELECT 1 FROM model_usage WHERE state <> 'SETTLED' LIMIT 1") or any(
        store.one(f"SELECT 1 FROM {table} WHERE state NOT IN ({states}) LIMIT 1")
        for table, states in (
            ("jobs", "'SUCCEEDED','FAILED','CANCELLED'"),
            ("outbox", "'SENT','FAILED','CANCELLED'"),
            ("tool_tasks", "'SUCCEEDED','FAILED','CANCELLED'"),
        )
    ):
        raise CoreError("LIVE_TEXT_INITIAL_WORK_MUST_BE_ZERO", 409)


def _ledger_custody(store, settings, expected_sha256=None):
    """Compare latest external erasures with applied DB state; never replay or repair here."""
    from companion.postgres import ERASURE_LEDGER

    if not store.in_transaction:
        return store.read_or_write(lambda: _ledger_custody(store, settings, expected_sha256))
    store.ledger_lock(ERASURE_LEDGER, shared=True)
    ledger = ErasureLedger(settings.erasure_ledger)
    try:
        raw = ledger.path.read_bytes()
    except OSError as error:
        raise CoreError("ERASURE_LEDGER_INVALID", 503) from error
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise CoreError("LIVE_TEXT_HANDOFF_LEDGER_CHANGED", 409)
    records = ledger.read()
    ledger_id = store.one("SELECT value FROM runtime_meta WHERE key = 'ledger_id'")
    if not ledger_id or ledger_id["value"] != records[0]["ledger_id"]:
        raise CoreError("ERASURE_LEDGER_MISMATCH", 503)
    applied = {
        row["erasure_id"]: row
        for row in store.rows(
            "SELECT erasure_id, state, restore_min_replay_version FROM privacy_erasure_ledger"
        )
    }
    if len(applied) != len(records) - 1 or any(
        not (row := applied.get(record["erasure_id"]))
        or row["state"] != "COMPLETE"
        or row["restore_min_replay_version"] != record["seq"]
        for record in records[1:]
    ):
        raise CoreError("ERASURE_REPLAY_REQUIRED", 503)
    if ledger.path.read_bytes() != raw:
        raise CoreError("ERASURE_LEDGER_CHANGED", 503)
    return digest


def _handoff_custody(store, settings, approval, old):
    scope = approval.handoff
    owner = store.one("SELECT value FROM runtime_meta WHERE key = 'owner_qq_id'")
    if (
        not owner
        or owner["value"] != settings.owner_qq_id
        or scope.identity_sha256 != paid_budget.sha([settings.owner_qq_id, settings.bot_qq_id])
        or old.get("identity_sha256", scope.identity_sha256) != scope.identity_sha256
        or old.get("protocol_epoch", scope.protocol_epoch) != scope.protocol_epoch
        or scope.privacy_epoch != epoch(store)
        or old["privacy_epoch"] > scope.privacy_epoch
        or old["budget_journal_id"] != approval.budget_journal_id
        or old["budget_journal_path"] != str(Path(settings.paid_model.journal_path).resolve())
    ):
        raise CoreError("LIVE_TEXT_HANDOFF_CUSTODY_CHANGED", 409)
    _ledger_custody(store, settings, scope.erasure_ledger_sha256)
    for row in store.rows("SELECT value FROM runtime_meta WHERE key LIKE %s", (INPUT + "%",)):
        value = json.loads(row["value"])
        if value.get("window_sha256") != old["plan_sha256"]:
            continue
        capture = value.get("capture")
        present = store.one("SELECT 1 FROM events WHERE event_id = %s", (value["event_id"],))
        recalled = False
        if not present:
            previous = _meta(store, HISTORY + old["plan_sha256"])
            try:
                original = type(approval).model_validate(previous["approval"])
            except (KeyError, ValueError, TypeError):
                original = None
            recalled = bool(
                original
                and original.plan_sha256 == old["plan_sha256"]
                and original.window_id == old["window_id"]
                and input_recall.verified_before_ingest(
                    store,
                    settings,
                    original,
                    value,
                )
            )
        if (
            (not present and not recalled)
            or capture
            and (
                capture["identity_sha256"] != scope.identity_sha256
                or capture["protocol_epoch"] != scope.protocol_epoch
            )
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_INPUT_UNRESOLVED", 409)


def prepare_handoff(store, settings, clock, reference):
    with store.transaction():
        history = _meta(store, HISTORY + reference.window_sha256)
        if history and history["state"] == "ABORTED":
            raise CoreError("LIVE_TEXT_HANDOFF_ALREADY_USED", 409)
        approval, binding, _ = guard(store, settings, clock, preparing=True)
        scope = approval.handoff
        if not scope or reference.window_sha256 != approval.plan_sha256:
            raise CoreError("LIVE_TEXT_APPROVED_HANDOFF_REQUIRED", 403)
        old = _meta(store, BINDING)
        if not old or (old["window_id"], old["plan_sha256"]) != (
            scope.predecessor_window_id,
            scope.predecessor_plan_sha256,
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_PREDECESSOR_CHANGED", 409)
        _idle(store)
        _handoff_custody(store, settings, approval, old)
        existing = _meta(store, HISTORY + approval.plan_sha256)
        custody = paid_budget.journal(store, settings.paid_model, allow_settlement=True)
        if existing:
            if (
                existing["state"] != "PREPARED"
                or existing["binding"] != binding
                or existing["approval"] != approval.model_dump(mode="json")
                or custody["lifecycle"]["state"] != "PREPARED"
            ):
                raise CoreError("LIVE_TEXT_HANDOFF_ALREADY_USED", 409)
            return existing["prepared_receipt"]
        if store.one(
            "SELECT 1 FROM model_budget_windows WHERE window_id = %s", (approval.window_id,)
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_ALREADY_USED", 409)
        lifecycle = custody["lifecycle"]
        if (
            not lifecycle
            or lifecycle["state"] != "STOPPED"
            or lifecycle["plan_sha256"] != old["plan_sha256"]
            or custody["head_sha256"] != scope.budget_journal_head_sha256
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_STOP_REQUIRED", 409)
        if approval.cumulative or store.one(
            "SELECT 1 FROM runtime_meta WHERE key = %s",
            (cumulative.PROJECTION,),
        ):
            cumulative.activate(store, settings, clock())
            cumulative.check(store, settings, clock())
        head = paid_budget.journal(
            store,
            settings.paid_model,
            {
                "kind": "window_handoff",
                "window_id": approval.window_id,
                "plan_sha256": approval.plan_sha256,
                "handoff_sha256": binding["handoff_sha256"],
                "predecessor_window_id": old["window_id"],
                "predecessor_plan_sha256": old["plan_sha256"],
            },
            allow_settlement=True,
        )["head_sha256"]
        receipt = {
            "state": "PREPARED",
            "window_sha256": approval.plan_sha256,
            "predecessor_window_sha256": old["plan_sha256"],
            "handoff_sha256": binding["handoff_sha256"],
            "budget_journal_head_sha256": head,
            "privacy_epoch": scope.privacy_epoch,
            "erasure_ledger_sha256": scope.erasure_ledger_sha256,
        }
        legacy = store.one("SELECT value FROM runtime_meta WHERE key = 'live_text_stopped'")
        if legacy and not _meta(store, LEGACY_STOP):
            _put(store, LEGACY_STOP, {"plan_sha256": old["plan_sha256"], **legacy})
        previous = _meta(store, HISTORY + old["plan_sha256"]) or {"binding": old}
        _put(
            store,
            HISTORY + old["plan_sha256"],
            {**previous, "state": "RETIRED", "successor_plan_sha256": approval.plan_sha256},
        )
        _put(
            store,
            HISTORY + approval.plan_sha256,
            {
                "state": "PREPARED",
                "binding": binding,
                "approval": approval.model_dump(mode="json"),
                "prepared_receipt": receipt,
            },
        )
        return receipt


def abort_handoff(store, settings, clock, reference):
    """Revoke one unprepared successor; never open a window or repair a ledger.

    Like STOP, revocation remains possible after code drift or expiry. A repeated
    native window_stop advances the independent head, permanently fencing the
    rejected successor even if a stale database loses its ABORTED projection.
    """
    with store.transaction():
        approval = settings.live_text_window
        scope = approval.handoff if approval else None
        old = _meta(store, BINDING)
        previous = _meta(store, HISTORY + scope.predecessor_plan_sha256) if scope else None
        if (
            not scope
            or reference.window_sha256 != approval.plan_sha256
            or not old
            or (old["window_id"], old["plan_sha256"])
            != (scope.predecessor_window_id, scope.predecessor_plan_sha256)
            or not previous
            or previous["state"] != "STOPPED"
            or previous["binding"] != old
            or not stopped(store, old["plan_sha256"])
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_ABORT_UNPROVEN", 409)
        _idle(store)
        _handoff_custody(store, settings, approval, old)
        if (
            store.one(
                "SELECT 1 FROM model_budget_windows WHERE window_id = %s", (approval.window_id,)
            )
            or store.one(
                "SELECT 1 FROM model_admissions WHERE window_id = %s",
                (approval.window_id,),
            )
            or store.one(
                (
                    (
                        'SELECT 1 FROM runtime_meta WHERE key LIKE %s AND (CASE '
                        'WHEN value IS JSON THEN '
                        "(value::json#>>'{window_sha256}') END)=%s"
                    )
                ),
                (INPUT + "%", approval.plan_sha256),
            )
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_ABORT_UNPROVEN", 409)
        existing = _meta(store, HISTORY + approval.plan_sha256)
        custody = paid_budget.journal(
            store,
            SimpleNamespace(
                journal_path=old["budget_journal_path"],
            ),
            allow_settlement=True,
            initialize=False,
        )
        lifecycle = custody["lifecycle"]
        if (
            not lifecycle
            or lifecycle["state"] != "STOPPED"
            or lifecycle["plan_sha256"] != old["plan_sha256"]
            or lifecycle["window_id"] != old["window_id"]
        ):
            raise CoreError("LIVE_TEXT_HANDOFF_ABORT_UNPROVEN", 409)
        if existing:
            if (
                existing.get("state") != "ABORTED"
                or existing.get("approval") != approval.model_dump(mode="json")
                or existing.get("abort_request") != reference.model_dump()
                or existing["aborted_receipt"]["budget_journal_head_sha256"]
                != custody["head_sha256"]
            ):
                raise CoreError("LIVE_TEXT_HANDOFF_ABORT_CONFLICT", 409)
            return existing["aborted_receipt"]
        if custody["head_sha256"] != scope.budget_journal_head_sha256:
            raise CoreError("LIVE_TEXT_HANDOFF_ABORT_UNPROVEN", 409)
        fenced = paid_budget.journal(
            store,
            SimpleNamespace(
                journal_path=old["budget_journal_path"],
            ),
            {
                "kind": "window_stop",
                "window_id": old["window_id"],
                "plan_sha256": old["plan_sha256"],
            },
            allow_settlement=True,
            initialize=False,
        )
        receipt = {
            "state": "ABORTED_BEFORE_PREPARE",
            "window_sha256": approval.plan_sha256,
            "predecessor_window_sha256": old["plan_sha256"],
            "handoff_sha256": paid_budget.sha(scope.model_dump(mode="json")),
            "predecessor_budget_journal_head_sha256": scope.budget_journal_head_sha256,
            "budget_journal_head_sha256": fenced["head_sha256"],
            "privacy_epoch": scope.privacy_epoch,
            "erasure_ledger_sha256": scope.erasure_ledger_sha256,
            "bridge_journal_head_sha256": reference.bridge_journal_head_sha256,
        }
        _put(
            store,
            HISTORY + approval.plan_sha256,
            {
                "state": "ABORTED",
                "approval": approval.model_dump(mode="json"),
                "abort_request": reference.model_dump(),
                "aborted_receipt": receipt,
            },
        )
        store.audit(
            "live_text.handoff_aborted",
            approval.window_id,
            "handoff",
            clock(),
            receipt_sha256=paid_budget.sha(receipt),
        )
        return receipt


def _cancelled_model_usage(store, settings, approval, job, now):
    usage = store.rows(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.job_id = %s ORDER BY record_sequence NULLS "
        "FIRST",
        (job["job_id"],),
    )
    admissions = store.rows(
        "SELECT model_admissions.request_id AS request_id, model_admissions.logical_request_id AS "
        "logical_request_id, model_admissions.attempt_id AS attempt_id, "
        "model_admissions.attempt_number AS attempt_number, model_admissions.window_id AS "
        "window_id, model_admissions.contract_sha256 AS contract_sha256, "
        "model_admissions.payload_sha256 AS payload_sha256, model_admissions.snapshot_json AS "
        "snapshot_json, model_admissions.reserved_micro_usd AS reserved_micro_usd, "
        "model_admissions.input_bound AS input_bound, model_admissions.output_bound AS "
        "output_bound, model_admissions.lease_token AS lease_token, model_admissions.lease_until "
        "AS lease_until, model_admissions.io_started_at AS io_started_at, "
        "model_admissions.journal_path AS journal_path, model_admissions.journal_id AS journal_id, "
        "model_admissions.measurement_json AS measurement_json, model_admissions.authority_json AS "
        "authority_json FROM model_admissions AS model_admissions WHERE "
        "model_admissions.logical_request_id = %s OR model_admissions.request_id IN (SELECT "
        "model_usage.request_id AS request_id FROM model_usage AS model_usage WHERE "
        "model_usage.job_id = %s) ORDER BY record_sequence NULLS FIRST",
        (job["job_id"], job["job_id"]),
    )
    if not usage:
        if admissions or store.one(
            "SELECT 1 FROM content_items WHERE owner_event_id = %s AND (NOT usage_request_id IS "
            "NULL OR NOT output_event_id IS NULL OR media_sha256 <> '' OR lease_until > "
            "%s)",
            (job["event_id"], now),
        ):
            raise CoreError("LIVE_TEXT_CANCELLED_INPUT_ATTEMPTED", 409)
        return {"model_attempts": 0}
    if (
        {row["request_id"] for row in usage} != {row["request_id"] for row in admissions}
        or any(row["state"] != "SETTLED" for row in usage)
        or any(
            row["logical_request_id"] != job["job_id"] or row["window_id"] != approval.window_id
            for row in admissions
        )
        or store.one(
            "SELECT 1 FROM content_items WHERE owner_event_id = %s AND (NOT state IN ('SUCCEEDED', "
            "'FAILED', 'UNKNOWN') OR lease_until > %s)",
            (job["event_id"], now),
        )
    ):
        raise CoreError("LIVE_TEXT_CANCELLED_INPUT_ATTEMPTED", 409)
    custody = paid_budget.journal(
        store,
        settings.paid_model,
        initialize=False,
        allow_settlement=True,
    )
    if (
        not custody["lifecycle"]
        or custody["lifecycle"]["state"] != "STOPPED"
        or custody["lifecycle"]["plan_sha256"] != approval.plan_sha256
    ):
        raise CoreError("LIVE_TEXT_CANCELLED_INPUT_UNPROVEN", 409)
    return {
        "model_attempts": len(usage),
        "settled_model_usage_sha256": paid_budget.sha([usage, admissions]),
        "billed_micro_usd": sum(row["billed_micro_usd"] for row in usage),
    }


def _failed_memory_input(store, approval, event, job, now):
    """Recognize the historical failed recall without changing its terminal record."""
    usage = store.rows(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.job_id = %s",
        (job["job_id"],),
    )
    evidence = store.rows(
        "SELECT audit_log.audit_id AS audit_id, audit_log.action AS action, audit_log.actor_id AS "
        "actor_id, audit_log.entity_type AS entity_type, audit_log.entity_id AS entity_id, "
        "audit_log.trace_id AS trace_id, audit_log.safe_detail_json AS safe_detail_json, "
        "audit_log.created_at AS created_at FROM audit_log AS audit_log WHERE audit_log.action = "
        "'memory.recall_invalid' AND audit_log.entity_id = %s",
        (job["job_id"],),
    )
    conversation = store.one(
        "SELECT conversations.conversation_id AS conversation_id, "
        "conversations.platform AS platform, conversations.platform_session_key AS "
        "platform_session_key, conversations.scope AS scope, conversations.owner_actor_id AS "
        "owner_actor_id, conversations.last_event_at AS last_event_at, conversations.created_at AS "
        "created_at, conversations.reply_revision AS reply_revision FROM conversations AS "
        "conversations WHERE conversations.conversation_id = %s",
        (event["conversation_id"],),
    )
    body = json.loads(event["body_json"])
    if (
        not approval
        or approval.companion_scope != "owner_private_v1"
        or approval.automatic_reply is not True
        or job["state"] != "FAILED"
        or job["last_error_code"] != "MEMORY_REVIEW_INVALID"
        or job["lease_token"] is not None
        or event["event_type"] not in {"message.received", "message.media_received"}
        or body.get("control_command")
        or body.get("diary_command")
        or not conversation
        or conversation["scope"] != "private"
        or conversation["owner_actor_id"] != "owner"
        or event["reply_revision"] != conversation["reply_revision"]
        or len(usage) != 1
        or usage[0]["scenario"] != "memory_review"
        or usage[0]["state"] != "SETTLED"
        or len(evidence) != 1
        or evidence[0]["actor_id"] != "system"
        or evidence[0]["trace_id"] != event["trace_id"]
        or store.one(
            "SELECT 1 FROM runtime_meta WHERE key = %s", ("erased_event:" + event["event_id"],)
        )
        or store.one("SELECT 1 FROM content_items WHERE owner_event_id = %s", (event["event_id"],))
    ):
        raise CoreError("LIVE_TEXT_FAILED_INPUT_UNPROVEN", 409)
    try:
        times = [
            datetime.fromisoformat(value)
            for value in (
                usage[0]["occurred_at"],
                evidence[0]["created_at"],
                job["updated_at"],
                now,
            )
        ]
        lease = datetime.fromisoformat(job["lease_until"]) if job["lease_until"] else None
        if (
            any(value.tzinfo is None for value in times)
            or not times[0] <= times[1] <= times[2] <= times[3]
            or lease
            and (lease.tzinfo is None or lease > times[-1])
        ):
            raise ValueError("FAILED_RECALL_TIMELINE_UNPROVEN")
    except (ValueError, TypeError):
        raise CoreError("LIVE_TEXT_FAILED_INPUT_UNPROVEN", 409) from None
    return {
        "state": "FAILED",
        "resolution": "SETTLED_NO_DELIVERY",
        "failure_code": "MEMORY_REVIEW_INVALID",
        "failure_phase": "memory_recall",
        "failure_evidence_sha256": paid_budget.sha(evidence[0]),
        "reply_revision": event["reply_revision"],
    }


def _rejected_budget_input(store, settings, approval, event, job, now):
    """Prove an unadmitted monthly-budget refusal without rewriting the failed job."""
    evidence = store.rows(
        "SELECT audit_log.audit_id AS audit_id, audit_log.action AS action, "
        "audit_log.actor_id AS actor_id, audit_log.entity_type AS entity_type, audit_log.entity_id "
        "AS entity_id, audit_log.trace_id AS trace_id, audit_log.safe_detail_json AS "
        "safe_detail_json, audit_log.created_at AS created_at FROM audit_log AS audit_log WHERE "
        "audit_log.action = 'job.failed' AND audit_log.entity_id = %s",
        (job["job_id"],),
    )
    conversation = store.one(
        "SELECT conversations.conversation_id AS conversation_id, "
        "conversations.platform AS platform, conversations.platform_session_key AS "
        "platform_session_key, conversations.scope AS scope, conversations.owner_actor_id AS "
        "owner_actor_id, conversations.last_event_at AS last_event_at, conversations.created_at AS "
        "created_at, conversations.reply_revision AS reply_revision FROM conversations AS "
        "conversations WHERE conversations.conversation_id = %s",
        (event["conversation_id"],),
    )
    body = json.loads(event["body_json"])
    if (
        not approval
        or approval.companion_scope != "owner_private_v1"
        or approval.automatic_reply is not True
        or job["state"] != "FAILED"
        or job["last_error_code"] != "MONTHLY_BUDGET_EXHAUSTED"
        or job["lease_token"] is not None
        or job["attempts"] != 1
        or event["event_type"] != "message.received"
        or body.get("control_command")
        or body.get("diary_command")
        or body.get("media")
        or body.get("shared_content")
        or body.get("rich")
        or not conversation
        or conversation["scope"] != "private"
        or conversation["owner_actor_id"] != "owner"
        or event["reply_revision"] != conversation["reply_revision"]
        or len(evidence) != 1
        or evidence[0]["actor_id"] != "system"
        or evidence[0]["trace_id"] != event["trace_id"]
        or json.loads(evidence[0]["safe_detail_json"])
        != {"code": "MONTHLY_BUDGET_EXHAUSTED", "state": "FAILED"}
        or store.one("SELECT 1 FROM model_usage WHERE job_id = %s", (job["job_id"],))
        or store.one(
            "SELECT 1 FROM model_admissions WHERE logical_request_id = %s", (job["job_id"],)
        )
        or store.one("SELECT 1 FROM content_items WHERE owner_event_id = %s", (event["event_id"],))
        or store.one(
            "SELECT 1 FROM runtime_meta WHERE key = %s", ("erased_event:" + event["event_id"],)
        )
    ):
        raise CoreError("LIVE_TEXT_FAILED_INPUT_UNPROVEN", 409)
    try:
        times = [
            datetime.fromisoformat(value)
            for value in (
                job["created_at"],
                evidence[0]["created_at"],
                job["updated_at"],
                now,
            )
        ]
        lease = datetime.fromisoformat(job["lease_until"]) if job["lease_until"] else None
        if (
            any(value.tzinfo is None for value in times)
            or not times[0] <= times[1] == times[2] <= times[3]
            or lease
            and (lease.tzinfo is None or lease > times[-1])
        ):
            raise ValueError("REJECTED_INPUT_TIMELINE_UNPROVEN")
    except (ValueError, TypeError):
        raise CoreError("LIVE_TEXT_FAILED_INPUT_UNPROVEN", 409) from None
    custody = paid_budget.journal(
        store,
        settings.paid_model,
        initialize=False,
        allow_settlement=True,
    )
    if (
        not custody["lifecycle"]
        or custody["lifecycle"]["state"] != "STOPPED"
        or custody["lifecycle"]["plan_sha256"] != approval.plan_sha256
    ):
        raise CoreError("LIVE_TEXT_FAILED_INPUT_UNPROVEN", 409)
    return {
        "state": "FAILED",
        "resolution": "REJECTED_BEFORE_DISPATCH",
        "failure_code": "MONTHLY_BUDGET_EXHAUSTED",
        "failure_phase": "model_admission",
        "failure_evidence_sha256": paid_budget.sha(evidence[0]),
        "reply_revision": event["reply_revision"],
    }


def cancelled_input(store, settings, clock, reference):
    """Prove stopped generation never sent; preserve every settled model attempt and fee."""
    with store.transaction():
        approval, binding = settings.live_text_window, _meta(store, BINDING)
        allowed = {approval.plan_sha256} if approval else set()
        if approval and approval.handoff:
            allowed.add(approval.handoff.predecessor_plan_sha256)
        receipt = _meta(store, INPUT + reference.event_id)
        event = store.one(
            "SELECT events.event_id AS event_id, events.schema_version AS "
            "schema_version, events.source AS source, events.source_event_id AS source_event_id, "
            "events.conversation_id AS conversation_id, events.actor_id AS actor_id, "
            "events.event_type AS event_type, events.occurred_at AS occurred_at, "
            "events.ingested_at AS ingested_at, events.privacy_scope AS privacy_scope, "
            "events.body_json AS body_json, events.trace_id AS trace_id, events.idempotency_key AS "
            "idempotency_key, events.fingerprint AS fingerprint, events.redacted AS redacted, "
            "events.memory_processed AS memory_processed, events.reply_revision AS reply_revision "
            "FROM events AS events WHERE events.event_id = %s",
            (reference.event_id,),
        )
        job = store.one(
            "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS "
            "kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS "
            "attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, "
            "jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, "
            "jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, "
            "jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked "
            "AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE "
            "jobs.event_id = %s AND jobs.kind = 'chat'",
            (reference.event_id,),
        )
        failed = bool(job and job["state"] == "FAILED")
        input_epoch = receipt.get("privacy_epoch") if receipt else None
        # Upgrading COMPLETE erasure projections can advance the epoch for an
        # unrelated source. Only this read-only failed-input proof may cross it;
        # source digest, erasure tombstone and all delivery/admission fences remain.
        epoch_matches = input_epoch == epoch(store) or (
            failed and type(input_epoch) is int and 0 <= input_epoch < epoch(store)
        )
        if (
            reference.window_sha256 not in allowed
            or not binding
            or binding["plan_sha256"] != reference.window_sha256
            or not stopped(store, reference.window_sha256)
            or not receipt
            or not event
            or not job
            or receipt.get("window_sha256") != reference.window_sha256
            or not epoch_matches
            or not receipt.get("capture")
            or receipt["capture"].get("source_event_id") != event["source_event_id"]
            or event["source"] != "qq.onebot"
            or event["actor_id"] != "owner"
            or event["privacy_scope"] != "private"
            or event["redacted"]
            or _input_digest(json.loads(event["body_json"])) != receipt.get("input_sha256")
        ):
            raise CoreError("LIVE_TEXT_CANCELLED_INPUT_UNPROVEN", 409)
        if failed:
            failed = (
                _rejected_budget_input(store, settings, approval, event, job, clock())
                if job["last_error_code"] == "MONTHLY_BUDGET_EXHAUSTED"
                else _failed_memory_input(store, approval, event, job, clock())
            )
        else:
            failed = None
        if failed:
            failed["input_privacy_epoch"] = input_epoch
        if not failed and (
            job["state"] != "CANCELLED" or job["last_error_code"] != "LIVE_WINDOW_STOPPED"
        ):
            raise CoreError("LIVE_TEXT_CANCELLED_INPUT_UNPROVEN", 409)
        if store.one("SELECT 1 FROM outbox WHERE origin_event_id = %s", (reference.event_id,)):
            raise CoreError("LIVE_TEXT_CANCELLED_INPUT_ATTEMPTED", 409)
        usage = _cancelled_model_usage(store, settings, approval, job, clock())
        _idle(store)
        return {
            "window_sha256": reference.window_sha256,
            "event_id": reference.event_id,
            "input_sha256": receipt["input_sha256"],
            **(failed or {"state": "CANCELLED"}),
            "event_sha256": paid_budget.sha(event),
            "job_sha256": paid_budget.sha(job),
            "privacy_epoch": epoch(store),
            **usage,
            "send_attempts": 0,
        }


def _verify_handoff_receipt(approval, history, receipt):
    value = receipt.model_dump(mode="json", exclude={"signature"})
    if (
        value["window_sha256"] != approval.plan_sha256
        or value["predecessor_window_sha256"] != approval.handoff.predecessor_plan_sha256
        or value["handoff_sha256"] != paid_budget.sha(approval.handoff.model_dump(mode="json"))
        or value["core_receipt_sha256"] != paid_budget.sha(history["prepared_receipt"])
    ):
        raise CoreError("LIVE_TEXT_HANDOFF_RECEIPT_CHANGED", 403)
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(approval.capture_public_key)).verify(
            bytes.fromhex(receipt.signature), HANDOFF_SIGNATURE_DOMAIN + encode(value).encode()
        )
    except (ValueError, InvalidSignature):
        raise CoreError("LIVE_TEXT_HANDOFF_SIGNATURE_REQUIRED", 403) from None


def approve_input(store, settings, clock, body):
    with store.transaction():
        approval, binding, attempts = guard(store, settings, clock, input_only=True)
        if body.window_sha256 != approval.plan_sha256:
            raise CoreError("LIVE_TEXT_WINDOW_MISMATCH", 403)
        if body.meme_offer is not None and (
            not private_life.enabled(settings)
            or not settings.expression_enabled
            or body.meme_offer.offer_key != "meme:" + body.event_id
        ):
            raise CoreError("MEME_OFFER_SOURCE_CHANGED", 403)
        capture = body.capture
        if approval.execution == "approved_live" and capture is None:
            raise CoreError("LIVE_TEXT_AUTHENTICATED_CAPTURE_REQUIRED", 403)
        if capture is not None:
            if approval.execution == "approved_live":
                try:
                    signed = capture.model_dump(mode="json", exclude={"signature"})
                    for key in ("received_at", "occurred_at"):
                        signed[key] = getattr(capture, key).isoformat()
                    Ed25519PublicKey.from_public_bytes(
                        bytes.fromhex(approval.capture_public_key or "")
                    ).verify(
                        bytes.fromhex(capture.signature or ""),
                        CAPTURE_SIGNATURE_DOMAIN + encode(signed).encode(),
                    )
                except (ValueError, InvalidSignature):
                    raise CoreError("LIVE_TEXT_CAPTURE_SIGNATURE_REQUIRED", 403) from None
            source_prefix = (
                f"{settings.bot_qq_id}:onebot-direct:FriendMessage:{settings.owner_qq_id}:"
                f"epoch:{capture.protocol_epoch}:"
            )
            source_id = capture.source_event_id.removeprefix(source_prefix)
            if (
                capture.window_sha256 != body.window_sha256
                or capture.event_id != body.event_id
                or capture.input_sha256 != body.input_sha256
                or capture.identity_sha256
                != paid_budget.sha([settings.owner_qq_id, settings.bot_qq_id])
                or not capture.source_event_id.startswith(source_prefix)
                or not re.fullmatch(r"-?[1-9][0-9]{0,19}", source_id)
                or body.event_id
                != "evt_" + hashlib.sha256(capture.source_event_id.encode()).hexdigest()
                or not approval.starts_at
                <= capture.occurred_at
                <= (capture.received_at)
                < approval.ends_at
                or not 0 <= (capture.received_at - capture.occurred_at).total_seconds() <= 30
                or (capture.received_at - datetime.fromisoformat(clock())).total_seconds() > 2
            ):
                raise CoreError("LIVE_TEXT_CAPTURE_BINDING_CHANGED", 403)
        value = {**body.model_dump(mode="json"), "privacy_epoch": binding["privacy_epoch"]}
        prior = store.one("SELECT value FROM runtime_meta WHERE key = %s", (INPUT + body.event_id,))
        if prior:
            recorded = json.loads(prior["value"])
            prior_epoch = recorded.get("privacy_epoch")
            epoch_matches = prior_epoch == binding["privacy_epoch"] or (
                private_life.enabled(settings)
                and capture is not None
                and type(prior_epoch) is int
                and 0 <= prior_epoch < binding["privacy_epoch"]
            )
            if not epoch_matches or recorded != {**value, "privacy_epoch": prior_epoch}:
                raise CoreError("LIVE_TEXT_INPUT_CONFLICT", 409)
        else:
            if store.one("SELECT 1 FROM events WHERE event_id = %s", (body.event_id,)):
                raise CoreError("LIVE_TEXT_HISTORICAL_INPUT_REFUSED", 409)
            count = store.one(
                (
                    (
                        'SELECT COUNT(*) AS n FROM runtime_meta WHERE key LIKE '
                        '%s AND (CASE WHEN value IS JSON THEN '
                        "(value::json#>>'{window_sha256}') END)=%s"
                    )
                ),
                (INPUT + "%", approval.plan_sha256),
            )["n"]
            from companion.poke import input_count

            count += input_count(store, approval.plan_sha256)
            if count >= approval.max_inputs or attempts >= approval.max_model_attempts:
                raise CoreError("LIVE_TEXT_INPUT_OR_MODEL_CAP", 429)
            if not private_life.enabled(settings) and store.one(
                "SELECT 1 FROM jobs WHERE state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT') LIMIT 1"
            ):
                raise CoreError("LIVE_TEXT_ONE_JOB_AT_A_TIME", 409)
            cumulative.admit_operation(
                store,
                settings,
                clock(),
                "input",
                body.event_id,
                paid_budget.sha(value),
                at=capture.received_at if capture else None,
            )
            store.insert("runtime_meta", key=INPUT + body.event_id, value=encode(value))
    return {"approved": True}


def check_input(store, settings, clock, event):
    if not active(store, settings):
        return False
    approval, _, _ = guard(store, settings, clock, input_only=True)
    text = event.payload.text
    references_only = approval.rich_input_scope == "references_v1" and bool(event.payload.rich)
    if (
        event.event_type != "message.received"
        or event.payload.media
        or any(
            item.kind not in {"image", "file"}
            or (item.kind == "image" and approval.image_scope != "owner_image_v1")
            or (item.kind == "file" and approval.file_scope != "owner_file_v1")
            for item in event.payload.shared_content
        )
        or (event.payload.rich and approval.rich_input_scope != "references_v1")
        or (not text.strip() and not references_only)
        or (text.strip().startswith("/") and not private_life.enabled(settings))
        or len(text) > approval.max_input_chars
        or not approval.starts_at <= event.occurred_at < approval.ends_at
    ):
        raise CoreError("LIVE_TEXT_ORDINARY_OWNER_TEXT_REQUIRED", 403)
    receipt = store.one("SELECT value FROM runtime_meta WHERE key = %s", (INPUT + event.event_id,))
    value = json.loads(receipt["value"]) if receipt else {}
    if (
        value.get("window_sha256") != approval.plan_sha256
        or (
            value.get("privacy_epoch") != epoch(store)
            and not (
                private_life.enabled(settings)
                and type(value.get("privacy_epoch")) is int
                and 0 <= value["privacy_epoch"] < epoch(store)
                and value.get("capture")
            )
        )
        or value.get("input_sha256") != _input_digest(event.payload.model_dump())
    ):
        raise CoreError("LIVE_TEXT_APPROVED_INPUT_REQUIRED", 403)
    image = qq_images.manifest(event.payload.model_dump())
    files = qq_files.manifest(event.payload.model_dump())
    if image and approval.image_scope != "owner_image_v1":
        raise CoreError("LIVE_IMAGE_APPROVAL_REQUIRED", 403)
    capture = json.loads(receipt["value"]).get("capture")
    if capture and private_life.enabled(settings):
        from companion.input_protocol import _signature

        _signature(approval, capture, CAPTURE_SIGNATURE_DOMAIN)
        if (
            capture.get("input_sha256") != value["input_sha256"]
            or capture.get("event_id") != event.event_id
            or capture.get("window_sha256") != approval.plan_sha256
        ):
            raise CoreError("LIVE_TEXT_CAPTURE_BINDING_CHANGED", 403)
    if (image or files) and not capture:
        raise CoreError("LIVE_TEXT_AUTHENTICATED_CAPTURE_REQUIRED", 403)
    if capture and (
        capture["source_event_id"] != event.source_event_id
        or datetime.fromisoformat(capture["occurred_at"]) != event.occurred_at
    ):
        raise CoreError("LIVE_TEXT_CAPTURE_BINDING_CHANGED", 403)
    return True


def check_job(store, settings, clock, job, *, current_request=None, current_delivery=None):
    with nullcontext() if store.in_transaction else store.transaction():
        from companion.reply_revision import check_job as check_reply_job

        check_reply_job(store, job)
        approval, _, attempts = guard(
            store,
            settings,
            clock,
            current_request=current_request,
            current_delivery=current_delivery,
        )
        _check_job(store, settings, clock, job, approval)
        return approval, attempts


def _check_job(store, settings, clock, job, approval):
    """Check job evidence after the caller's current runtime guard."""
    if private_life.enabled(settings):
        private_life.check_job(store, settings, approval, job)
        return
    receipt = store.one("SELECT value FROM runtime_meta WHERE key = %s", (INPUT + job["event_id"],))
    event = store.one(
        "SELECT events.event_id AS event_id, events.schema_version AS "
        "schema_version, events.source AS source, events.source_event_id AS source_event_id, "
        "events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type "
        "AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, "
        "events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS "
        "trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, "
        "events.redacted AS redacted, events.memory_processed AS memory_processed, "
        "events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = "
        "%s",
        (job["event_id"],),
    )
    value = json.loads(receipt["value"]) if receipt else {}
    if (
        not event
        or event["redacted"]
        or job["kind"] != "chat"
        or value.get("window_sha256") != approval.plan_sha256
        or value.get("privacy_epoch") != epoch(store)
    ):
        raise CoreError("LIVE_TEXT_APPROVED_JOB_REQUIRED", 403)
    body = json.loads(event["body_json"])
    text = body["text"]
    if (
        text.strip().startswith("/")
        or (body.get("rich") and approval.rich_input_scope != "references_v1")
        or _input_digest(body) != json.loads(receipt["value"])["input_sha256"]
    ):
        raise CoreError("LIVE_TEXT_INPUT_CHANGED", 403)
    if qq_images.manifest(body):
        qq_images.source(store, settings, approval, job["event_id"])
    _check_context(store, settings, clock, job, approval)
    if approval.memory_scope == "approved_facts":
        _check_memory(store, settings, clock, job, approval)


def _history_fingerprint(store, settings, clock, approval, source):
    """Validate original input/reply receipts without copying private text into metadata."""
    receipt = store.one(
        "SELECT value FROM runtime_meta WHERE key = %s", (INPUT + source["event_id"],)
    )
    reply = store.one(
        "SELECT value FROM runtime_meta WHERE key = %s",
        ("live_text_reply:" + source["message_id"],),
    )
    if not receipt or not reply:
        return None
    value, reply_value = json.loads(receipt["value"]), json.loads(reply["value"])
    capture = value.get("capture")
    if not capture:
        return None
    try:
        capture = CaptureReceipt.model_validate(capture)
        signed = capture.model_dump(mode="json", exclude={"signature"})
        for key in ("received_at", "occurred_at"):
            signed[key] = getattr(capture, key).isoformat()
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(approval.capture_public_key or "")).verify(
            bytes.fromhex(capture.signature or ""),
            CAPTURE_SIGNATURE_DOMAIN + encode(signed).encode(),
        )
    except (ValueError, InvalidSignature):
        return None
    if (
        value.get("window_sha256") != approval.plan_sha256
        or capture.window_sha256 != approval.plan_sha256
    ):
        return None
    body = json.loads(source["body_json"])
    jobs = store.rows(
        "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, "
        "jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS attempts, "
        "jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS "
        "lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS "
        "last_error_code, jobs.created_at AS created_at, jobs.updated_at AS updated_at, "
        "jobs.lease_token AS lease_token, jobs.context_tracked AS context_tracked, "
        "jobs.authority_json AS authority_json FROM jobs AS jobs WHERE jobs.event_id = %s AND "
        "jobs.kind = 'chat'",
        (source["event_id"],),
    )
    if len(jobs) != 1 or jobs[0]["state"] != "SUCCEEDED":
        return None
    try:
        usage = _reply_usage(store, settings, clock, jobs[0], approval)
        _check_context(store, settings, clock, jobs[0], approval, required=True)
    except CoreError:
        return None
    source_prefix = (
        f"{settings.bot_qq_id}:onebot-direct:FriendMessage:{settings.owner_qq_id}:"
        f"epoch:{capture.protocol_epoch}:"
    )
    if (
        value.get("privacy_epoch") != epoch(store)
        or value.get("event_id") != source["event_id"]
        or value.get("input_sha256") != _input_digest(body)
        or (body.get("rich") and approval.rich_input_scope != "references_v1")
        or capture.event_id != source["event_id"]
        or capture.source_event_id != source["source_event_id"]
        or capture.input_sha256 != value["input_sha256"]
        or not capture.source_event_id.startswith(source_prefix)
        or capture.identity_sha256 != paid_budget.sha([settings.owner_qq_id, settings.bot_qq_id])
        or datetime.fromisoformat(source["occurred_at"]) != capture.occurred_at
        or not approval.starts_at <= capture.occurred_at <= capture.received_at < approval.ends_at
        or body.get("media")
        or body.get("shared_content")
        or source["user_text"].strip().startswith("/")
        or source["event_type"] != "message.received"
    ):
        return None
    if "reply_segments" in source:
        try:
            group, envelopes = _reply_material(store, approval, source["message_id"])
        except CoreError:
            return None
        if not group["segmented"]:
            return None
        segments = [
            {"message_id": part["message_id"], **envelopes[part["message_id"]]}
            for part in group["rows"]
        ]
        if (
            group["text"] != source["reply_text"]
            or source["reply_segments"] != segments
            or any(part["state"] != "SENT" for part in group["rows"])
        ):
            return None
        receipts = []
        for part in group["rows"]:
            actual = _meta(store, "live_text_reply:" + part["message_id"])
            expected = _reply_receipt(
                approval,
                jobs[0],
                part["message_id"],
                json.loads(part["content_json"])["text"],
                usage,
                epoch(store),
                envelopes[part["message_id"]],
            )
            if actual != expected:
                return None
            receipts.append(actual)
        return {
            "event_id": source["event_id"],
            "message_id": source["message_id"],
            "message_ids": [part["message_id"] for part in group["rows"]],
            "source_sha256": paid_budget.sha(dict(source)),
            "input_receipt_sha256": paid_budget.sha(value),
            "reply_receipts_sha256": paid_budget.sha(receipts),
        }
    if (
        source["delivery_key"] != f"reply:{source['event_id']}:v1"
        or source["content_sha256"] != paid_budget.sha({"text": source["reply_text"]})
        or reply_value
        != _reply_receipt(
            approval,
            jobs[0],
            source["message_id"],
            source["reply_text"],
            usage,
            epoch(store),
        )
    ):
        return None
    return {
        "event_id": source["event_id"],
        "message_id": source["message_id"],
        "source_sha256": paid_budget.sha(dict(source)),
        "input_receipt_sha256": paid_budget.sha(value),
        "reply_receipt_sha256": paid_budget.sha(reply_value),
    }


def history_sources(store, settings, clock, job):
    from companion import dialogue_history

    approval, _ = check_job(store, settings, clock, job)
    if approval.history_scope == "none":
        return []
    allowed = [
        row["key"].removeprefix(INPUT)
        for row in store.rows("SELECT key FROM runtime_meta WHERE key LIKE %s", (INPUT + "%",))
    ]
    sources = [
        source
        for source in dialogue_history.recent_sent_sources(
            store,
            job["conversation_id"],
            job["event_id"],
            approval.max_inputs,
            event_ids=allowed,
        )
        if _history_fingerprint(store, settings, clock, approval, source)
    ]
    sources = sources[-approval.max_history_turns :]
    while (
        sources
        and sum(
            len(message["content"].encode("utf-8"))
            for source in sources
            for message in dialogue_history.turn_messages(source)
        )
        > approval.max_history_utf8_bytes
    ):
        sources.pop(0)
    return sources


def memory_fact(store, settings, clock, memory_id):
    """A native owner fact's approval pins, never a substitute for semantic retrieval."""
    row = store.one(
        "SELECT memories.memory_id AS memory_id, memories.subject AS subject, "
        "memories.kind AS kind, memories.content AS content, memories.status AS status, "
        "memories.origin AS origin, memories.confidence AS confidence, memories.importance AS "
        "importance, memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS "
        "supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS "
        "updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, "
        "memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, "
        "memories.needs_review AS needs_review FROM memories AS memories WHERE memories.memory_id "
        "= %s",
        (memory_id,),
    )
    evidence = store.rows(
        "SELECT e.event_id AS event_id, e.schema_version AS schema_version, e.source AS source, "
        "e.source_event_id AS source_event_id, e.conversation_id AS conversation_id, e.actor_id AS "
        "actor_id, e.event_type AS event_type, e.occurred_at AS occurred_at, e.ingested_at AS "
        "ingested_at, e.privacy_scope AS privacy_scope, e.body_json AS body_json, e.trace_id AS "
        "trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS fingerprint, e.redacted "
        "AS redacted, e.memory_processed AS memory_processed, e.reply_revision AS reply_revision, "
        "v.polarity AS polarity, c.scope AS scope, c.owner_actor_id AS owner_actor_id FROM "
        "memory_evidence AS v JOIN events AS e ON v.event_id = e.event_id JOIN conversations AS c "
        "ON e.conversation_id = c.conversation_id WHERE v.memory_id = %s ORDER BY e.event_id NULLS "
        "FIRST",
        (memory_id,),
    )
    if (
        not row
        or row["status"] != "CONFIRMED"
        or row["needs_review"]
        or row["subject"] not in {"user", "project"}
        or row["privacy_scope"] != "private"
        or row["confidence"] <= 0
        or not row["content"]
        or not evidence
        or any(
            e["actor_id"] != "owner"
            or e["scope"] != "private"
            or e["owner_actor_id"] != "owner"
            or e["privacy_scope"] != "private"
            or e["redacted"]
            or e["polarity"] != 1
            or e["source"] not in {"qq.onebot", "owner.decision"}
            or e["ingested_at"] < after(clock(), -settings.raw_event_retention_days * 86400)
            or store.one(
                "SELECT 1 FROM runtime_meta WHERE key = %s", ("erased_event:" + e["event_id"],)
            )
            or store.one("SELECT 1 FROM source_grant_events WHERE event_id = %s", (e["event_id"],))
            for e in evidence
        )
    ):
        raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
    return {
        "memory_id": memory_id,
        "subject": row["subject"],
        "status": row["status"],
        "content_sha256": paid_budget.sha(row["content"]),
        "memory_sha256": paid_budget.sha(row),
        "evidence_sha256": paid_budget.sha(evidence),
    }


def memory_sources(store, settings, clock, approval):
    expected = [fact.model_dump() for fact in approval.memory_facts]
    actual = [memory_fact(store, settings, clock, fact["memory_id"]) for fact in expected]
    if actual != expected:
        raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
    for fact in expected:
        oldest = store.one(
            "SELECT MIN(e.ingested_at) AS at FROM memory_evidence AS v JOIN events "
            "AS e USING (event_id) WHERE v.memory_id = %s",
            (fact["memory_id"],),
        )["at"]
        if approval.ends_at > datetime.fromisoformat(
            after(
                oldest,
                settings.raw_event_retention_days * 86400,
            )
        ):
            raise CoreError("LIVE_TEXT_MEMORY_SOURCE_EXPIRES", 503)
    return actual


def memory_ids(store, settings, clock, job):
    approval, _ = check_job(store, settings, clock, job)
    if approval.memory_scope != "approved_facts":
        raise CoreError("LIVE_TEXT_MEMORY_SCOPE_REQUIRED", 403)
    return [fact.memory_id for fact in approval.memory_facts]


def _checked_memory_rows(store, settings, clock, approval, rows):
    allowed = {item["memory_id"]: item for item in memory_sources(store, settings, clock, approval)}
    sources = []
    for row in rows:
        source = allowed.get(row["memory_id"])
        if (
            not source
            or paid_budget.sha(row["content"]) != source["content_sha256"]
            or row["status"] != source["status"]
            or row["subject"] != source["subject"]
            or row["needs_review"]
        ):
            raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
        sources.append(source)
    if len({source["memory_id"] for source in sources}) != len(sources):
        raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
    return sources


def _check_history(store, settings, clock, job, approval, expected):
    from companion import dialogue_history

    sources = dialogue_history.recent_sent_sources(
        store,
        store.one("SELECT conversation_id FROM events WHERE event_id = %s", (job["event_id"],))[
            "conversation_id"
        ],
        job["event_id"],
        approval.max_history_turns,
        event_ids=[item["event_id"] for item in expected],
    )
    if (approval.history_scope == "none" and expected) or [
        _history_fingerprint(store, settings, clock, approval, s) for s in sources
    ] != expected:
        raise CoreError("LIVE_TEXT_HISTORY_CHANGED", 503)


def _has_images(store, approval, job):
    if approval.image_scope != "owner_image_v1":
        return False
    body = json.loads(
        store.one(
            "SELECT body_json FROM events WHERE event_id = %s",
            (job["event_id"],),
        )["body_json"]
    )
    return bool(qq_images.manifest(body))


def _link_sources(store, settings, job):
    """Bind current-card Reader results, including explicit unavailable states."""
    from companion.sources import content_target

    body = json.loads(
        store.one(
            "SELECT body_json FROM events WHERE event_id = %s",
            (job["event_id"],),
        )["body_json"]
    )
    urls = {
        field["value"]
        for segment in (body.get("rich") or {}).get("segments", [])
        if segment["type"] == "json"
        for field in segment["fields"]
        if field["kind"] == "url"
    }
    result = []
    for item in store.rows(
        "SELECT content_items.item_id AS item_id, content_items.owner_event_id AS owner_event_id, "
        "content_items.kind AS kind, content_items.slot AS slot, content_items.source_event_id AS "
        "source_event_id, content_items.target_url AS target_url, content_items.read_request_id AS "
        "read_request_id, content_items.state AS state, content_items.media_sha256 AS "
        "media_sha256, content_items.fingerprint AS fingerprint, "
        "content_items.captured_privacy_epoch AS captured_privacy_epoch, "
        "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
        "usage_request_id, content_items.output_event_id AS output_event_id, "
        "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
        "content_items.deadline AS deadline, content_items.error_code AS error_code, "
        "content_items.redacted AS redacted, content_items.created_at AS created_at, "
        "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
        "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'link' ORDER BY "
        "content_items.slot NULLS FIRST",
        (job["event_id"],),
    ):
        if (
            item["redacted"]
            or item["captured_privacy_epoch"] != epoch(store)
            or item["target_url"] not in urls
        ):
            raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)
        read = store.one(
            "SELECT exploration_requests.request_id AS request_id, "
            "exploration_requests.request_key AS request_key, exploration_requests.adapter_id AS "
            "adapter_id, exploration_requests.operation AS operation, exploration_requests.url AS "
            "url, exploration_requests.goal_step_json AS goal_step_json, "
            "exploration_requests.state AS state, exploration_requests.lease_token AS lease_token, "
            "exploration_requests.lease_until AS lease_until, "
            "exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, "
            "exploration_requests.reserved_bytes AS reserved_bytes, "
            "exploration_requests.used_bytes AS used_bytes, exploration_requests.result_sha256 AS "
            "result_sha256, exploration_requests.last_error_code AS last_error_code, "
            "exploration_requests.created_at AS created_at, exploration_requests.started_at AS "
            "started_at, exploration_requests.updated_at AS updated_at, "
            "exploration_requests.redacted AS redacted, exploration_requests.reserved_requests AS "
            "reserved_requests, exploration_requests.used_requests AS used_requests, "
            "exploration_requests.source_grant_id AS source_grant_id, "
            "exploration_requests.source_grant_epoch AS source_grant_epoch, "
            "exploration_requests.authority_json AS authority_json, exploration_requests.query AS "
            "query, exploration_requests.discovery_mode AS discovery_mode FROM "
            "exploration_requests AS exploration_requests WHERE exploration_requests.request_id = "
            "%s",
            (item["read_request_id"],),
        )
        evidence = store.rows(
            "SELECT o.event_id AS event_id, o.adapter_id AS adapter_id, o.source_kind AS "
            "source_kind, o.result_status AS result_status, o.source_sha256 AS source_sha256, "
            "o.canonical_url AS canonical_url, o.device_id AS device_id, o.session_id AS "
            "session_id, o.consent_id AS consent_id, o.evidence_json AS evidence_json, "
            "o.captured_privacy_epoch AS captured_privacy_epoch, o.redacted AS redacted, "
            "o.created_at AS created_at, e.body_json AS body_json, e.redacted AS event_redacted "
            "FROM observations AS o JOIN events AS e ON o.event_id = e.event_id JOIN "
            "exploration_evidence AS x ON o.event_id = x.event_id WHERE x.request_id = %s ORDER BY "
            "o.event_id NULLS FIRST",
            (item["read_request_id"],),
        )
        if read:
            adapter = settings.observation_adapters.get(read["adapter_id"])
            if (
                read["redacted"]
                or read["captured_privacy_epoch"] != epoch(store)
                or read["operation"] != "read"
                or read["url"] != item["target_url"]
                or adapter is None
                or adapter.source != content_target(item["target_url"])[0]
                or not store.one(
                    "SELECT 1 FROM exploration_evidence WHERE request_id = %s AND event_id = %s",
                    (read["request_id"], job["event_id"]),
                )
            ):
                raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)
        elif item["read_request_id"]:
            raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)
        dependencies = []
        for observed in evidence:
            sources = store.rows(
                "SELECT source_event_id FROM event_dependencies WHERE event_id = "
                "%s ORDER BY source_event_id NULLS FIRST",
                (observed["event_id"],),
            )
            text = json.loads(observed["body_json"]).get("text", "")
            if (
                observed["redacted"]
                or observed["event_redacted"]
                or observed["captured_privacy_epoch"] != epoch(store)
                or {"source_event_id": job["event_id"]} not in sources
                or hashlib.sha256(text.encode()).hexdigest() != observed["source_sha256"]
            ):
                raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)
            dependencies.append(sources)
        result.append(paid_budget.sha([item, read, evidence, dependencies]))
    return result


def freeze_memory_review(store, settings, clock, job, rows, messages, history=()):
    from companion.model import model_payload

    with store.transaction():
        approval, _ = check_job(store, settings, clock, job)
        sources = _checked_memory_rows(store, settings, clock, approval, rows)
        historical = [_history_fingerprint(store, settings, clock, approval, s) for s in history]
        if any(source is None for source in historical):
            raise CoreError("LIVE_TEXT_HISTORY_CHANGED", 503)
        _check_history(store, settings, clock, job, approval, historical)
        value = {
            "plan_sha256": approval.plan_sha256,
            "privacy_epoch": epoch(store),
            "event_id": job["event_id"],
            "candidates": sources,
            "history_sources": historical,
            "history_sources_sha256": paid_budget.sha(historical),
            "candidates_sha256": paid_budget.sha(sources),
            "review_payload_sha256": paid_budget.sha(
                model_payload(settings, messages, "memory_review")
            )
            if sources
            else None,
            "selected": None if sources else [],
            "review_request_id": None,
            "selected_sha256": None if sources else paid_budget.sha([]),
        }
        if _has_images(store, approval, job):
            value["image_sources_sha256"] = paid_budget.sha(
                qq_images.result(store, settings, approval, job),
            )
        if _meta(store, MEMORY + job["job_id"]) is not None:
            raise CoreError("LIVE_TEXT_MEMORY_ALREADY_FROZEN", 409)
        _put(store, MEMORY + job["job_id"], value)


def _check_memory(store, settings, clock, job, approval):
    value = _meta(store, MEMORY + job["job_id"])
    usage = store.rows(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.job_id = %s ORDER BY record_sequence NULLS "
        "FIRST",
        (job["job_id"],),
    )
    images = _has_images(store, approval, job)
    if images:
        usage = [row for row in usage if row["scenario"] != "content_vision"]
    if value is None:
        if usage:
            raise CoreError("LIVE_TEXT_MEMORY_CONTEXT_REQUIRED", 503)
        return None
    if images and value.get("image_sources_sha256") != paid_budget.sha(
        qq_images.result(store, settings, approval, job),
    ):
        raise CoreError("LIVE_IMAGE_RESULT_CHANGED", 403)
    if paid_budget.sha(value["history_sources"]) != value["history_sources_sha256"]:
        raise CoreError("LIVE_TEXT_HISTORY_CHANGED", 503)
    _check_history(store, settings, clock, job, approval, value["history_sources"])
    allowed = {item["memory_id"]: item for item in memory_sources(store, settings, clock, approval)}
    if (
        value["plan_sha256"] != approval.plan_sha256
        or value["privacy_epoch"] != epoch(store)
        or value["event_id"] != job["event_id"]
        or bool(value["candidates"]) != bool(value["review_payload_sha256"])
        or (not value["candidates"] and value["review_request_id"] is not None)
        or (
            value["candidates"] and value["selected"] is not None and not value["review_request_id"]
        )
        or paid_budget.sha(value["candidates"]) != value["candidates_sha256"]
        or (
            value["selected"] is not None
            and paid_budget.sha(value["selected"]) != value["selected_sha256"]
        )
        or len({s["memory_id"] for s in value["candidates"]}) != len(value["candidates"])
        or (
            value["selected"] is not None
            and len({s["memory_id"] for s in value["selected"]}) != len(value["selected"])
        )
        or any(allowed.get(item["memory_id"]) != item for item in value["candidates"])
        or (
            value["selected"] is not None
            and any(item not in value["candidates"] for item in value["selected"])
        )
    ):
        raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
    if value["candidates"] and usage:
        admission = store.one(
            "SELECT payload_sha256 FROM model_admissions WHERE request_id = %s",
            (usage[0]["request_id"],),
        )
        if (
            usage[0]["scenario"] != "memory_review"
            or not admission
            or admission["payload_sha256"] != value["review_payload_sha256"]
        ):
            raise CoreError("LIVE_TEXT_MEMORY_REVIEW_REQUIRED", 503)
    if value["review_request_id"] and (
        not usage
        or usage[0]["request_id"] != value["review_request_id"]
        or usage[0]["scenario"] != "memory_review"
        or usage[0]["state"] != "SETTLED"
    ):
        raise CoreError("LIVE_TEXT_MEMORY_REVIEW_REQUIRED", 503)
    return value


def select_memory(store, settings, clock, job, request_id, rows):
    with store.transaction():
        approval, _ = check_job(store, settings, clock, job)
        value = _check_memory(store, settings, clock, job, approval)
        usage = store.rows(
            "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
            "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
            "model_usage.provider AS provider, model_usage.model AS model, "
            "model_usage.input_tokens AS input_tokens, model_usage.cached_input_tokens AS "
            "cached_input_tokens, model_usage.output_tokens AS output_tokens, "
            "model_usage.billed_micro_usd AS billed_micro_usd, model_usage.is_estimate AS "
            "is_estimate, model_usage.occurred_at AS occurred_at, model_usage.state AS state, "
            "model_usage.pricing_known AS pricing_known FROM model_usage AS model_usage WHERE "
            "model_usage.job_id = %s ORDER BY record_sequence NULLS FIRST",
            (job["job_id"],),
        )
        if _has_images(store, approval, job):
            usage = [row for row in usage if row["scenario"] != "content_vision"]
        if (
            not value
            or value["selected"] is not None
            or len(usage) != 1
            or usage[0]["request_id"] != request_id
            or usage[0]["scenario"] != "memory_review"
            or usage[0]["state"] != "SETTLED"
        ):
            raise CoreError("LIVE_TEXT_MEMORY_REVIEW_REQUIRED", 503)
        selected = _checked_memory_rows(store, settings, clock, approval, rows)
        if any(item not in value["candidates"] for item in selected):
            raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
        value.update(
            selected=selected,
            selected_sha256=paid_budget.sha(selected),
            review_request_id=request_id,
        )
        _put(store, MEMORY + job["job_id"], value)


def freeze_context(store, settings, clock, job, sources, messages, memories=()):
    from companion.model import model_payload

    with store.transaction():
        approval, _ = check_job(store, settings, clock, job)
        frozen = [_history_fingerprint(store, settings, clock, approval, s) for s in sources]
        if any(source is None for source in frozen):
            raise CoreError("LIVE_TEXT_HISTORY_CHANGED", 503)
        value = {
            "plan_sha256": approval.plan_sha256,
            "privacy_epoch": epoch(store),
            "conversation_id": job["conversation_id"],
            "event_id": job["event_id"],
            "sources": frozen,
            "payload_sha256": paid_budget.sha(model_payload(settings, messages, "chat")),
        }
        if approval.link_scope == "owner_links_v1":
            value["link_sources"] = _link_sources(store, settings, job)
        if approval.image_scope == "owner_image_v1":
            body = json.loads(
                store.one("SELECT body_json FROM events WHERE event_id = %s", (job["event_id"],))[
                    "body_json"
                ]
            )
            value["image_sources"] = (
                [qq_images.result(store, settings, approval, job)]
                if qq_images.manifest(body)
                else []
            )
        if approval.memory_scope == "approved_facts":
            memory = _check_memory(store, settings, clock, job, approval)
            selected = _checked_memory_rows(store, settings, clock, approval, memories)
            if (
                not memory
                or memory["selected"] is None
                or any(item not in memory["selected"] for item in selected)
            ):
                raise CoreError("LIVE_TEXT_MEMORY_REVIEW_REQUIRED", 503)
            value["memory_sources"] = selected
            if approval.history_scope == "approved_window" and (
                frozen != memory["history_sources"] or selected != memory["selected"]
            ):
                raise CoreError("LIVE_TEXT_CONTEXT_SOURCES_CHANGED", 503)
        prior = store.one(
            "SELECT value FROM runtime_meta WHERE key = %s", (CONTEXT + job["job_id"],)
        )
        if prior and json.loads(prior["value"]) != value:
            raise CoreError("LIVE_TEXT_CONTEXT_ALREADY_FROZEN", 409)
        if not prior:
            store.insert("runtime_meta", key=CONTEXT + job["job_id"], value=encode(value))


def _check_context(store, settings, clock, job, approval, *, required=False):
    row = store.one("SELECT value FROM runtime_meta WHERE key = %s", (CONTEXT + job["job_id"],))
    if not row:
        # Early job checks precede context construction. Once an attempt has been
        # reserved, missing evidence must also fence response acceptance/delivery.
        usage_sql = "SELECT 1 FROM model_usage WHERE job_id=%s"
        if approval.memory_scope == "approved_facts" or approval.image_scope == "owner_image_v1":
            usage_sql += " AND scenario='chat'"
        if required or store.one(usage_sql, (job["job_id"],)):
            raise CoreError("LIVE_TEXT_CONTEXT_REQUIRED", 503)
        return None
    value = json.loads(row["value"])
    event = store.one("SELECT conversation_id FROM events WHERE event_id = %s", (job["event_id"],))
    if (
        value["plan_sha256"] != approval.plan_sha256
        or value["privacy_epoch"] != epoch(store)
        or value["event_id"] != job["event_id"]
        or not event
        or value["conversation_id"] != event["conversation_id"]
    ):
        raise CoreError("LIVE_TEXT_HISTORY_CHANGED", 503)
    expected = value["sources"]
    _check_history(store, settings, clock, job, approval, expected)
    if approval.link_scope == "owner_links_v1" and (
        value.get("link_sources") != _link_sources(store, settings, job)
    ):
        raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)
    if approval.image_scope == "owner_image_v1":
        body = json.loads(
            store.one("SELECT body_json FROM events WHERE event_id = %s", (job["event_id"],))[
                "body_json"
            ]
        )
        expected_images = (
            [qq_images.result(store, settings, approval, job)] if qq_images.manifest(body) else []
        )
        if value.get("image_sources") != expected_images:
            raise CoreError("LIVE_IMAGE_RESULT_CHANGED", 403)
    if approval.memory_scope == "approved_facts":
        memory = _meta(store, MEMORY + job["job_id"])
        if (
            not memory
            or memory["selected"] is None
            or any(item not in memory["selected"] for item in value.get("memory_sources", []))
            or "memory_sources" not in value
        ):
            raise CoreError("LIVE_TEXT_MEMORY_SOURCE_CHANGED", 503)
        if approval.history_scope == "approved_window" and (
            expected != memory["history_sources"] or value["memory_sources"] != memory["selected"]
        ):
            raise CoreError("LIVE_TEXT_CONTEXT_SOURCES_CHANGED", 503)
    if approval.memory_scope == "approved_facts" or approval.image_scope == "owner_image_v1":
        admission = store.one(
            "SELECT a.payload_sha256 FROM model_admissions AS a JOIN model_usage AS m USING "
            "(request_id) WHERE m.job_id = %s AND m.scenario = 'chat'",
            (job["job_id"],),
        )
        if admission and admission["payload_sha256"] != value["payload_sha256"]:
            raise CoreError("LIVE_TEXT_CONTEXT_PAYLOAD_CHANGED", 503)
    return value


def reserve_model(store, settings, clock, job_id, scenario):
    if not active(store, settings):
        return
    if private_life.enabled(settings):
        return private_life.admit_model(store, settings, clock, job_id, scenario)
    job = store.one(
        "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, "
        "jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS attempts, "
        "jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS "
        "lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS "
        "last_error_code, jobs.created_at AS created_at, jobs.updated_at AS updated_at, "
        "jobs.lease_token AS lease_token, jobs.context_tracked AS context_tracked, "
        "jobs.authority_json AS authority_json FROM jobs AS jobs WHERE jobs.job_id = %s",
        (job_id,),
    )
    if not job:
        raise CoreError("LIVE_TEXT_CHAT_ONLY", 403)
    approval, attempts = check_job(store, settings, clock, job)
    usage = store.rows(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.job_id = %s ORDER BY record_sequence NULLS "
        "FIRST",
        (job_id,),
    )
    if attempts >= approval.max_model_attempts:
        raise CoreError("LIVE_TEXT_MODEL_ATTEMPT_CAP", 429)
    images = _has_images(store, approval, job)
    if images and scenario == "content_vision":
        value, row = qq_images.frozen(store, settings, approval, job)
        qq_images.lease(
            store, settings, approval, clock, job["event_id"], request_id=value["request_id"]
        )
        if usage or row["state"] != "RUNNING":
            raise CoreError("LIVE_IMAGE_ALREADY_ATTEMPTED", 409)
        return
    prefix = ["content_vision"] if images else []
    if images:
        qq_images.result(store, settings, approval, job)
    if approval.memory_scope == "approved_facts":
        memory = _check_memory(store, settings, clock, job, approval)
        if not memory:
            raise CoreError("LIVE_TEXT_MEMORY_CONTEXT_REQUIRED", 503)
        if scenario == "memory_review":
            valid = bool(
                memory["candidates"]
                and memory["selected"] is None
                and [row["scenario"] for row in usage] == prefix
                and all(row["state"] == "SETTLED" for row in usage)
            )
        elif scenario == "chat":
            _check_context(store, settings, clock, job, approval, required=True)
            expected = prefix + (["memory_review"] if memory["candidates"] else [])
            valid = (
                memory["selected"] is not None
                and [row["scenario"] for row in usage] == expected
                and all(row["state"] == "SETTLED" for row in usage)
                and (
                    not memory["candidates"]
                    or memory["review_request_id"] == usage[-1]["request_id"]
                )
            )
        else:
            valid = False
        if not valid:
            raise CoreError("LIVE_TEXT_MEMORY_STAGE_REQUIRED", 403)
    elif (
        scenario != "chat"
        or [row["scenario"] for row in usage] != prefix
        or any(row["state"] != "SETTLED" for row in usage)
    ):
        raise CoreError("LIVE_TEXT_MODEL_ATTEMPT_CAP", 429)
    elif images:
        _check_context(store, settings, clock, job, approval, required=True)


def check_model(store, settings, clock, request_id):
    if not active(store, settings):
        return
    if private_life.enabled(settings):
        return private_life.check_model(store, settings, clock, request_id)
    job = store.one(
        "SELECT j.job_id AS job_id, j.event_id AS event_id, j.kind AS kind, j.state AS state, "
        "j.idempotency_key AS idempotency_key, j.attempts AS attempts, j.max_attempts AS "
        "max_attempts, j.lease_owner AS lease_owner, j.lease_until AS lease_until, "
        "j.next_attempt_at AS next_attempt_at, j.last_error_code AS last_error_code, j.created_at "
        "AS created_at, j.updated_at AS updated_at, j.lease_token AS lease_token, "
        "j.context_tracked AS context_tracked, j.authority_json AS authority_json FROM jobs AS j "
        "JOIN model_usage AS m ON j.job_id = m.job_id WHERE m.request_id = %s",
        (request_id,),
    )
    usage_row = store.one("SELECT scenario FROM model_usage WHERE request_id = %s", (request_id,))
    vision = bool(usage_row and usage_row["scenario"] == "content_vision")
    if not job or (not vision and (job["state"] != "RUNNING" or job["lease_until"] <= clock())):
        raise CoreError("LIVE_TEXT_MODEL_JOB_FENCED", 409)
    approval, _ = check_job(store, settings, clock, job, current_request=request_id)
    usage = store.rows(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.job_id = %s ORDER BY record_sequence NULLS "
        "FIRST",
        (job["job_id"],),
    )
    if not usage or usage[-1]["request_id"] != request_id or usage[-1]["state"] != "RESERVED":
        raise CoreError("LIVE_TEXT_MEMORY_STAGE_REQUIRED", 403)
    if vision:
        value, _ = qq_images.frozen(store, settings, approval, job)
        qq_images.lease(store, settings, approval, clock, job["event_id"], request_id=request_id)
        if len(usage) != 1:
            raise CoreError("LIVE_IMAGE_STAGE_REQUIRED", 403)
        payload = value["payload_sha256"]
    elif approval.memory_scope == "approved_facts" and usage[-1]["scenario"] == "memory_review":
        memory = _check_memory(store, settings, clock, job, approval)
        prefix = ["content_vision"] if _has_images(store, approval, job) else []
        if (
            not memory
            or memory["selected"] is not None
            or [row["scenario"] for row in usage] != [*prefix, "memory_review"]
            or any(row["state"] != "SETTLED" for row in usage[:-1])
        ):
            raise CoreError("LIVE_TEXT_MEMORY_STAGE_REQUIRED", 403)
        payload = memory["review_payload_sha256"]
    else:
        context = _check_context(store, settings, clock, job, approval, required=True)
        payload = context["payload_sha256"]
        if approval.memory_scope == "approved_facts" or approval.image_scope == "owner_image_v1":
            _reply_usage(store, settings, clock, job, approval, pending=True)
    admission = store.one(
        "SELECT payload_sha256 FROM model_admissions WHERE request_id = %s", (request_id,)
    )
    if not admission or admission["payload_sha256"] != payload:
        raise CoreError("LIVE_TEXT_CONTEXT_PAYLOAD_CHANGED", 503)
    if vision:
        row, _, _ = qq_images.lease(
            store, settings, approval, clock, job["event_id"], request_id=request_id
        )
        return {**job, "lease_until": row["lease_until"], "lease_token": row["lease_token"]}
    current = store.one(
        "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS "
        "kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS "
        "attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, "
        "jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, "
        "jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, jobs.updated_at "
        "AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked AS context_tracked, "
        "jobs.authority_json AS authority_json FROM jobs AS jobs WHERE jobs.job_id = "
        "%s",
        (job["job_id"],),
    )
    if not current or current["state"] != "RUNNING" or current["lease_token"] != job["lease_token"]:
        raise CoreError("LIVE_TEXT_MODEL_JOB_FENCED", 409)
    if usage[-1]["scenario"] == "chat" and _has_images(store, approval, job):
        qq_images.original_parts(store, settings, clock, job)
        current["lease_until"] = min(
            current["lease_until"],
            qq_images.original_deadline(store, job),
        )
    return current


def dispatch_fence(
    settings,
    clock,
    job_lease_until,
    admission_lease_until,
    *,
    quota_state=None,
    request_id=None,
):
    """Pure final check after ALL journal/file/DB work; no I/O follows this clock read."""
    approval, paid = settings.live_text_window, settings.paid_model
    if approval is None or paid is None or approval.live_approved is not True:
        raise CoreError("LIVE_TEXT_APPROVAL_REQUIRED", 503)
    if settings.mode != "live" or approval.core_config_sha256 != config_sha256(settings):
        raise CoreError("LIVE_TEXT_RUNTIME_CHANGED", 503)
    # Parse leases before sampling time too; there are no locks, file reads or
    # awaits between the following sample and returning to the HTTP transport.
    leases = [
        datetime.fromisoformat(job_lease_until),
        datetime.fromisoformat(admission_lease_until),
    ]
    now = datetime.fromisoformat(clock())
    if not approval.starts_at <= now < approval.ends_at:
        raise CoreError("LIVE_TEXT_APPROVAL_EXPIRED", 503)
    if not paid_budget.active(paid, now) or not paid_budget.rates_valid(paid, now):
        raise CoreError("PAID_APPROVAL_EXPIRED", 503)
    if any(lease <= now for lease in leases):
        raise CoreError("LIVE_TEXT_MODEL_JOB_FENCED", 409)
    if quota_state is not None:
        cumulative.final_fence(settings, quota_state, now, request_id)


def _reply_usage(store, settings, clock, job, approval, *, pending=False):
    usage = store.rows(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.job_id = %s ORDER BY record_sequence NULLS "
        "FIRST",
        (job["job_id"],),
    )
    expected = []
    if _has_images(store, approval, job):
        qq_images.result(store, settings, approval, job)
        expected.append("content_vision")
    if approval.memory_scope == "approved_facts":
        memory = _check_memory(store, settings, clock, job, approval)
        if not memory or memory["selected"] is None:
            raise CoreError("LIVE_TEXT_MEMORY_REVIEW_REQUIRED", 503)
        if memory["candidates"]:
            if (
                len(usage) <= len(expected)
                or memory["review_request_id"] != usage[len(expected)]["request_id"]
            ):
                raise CoreError("LIVE_TEXT_MEMORY_REVIEW_REQUIRED", 503)
            expected.append("memory_review")
    expected.append("chat")
    states = ["SETTLED"] * len(expected)
    if pending:
        states[-1] = "RESERVED"
    if [r["scenario"] for r in usage] != expected or [r["state"] for r in usage] != states:
        raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
    return usage


def remaining_send_slots(store, approval):
    """Count attempts and each queued future attempt against this exact window."""
    if approval.max_send_attempts is None:
        return None
    if approval.companion_scope == "owner_private_v1":
        occupied = store.one(
            (
                (
                    'SELECT COALESCE(SUM(o.attempt_count + CASE WHEN '
                    "o.state IN ('PENDING','LEASED','RETRY_WAIT') THEN 1 "
                    'ELSE 0 END),0) n FROM outbox o JOIN runtime_meta r ON '
                    'r.key IN (%s || o.message_id, '
                    "'account_action_delivery:' || o.message_id) WHERE "
                    '(CASE WHEN r.value IS JSON THEN '
                    "(r.value::json#>>'{plan_sha256}') END)=%s"
                )
            ),
            (private_life.DELIVERY, approval.plan_sha256),
        )["n"]
        return approval.max_send_attempts - occupied
    occupied = store.one(
        (
            (
                'SELECT COALESCE(SUM(o.attempt_count + CASE WHEN '
                "o.state IN ('PENDING','LEASED','RETRY_WAIT') THEN 1 "
                'ELSE 0 END),0) AS n FROM outbox o JOIN runtime_meta r '
                'ON r.key=%s || o.origin_event_id WHERE '
                "o.action_type='qq.private.reply' AND (CASE WHEN "
                'r.value IS JSON THEN '
                "(r.value::json#>>'{window_sha256}') END)=%s"
            )
        ),
        (INPUT, approval.plan_sha256),
    )["n"]
    return approval.max_send_attempts - occupied


def _reply_material(store, approval, message_id):
    from companion.transport import reply_group, segment_envelopes

    group = reply_group(store, message_id)
    if not group or len(group["text"]) > approval.max_reply_chars:
        raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
    envelopes = segment_envelopes(group)
    if group["segmented"] and (
        approval.segmented_reply is None
        or (
            approval.segmented_reply.max_segments != "unlimited"
            and len(group["rows"]) > approval.segmented_reply.max_segments
        )
        or any(
            value["interval_ms"] != approval.segmented_reply.interval_ms
            for value in envelopes.values()
        )
    ):
        raise CoreError("LIVE_TEXT_REPLY_SCOPE_CHANGED", 403)
    return group, envelopes


def _reply_receipt(approval, job, message_id, reply, usage, privacy_epoch, segment=None):
    requests = (
        {"request_ids": [r["request_id"] for r in usage]}
        if (approval.memory_scope == "approved_facts" or len(usage) > 1)
        else {"request_id": usage[0]["request_id"]}
    )
    return {
        "message_id": message_id,
        "event_id": job["event_id"],
        "job_id": job["job_id"],
        **requests,
        "reply_sha256": paid_budget.sha(reply),
        "plan_sha256": approval.plan_sha256,
        "privacy_epoch": privacy_epoch,
        **({"reply_segments": segment} if segment is not None else {}),
    }


def check_delivery(store, settings, clock, message_id, *, check=None):
    with nullcontext() if store.in_transaction else store.transaction():
        return _check_delivery(store, settings, clock, message_id, check=check)


def _check_delivery(store, settings, clock, message_id, *, check):
    from companion.reply_revision import check_delivery as check_reply_delivery
    from companion.reply_revision import require_reply_source

    check_reply_delivery(store, message_id)
    if (
        check
        and check.reply_revision is not None
        and store.one(
            "SELECT 1 FROM outbox WHERE message_id = %s AND action_type = 'qq.private.reply'",
            (message_id,),
        )
    ):
        require_reply_source(store, check.event_id, check.reply_revision)
    if private_life.enabled(settings):
        row = private_life.check_delivery(store, settings, clock, message_id, check)
        if check and (row["state"] != "SENDING" or row["lease_until"] <= clock()):
            raise CoreError("LIVE_TEXT_SEND_FENCED", 409)
        return (
            {"approved": True, "lease_until": row["lease_until"]} if check else {"approved": True}
        )
    approval, _, _ = guard(store, settings, clock, current_delivery=message_id)
    row = store.one(
        "SELECT outbox.message_id AS message_id, outbox.origin_event_id AS "
        "origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS "
        "action_type, outbox.content_json AS content_json, outbox.content_sha256 AS "
        "content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, "
        "outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, "
        "outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, "
        "outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS "
        "last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, "
        "outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS "
        "next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS "
        "segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS "
        "reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, "
        "outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, "
        "outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = "
        "%s",
        (message_id,),
    )
    if not row or row["action_type"] != "qq.private.reply":
        raise CoreError("LIVE_TEXT_REPLY_REQUIRED", 403)
    job = store.one(
        "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, jobs.state AS "
        "state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS attempts, "
        "jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS "
        "lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS "
        "last_error_code, jobs.created_at AS created_at, jobs.updated_at AS updated_at, "
        "jobs.lease_token AS lease_token, jobs.context_tracked AS context_tracked, "
        "jobs.authority_json AS authority_json FROM jobs AS jobs WHERE jobs.event_id = %s AND "
        "jobs.kind = 'chat'",
        (row["origin_event_id"],),
    )
    if not job or job["state"] != "SUCCEEDED":
        raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
    _check_job(store, settings, clock, job, approval)
    usage = _reply_usage(store, settings, clock, job, approval)
    group, envelopes = _reply_material(store, approval, message_id)
    for part in group["rows"]:
        text = json.loads(part["content_json"])["text"]
        receipt = _meta(store, "live_text_reply:" + part["message_id"])
        expected = _reply_receipt(
            approval,
            job,
            part["message_id"],
            text,
            usage,
            epoch(store),
            envelopes[part["message_id"]],
        )
        key = (
            f"reply:{part['origin_event_id']}:v2:{part['segment_index']}"
            if group["segmented"]
            else f"reply:{part['origin_event_id']}:v1"
        )
        if (
            receipt != expected
            or part["delivery_key"] != key
            or part["content_sha256"] != paid_budget.sha({"text": text})
        ):
            raise CoreError("LIVE_TEXT_REPLY_RECEIPT_CHANGED", 403)
    slots = remaining_send_slots(store, approval)
    if slots is not None and slots < 0:
        raise CoreError("LIVE_TEXT_SEND_ATTEMPT_CAP", 429)
    from companion.transport import check_segment_ready

    check_segment_ready(store, message_id, clock())
    text = json.loads(row["content_json"])["text"]
    if check and (
        check.window_sha256 != approval.plan_sha256
        or check.event_id != row["origin_event_id"]
        or check.reply_sha256 != paid_budget.sha(text)
        or check.lease_token != row["lease_token"]
        or row["state"] != "SENDING"
        or row["lease_until"] <= clock()
    ):
        raise CoreError("LIVE_TEXT_SEND_FENCED", 409)
    return {"approved": True, "lease_until": row["lease_until"]} if check else {"approved": True}


def record_replies(store, settings, clock, job, message_ids):
    """Validate and record one complete frozen reply group in the same transaction."""
    if not message_ids:
        return
    if private_life.enabled(settings):
        return private_life.record_replies(store, settings, clock, job, message_ids)
    with nullcontext() if store.in_transaction else store.transaction():
        approval, _ = check_job(store, settings, clock, job)
        usage = _reply_usage(store, settings, clock, job, approval)
        group, envelopes = _reply_material(store, approval, message_ids[0])
        if [part["message_id"] for part in group["rows"]] != message_ids or any(
            part["origin_event_id"] != job["event_id"]
            or part["conversation_id"] != job["conversation_id"]
            for part in group["rows"]
        ):
            raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
        slots = remaining_send_slots(store, approval)
        if slots is not None and slots < 0:
            raise CoreError("LIVE_TEXT_SEND_ATTEMPT_CAP", 429)
        privacy_epoch = epoch(store)
        for part in group["rows"]:
            message_id = part["message_id"]
            receipt = _reply_receipt(
                approval,
                job,
                message_id,
                json.loads(part["content_json"])["text"],
                usage,
                privacy_epoch,
                envelopes[message_id],
            )
            store.insert("runtime_meta", key="live_text_reply:" + message_id, value=encode(receipt))


def send_check(store, settings, clock, body):
    with store.transaction():
        result = check_delivery(store, settings, clock, body.message_id, check=body)
        if settings.voice.enabled or body.voice is not None:
            from companion.voice_flow import VoiceFlow

            if store.one(
                "SELECT 1 FROM poke_batches WHERE response_message_id = %s", (body.message_id,)
            ):
                if body.voice is not None:
                    raise CoreError("VOICE_DELIVERY_BINDING_CHANGED", 403)
            else:
                VoiceFlow(store, settings, clock).validate_send(
                    body.message_id,
                    body.lease_token,
                    body.voice,
                )
        cumulative.admit_operation(
            store,
            settings,
            clock(),
            "send",
            body.message_id,
            paid_budget.sha(body.model_dump(mode="json")),
            logical=body.event_id,
        )
        return result


def stop_window(store, reference, clock=None):
    # Revocation must remain possible after expiry, approval removal or code drift.
    with store.transaction():
        history = _meta(store, HISTORY + reference.window_sha256)
        binding = _meta(store, BINDING)
        if history and history["state"] in {"STOPPED", "RETIRED"}:
            # Idempotent old STOP cannot alter the successor's journal or pointer.
            current = paid_budget.journal(
                store,
                SimpleNamespace(journal_path=history["binding"]["budget_journal_path"]),
                allow_settlement=True,
            )["lifecycle"]
            if not current or (
                current["plan_sha256"] == reference.window_sha256 and current["state"] != "STOPPED"
            ):
                raise CoreError("LIVE_TEXT_LIFECYCLE_MISMATCH", 503)
            image_spool.discard_window(store, reference.window_sha256)
            return {"stopped": True}
        if history and history["state"] == "PREPARED":
            binding = history["binding"]
        if not binding or binding["plan_sha256"] != reference.window_sha256:
            raise CoreError("LIVE_TEXT_WINDOW_MISMATCH", 403)
        frozen = binding
        identity = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
        if not identity or identity["value"] != frozen["budget_journal_id"]:
            raise CoreError("LIVE_TEXT_BUDGET_JOURNAL_CHANGED", 503)
        # Fsync the stop outside the DB's failure domain before acknowledging it.
        # Existing in-flight settlements can still record known costs, but this
        # record permanently fences new admission even if an old DB loses its flag.
        custody = SimpleNamespace(journal_path=frozen["budget_journal_path"])
        current = paid_budget.journal(store, custody, allow_settlement=True)["lifecycle"]
        if not current or current["state"] != "STOPPED":
            paid_budget.journal(
                store,
                custody,
                {
                    "kind": "window_stop",
                    "window_id": frozen["window_id"],
                    "plan_sha256": reference.window_sha256,
                },
                allow_settlement=True,
            )
        if store.one("SELECT 1 FROM runtime_meta WHERE key = %s", (cumulative.PROJECTION,)):
            cumulative.pause(store, custody, clock() if callable(clock) else None, "WINDOW_STOP")
        store.execute(
            "INSERT INTO runtime_meta VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (STOP + reference.window_sha256, "OWNER_STOP"),
        )
        store.execute(
            "INSERT INTO runtime_meta VALUES ('live_text_stopped', 'OWNER_STOP') ON CONFLICT DO "
            "NOTHING"
        )
        _put(
            store,
            HISTORY + reference.window_sha256,
            {**(history or {}), "binding": frozen, "state": "STOPPED"},
        )
        store.execute(
            (
                (
                    'UPDATE exploration_requests SET state=CASE state WHEN '
                    "'QUEUED' THEN 'CANCELLED' ELSE 'UNKNOWN' END,"
                    "lease_token=NULL,last_error_code='LIVE_WINDOW_STOPPED' "
                    "WHERE state IN ('QUEUED','RUNNING') AND request_id IN "
                    '(SELECT c.read_request_id FROM content_items c JOIN '
                    'runtime_meta r ON r.key=%s || c.owner_event_id WHERE '
                    "c.kind='link' AND (CASE WHEN r.value IS JSON THEN "
                    "(r.value::json#>>'{window_sha256}') END)=%s)"
                )
            ),
            (INPUT, reference.window_sha256),
        )
        if (history or {}).get("approval", {}).get("companion_scope") == "owner_private_v1":
            private_life.stop_work(store, clock() if callable(clock) else utcnow())
        from companion.evaluation import stop_window as stop_evaluations

        stop_evaluations(store, reference.window_sha256, clock() if callable(clock) else utcnow())
        image_spool.discard_window(store, reference.window_sha256)
    return {"stopped": True}
