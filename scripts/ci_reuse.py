"""Reuse a merged PR's complete CI only for the exact tree it actually checked out."""

import argparse
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlencode

if __package__:
    from . import ci_container_artifacts as images
else:
    import ci_container_artifacts as images

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/ci.yml"
RECORD = "verified-tree.json"
REQUIRED_JOBS = {
    "preflight", "quality", "ci", "container", "browser-reader", "plugin-native", "flutter-client",
    *(f"test ({platform}-latest, {shard})" for platform in ("ubuntu", "windows")
      for shard in range(8)),
}


def checkout(root=ROOT):
    def git(value):
        return subprocess.check_output(
            ["git", "rev-parse", "--verify", value], cwd=root, text=True, timeout=10,
        ).strip()

    return {"commit": git("HEAD"), "tree": git("HEAD^{tree}")}


def api(path, *, binary=False):
    # The runner's existing gh handles authenticated API calls and strips API
    # credentials when following the artifact's signed storage redirect.
    print("Checking CI evidence: " + path.split("?", 1)[0])
    response = subprocess.run(
        ["gh", "api", "--hostname", "github.com", path],
        capture_output=True, check=True, timeout=30,
    ).stdout
    if len(response) > 1_000_000:
        raise ValueError("CI API response too large")
    return response if binary else json.loads(response)


def unpack_record(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != RECORD or entries[0].file_size > 4096:
            raise ValueError("Invalid CI verification artifact")
        return json.loads(archive.read(entries[0]))


def verified_run(run, repo, head, read, *, expected_event="pull_request"):
    """Every condition comes from this repository's API and this run's artifact."""
    if (
        type(run.get("id")) is not int or type(run.get("run_attempt")) is not int
        or run["id"] <= 0 or run["run_attempt"] <= 0
        or expected_event not in {"pull_request", "push"}
        or run.get("event") != expected_event or run.get("path") != WORKFLOW
        or expected_event == "push" and run.get("head_branch") != "main"
        or run.get("status") != "completed" or run.get("conclusion") != "success"
        or run.get("head_sha") != head
        or run.get("repository", {}).get("full_name") != repo
        or run.get("head_repository", {}).get("full_name") != repo
    ):
        return None
    prefix = f"repos/{repo}/actions/runs/{run['id']}"
    artifacts = read(prefix + "/artifacts?per_page=100")
    matches = [item for item in artifacts["artifacts"]
               if item.get("name") == f"ci-full-{run['run_attempt']}"
               and item.get("expired") is False]
    if len(matches) != 1:
        return None
    item = matches[0]
    if (type(item.get("id")) is not int or item["id"] <= 0
        or type(item.get("size_in_bytes")) is not int or not 0 < item["size_in_bytes"] <= 16384):
        return None
    record = unpack_record(read(f"repos/{repo}/actions/artifacts/{item['id']}/zip", binary=True))
    if (
        type(record.get("version")) is not int or record["version"] != 3
        or record.get("profile") != "full"
        or record.get("coverage") != {"windows_product": True, "windows_browser": True}
        or record.get("repository") != repo or record.get("event") != expected_event
        or record.get("run_id") != run["id"] or record.get("attempt") != run["run_attempt"]
        or not re.fullmatch(r"[0-9a-f]{40}", record.get("tree", ""))
        or not re.fullmatch(r"[0-9a-f]{40}", record.get("commit", ""))
    ):
        return None
    jobs = read(prefix + f"/attempts/{run['run_attempt']}/jobs?per_page=100")
    if (
        jobs.get("total_count") != len(REQUIRED_JOBS)
        or len(jobs["jobs"]) != len(REQUIRED_JOBS)
        or {job.get("name") for job in jobs["jobs"]} != REQUIRED_JOBS
        or any(job.get("status") != "completed" or job.get("conclusion") != "success"
               for job in jobs["jobs"])
    ):
        return None
    try:
        expected = {key: record[key] for key in ("commit", "tree", "run_id", "attempt")}
        metadata = images.verified_metadata(record.get("container_images"), artifacts["artifacts"],
                                            read, repo, {"repository": repo, **expected})
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile):
        return None
    return metadata, {"run_id": run["id"], "attempt": run["run_attempt"], "tree": record["tree"]}


def find_verified_run(event_name, ref, event, *, root=ROOT, read=api):
    """Bounded lookup; any missing, expired or ambiguous evidence keeps full CI."""
    default = event.get("repository", {}).get("default_branch")
    if event_name != "push" or not default or ref != "refs/heads/" + default:
        return None
    repo = event["repository"]["full_name"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None
    target = checkout(root)
    if target["commit"] != event.get("after"):
        return None
    pulls = read(f"repos/{repo}/commits/{target['commit']}/pulls?per_page=100")
    merged = [pr for pr in pulls if pr.get("merged_at") and pr.get("state") == "closed"
              and pr.get("merge_commit_sha") == target["commit"]
              and pr.get("base", {}).get("ref") == default
              and pr.get("base", {}).get("repo", {}).get("full_name") == repo
              and pr.get("head", {}).get("repo", {}).get("full_name") == repo]
    if len(merged) != 1:
        return None
    head = merged[0]["head"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    query = urlencode({"event": "pull_request", "head_sha": head, "per_page": 20})
    runs = read(f"repos/{repo}/actions/workflows/ci.yml/runs?{query}")["workflow_runs"]
    # Inspect the latest run, including its latest attempt. An older green run
    # must not conceal a newer failed, cancelled, or still-running validation.
    candidates = [run for run in runs if run.get("head_sha") == head]
    if not candidates:
        return None
    latest = max(candidates, key=lambda run: run["id"])
    verified = verified_run(latest, repo, head, read)
    if verified is None:
        return None
    _, proof = verified
    return proof if proof["tree"] == target["tree"] else None


def validate_reuse(env=os.environ, *, root=ROOT):
    record = json.loads(env.get("CI_REUSE", "null"))
    if (
        not isinstance(record, dict) or set(record) != {"run_id", "attempt", "tree"}
        or type(record["run_id"]) is not int or record["run_id"] <= 0
        or type(record["attempt"]) is not int or record["attempt"] <= 0
        or record["tree"] != checkout(root)["tree"]
        or env.get("GITHUB_EVENT_NAME") != "push"
    ):
        raise ValueError("Verified CI tree binding required")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["record", "validate"])
    args = parser.parse_args()
    if args.operation == "validate":
        proof = validate_reuse()
        url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{proof['run_id']}"
        print("Verified full CI reused: " + url)
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(f"Reused [full CI]({url}) for identical tree `{proof['tree']}`.\n")
    else:
        target = checkout()
        windows = json.loads(os.environ.get("CI_WINDOWS", "null"))
        if os.environ["CI_PROFILE"] != "full" or target["commit"] != os.environ["GITHUB_SHA"]:
            raise ValueError("Only a complete checked-out full run can record verification")
        if not isinstance(windows, dict) or windows.get("product") is not True or (
            windows.get("browser") is not True
        ):
            raise ValueError("Full Windows product and browser coverage required")
        record = {"version": 3, "profile": "full", **target,
                  "coverage": {"windows_product": True, "windows_browser": True},
                  "repository": os.environ["GITHUB_REPOSITORY"],
                  "event": os.environ["GITHUB_EVENT_NAME"],
                  "run_id": int(os.environ["GITHUB_RUN_ID"]),
                  "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                  "container_images": images.metadata_reference()}
        path = ROOT / ".local/ci" / RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
