"""Prepare, run and inspect repeatable native memory evaluations."""

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tomllib
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock

from scripts.memory_benchmark.budget import Budget
from scripts.memory_benchmark.runtime import credential

REPOSITORY = Path(__file__).resolve().parents[2]
NATIVE_OPTIONS = {
    "context_soft_tokens",
    "max_output_tokens",
    "memory_extract_max_output_tokens",
    "chat_thinking",
    "chat_thinking_tokens",
    "timeout_seconds",
    "lease_seconds",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def read_rows(path):
    return [json.loads(s) for s in path.read_text("utf-8").splitlines()] if path.exists() else []


def state(run, name, **extra):
    save(run / "status.json", {"status": name, "at": datetime.now(UTC).isoformat(), **extra})


def git(source, *arguments):
    return subprocess.check_output(["git", "-C", str(source), *arguments], encoding="utf-8").strip()


def under_workspace(path, workspace):
    path = Path(path).resolve()
    if not path.is_relative_to(workspace):
        raise ValueError("Generated and source paths must stay inside configured workspace")
    return path


def configuration(path, preset=None):
    config = tomllib.loads(Path(path).read_text("utf-8-sig"))
    workspace = Path(config.get("workspace", REPOSITORY)).resolve()
    source = under_workspace(config.get("source_checkout", REPOSITORY), workspace)
    suite = under_workspace(config.get("fixture_root", REPOSITORY / "benchmarks/memory"), workspace)
    config.update(workspace=str(workspace), source_checkout=str(source), fixture_root=str(suite))
    config["embedding_cache_dir"] = str(
        under_workspace(
            config.get("embedding_cache_dir", workspace / "data/embedding-cache"), workspace
        )
    )
    config["source_commit"] = git(
        source, "rev-parse", "--verify", config.get("revision", "HEAD") + "^{commit}"
    )
    config["preset"] = preset or config.get("preset", "full")
    if config["preset"] not in ("full", "smoke"):
        raise ValueError("Preset must be full or smoke")
    config.setdefault("concurrency", 4)
    if type(config["concurrency"]) is not int or not 1 <= config["concurrency"] <= 4:
        raise ValueError("Concurrency must be1..4")
    config.setdefault("budget", {"total_cny": 30, "inference_cny": 27.5})
    total, inference = config["budget"]["total_cny"], config["budget"]["inference_cny"]
    if (
        not all(type(v) in (int, float) and math.isfinite(v) for v in (total, inference))
        or not 0 < inference < total <= 80
    ):
        raise ValueError(
            "Budget must satisfy0<inference<total<=80CNY (this round explicitly authorized)"
        )
    price = config["pricing"]
    if not all(
        type(price[k]) in (int, float) and math.isfinite(price[k]) and price[k] > 0
        for k in ("input_rate", "cached_input_rate", "output_rate", "safety_factor")
    ):
        raise ValueError("Prices and safety_factor must be finite and positive")
    if price["safety_factor"] < 1:
        raise ValueError("Safety factor cannot use a discount to expand the budget")
    if price["cached_input_rate"] > price["input_rate"]:
        raise ValueError("Cached price must not exceed the input reservation rate")
    provider = config["provider"]
    url = urlparse(provider["base_url"].rstrip("/"))
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Provider requires an HTTPS base URL without credentials/query")
    provider["base_url"] = provider["base_url"].rstrip("/")
    if provider.get("adapter", "deepseek") != "deepseek":
        raise ValueError("This fixture driver currently uses the native DeepSeek adapter")
    provider["adapter"] = "deepseek"
    if any(k in provider for k in ("key", "api_key", "token")):
        raise ValueError("Use credential paths, never inline keys")
    location = config["credentials"]
    if bool(location.get("key_file")) == bool(location.get("env_file")):
        raise ValueError("Select either key_file or env_file")
    for key in ("key_file", "env_file"):
        if location.get(key):
            location[key] = str(Path(location[key]).resolve())
    if location.get("env_file"):
        location.setdefault("env_name", "COMPANION_MODEL_KEY")
    credential(config)  # Format/read check only; no network or credential output.
    if set(config.get("native", {})) - NATIVE_OPTIONS:
        raise ValueError("Only model/context/timeout native overrides are allowed")
    parser = config["parser"]
    if parser["transport"] not in ("docker", "ssh") or not parser["image"].startswith("sha256:"):
        raise ValueError("Parser requires docker/ssh and an immutable local image ID")
    if parser["transport"] == "ssh":
        parser["known_hosts_file"] = str(Path(parser["known_hosts_file"]).resolve())
        if not Path(parser["known_hosts_file"]).is_file():
            raise ValueError("SSH known_hosts file is required")
        if parser["host"].startswith("-") or not 1 <= parser["port"] <= 65535:
            raise ValueError("Invalid SSH target")
    # git show text decoding/newline normalization is intentionally not used for the digest.
    raw = subprocess.check_output(
        ["git", "-C", str(source), "show", config["source_commit"] + ":companion/file_parser.py"]
    )
    if hashlib.sha256(raw).hexdigest() != parser["source_sha256"]:
        raise ValueError(
            "Parser image source does not match the selected Core revision; rebui"
            "ld its native image"
        )
    bundle = json.loads((suite / "bundle-manifest.json").read_text("utf-8"))
    for name, digest in bundle["files_sha256"].items():
        item = under_workspace(suite / name, suite)
        if sha(item) != digest:
            raise ValueError("Frozen fixture hash mismatch: " + name)
    questions = [
        q for q in read_rows(suite / "suite-v1/questions.jsonl") if q["kind"] == "semantic_memory"
    ]
    if len(questions) != bundle["semantic"] or len({q["id"] for q in questions}) != len(questions):
        raise ValueError("Invalid semantic question coverage")
    if config["preset"] == "smoke":
        groups = {"P02-C01", "P02-C02", "P02-C09", "P02-C10", "P02-C17", "P02-C23"}
        questions = [q for q in questions if q["group_id"] in groups]
        if len(questions) != 24:
            raise ValueError("Smoke requires the frozen24instances")
    config["selected_ids"] = [q["id"] for q in questions]
    config["personas"] = sorted({q["persona"] for q in questions})
    config["suite_dir"], config["asr_file"] = (
        str(suite / "suite-v1"),
        str(suite / "asr-observations.jsonl"),
    )
    config["bundle_manifest_sha256"] = sha(suite / "bundle-manifest.json")
    all_dates = [
        datetime.fromisoformat(q["now"])
        for q in read_rows(suite / "suite-v1/questions.jsonl")
        if q.get("now")
    ]
    config["logical_starts_at"] = min(
        datetime.fromisoformat(r["now"])
        for p in (suite / "suite-v1/inputs").glob("*.jsonl")
        for r in read_rows(p)
    ).isoformat()
    config["logical_expires_at"] = (max(all_dates) + timedelta(days=2)).isoformat()
    return config


def parser_probe(config):
    arguments = ["docker", "image", "inspect", "--format", "{{.Id}}", config["parser"]["image"]]
    if config["parser"]["transport"] == "ssh":
        import shlex

        p = config["parser"]
        arguments = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "UserKnownHostsFile=" + p["known_hosts_file"],
            "-p",
            str(p["port"]),
            p["host"],
            shlex.join(arguments),
        ]
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != config["parser"]["image"]:
        raise RuntimeError("Pinned native parser image is unavailable")


def freeze(config, run):
    run.mkdir(parents=True, exist_ok=False)
    source = run / "source"
    source.mkdir()
    archive = subprocess.check_output(
        ["git", "-C", config["source_checkout"], "archive", config["source_commit"]]
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, filter="data")
    tooling = run / "tooling/scripts/memory_benchmark"
    tooling.parent.mkdir(parents=True)
    shutil.copytree(Path(__file__).parent, tooling, ignore=shutil.ignore_patterns("__pycache__"))
    (tooling.parent / "__init__.py").write_text("", "utf-8")
    # Test-only overlay reuses the existing eighteen native lifecycle cases.
    lifecycle = source / "tests/_benchmark_lifecycle_checks.py"
    shutil.copyfile(REPOSITORY / "tests/test_memory_benchmark_lifecycle.py", lifecycle)
    config.update(
        run_id=run.name,
        run_dir=str(run),
        source_snapshot=str(source),
        tooling_root=str(run / "tooling"),
    )
    save(run / "run.json", config)
    hashes = {
        str(p.relative_to(run)): sha(p)
        for directory in (source, run / "tooling")
        for p in directory.rglob("*")
        if p.is_file()
    }
    hashes["run.json"] = sha(run / "run.json")
    hashes["fixture-bundle"] = config["bundle_manifest_sha256"]
    save(run / "freeze.json", hashes)
    return config


def validate_run(run):
    config = json.loads((run / "run.json").read_text("utf-8"))
    for name, digest in json.loads((run / "freeze.json").read_text("utf-8")).items():
        path = (
            Path(config["fixture_root"]) / "bundle-manifest.json"
            if name == "fixture-bundle"
            else run / name
        )
        if sha(path) != digest:
            raise ValueError("Frozen run modified: " + name)
    bundle = json.loads((Path(config["fixture_root"]) / "bundle-manifest.json").read_text("utf-8"))
    for name, digest in bundle["files_sha256"].items():
        if sha(Path(config["fixture_root"]) / name) != digest:
            raise ValueError("Frozen fixture modified: " + name)
    return config


def environment(config):
    return dict(
        os.environ,
        COMPANION_MEMORY_BENCHMARK_RUN=str(Path(config["run_dir"]) / "run.json"),
        PYTHONPATH=os.pathsep.join([config["tooling_root"], config["source_snapshot"]]),
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        PYDANTIC_AI_NO_BANNER="1",
        # Content-free stage timings go to the inference log (one JSON object per line).
        **({"COMPANION_TELEMETRY": "1", "COMPANION_TELEMETRY_EVIDENCE": "PAID_MODEL"}
           if config.get("telemetry", True) else {}),
        TEMP=str(Path(config["run_dir"]) / ".cache/tmp"),
        TMP=str(Path(config["run_dir"]) / ".cache/tmp"),
    )


def execute(config, arguments, log_name):
    run = Path(config["run_dir"])
    (run / ".cache/tmp").mkdir(parents=True, exist_ok=True)
    with (run / log_name).open("a", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, "-B", *arguments],
            cwd=config["workspace"],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment(config),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    if result.returncode:
        raise RuntimeError("Stage failed; see " + str(run / log_name))


def closure(config):
    run = Path(config["run_dir"])
    profiles = [json.loads(p.read_text("utf-8")) for p in (run / "results").glob("*/manifest.json")]
    records = [r for p in (run / "results").glob("*/answers.jsonl") for r in read_rows(p)]
    if len(profiles) != len(config["personas"]) or not all(
        p["status"] == "COMPLETED" for p in profiles
    ):
        raise RuntimeError(
            "Incomplete native profiles; inference resume is deliberately not automatic"
        )
    if len(records) != len({r["id"] for r in records}) or {r["id"] for r in records} != set(
        config["selected_ids"]
    ):
        raise RuntimeError("Answer coverage is incomplete or duplicated")
    if any(r["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED") for r in records):
        raise RuntimeError("Nonterminal questions cannot be graded")
    databases = list((run / "results").glob("*/core.sqlite3"))
    if len(databases) != len(config["personas"]):
        raise RuntimeError("Native profile database coverage is incomplete")
    closed_unknown_counts = {}
    for path in databases:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute(
                "SELECT count(*) FROM model_usage WHERE state='RESERVED'"
            ).fetchone()[0]:
                raise RuntimeError("Native reservation remains active; preserve it and inspect")
            count = connection.execute(
                "SELECT count(*) FROM model_usage WHERE state='UNKNOWN'"
            ).fetchone()[0]
            closed_unknown_counts[path.parent.name] = count
            if count:
                audit = json.loads((path.parent / "manifest.json").read_text("utf-8")).get(
                    "model_execution", {}
                )
                connection.row_factory = sqlite3.Row
                actual = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT request_id,state,billed_micro_usd FROM model_usage "
                        "WHERE state='UNKNOWN' ORDER BY request_id"
                    )
                ]
                if (
                    audit.get("basis") != "local_active_attempts_v2"
                    or audit.get("unclosed_requests") != []
                    or audit.get("uncertain") != actual
                    or set(audit.get("closed_unknowns", {})) != {r["request_id"] for r in actual}
                    or audit.get("journal_sha256") != sha(path.parent / "paid-budget.jsonl")
                ):
                    raise RuntimeError(
                        "Native UNKNOWN lacks verified local closure; preserve it and inspect"
                    )
    _, latest = Budget.from_context(config).snapshot()
    outer_unknowns = {}
    for row in latest.values():
        if row["state"] == "SETTLED":
            continue
        phase = row.get("phase", "").split(":")
        if config.get("campaign_arm") and phase[0] == "inference" and len(phase) >= 3:
            # Other arms are audited separately before campaign-wide answer freeze.
            # Their money remains in the SAME ledger/cap throughout.
            if phase[1] != config["campaign_arm"]:
                continue
            phase = [phase[0], *phase[2:]]
        if row["state"] != "UNKNOWN" or len(phase) < 3 or phase[0] != "inference":
            raise RuntimeError(
                "Outer unclosed/unknown judge request remains; preserve it and inspect"
            )
        outer_unknowns[phase[1]] = outer_unknowns.get(phase[1], 0) + 1
    if any(n > closed_unknown_counts.get(persona, 0) for persona, n in outer_unknowns.items()):
        raise RuntimeError("Outer UNKNOWN has no native closure inventory")
    hashes = {str(p.relative_to(run)): sha(p) for p in (run / "results").glob("*/answers.jsonl")}
    frozen = run / "answers-frozen.json"
    if frozen.exists() and json.loads(frozen.read_text("utf-8")) != hashes:
        raise RuntimeError("Frozen answers were modified; do not regrade changed answers")
    save(frozen, hashes)


def postprocess(run):
    with FileLock(str(run / "postprocess.lock"), timeout=0):
        config = validate_run(run)
        closure(config)
        state(run, "GRADING")
        execute(config, ["-m", "scripts.memory_benchmark.grading"], "grading.log")
        state(run, "BUILDING_REPORT")
        execute(config, ["-m", "scripts.memory_benchmark.report"], "report.log")
        state(run, "AUTOMATED_COMPLETE_REVIEW_PENDING", report="memory-report.html")


def run_suite(config, run):
    (Path(config["workspace"]) / ".local").mkdir(parents=True, exist_ok=True)
    with FileLock(
        str(Path(config["workspace"]) / ".local/memory-benchmark-active.lock"), timeout=0
    ):
        parser_probe(config)
        config = freeze(config, run)
        try:
            state(run, "LIFECYCLE")
            execute(
                config,
                [
                    "-m",
                    "pytest",
                    str(Path(config["source_snapshot"]) / "tests/_benchmark_lifecycle_checks.py"),
                    "-q",
                    "--junitxml=" + str(run / "lifecycle.xml"),
                    "--basetemp=" + str(run / ".cache/lifecycle"),
                    "-o",
                    "cache_dir=" + str(run / ".cache/pytest"),
                ],
                "lifecycle.log",
            )
            tree = ET.parse(run / "lifecycle.xml")
            if (
                len(list(tree.iter("testcase"))) != 18
                or list(tree.iter("failure"))
                or list(tree.iter("error"))
                or list(tree.iter("skipped"))
            ):
                raise RuntimeError("All18native lifecycle checks must pass")
            state(run, "RUNNING_INFERENCE")
            execute(config, ["-m", "scripts.memory_benchmark.worker"], "inference.log")
            postprocess(run)
        except BaseException as error:
            state(run, "NEEDS_ATTENTION", error_type=type(error).__name__, reason=str(error))
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("prepare", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--preset", choices=["full", "smoke"])
        command.add_argument("--output")
    for name in ("status", "postprocess"):
        commands.add_parser(name).add_argument("--run", required=True)
    args = parser.parse_args(argv)
    if args.action in ("prepare", "run"):
        config = configuration(args.config, args.preset)
        if args.action == "prepare":
            print(
                json.dumps(
                    {
                        "status": "READY_NO_MODEL_CALLS",
                        "source_commit": config["source_commit"],
                        "semantic_instances": len(config["selected_ids"]),
                        "lifecycle": 18,
                        "provider": config["provider"],
                        "budget": config["budget"],
                        "parser": config["parser"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        run = under_workspace(
            args.output
            or Path(config["workspace"])
            / ".local/memory-benchmark/runs"
            / (datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]),
            Path(config["workspace"]),
        )
        print("Run directory:", run, flush=True)
        run_suite(config, run)
    elif args.action == "postprocess":
        run = Path(args.run).resolve()
        postprocess(run)
    else:
        run = Path(args.run).resolve()
        config = json.loads((run / "run.json").read_text("utf-8"))
        total, latest = Budget.from_context(config).snapshot()
        records = [r for p in (run / "results").glob("*/answers.jsonl") for r in read_rows(p)]
        print(
            json.dumps(
                {
                    "pipeline": json.loads((run / "status.json").read_text("utf-8")),
                    "answered_records": len(records),
                    "expected_semantic": len(config["selected_ids"]),
                    "answer_states": dict(Counter(r["status"] for r in records)),
                    "charged_or_held_cny": total,
                    "cap_cny": config["budget"]["total_cny"],
                    "usage_states": dict(Counter(r["state"] for r in latest.values())),
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
