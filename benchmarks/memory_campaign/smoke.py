"""Plumbing smoke of the new arm on an already-exposed bank (never the round's own bank).

  python smoke.py --round DIR

Uses round.json "smoke": {"bank": <exposed bank dir>, "persona", "questions", "budget_cny"}.
Writes <round>/smoke/ with its own ledger; its spend counts toward the round's total.
"""

import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
from scripts.memory_benchmark.cli import configuration, execute, freeze  # noqa: E402

from prepare import write_configs  # noqa: E402
from roundcfg import Round, read_rows  # noqa: E402

R = Round()


def main():
    smoke_cfg = R.cfg["smoke"]
    if not smoke_cfg["bank"]:
        raise SystemExit('Set "smoke": {"bank": ...} to an already-exposed bank')
    bank = Path(smoke_cfg["bank"]).resolve()
    assert bank != R.bank, "Smoke must not touch the round's own bank"
    out = R.root / "smoke"
    out.mkdir(exist_ok=False)
    written = write_configs(R)
    toml = written["new"].read_text("utf-8")
    for leftover in written.values():
        leftover.unlink()
    budget = R.cfg["budget"]
    toml = toml.replace(f'fixture_root = "{R.bank.as_posix()}"', f'fixture_root = "{bank.as_posix()}"')
    toml = toml.replace(f'campaign_budget_path = "{R.deepseek_ledger.as_posix()}"',
                        f'campaign_budget_path = "{(out / "deepseek-usage.jsonl").as_posix()}"')
    toml = toml.replace(f"total_cny = {budget['deepseek_total_cny']!r}\n"
                        f"inference_cny = {budget['deepseek_inference_cny']!r}",
                        f"total_cny = {smoke_cfg['budget_cny']!r}\n"
                        f"inference_cny = {smoke_cfg['budget_cny'] * 0.8!r}")
    assert bank.as_posix() in toml and f"total_cny = {smoke_cfg['budget_cny']!r}\n" in toml
    (out / "smoke.toml").write_text(toml, "utf-8")
    config = configuration(out / "smoke.toml")
    questions = sorted((q for q in read_rows(bank / "suite-v1/questions.jsonl")),
                       key=lambda q: (q["now"], q["id"]))
    persona = smoke_cfg["persona"] or sorted({q["persona"] for q in questions})[0]
    chosen = [q["id"] for q in questions if q["persona"] == persona][:smoke_cfg["questions"]]
    config.update(selected_ids=chosen, personas=[persona])
    config = freeze(config, out / "arm")
    execute(config, ["-m", "scripts.memory_benchmark.worker"], "inference.log")
    answers = read_rows(next((out / "arm/results").glob("*/answers.jsonl")))
    print(json.dumps({"persona": persona, "answers": len(answers),
                      "succeeded": sum(a["status"] == "SUCCEEDED" for a in answers)}))


if __name__ == "__main__":
    main()
