"""Run a prepared round: lifecycle checks, both native arms, the RAG reference, any
zero-material controls (round.json "controls"), answer freeze.

  python run.py --round DIR            fresh start (DeepSeek ledger must not exist)
  python run.py --round DIR --resume   continue after native completion (RAG / freeze only)

Native inference itself is never resumed: a stopped native run needs a new round.
"""

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
from filelock import FileLock  # noqa: E402
from scripts.memory_benchmark.budget import Budget  # noqa: E402
from scripts.memory_benchmark.cli import closure, environment, execute, validate_run  # noqa: E402

from roundcfg import TOOLS, Round, ledger_total, read_rows, save, sha  # noqa: E402

R = Round()
ROOT = R.root


def state(status, **details):
    save(ROOT / "pipeline-status.json", {"status": status, "at": datetime.now(UTC).isoformat(),
                                         "pid": os.getpid(), **details})


def lifecycle(config):
    run = Path(config["run_dir"])
    temporary = Path(R.cfg["workspace"]) / ".local/test-temp" / f"{R.name}-{config['campaign_arm']}"
    temporary.mkdir(parents=True, exist_ok=True)
    execute(config, ["-m", "pytest",
                     str(Path(config["source_snapshot"]) / "tests/_benchmark_lifecycle_checks.py"),
                     "-q", "--junitxml=" + str(run / "lifecycle.xml"),
                     "--basetemp=" + str(temporary / "lifecycle"),
                     "-o", "cache_dir=" + str(temporary / "pytest-cache")], "lifecycle.log")
    tree = ET.parse(run / "lifecycle.xml")
    assert len(list(tree.iter("testcase"))) == 18
    assert not any(list(tree.iter(tag)) for tag in ("failure", "error", "skipped"))
    shutil.rmtree(temporary, ignore_errors=True)


def native(configs):
    state("LIFECYCLE_CHECKS")
    for config in configs.values():
        lifecycle(config)
    state("RUNNING_NATIVE_INFERENCE")
    codes = {}
    # One arm at a time: otherwise local embedding/source checks compete for CPU
    # and the before-provider timings measure this harness's contention too.
    for arm, config in configs.items():
        with (R.arm_dir(arm) / "inference.log").open("a", encoding="utf-8") as log:
            worker = subprocess.Popen(
                [sys.executable, "-B", "-m", "scripts.memory_benchmark.worker"], cwd=ROOT,
                env=environment(config), stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            state("RUNNING_NATIVE_INFERENCE", arm=arm, worker=worker.pid, schedule="serial")
            codes[arm] = worker.wait()
    save(ROOT / "native-run-completion.json", {"exit_codes": codes,
                                               "at": datetime.now(UTC).isoformat()})
    for config in configs.values():
        closure(config)


def completed(name):
    path = ROOT / name / "manifest.json"
    return path.exists() and json.loads(path.read_text("utf-8"))["status"] == "COMPLETED"


def rag(name="rag"):
    """The RAG reference, or a zero-material control answered by the same generator."""
    state("RUNNING_QWEN_RAG" if name == "rag" else "RUNNING_CONTROL_" + name.upper())
    extra = [] if name == "rag" else ["--control", name]
    with (ROOT / f"{name}.log").open("a", encoding="utf-8") as log:
        result = subprocess.run([sys.executable, "-B", str(TOOLS / "rag.py"), "--round", str(ROOT),
                                 *extra], stdout=log, stderr=subprocess.STDOUT, check=False)
    assert result.returncode == 0, f"{name} needs inspection; rerun with --resume when fixed"


def freeze_answers(configs):
    for name in ("rag", *R.controls):
        assert completed(name), name
        answers = read_rows(ROOT / name / "answers.jsonl")
        assert len(answers) == len({a["id"] for a in answers}) == R.total
        assert {a["id"] for a in answers} == {q["id"] for q in R.questions}
    held, latest = Budget.from_context(next(iter(configs.values()))).snapshot()
    assert held <= R.cfg["budget"]["deepseek_total_cny"]
    assert not any(v["state"] == "RESERVED" for v in latest.values())
    qwen, qlatest = ledger_total(R.qwen_ledger)
    assert qwen <= R.cfg["budget"]["qwen_total_cny"]
    assert not any(v["state"] == "RESERVED" for v in qlatest.values())
    files = [p for a in configs for p in (R.arm_dir(a) / "results").glob("*/answers.jsonl")]
    files += [ROOT / name / "answers.jsonl" for name in ("rag", *R.controls)]
    save(ROOT / "all-answers-frozen.json", {
        "status": "ALL_ANSWERS_CLOSED_BEFORE_GOLD_READ", "answers": R.answers_total,
        "files_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in files},
        "corpus_freeze_sha256": sha(ROOT / "corpus-freeze.json"),
        "charged_or_held_cny": held, "qwen_charged_or_held_cny": qwen,
        "failures_kept_in_denominator": True, "at": datetime.now(UTC).isoformat()})
    state("READY_FOR_BLIND_GRADING", answers_per_arm=R.total, deepseek_charged_or_held_cny=held)


def main():
    resume = "--resume" in sys.argv
    with FileLock(str(ROOT / "campaign.lock"), timeout=0):
        frozen = json.loads((ROOT / "execution-freeze.json").read_text("utf-8"))
        for name, expected in frozen["files_sha256"].items():
            assert sha(ROOT / name) == expected, "Execution freeze changed: " + name
        configs = {a: validate_run(R.arm_dir(a)) for a in R.cfg["arms"]}
        try:
            if not resume:
                assert not R.deepseek_ledger.exists(), "Fresh round must start at zero spend"
                save(ROOT / "run-started.json", {"pid": os.getpid(),
                                                 "at": datetime.now(UTC).isoformat()})
                native(configs)
            else:
                assert (ROOT / "native-run-completion.json").exists(), (
                    "Native inference never resumes; start a new round")
            for name in ("rag", *R.controls):
                if not completed(name):
                    rag(name)
            freeze_answers(configs)
        except BaseException as error:
            state("NEEDS_ATTENTION", error_type=type(error).__name__, error=str(error)[:800])
            raise


if __name__ == "__main__":
    main()
