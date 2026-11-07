"""Native planner and Core-owned meme offer share the actual paid/delivery path."""

import json

import pytest

from companion import context_planner as planner
from companion.paid_budget import sha
from companion.token_measurement import measure_payload
from tests.support.meme_core_process import (
    check_signed_core_worker_and_bridge_delivery as signed_meme,
)
from tests.support.meme_core_runtime import runtime_case as runtime_case


@pytest.mark.parametrize("runtime_case", ["no_current"], indirect=True)
def test_native_planner_binds_core_offer_actual_wire_and_ack(runtime_case, tmp_path):
    c = runtime_case
    c.core.settings.context_soft_tokens = 1000
    # Reuse the full signed HTTP ingress, independent worker/provider and Bridge
    # process case, including original image/source, settlement and OneBot ACK.
    signed_meme(c, tmp_path)

    job = c.core.store.one(
        "SELECT j.* FROM jobs j JOIN outbox o ON o.origin_event_id=j.event_id "
        "WHERE j.kind='chat' AND o.state='SENT'"
    )
    saved = c.core.store.one(
        "SELECT * FROM agent_conversations WHERE job_id=?", (job["job_id"],)
    )
    assert saved["phase"] == "COMPLETE"
    snapshots = json.loads(saved["snapshots_json"])
    plan = planner.check(c.core, job, snapshots)
    assert plan is not None and plan["pending"] is None
    assert len(plan["requests"]) == 1
    record = plan["requests"][0]
    payload = json.loads((tmp_path / "result.json").read_text("utf-8"))["calls"][0]
    measured = measure_payload(c.core.settings, payload, "chat", tools=payload.get("tools", ()))
    assert record["state"] == "ADMITTED"
    assert record["payload_sha256"] == sha(payload) == measured.payload_sha256
    assert record["measurement_sha256"] == sha(measured.receipt())
    # This grant has no optional history/memory. Under soft pressure, the native
    # planner must keep the required rules (including the offer) and current input.
    assert measured.reserved_input_tokens > c.core.settings.context_soft_tokens
    assert not record["dropped"] and all(group["required"] for group in plan["groups"])
    assert {group["kind"] for group in plan["groups"]} == {"rules", "current"}
    proofs = [item for item in snapshots if item["kind"] != "context_plan"]
    assert record["source_proof_count"] == plan["initial_source_proof_count"] == len(proofs)
    assert record["source_proofs_sha256"] == plan["initial_source_proofs_sha256"] == sha(proofs)
    usage = c.core.store.one(
        "SELECT * FROM model_usage WHERE request_id=?", (record["request_id"],)
    )
    assert usage["state"] == "SETTLED" and usage["job_id"] == job["job_id"]
    admission = c.core.store.one(
        "SELECT * FROM model_admissions WHERE request_id=?", (record["request_id"],)
    )
    assert json.loads(admission["measurement_json"]) == measured.receipt()
    assert admission["payload_sha256"] == sha(payload)
    offer = json.loads(c.core.store.one(
        "SELECT value FROM runtime_meta WHERE key=?", ("meme_offer:" + job["event_id"],)
    )["value"])
    candidate = c.item["asset_sha256"]
    assert offer["catalog_owner"] == "core" and offer["binding_version"] == 2
    assert offer["catalog_bindings"][candidate]["caption_job_id"] == c.caption_id
    assert offer["catalog_bindings"][candidate]["source_sha256"] == c.item["source_sha256"]
    assert candidate in json.dumps(payload["messages"], ensure_ascii=False)
    assert payload["max_tokens"] == c.core.settings.output_tokens("chat")
