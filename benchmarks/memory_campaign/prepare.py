"""Freeze the source before authoring, then prepare both Core arms. No paid calls.

  python prepare.py source --round DIR   record arm commits/settings (before the bank)
  python prepare.py arms --round DIR     write arm configs, probe the parser, freeze arms
"""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
from scripts.memory_benchmark.cli import configuration, freeze, parser_probe, validate_run  # noqa: E402

from roundcfg import TOOLS, Round, save, sha


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def table(name, values):
    return f"[{name}]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in values.items())


def write_configs(r):
    cfg, paths = r.cfg, {}
    budget = cfg["budget"]
    for arm, commit in cfg["arms"].items():
        top = {"workspace": cfg["workspace"], "source_checkout": cfg["source_checkout"],
               "fixture_root": r.bank.as_posix(), "embedding_cache_dir": cfg["embedding_cache_dir"],
               "revision": commit, "preset": "full", "concurrency": cfg["concurrency"],
               "campaign_arm": arm, "campaign_budget_path": r.deepseek_ledger.as_posix(),
               "telemetry": cfg["telemetry"]}
        content = "".join(f"{k} = {toml_value(v)}\n" for k, v in top.items()) + "\n" + "\n".join([
            table("provider", cfg["provider"]), table("credentials", cfg["credentials"]),
            table("budget", {"total_cny": budget["deepseek_total_cny"],
                             "inference_cny": budget["deepseek_inference_cny"]}),
            table("pricing", cfg["pricing"]), table("native", cfg["native"]),
            table("history_grant", cfg["history_grant"]), table("parser", cfg["parser"])])
        paths[arm] = r.root / f"{arm}.toml"
        paths[arm].write_text(content, "utf-8")
    return paths


def source(r):
    if (r.root / "source-freeze.json").exists():
        raise SystemExit("source-freeze.json exists; a new source means a new round")
    commits = {arm: subprocess.check_output(
        ["git", "-C", r.cfg["source_checkout"], "rev-parse", commit + "^{commit}"],
        text=True).strip() for arm, commit in r.cfg["arms"].items()}
    save(r.root / "source-freeze.json", {
        "status": "SOURCE_AND_SETTINGS_FROZEN_BEFORE_BANK", "at": datetime.now(UTC).isoformat(),
        "arms": commits, "settings": {k: r.cfg[k] for k in (
            "native", "history_grant", "provider", "pricing", "rag", "grading")},
        "round_json_sha256": sha(r.root / "round.json"), "provider_calls": 0})
    print(json.dumps(commits, indent=1))


def arms(r):
    if (r.root / "campaign-freeze.json").exists():
        raise SystemExit("Arms already prepared for this round")
    if not (r.bank / "bundle-manifest.json").exists():
        raise SystemExit("Freeze the bank first (bank.py freeze)")
    configs = {arm: configuration(path) for arm, path in write_configs(r).items()}
    for arm, config in configs.items():
        assert config["personas"] == r.personas, arm
        assert len(config["selected_ids"]) == r.total, arm
    parser_probe(next(iter(configs.values())))
    for arm, config in configs.items():
        run = r.arm_dir(arm)
        freeze(config, run)
        validate_run(run)
        print("prepared", arm, len(config["selected_ids"]), config["source_commit"], flush=True)
    tooling = {p.relative_to(TOOLS).as_posix(): sha(p) for p in TOOLS.rglob("*.py")
               if "__pycache__" not in p.parts}
    save(r.root / "campaign-freeze.json", {
        "status": "PREPARED_NOT_STARTED", "at": datetime.now(UTC).isoformat(),
        "source_freeze_sha256": sha(r.root / "source-freeze.json"),
        "corpus_freeze_sha256": sha(r.root / "corpus-freeze.json"),
        "round_json_sha256": sha(r.root / "round.json"), "tooling_sha256": tooling,
        "arm_freeze_sha256": {a: sha(r.arm_dir(a) / "freeze.json") for a in configs},
        "answers_per_arm": r.total, "paid_calls": 0})
    files = ["campaign-freeze.json", "source-freeze.json", "corpus-freeze.json", "round.json"]
    save(r.root / "execution-freeze.json", {
        "status": "EXECUTION_FROZEN_BEFORE_FIRST_PROVIDER_CALL",
        "at": datetime.now(UTC).isoformat(),
        "files_sha256": {name: sha(r.root / name) for name in files}})


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    r = Round()
    {"source": source, "arms": arms}.get(command, lambda _: sys.exit(__doc__))(r)


if __name__ == "__main__":
    sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
    main()
