"""Read-only release plan for a running Compose deployment.

Run on the deployment host as the Docker operator. It never stops, starts, migrates or
writes the running services or their data. It reads the running containers, copies the
Core data directories into a private tmpfs directory, runs `companion.release_probe`
from the target Core image against that copy on an explicitly isolated network, deletes it, and
writes `plan.json` and `plan.md` that say what the release needs:

- NO_CHANGE: nothing to deploy.
- EDGE: only Bridge, Reader or UI images change; replace those services one at a time
  (`scripts/edge_release.py`). A Bridge change also needs its custody ACTIVE with no
  unsettled send, and the same `companion_delivery_protocol` as the running Core.
- A: Core code changes, no migration, the stored OwnerAuthority policy stays valid.
- R: with `--rolling`, an A release done without STOP by restarting one worker role at a
  time (`scripts/rolling_worker_release.py`); API, Bridge and Readers keep running.
- B: as A, plus a schema migration (timed on the copy).
- C: the policy no longer matches, or a policy change was requested (renewal, purpose).
- NOT_READY: the copy has blockers the release cannot resolve on its own.

Standard library only, so it runs with the host's Python.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

GROUPS = {"core": "companion-api", "bridge": "bridge", "reader": "bilibili-reader",
          "ui": "console-ui"}
CORE_WORKERS = ("companion-content-worker", "companion-executive-worker",
                "companion-life-worker", "companion-worker")
MIGRATION_BLOCKERS = {"MIGRATION_REQUIRED"}
SQLITE_HEADER = b"SQLite format 3\x00"
SKIP_SUFFIXES = ("-wal", "-shm", "-journal")
PROBE_TIMEOUT = 900


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result


def containers(project):
    names = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                   "--format", "{{.Names}}").stdout.split()
    if not names:
        raise RuntimeError(f"No containers for Compose project {project}")
    return json.loads(docker("inspect", *names).stdout)


def services(inspected):
    """service -> image, state, user, entrypoint, env and mounts of one container."""
    result = {}
    for item in inspected:
        labels = item["Config"].get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if not service:
            continue
        result[service] = {
            "container": item["Name"].lstrip("/"),
            "image": item["Image"],
            "state": item["State"]["Status"],
            "user": item["Config"].get("User") or "",
            "entrypoint": item["Config"].get("Entrypoint") or [],
            "env": [e for e in item["Config"].get("Env") or [] if e.startswith("COMPANION_")
                    and not e.startswith("COMPANION_TELEMETRY")],
            "mounts": [{"type": m["Type"], "source": m.get("Source"),
                        "destination": m["Destination"], "rw": bool(m.get("RW"))}
                       for m in item.get("Mounts", [])],
        }
    return result


REVISION_LABELS = ("com.companion.revision", "org.opencontainers.image.revision")
PROTOCOL_DIGEST = (
    "import hashlib,pathlib,companion_delivery_protocol as p\n"
    "root=pathlib.Path(p.__file__).parent\n"
    "h=hashlib.sha256()\n"
    "for f in sorted(root.rglob('*.py')):\n"
    "    h.update(f.relative_to(root).as_posix().encode()+b'\\0'+f.read_bytes()+b'\\0')\n"
    "print(h.hexdigest())\n")


def versions(inspected):
    """service -> code revision labels and the hashes of its read-only config files."""
    result = {}
    for item in inspected:
        labels = item["Config"].get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        if not service:
            continue
        configs = {}
        for mount in item.get("Mounts", []):
            source = Path(mount.get("Source") or "")
            if mount["Type"] == "bind" and not mount.get("RW") and source.is_file():
                configs[mount["Destination"]] = hashlib.sha256(source.read_bytes()).hexdigest()
        result[service] = {"image": item["Image"],
                           "revision": {key: labels[key] for key in REVISION_LABELS
                                        if key in labels},
                           "configs": configs}
    return result


def protocol_digest(image, python):
    """Digest of the shared Bridge/Core contract package inside an image, without network."""
    result = docker("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--entrypoint", python, image, "-c", PROTOCOL_DIGEST, check=False)
    lines = result.stdout.split() if not result.returncode else []
    return lines[-1] if lines and len(lines[-1]) == 64 else None


def bridge_custody(bridge):
    """Read-only view of the running Bridge Spool: authority state and unsettled work."""
    for mount in bridge["mounts"]:
        database = Path(mount["source"] or "", "spool.sqlite3")
        if mount["type"] != "bind" or not mount["rw"] or not database.is_file():
            continue
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            def table(name, db=db):
                return db.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                                  (name,)).fetchone() is not None

            row = (db.execute("SELECT state FROM owner_runtime WHERE id=1").fetchone()
                   if table("owner_runtime") else None)
            sends = (dict(db.execute(
                "SELECT state,COUNT(*) FROM window_attempts WHERE state IN "
                "('RESERVED','IO_STARTED','UNKNOWN') GROUP BY state"))
                if table("window_attempts") else {})
            held = (db.execute("SELECT COUNT(*) FROM held_inputs WHERE state='HELD'"
                               ).fetchone()[0] if table("held_inputs") else 0)
            pending = db.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'"
                                 ).fetchone()[0]
        finally:
            db.close()
        stopped = any(database.parent.glob("owner-*.stop"))
        return {"state": row[0] if row else "ABSENT", "stop_requested": stopped,
                "unsettled_sends": sends, "held_inputs": held, "pending_inputs": pending}
    return {"state": "UNREADABLE"}


def image_id(reference):
    """Containers report image IDs; a tag must resolve to the loaded image it names."""
    return docker("image", "inspect", "--format", "{{.Id}}", reference).stdout.strip()


def changes(current, targets):
    """Which services each target image would replace; unknown groups are an error."""
    result = {}
    for group, image in targets.items():
        anchor = GROUPS.get(group)
        if anchor not in current:
            raise ValueError(f"Unknown or absent component {group}")
        old = current[anchor]["image"]
        # After a rolling release the workers run a newer image than the API; they still
        # belong to the Core group.
        replaced = sorted(name for name, svc in current.items() if svc["image"] == old
                          or group == "core" and name in CORE_WORKERS)
        if image != old:
            result[group] = {"from": old, "to": image, "services": replaced}
    return result


def copy_file(path, destination):
    """SQLite through the backup API (committed WAL included); anything else byte for byte."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as stream:
        header = stream.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        shutil.copy2(path, destination)
        return
    src = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def copy_tree(source, target):
    """Copy a writable bind (directory or single file); WAL side files are folded in."""
    source, target = Path(source), Path(target)
    if source.is_file():
        copy_file(source, target)
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif not path.name.endswith(SKIP_SUFFIXES):
            copy_file(path, destination)


def run_probe(api, image, workspace, probe_config=None, probe_network=None):
    """Use an operator-restored private PostgreSQL copy and copied writable mounts."""
    if not probe_config or not Path(probe_config).is_file():
        return {"error": "POSTGRES_PROBE_CONFIG_REQUIRED"}
    if not probe_network or probe_network in {"host", "none", "bridge"}:
        return {"error": "POSTGRES_PROBE_NETWORK_ISOLATION_REQUIRED"}
    network, = json.loads(docker("network", "inspect", probe_network).stdout)
    peers = {row["Name"] for row in (network.get("Containers") or {}).values()}
    if not network.get("Internal") or api["container"] in peers:
        return {"error": "POSTGRES_PROBE_NETWORK_ISOLATION_REQUIRED"}
    args = ["run", "--rm", "--network", probe_network, "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    if api["user"]:
        args += ["--user", api["user"]]
    private_config = Path(workspace, "probe.toml")
    private_config.write_bytes(Path(probe_config).read_bytes())
    os.chmod(private_config, 0o600)
    # Never inherit live COMPANION_DATABASE or other environment overrides: they
    # take precedence over configuration and could reconnect a probe to production.
    config_target = "/run/companion-release-probe.toml"
    args += ["-e", f"COMPANION_CONFIG={config_target}",
             "-v", f"{private_config}:{config_target}:ro"]
    for index, mount in enumerate(api["mounts"]):
        if mount["type"] != "bind":
            raise RuntimeError(f"Unsupported mount type {mount['type']} at "
                               f"{mount['destination']}")
        if mount["rw"]:
            copy = Path(workspace, f"mount-{index}")
            copy_tree(mount["source"], copy)
            args += ["-v", f"{copy}:{mount['destination']}"]
        else:
            args += ["-v", f"{mount['source']}:{mount['destination']}:ro"]
    if api["user"]:
        uid, _, gid = api["user"].partition(":")
        subprocess.run(["chown", "-R", f"{uid}:{gid or uid}", str(workspace)], check=True)
    python = (api["entrypoint"] or ["/app/.venv/bin/python"])[0]
    args += ["--entrypoint", python, image, "-m", "companion.release_probe"]
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return {"error": "PROBE_TIMEOUT"}
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode or not lines:
        missing = "No module named companion.release_probe" in result.stderr
        return {"error": "PROBE_UNAVAILABLE" if missing else "PROBE_FAILED",
                "exit_code": result.returncode}
    return json.loads(lines[-1])


STEPS = {
    "R": [
        "不执行 STOP；授权、Bridge、Reader 和 API 都不动",
        "用 scripts/rolling_worker_release.py 依次滚动 content、executive、life、chat worker",
        "每个 worker 先平滑停止（做完当前单元，退出码 0、释放文件锁、没有新增 UNKNOWN），"
        "再用新镜像启动并确认它重新持有锁",
        "API 上的代码改动要等下一次 STOP 发布才生效",
        "观察日志和 worker 心跳",
    ],
    "EDGE": [
        "不执行 STOP；Core API 和 worker 都不动",
        "用 scripts/edge_release.py 按 UI、Reader、Bridge 的顺序逐个替换下列服务",
        "每个服务先平滑停止（退出码 0；Bridge 还要释放 instance.lock、没有未结发送），"
        "再用新镜像启动，确认运行稳定且配置只有镜像变化",
        "失败时该服务回到旧镜像并停止后续服务；旧镜像也起不来时转为 STOP 发布",
        "观察健康检查、Bridge 的 bridge_shutdown_report 与 owner_backfill 日志",
    ],
    "A": [
        "确认 Bridge 预检中的暂存输入为 0（STOP 会把仍在暂存的消息记为 OWNER_STOPPED 并删除）",
        "Core 执行 STOP（同一份授权）；Bridge 执行 owner-handoff-prepare",
        "确认 API 以外的 worker、Bridge、Reader 都已退出",
        "换镜像，用同一份 policy 执行 preview/activate（revision +2）",
        "Bridge 执行 owner-handoff",
        "先启动 life-worker，90 秒后启动其余 worker、Bridge 和 Reader",
        "观察健康检查、STOP 状态和日志",
    ],
    "B": [
        "确认 Bridge 预检中的暂存输入为 0（STOP 会把仍在暂存的消息记为 OWNER_STOPPED 并删除）",
        "Core 执行 STOP（同一份授权）；Bridge 执行 owner-handoff-prepare",
        "确认 API、worker、Bridge、Reader 都已退出",
        "加密备份数据库，并单独复制最新遗忘日志",
        "用新镜像执行一次 `companion init` 迁移（API 也停止）",
        "启动 API，用同一份 policy 执行 preview/activate（revision +2）",
        "Bridge 执行 owner-handoff",
        "先启动 life-worker，90 秒后启动其余 worker、Bridge 和 Reader",
        "观察健康检查、STOP 状态和日志",
    ],
    "C": [
        "准备新的 policy（续期、purpose 或能力配置变化），交主人批准",
        "确认 Bridge 预检中的暂存输入为 0（STOP 会把仍在暂存的消息记为 OWNER_STOPPED 并删除）",
        "Core 执行 STOP；确认所有写入进程已退出",
        "有未结 UNKNOWN 时走离线 carry 和 owner-carry-handoff-prepare；否则 owner-handoff-prepare",
        "加密备份数据库，复制最新遗忘日志；需要迁移时执行一次 `companion init`",
        "启动 API，preview/activate 新 policy",
        "Bridge 执行 owner-handoff",
        "先启动 life-worker，90 秒后启动其余 worker、Bridge 和 Reader",
        "观察健康检查、STOP 状态和日志",
    ],
}


def protocol_mismatch(edge):
    protocol = (edge or {}).get("protocol") or {}
    return not protocol.get("bridge") or protocol["bridge"] != protocol.get("core")


def edge_blockers(changed, edge):
    """Why a Bridge replacement without STOP cannot proceed; empty when nothing blocks it.

    The new Bridge inherits the old one's custody, so that custody must be ACTIVE with
    nothing in flight, and it must speak the protocol of the Core it meets.
    """
    if "bridge" not in changed:
        return []
    if edge is None:
        return ["BRIDGE_PREFLIGHT_NOT_RUN"]
    blockers = []
    custody = edge.get("custody") or {}
    if custody.get("state") != "ACTIVE" or custody.get("stop_requested"):
        blockers.append("BRIDGE_CUSTODY_NOT_ACTIVE")
    if custody.get("unsettled_sends"):
        blockers.append("BRIDGE_SEND_UNSETTLED")
    if protocol_mismatch(edge):
        blockers.append("BRIDGE_CORE_PROTOCOL_MISMATCH")
    return blockers


def classify(changed, probe, policy_change=None, rolling=False, edge=None):
    """Return (class, reasons). Pure, so the decision is testable without Docker.

    `rolling` turns A into R only when nothing but the Core image changes; a release that
    also replaces Bridge, Reader or UI images keeps the STOP procedure. `edge` is the
    Bridge preflight (`bridge_custody` and protocol digests) when the Bridge changes.
    """
    if not changed and not policy_change:
        return "NO_CHANGE", []
    if "core" not in changed and not policy_change:
        blockers = edge_blockers(changed, edge)
        return ("NOT_READY", blockers) if blockers else ("EDGE", [])
    if probe is None:
        return "NOT_READY", ["PROBE_NOT_RUN"]
    if probe.get("error"):
        return "NOT_READY", [probe["error"]]
    migration = probe["migration"]
    if migration.get("needed") and not migration.get("ok"):
        return "NOT_READY", ["MIGRATION_FAILED_ON_COPY"]
    final = probe.get("after") or probe["before"]
    blockers = [b for b in final["blockers"] if b not in MIGRATION_BLOCKERS]
    authority = probe.get("authority", {})
    if authority.get("state") == "UNREADABLE":
        blockers.append("AUTHORITY_UNREADABLE")
    # STOP hands the old custody off, but the new Bridge must still match the new Core.
    if "bridge" in changed and edge is not None and protocol_mismatch(edge):
        blockers.append("BRIDGE_CORE_PROTOCOL_MISMATCH")
    if blockers:
        return "NOT_READY", blockers
    reasons = []
    if policy_change:
        reasons.append("POLICY_CHANGE_REQUESTED")
    if authority.get("policy_problem"):
        reasons.append(authority["policy_problem"])
    if reasons:
        return "C", reasons
    if migration.get("needed"):
        return "B", [f"SCHEMA {probe['before']['schema']} -> {final['schema']}"]
    if "core" not in changed:
        return "EDGE", []
    if rolling and set(changed) == {"core"}:
        return "R", ["NO_MIGRATION_POLICY_VALID_WORKERS_ONLY"]
    return "A", []


def report(plan):
    lines = [f"# 发布预检：{plan['class']}", ""]
    if plan["reasons"]:
        lines += ["原因：" + "、".join(plan["reasons"]), ""]
    lines += ["## 镜像变化", ""]
    for group, change in plan["changes"].items():
        lines.append(f"- {group}：{change['from'][:19]} → {change['to'][:19]}"
                     f"（{', '.join(change['services'])}）")
    if not plan["changes"]:
        lines.append("- 无")
    probe = plan.get("probe") or {}
    if probe and not probe.get("error"):
        before, migration = probe["before"], probe["migration"]
        authority = probe.get("authority", {})
        lines += ["", "## 隔离副本检查（目标代码）", "",
                  f"- schema：{before['schema']} → 目标 {before['expected_schema']}",
                  "- 迁移：" + (f"需要，副本上耗时 {migration['seconds']} 秒"
                               if migration.get("ok") else
                               "需要但失败" if migration.get("needed") else "不需要"),
                  f"- 授权：{authority.get('state')} r{authority.get('revision')}，"
                  + ("新代码下仍有效" if not authority.get("policy_problem")
                     else f"新代码下失效（{authority['policy_problem']}）"),
                  f"- 未结工作：任务 {before['active_jobs']}，待发送 {before['open_deliveries']}"
                  f"（不确定 {before['uncertain_deliveries']}），"
                  f"UNKNOWN 模型调用 {before['unknown_model_calls']}"]
    edge = plan.get("edge") or {}
    if edge:
        custody = edge.get("custody") or {}
        lines += ["", "## Bridge 预检（只读）", "",
                  f"- 授权状态：{custody.get('state')}"
                  + ("，已请求 STOP" if custody.get("stop_requested") else ""),
                  f"- 未结发送：{custody.get('unsettled_sends') or 0}，"
                  f"暂存输入 {custody.get('held_inputs', 0)}，"
                  f"待上传输入 {custody.get('pending_inputs', 0)}",
                  *(["- 与 Core 的协议包：" + ("不一致或无法读取" if protocol_mismatch(edge)
                                             else "一致")] if "protocol" in edge else [])]
    if plan.get("versions"):
        lines += ["", "## 运行版本", ""]
        for name, version in sorted(plan["versions"].items()):
            revision = next(iter(version["revision"].values()), "未标注")
            lines.append(f"- {name}：{version['image'][:19]}，代码 {revision[:12]}，"
                         f"只读配置 {len(version['configs'])} 个")
    lines += ["", "## 步骤", ""]
    lines += [f"{i}. {step}" for i, step in enumerate(STEPS.get(plan["class"], []), 1)]
    if plan["class"] == "NOT_READY":
        lines.append("先处理上面的原因，再重新预检。")
    if plan["class"] == "C":
        lines += ["", "需要主人批准新的 policy。"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="companion-main")
    parser.add_argument("--target", action="append", default=[], metavar="COMPONENT=IMAGE",
                        help="core, bridge, reader or ui, with a loaded image ID")
    parser.add_argument("--policy-change", metavar="REASON")
    parser.add_argument("--rolling", action="store_true",
                        help="Plan an A release as a rolling worker restart (class R)")
    parser.add_argument("--probe-config", type=Path,
                        help="Private Core TOML for an independently restored PostgreSQL copy")
    parser.add_argument("--probe-network",
                        help="Internal Docker network containing only the isolated probe database")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    targets = {group: image_id(image) for group, image in
               (item.split("=", 1) for item in args.target)}
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    inspected = containers(args.project)
    current = services(inspected)
    changed = changes(current, targets)
    # Read-only and cheap; a STOP release must also know what the Bridge still holds.
    edge = {"custody": bridge_custody(current["bridge"])} if "bridge" in current else None
    if "bridge" in changed:
        core_image = (changed["core"]["to"] if "core" in changed
                      else current["companion-api"]["image"])
        core_python = (current["companion-api"]["entrypoint"] or ["/app/.venv/bin/python"])[0]
        edge["protocol"] = {"bridge": protocol_digest(changed["bridge"]["to"], "python"),
                            "core": protocol_digest(core_image, core_python)}
    probe = None
    if "core" in changed or args.policy_change:
        image = changed["core"]["to"] if "core" in changed else current["companion-api"]["image"]
        base = "/dev/shm" if Path("/dev/shm").is_dir() else None
        workspace = Path(tempfile.mkdtemp(prefix="release-plan-", dir=base))
        try:
            probe = run_probe(current["companion-api"], image, workspace,
                              args.probe_config, args.probe_network)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
    kind, reasons = classify(changed, probe, args.policy_change, rolling=args.rolling,
                             edge=edge)
    plan = {"schema_version": 1, "project": args.project, "class": kind, "reasons": reasons,
            "changes": changed, "probe": probe, "edge": edge, "versions": versions(inspected),
            "services": {name: {"image": svc["image"], "state": svc["state"]}
                         for name, svc in current.items()}}
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False)
                                           + "\n", encoding="utf-8")
    (args.output / "plan.md").write_text(report(plan), encoding="utf-8")
    print(report(plan))
    return 0 if kind != "NOT_READY" else 2


if __name__ == "__main__":
    sys.exit(main())
