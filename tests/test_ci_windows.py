"""Windows omission is scoped evidence, never a substitute for cross-platform CI."""

import json
import sys

import pytest
import yaml

from scripts import ci_policy, ci_reuse
from tests.support.ci_policy import ROOT, commit, git, needs


def selection(files, event="pull_request", ref="refs/pull/1/merge", profile="full"):
    return ci_policy.windows_selection(profile, event, ref, files)


@pytest.mark.parametrize("path", [
    "pyproject.toml", "uv.lock", "deploy/executor.lock", "Dockerfile",
    "deploy/compose.executors.yaml", "scripts/check.ps1", "scripts/cleanup-voice-node.ps1",
    "scripts/ci_policy.py", ".github/workflows/ci.yml", "tests/conftest.py",
    "companion/worker_lock.py", "companion/backup.py", "companion/executor_files.py",
    "companion/core.py", "companion/model.py", "companion/config.py", "companion/store.py",
    "companion_voice_node/state.py", "companion_onebot_bridge/voice_media.py",
    "source_reader/browser.py", "tests/test_source_browser.py", "tests/test_new_feature.py",
    "companion/new_backend.py", "unknown/new.txt", "docs/unknown.json", "companion/NEW.py",
    "companion/console_api.py", "companion/console_cognition.py", "tsconfig.console.json",
    "tests/test_console_static.py", "tests/test_owner_console.py",
    "tests/test_console_cognition.py", "tests/test_console_browser_extra.py",
    "tests/test_console_supplement_browser_new.py", "contracts/new.json",
    "migrations/versions/new.py", "companion_delivery_protocol/__init__.py",
])
def test_shared_platform_browser_and_unknown_paths_keep_both_windows_checks(path):
    value = selection(["companion/console_static/app.css", "tests/test_console_browser.py",
                       "tests/test_console_supplement_browser.py", path])
    assert value["product"] and value["browser"]


@pytest.mark.parametrize("files,product,browser", [
    (["companion/console_static/app.css"], False, True),
    (["companion/console_static/app.js", "docs/guide.md"], False, True),
    (["companion/console_static/app.js", "docs/qq-beta/p13-gates.json"], False, True),
    (["tests/test_console_browser.py"], False, True),
    (["tests/test_console_supplement_browser.py"], False, True),
    (["tests/test_console_browser.py", "tests/test_console_supplement_browser.py"], False, True),
    (["companion/console_static/app.js", "tests/test_console_browser.py",
      "tests/test_console_supplement_browser.py", "docs/guide.md"], False, True),
    (["companion/expression.py", "tests/test_expression.py"], True, False),
    (["companion/diary.py", "docs/guide.md"], True, False),
    (["companion/diary.py", "companion/console_static/app.css"], True, True),
    (["companion/console_static/new.js"], True, True),
])
def test_only_reviewed_paths_can_reduce_one_surface(files, product, browser):
    assert (selection(files)["product"], selection(files)["browser"]) == (product, browser)


@pytest.mark.parametrize("event,ref", [
    ("push", "refs/heads/main"), ("push", "refs/heads/topic"), ("push", "refs/tags/v1"),
    ("merge_group", "refs/heads/queue"), ("workflow_dispatch", "refs/heads/main"),
    ("pull_request", "unknown"), ("unknown", "refs/pull/1/merge"),
])
def test_non_pr_events_keep_full_platform_checks(event, ref):
    value = selection(["companion/console_static/app.css", "tests/test_console_browser.py",
                       "tests/test_console_supplement_browser.py"], event, ref)
    assert value["product"] and value["browser"]


@pytest.mark.parametrize("files", [None, [], ["../app.css"], ["docs/guide.md\nprofile=docs"]])
def test_missing_or_malformed_diff_cannot_omit_windows(files):
    value = selection(files)
    assert value["product"] and value["browser"]


@pytest.mark.parametrize("profile", ["docs", "reuse"])
def test_docs_and_reuse_have_no_new_windows_jobs(profile):
    value = selection(["docs/guide.md"], profile=profile)
    assert value == {"product": False, "browser": False, "reason": profile}
    assert ci_policy.check_results(profile, needs(profile), value)


def test_allowlists_are_existing_files_and_do_not_hide_new_tests():
    assert all((ROOT / name).is_file() for name in (
        ci_policy.STATIC_UI | ci_policy.CONSOLE_BROWSER_TESTS | ci_policy.NON_BROWSER))
    assert not any(name.startswith("companion_voice_node/") for name in ci_policy.NON_BROWSER)


def test_last_ui_commit_cannot_hide_backend_changes_earlier_in_the_pr(tmp_path):
    git(tmp_path, "init", "-q")
    static = tmp_path / "companion/console_static"
    static.mkdir(parents=True)
    (static / "app.js").write_text("initial\n")
    backend = tmp_path / "companion/console_api.py"
    backend.write_text("initial\n")
    base = commit(tmp_path)
    backend.write_text("changed\n")
    commit(tmp_path)
    (static / "app.js").write_text("changed\n")
    head = commit(tmp_path)
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    files = ci_policy.changed_files("pull_request", event, tmp_path)
    assert set(files) == {"companion/console_api.py", "companion/console_static/app.js"}
    assert selection(files)["product"] and selection(files)["browser"]


@pytest.mark.parametrize("event,files,profile,count,browser", [
    ("pull_request", ["companion/console_static/app.css"], "selected", 1, True),
    ("pull_request", ["tests/test_console_browser.py"], "selected", 1, True),
    ("pull_request", ["companion/console_static/app.js",
                      "tests/test_console_supplement_browser.py"], "selected", 1, True),
    ("pull_request", ["companion/console_static/app.js", "tests/test_console_browser.py",
                      "tests/test_console_supplement_browser.py"], "selected", 1, True),
    ("pull_request", ["tests/test_console_supplement_browser.py",
                      "companion/console_api.py"], "full", 8, True),
    ("pull_request", ["tests/test_console_browser.py",
                      "tests/test_console_browser_extra.py"], "full", 8, True),
    ("pull_request", ["tests/test_console_browser.py",
                      ".github/workflows/ci.yml"], "full", 8, True),
    ("pull_request", ["companion/expression.py"], "selected", 8, False),
    ("pull_request", ["companion/new.py"], "full", 8, True),
    ("pull_request", ["uv.lock"], "full", 8, True),
    ("pull_request", ["docs/guide.md"], "docs", 4, False),
    ("push", ["companion/console_static/app.css"], "full", 8, True),
])
def test_plan_cli_emits_consistent_profile_selection_and_matrix(
    tmp_path, monkeypatch, event, files, profile, count, browser,
):
    source, output = tmp_path / "event.json", tmp_path / "output"
    source.write_text(json.dumps({"repository": {"default_branch": "main"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(source))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", "refs/pull/1/merge" if event == "pull_request"
                       else "refs/heads/main")
    monkeypatch.setattr(ci_policy, "changed_files", lambda *args: files)
    monkeypatch.setattr(ci_reuse, "find_verified_run", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["ci_policy.py", "plan"])
    ci_policy.main()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["profile"] == profile
    assert json.loads(values["windows"])["browser"] is browser
    assert len(json.loads(values["matrix"])["include"]) == count
    # An unreadable/incomplete repository identity cannot use the path exception.
    source.write_text("{}")
    output.write_text("")
    ci_policy.main()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["profile"] == "full"
    assert len(json.loads(values["matrix"])["include"]) == 8


@pytest.mark.parametrize("product,browser", [(True, True), (False, True), (True, False)])
def test_matrix_and_gate_cover_every_selected_shard_and_required_job(product, browser):
    value = {"product": product, "browser": browser, "reason": "synthetic"}
    matrix = ci_policy.test_matrix(value)["include"]
    expected_platforms = ["ubuntu-latest"] + (["windows-latest"] if product else [])
    assert {(row["os"], row["shard"]) for row in matrix} == {
        (platform, shard) for platform in expected_platforms
        for shard in range(4 if product else 1)}
    assert sum(row["shard"] == row["verify_shard"] for row in matrix) == len(expected_platforms)
    profile = "full" if product and browser else "selected"
    results = needs("full")
    results["browser-reader"]["result"] = "success" if browser else "skipped"
    if not product:
        results["container"]["result"] = results["plugin-native"]["result"] = "skipped"
    assert ci_policy.check_results(profile, results, value)
    for job in ("preflight", "quality", *ci_policy.FULL_JOBS):
        original = results[job]["result"]
        for result in ("failure", "cancelled", "missing"):
            results[job]["result"] = result
            assert not ci_policy.check_results(profile, results, value)
        results[job]["result"] = original
    if not browser:
        results["browser-reader"]["result"] = "success"
        assert not ci_policy.check_results(profile, results, value)
    assert not ci_policy.check_results("full", results, {**value, "product": False})


def test_static_ui_workflow_requires_console_types_browser_contracts_and_packaging():
    jobs = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(),
                     Loader=yaml.BaseLoader)["jobs"]
    product = "${{ fromJSON(needs.preflight.outputs.windows).product }}"
    console = "${{ !fromJSON(needs.preflight.outputs.windows).product }}"
    types = next(step for step in jobs["quality"]["steps"]
                 if "tsc -p tsconfig.console.json" in step.get("run", ""))
    assert "needs.preflight.outputs.profile == 'selected'" in types["if"]
    docs = next(step for step in jobs["quality"]["steps"]
                if "scripts/ci_policy.py docs" in step.get("run", ""))
    assert "needs.preflight.outputs.profile == 'selected'" in docs["if"]
    assert "!fromJSON(needs.preflight.outputs.windows).product" in docs["if"]
    tests = jobs["test"]["steps"]
    full = next(step for step in tests if "-p scripts.ci_shards" in step.get("run", ""))
    assert full["if"] == product
    assert "python -B -m scripts.test_feedback --workers 2 --" in full["run"]
    assert "--impact" not in full["run"] and "--skip-model-preparation" not in full["run"]
    ui = next(step for step in tests if step.get("if") == console)
    assert "-p scripts.ci_shards" not in ui["run"]
    for name in ("test_console_static.py", "test_owner_console.py", "test_console_cognition.py"):
        assert "tests/" + name in ui["run"] and (ROOT / "tests" / name).is_file()
    for command in ("uv build", "uv run python scripts/verify_package.py"):
        step = next(step for step in tests if step.get("run") == command)
        assert step["if"] == "${{ matrix.shard == matrix.verify_shard }}"
    browser_job = jobs["browser-reader"]
    assert browser_job["env"]["COMPANION_TEST_BROWSER"] == "1"
    browser_steps = browser_job["steps"]
    proxy = next(step for step in browser_steps
                 if "check_console_proxy.ps1 -PrepareOnly" in step.get("run", ""))
    assert proxy["shell"] == "pwsh" and "if" not in proxy
    assert '"COMPANION_TEST_NGINX=$nginx" >> $env:GITHUB_ENV' in proxy["run"]
    browsers = [step for step in browser_steps
                if "pytest" in step.get("run", "")]
    assert len(browsers) == 2
    assert all(browser_steps.index(proxy) < browser_steps.index(step) for step in browsers)
    assert all("tests/test_console_frontend.py" in step["run"] for step in browsers)
    scoped = next(step for step in browsers if step["if"] == console)
    assert "tests/test_console_browser.py" in scoped["run"]
    assert "tests/test_console_supplement_browser.py" in scoped["run"]
    assert "test_source_browser.py" not in scoped["run"]
    assert next(step for step in browsers if step is not scoped)["if"] == product
    for name in ("container", "plugin-native"):
        assert "&& fromJSON(needs.preflight.outputs.windows).product" in jobs[name]["if"]
    container_steps = jobs["container"]["steps"]
    prepare = next(step for step in container_steps
                   if "apt-get install --yes nginx" in step.get("run", ""))
    assert "uv sync --frozen --group reader" in prepare["run"] and "if" not in prepare
    native = next(step for step in container_steps
                  if "scripts/smoke_console_ui.py" in step.get("run", ""))
    command = native["run"]
    assert container_steps.index(prepare) < container_steps.index(native) and "if" not in native
    for required in ("artifacts.py build ui --context", "tests/test_console_frontend.py",
                     "COMPANION_TEST_CONSOLE_PROXY=1", "COMPANION_TEST_CONSOLE_UI_URL=",
                     "trap cleanup EXIT", '--replacement-ui-image "$candidate_id"',
                     "companion-core:test companion-onebot-bridge:review "
                     "companion-source-reader:test"):
        assert required in command
    assert "docker image inspect" in command and '"protected_running"' in command
    assert "playwright install" not in command
    evidence = next(step for step in container_steps if step.get("with", {}).get("name")
                    == "console-ui-native-${{ github.run_attempt }}")
    assert evidence["if"] == "${{ always() }}"
    assert ".local/test-results/console-oci.xml" in evidence["with"]["path"]
    assert ".local/test-results/console-ui-replacement.json" in evidence["with"]["path"]


@pytest.mark.parametrize("job", ["preflight", "quality", "test", "browser-reader"])
def test_ui_selection_cannot_pass_if_any_console_validation_is_skipped(job):
    value = selection(["companion/console_static/app.js"])
    results = needs("full")
    results["container"]["result"] = results["plugin-native"]["result"] = "skipped"
    assert ci_policy.check_results("selected", results, value)
    results[job]["result"] = "skipped"
    assert not ci_policy.check_results("selected", results, value)


@pytest.mark.parametrize("profile,raw", [("full", ""), ("full", "null"), ("", "")])
def test_failed_preflight_still_writes_machine_result(tmp_path, monkeypatch, profile, raw):
    monkeypatch.setattr(ci_policy, "ROOT", tmp_path)
    monkeypatch.setattr(ci_policy.subprocess, "check_output", lambda *a, **k: "a\nb\n")
    monkeypatch.setenv("NEEDS_JSON", json.dumps({"preflight": {"result": "failure"}}))
    monkeypatch.setenv("CI_WINDOWS", raw)
    monkeypatch.setenv("CI_REUSE", "")
    monkeypatch.setattr(sys, "argv", ["ci_policy.py", "gate", "--profile", profile])
    with pytest.raises(SystemExit):
        ci_policy.main()
    record = json.loads((tmp_path / ".local/ci/validation.json").read_text())
    assert record["passed"] is False and record["checks"]["quality"] == "missing"


@pytest.mark.parametrize("path,reason,skips", [
    ("companion/expression.py", "known-backend-only", ["browser-reader"]),
    ("companion/console_static/app.js", "known-static-ui-only", ["container", "plugin-native"]),
    ("tests/test_console_browser.py", "known-static-ui-only", ["container", "plugin-native"]),
    ("tests/test_console_supplement_browser.py", "known-static-ui-only",
     ["container", "plugin-native"]),
])
def test_selected_receipt_records_skips_and_cannot_mint_full_reuse(
    tmp_path, monkeypatch, path, reason, skips,
):
    value = selection([path])
    results = needs("full")
    for job in skips:
        results[job]["result"] = "skipped"
    monkeypatch.setattr(ci_policy, "ROOT", tmp_path)
    monkeypatch.setattr(ci_policy.subprocess, "check_output", lambda *a, **k: "a\nb\n")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("NEEDS_JSON", json.dumps(results))
    monkeypatch.setenv("CI_WINDOWS", json.dumps(value))
    monkeypatch.setenv("CI_REUSE", "null")
    monkeypatch.setattr(sys, "argv", ["ci_policy.py", "gate", "--profile", "selected"])
    ci_policy.main()
    record = json.loads((tmp_path / ".local/ci/validation.json").read_text())
    assert record["passed"] and record["windows"]["reason"] == reason
    for job in skips:
        assert record["checks"][job] == record["expected"][job] == "skipped"
    monkeypatch.setattr(ci_reuse, "checkout", lambda: {"commit": "a", "tree": "b"})
    monkeypatch.setenv("GITHUB_SHA", "a")
    for profile in ("selected", "full"):
        monkeypatch.setenv("CI_PROFILE", profile)
        monkeypatch.setattr(sys, "argv", ["ci_reuse.py", "record"])
        with pytest.raises(ValueError):
            ci_reuse.main()


def test_workflow_preserves_receipt_on_failure_and_uses_selection_everywhere():
    jobs = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(),
                     Loader=yaml.BaseLoader)["jobs"]
    assert jobs["preflight"]["outputs"]["matrix"] == "${{ steps.plan.outputs.matrix }}"
    gate = next(step for step in jobs["ci"]["steps"] if " gate " in step.get("run", ""))
    assert gate["env"]["CI_WINDOWS"] == "${{ needs.preflight.outputs.windows }}"
    upload = next(step for step in jobs["ci"]["steps"]
                  if step.get("with", {}).get("name") == "ci-validation-${{ github.run_attempt }}")
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["path"] == ".local/ci/validation.json"


def test_job_timings_are_native_optional_and_never_invent_completion():
    env = {"CI_JOB_TIMINGS": "true", "GITHUB_REPOSITORY": "synthetic/repo",
           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
    jobs = [{"name": "test (windows-latest, 0)", "status": "completed",
             "conclusion": "success", "started_at": "2026-10-24T00:00:00Z",
             "completed_at": "2026-10-24T00:01:05Z"},
            {"name": "ci", "status": "in_progress", "started_at": "2026-10-24T00:01:05Z"}]

    def read(path):
        assert path == "repos/synthetic/repo/actions/runs/123/attempts/2/jobs?per_page=100"
        return {"total_count": len(jobs), "jobs": jobs}

    result = ci_policy.job_timings(env, read)
    assert result["available"]
    assert [job["elapsed_seconds"] for job in result["jobs"]] == [65, None]
    assert not ci_policy.job_timings({}, read)["available"]
    assert not ci_policy.job_timings(env, lambda _: {"total_count": 200, "jobs": []})["available"]

    def unavailable(_):
        raise OSError("synthetic API unavailable")

    assert ci_policy.job_timings(env, unavailable) == {"available": False, "reason": "OSError"}
