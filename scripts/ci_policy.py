"""Conservative CI selection and the final required-result check (stdlib only)."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FULL_JOBS = ("test", "container", "browser-reader", "plugin-native", "flutter-client")
# Only these existing Console assets and their browser regressions can use Console
# checks instead of backend suites. Other Python files and new assets keep full CI.
STATIC_UI = {"companion/console_static/" + name for name in ("app.css", "app.js", "index.html")}
CONSOLE_BROWSER_TESTS = {
    "tests/test_console_browser.py", "tests/test_console_supplement_browser.py",
}
# These existing backend-only files still run complete Ubuntu AND Windows suites,
# but do not change the browser reader. Shared code, new tests and unknown paths
# intentionally stay outside this narrow reviewed list.
NON_BROWSER = {
    "companion/affect.py", "companion/consolidation.py", "companion/expression.py",
    "companion/goals.py", "companion/growth.py", "companion/interest.py",
    "companion/reflection.py", "companion/diary.py",
    "tests/test_affect.py", "tests/test_consolidation.py", "tests/test_expression.py",
    "tests/test_goals.py", "tests/test_diary.py",
}
# Each fast-path evidence file names the checks that validate its actual contents.
# Unknown JSON and changes to these checks still require full CI.
DOC_EVIDENCE = {
    "docs/design-v0.6/p4-gates.json": ("tests/test_p4_gates.py",),
    "docs/qq-beta/p8b-gates.json": ("tests/test_p8b_evidence.py",),
    "docs/qq-beta/p8c-readiness.json": ("tests/test_p8c_readiness.py",),
    "docs/qq-beta/p8c1-gates.json": ("tests/test_p8c1_gates.py",),
    "docs/qq-beta/p8c1-contracts-gates.json": ("tests/test_p8c1_contracts_gates.py",),
    "docs/qq-beta/p10-contracts-evidence.json": ("tests/test_p10_contracts_evidence.py",),
    "docs/qq-beta/p12-gates.json": (
        "tests/test_p12_beta_operations.py::"
        "test_private_beta_configuration_and_baseline_never_approve_runtime",
    ),
    "docs/qq-beta/p13-gates.json": ("tests/test_p13_gates.py",),
}
# Docs mode stops on privacy failure before later validators can echo document values.
DOC_TESTS = tuple(dict.fromkeys((
    "tests/test_p8c_public_materials.py", "tests/test_ci_policy.py",
    *(check for checks in DOC_EVIDENCE.values() for check in checks),
)))
CRITICAL_FILES = {
    "Dockerfile", "pyproject.toml", "uv.lock", "compose.yaml", "config.example.toml",
    "scripts/ci_policy.py", "tests/test_ci_policy.py", "AGENTS.md",
    "companion/config.py", "companion/contracts.py", "companion/source_policy.py",
    "companion/sources.py", "companion/privacy.py", "companion/erasure.py",
    "companion/budget.py", "companion/paid_budget.py", "companion/store.py",
    "companion/transport.py", "companion/source_grants.py", "companion/worker_lock.py",
    "companion/api.py", "companion/policy.py", "companion/backup.py", "companion/core.py",
    "companion/runtime.py", "companion/model.py", "companion/memory.py",
    "companion/live_text.py",
    "companion/retrieval.py", "companion/observation.py", "companion/content.py",
    "companion/executive.py", "companion/executive_protocol.py", "companion/executor_gateway.py",
    "companion/executor_budget_proxy.py", "companion/executor_files.py", "companion/dsh_job.py",
    "source_reader/accounts.py", "source_reader/network.py", "source_reader/worker.py",
}
CRITICAL_DIRS = (".github/", "contracts/", "migrations/", "db/", "deploy/",
                 "companion_onebot_bridge/", "companion_meme_protocol/", "companion_voice_node/",
                 "companion_delivery_protocol/", "clients/chat/",
                 "astrbot_plugin_companion_bridge/")


def safe_path(name):
    return (
        isinstance(name, str) and bool(name)
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
        and "\\" not in name and not name.startswith("/")
        and ".." not in PurePosixPath(name).parts and PurePosixPath(name).as_posix() == name
    )


def choose_profile(event_name, ref, event, files):
    default = event.get("repository", {}).get("default_branch")
    if event_name in {"workflow_dispatch", "merge_group"}:
        return "full", "integration-or-manual"
    if not default or ref.startswith("refs/tags/"):
        return "full", "protected-ref"
    if event_name not in {"push", "pull_request"} or not files:
        return "full", "unknown-change-set"
    if any(not safe_path(name) for name in files):
        return "full", "unrecognized-path"
    if any(name in CRITICAL_FILES or name.startswith(CRITICAL_DIRS)
           or name.endswith((".lock", ".Dockerfile")) for name in files):
        return "full", "critical-change"
    if all(name in DOC_EVIDENCE or (
        name.endswith(".md") and (name == "README.md" or name.startswith("docs/"))
    ) for name in files):
        return "docs", "documentation-only"
    if ref == "refs/heads/" + default:
        return "full", "protected-ref"
    if event_name == "pull_request":
        return "full", "pr-merge-preview"
    return "full", "unrecognized-change"


def changed_files(event_name, event, root=ROOT):
    """Use the complete Git diff, including deletions and both sides of renames."""
    try:
        if event_name == "pull_request":
            base = event["pull_request"]["base"]["sha"]
            head = event["pull_request"]["head"]["sha"]
            separator = "..."
        elif event_name == "push":
            base, head, separator = event["before"], event["after"], ".."
            if base == "0" * 40:
                default = event["repository"]["default_branch"]
                if not re.fullmatch(r"[A-Za-z0-9_./-]+", default) or ".." in default:
                    return None
                base, separator = "refs/remotes/origin/" + default, "..."
        else:
            return None
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            return None
        if not base.startswith("refs/remotes/origin/") and not re.fullmatch(r"[0-9a-f]{40}", base):
            return None
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base + separator + head, "--"],
            cwd=root, capture_output=True, check=True, timeout=30,
        )
        return result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return None


def windows_selection(profile, event_name, ref, files):
    """Only PRs with a complete, recognized diff may omit a Windows surface."""
    if profile in {"docs", "reuse"}:
        return {"product": False, "browser": False, "reason": profile}
    if event_name != "pull_request" or not ref.startswith("refs/pull/"):
        return {"product": True, "browser": True, "reason": "integration-or-unknown-event"}
    if not files or any(not safe_path(name) for name in files):
        return {"product": True, "browser": True, "reason": "unknown-change-set"}
    code = [name for name in files if not (
        name in DOC_EVIDENCE or name.endswith(".md")
        and (name == "README.md" or name.startswith("docs/"))
    )]
    if code and all(name in STATIC_UI | CONSOLE_BROWSER_TESTS for name in code):
        return {"product": False, "browser": True, "reason": "known-static-ui-only"}
    if code and all(name in NON_BROWSER for name in code):
        return {"product": True, "browser": False, "reason": "known-backend-only"}
    return {"product": True, "browser": True, "reason": "shared-platform-or-unknown-path"}


def test_matrix(selection):
    if not selection["product"] and selection["browser"]:
        return {"include": [{"os": "ubuntu-latest", "shard": 0, "verify_shard": 0}]}
    platforms = ["ubuntu-latest"] + (["windows-latest"] if selection["product"] else [])
    return {"include": [{"os": platform, "shard": shard,
                         "verify_shard": int(platform == "windows-latest")}
                        for platform in platforms for shard in range(8)]}


def valid_selection(selection):
    return (isinstance(selection, dict) and set(selection) == {"product", "browser", "reason"}
            and type(selection["product"]) is bool and type(selection["browser"]) is bool
            and isinstance(selection["reason"], str) and bool(selection["reason"]))


def expected_results(profile, selection):
    expected = "success" if profile in {"full", "selected"} else "skipped"
    results = {"preflight": "success", "quality": "success",
               **dict.fromkeys(FULL_JOBS, expected)}
    if profile == "selected" and not selection["browser"]:
        results["browser-reader"] = "skipped"
    if profile == "selected" and not selection["product"]:
        results.update(container="skipped", **{"plugin-native": "skipped"})
    return results


def check_results(profile, needs, selection=None):
    if selection is None:
        selection = {"product": profile == "full", "browser": profile == "full", "reason": profile}
    if not valid_selection(selection):
        return False
    if profile == "full" and not (selection["product"] and selection["browser"]):
        return False
    if profile == "selected" and (selection["product"], selection["browser"]) not in {
        (True, False), (False, True),
    }:
        return False
    if profile in {"docs", "reuse"} and (selection["product"] or selection["browser"]):
        return False
    return (
        profile in {"full", "selected", "docs", "reuse"}
        and all(needs.get(name, {}).get("result") == expected
                for name, expected in expected_results(profile, selection).items())
    )


def run_docs(root=ROOT):
    scratch = root / ".local/test-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    # One owned directory per invocation; existing diagnostics are never cleared.
    with tempfile.TemporaryDirectory(prefix="ci-docs-", dir=scratch) as directory:
        temporary = Path(directory)
        if temporary.resolve().parent != scratch.resolve():
            raise RuntimeError("CI temporary directory outside workspace scratch")
        return subprocess.call([
            sys.executable, "-m", "pytest", "-q", "-x", *DOC_TESTS,
            "--basetemp=" + str(temporary / "pytest"),
            "-o", "cache_dir=" + str(temporary / "cache"),
        ], cwd=root)


def job_timings(env=os.environ, read=None):
    """Optional native Actions timings; absence never invents zero-duration evidence."""
    if env.get("CI_JOB_TIMINGS") != "true":
        return {"available": False, "reason": "not-requested"}
    try:
        if read is None:
            if __package__:
                from .ci_reuse import api
            else:
                from ci_reuse import api
            read = api
        repo, run, attempt = (env[key] for key in (
            "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not (
            run.isdigit() and attempt.isdigit()
        ):
            raise ValueError("Invalid run identity")
        result = read(f"repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100")
        if result["total_count"] != len(result["jobs"]):
            raise ValueError("Incomplete job timing page")
        jobs = []
        for job in result["jobs"]:
            start, end = job.get("started_at"), job.get("completed_at")
            seconds = ((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
                       if start and end else None)
            if seconds is not None and seconds < 0:
                raise ValueError("Invalid job timestamps")
            jobs.append({key: job.get(key) for key in (
                "name", "status", "conclusion", "started_at", "completed_at")}
                        | {"elapsed_seconds": seconds})
        return {"available": True, "jobs": jobs}
    except Exception as error:
        return {"available": False, "reason": type(error).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "docs", "gate"])
    parser.add_argument("--profile")
    args = parser.parse_args()
    if args.operation == "plan":
        reuse = None
        name, ref, event = "", "", {}
        files = None
        try:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text("utf-8"))
            name, ref = os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"]
            files = changed_files(name, event)
            profile, reason = choose_profile(name, ref, event, files)
        except (KeyError, TypeError, ValueError, OSError, AttributeError):
            profile, reason = "full", "unreadable-event"
        if profile == "full":
            try:
                if __package__:
                    from .ci_reuse import find_verified_run
                else:
                    from ci_reuse import find_verified_run

                reuse = find_verified_run(name, ref, event)
            except Exception as error:
                # Reuse is optional; unavailable or malformed evidence runs full CI.
                print(f"CI reuse unavailable ({type(error).__name__}); running full CI")
                reuse = None
            if reuse:
                profile, reason = "reuse", "verified-identical-pr-tree"
        selection = windows_selection(
            profile, name if profile != "full" or reason == "pr-merge-preview" else "", ref, files,
        )
        if profile == "full" and not (selection["product"] and selection["browser"]):
            profile, reason = "selected", selection["reason"]
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"profile={profile}\n")
            output.write("reuse=" + json.dumps(reuse, separators=(",", ":")) + "\n")
            output.write("windows=" + json.dumps(selection, separators=(",", ":")) + "\n")
            output.write("matrix=" + json.dumps(test_matrix(selection),
                                                 separators=(",", ":")) + "\n")
        print(json.dumps({"profile": profile, "reason": reason, "windows": selection}))
    elif args.operation == "gate":
        needs = json.loads(os.environ["NEEDS_JSON"])
        try:
            selection = json.loads(os.environ.get("CI_WINDOWS", "null"))
        except ValueError:
            selection = {"error": "invalid-preflight-selection"}
        if selection is None and "CI_WINDOWS" not in os.environ:
            selection = {"product": args.profile == "full", "browser": args.profile == "full",
                         "reason": args.profile}
        passed = valid_selection(selection) and check_results(args.profile, needs, selection)
        image_artifact = None
        if args.profile == "full":
            if __package__:
                from .ci_container_artifacts import metadata_reference
            else:
                from ci_container_artifacts import metadata_reference
            try:
                image_artifact = metadata_reference()
            except (KeyError, TypeError, ValueError):
                passed = False
        try:
            reuse = json.loads(os.environ.get("CI_REUSE", "null"))
        except ValueError:
            reuse = {"error": "invalid-preflight-reuse"}
        commit, tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD", "HEAD^{tree}"], cwd=ROOT, text=True, timeout=10,
        ).splitlines()
        machine = {"version": 1, "profile": args.profile, "commit": commit, "tree": tree,
                   "passed": passed, "windows": selection,
                   "matrix": test_matrix(selection) if valid_selection(selection)
                   and args.profile in {"full", "selected"} else {"include": []},
                   "checks": {job: needs.get(job, {}).get("result", "missing")
                              for job in ("preflight", "quality", *FULL_JOBS)},
                   "expected": expected_results(args.profile, selection)
                   if valid_selection(selection) else {},
                   "reuse": reuse, "container_images": image_artifact,
                   "timings": job_timings(),
                   "run_id": os.environ.get("GITHUB_RUN_ID"),
                   "attempt": os.environ.get("GITHUB_RUN_ATTEMPT")}
        path = ROOT / ".local/ci/validation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(machine, indent=2) + "\n", encoding="utf-8")
        if not passed:
            raise SystemExit("Required CI validation did not complete successfully")
        print(f"CI passed: {args.profile}")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            # This receipt describes this run; full-tree reuse still requires its
            # separately authenticated artifact and all successful job results.
            run = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
                   f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
            with Path(summary).open("a", encoding="utf-8") as receipt:
                receipt.write(
                    f"### Validation receipt\n\nProfile: `{args.profile}`\n\n"
                    f"Windows selection: `{json.dumps(selection, sort_keys=True)}`\n\n"
                    f"Commit: `{commit}`\n\nTree: `{tree}`\n\n[CI run]({run})\n\n"
                    "| Check | Result |\n| --- | --- |\n"
                )
                for job in ("preflight", "quality", *FULL_JOBS):
                    receipt.write(f"| {job} | {needs[job]['result']} |\n")
                if machine["timings"]["available"]:
                    receipt.write("\n| Actual job | Elapsed seconds |\n| --- | --- |\n")
                    for job in machine["timings"]["jobs"]:
                        seconds = job["elapsed_seconds"]
                        elapsed = seconds if seconds is not None else "pending"
                        receipt.write(f"| {job['name']} | {elapsed} |\n")
    else:
        raise SystemExit(run_docs())


if __name__ == "__main__":
    main()
