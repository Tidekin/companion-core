"""Offline checks for the authorized three-arm shared ceiling; no provider calls."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.memory_benchmark.budget import Budget, MeteredTransport
from scripts.memory_benchmark.cli import closure


def config(tmp_path, arm):
    return {
        "run_dir": str(tmp_path / arm),
        "campaign_budget_path": str(tmp_path / "shared.jsonl"),
        "provider": {"base_url": "https://api.deepseek.com"},
        "pricing": {
            "input_rate": 2,
            "cached_input_rate": 0.04,
            "output_rate": 8,
            "safety_factor": 1.5,
        },
        "concurrency": 1,
        "budget": {"total_cny": 50, "inference_cny": 47.5},
    }


def test_shared_arms_keep_unknown_and_reserve_judge_headroom(tmp_path):
    old, new, rag, judge = [
        Budget.from_context(config(tmp_path, a)) for a in ("old", "new", "rag", "judge")
    ]
    old.append(
        {
            "id": "retained-failure",
            "state": "UNKNOWN",
            "phase": "inference:old",
            "charged_or_held_cny": 47.499,
        }
    )
    for arm in (old, new, rag):
        with pytest.raises(RuntimeError, match="CNY_CAP"):
            arm.reserve({"messages": [], "max_tokens": 1}, "inference")
    request, maximum = judge.reserve({"messages": [], "max_tokens": 1}, "judge")
    assert new.snapshot()[1][request]["state"] == "RESERVED"
    assert judge.snapshot()[1]["retained-failure"]["state"] == "UNKNOWN"
    judge.unknown(request, maximum, "transport")
    assert old.snapshot()[0] == 47.499 + maximum


@pytest.mark.parametrize("cap", [50, 80])
def test_cap_is_total_not_per_arm_and_new_round_starts_empty(tmp_path, cap):
    config_old, config_new = config(tmp_path, "old"), config(tmp_path, "new")
    for settings in (config_old, config_new):
        settings["budget"] = {"total_cny": cap, "inference_cny": cap - 2.5}
    old, new = Budget.from_context(config_old), Budget.from_context(config_new)
    assert old.snapshot() == (0, {})
    old.append(
        {
            "id": "previous-new-round-calls",
            "state": "SETTLED",
            "phase": "inference",
            "charged_or_held_cny": cap - .001,
        }
    )
    with pytest.raises(RuntimeError, match="CNY_CAP"):
        new.reserve({"messages": [], "max_tokens": 1}, "judge")
    assert not (tmp_path / "old" / "usage.jsonl").exists()
    assert not (tmp_path / "new" / "usage.jsonl").exists()


def test_http_failure_keeps_money_visible_to_other_arm(tmp_path):
    async def run():
        meter = Budget.from_context(config(tmp_path, "rag"))
        calls = []

        async def provider(request):
            calls.append(request)
            return httpx.Response(502, json={"error": "fixture"})

        transport = MeteredTransport(meter, "inference:rag", inner=httpx.MockTransport(provider))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                json={"messages": [], "max_tokens": 768},
            )
            assert response.status_code == 502
        spent, rows = Budget.from_context(config(tmp_path, "new")).snapshot()
        assert len(calls) == 1
        assert spent > 0
        assert [v["state"] for v in rows.values()] == ["UNKNOWN"]

    asyncio.run(run())


def test_non_p_personas_are_audited_and_missing_answer_refused(tmp_path):
    cfg = config(tmp_path, "old")
    run = Path(cfg["run_dir"])
    cfg.update(
        personas=["N01", "N02"], selected_ids=["N01-K01/d00", "N02-K01/d00"], campaign_arm="old"
    )
    for persona in cfg["personas"]:
        directory = run / "results" / persona
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({"status": "COMPLETED"}), "utf-8")
        (directory / "answers.jsonl").write_text(
            json.dumps({"id": persona + "-K01/d00", "status": "SUCCEEDED"}) + "\n", "utf-8"
        )
        with sqlite3.connect(directory / "core.sqlite3") as db:
            db.execute("CREATE TABLE model_usage (state TEXT)")
    meter = Budget.from_context(cfg)
    meter.append(
        {
            "id": "other-arm-unknown",
            "state": "UNKNOWN",
            "phase": "inference:new:N01:exam",
            "charged_or_held_cny": 0.3,
        }
    )
    closure(cfg)
    frozen = json.loads((run / "answers-frozen.json").read_text("utf-8"))
    assert len(frozen) == 2
    assert meter.snapshot()[0] == 0.3
    (run / "results/N02/answers.jsonl").write_text("", "utf-8")
    with pytest.raises(RuntimeError, match="coverage is incomplete"):
        closure(cfg)
