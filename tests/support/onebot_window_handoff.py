import json
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion_onebot_bridge.spool import encode
from companion_onebot_bridge.window import (
    HANDOFF_DOMAIN,
    TestWindow,
    WindowPlan,
    bridge_code_sha256,
    digest,
)
from tests.support.onebot_window import plan_fixture


def plan_for(tmp_path, number=1, *, predecessor=None, approved=False):
    cfg, value, inventory, now = plan_fixture(tmp_path)
    cfg = replace(cfg, capture_signing_key="12" * 32)
    inventory["config_sha256"] = digest(cfg.snapshot())
    value.update(
        config_sha256=inventory["config_sha256"],
        window_id=f"win_{number:032x}",
        journal_path=str(tmp_path / f"window-{number}.jsonl"),
        runtime_attestation_sha256=digest(inventory),
        mode="live_text",
    )
    scope = {
        "live_approved": True,
        "execution": "local_synthetic",
        "window_id": value["window_id"],
        "starts_at": value["starts_at"],
        "ends_at": value["ends_at"],
        "core_config_sha256": "1" * 64,
        "core_code_sha256": "2" * 64,
        "bridge_code_sha256": bridge_code_sha256(),
        "candidate_sha": "a" * 40,
        "paid_contract_sha256": "3" * 64,
        "budget_journal_id": "budget_" + "4" * 32,
        "custodian_sha256": "5" * 64,
        "approval_sha256": "6" * 64,
        "max_inputs": 5,
        "max_model_attempts": 5,
        "max_input_chars": 500,
        "max_reply_chars": 500,
        "capture_public_key": cfg.capture_public_key,
        "history_scope": "none",
        "max_history_turns": 0,
        "max_history_utf8_bytes": 0,
        "memory_scope": "none",
        "memory_facts": [],
        "memory_approval_sha256": None,
        "handoff": None,
    }
    if predecessor:
        scope["handoff"] = {
            "predecessor_plan_sha256": predecessor.plan.sha256,
            "predecessor_window_id": predecessor.plan.value["window_id"],
            "budget_journal_head_sha256": "7" * 64,
            "bridge_journal_head_sha256": predecessor.row()["journal_head"],
            "identity_sha256": value["identity_sha256"],
            "protocol_epoch": cfg.protocol_epoch,
            "privacy_epoch": 0,
            "erasure_ledger_sha256": "8" * 64,
            "evidence_sha256": scope["approval_sha256"],
        }
    value["live_text"] = scope
    authorization = None
    if approved:
        value["execution"] = scope["execution"] = "approved_live"
        authorization = {
            "kind": "owner_qq_window",
            "approved": True,
            "evidence_sha256": "b" * 64,
            "scope_sha256": digest({k: v for k, v in value.items() if k != "authorization_sha256"}),
        }
        value["authorization_sha256"] = digest(authorization)
    return cfg, WindowPlan(value, cfg, inventory, authorization), now


class CoreContract:
    def __init__(self, plan):
        self.current, self.successor = plan, None
        self.calls, self.sent, self.prepared = [], 0, None
        self.failure = None

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((path, body))
        if path == "/internal/v1/ops/status":
            return httpx.Response(
                200,
                json={
                    "mode": "live",
                    "features": {"proactive": False},
                    "jobs": [],
                    "deliveries": [{"state": "SENT", "count": self.sent}],
                    "live_text_window": {
                        "approved": True,
                        "plan_sha256": self.current.sha256,
                        "scope_sha256": digest(self.current.value["live_text"]),
                        "model_attempts": 0,
                    },
                },
            )
        if path.endswith("/handoff"):
            assert body == {"window_sha256": self.successor.sha256}
            if self.failure == "lost_handoff":
                raise httpx.ReadTimeout("synthetic lost receipt")
            scope = self.successor.value["live_text"]["handoff"]
            self.prepared = {
                "state": "PREPARED",
                "window_sha256": self.successor.sha256,
                "predecessor_window_sha256": self.current.sha256,
                "handoff_sha256": digest(scope),
                "budget_journal_head_sha256": "a" * 64,
                "privacy_epoch": scope["privacy_epoch"],
                "erasure_ledger_sha256": scope["erasure_ledger_sha256"],
            }
            self.current = self.successor
            return httpx.Response(200, json=self.prepared)
        if path.endswith("/open"):
            if self.prepared:
                receipt = dict(body["handoff_receipt"])
                signature = receipt.pop("signature")
                assert receipt["core_receipt_sha256"] == digest(self.prepared)
                Ed25519PrivateKey.from_private_bytes(
                    bytes.fromhex(self.current.config.capture_signing_key)
                ).public_key().verify(
                    bytes.fromhex(signature), HANDOFF_DOMAIN + encode(receipt).encode()
                )
            if self.failure == "lost_open":
                raise httpx.ReadTimeout("synthetic lost open receipt")
            return httpx.Response(200, json={"opened": True})
        if path.endswith("/stop"):
            return httpx.Response(200, json={"stopped": True})
        if path.endswith("/inputs/approve"):
            return httpx.Response(200, json={"approved": True})
        if path == "/internal/v1/inbox/events":
            return httpx.Response(
                202,
                json={
                    "event_id": body["event_id"],
                    "job_id": "job_synthetic",
                    "duplicate": False,
                    "state": "SUCCEEDED",
                },
            )
        pytest.fail("unexpected Core operation")

    def client(self):
        return httpx.AsyncClient(
            base_url=self.current.config.core_url,
            headers={"Authorization": "Bearer " + self.current.config.bridge_token},
            transport=httpx.MockTransport(self),
        )


async def source(spool, plan, now, http):
    old = TestWindow(spool, plan, clock=lambda: now)
    await old.open(http)
    await old.stop_live(http)
    return old
