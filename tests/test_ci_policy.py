"""CI can reduce work only when it can prove which validation remains required."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_policy import (
    CRITICAL_FILES,
    DOC_EVIDENCE,
    DOC_TESTS,
    FULL_JOBS,
    changed_files,
    check_results,
    choose_profile,
    run_docs,
)
from tests.support.ci_policy import ROOT, commit, git, needs
from tests.support.ci_policy import repository as repository

EVENT = {"repository": {"default_branch": "main"}}


@pytest.mark.parametrize("name,ref,files,expected", [
    ("push", "refs/heads/topic", ["companion/expression.py"], "full"),
    ("push", "refs/heads/topic", ["tests/test_expression.py"], "full"),
    ("push", "refs/heads/topic", ["docs/guide.md", "README.md"], "docs"),
    ("pull_request", "refs/pull/1/merge", ["docs/guide.md"], "docs"),
    ("pull_request", "refs/pull/1/merge", ["companion/expression.py"], "full"),
    ("push", "refs/heads/main", ["docs/guide.md"], "docs"),
    ("push", "refs/heads/main", ["docs/qq-beta/p10-contracts-evidence.json"], "docs"),
    ("push", "refs/heads/main", ["companion/expression.py"], "full"),
    ("push", "refs/heads/main", ["docs/guide.md", "companion/expression.py"], "full"),
    ("pull_request", "refs/pull/1/merge", ["docs/unknown.json"], "full"),
    ("pull_request", "refs/pull/1/merge", ["docs/experiment.py"], "full"),
    ("push", "refs/tags/v1", ["docs/guide.md"], "full"),
    ("workflow_dispatch", "refs/heads/topic", ["docs/guide.md"], "full"),
    ("merge_group", "refs/heads/queue", ["docs/guide.md"], "full"),
    ("unknown", "refs/heads/topic", ["docs/guide.md"], "full"),
    ("push", "refs/heads/topic", None, "full"),
    ("push", "refs/heads/topic", [], "full"),
    ("push", "refs/heads/topic", ["new_runtime/data.bin"], "full"),
    ("push", "refs/heads/topic", ["docs/guide.md", "new_runtime/data.bin"], "full"),
    ("push", "refs/heads/topic", ["docs/../contracts/schema.md"], "full"),
    ("push", "refs/heads/topic", ["docs/guide.md\nprofile=docs"], "full"),
])
def test_profiles_keep_integration_refs_full_and_only_reduce_known_changes(
    name, ref, files, expected,
):
    assert choose_profile(name, ref, EVENT, files)[0] == expected


@pytest.mark.parametrize("name", sorted(CRITICAL_FILES) + [
    "contracts/event.v1.schema.json", "migrations/versions/new.py",
    "deploy/source-reader.Dockerfile", "docs/design-v0.6/upstream-native-p3.lock",
    ".github/workflows/ci.yml", "companion_onebot_bridge/outbound.py",
    "companion_meme_protocol/__init__.py",
])
def test_critical_changes_cannot_use_development_or_docs_shortcut(name):
    assert choose_profile("push", "refs/heads/topic", EVENT, [name])[0] == "full"


@pytest.mark.parametrize("name", [
    "companion/api.py", "companion/policy.py", "companion/backup.py",
    "companion/model.py", "companion/memory.py", "companion/core.py",
    "companion/runtime.py", "companion/observation.py", "companion/content.py",
    "companion/live_text.py",
    "companion/executor_gateway.py", "companion/executor_budget_proxy.py",
    "companion/dsh_job.py", "source_reader/worker.py",
    "astrbot_plugin_companion_bridge/bridge.py",
])
def test_auth_consent_restore_paid_io_and_evidence_paths_always_require_full(name):
    # Independent risk examples: removing a path from the policy must break this test.
    assert choose_profile("push", "refs/heads/topic", EVENT, [name])[0] == "full"


@pytest.mark.parametrize("profile", ["full", "docs", "reuse"])
def test_gate_accepts_only_the_expected_complete_result_set(profile):
    assert check_results(profile, needs(profile))
    assert not check_results("unknown", needs(profile))
    assert not check_results(profile, {})


@pytest.mark.parametrize("job", ["preflight", "quality", *FULL_JOBS])
@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", ""])
def test_full_gate_rejects_failed_cancelled_missing_or_skipped_required_job(job, state):
    results = needs("full")
    results[job]["result"] = state
    assert not check_results("full", results)


def test_docs_gate_cannot_hide_a_failed_attempted_job():
    profile = "docs"
    results = needs(profile)
    results["container"]["result"] = "failure"
    assert not check_results(profile, results)
    results = needs(profile)
    del results["container"]
    assert not check_results(profile, results)


def test_actual_git_diff_retains_old_code_path_when_it_is_renamed_to_markdown(repository):
    source = repository / "companion/expression.py"
    source.parent.mkdir()
    source.write_text("text\n", encoding="utf-8")
    base = commit(repository)
    destination = repository / "docs/renamed.md"
    destination.parent.mkdir()
    source.rename(destination)
    head = commit(repository)
    event = {**EVENT, "before": base, "after": head}
    files = changed_files("push", event, repository)
    assert set(files) == {"companion/expression.py", "docs/renamed.md"}
    assert choose_profile("push", "refs/heads/topic", event, files)[0] == "full"


def test_large_diff_has_no_api_page_limit_and_new_branch_uses_main_merge_base(repository):
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/remotes/origin/main", base)
    directory = repository / "docs"
    directory.mkdir()
    for number in range(320):
        (directory / f"guide-{number}.md").write_text("fixture\n", encoding="utf-8")
    (repository / "uv.lock").write_text("fixture\n", encoding="utf-8")
    event = {**EVENT, "before": "0" * 40, "after": commit(repository)}
    files = changed_files("push", event, repository)
    assert len(files) == 321 and "uv.lock" in files
    assert choose_profile("push", "refs/heads/topic", event, files)[0] == "full"


def test_pr_comparison_excludes_unrelated_target_branch_changes(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "topic.py").write_text("fixture\n", encoding="utf-8")
    head = commit(repository)
    git(repository, "checkout", "-q", "--detach", base)
    (repository / "uv.lock").write_text("fixture\n", encoding="utf-8")
    target = commit(repository)
    event = {**EVENT, "pull_request": {"base": {"sha": target}, "head": {"sha": head}}}
    assert changed_files("pull_request", event, repository) == ["topic.py"]
    assert changed_files("push", {**EVENT, "before": "f" * 40, "after": head}, repository) is None


def test_cli_event_failure_chooses_full_and_gate_failure_returns_nonzero(tmp_path):
    output = tmp_path / "outputs"
    env = {**os.environ, "GITHUB_OUTPUT": str(output),
           "GITHUB_EVENT_PATH": str(tmp_path / "missing.json"), "GITHUB_EVENT_NAME": "push",
           "GITHUB_REF": "refs/heads/topic"}
    result = subprocess.run([sys.executable, "scripts/ci_policy.py", "plan"],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0
    outputs = dict(line.split("=", 1) for line in output.read_text("utf-8").splitlines())
    assert outputs["profile"] == "full" and outputs["reuse"] == "null"
    assert json.loads(outputs["windows"])["product"] is True
    assert len(json.loads(outputs["matrix"])["include"]) == 8
    env["NEEDS_JSON"] = json.dumps(needs("docs"))
    result = subprocess.run([sys.executable, "scripts/ci_policy.py", "gate", "--profile", "full"],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0


def test_documentation_checks_cover_public_materials_and_every_fast_path_evidence_file():
    assert all((ROOT / node.split("::", 1)[0]).is_file() for node in DOC_TESTS)
    assert "tests/test_p8c_public_materials.py" in DOC_TESTS
    assert "tests/test_p10_contracts_evidence.py" in DOC_TESTS
    assert "tests/test_p13_gates.py" in DOC_TESTS
    assert any(node.startswith("tests/test_p12_beta_operations.py::") for node in DOC_TESTS)
    assert len(DOC_TESTS) == len(set(DOC_TESTS))
    assert DOC_TESTS[0] == "tests/test_p8c_public_materials.py"
    for name, checks in DOC_EVIDENCE.items():
        assert (ROOT / name).is_file()
        assert checks and set(checks) <= set(DOC_TESTS)
        assert choose_profile("push", "refs/heads/main", EVENT, [name])[0] == "docs"
        assert choose_profile("pull_request", "refs/pull/1/merge", EVENT, [name])[0] == "docs"
    assert choose_profile("push", "refs/heads/main", EVENT, ["docs/qq-beta/p14-gates.json"])[
        0
    ] == "full"


@pytest.mark.parametrize("profile", ["full", "docs", "reuse"])
def test_cli_receipt_records_actual_checkout_only_after_required_checks_pass(tmp_path, profile):
    summary = tmp_path / "summary.md"
    results = needs(profile)
    results["quality"]["result"] = "failure"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary),
           "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "synthetic/project",
           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
           "CI_IMAGES_ARTIFACT_ID": "301", "CI_IMAGES_ARTIFACT_DIGEST": "a" * 64,
           "NEEDS_JSON": json.dumps(results)}
    command = [sys.executable, "scripts/ci_policy.py", "gate", "--profile", profile]
    assert subprocess.run(command, cwd=ROOT, env=env, capture_output=True).returncode != 0
    assert not summary.exists()
    env["NEEDS_JSON"] = json.dumps(needs(profile))
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    receipt = summary.read_text("utf-8")
    assert f"Profile: `{profile}`" in receipt
    assert git(ROOT, "rev-parse", "HEAD") in receipt
    assert git(ROOT, "rev-parse", "HEAD^{tree}") in receipt
    assert "https://github.com/synthetic/project/actions/runs/123" in receipt
    assert f"| container | {'success' if profile == 'full' else 'skipped'} |" in receipt


@pytest.mark.parametrize("result", [0, 1])
def test_docs_bootstrap_fresh_scratch_and_clean_only_their_own_round(
    tmp_path, monkeypatch, result,
):
    scratch = tmp_path / ".local/test-temp"
    assert not scratch.exists()
    rounds = []

    def pytest_process(command, cwd):
        assert cwd == tmp_path and scratch.is_dir()
        directory = Path(next(arg for arg in command if arg.startswith("--basetemp="))[11:])
        assert directory.parent.is_dir() and directory.parent.parent == scratch
        rounds.append(directory.parent)
        # An unrelated retained diagnostic must survive a later successful/failed run.
        (scratch / "retained.txt").write_text("synthetic diagnostic", encoding="utf-8")
        return result

    monkeypatch.setattr("scripts.ci_policy.subprocess.call", pytest_process)
    assert run_docs(tmp_path) == result
    assert not rounds[0].exists()
    assert (scratch / "retained.txt").read_text("utf-8") == "synthetic diagnostic"


def test_documentation_failure_stops_before_later_evidence_checks(tmp_path, monkeypatch, capfd):
    (tmp_path / "test_documentation.py").write_text(
        "import pytest\n"
        "def test_privacy():\n    pytest.fail('privacy category only', pytrace=False)\n"
        "def test_later_evidence():\n"
        "    from pathlib import Path\n    Path('later-evidence-ran').touch()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.ci_policy.DOC_TESTS", ("test_documentation.py",))
    assert run_docs(tmp_path) == 1
    assert "privacy category only" in capfd.readouterr().out
    assert not (tmp_path / "later-evidence-ran").exists()


def test_workflow_preserves_complete_platform_coverage_and_fails_closed():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text("utf-8"),
                         Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert set(jobs) == {"preflight", "quality", "ci", *FULL_JOBS}
    assert jobs["test"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.preflight.outputs.matrix) }}")
    assert jobs["test"]["name"] == "test (${{ matrix.os }}, ${{ matrix.shard }})"
    assert jobs["test"]["strategy"]["fail-fast"] == "false"
    for name in FULL_JOBS:
        assert jobs[name]["needs"] == "preflight"
        assert "needs.preflight.outputs.profile == 'full'" in jobs[name]["if"]
        assert "needs.preflight.outputs.profile == 'selected'" in jobs[name]["if"]
    assert "fromJSON(needs.preflight.outputs.windows).browser" in jobs["browser-reader"]["if"]
    assert jobs["ci"]["if"] == "${{ always() }}"
    assert set(jobs["ci"]["needs"]) == {"preflight", "quality", *FULL_JOBS}
    # Choosing work must not wait for dependency installs or duplicate runtime tests.
    assert all("pytest" not in step.get("run", "") and "uv" not in step.get("run", "")
               for step in jobs["preflight"]["steps"])
    steps = jobs["test"]["steps"]
    assert any("-p scripts.ci_shards" in step.get("run", "") for step in steps)
    for command in ("uv run python scripts/replay.py", "uv run python scripts/smoke.py",
                    "uv build", "uv run python scripts/verify_package.py"):
        matches = [step for step in steps if step.get("run") == command]
        assert len(matches) == 1
        condition = "${{ matrix.shard == matrix.verify_shard"
        if command.endswith(("scripts/replay.py", "scripts/smoke.py")):
            condition += " && fromJSON(needs.preflight.outputs.windows).product"
        assert matches[0]["if"] == condition + " }}"
    assert not any("ruff" in step.get("run", "")
                   for step in jobs["browser-reader"]["steps"])
    assert set(workflow["on"]) == {"push", "pull_request", "merge_group", "workflow_dispatch"}
    # A branch push has no check until a PR is opened. Only its PR merge preview
    # may produce the required `ci` check, so a short push cannot mask a failed PR.
    assert workflow["on"]["push"] == {"branches": ["main"], "tags": ["**"]}
    assert workflow["permissions"] == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert "github.workflow" in concurrency["group"] and "github.ref" in concurrency["group"]
    assert "default_branch" in concurrency["cancel-in-progress"]


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", ""])
def test_docs_gate_requires_its_documentation_validation(state):
    results = needs("docs")
    results["quality"]["result"] = state
    assert not check_results("docs", results)


def test_every_workflow_action_is_pinned_to_a_full_commit_sha():
    import re

    refs = [match.group(1)
            for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
            for match in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", path.read_text(encoding="utf-8"),
                                     re.MULTILINE)]
    assert refs
    # Tags are mutable; a regressed merge once restored tag pins after they were fixed.
    assert [ref for ref in refs if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref)] == []
