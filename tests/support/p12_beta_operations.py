import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def feedback(kind="local_synthetic"):
    record = json.loads((ROOT / "docs/qq-beta/p12-beta-feedback.template.json").read_text("utf-8"))
    record["evidence_type"] = kind
    # Fabricated references for schema tests, never an approval receipt.
    record["authorization_refs"].update(R3="d" * 64, R4="e" * 64)
    record["reason_private"] = "PRIVATE_REASON_NEVER_PRINT"
    return record


def write(path, value):
    raw = json.dumps(value, ensure_ascii=False).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def bundle(directory, entries):
    manifest = {"schema_version": 1, "timezone": "Asia/Shanghai", "samples": []}
    for index, (record, report) in enumerate(entries):
        name = f"sample-{index}"
        if report is not None:
            record["inspector_sha256"] = write(directory / (name + ".inspector.json"), report)
            record["trace_ref_sha256"] = report["trace_ref_sha256"]
            record["candidate_sha"] = report["configured_candidate_sha"]
        write(directory / (name + ".feedback.json"), record)
        manifest["samples"].append({"feedback": name + ".feedback.json",
                                    "inspector": name + ".inspector.json" if report else None})
    path = directory / "observations.json"
    write(path, manifest)
    return path


def inspector(index, when="2026-10-21T16:01:00+00:00", state="SETTLED"):
    # Public-shaped synthetic accounting, not platform or provider evidence.
    ref = hashlib.sha256(str(index).encode()).hexdigest()
    settled = state == "SETTLED"
    call = {"request_ref_sha256": ref, "logical_request_ref_sha256": ref,
            "state": state, "paid_dispatch_recorded": True,
            "prompt_tokens": 10 if settled else None,
            "prompt_cache_hit_tokens": 4 if settled else None,
            "completion_tokens": 3 if settled else None,
            "configured_price_micro_usd": 5 if settled else None,
            "accounted_or_reserved_micro_usd": 5 if settled else 10,
            "reservation_micro_usd": 10}
    return {"schema_version": 1, "read_only": True, "diagnostic_only": True,
            "configured_approval_sha256": "d" * 64,
            "trace_ref_sha256": ref, "configured_candidate_sha": "c" * 40, "blockers": [],
            "trace": {"state": "RECORDED", "event_ref_sha256": ref,
                      "recorded_window": {
                          "state": "RECORDED", "plan_sha256": "a" * 64,
                          "approval_sha256": "d" * 64, "candidate_sha": "c" * 40,
                          "execution": "approved_live",
                      },
                      "event_occurred_at_utc": when, "outbox": {"SENT": 1},
                      "jobs": [{"model_calls": [call], "model_attempt_count": 1}]}}
