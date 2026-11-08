"""Rolling worker release for a class R plan: no STOP, one worker role at a time.

Run on the deployment host as the Docker operator, after `scripts/release_plan.py
--rolling` reported class R for the same Core image. For each Core worker service, in a
fixed order, it:

1. checks the OwnerAuthority is still ACTIVE and records the UNKNOWN model call count;
2. stops the old container with a long grace period, so the worker finishes its current
   unit (model call, send, commit) and exits 0 after releasing its file locks;
3. refuses to continue if the exit was not clean, a lock is still held or a new UNKNOWN
   appeared;
4. starts the service again from its original Compose files plus an image-only override,
   and waits until the new container is running and holds the same locks.

The API, Bridge, Readers and UI keep running on their current images; API code changes
take effect at the next STOP release. A failure starts the old image again for that
service and stops. Standard library only, so it runs with the host's Python.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# content and executive first: they hold no chat or Life work the owner is waiting on.
ORDER = ("companion-content-worker", "companion-executive-worker",
         "companion-life-worker", "companion-worker")
LOCK_KINDS = {
    "companion-content-worker": ("content-worker",),
    "companion-executive-worker": ("executive-worker",),
    "companion-life-worker": ("life-worker", "diary-worker"),
    "companion-worker": ("worker",),
}
GRACE_SECONDS = 150
START_SECONDS = 90
STEADY_SECONDS = 20
CONFIG_LABEL = "com.docker.compose.project.config_files"
REVISION_LABELS = ("com.companion.revision", "org.opencontainers.image.revision")
WORKDIR_LABEL = "com.docker.compose.project.working_dir"


def docker(*args, check=True, timeout=300):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False,
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result


def inspect(project):
    names = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                   "--format", "{{.Names}}").stdout.split()
    rows = json.loads(docker("inspect", *names).stdout) if names else []
    return {row["Config"]["Labels"]["com.docker.compose.service"]: row for row in rows
            if (row["Config"].get("Labels") or {}).get("com.docker.compose.service")}


def effective(row):
    """What must not change across the swap except the image itself."""
    config, host = row["Config"], row["HostConfig"]
    return {"env": sorted(config.get("Env") or []), "user": config.get("User") or "",
            "cmd": config.get("Cmd"), "entrypoint": config.get("Entrypoint"),
            "mounts": sorted((m["Type"], m.get("Source"), m["Destination"], bool(m.get("RW")))
                             for m in row.get("Mounts", [])),
            "restart": host.get("RestartPolicy"), "network": host.get("NetworkMode")}


def plan_services(plan, rows, image):
    """The worker services this plan may roll, after checking the plan still matches."""
    if plan.get("class") != "R":
        raise ValueError("A class R plan from release_plan.py --rolling is required")
    change = plan.get("changes", {}).get("core")
    if not change or change["to"] != image or set(plan["changes"]) != {"core"}:
        raise ValueError("The plan was made for a different Core image")
    for name, saved in plan["services"].items():
        if name not in rows or rows[name]["Image"] != saved["image"]:
            raise ValueError(f"{name} changed since the plan was made")
    # A worker may run a newer image than the API after an earlier rolling release.
    services = [name for name in ORDER if name in rows and name in change["services"]
                and rows[name]["Image"] != image]
    if not services:
        raise ValueError("No Core worker needs the planned image")
    return services


def lock_paths(state_directory, service):
    return [Path(state_directory) / f"{kind}.lock" for kind in LOCK_KINDS[service]]


def lock_holders(path, locks_text):
    """PIDs holding a FLOCK on `path`, read from /proc/locks."""
    info = path.stat()
    holders = []
    for line in locks_text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1] != "FLOCK":
            continue
        major, minor, inode = parts[5].split(":")
        if (int(inode) == info.st_ino and int(major, 16) == os.major(info.st_dev)
                and int(minor, 16) == os.minor(info.st_dev)):
            holders.append(int(parts[4]))
    return holders


LEDGER_SCRIPT = '''
import json
import psycopg
from companion.config import Settings
settings=Settings.load(apply_operator_config=False)
with psycopg.connect(settings.database.get_secret_value()) as db:
    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    authority=db.execute("SELECT revision,state FROM owner_authority_revisions "
                         "ORDER BY revision DESC LIMIT 1").fetchone()
    unknown=db.execute("SELECT count(*) FROM model_usage WHERE state='UNKNOWN'").fetchone()[0]
    jobs=dict(db.execute("SELECT state,count(*) FROM jobs WHERE state IN "
                         "('QUEUED','RUNNING','RETRY_WAIT') GROUP BY state").fetchall())
    outbox=dict(db.execute("SELECT state,count(*) FROM outbox WHERE state IN "
                          "('PENDING','LEASED','SENDING','RETRY_WAIT',"
                          "'DELIVERY_UNCERTAIN') GROUP BY state").fetchall())
print(json.dumps({'authority':authority,'unknown':unknown,'jobs':jobs,'deliveries':outbox}))
'''


def ledger(api):
    """Read the native snapshot inside the API's existing deployment configuration."""
    python = (api["Config"].get("Entrypoint") or ["/app/.venv/bin/python"])[0]
    return json.loads(docker("exec", api["Id"], python, "-B", "-c", LEDGER_SCRIPT,
                             timeout=30).stdout)


def compose(row, override):
    labels = row["Config"]["Labels"]
    files = [item for path in labels[CONFIG_LABEL].split(",") for item in ("-f", path)]
    if override is not None:
        files += ["-f", str(override)]
    return ["compose", "-p", labels["com.docker.compose.project"],
            "--project-directory", labels[WORKDIR_LABEL], *files]


def write_override(path, images):
    path.write_text(json.dumps({"services": {name: {"image": image}
                                             for name, image in images.items()}},
                               indent=2) + "\n", encoding="utf-8")


def wait_started(project, service, image, state_directory, old):
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        row = inspect(project).get(service)
        locks = Path("/proc/locks").read_text()
        if (row and row["Image"] == image and row["State"]["Running"]
                and row["Id"] != old["Id"]
                and all(lock_holders(path, locks)
                        for path in lock_paths(state_directory, service))):
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"{service} did not start and take its locks in {START_SECONDS}s")
    time.sleep(STEADY_SECONDS)
    row = inspect(project)[service]
    if not row["State"]["Running"] or row["RestartCount"]:
        raise RuntimeError(f"{service} did not stay up")
    if effective(row) != effective(old):
        raise RuntimeError(f"{service} configuration changed beyond the image")
    return row


def roll(project, service, image, state_directory, override, images, receipt):
    rows = inspect(project)
    old = rows[service]
    before = ledger(rows["companion-api"])
    if not before["authority"] or before["authority"][1] != "ACTIVE":
        raise RuntimeError("OwnerAuthority is not ACTIVE; use the STOP procedure")
    labels = old["Config"].get("Labels") or {}
    step = {"service": service, "old_container": old["Id"], "old_image": old["Image"],
            # Compose labels; an image-only override does not rewrite them.
            "deployment_revision": {key: labels[key] for key in REVISION_LABELS
                                    if key in labels},
            "authority": before["authority"], "unknown_before": before["unknown"],
            "jobs_before": before["jobs"], "deliveries_before": before["deliveries"],
            "stop_started": datetime.now(UTC).isoformat()}
    receipt["steps"].append(step)
    docker("stop", "--time", str(GRACE_SECONDS), old["Id"], timeout=GRACE_SECONDS + 60)
    stopped = json.loads(docker("inspect", old["Id"]).stdout)[0]["State"]
    locks = Path("/proc/locks").read_text()
    after = ledger(rows["companion-api"])
    step.update(exit_code=stopped["ExitCode"], stopped_at=stopped["FinishedAt"],
                unknown_after_stop=after["unknown"], jobs_after_stop=after["jobs"],
                deliveries_after_stop=after["deliveries"])
    if stopped["Running"] or stopped["ExitCode"] != 0:
        raise RuntimeError(f"{service} did not stop cleanly (exit {stopped['ExitCode']})")
    if any(lock_holders(path, locks) for path in lock_paths(state_directory, service)):
        raise RuntimeError(f"{service} still holds a worker lock after stopping")
    if after["unknown"] != before["unknown"]:
        raise RuntimeError(f"{service} stop left a new UNKNOWN model call")
    images[service] = image
    write_override(override, images)
    docker(*compose(old, override), "up", "-d", "--no-deps", "--no-build", "--pull", "never",
           "--force-recreate", service, timeout=300)
    new = wait_started(project, service, image, state_directory, old)
    step.update(new_container=new["Id"], started=new["State"]["StartedAt"],
                ready_at=datetime.now(UTC).isoformat(), result="ROLLED")


def restore(project, service, row, override, images):
    """Start the service again on its previous image after a failed step."""
    images.pop(service, None)
    write_override(override, images)
    docker(*compose(row, override), "up", "-d", "--no-deps", "--no-build", "--pull", "never",
           "--force-recreate", service, timeout=300)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="companion-main")
    parser.add_argument("--plan", type=Path, required=True, help="plan.json of class R")
    parser.add_argument("--core-image", required=True)
    parser.add_argument("--state-directory", required=True,
                        help="Host path of the Core state directory containing worker locks")
    parser.add_argument("--output", type=Path, required=True,
                        help="New directory kept for the override file and the receipt")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    rows = inspect(args.project)
    # Containers report image IDs; resolve a tag to the loaded image it names.
    args.core_image = docker("image", "inspect", "--format", "{{.Id}}",
                             args.core_image).stdout.strip()
    services = plan_services(plan, rows, args.core_image)
    if not Path(args.state_directory).is_dir():
        raise SystemExit("Core state directory not found on this host")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    override = args.output / "compose.rolling.json"
    images = {}
    receipt = {"schema_version": 1, "project": args.project, "image": args.core_image,
               "services": services, "steps": [], "override": str(override)}
    status = 0
    for service in services:
        try:
            roll(args.project, service, args.core_image, args.state_directory, override, images,
                 receipt)
        except Exception as error:  # noqa: BLE001 - report, put the old image back, stop
            receipt["steps"][-1:] and receipt["steps"][-1].update(
                result="FAILED", error=str(error)[:300])
            if service in images or not inspect(args.project)[service]["State"]["Running"]:
                restore(args.project, service, rows[service], override, images)
                # Only the image goes back; jobs, deliveries and ledger rows stay as written.
                receipt["steps"][-1:] and receipt["steps"][-1].update(rollback="OLD_IMAGE")
            receipt["stopped_at"] = service
            status = 2
            break
    receipt["finished"] = datetime.now(UTC).isoformat()
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n",
                                              encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
