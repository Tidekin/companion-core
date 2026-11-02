"""One-shot test windows. Receipts extend Spool; there is no second delivery queue.

The separate fsynced journal detects missing/older Spool state. Neither artifact is
an authorization signature: live operation trusts independently approved private
files and deployment attestation. Losing/rolling back both is a recovery blocker.
"""

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import account_actions, cumulative, reply_segments
from .event_normalizer import decode, normalize
from .journal import digest
from .network import (
    checked_http_client,
    private_http_client,
    private_request_matches,
    seal_protocol_content,
)
from .onebot_client import OneBotClient, bounded_post
from .outbound import deliver_tick
from .spool import Spool, encode

SHA = re.compile(r"[a-f0-9]{64}")
ACTIVE = {"PENDING", "LEASED", "SENDING", "RETRY_WAIT", "DELIVERY_UNCERTAIN"}
HANDOFF_DOMAIN = b"companion-core/window-handoff/v1\x00"
HANDOFF_FIELDS = {"predecessor_plan_sha256", "predecessor_window_id",
                  "budget_journal_head_sha256", "bridge_journal_head_sha256",
                  "identity_sha256", "protocol_epoch", "privacy_epoch",
                  "erasure_ledger_sha256", "evidence_sha256"}
INPUT_STATE_FIELDS = {"window_sha256", "event_id", "input_sha256", "reply_revision", "state",
                      "unsettled_model", "unsettled_delivery"}


def config_matches(config, expected):
    value = config.snapshot()
    if digest(value) == expected:
        return True
    # Historical v1 evidence predates the optional native authority field.
    if value.pop("owner_authority", None) is None:
        return digest(value) == expected
    return False


def delivery_origin(item):
    # Account plans have their own actual reference; they are never owner input events.
    return item["content"]["plan_id"] if account_actions.is_account(item) else (
        reply_segments.event_id(item))


def handoff_abort_proof(plan, successor, scope, requested, receipt):
    """An abort fences one exact proposal without authorizing its replacement."""
    fields = {"state", "window_sha256", "predecessor_window_sha256", "handoff_sha256",
              "predecessor_budget_journal_head_sha256", "budget_journal_head_sha256",
              "privacy_epoch", "erasure_ledger_sha256", "bridge_journal_head_sha256"}
    if (not isinstance(scope, dict) or set(scope) != HANDOFF_FIELDS
        or any(not isinstance(scope[key], str) or not SHA.fullmatch(scope[key])
               for key in HANDOFF_FIELDS if key.endswith("_sha256"))
        or scope["predecessor_plan_sha256"] != plan.sha256
        or scope["predecessor_window_id"] != plan.value["window_id"]
        or scope["identity_sha256"] != plan.value["identity_sha256"]
        or scope["protocol_epoch"] != plan.value["protocol_epoch"]
        or type(scope["privacy_epoch"]) is not int or scope["privacy_epoch"] < 0
        or not isinstance(successor, str) or not SHA.fullmatch(successor)
        or successor == plan.sha256
        or requested["kind"] != "HANDOFF_REQUESTED"
        or requested["previous"] != scope["bridge_journal_head_sha256"]
        or requested["data"] != {"successor_window_sha256": successor,
                                 "handoff_sha256": digest(scope)}
        or not isinstance(receipt, dict) or set(receipt) != fields
        or receipt["state"] != "ABORTED_BEFORE_PREPARE"
        or receipt["window_sha256"] != successor
        or receipt["predecessor_window_sha256"] != plan.sha256
        or receipt["handoff_sha256"] != digest(scope)
        or receipt["predecessor_budget_journal_head_sha256"]
        != scope["budget_journal_head_sha256"]
        or receipt["privacy_epoch"] != scope["privacy_epoch"]
        or type(receipt["privacy_epoch"]) is not int
        or receipt["erasure_ledger_sha256"] != scope["erasure_ledger_sha256"]
        or receipt["bridge_journal_head_sha256"] != digest(requested)
        or not isinstance(receipt["budget_journal_head_sha256"], str)
        or not SHA.fullmatch(receipt["budget_journal_head_sha256"])
        or receipt["budget_journal_head_sha256"] == scope["budget_journal_head_sha256"]):
        raise ValueError("WINDOW_HANDOFF_ABORT_CORE_UNCONFIRMED")


def cancelled_input_proof(row, value):
    fields = {"window_sha256", "event_id", "input_sha256", "state", "event_sha256",
              "job_sha256", "privacy_epoch", "model_attempts", "send_attempts"}
    paid_fields = {"settled_model_usage_sha256", "billed_micro_usd"}
    failure_fields = {"resolution", "failure_code", "failure_phase", "failure_evidence_sha256",
                      "reply_revision", "input_privacy_epoch"}
    failed = isinstance(value, dict) and value.get("state") == "FAILED"
    rejected = failed and value.get("failure_code") == "MONTHLY_BUDGET_EXHAUSTED"
    permitted = ((fields | failure_fields,) if rejected else
                 (fields | paid_fields | failure_fields,) if failed
                 else (fields, fields | paid_fields))
    if (not isinstance(value, dict) or set(value) not in permitted
        or value["state"] not in {"CANCELLED", "FAILED"}
        or value["window_sha256"] != row["plan_hash"] or value["event_id"] != row["event_id"]
        or value["input_sha256"] != row["input_hash"]
        or any(not isinstance(value[name], str) or not SHA.fullmatch(value[name])
               for name in ("event_sha256", "job_sha256"))
        or type(value["privacy_epoch"]) is not int or value["privacy_epoch"] < 0
        or type(value["send_attempts"]) is not int or value["send_attempts"] != 0
        or type(value["model_attempts"]) is not int or value["model_attempts"] < 0):
        raise ValueError("CANCELLED_INPUT_CORE_PROOF_REQUIRED")
    if failed and (
        (value["resolution"], value["failure_code"], value["failure_phase"],
         value["model_attempts"]) != (
            ("REJECTED_BEFORE_DISPATCH", "MONTHLY_BUDGET_EXHAUSTED", "model_admission", 0)
            if rejected else ("SETTLED_NO_DELIVERY", "MEMORY_REVIEW_INVALID", "memory_recall", 1))
        or not isinstance(value["failure_evidence_sha256"], str)
        or not SHA.fullmatch(value["failure_evidence_sha256"])
        or type(value["reply_revision"]) is not int or value["reply_revision"] < 0
        or type(value["input_privacy_epoch"]) is not int
        or not 0 <= value["input_privacy_epoch"] <= value["privacy_epoch"]
    ):
        raise ValueError("CANCELLED_INPUT_CORE_PROOF_REQUIRED")
    if value["model_attempts"]:
        if (set(value) != fields | paid_fields | (failure_fields if failed else set())
            or not isinstance(value["settled_model_usage_sha256"], str)
            or not SHA.fullmatch(value["settled_model_usage_sha256"])
            or type(value["billed_micro_usd"]) is not int or value["billed_micro_usd"] < 0):
            raise ValueError("CANCELLED_INPUT_CORE_PROOF_REQUIRED")
    elif set(value) != fields | (failure_fields if failed else set()):
        raise ValueError("CANCELLED_INPUT_CORE_PROOF_REQUIRED")


def input_state_proof(row, value, *, terminal=False):
    if (not isinstance(value, dict)
        or set(value) not in (INPUT_STATE_FIELDS, INPUT_STATE_FIELDS | {"reason"})
        or value["window_sha256"] != row["plan_hash"] or value["event_id"] != row["event_id"]
        or value["input_sha256"] != row["input_hash"]
        or type(value["reply_revision"]) is not int or value["reply_revision"] < 0
        or value["state"] not in {"QUEUED", "RUNNING", "REPLIED", "ACK", "SUPERSEDED",
                                  "RECALLED", "FAILED"}
        or type(value["unsettled_model"]) is not bool
        or type(value["unsettled_delivery"]) is not bool
        or ("reason" in value and value["reason"] is not None
            and not isinstance(value["reason"], str))
        or (terminal and (value["state"] not in {"SUPERSEDED", "RECALLED"}
                          or value["unsettled_model"] or value["unsettled_delivery"]))):
        raise ValueError("INPUT_STATE_CORE_PROOF_REQUIRED")
    return value


def not_sent_proof(message_id, value):
    if (value != {"cancelled": True, "message_id": message_id, "state": "CANCELLED",
                  "send_attempt_retained": True}
        or value["cancelled"] is not True or value["send_attempt_retained"] is not True):
        raise ValueError("NOT_SENT_CORE_PROOF_REQUIRED")


def bridge_code_sha256():
    import companion_delivery_protocol
    import companion_meme_protocol
    import companion_meme_sources

    package = Path(__file__).parent
    protocol = Path(companion_meme_protocol.__file__).parent
    delivery = Path(companion_delivery_protocol.__file__).parent
    sources = Path(companion_meme_sources.__file__).parent
    return digest([[p.name, hashlib.sha256(p.read_bytes()).hexdigest()]
                   for p in sorted(package.glob("*.py"))]
                  + [["companion_meme_protocol/" + p.relative_to(protocol).as_posix(),
                      hashlib.sha256(p.read_bytes()).hexdigest()]
                     for p in sorted(protocol.rglob("*.py"))]
                  + [["companion_delivery_protocol/" + p.relative_to(delivery).as_posix(),
                      hashlib.sha256(p.read_bytes()).hexdigest()]
                     for p in sorted(delivery.rglob("*.py"))]
                  + [["companion_meme_sources/" + p.relative_to(sources).as_posix(),
                      hashlib.sha256(p.read_bytes()).hexdigest()]
                     for p in sorted(sources.rglob("*.py"))])


def private_json(path, max_bytes=65536):
    target = Path(path)
    info = target.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or target.is_symlink()
        or (os.name != "nt" and info.st_mode & 0o077)
        or info.st_size > max_bytes
    ):
        raise ValueError("PRIVATE_WINDOW_FILE_REQUIRED")
    return decode(target.read_bytes(), max_bytes)


def stop_path(directory, value, plan_hash):
    return Path(directory) / (
        f"window-{plan_hash}.stop" if value.get("live_text", {}).get("handoff") else "window.stop"
    )


def request_stop(directory, *, plan=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir() or (
        os.name != "nt" and directory.stat().st_mode & 0o077
    ):
        raise ValueError("PRIVATE_DATA_DIRECTORY_REQUIRED")
    if plan is None:
        # A WAL reader does not need the receiver's OS-held Spool lock.
        database = directory / "spool.sqlite3"
        if database.is_symlink() or not database.is_file():
            raise ValueError("WINDOW_NOT_FOUND")
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            row = db.execute(
                "SELECT plan_hash,plan_json FROM test_window WHERE singleton=1"
            ).fetchone()
        if not row or digest(value := json.loads(row[1])) != row[0]:
            raise ValueError("WINDOW_PLAN_MISMATCH")
        plan_hash = row[0]
    else:
        value, plan_hash = plan.value, plan.sha256
    path = stop_path(directory, value, plan_hash)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != b"OWNER_STOP\n":
            raise ValueError("INVALID_WINDOW_STOP_REQUEST") from None
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(b"OWNER_STOP\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def instant(value):
    if not isinstance(value, str):
        raise ValueError("ABSOLUTE_SHANGHAI_TIME_REQUIRED")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(hours=8):
        raise ValueError("ABSOLUTE_SHANGHAI_TIME_REQUIRED")
    return parsed


def counts(rows):
    if not isinstance(rows, list):
        raise ValueError("INVALID_CORE_COUNTS")
    result = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"state", "count"}
            or not isinstance(row["state"], str)
            or row["state"] in result
            or type(row["count"]) is not int
            or row["count"] < 0
        ):
            raise ValueError("INVALID_CORE_COUNTS")
        result[row["state"]] = row["count"]
    return result


def stored_plan(encoded, config):
    """Read retired authority without re-authorizing its expired configuration."""
    plan = object.__new__(WindowPlan)
    plan.value, plan.config = json.loads(encoded), config
    plan.sha256 = digest(plan.value)
    plan.mode = plan.value.get("mode", "demo")
    plan.journal = Path(plan.value["journal_path"])
    plan.start, plan.end = instant(plan.value["starts_at"]), instant(plan.value["ends_at"])
    return plan


def read_status(config):
    """Inspect an active receiver without taking its exclusive Spool lock."""
    directory = Path(config.data_dir)
    database = directory / "spool.sqlite3"
    if (directory.is_symlink() or database.is_symlink() or not database.is_file()
        or (os.name != "nt" and directory.stat().st_mode & 0o077)):
        raise ValueError("PRIVATE_DATA_DIRECTORY_REQUIRED")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT plan_json FROM test_window WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("WINDOW_NOT_FOUND")
        plan = stored_plan(row[0], config)
        return TestWindow(SimpleNamespace(db=db, directory=directory), plan).status()


class WindowPlan:
    def __init__(self, value, config, inventory, authorization=None):
        fields = {
            "window_id",
            "timezone",
            "starts_at",
            "ends_at",
            "protocol_epoch",
            "identity_sha256",
            "config_sha256",
            "config_version",
            "image_ids",
            "runtime_attestation_sha256",
            "authorization_sha256",
            "journal_path",
            "max_new_inputs",
            "max_attempts",
            "max_visible_replies",
            "concurrency",
            "max_input_chars",
            "max_reply_chars",
            "execution",
        }
        if not isinstance(value, dict):
            raise ValueError("INVALID_WINDOW_PLAN")
        self.mode = value.get("mode", "demo")
        if self.mode == "live_text":
            fields |= {"mode", "live_text"}
        elif self.mode != "demo":
            raise ValueError("INVALID_WINDOW_MODE")
        if set(value) != fields:
            raise ValueError("INVALID_WINDOW_PLAN")
        self.value, self.config = value, config
        self.sha256 = digest(value)
        if not re.fullmatch(r"win_[a-f0-9]{32}", value["window_id"]):
            raise ValueError("INVALID_WINDOW_ID")
        if value["timezone"] != "Asia/Shanghai":
            raise ValueError("ABSOLUTE_SHANGHAI_TIME_REQUIRED")
        self.start, self.end = instant(value["starts_at"]), instant(value["ends_at"])
        if not self.start < self.end:
            raise ValueError("INVALID_WINDOW_INTERVAL")
        if self.mode == "live_text":
            scope = value["live_text"]
            expected_scope = {
                "live_approved",
                "execution",
                "window_id",
                "starts_at",
                "ends_at",
                "core_config_sha256",
                "core_code_sha256",
                "bridge_code_sha256",
                "candidate_sha",
                "paid_contract_sha256",
                "budget_journal_id",
                "custodian_sha256",
                "approval_sha256",
                "max_inputs",
                "max_model_attempts",
                "max_input_chars",
                "max_reply_chars",
                "capture_public_key",
                "history_scope",
                "max_history_turns",
                "max_history_utf8_bytes",
                "memory_scope",
                "memory_facts",
                "memory_approval_sha256",
                "handoff",
            }
            if not isinstance(scope, dict) or scope.get("live_approved") is not True:
                raise ValueError("LIVE_TEXT_APPROVAL_REQUIRED")
            companion = scope.get("companion_scope") == "owner_private_v1"
            if "companion_scope" in scope or "companion_approval_sha256" in scope:
                expected_scope |= {"companion_scope", "companion_approval_sha256"}
                if (not companion or scope.get("automatic_reply") is not True
                    or scope.get("history_scope") != "owner_private"
                    or scope.get("memory_scope") != "owner_private"
                    or not scope.get("history_approval_sha256")
                    or not scope.get("memory_approval_sha256") or scope.get("memory_facts")):
                    raise ValueError("EXPLICIT_PRIVATE_COMPANION_SCOPE_REQUIRED")
            if "cumulative" in scope:
                expected_scope.add("cumulative")
                cumulative.validate(scope["cumulative"], config, self.start, self.end)
            if "life_shadow" in scope:
                # Core owns this no-delivery extension. Carry it in the frozen window
                # identity without interpreting it as Bridge send authority.
                shadow = scope["life_shadow"]
                if (not companion or not isinstance(shadow, dict)
                    or shadow.get("scope") != "owner_private_life_shadow_v1"):
                    raise ValueError("INVALID_LIFE_SHADOW_SCOPE")
                expected_scope.add("life_shadow")
            if "automatic_reply" in scope:
                expected_scope.add("automatic_reply")
                if (scope["automatic_reply"] is not True or not scope["capture_public_key"]
                    or self.end - self.start > timedelta(hours=72)):
                    raise ValueError("INVALID_AUTOMATIC_REPLY_SCOPE")
            if "segmented_reply" in scope or "max_send_attempts" in scope:
                if "segmented_reply" in scope:
                    reply_segments.validate_scope(scope, value)
                    expected_scope.add("segmented_reply")
                elif (
                    type(scope.get("max_send_attempts")) is not int
                    or not 1 <= scope["max_send_attempts"] <= 2**31 - 1
                    or any(type(value[key]) is not int
                           for key in ("max_attempts", "max_visible_replies"))
                    or scope["max_send_attempts"] != min(
                        value["max_attempts"], value["max_visible_replies"],
                    )
                ):
                    raise ValueError("INVALID_WINDOW_SEND_LIMIT")
                expected_scope.add("max_send_attempts")
            # Retired plain scopes retain their original bytes/hash; new scopes may opt in.
            if "rich_input_scope" in scope:
                expected_scope.add("rich_input_scope")
            if scope.get("rich_input_scope", "none") not in ("none", "references_v1"):
                raise ValueError("INVALID_RICH_INPUT_SCOPE")
            if "link_scope" in scope:
                expected_scope.add("link_scope")
            if (
                scope.get("link_scope", "none") not in {"none", "owner_links_v1"}
                or (scope.get("link_scope") == "owner_links_v1" and (
                    scope.get("rich_input_scope") != "references_v1"
                    or not scope["capture_public_key"]
                ))
            ):
                raise ValueError("INVALID_LINK_SCOPE")
            for field in ("image_scope", "image_approval_sha256"):
                if field in scope:
                    expected_scope.add(field)
            if (
                scope.get("image_scope", "none") not in ("none", "owner_image_v1")
                or (
                    scope.get("image_scope", "none") == "none"
                    and scope.get("image_approval_sha256") is not None
                )
                or (
                    scope.get("image_scope") == "owner_image_v1"
                    and (
                        scope.get("rich_input_scope") != "references_v1"
                        or (scope["history_scope"] != "none" and not companion)
                        or not isinstance(scope.get("image_approval_sha256"), str)
                        or not SHA.fullmatch(scope["image_approval_sha256"])
                        or not scope["capture_public_key"]
                    )
                )
            ):
                raise ValueError("INVALID_IMAGE_SCOPE")
            if "file_scope" in scope:
                expected_scope.add("file_scope")
            if (scope.get("file_scope", "none") not in ("none", "owner_file_v1")
                or (scope.get("file_scope") == "owner_file_v1" and (
                    not companion or scope.get("rich_input_scope") != "references_v1"
                    or not scope["capture_public_key"]))):
                raise ValueError("INVALID_FILE_SCOPE")
            if "poke_scope" in scope or "poke_policy_sha256" in scope:
                expected_scope |= {"poke_scope", "poke_policy_sha256"}
                if (scope.get("poke_scope") != "owner_poke_v1" or not companion
                    or scope.get("automatic_reply") is not True or not scope["capture_public_key"]):
                    raise ValueError("INVALID_POKE_SCOPE")
            for field, enabled, proof in (
                ("voice_scope", "owner_voice_v1", "voice_policy_sha256"),
                ("account_actions_scope", "qq_social_v1",
                 "account_actions_authorization_sha256"),
                ("bili_video_scope", "bounded_video_media_v1", "bili_video_policy_sha256"),
            ):
                expected_scope.update(key for key in (field, proof) if key in scope)
                if (scope.get(field, "none") not in {"none", enabled}
                    or (scope.get(field, "none") == "none" and scope.get(proof) is not None)
                    or (scope.get(field) == enabled and (
                        not companion or not scope["capture_public_key"]
                        or not isinstance(scope.get(proof), str)
                        or not SHA.fullmatch(scope[proof])))):
                    raise ValueError("INVALID_" + field.upper())
            if (scope.get("bili_video_scope", "none") != "none"
                and scope.get("link_scope") != "owner_links_v1"):
                raise ValueError("INVALID_BILI_VIDEO_SCOPE")
            # Old history-only authority remains byte exact; no new grant is inferred.
            if set(scope) - {"history_approval_sha256"} != expected_scope or any(
                not isinstance(scope[k], str) or not SHA.fullmatch(scope[k])
                for k in expected_scope
                if k.endswith("_sha256")
                and k not in {"memory_approval_sha256", "image_approval_sha256"}
                and not (k in {"voice_policy_sha256", "account_actions_authorization_sha256",
                               "bili_video_policy_sha256"}
                         and scope[k] is None)
            ):
                raise ValueError("INVALID_LIVE_TEXT_SCOPE")
            facts = scope["memory_facts"]
            if (
                scope["memory_scope"] not in ({"none", "approved_facts", "owner_private"}
                                               if companion else {"none", "approved_facts"})
                or not isinstance(facts, list)
                or len(facts) > 6
                or (
                    scope["memory_scope"] == "none"
                    and (facts or scope["memory_approval_sha256"] is not None)
                )
                or (
                    scope["memory_scope"] == "approved_facts"
                    and (
                        not facts
                        or not isinstance(scope["memory_approval_sha256"], str)
                        or not SHA.fullmatch(scope["memory_approval_sha256"])
                        or not scope["capture_public_key"]
                    )
                )
                or any(
                    not isinstance(fact, dict)
                    or set(fact)
                    != {
                        "memory_id",
                        "subject",
                        "status",
                        "content_sha256",
                        "evidence_sha256",
                        "memory_sha256",
                    }
                    or not isinstance(fact["memory_id"], str)
                    or not re.fullmatch(r"mem_[a-f0-9]{32}", fact["memory_id"])
                    or fact["subject"] not in {"user", "project"}
                    or fact["status"] != "CONFIRMED"
                    or any(
                        not isinstance(fact[k], str) or not SHA.fullmatch(fact[k])
                        for k in ("content_sha256", "evidence_sha256", "memory_sha256")
                    )
                    for fact in facts
                )
                or len({fact["memory_id"] for fact in facts}) != len(facts)
            ):
                raise ValueError("INVALID_READONLY_MEMORY_SCOPE")
            history_approval = scope.get("history_approval_sha256")
            if (
                history_approval is not None
                and (
                    not isinstance(history_approval, str)
                    or not SHA.fullmatch(history_approval)
                    or scope["history_scope"] == "none"
                )
            ) or (
                scope["history_scope"] == "approved_window"
                and scope["memory_scope"] == "approved_facts"
                and not history_approval
            ):
                raise ValueError("SEPARATE_HISTORY_APPROVAL_REQUIRED")
            handoff = scope["handoff"]
            if handoff is not None and (
                not isinstance(handoff, dict)
                or set(handoff)
                != {
                    "predecessor_plan_sha256",
                    "predecessor_window_id",
                    "budget_journal_head_sha256",
                    "bridge_journal_head_sha256",
                    "identity_sha256",
                    "protocol_epoch",
                    "privacy_epoch",
                    "erasure_ledger_sha256",
                    "evidence_sha256",
                }
                or any(
                    not isinstance(handoff[key], str) or not SHA.fullmatch(handoff[key])
                    for key in handoff
                    if key.endswith("_sha256")
                )
                or not re.fullmatch(r"win_[a-f0-9]{32}", handoff["predecessor_window_id"])
                or type(handoff["privacy_epoch"]) is not int
                or handoff["privacy_epoch"] < 0
                or handoff["identity_sha256"] != value["identity_sha256"]
                or handoff["protocol_epoch"] != value["protocol_epoch"]
                or not config.capture_signing_key
            ):
                raise ValueError("INVALID_WINDOW_HANDOFF")
            if (
                scope["history_scope"] not in (("none", "approved_window", "owner_private")
                                                if companion else ("none", "approved_window"))
                or type(scope["max_history_turns"]) is not int
                or type(scope["max_history_utf8_bytes"]) is not int
                or not 0 <= scope["max_history_turns"] <= 15
                or not 0 <= scope["max_history_utf8_bytes"] <= 16000
                or (
                    scope["history_scope"] == "none"
                    and (scope["max_history_turns"] or scope["max_history_utf8_bytes"])
                )
                or (
                    scope["history_scope"] != "none"
                    and not (
                        scope["max_history_turns"]
                        and scope["max_history_utf8_bytes"]
                        and scope["capture_public_key"]
                    )
                )
                or scope["bridge_code_sha256"] != bridge_code_sha256()
                or scope["capture_public_key"] != config.capture_public_key
                or scope["execution"] != value["execution"]
                or scope["window_id"] != value["window_id"]
                or instant(scope["starts_at"]) != self.start
                or instant(scope["ends_at"]) != self.end
                or not re.fullmatch(r"[a-f0-9]{40}", scope["candidate_sha"])
                or not re.fullmatch(r"budget_[a-f0-9]{32}", scope["budget_journal_id"])
                or type(scope["max_model_attempts"]) is not int
                or not 1 <= scope["max_model_attempts"] <= 2**31 - 1
                or any(
                    type(scope[key]) is not int or scope[key] != value[other]
                    for key, other in (
                        ("max_inputs", "max_new_inputs"),
                        ("max_input_chars", "max_input_chars"),
                        ("max_reply_chars", "max_reply_chars"),
                    )
                )
            ):
                raise ValueError("LIVE_TEXT_SCOPE_MISMATCH")
        for field in ("max_new_inputs", "max_attempts", "max_visible_replies"):
            if type(value[field]) is not int or not 1 <= value[field] <= 2**31 - 1:
                raise ValueError("INVALID_WINDOW_LIMIT")
        if type(value["concurrency"]) is not int or value["concurrency"] != 1:
            raise ValueError("INVALID_WINDOW_LIMIT")
        for field in ("max_input_chars", "max_reply_chars"):
            if type(value[field]) is not int or not 1 <= value[field] <= 20000:
                raise ValueError("INVALID_WINDOW_TEXT_LIMIT")
        for field in (
            "identity_sha256",
            "config_sha256",
            "runtime_attestation_sha256",
            "authorization_sha256",
        ):
            if not isinstance(value[field], str) or not SHA.fullmatch(value[field]):
                raise ValueError("INVALID_WINDOW_DIGEST")
        images = value["image_ids"]
        if (
            not isinstance(images, dict)
            or set(images) != {"core", "bridge", "protocol"}
            or any(
                not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
                for image in images.values()
            )
        ):
            raise ValueError("PINNED_RUNTIME_IMAGES_REQUIRED")
        if not isinstance(value["config_version"], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", value["config_version"]
        ):
            raise ValueError("INVALID_CONFIG_VERSION")
        if (
            config.delivery_enabled
            or value["protocol_epoch"] != config.protocol_epoch
            or value["identity_sha256"] != digest([config.owner_qq_id, config.bot_qq_id])
            or not config_matches(config, value["config_sha256"])
        ):
            raise ValueError("WINDOW_CONFIG_MISMATCH")
        expected = {
            key: value[key]
            for key in (
                "identity_sha256",
                "protocol_epoch",
                "config_sha256",
                "config_version",
                "image_ids",
            )
        }
        if inventory != expected or digest(inventory) != value["runtime_attestation_sha256"]:
            raise ValueError("RUNTIME_ATTESTATION_MISMATCH")
        journal = Path(value["journal_path"])
        if (
            not journal.is_absolute()
            or journal.resolve().is_relative_to(Path(config.data_dir).resolve())
            or journal.resolve() != journal.absolute()
            or not journal.parent.is_dir()
            or journal.parent.is_symlink()
            or (os.name != "nt" and journal.parent.stat().st_mode & 0o077)
        ):
            raise ValueError("INDEPENDENT_WINDOW_JOURNAL_REQUIRED")
        self.journal = journal
        if value["execution"] == "local_synthetic":
            if any(
                urlsplit(url).hostname not in {"127.0.0.1", "::1", "localhost"}
                for url in (config.core_url, config.onebot_http_url, config.onebot_ws_url)
            ):
                raise ValueError("SYNTHETIC_LOOPBACK_REQUIRED")
        elif value["execution"] == "approved_live":
            if config.capture_signing_key is None:
                raise ValueError("INDEPENDENT_CAPTURE_SIGNING_KEY_REQUIRED")
            scope = {key: val for key, val in value.items() if key != "authorization_sha256"}
            if (
                not isinstance(authorization, dict)
                or set(authorization) != {"kind", "approved", "scope_sha256", "evidence_sha256"}
                or authorization["kind"] != "owner_qq_window"
                or authorization["approved"] is not True
                or authorization["scope_sha256"] != digest(scope)
                or not isinstance(authorization["evidence_sha256"], str)
                or not SHA.fullmatch(authorization["evidence_sha256"])
                or digest(authorization) != value["authorization_sha256"]
            ):
                raise ValueError("NEW_WINDOW_AUTHORIZATION_REQUIRED")
        else:
            raise ValueError("INVALID_WINDOW_EXECUTION_MODE")


class TestWindow:
    """Exclusive Spool owner. Every irreversible allowance is journaled before I/O."""

    __test__ = False

    def __init__(self, spool, plan, *, clock=None):
        self.spool, self.db, self.plan = spool, spool.db, plan
        self.db.row_factory = sqlite3.Row
        self.clock = clock or (lambda: datetime.now(UTC))
        self.busy = False
        self._memes = None
        self.model_attempts = None
        self.core_stop_ack = False
        self.stop_path = stop_path(spool.directory, plan.value, plan.sha256)
        # Revocation must survive damage to the mutable plan, Spool or journal.
        self._stop_identity = (plan.mode, plan.sha256, plan.config,
                               plan.value["execution"] == "local_synthetic")

    @property
    def memes(self):
        if self._memes is None:
            from .meme_runtime import MemeRuntime

            self._memes = MemeRuntime(self)
        return self._memes

    def row(self):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute("SELECT * FROM test_window WHERE singleton=1").fetchone()
        if not row or row["plan_hash"] != self.plan.sha256:
            raise ValueError("WINDOW_IMMUTABLE_OR_MISSING")
        return row

    def check_schema(self):
        """Runtime verification never assigns ownership or repairs a persisted schema."""
        required = {
            "test_window": "singleton,plan_hash,plan_json,journal_head,state,fence,"
                           "initial_counts,initial_inbox_rowid,reason",
            "window_inputs": "event_id,input_hash,approval_hash,state,capture_json,plan_hash",
            "window_attempts": "message_id,event_id,fence,state,text_hash,approval_hash,"
                               "provider_ack_hash,item_hash,plan_hash",
            "window_history": "plan_hash,record_json,retirement_head,successor_hash",
        }
        try:
            for table, columns in required.items():
                self.db.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_ERROR:
                raise
            raise ValueError("WINDOW_SCHEMA_PREPARATION_REQUIRED") from error

    def prepare_schema(self):
        """Explicit preparation removes the old input uniqueness, preserving every receipt."""
        self.check_schema()
        unique_event = False
        unexpected_index = False
        indexes = self.db.execute("PRAGMA index_list(window_attempts)").fetchall()
        for index in indexes:
            columns = [r[2] for r in self.db.execute(
                "SELECT * FROM pragma_index_info(?)", (index[1],)
            )]
            if index[2] and index[3] == "u" and columns == ["event_id"]:
                unique_event = True
            elif not (index[2] and index[3] == "pk" and columns == ["message_id"]):
                unexpected_index = True
        if not unique_event:
            return
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(window_attempts)")]
        expected = ["message_id", "event_id", "fence", "state", "text_hash",
                    "approval_hash", "provider_ack_hash", "item_hash", "plan_hash"]
        if (columns != expected
            or self.db.execute("SELECT COUNT(*) FROM test_window").fetchone()[0] != 1
            or unexpected_index
            or self.db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                               "AND tbl_name='window_attempts'").fetchone()):
            raise ValueError("WINDOW_SCHEMA_AMBIGUOUS")
        self.db.execute("SAVEPOINT window_preparation")
        try:
            self.db.execute(
                "CREATE TABLE window_attempts_segments(message_id TEXT PRIMARY KEY,"
                "event_id TEXT NOT NULL,fence INTEGER NOT NULL,state TEXT NOT NULL,"
                "text_hash TEXT NOT NULL,approval_hash TEXT NOT NULL,provider_ack_hash TEXT,"
                "item_hash TEXT NOT NULL,plan_hash TEXT NOT NULL)"
            )
            names = ",".join(expected)
            self.db.execute(
                f"INSERT INTO window_attempts_segments(rowid,{names}) "
                f"SELECT rowid,{names} FROM window_attempts"
            )
            self.db.execute("DROP TABLE window_attempts")
            self.db.execute("ALTER TABLE window_attempts_segments RENAME TO window_attempts")
        except BaseException:
            self.db.execute("ROLLBACK TO window_preparation")
            raise
        finally:
            self.db.execute("RELEASE window_preparation")

    def count(self, table, condition="1=1"):
        if table not in {"window_inputs", "window_attempts"}:
            raise ValueError("INVALID_WINDOW_TABLE")
        return self.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE plan_hash=? AND ({condition})",
            (self.plan.sha256,),
        ).fetchone()[0]

    def verify_history(self, records):
        plan, journal = self.plan, records
        history = [(plan, journal)]
        seen = set()
        while scope := plan.value.get("live_text", {}).get("handoff"):
            predecessor = scope["predecessor_plan_sha256"]
            if predecessor in seen:
                raise ValueError("WINDOW_HISTORY_MISMATCH")
            seen.add(predecessor)
            saved = self.db.execute(
                "SELECT * FROM window_history WHERE plan_hash=?", (predecessor,)
            ).fetchone()
            if not saved or saved["successor_hash"] != plan.sha256:
                raise ValueError("WINDOW_HISTORY_MISMATCH")
            snapshot = json.loads(saved["record_json"])
            old = stored_plan(snapshot["plan_json"], self.plan.config)
            old_records, old_head = TestWindow(self.spool, old, clock=self.clock).read_journal()
            if (
                old.sha256 != predecessor or old_head != saved["retirement_head"]
                or not old_records or old_records[-1]["kind"] != "RETIRED"
                or old_records[-1]["data"].get("snapshot_sha256") != digest(snapshot)
                or old_records[-1]["data"].get("successor_window_sha256") != plan.sha256
                or not journal or journal[0]["kind"] != "HANDOFF_PREPARED"
                or journal[0]["data"].get("retirement_head") != old_head
                or journal[0]["data"].get("predecessor_window_sha256") != predecessor
                or journal[0]["data"].get("handoff_sha256") != digest(scope)
            ):
                raise ValueError("WINDOW_HISTORY_MISMATCH")
            marker = stop_path(self.spool.directory, old.value, old.sha256)
            if (marker.is_symlink() or not marker.is_file()
                    or marker.read_bytes() != b"OWNER_STOP\n"):
                raise ValueError("WINDOW_STOP_EVIDENCE_MISSING")
            plan, journal = old, old_records
            history.append((plan, journal))
        return history

    def verify(self, *, active=True, dispatch=False, capture_fence=None):
        if (active or dispatch) and self.spool.reconciliation_only:
            raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
        if digest(self.plan.value) != self.plan.sha256:
            raise ValueError("WINDOW_IMMUTABLE_OR_MISSING")
        if (active or dispatch) and (
            not config_matches(self.plan.config, self.plan.value["config_sha256"])
            or (self.plan.mode == "live_text" and (
                self.plan.value["live_text"]["bridge_code_sha256"] != bridge_code_sha256()
            ))
        ):
            raise ValueError("LIVE_TEXT_RUNTIME_CHANGED")
        if (active or dispatch) and self.stop_path.exists():
            raise ValueError("WINDOW_STOPPED")
        self.check_schema()
        row = self.row()
        records, head = self.read_journal()
        if not records or head != row["journal_head"]:
            raise ValueError("WINDOW_JOURNAL_RECONCILIATION_REQUIRED")
        history = self.verify_history(records)
        self.check_handoff_aborts(history)
        self.check_cancelled_inputs(history)
        self.check_terminal_inputs(history)
        self.check_recalls(history)
        from .pokes import check_history

        check_history(self, history)
        from .pending_media import verify_history

        verify_history(self, history)
        from .owner_resolution import check_history as check_resolutions

        check_resolutions(self, history)
        quota = cumulative.fold(self, history)
        if active or dispatch:
            cumulative.check(self, quota, self.clock())
        if active:
            capturing = (capture_fence is not None and row["state"] == "CAPTURING"
                         and row["fence"] == capture_fence)
            if row["state"] == "CAPTURING" and not capturing:
                raise ValueError("CAPTURE_RECONCILIATION_REQUIRED")
            if row["state"] != "OPEN" and not capturing:
                raise ValueError("WINDOW_STOPPED")
            unknown = self.count("window_attempts", "state NOT IN ('ACK','NOT_SENT')")
            if unknown:
                raise ValueError("WINDOW_ATTEMPT_RECONCILIATION_REQUIRED")
            if not self.plan.start <= self.clock() < self.plan.end:
                raise ValueError("WINDOW_OUTSIDE_INTERVAL")
        return row

    def check_handoff_aborts(self, history):
        for plan, records in history:
            seen = set()
            for index, record in enumerate(records):
                if record["kind"] != "HANDOFF_ABORTED":
                    continue
                data = record["data"]
                if (not index or not isinstance(data, dict)
                    or set(data) != {"successor_window_sha256", "handoff", "core_receipt"}
                    or data["successor_window_sha256"] in seen):
                    raise ValueError("WINDOW_HANDOFF_ABORT_JOURNAL_REQUIRED")
                handoff_abort_proof(plan, data["successor_window_sha256"], data["handoff"],
                                    records[index - 1], data["core_receipt"])
                seen.add(data["successor_window_sha256"])

    def check_cancelled_inputs(self, history):
        journals = {plan.sha256: records for plan, records in history}
        for row in self.db.execute("SELECT * FROM window_inputs WHERE state='CANCELLED'"):
            entries = [entry for entry in journals.get(row["plan_hash"], [])
                       if entry["kind"] == "INPUT_CANCELLED"
                       and entry["data"].get("event_hash") == digest(row["event_id"])]
            if len(entries) != 1:
                raise ValueError("CANCELLED_INPUT_JOURNAL_REQUIRED")
            cancelled_input_proof(row, entries[0]["data"].get("core_receipt"))

    def check_terminal_inputs(self, history):
        journals = {plan.sha256: records for plan, records in history}
        for row in self.db.execute("SELECT * FROM window_inputs "
                                    "WHERE state IN ('SUPERSEDED','RECALLED')"):
            entries = [entry for entry in journals.get(row["plan_hash"], [])
                       if entry["kind"] == "INPUT_" + row["state"]
                       and entry["data"].get("event_hash") == digest(row["event_id"])]
            if len(entries) != 1:
                raise ValueError("TERMINAL_INPUT_JOURNAL_REQUIRED")
            proof = input_state_proof(row, entries[0]["data"].get("core_receipt"), terminal=True)
            if proof["state"] != row["state"]:
                raise ValueError("INPUT_STATE_CORE_PROOF_REQUIRED")
        for row in self.db.execute("SELECT * FROM window_attempts WHERE state='NOT_SENT'"):
            entries = [entry for entry in journals.get(row["plan_hash"], [])
                       if entry["kind"] == "NOT_SENT"
                       and entry["data"].get("fence") == row["fence"]]
            if len(entries) != 1:
                raise ValueError("NOT_SENT_JOURNAL_REQUIRED")
            result = self.db.execute("SELECT * FROM results WHERE message_id=?",
                                      (row["message_id"],)).fetchone()
            body = json.loads(result["body"]) if result else {}
            proof = entries[0]["data"].get("core_receipt")
            if re.fullmatch(r"social_[a-f0-9]{32}", row["event_id"]):
                account_actions.cancelled_proof(proof, message_id=row["message_id"],
                    lease_token=body.get("lease_token"), content_sha256=row["text_hash"])
            else:
                not_sent_proof(row["message_id"], proof)
            if (not result or result["state"] != "accepted" or body.get("result") != "NOT_SENT"
                or body.get("core_receipt") != entries[0]["data"]["core_receipt"]):
                raise ValueError("NOT_SENT_RESULT_REQUIRED")
        if self.db.execute("SELECT 1 FROM results r WHERE json_extract(r.body,'$.result')="
                           "'NOT_SENT' AND NOT EXISTS (SELECT 1 FROM window_attempts a "
                           "WHERE a.message_id=r.message_id AND a.state='NOT_SENT')").fetchone():
            raise ValueError("NOT_SENT_RESULT_REQUIRED")

    async def input_state(self, client, row):
        value = await self.core_get(client, "/internal/v1/live-text/inputs/" + row["event_id"]
                                    + "/state")
        return input_state_proof(row, value)

    def check_recalls(self, history):
        received, accepted = {}, {}
        for plan, entries in history:
            for entry in entries:
                if entry["kind"] in {"RECALL_RECEIVED", "RECALL_ACCEPTED"}:
                    target = received if entry["kind"] == "RECALL_RECEIVED" else accepted
                    key = entry["data"].get("recall_id")
                    if key in target:
                        raise ValueError("RECALL_JOURNAL_RECONCILIATION_REQUIRED")
                    target[key] = (plan.sha256, entry["data"])
        exists = self.db.execute("SELECT 1 FROM sqlite_master "
                                  "WHERE name='automatic_recalls'").fetchone()
        rows = self.db.execute("SELECT * FROM automatic_recalls").fetchall() if exists else []
        if set(received) != {row["recall_id"] for row in rows} or set(accepted) - set(received):
            raise ValueError("RECALL_JOURNAL_RECONCILIATION_REQUIRED")
        for row in rows:
            plan_hash, data = received[row["recall_id"]]
            receipt = json.loads(row["record_json"])
            if (plan_hash != row["window_sha256"]
                or data.get("receipt_sha256") != digest(receipt)
                or receipt.get("recall_id") != row["recall_id"]
                or receipt.get("target_event_id") != row["target_event_id"]):
                raise ValueError("RECALL_JOURNAL_RECONCILIATION_REQUIRED")
            prior = accepted.get(row["recall_id"])
            if row["state"] == "ACCEPTED":
                if (not prior or prior[0] != plan_hash or not row["response_json"]
                    or prior[1].get("core_receipt") != json.loads(row["response_json"])):
                    raise ValueError("RECALL_JOURNAL_RECONCILIATION_REQUIRED")
            elif row["state"] != "PENDING" or prior or row["response_json"] is not None:
                raise ValueError("RECALL_JOURNAL_RECONCILIATION_REQUIRED")

    def retire_input(self, row, value):
        input_state_proof(row, value, terminal=True)
        if self.db.execute("SELECT 1 FROM window_attempts WHERE event_id=? "
                           "AND state NOT IN ('ACK','NOT_SENT')", (row["event_id"],)).fetchone():
            raise ValueError("INPUT_ATTEMPT_RECONCILIATION_REQUIRED")
        current = self.db.execute("SELECT state FROM window_inputs WHERE event_id=?",
                                   (row["event_id"],)).fetchone()
        if current["state"] == value["state"]:
            return
        if current["state"] != "APPROVED":
            raise ValueError("INPUT_STATE_CORE_PROOF_REQUIRED")
        head = self.append("INPUT_" + value["state"], {"event_hash": digest(row["event_id"]),
                                                       "core_receipt": value})
        with self.db:
            self.db.execute("UPDATE window_inputs SET state=? WHERE event_id=?",
                            (value["state"], row["event_id"]))
            self.db.execute("UPDATE test_window SET journal_head=?", (head,))

    async def reconcile_cancelled_inputs(self, client):
        """Retire inputs with no send; settled model attempts remain charged and counted."""
        row = self.verify(active=False)
        records, _ = self.read_journal()
        if (row["state"] != "STOPPED" or self.plan.mode != "live_text"
            or not any(entry["kind"] == "CORE_STOP_CONFIRMED" for entry in records)
            or not self.stop_path.is_file() or self.stop_path.is_symlink()
            or self.stop_path.read_bytes() != b"OWNER_STOP\n"):
            raise ValueError("STOPPED_INPUT_RECONCILIATION_REQUIRED")
        changed = 0
        for source in self.db.execute("SELECT * FROM window_inputs WHERE plan_hash=? "
                                      "AND state NOT IN "
                                      "('ACK','CANCELLED','SUPERSEDED','RECALLED')",
                                      (self.plan.sha256,)).fetchall():
            if (source["state"] != "APPROVED" or not source["capture_json"]
                or self.db.execute("SELECT 1 FROM window_attempts WHERE event_id=?",
                                   (source["event_id"],)).fetchone()):
                raise ValueError("INPUT_ATTEMPT_RECONCILIATION_REQUIRED")
            before = self.row()["journal_head"]
            result = await self.core_post(client, "/internal/v1/live-text/inputs/cancelled", {
                "window_sha256": self.plan.sha256, "event_id": source["event_id"],
            })
            cancelled_input_proof(source, result)
            current = self.verify(active=False)
            if current["state"] != "STOPPED" or current["journal_head"] != before:
                raise ValueError("WINDOW_JOURNAL_RECONCILIATION_REQUIRED")
            head = self.append("INPUT_CANCELLED", {"event_hash": digest(source["event_id"]),
                                                   "core_receipt": result})
            with self.db:
                self.db.execute("UPDATE window_inputs SET state='CANCELLED' WHERE event_id=?",
                                (source["event_id"],))
                self.db.execute("UPDATE test_window SET journal_head=?", (head,))
            changed += 1
        return self.status() | {"cancelled_inputs": changed}

    def read_journal(self):
        from . import journal

        maximum = (32 * 1048576
                   if self.plan.value.get("live_text", {}).get("automatic_reply") else 1048576)
        return journal.read(self.plan.journal, maximum=maximum)

    def append(self, kind, data, *, create=False):
        from . import journal

        if not create and kind in {"CAPTURED", "INPUT", "RESERVED"}:
            cumulative.admit(self, "send_attempts" if kind == "RESERVED" else "owner_inputs",
                             data["message_hash"] if kind == "RESERVED" else data["event_hash"])
        maximum = (32 * 1048576
                   if self.plan.value.get("live_text", {}).get("automatic_reply") else 1048576)
        return journal.append(
            self.plan.journal, kind, data, at=self.clock().isoformat(), create=create,
            expected_head="0" * 64 if create else self.row()["journal_head"], maximum=maximum,
        )

    async def core_counts(self, client, *, initial=False):
        if self.plan.mode == "live_text":
            self.check_core_client(client)
        content = bytearray()
        async with self.core_http(client) as scoped:
            async with scoped.stream("GET", "/internal/v1/ops/status") as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise ValueError("INVALID_CORE_COUNTS")
        status = decode(bytes(content), 65536)
        deliveries, jobs = counts(status.get("deliveries")), counts(status.get("jobs"))
        readonly_memory = (
            self.plan.mode == "live_text"
            and self.plan.value["live_text"]["memory_scope"] == "approved_facts"
        )
        owner_content = (
            self.plan.mode == "live_text"
            and (
                self.plan.value["live_text"].get("image_scope") == "owner_image_v1"
                or self.plan.value["live_text"].get("link_scope") == "owner_links_v1"
                or self.plan.value["live_text"].get("file_scope") == "owner_file_v1"
            )
        )
        closed = (
            isinstance(status.get("features"), dict)
            and all(
                value is (readonly_memory if key == "memory" else owner_content if key == "content"
                          else False)
                for key, value in status["features"].items()
            )
            and (not readonly_memory or status["features"].get("memory") is True)
            and (not owner_content or status["features"].get("content") is True)
        )
        companion = (self.plan.value.get("live_text", {}).get("companion_scope")
                     == "owner_private_v1")
        if companion:
            allowed_features = {"memory", "diary", "affect", "consolidation", "reflection",
                                "goals", "wake", "content"}
            closed = (isinstance(status.get("features"), dict) and all(
                type(value) is bool and (key in allowed_features or value is False)
                for key, value in status["features"].items()))
        if self.plan.mode == "live_text":
            live = status.get("live_text_window", {})
            if (
                status.get("mode") != "live"
                or not closed
                or live.get("approved") is not True
                or live.get("plan_sha256") != self.plan.sha256
                or live.get("scope_sha256") != digest(self.plan.value["live_text"])
                or type(live.get("model_attempts")) is not int
                or not 0
                <= live["model_attempts"]
                <= self.plan.value["live_text"]["max_model_attempts"]
            ):
                raise ValueError("LIVE_TEXT_CORE_APPROVAL_REQUIRED")
            self.model_attempts = live["model_attempts"]
        elif (
            status.get("mode") != "demo"
            or not isinstance(status.get("features"), dict)
            or any(value is not False for value in status["features"].values())
        ):
            raise ValueError("DEMO_CLOSED_FEATURES_REQUIRED")
        allowed = {"SENT", "FAILED", "CANCELLED", *ACTIVE}
        if set(deliveries) - allowed:
            raise ValueError("UNKNOWN_CORE_DELIVERY_STATE")
        if any(deliveries.get(state, 0) for state in ACTIVE - {"PENDING"}):
            raise ValueError("CORE_DELIVERY_RECONCILIATION_REQUIRED")
        if initial:
            if deliveries.get("PENDING", 0) or any(
                jobs.get(state, 0) for state in {"QUEUED", "RUNNING", "RETRY_WAIT"}
            ):
                raise ValueError("INITIAL_PENDING_MUST_BE_ZERO")
        else:
            baseline = json.loads(self.row()["initial_counts"])
            acked = self.count("window_attempts", "state='ACK'")
            for state in ("SENT", "FAILED", "CANCELLED"):
                if companion and state != "SENT":
                    continue
                if deliveries.get(state, 0) != baseline.get(state, 0) + (
                    acked if state == "SENT" else 0
                ):
                    raise ValueError("CORE_DELIVERY_COUNTS_CHANGED")
            segmented = reply_segments.scope(self.plan)
            maximum = (self.plan.value["max_attempts"] if companion else
                       (self.plan.value["max_attempts"] if segmented["max_segments"] == "unlimited"
                        else segmented["max_segments"]) if segmented else 1)
            if deliveries.get("PENDING", 0) > maximum or (
                segmented and not companion and deliveries.get("PENDING", 0)
                and self.count("window_inputs", "state='APPROVED'") != 1
            ):
                raise ValueError("UNATTENDED_CORE_DELIVERY")
        return deliveries

    def check_core_client(self, client):
        if (
            str(client.base_url).rstrip("/") != self.plan.config.core_url.rstrip("/")
            or client.headers.get("Authorization") != "Bearer " + self.plan.config.bridge_token
        ):
            raise ValueError("LIVE_TEXT_CORE_ENDPOINT_CHANGED")

    async def check_core_request(self, request):
        path = request.url.path
        if request.url.query:
            expected = urlencode({"window_sha256": self.plan.sha256,
                "authorization_sha256": self.plan.value.get("live_text", {}).get(
                    "account_actions_authorization_sha256")})
            if (not account_actions.enabled(self) or request.method != "GET"
                or path != "/internal/v1/account-actions/profile-baselines/state"
                or request.url.query != expected.encode()):
                raise ValueError("LIVE_TEXT_CORE_ENDPOINT_CHANGED")
            path += "?" + expected
        if (
            not (
                request.url.path.startswith("/internal/v1/")
                or (
                    self.plan.value.get("live_text", {}).get("image_scope") == "owner_image_v1"
                    and (re.fullmatch(
                        r"/internal/v2/content/evt_[a-f0-9]{64}/image(?:/claim|/check)?",
                        request.url.path,
                    ) or (request.method == "GET" and re.fullmatch(
                        r"/internal/v2/content/evt_[a-f0-9]{64}", request.url.path)))
                )
                or (
                    self.plan.value.get("live_text", {}).get("file_scope") == "owner_file_v1"
                    and ((request.method == "POST" and re.fullmatch(
                        r"/internal/v2/content/evt_[a-f0-9]{64}/file(?:/claim|/check)?",
                        request.url.path,
                    )) or (request.method == "GET" and re.fullmatch(
                        r"/internal/v2/content/evt_[a-f0-9]{64}", request.url.path)))
                )
            )
            or request.headers.get("Authorization") != "Bearer " + self.plan.config.bridge_token
            or not private_request_matches(
                request,
                self.plan.config.core_url,
                path,
                synthetic=self.plan.value["execution"] == "local_synthetic",
            )
        ):
            raise ValueError("LIVE_TEXT_CORE_ENDPOINT_CHANGED")

    def core_http(self, client):
        self.check_core_client(client)
        return checked_http_client(client, self.check_core_request)

    def file_http(self, protocol, original, check):
        """Only the currently leased original file may resolve a native download URL."""
        scope = self.plan.value.get("live_text", {})
        if (scope.get("file_scope") != "owner_file_v1"
            or scope.get("companion_scope") != "owner_private_v1"
            or scope.get("rich_input_scope") != "references_v1"
            or not self.plan.config.capture_public_key):
            raise ValueError("INVALID_FILE_SCOPE")
        body = {"user_id": int(self.plan.config.owner_qq_id), "file_id": original.file_id,
                "file_hash": original.file_hash}

        async def boundary(request):
            check_protocol_request(self, request, file_read=True)
            content = request.content
            if decode(content, 16384) != body or "Cookie" in request.headers:
                raise ValueError("FILE_ACTION_CHANGED")
            await check()
            self.verify()
            seal_protocol_content(request, content)

        return checked_http_client(protocol, boundary)

    async def core_post(self, client, path, body):
        async with self.core_http(client) as scoped:
            return await bounded_post(scoped, path, body)

    async def core_get(self, client, path):
        content = bytearray()
        async with self.core_http(client) as scoped:
            async with scoped.stream("GET", path) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise ValueError("INVALID_CORE_RECEIPT")
        return decode(bytes(content), 65536)

    async def stop_core(self, client):
        mode, plan_hash, config, synthetic = self._stop_identity
        if mode != "live_text":
            return
        try:
            self.verify(active=False)
            records, _ = self.read_journal()
            confirmed = (self.plan.sha256 == plan_hash
                         and any(r.get("kind") == "CORE_STOP_CONFIRMED" for r in records))
        except Exception:
            # Unreadable local evidence cannot veto the independent STOP request.
            confirmed = False
        if confirmed:
            self.core_stop_ack = True
            return
        path = "/internal/v1/live-text/stop"
        body = {"window_sha256": plan_hash}

        async def boundary(request):
            if (request.method != "POST" or request.url.path != path
                or request.headers.get("Authorization") != "Bearer " + config.bridge_token
                or not private_request_matches(request, config.core_url, path,
                                               synthetic=synthetic)
                or decode(request.content, 65536) != body):
                raise ValueError("LIVE_TEXT_CORE_ENDPOINT_CHANGED")
            seal_protocol_content(request, request.content)

        self.core_stop_ack = False
        try:
            if (str(client.base_url).rstrip("/") != config.core_url.rstrip("/")
                or client.headers.get("Authorization") != "Bearer " + config.bridge_token):
                raise ValueError("LIVE_TEXT_CORE_ENDPOINT_CHANGED")
            async with checked_http_client(client, boundary) as scoped:
                result = await bounded_post(scoped, path, body)
            if result != {"stopped": True}:
                raise ValueError("LIVE_TEXT_CORE_STOP_UNCONFIRMED")
        except Exception:
            raise ValueError("LIVE_TEXT_CORE_STOP_UNCONFIRMED") from None
        # The remote ACK is real even if local evidence cannot be recorded. Never
        # repair/replace damaged evidence or mistake a persistence error for no ACK.
        self.core_stop_ack = True
        self.verify(active=False)
        records, _ = self.read_journal()
        if not any(r.get("kind") == "CORE_STOP_CONFIRMED" for r in records):
            head = self.append("CORE_STOP_CONFIRMED", {})
            with self.db:
                self.db.execute("UPDATE test_window SET journal_head=?", (head,))

    async def stop_live(self, client, reason="OWNER_STOP"):
        if self.plan.mode != "live_text":
            raise ValueError("LIVE_TEXT_WINDOW_REQUIRED")
        try:
            self.stop(reason)
        finally:
            await self.stop_core(client)
        return self.status()

    def check_opening(self):
        if digest(self.plan.value) != self.plan.sha256:
            raise ValueError("WINDOW_IMMUTABLE_OR_MISSING")
        if not config_matches(self.plan.config, self.plan.value["config_sha256"]) or (
            self.plan.mode == "live_text"
            and self.plan.value["live_text"]["bridge_code_sha256"] != bridge_code_sha256()
        ):
            raise ValueError("LIVE_TEXT_RUNTIME_CHANGED")
        if self.stop_path.exists() or self.stop_path.is_symlink():
            raise ValueError("WINDOW_STOPPED")
        # All runtime/file checks precede the final clock sample. The probe's
        # transport calls this after every request hook, including DNS pinning.
        if not self.plan.start <= self.clock() < self.plan.end:
            raise ValueError("WINDOW_OUTSIDE_INTERVAL")

    async def probe(self, protocol):
        if (
            str(protocol.base_url).rstrip("/") != self.plan.config.onebot_http_url.rstrip("/")
            or protocol.headers.get("Authorization") != "Bearer " + self.plan.config.onebot_token
        ):
            raise ValueError("LIVE_TEXT_ONEBOT_ENDPOINT_CHANGED")

        async def boundary(request):
            check_protocol_request(self, request)
            content = request.content
            if request.url.path == "/send_private_msg" or decode(content, 64) != {}:
                raise ValueError("WINDOW_PROBE_CHANGED")
            self.check_opening()
            seal_protocol_content(request, content)

        async with checked_http_client(protocol, boundary) as scoped:
            if not (await OneBotClient(self.plan.config, scoped).probe())["connection_ready"]:
                raise ValueError("ONEBOT_NOT_READY")

    async def handoff(self, client):
        scope = self.plan.value.get("live_text", {}).get("handoff")
        if self.plan.mode != "live_text" or not scope:
            raise ValueError("WINDOW_HANDOFF_APPROVAL_REQUIRED")
        if digest(self.plan.value) != self.plan.sha256 or (
            not config_matches(self.plan.config, self.plan.value["config_sha256"])
            or self.plan.value["live_text"]["bridge_code_sha256"] != bridge_code_sha256()
        ):
            raise ValueError("LIVE_TEXT_RUNTIME_CHANGED")
        current = self.db.execute("SELECT * FROM test_window WHERE singleton=1").fetchone()
        if not current:
            raise ValueError("WINDOW_NOT_FOUND")
        old = TestWindow(self.spool, stored_plan(current["plan_json"], self.plan.config),
                         clock=self.clock)
        source = old.verify(active=False)
        records, head = old.read_journal()
        capturing = False
        for record in records:
            if record["kind"] == "CAPTURE_STARTED":
                capturing = True
            elif record["kind"] in {"CAPTURED", "CAPTURE_DUPLICATE"}:
                capturing = False
            elif record["kind"] == "STOP" and record["data"].get("reason") == (
                "CAPTURE_FAILED_CLOSED"
            ):
                # The exclusive receiver exited its WS context before persisting
                # this terminal failure. A CAPTURED-fsync/DB crash instead leaves
                # a mismatching head (already refused), or an unresolved input.
                capturing = False
        if (
            source["state"] != "STOPPED" or old.plan.mode != "live_text"
            or scope["predecessor_plan_sha256"] != old.plan.sha256
            or scope["predecessor_window_id"] != old.plan.value["window_id"]
            or scope["bridge_journal_head_sha256"] != head
            or scope["identity_sha256"] != old.plan.value["identity_sha256"]
            or scope["protocol_epoch"] != old.plan.value["protocol_epoch"]
            or self.plan.value["live_text"]["budget_journal_id"]
            != old.plan.value["live_text"]["budget_journal_id"]
            or self.plan.value["window_id"] == old.plan.value["window_id"]
            or self.plan.journal == old.plan.journal or self.plan.journal.exists()
            or self.plan.journal.is_symlink() or self.stop_path.exists()
            or not self.plan.start <= self.clock() < self.plan.end
        ):
            raise ValueError("WINDOW_HANDOFF_SOURCE_MISMATCH")
        if capturing:
            raise ValueError("CAPTURE_RECONCILIATION_REQUIRED")
        if not any(record["kind"] == "CORE_STOP_CONFIRMED" for record in records):
            raise ValueError("LIVE_TEXT_CORE_STOP_UNCONFIRMED")
        identity = self.db.execute("SELECT value FROM identity").fetchall()
        if len(identity) != 1 or identity[0][0] != encode([
            self.plan.config.owner_qq_id, self.plan.config.bot_qq_id,
            self.plan.config.origin, self.plan.config.protocol_epoch,
        ]):
            raise ValueError("BRIDGE_IDENTITY_CHANGED")
        if self.db.execute("SELECT 1 FROM window_history WHERE plan_hash=?", (
            self.plan.sha256,
        )).fetchone() or any(
            json.loads(json.loads(row[0])["plan_json"])[
                "window_id"
            ] == self.plan.value["window_id"]
            for row in self.db.execute("SELECT record_json FROM window_history")
        ):
            raise ValueError("WINDOW_ALREADY_REGISTERED")
        old.check_spool()
        from .owner_resolution import unsettled_attempts, unsettled_inputs, unsettled_results

        if unsettled_inputs(old) or unsettled_attempts(old) or unsettled_results(old):
            raise ValueError("WINDOW_HANDOFF_RECONCILIATION_REQUIRED")
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'").fetchone():
            if self.db.execute("SELECT 1 FROM automatic_recalls WHERE state!='ACCEPTED' "
                               "LIMIT 1").fetchone():
                raise ValueError("WINDOW_RECALL_RECONCILIATION_REQUIRED")
        from .pending_media import exists

        if exists(self) and self.db.execute("SELECT 1 FROM automatic_media "
                                           "WHERE state NOT IN ('DONE','CANCELLED')").fetchone():
            raise ValueError("WINDOW_MEDIA_RECONCILIATION_REQUIRED")
        if (
            old.stop_path.is_symlink() or not old.stop_path.is_file()
            or old.stop_path.read_bytes() != b"OWNER_STOP\n"
        ):
            raise ValueError("WINDOW_STOP_EVIDENCE_MISSING")
        # Validate account continuity before preparing either side of the handoff.
        cumulative.fold(self, old.verify_history(records))
        self.prepare_schema()
        snapshot = dict(source)
        # Close locally BEFORE crossing HTTP. A lost acknowledgment is never an
        # invitation to retry another successor or refill the old allowance.
        requested = old.append("HANDOFF_REQUESTED", {
            "successor_window_sha256": self.plan.sha256, "handoff_sha256": digest(scope),
        })
        with self.db:
            self.db.execute("UPDATE test_window SET state='HANDOFF_UNCONFIRMED',journal_head=?",
                            (requested,))
        result = await self.core_post(client, "/internal/v1/live-text/handoff", {
            "window_sha256": self.plan.sha256,
        })
        if (
            not isinstance(result, dict) or set(result) != {
                "state", "window_sha256", "predecessor_window_sha256", "handoff_sha256",
                "budget_journal_head_sha256", "privacy_epoch", "erasure_ledger_sha256",
            }
            or result["state"] != "PREPARED" or result["window_sha256"] != self.plan.sha256
            or result["predecessor_window_sha256"] != old.plan.sha256
            or result["handoff_sha256"] != digest(scope)
            or result["privacy_epoch"] != scope["privacy_epoch"]
            or result["erasure_ledger_sha256"] != scope["erasure_ledger_sha256"]
            or not isinstance(result["budget_journal_head_sha256"], str)
            or not SHA.fullmatch(result["budget_journal_head_sha256"])
        ):
            raise ValueError("WINDOW_HANDOFF_CORE_UNCONFIRMED")
        closed = old.verify(active=False)
        if closed["state"] != "HANDOFF_UNCONFIRMED" or closed["journal_head"] != requested:
            raise ValueError("WINDOW_HANDOFF_SOURCE_MISMATCH")
        old.check_spool()
        retired = old.append("RETIRED", {
            "snapshot_sha256": digest(snapshot), "successor_window_sha256": self.plan.sha256,
            "core_receipt_sha256": digest(result), "handoff_sha256": digest(scope),
        })
        prepared = self.append("HANDOFF_PREPARED", {
            "predecessor_window_sha256": old.plan.sha256, "retirement_head": retired,
            "core_receipt": result, "handoff_sha256": digest(scope),
        }, create=True)
        with self.db:
            self.db.execute("INSERT INTO window_history VALUES(?,?,?,?)", (
                old.plan.sha256, encode(snapshot), retired, self.plan.sha256,
            ))
            self.db.execute(
                "UPDATE test_window SET plan_hash=?,plan_json=?,journal_head=?,state='PREPARED',"
                "fence=0,initial_counts='{}',initial_inbox_rowid=?,reason=NULL", (
                    self.plan.sha256, encode(self.plan.value), prepared,
                    self.db.execute("SELECT COALESCE(MAX(rowid),0) FROM inbox").fetchone()[0],
                ),
            )
        return self.status()

    def handoff_abort_source(self):
        # Only revocation uses stored authority after code drift/expiry. Neither
        # this path nor its receipt can open the old or proposed window.
        scope = self.plan.value.get("live_text", {}).get("handoff")
        if (self.plan.mode != "live_text" or not scope
            or digest(self.plan.value) != self.plan.sha256):
            raise ValueError("WINDOW_HANDOFF_ABORT_SOURCE_MISMATCH")
        current = self.db.execute("SELECT plan_json FROM test_window WHERE singleton=1").fetchone()
        if not current:
            raise ValueError("WINDOW_NOT_FOUND")
        old = TestWindow(self.spool, stored_plan(current[0], self.plan.config), clock=self.clock)
        source = old.verify(active=False)
        records, _ = old.read_journal()
        aborted = records[-1]["kind"] == "HANDOFF_ABORTED"
        requested = records[-2] if aborted and len(records) > 1 else records[-1]
        if (source["state"] != ("STOPPED" if aborted else "HANDOFF_UNCONFIRMED")
            or old.plan.mode != "live_text" or not isinstance(scope, dict)
            or set(scope) != HANDOFF_FIELDS
            or scope["predecessor_plan_sha256"] != old.plan.sha256
            or scope["predecessor_window_id"] != old.plan.value["window_id"]
            or self.plan.value["identity_sha256"] != old.plan.value["identity_sha256"]
            or self.plan.value["protocol_epoch"] != old.plan.value["protocol_epoch"]
            or self.plan.value["live_text"]["budget_journal_id"]
            != old.plan.value["live_text"]["budget_journal_id"]
            or self.plan.value["window_id"] == old.plan.value["window_id"]
            or requested["kind"] != "HANDOFF_REQUESTED"
            or requested["previous"] != scope["bridge_journal_head_sha256"]
            or requested["data"] != {"successor_window_sha256": self.plan.sha256,
                                     "handoff_sha256": digest(scope)}
            or sum(r["kind"] == "HANDOFF_REQUESTED" and r["data"].get(
                "successor_window_sha256") == self.plan.sha256 for r in records) != 1
            or self.plan.journal == old.plan.journal or self.plan.journal.exists()
            or self.plan.journal.is_symlink()
            or not any(r["kind"] == "CORE_STOP_CONFIRMED" for r in records)
            or old.stop_path.is_symlink() or not old.stop_path.is_file()
            or old.stop_path.read_bytes() != b"OWNER_STOP\n"
            or self.db.execute("SELECT 1 FROM window_history WHERE plan_hash=? OR successor_hash=?",
                               (self.plan.sha256, self.plan.sha256)).fetchone()):
            raise ValueError("WINDOW_HANDOFF_ABORT_SOURCE_MISMATCH")
        identity = self.db.execute("SELECT value FROM identity").fetchall()
        if len(identity) != 1 or identity[0][0] != encode([
            self.plan.config.owner_qq_id, self.plan.config.bot_qq_id,
            self.plan.config.origin, self.plan.config.protocol_epoch,
        ]):
            raise ValueError("BRIDGE_IDENTITY_CHANGED")
        old.check_spool()
        if (self.db.execute("SELECT 1 FROM window_inputs WHERE state NOT IN "
                            "('ACK','CANCELLED','SUPERSEDED','RECALLED') LIMIT 1").fetchone()
            or self.db.execute("SELECT 1 FROM window_attempts WHERE state NOT IN "
                               "('ACK','NOT_SENT') LIMIT 1").fetchone()
            or self.db.execute("SELECT 1 FROM results WHERE state!='accepted' OR "
                               "json_extract(body,'$.result') NOT IN ('SENT','NOT_SENT') "
                               "LIMIT 1").fetchone()):
            raise ValueError("WINDOW_HANDOFF_RECONCILIATION_REQUIRED")
        for table, condition in (("automatic_recalls", "state!='ACCEPTED'"),
                                 ("automatic_media", "state NOT IN ('DONE','CANCELLED')")):
            if (self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                and self.db.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1").fetchone()):
                raise ValueError("WINDOW_HANDOFF_RECONCILIATION_REQUIRED")
        return old, scope, records, requested, aborted

    async def abort_handoff(self, client):
        old, scope, records, requested, aborted = self.handoff_abort_source()
        if aborted:
            data = records[-1]["data"]
            if data["successor_window_sha256"] != self.plan.sha256 or data["handoff"] != scope:
                raise ValueError("WINDOW_HANDOFF_ABORT_SOURCE_MISMATCH")
            return data["core_receipt"]
        requested_head = digest(requested)
        body = {"window_sha256": self.plan.sha256,
                "bridge_journal_head_sha256": requested_head}
        path = "/internal/v1/live-text/handoff/abort"
        self.check_core_client(client)

        async def boundary(request):
            await self.check_core_request(request)
            if (request.method != "POST" or request.url.path != path
                or decode(request.content, 65536) != body):
                raise ValueError("WINDOW_HANDOFF_ABORT_SOURCE_MISMATCH")
            _, _, _, current, completed = self.handoff_abort_source()
            if completed or digest(current) != requested_head:
                raise ValueError("WINDOW_HANDOFF_ABORT_SOURCE_MISMATCH")
            seal_protocol_content(request, request.content)

        async with checked_http_client(client, boundary) as scoped:
            result = await bounded_post(scoped, path, body)
        handoff_abort_proof(old.plan, self.plan.sha256, scope, requested, result)
        _, _, _, current, completed = self.handoff_abort_source()
        if completed or digest(current) != requested_head:
            raise ValueError("WINDOW_HANDOFF_ABORT_SOURCE_MISMATCH")
        head = old.append("HANDOFF_ABORTED", {"successor_window_sha256": self.plan.sha256,
                                             "handoff": scope, "core_receipt": result})
        with self.db:
            self.db.execute("UPDATE test_window SET state='STOPPED',journal_head=?,reason=?",
                            (head, "HANDOFF_ABORTED_BEFORE_PREPARE"))
        old.verify(active=False)
        return result

    async def open_successor(self, client):
        row = self.verify(active=False)
        records, head = self.read_journal()
        if row["state"] != "PREPARED" or len(records) != 1:
            raise ValueError("WINDOW_HANDOFF_NOT_PREPARED")
        data = records[0]["data"]
        receipt = {
            "kind": "stopped_window_handoff", "window_sha256": self.plan.sha256,
            "predecessor_window_sha256": data["predecessor_window_sha256"],
            "handoff_sha256": data["handoff_sha256"],
            "core_receipt_sha256": digest(data["core_receipt"]),
            "bridge_journal_head_sha256": head,
        }
        receipt["signature"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
            self.plan.config.capture_signing_key
        )).sign(HANDOFF_DOMAIN + encode(receipt).encode()).hex()
        requested = self.append("OPEN_REQUESTED", {"receipt_sha256": digest(receipt)})
        with self.db:
            self.db.execute("UPDATE test_window SET state='OPEN_UNCONFIRMED',journal_head=?",
                            (requested,))
        await self.core_post(client, "/internal/v1/live-text/open", {
            "window_sha256": self.plan.sha256, "handoff_receipt": receipt,
        })
        baseline = await self.core_counts(client, initial=True)
        self.verify(active=False)
        if not self.plan.start <= self.clock() < self.plan.end or self.stop_path.exists():
            raise ValueError("WINDOW_STOPPED")
        head = self.append("OPEN", {"plan_sha256": self.plan.sha256})
        with self.db:
            self.db.execute("UPDATE test_window SET state='OPEN',journal_head=?,initial_counts=?",
                            (head, encode(baseline)))
        return self.status()

    async def open(self, client, protocol=None):
        self.check_opening()
        successor = bool(self.plan.value.get("live_text", {}).get("handoff"))
        if successor:
            row = self.verify(active=False)
            records, _ = self.read_journal()
            if row["state"] != "PREPARED" or len(records) != 1:
                raise ValueError("WINDOW_HANDOFF_NOT_PREPARED")
        elif self.plan.journal.exists() or self.plan.journal.is_symlink() or self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='test_window'"
        ).fetchone():
            raise ValueError("WINDOW_ALREADY_REGISTERED")
        if self.plan.value["execution"] == "approved_live" and protocol is None:
            raise ValueError("ONEBOT_READINESS_REQUIRED")
        if (
            self.spool.pending_count
            or self.db.execute(
                "SELECT 1 FROM results WHERE state!='accepted' "
                "OR json_extract(body,'$.result')!='SENT' LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("INITIAL_SPOOL_MUST_BE_CONFIRMED")
        if protocol is not None:
            await self.probe(protocol)
        self.check_opening()
        if successor:
            return await self.open_successor(client)
        if self.plan.mode == "live_text":
            self.check_core_client(client)
            await self.core_post(client, "/internal/v1/live-text/open", {
                "window_sha256": self.plan.sha256,
            })
        baseline = await self.core_counts(client, initial=True)
        if not self.plan.start <= self.clock() < self.plan.end:
            raise ValueError("WINDOW_OUTSIDE_INTERVAL")
        head = self.append("OPEN", {"plan_sha256": self.plan.sha256}, create=True)
        self.db.executescript(
            "CREATE TABLE test_window (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
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
            "CREATE TABLE window_history(plan_hash TEXT PRIMARY KEY,record_json TEXT NOT NULL,"
            "retirement_head TEXT NOT NULL,successor_hash TEXT NOT NULL);"
        )
        with self.db:
            self.db.execute(
                "INSERT INTO test_window VALUES(1,?,?,?,'OPEN',0,?,?,NULL)",
                (
                    self.plan.sha256,
                    encode(self.plan.value),
                    head,
                    encode(baseline),
                    self.db.execute("SELECT COALESCE(MAX(rowid),0) FROM inbox").fetchone()[0],
                ),
            )
        return self.status()

    def status(self):
        row = self.verify(active=False)
        attempts = dict(
            self.db.execute("SELECT state,COUNT(*) FROM window_attempts WHERE plan_hash=? "
                            "GROUP BY state", (self.plan.sha256,)).fetchall()
        )
        result = {
            "state": row["state"],
            "reason": row["reason"],
            "plan_sha256": row["plan_hash"],
            "fence": row["fence"],
            "new_inputs": self.count("window_inputs"),
            "attempts_reserved": sum(attempts.values()),
            "attempt_states": attempts,
            "visible_replies_upper_bound": sum(attempts.values()),
            "phone_receipts": None,
            "e2e_p50_ms": None,
            "e2e_p95_ms": None,
        }
        if row["state"] == "OPEN" and self.stop_path.exists():
            result.update(state="STOP_REQUESTED", reason="OWNER_STOP_REQUESTED")
        if row["state"] == "CAPTURING":
            result.update(state="CAPTURE_UNCONFIRMED", reason="CAPTURE_RECONCILIATION_REQUIRED")
        if self.plan.mode == "live_text":
            records, _ = self.read_journal()
            confirmed = any(record.get("kind") == "CORE_STOP_CONFIRMED" for record in records)
            result.update(mode="live_text", model_attempts=self.model_attempts,
                          local_state=result["state"], core_stop_confirmed=confirmed)
            if row["state"] == "STOPPED" and not confirmed:
                result["state"] = "CORE_STOP_UNCONFIRMED"
        return result

    def stop(self, reason="OWNER_STOP"):
        request_stop(self.spool.directory, plan=self.plan)
        self.verify(active=False)
        head = self.append("STOP", {"reason": reason})
        with self.db:
            self.db.execute(
                "UPDATE test_window SET state='STOPPED',reason=?,journal_head=? WHERE singleton=1",
                (reason, head),
            )
            if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='automatic_received'",
            ).fetchone():
                self.db.execute("DELETE FROM automatic_received WHERE window_sha256=?",
                                (self.plan.sha256,))
        from .pending_media import stop

        stop(self)
        if self.plan.config.meme_enabled:
            for row in self.db.execute("SELECT event_id FROM window_inputs WHERE plan_hash=?",
                                       (self.plan.sha256,)).fetchall():
                self.memes.cancel(row["event_id"])
        return self.status()

    def check_spool(self):
        from .owner_resolution import pending_result

        row = self.row()
        if (
            self.spool.pending_count
            or pending_result(self.spool)
        ):
            raise ValueError("SPOOL_RECONCILIATION_REQUIRED")
        if self.db.execute(
            "SELECT 1 FROM inbox i WHERE i.rowid>? AND NOT EXISTS "
            "(SELECT 1 FROM window_inputs w WHERE w.event_id=i.key AND w.plan_hash=?) LIMIT 1",
            (row["initial_inbox_rowid"], self.plan.sha256),
        ).fetchone():
            raise ValueError("UNAPPROVED_SPOOL_INPUT")

    async def ingest(self, client, raw, approval):
        if self.busy:
            raise ValueError("WINDOW_EXECUTOR_BUSY")
        self.busy = True
        try:
            return await self._ingest(client, raw, approval)
        except BaseException as error:
            try:
                await self.stop_core(client)
            except BaseException:
                pass
            try:
                self.stop("INPUT_FAILED_CLOSED")
            except Exception:
                pass
            if (
                self.plan.mode == "live_text" and not self.core_stop_ack
                and isinstance(error, Exception)
            ):
                raise ValueError("LIVE_TEXT_CORE_STOP_UNCONFIRMED") from None
            raise
        finally:
            self.busy = False

    async def _ingest(self, client, raw, approval):
        self.verify()
        if self.plan.value["execution"] != "local_synthetic":
            raise ValueError("AUTHENTICATED_CAPTURE_REQUIRED")
        self.check_spool()
        await self.core_counts(client)
        message = raw.get("message")
        if isinstance(message, str):
            plain = "[CQ:" not in message
        else:
            plain = (
                isinstance(message, list)
                and bool(message)
                and all(
                    isinstance(segment, dict) and segment.get("type") == "text"
                    for segment in message
                )
            )
        event = normalize(encode(raw), self.plan.config, now=self.clock().timestamp())
        if (
            not plain
            or event is None
            or not event["payload"]["text"].strip()
            or len(event["payload"]["text"]) > self.plan.value["max_input_chars"]
            or not self.plan.start <= datetime.fromisoformat(event["occurred_at"]) < self.plan.end
        ):
            raise ValueError("WINDOW_OWNER_TEXT_INPUT_REQUIRED")
        if self.plan.mode == "live_text" and (
            event["payload"]["text"].strip().startswith("/")
            or self.plan.value["execution"] != "local_synthetic"
        ):
            raise ValueError("LIVE_TEXT_ORDINARY_AUTHENTICATED_INPUT_REQUIRED")
        event_id, text_hash = event["event_id"], digest(event["payload"]["text"])
        expected = {
            "window_sha256": self.plan.sha256,
            "event_id": event_id,
            "input_sha256": text_hash,
        }
        if (
            not isinstance(approval, dict)
            or set(approval) != {*expected, "evidence_sha256"}
            or any(approval[key] != val for key, val in expected.items())
            or not isinstance(approval["evidence_sha256"], str)
            or not SHA.fullmatch(approval["evidence_sha256"])
        ):
            raise ValueError("INDIVIDUAL_INPUT_APPROVAL_REQUIRED")
        prior = self.db.execute(
            "SELECT input_hash,plan_hash FROM window_inputs WHERE event_id=?", (event_id,)
        ).fetchone()
        if prior:
            if prior["plan_hash"] != self.plan.sha256:
                raise ValueError("HISTORICAL_INPUT_REFUSED")
            # Spool also checks full source fingerprint: same ID/time/text changes are conflicts.
            if prior[0] != text_hash:
                raise ValueError("SOURCE_EVENT_CONFLICT")
            self.spool.put(event, digest({"message": raw["message"], "time": raw["time"]}))
            return self.status()
        if (
            self.count("window_inputs")
            >= self.plan.value["max_new_inputs"]
        ):
            raise ValueError("WINDOW_INPUT_CAP")
        if self.count("window_inputs", "state!='ACK'"):
            raise ValueError("ONE_INPUT_AT_A_TIME")
        if self.db.execute("SELECT 1 FROM inbox WHERE key=?", (event_id,)).fetchone():
            raise ValueError("HISTORICAL_INPUT_REFUSED")
        self.verify()
        if self.plan.mode == "live_text":
            cumulative.admit(self, "owner_inputs", digest(event_id))
            input_at = self.clock()
            await self.core_post(client, "/internal/v1/live-text/inputs/approve", approval)
            self.verify()
            quota = cumulative.scope(self.plan)
            if quota and cumulative.periods(input_at, quota["timezone"]) != cumulative.periods(
                self.clock(), quota["timezone"],
            ):
                raise ValueError("CUMULATIVE_CAPTURE_PERIOD_CHANGED")
        head = self.append(
            "INPUT",
            {
                "event_hash": digest(event_id),
                "text_sha256": text_hash,
                "approval_sha256": digest(approval),
            },
        )
        with self.db:
            self.spool.put(event, digest({"message": raw["message"], "time": raw["time"]}))
            self.db.execute(
                "INSERT INTO window_inputs VALUES(?,?,?,'APPROVED',NULL,?)",
                (event_id, text_hash, digest(approval), self.plan.sha256),
            )
            self.db.execute("UPDATE test_window SET journal_head=?", (head,))
        self.verify()
        async with self.core_http(client) as scoped:
            await self.spool.flush_one(scoped)
        return self.status()

    async def capture_once(self, client, protocol):
        from .capture import capture_once

        return await capture_once(self, client, protocol)

    async def approve_captured(self, client, approval, **image_options):
        from .capture import approve_captured

        return await approve_captured(self, client, approval, **image_options)

    async def send_once(self, client, protocol, approval):
        if self.busy:
            raise ValueError("WINDOW_EXECUTOR_BUSY")
        self.busy = True
        operation = SendOperation(self, client, approval)
        try:
            self.verify()
            self.check_spool()
            deliveries = await self.core_counts(client)
            self.verify()
            sender = WindowOneBot(operation, protocol)
            async with self.core_http(client) as scoped, sender.delegate.client:
                if not await deliver_tick(scoped, sender, self.spool, guard=operation):
                    if operation.has_pending_segment(deliveries):
                        return self.status() | {"delivery_wait": "SEGMENT_NOT_READY"}
                    raise ValueError("NO_APPROVED_DELIVERY_READY")
            if self.row()["state"] == "STOPPED":
                await self.stop_core(client)
            return self.status()
        except BaseException as error:
            try:
                await self.stop_core(client)
            except BaseException:
                pass
            # Cancellation/crash never refunds reservations. A hard kill leaves RESERVED/
            # IO_STARTED and the next invocation blocks before any new send.
            try:
                self.stop("SEND_FAILED_CLOSED")
            except Exception:
                pass
            if (
                self.plan.mode == "live_text" and not self.core_stop_ack
                and isinstance(error, Exception)
            ):
                raise ValueError("LIVE_TEXT_CORE_STOP_UNCONFIRMED") from None
            raise
        finally:
            self.busy = False


class SendOperation:
    """One deliver_tick invocation; no send state escapes into the window object."""

    def __init__(self, window, client, approval):
        self.window, self.db, self.client, self.approval = window, window.db, client, approval
        self.clock = window.clock
        self.current = self.current_item = None
        self.lease_until = None
        self.segmented_scope = reply_segments.scope(window.plan)
        self.dispatched = False
        self.meme_prepared = False
        self.meme_sending = False
        self.prepared_voice = None

    async def cancel_before_send(self, item):
        from .continuous import interruptible, pending_input

        account = account_actions.is_account(item)
        if ((not interruptible(self.window) and not account) or self.dispatched or not self.current
            or self.current_item is not item):
            return False
        attempt = self.db.execute("SELECT * FROM window_attempts WHERE message_id=? AND fence=?",
                                   self.current).fetchone()
        if (not attempt or attempt["state"] not in {"RESERVED", "IO_STARTED"}
            or attempt["item_hash"] != digest(item)):
            return False
        cause = None if account else pending_input(self.window, delivery_origin(item))
        revision = item.get("reply_revision")
        poke_response = item.get("delivery_key", "").startswith("poke-response:")
        if account:
            if not account_actions.enabled(self.window):
                return False
        elif poke_response:
            if (item.get("action_type") not in {"qq.private.reply", "qq.private.poke"}
                or "reply_revision" in item):
                return False
        elif item.get("action_type") == "qq.private.reply":
            if item.get("window_notice") == "commitment" and "reply_revision" not in item:
                # A new conversational turn does not revoke an accepted task.
                # Core must establish that this exact notice's source is invalid.
                cause = None
            elif type(revision) is not int or revision < 0:
                return False
        elif (item.get("action_type") != "qq.private.proactive" or not cause
              or "reply_revision" in item):
            return False
        try:
            if account:
                receipt = await self.window.core_post(self.client,
                    "/internal/v1/account-actions/cancel-before-send",
                    account_actions.send_check(self.window, item)
                    | {"reason": "BRIDGE_PRE_DISPATCH_FENCED"})
            else:
                receipt = await self.window.core_post(self.client,
                    "/internal/v1/live-text/cancel-before-send", {
                    "window_sha256": self.window.plan.sha256, "message_id": item["message_id"],
                    "lease_token": item["lease_token"],
                } | ({"reply_revision": revision} if revision is not None else {})
                | ({"content_sha256": digest(item["content"])}
                   if item.get("window_notice") == "commitment" or poke_response else {})
                | ({"cause": "OWNER_INPUT_PENDING", "pending_event_id": cause} if cause else {}))
        except httpx.HTTPError:
            return False
        if account:
            account_actions.cancelled_proof(receipt, message_id=item["message_id"],
                lease_token=item["lease_token"], content_sha256=item["content_sha256"],
                payload_sha256=item["content"]["payload_sha256"])
        else:
            not_sent_proof(item["message_id"], receipt)
        if self.dispatched or attempt["item_hash"] != digest(item):
            raise ValueError("WINDOW_SEND_FENCED")
        self.window.verify(active=False)
        head = self.window.append("NOT_SENT", {"fence": self.current[1], "core_receipt": receipt})
        with self.db:
            self.window.spool.cancel_delivery(item, receipt)
            self.db.execute("UPDATE window_attempts SET state='NOT_SENT' WHERE message_id=?",
                            (item["message_id"],))
            self.db.execute("UPDATE test_window SET journal_head=?", (head,))
        if self.meme_prepared:
            self.window.memes.confirm_not_sent(item, self, receipt)
        elif item.get("content", {}).get("image", {}).get("source") == "companion.meme_catalog":
            self.window.memes.cancel(reply_segments.event_id(item))
        return True

    def segment_context(self, item, event_id, *, current=False):
        segment = reply_segments.envelope(item)
        rows = self.db.execute(
            "SELECT * FROM window_attempts WHERE event_id=? AND plan_hash=? ORDER BY fence",
            (event_id, self.window.plan.sha256),
        ).fetchall()
        if segment is None:
            if rows and not current:
                raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
            return None, None
        policy = self.segmented_scope
        if not policy:
            raise ValueError("SEGMENTED_REPLY_SCOPE_REQUIRED")
        if ((policy["max_segments"] != "unlimited" and segment["count"] > policy["max_segments"])
            or segment["interval_ms"] != policy["interval_ms"]
            or segment["total_chars"] > self.window.plan.value["max_reply_chars"]
            or len(rows) != segment["index"] + int(current)):
            raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
        records, _ = self.window.read_journal()
        reservations = [record for record in records if record["kind"] == "RESERVED"
                        and record["data"].get("event_hash") == digest(event_id)]
        if len(reservations) != len(rows):
            raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
        terminals = {record["data"]["fence"]: record for record in records
                     if record["kind"] in {"IO_STARTED", "ACK", "UNKNOWN"}}
        saved = reply_segments.journal_value(segment, item["content"]["text"])
        common = {key: value for key, value in saved.items() if key not in {"index", "chars"}}
        chars, not_before = 0, None
        for index, (row, record) in enumerate(zip(rows, reservations, strict=True)):
            prior = record["data"].get("reply_segment")
            if (not isinstance(prior, dict) or set(prior) != set(saved)
                or prior["index"] != index or type(prior["chars"]) is not int
                or not (0 if segment["version"] == 3 else 1) <= prior["chars"]
                <= segment["total_chars"]
                or {key: value for key, value in prior.items()
                    if key not in {"index", "chars"}} != common
                or record["data"].get("fence") != row["fence"]
                or record["data"].get("message_hash") != digest(row["message_id"])
                or record["data"].get("item_sha256") != row["item_hash"]
                or row["text_hash"] != segment["part_sha256s"][index]):
                raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
            if index < segment["index"]:
                terminal = terminals.get(row["fence"])
                if row["state"] != "ACK" or not terminal or terminal["kind"] != "ACK":
                    raise ValueError("REPLY_SEGMENT_PREDECESSOR_REQUIRED")
                not_before = cumulative.timestamp(terminal["at"]) + timedelta(
                    milliseconds=segment["interval_ms"])
                chars += prior["chars"]
            elif (row["message_id"] != item["message_id"] or prior != saved):
                raise ValueError("REPLY_SEGMENT_SEQUENCE_REQUIRED")
        chars += len(item["content"]["text"])
        if chars > segment["total_chars"] or (
            segment["index"] == segment["count"] - 1 and chars != segment["total_chars"]
        ):
            raise ValueError("REPLY_SEGMENT_TOTAL_CHANGED")
        return segment, not_before

    def has_pending_segment(self, deliveries):
        # Classify an empty Core lease; this is not the native pre-lease pacing hook.
        if not self.segmented_scope or not deliveries.get("PENDING", 0):
            return False
        approval = self.approval
        if (not isinstance(approval, dict)
            or approval.get("window_sha256") != self.window.plan.sha256):
            return False
        row = self.db.execute(
            "SELECT a.* FROM window_attempts a JOIN window_inputs i USING(event_id) "
            "WHERE a.plan_hash=? AND i.plan_hash=? AND i.state='APPROVED' "
            "AND a.event_id=? ORDER BY a.fence DESC LIMIT 1",
            (self.window.plan.sha256, self.window.plan.sha256, approval.get("event_id")),
        ).fetchone()
        if not row or row["state"] != "ACK":
            return False
        records, _ = self.window.read_journal()
        record = next((r for r in records if r["kind"] == "RESERVED"
                       and r["data"].get("fence") == row["fence"]), None)
        segment = record["data"].get("reply_segment") if record else None
        return bool(segment and segment["index"] < segment["count"] - 1)

    def reserve(self, item):
        from . import pokes
        from .native_actions import approval_hash, prepare

        self.window.verify()
        approval = self.approval
        event_id = delivery_origin(item)
        segment, not_before = self.segment_context(item, event_id)
        expected = {
            "window_sha256": self.window.plan.sha256,
            "event_id": event_id,
            "message_id": item["message_id"],
            **approval_hash(item),
        }
        if (
            not isinstance(approval, dict)
            or set(approval) != {*expected, "evidence_sha256"}
            or any(approval[key] != val for key, val in expected.items())
            or not isinstance(approval["evidence_sha256"], str)
            or not SHA.fullmatch(approval["evidence_sha256"])
        ):
            raise ValueError("INDIVIDUAL_REPLY_APPROVAL_REQUIRED")
        account = account_actions.is_account(item)
        native = item.get("action_type") == "qq.private.poke" or account
        if account:
            self.prepared_account = account_actions.prepare(self.window, item)
        elif native:
            if not pokes.enabled(self.window):
                raise ValueError("PRIVATE_POKE_SCOPE_REQUIRED")
            self.prepared_native = prepare(item["content"], self.window.plan.config.owner_qq_id)
        if "voice" in item:
            from .voice_runtime import prepare_delivery

            self.prepared_voice = prepare_delivery(self.window, item)
        if not native and len(item["content"]["text"]) > self.window.plan.value["max_reply_chars"]:
            raise ValueError("WINDOW_REPLY_TEXT_LIMIT")
        if not self.db.execute(
            "SELECT 1 FROM window_inputs WHERE event_id=? AND state='APPROVED' AND plan_hash=?",
            (event_id, self.window.plan.sha256),
        ).fetchone() and not (
            self.window.plan.value.get("live_text", {}).get("companion_scope") == "owner_private_v1"
            and (item.get("action_type") == "qq.private.proactive"
                 or item.get("window_notice") == "commitment"
                 or (account and account_actions.enabled(self.window))
                 or (pokes.enabled(self.window) and pokes.admitted(self.window, event_id)
                     and re.fullmatch(r"poke-response:job_[a-f0-9]{32}",
                                      item.get("delivery_key", ""))))
        ):
            raise ValueError("UNAPPROVED_DELIVERY_INPUT")
        attempts = self.window.count("window_attempts")
        remaining = segment["count"] if segment and segment["index"] == 0 else 1
        if attempts + remaining > min(
            self.window.plan.value["max_attempts"], self.window.plan.value["max_visible_replies"]
        ):
            raise ValueError("WINDOW_ATTEMPT_CAP")
        if not_before is not None and self.clock() < not_before:
            raise ValueError("REPLY_SEGMENT_INTERVAL_REQUIRED")
        if segment and segment["index"] == 0:
            cumulative.admit(self.window, "send_attempts", digest(item["message_id"]),
                             amount=segment["count"])
        fence = self.window.row()["fence"] + 1
        item_hash = digest(item)
        from .owner_resolution import binding

        retained = binding(self.window, item, event_id)
        head = self.window.append(
            "RESERVED",
            {
                "message_hash": digest(item["message_id"]),
                "event_hash": digest(event_id),
                "fence": fence,
                "item_sha256": item_hash,
                **({"delivery_binding": retained} if retained is not None else {}),
                **({"reply_segment": reply_segments.journal_value(
                    segment, item["content"]["text"])} if segment else {}),
            },
        )
        with self.db:
            self.db.execute(
                "INSERT INTO window_attempts VALUES(?,?,?,'RESERVED',?,?,NULL,?,?)",
                (item["message_id"], event_id, fence, next(iter(approval_hash(item).values())),
                 digest(approval),
                 item_hash, self.window.plan.sha256),
            )
            self.db.execute("UPDATE test_window SET fence=?,journal_head=?", (fence, head))
        self.current = (item["message_id"], fence)
        self.current_item = item
        self.lease_until = datetime.fromisoformat(item["lease_until"])

    def before_io(self, text):
        self.window.verify(active=False, dispatch=True)
        row = self.window.row()
        if (
            row["state"] != "OPEN"
            or not self.window.plan.start <= self.clock() < self.window.plan.end
            or not self.current
            or self.current[1] != row["fence"]
        ):
            raise ValueError("WINDOW_SEND_FENCED")
        attempt = self.db.execute(
            "SELECT * FROM window_attempts WHERE message_id=? AND fence=? AND plan_hash=?",
            (*self.current, self.window.plan.sha256),
        ).fetchone()
        if not attempt or attempt["state"] != "RESERVED" or attempt["text_hash"] != digest(text):
            raise ValueError("WINDOW_SEND_FENCED")
        head = self.window.append("IO_STARTED", {"fence": row["fence"]})
        with self.db:
            self.db.execute(
                "UPDATE window_attempts SET state='IO_STARTED' WHERE message_id=?",
                (self.current[0],),
            )
            self.db.execute("UPDATE test_window SET journal_head=?", (head,))
        # No await occurs between the final clock check and the existing send adapter.
        if not self.window.plan.start <= self.clock() < self.window.plan.end:
            raise ValueError("WINDOW_SEND_FENCED")

    def before_dispatch(self, *, state="IO_STARTED", request=None):
        from .native_actions import approval_hash, prepare

        # HTTP request hooks can await DNS pinning after before_io(). Check again
        # after those hooks, at the transport boundary; keep the spent reservation.
        self.window.verify(active=False, dispatch=True)
        row = self.window.row()
        attempt = self.db.execute(
            "SELECT * FROM window_attempts WHERE message_id=? AND fence=? AND plan_hash=?",
            (*self.current, self.window.plan.sha256),
        ).fetchone()
        item = self.current_item
        lease_until = min(self.lease_until, datetime.fromisoformat(item["lease_until"]))
        quota = cumulative.inspect(self.window)
        _, not_before = (
            self.segment_context(item, attempt["event_id"], current=True)
            if attempt else (None, None)
        )
        if request is not None:
            account = account_actions.is_account(item)
            native = item.get("action_type") == "qq.private.poke"
            check_protocol_request(self.window, request, native_poke=native,
                account_action=self.prepared_account.path if account else None)
            content = request.content
            if account:
                prepared = account_actions.prepare(self.window, item)
                if (prepared != self.prepared_account or request.url.path != prepared.path
                    or content != prepared.body or not attempt
                    or prepared.content_sha256 != attempt["text_hash"]):
                    raise ValueError("WINDOW_SEND_FENCED")
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'")\
                        .fetchone() and self.db.execute("SELECT 1 FROM automatic_recalls WHERE "
                            "window_sha256=? AND state='PENDING' LIMIT 1",
                            (self.window.plan.sha256,)).fetchone():
                    raise ValueError("OWNER_RECALL_PENDING")
            elif native:
                prepared = prepare(item["content"], self.window.plan.config.owner_qq_id)
                if (request.url.path != prepared.path or content != prepared.body
                    or prepared != self.prepared_native or not attempt
                    or prepared.content_sha256 != attempt["text_hash"]):
                    raise ValueError("WINDOW_SEND_FENCED")
            elif self.prepared_voice is not None:
                from .voice_runtime import prepare_delivery

                prepared = prepare_delivery(self.window, item)
                body = decode(content, self.window.plan.config.max_voice_lease_bytes)
                expected = {"user_id": int(self.window.plan.config.owner_qq_id),
                            "message": prepared.message, "auto_escape": True}
                if (request.url.path != "/send_private_msg" or prepared != self.prepared_voice
                    or body != expected or digest(body) != prepared.body_sha256 or not attempt
                    or digest(item["content"]["text"]) != attempt["text_hash"]):
                    raise ValueError("WINDOW_SEND_FENCED")
            else:
                expected = {"user_id": int(self.window.plan.config.owner_qq_id),
                        "message": getattr(self, "prepared_message", None) or [
                            {"type": "text", "data": {"text": reply_segments.wire_text(item)}}],
                        "auto_escape": True}
                body = decode(content, 6000000 if getattr(self, "prepared_message", None)
                              else 65536)
                if (request.url.path != "/send_private_msg" or body != expected or not attempt
                    or body["message"][0]["data"]["text"] != reply_segments.wire_text(item)
                    or digest(item["content"]["text"]) != attempt["text_hash"]):
                    raise ValueError("WINDOW_SEND_FENCED")
        stop_requested = self.window.stop_path.exists()
        # Assets and ledger reads are complete. No await or file I/O follows the
        # final clock sample before the checked request stream is handed off.
        if (
            stop_requested or not attempt
            or digest(self.approval) != attempt["approval_hash"]
            or digest(item) != attempt["item_hash"]
            or item["message_id"] != self.current[0]
            or self.approval["message_id"] != self.current[0]
            or delivery_origin(item) != attempt["event_id"]
            or self.approval["event_id"] != attempt["event_id"]
            or any(self.approval.get(key) != value or value != attempt["text_hash"]
                   for key, value in approval_hash(item).items())
            or digest(self.window.plan.value) != self.window.plan.sha256
            or not config_matches(self.window.plan.config, self.window.plan.value["config_sha256"])
            or row["state"] != "OPEN"
            or row["fence"] != self.current[1]
            or attempt["state"] != state
            or not self.window.plan.start <= (now := self.clock()) < self.window.plan.end
            or now >= lease_until
            or (not_before is not None and now < not_before)
        ):
            raise ValueError("WINDOW_SEND_FENCED")
        if request is not None:
            cumulative.dispatch(self.window, quota, self.current[0], now)
            seal_protocol_content(request, content)

    def complete(self, item, provider_id):
        from .native_actions import AccountAck, NativeAck

        state = "ACK" if provider_id else "UNKNOWN"
        head = self.window.append(state, {"fence": self.current[1]})
        with self.db:
            self.db.execute(
                "UPDATE window_attempts SET state=?,provider_ack_hash=? WHERE message_id=?",
                (state, digest(provider_id.value(item) if isinstance(provider_id, AccountAck)
                               else provider_id.value() if isinstance(provider_id, NativeAck)
                               else provider_id) if provider_id else None, self.current[0]),
            )
            segment = reply_segments.envelope(item)
            if provider_id and (segment is None or segment["index"] == segment["count"] - 1):
                self.db.execute(
                    "UPDATE window_inputs SET state='ACK' WHERE event_id="
                    "(SELECT event_id FROM window_attempts WHERE message_id=?)",
                    (self.current[0],),
                )
            self.db.execute("UPDATE test_window SET journal_head=?", (head,))
        if not provider_id:
            self.window.stop("DELIVERY_UNCERTAIN")
        elif self.window.count("window_attempts") >= min(
            self.window.plan.value["max_attempts"], self.window.plan.value["max_visible_replies"],
            *([] if self.segmented_scope else [self.window.plan.value["max_new_inputs"]]),
        ):
            self.window.stop("WINDOW_LIMIT_REACHED")


class WindowOneBot:
    def __init__(self, operation, protocol):
        self.operation, self.window = operation, operation.window
        self.config = self.window.plan.config
        from .voice_runtime import enabled

        self.voice_scope = enabled(self.window)
        self.companion = (self.window.plan.value.get("live_text", {}).get("companion_scope")
                          == "owner_private_v1")
        self.account_scope = account_actions.enabled(self.window)
        if (
            str(protocol.base_url).rstrip("/") != self.config.onebot_http_url.rstrip("/")
            or protocol.headers.get("Authorization") != "Bearer " + self.config.onebot_token
        ):
            raise ValueError("LIVE_TEXT_ONEBOT_ENDPOINT_CHANGED")
        scoped = checked_http_client(protocol, self.before_dispatch)
        # Ephemeral permission only; persistent delivery_enabled remains false.
        self.delegate = OneBotClient(replace(self.config, delivery_enabled=True), scoped)

    async def before_dispatch(self, request):
        live = self.window.plan.mode == "live_text"
        native = self.operation.current_item.get("action_type") == "qq.private.poke"
        account = account_actions.is_account(self.operation.current_item)
        check_protocol_request(self.window, request, native_poke=native,
            account_action=self.operation.prepared_account.path if account else None)
        if request.url.path in {"/get_login_info", "/get_status", "/get_version_info"}:
            content = request.content
            if decode(content, 64) != {}:
                raise ValueError("WINDOW_PROBE_CHANGED")
            self.operation.before_dispatch(state="RESERVED")
            seal_protocol_content(request, content)
            return
        item, approval = self.operation.current_item, self.operation.approval
        if account:
            from .continuous import flush_recalls

            await flush_recalls(self.window, self.operation.client)
            # A local rejection before the one-shot Core mark can still prove no dispatch.
            self.operation.before_dispatch(request=request)
            check = account_actions.send_check(self.window, item)
            receipt = await self.window.core_post(self.operation.client,
                "/internal/v1/account-actions/send-check", check)
            if (set(receipt) != {"approved", "lease_until", "content_sha256", "payload_sha256"}
                or receipt["approved"] is not True
                or any(receipt[key] != check[key] for key in ("content_sha256", "payload_sha256"))):
                raise ValueError("ACCOUNT_ACTION_SEND_FENCED")
            self.operation.lease_until = datetime.fromisoformat(receipt["lease_until"])
        elif live:
            from .continuous import flush_recalls, interruptible, pending_input

            while True:
                if interruptible(self.window):
                    await flush_recalls(self.window, self.operation.client)
                receipt = await self.window.core_post(
                    self.operation.client, "/internal/v1/live-text/send-check",
                    {key: approval[key] for key in (
                        "window_sha256", "event_id", "message_id",
                        "content_sha256" if native else "reply_sha256",
                    )} | {"lease_token": item["lease_token"]}
                    | ({"reply_revision": item["reply_revision"]}
                       if "reply_revision" in item else {})
                    | ({"voice": self.operation.prepared_voice.check}
                       | {"reply_revision": item["voice"]["binding"]["reply_revision"]}
                       if self.operation.prepared_voice is not None else {}),
                )
                pending = (interruptible(self.window) and self.window.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'",
                ).fetchone() and self.window.db.execute(
                    "SELECT 1 FROM automatic_recalls WHERE window_sha256=? "
                    "AND state='PENDING' LIMIT 1", (self.window.plan.sha256,),
                ).fetchone())
                if not pending:
                    break
            if set(receipt) != {"approved", "lease_until"} or receipt["approved"] is not True:
                raise ValueError("LIVE_TEXT_SEND_FENCED")
            self.operation.lease_until = datetime.fromisoformat(receipt["lease_until"])
            # Core's await is the final asynchronous approval boundary. Rebind
            # the actual pending request as well as the local operation after it.
            if (native or item["action_type"] == "qq.private.proactive"
                or "reply_revision" in item or item.get("delivery_key", "").startswith(
                    "poke-response:")) and (
                pending_input(self.window, reply_segments.event_id(item))
            ):
                raise ValueError("OWNER_INPUT_PENDING")
        if self.operation.meme_prepared:
            self.window.memes.mark_sending(
                item, check=lambda: self.operation.before_dispatch(request=request),
            )
            self.operation.meme_sending = True
        self.operation.before_dispatch(request=request)
        # CheckedTransport invokes its delegate immediately after this hook returns.
        # Once set, even a transport exception cannot establish non-delivery.
        self.operation.dispatched = True

    async def probe(self):
        return await self.delegate.probe()

    async def send_text(self, text):
        self.operation.before_io(text)
        return await self.delegate.send_text(reply_segments.wire_text(self.operation.current_item))

    async def send_content(self, content):
        if self.operation.prepared_voice is not None:
            self.operation.before_io(content["text"])
            return await self.delegate.send_message(self.operation.prepared_voice.message)
        if account_actions.is_account(self.operation.current_item):
            self.operation.before_io(content)
            return await self.delegate.send_account(self.operation.prepared_account,
                                                    self.operation.current_item)
        if self.operation.current_item.get("action_type") == "qq.private.poke":
            self.operation.before_io(content)
            return await self.delegate.send_native(self.operation.prepared_native)
        if "image" not in content:
            return await self.send_text(content["text"])
        if not self.companion:
            raise ValueError("EXPRESSION_SCOPE_REQUIRED")
        item = self.operation.current_item
        message = [{"type": "text", "data": {"text": reply_segments.wire_text(item)}}]
        image = None
        if content["image"]["source"] == "companion.meme_catalog":
            image = self.window.memes.prepare(
                item, check=lambda: self.operation.before_dispatch(state="RESERVED"),
            )
            self.operation.meme_prepared = image is not None
            if image is None:
                self.window.memes.cancel(reply_segments.event_id(item))
        elif self.config.meme_directory:
            from .meme_library import MemeLibrary

            library = MemeLibrary(self.config.meme_directory, enabled=True)
            image = library.prepare(content["image"], item["delivery_key"])
        if image:
            message.append({"type": "image", "data": {
                "file": "base64://" + base64.b64encode(image.data).decode("ascii")}})
        self.operation.prepared_message = message
        self.operation.before_io(content["text"])
        try:
            provider_id = await self.delegate.send_message(message)
        except BaseException:
            if self.operation.meme_sending and self.operation.dispatched:
                self.window.memes.receipt(item, "UNKNOWN")
            raise
        if self.operation.meme_sending:
            self.window.memes.receipt(item, "SENT" if provider_id else "UNKNOWN")
        return provider_id


def check_protocol_request(window, request, *, reference_read=False, file_read=False,
                           native_poke=False, account_action=None):
    config = window.plan.config
    if (
        sum((reference_read, file_read, native_poke, account_action is not None)) > 1
        or (account_action is not None and (account_action not in account_actions.PATHS.values()
                                           or not account_actions.enabled(window)))
        or request.method != "POST" or request.url.path not in (
            {"/get_private_file_url"} if file_read else
            {"/get_login_info", "/get_status", "/get_version_info", account_action}
            if account_action is not None else
            {"/get_login_info", "/get_status", "/get_version_info", "/friend_poke"}
            if native_poke else
            {"/get_msg", "/get_forward_msg"} if reference_read else {
                "/get_login_info", "/get_status", "/get_version_info", "/send_private_msg"
            }
        )
        or not private_request_matches(
            request, config.onebot_http_url, request.url.path,
            synthetic=window.plan.value["execution"] == "local_synthetic",
        )
        or request.headers.get("Authorization") != "Bearer " + config.onebot_token
    ):
        raise ValueError("LIVE_TEXT_ONEBOT_REQUEST_CHANGED")


async def command(config, args):
    if args.command in {"window-open", "window-handoff", "window-abort-handoff",
                        "window-ingest", "window-capture-once", "window-approve-input",
                        "window-send-once", "window-run"}:
        raise ValueError("LEGACY_WINDOW_EXECUTION_RETIRED")
    if args.command in {"window-delivery-prepare", "window-delivery-reconcile"}:
        from .owner_resolution import command as reconcile_command

        return await reconcile_command(config, args)
    if args.command == "window-status":
        return read_status(config)
    if args.command == "window-abort-handoff":
        if args.window is None:
            raise ValueError("WINDOW_REQUIRED")
        plan = stored_plan(encode(private_json(args.window)), config)
        with Spool(config) as spool:
            window = TestWindow(spool, plan)
            async with private_http_client(config.core_url, config.bridge_token) as client:
                return await window.abort_handoff(client)
    if args.command in {"window-stop", "window-quota-inventory"}:
        database = Path(config.data_dir) / "spool.sqlite3"
        if not database.is_file() or database.is_symlink():
            raise ValueError("WINDOW_NOT_FOUND")
        if args.command == "window-stop":
            # This reaches the active receive/send owner without acquiring its lock.
            request_stop(config.data_dir)
        try:
            spool = Spool(config)
        except RuntimeError as error:
            if args.command == "window-stop" and str(error) == "BRIDGE_ALREADY_RUNNING":
                raise ValueError("WINDOW_STOP_PENDING") from None
            raise
        with spool:
            if not spool.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='test_window'"
            ).fetchone():
                raise ValueError("WINDOW_NOT_FOUND")
            value = json.loads(spool.db.execute("SELECT plan_json FROM test_window").fetchone()[0])
            # Stop/status performs no network I/O and does not require unexpired authorization.
            plan = stored_plan(encode(value), config)
            window = TestWindow(spool, plan)
            if args.command == "window-quota-inventory":
                return cumulative.legacy_inventory(window)
            # Always persist the local stop; an unreachable Core is not a confirmed
            # global stop. Report that failure, never claim a paid call was revoked.
            if plan.mode == "live_text":
                async with private_http_client(config.core_url, config.bridge_token) as client:
                    return await window.stop_live(client)
            return window.stop()
    if args.window is None or args.inventory is None:
        raise ValueError("WINDOW_AND_INVENTORY_REQUIRED")
    plan = WindowPlan(
        private_json(args.window),
        config,
        private_json(args.inventory),
        private_json(args.authorization) if args.authorization else None,
    )
    with Spool(config) as spool:
        window = TestWindow(spool, plan)
        async with private_http_client(config.core_url, config.bridge_token) as client:
            if args.command == "window-run":
                from .continuous import run

                async with private_http_client(
                    config.onebot_http_url, config.onebot_token,
                ) as protocol:
                    return await run(window, client, protocol)
            if args.command == "window-open":
                async with private_http_client(
                    config.onebot_http_url, config.onebot_token
                ) as protocol:
                    return await window.open(client, protocol)
            if args.command == "window-handoff":
                return await window.handoff(client)
            if args.command == "window-ingest":
                if args.event is None or args.approval is None:
                    raise ValueError("INPUT_AND_APPROVAL_REQUIRED")
                return await window.ingest(
                    client, private_json(args.event), private_json(args.approval)
                )
            if args.command == "window-approve-input":
                if args.approval is None:
                    raise ValueError("INDIVIDUAL_CAPTURE_APPROVAL_REQUIRED")
                return await window.approve_captured(client, private_json(args.approval))
            if args.command == "window-capture-once":
                async with private_http_client(
                    config.onebot_http_url, config.onebot_token
                ) as protocol:
                    return await window.capture_once(client, protocol)
            if args.command == "window-send-once":
                if args.approval is None:
                    raise ValueError("INDIVIDUAL_REPLY_APPROVAL_REQUIRED")
                async with private_http_client(
                    config.onebot_http_url, config.onebot_token
                ) as protocol:
                    return await window.send_once(client, protocol, private_json(args.approval))
    raise ValueError("INVALID_WINDOW_COMMAND")
