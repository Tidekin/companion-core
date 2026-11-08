import copy
import json
import tomllib
from pathlib import Path

import pytest

from companion.config import Settings
from scripts.verify_p4_gates import REQUIRED, validate


def evidence():
    return json.loads(
        (Path(__file__).resolve().parents[1] / "docs/design-v0.6/p4-gates.json").read_text("utf-8")
    )


def test_incomplete_evidence_remains_a_valid_blocked_report():
    gates = evidence()
    assert not gates["production_ready"]
    assert "signoff" in validate(gates)


@pytest.mark.parametrize(
    "change",
    [
        {"production_ready": True},
        {"production_ready": "false"},
        {"real_qq_private_messages_accepted": True},
        {
            "real_diary_reviews": [
                {"id": "simulated-day", "kind": "SYNTHETIC", "evidence": "fixture"}
            ]
        },
    ],
)


def test_gate_rejects_false_readiness_or_synthetic_human_acceptance(change):
    gates = copy.deepcopy(evidence())
    gates.update(change)
    with pytest.raises(ValueError):
        validate(gates)


def test_twenty_ids_on_one_day_cannot_replace_twenty_real_diary_dates():
    gates = evidence()
    gates.update({key: "PASS" for key in REQUIRED})
    gates.update(
        production_ready=True,
        signoff="GRANTED",
        real_qq_private_messages_accepted=20,
        real_diary_distinct_dates_reviewed=20,
    )
    gates["real_qq_acceptance"] = [
        {"id": str(n), "kind": "LIVE_HUMAN_ACCEPTED", "evidence": "private-reviewed-trace"}
        for n in range(20)
    ]
    gates["real_diary_reviews"] = [
        {**row, "local_date": "2026-09-01", "timezone": "Asia/Shanghai"}
        for row in gates["real_qq_acceptance"]
    ]
    with pytest.raises(ValueError, match="differs"):
        validate(gates)
    for n, row in enumerate(gates["real_diary_reviews"], 1):
        row["local_date"] = f"2026-09-{n:02}"
    assert validate(gates) == []  # Synthetic unit-test records; never saved as real evidence.


def test_rehearsal_configuration_is_valid_and_has_no_model_or_sending_authority():
    path = Path(__file__).resolve().parents[1] / "docs/design-v0.6/p4-rehearsal.example.toml"
    cfg = Settings.model_validate(tomllib.loads(path.read_text("utf-8")))
    assert cfg.mode == "demo" and not cfg.model_key.get_secret_value()
    assert cfg.paid_model is None
    assert not cfg.proactive_enabled and not cfg.diary_auto_share
