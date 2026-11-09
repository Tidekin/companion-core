"""Validate the native dev Compose merge with synthetic files, without a daemon."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

if __package__:
    from .ci_container_artifacts import POSTGRES_IMAGE
else:
    from ci_container_artifacts import POSTGRES_IMAGE

ROOT = Path(__file__).resolve().parents[1]
CORE = {
    "migrate": ["init"],
    "companion-api": ["serve", "--host", "0.0.0.0"],
    "companion-worker": ["worker"],
    "companion-life-worker": ["life-worker"],
    "companion-executive-worker": ["executive-worker"],
    "companion-content-worker": ["content-worker"],
}
SOURCE = {"/app/config.toml": "deploy/qq-beta-core.example.toml",
          "/app/companion": "companion", "/app/migrations": "migrations", "/app/db": "db"}
SYNTHETIC_DB_ENV = "".join(
    f"COMPANION_DEV_{role}_PASSWORD=synthetic-{role.lower()}-database-password-for-tests-only\n"
    for role in ("ADMIN", "CORE", "ARCHIVE")
)
SYNTHETIC_TOKEN = "synthetic-dev-compose-token-" * 2


def environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("COMPANION_", "ONEBOT_", "SNOWLUMA_", "COMPOSE_", "DOCKER_"))}


def compose_command(root, env_file, project, *, context=None):
    if not re.fullmatch(r"companion-dev-[a-z0-9][a-z0-9-]{0,47}", project):
        raise ValueError("DEV_PROJECT_NAME_REQUIRED")
    command = ["docker"]
    if context:
        command += ["--context", context]
    return [*command, "compose", "--project-directory", str(root), "--env-file", str(env_file),
            "-p", project, "-f", str(root / "compose.yaml"),
            "-f", str(root / "deploy/compose.dev.yaml")]


def render(root, env_file, project, *, context=None, env=None):
    command = compose_command(root, env_file, project, context=context)
    process_env = environment() if env is None else env
    result = subprocess.run(
        [*command, "--profile", "*", "config", "--format", "json"],
        cwd=root, env=process_env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError("DEV_COMPOSE_RENDER_FAILED")
    config = json.loads(result.stdout)
    version = subprocess.run(
        [*command, "version", "--short"], cwd=root, env=process_env,
        capture_output=True, text=True, timeout=15,
    )
    if version.returncode:
        raise RuntimeError("DEV_COMPOSE_VERSION_FAILED")
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)",
                         version.stdout.strip())
    # Official Compose 2.30.0..2.40.3 use bool + omitempty for this field.
    # From 5.0.0 it is OptOut; unknown versions keep strict missing-value rejection.
    if match and (2, 30, 0) <= tuple(map(int, match.groups())) <= (2, 40, 3):
        for service in config.get("services", {}).values():
            for mount in service.get("volumes", []):
                if mount.get("type") == "bind" and isinstance(mount.get("bind"), dict):
                    mount["bind"].setdefault("create_host_path", False)
    return config


def validate(config, root, project, token):
    """Check the resolved native configuration before any explicit container smoke."""
    def require(condition):
        if not condition:
            raise ValueError("DEV_COMPOSE_ISOLATION_FAILED")

    require(len(token) >= 32 and config["name"] == project)
    services = config["services"]
    require(set(services) == {*CORE, "dev-backup", "dev-postgres"})
    database = services["dev-postgres"]
    credentials = database["environment"]
    require(set(credentials) == {"POSTGRES_PASSWORD", "DEV_CORE_PASSWORD", "DEV_ARCHIVE_PASSWORD"})
    require(all(isinstance(value, str) and len(value) >= 24 for value in credentials.values()))
    require(len(set(credentials.values()) | {token}) == 4)
    require(database["image"] == POSTGRES_IMAGE and database["user"] == "postgres")
    require(not any(database.get(key) for key in (
        "build", "ports", "privileged", "network_mode", "cap_add", "entrypoint", "command",
        "env_file", "devices", "pid", "ipc",
    )))
    require(database.get("read_only") and database.get("cap_drop") == ["ALL"])
    require(database.get("security_opt") == ["no-new-privileges:true"])
    require(set(database["networks"]) == {"companion"})
    db_mounts = {item["target"]: item for item in database["volumes"]}
    require(len(database["volumes"]) == 2 and set(db_mounts) == {
        "/var/lib/postgresql/data", "/docker-entrypoint-initdb.d/10-dev.sql"})
    data = db_mounts["/var/lib/postgresql/data"]
    require(data["type"] == "volume" and data["source"] == "dev-postgres-data")
    setup = db_mounts["/docker-entrypoint-initdb.d/10-dev.sql"]
    require(setup["type"] == "bind" and setup.get("read_only") is True)
    require(Path(setup["source"]).resolve() == (root / "deploy/dev-postgres-init.sql").resolve())
    require(setup.get("bind", {}).get("create_host_path") is False)
    for name in (*CORE, "dev-backup"):
        service = services[name]
        backup = name == "dev-backup"
        values = service["environment"]
        expected = {
            "COMPANION_CONFIG": "/app/config.toml", "COMPANION_MODE": "demo",
            "COMPANION_OWNER_QQ_ID": "10001", "COMPANION_BOT_QQ_ID": "10002",
            "COMPANION_BRIDGE_TOKEN": token, "COMPANION_MODEL_KEY": "",
            "COMPANION_MODEL_URL": "https://synthetic.invalid/v1",
            "COMPANION_INPUT_RATE": "0", "COMPANION_CACHED_INPUT_RATE": "0",
            "COMPANION_OUTPUT_RATE": "0", "COMPANION_MONTHLY_LIMIT_MICRO_USD": "0",
            "COMPANION_AUTONOMOUS_DAILY_MICRO_USD": "0", "COMPANION_MEMORY_DAILY_MICRO_USD": "0",
            "COMPANION_DATABASE": "host=dev-postgres dbname=companion user="
                                  + ("dev_archive" if backup else "dev_core"),
            "PGPASSWORD": credentials["DEV_ARCHIVE_PASSWORD" if backup else "DEV_CORE_PASSWORD"],
            "COMPANION_STATE_DIRECTORY": "/data",
            "COMPANION_ERASURE_LEDGER": "/privacy/erasures.jsonl",
            "COMPANION_EMBEDDING_CACHE_DIR": "/data/embedding-cache",
            "COMPANION_SEGMENTED_REPLY": "false",
        }
        require(set(values) == set(expected) | ({"COMPANION_BACKUP_KEY"} if backup else set()))
        require(all(values.get(key) == value for key, value in expected.items()))
        require(not service.get("env_file") and not service.get("ports"))
        require(service["image"] == f"companion-core-dev:{project}")
        require(Path(service["build"]["context"]).resolve() == root.resolve())
        require(not service.get("entrypoint") and not service.get("privileged"))
        require(not service.get("network_mode") and set(service["networks"]) == {"companion"})
        require(service.get("read_only") and service.get("user") == "10001:10001")
        require(service.get("cap_drop") == ["ALL"] and not service.get("cap_add"))
        require(service.get("security_opt") == ["no-new-privileges:true"])
        volumes = {entry["target"]: entry for entry in service["volumes"]}
        targets = {*SOURCE, "/data", "/privacy"}
        require(set(volumes) == targets and len(volumes) == len(service["volumes"]))
        for target, relative in SOURCE.items():
            entry = volumes[target]
            require(entry["type"] == "bind" and entry.get("read_only") is True)
            require(Path(entry["source"]).resolve() == (root / relative).resolve())
            require(entry.get("bind", {}).get("create_host_path") is False)
        expected_volumes = {"/data": "dev-snapshots" if backup else "core-data",
                            "/privacy": "erasure-ledger"}
        for target, source in expected_volumes.items():
            entry = volumes[target]
            require(entry["type"] == "volume" and entry["source"] == source)
            require(bool(entry.get("read_only")) == (backup and target != "/data"))
        require(service["command"] == (["--help"] if backup else CORE[name]))
        dependencies = service.get("depends_on", {})
        if name in {"migrate", "dev-backup", "companion-api"}:
            require(not dependencies)
        else:
            require(set(dependencies) == {"companion-api"})
            require(dependencies["companion-api"]["condition"] == "service_healthy")
    require(not services["companion-api"].get("depends_on"))
    require(not services["companion-api"].get("profiles"))
    require(not services["companion-worker"].get("profiles"))
    for name, profile in (("migrate", "dev-maintenance"), ("dev-backup", "dev-maintenance"),
                          ("companion-life-worker", "life"),
                          ("companion-executive-worker", "tools"),
                          ("companion-content-worker", "content")):
        require(services[name]["profiles"] == [profile])
    networks, volumes = config["networks"], config["volumes"]
    require(set(networks) == {"companion"})
    require(networks["companion"].get("internal") is True)
    require(networks["companion"]["name"] == f"{project}_companion")
    require(not networks["companion"].get("external"))
    require(set(volumes) == {"core-data", "erasure-ledger", "dev-snapshots", "dev-postgres-data"})
    for name, value in volumes.items():
        require(value["name"] == f"{project}_{name}")
        require(not value.get("external") and not value.get("driver_opts"))
    return {network["name"] for network in networks.values()} | {
        volume["name"] for volume in volumes.values()}


def verify(root=ROOT):
    if shutil.which("docker") is None:
        raise RuntimeError("DEV_COMPOSE_CLI_REQUIRED")
    scratch = root / ".local/test-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dev-compose-", dir=scratch) as directory:
        fixture = Path(directory)
        (fixture / "deploy").mkdir()
        for relative in ("compose.yaml", "deploy/compose.dev.yaml",
                         "deploy/qq-beta-core.example.toml", "deploy/dev-postgres-init.sql"):
            shutil.copyfile(root / relative, fixture / relative)
        for relative in ("companion", "migrations", "db"):
            (fixture / relative).mkdir()
        (fixture / ".env").write_text(
            "COMPANION_MODE=live\nCOMPANION_MODEL_KEY=synthetic-root-poison\n"
            "COMPOSE_PROJECT_NAME=production-poison\n", encoding="utf-8")
        live = fixture / "config.toml"
        live.write_text('mode="live"\nmodel_key="synthetic-config-poison"\n', encoding="utf-8")
        env_file = fixture / "dev.env"
        env_file.write_text(
            f"COMPANION_DEV_BRIDGE_TOKEN={SYNTHETIC_TOKEN}\n"
            "COMPANION_DEV_BACKUP_KEY=synthetic-maintenance-only\n" + SYNTHETIC_DB_ENV,
            encoding="utf-8",
        )
        polluted = {**environment(), "COMPANION_MODE": "live",
                    "COMPANION_MODEL_KEY": "synthetic-host-poison",
                    "COMPOSE_PROJECT_NAME": "production-poison"}
        resources = []
        for project in ("companion-dev-proof-a", "companion-dev-proof-b"):
            config = render(fixture, env_file, project, env=polluted)
            resources.append(validate(config, fixture, project, SYNTHETIC_TOKEN))
        assert not resources[0] & resources[1]
        live.unlink()
        config = render(fixture, env_file, "companion-dev-proof-a", env=polluted)
        validate(config, fixture, "companion-dev-proof-a", SYNTHETIC_TOKEN)
        default = subprocess.run(
            [*compose_command(fixture, env_file, "companion-dev-proof-a"), "config", "--services"],
            cwd=fixture, env=polluted, capture_output=True, text=True, check=True, timeout=60,
        )
        assert set(default.stdout.split()) == {"companion-api", "companion-worker", "dev-postgres"}
    return {"evidence_type": "OFFLINE_FIXTURE", "native_compose_config": "PASS",
            "isolated_projects": 2, "all_core_services": 6,
            "missing_root_config": "PASS", "daemon_contacted": False,
            "real_env_read": False, "container_smoke": "NOT_RUN"}


def main():
    try:
        print(json.dumps(verify()))
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, AssertionError) as error:
        print(json.dumps({"native_compose_config": "FAIL", "error": type(error).__name__,
                          "daemon_contacted": False, "container_smoke": "NOT_RUN"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
