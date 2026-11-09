"""Own a fresh native PostgreSQL cluster inside an ephemeral GitHub runner checkout.

Uses the runner's installed PostgreSQL binaries, never its pre-existing service.
https://github.com/actions/runner-images/tree/main/images
"""

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / ".local" / "ci-postgres"
PRIVATE = LAB / "connection.private.json"


def binaries():
    if os.name == "nt":
        directory = Path(os.environ["PGBIN"])
    else:
        config = shutil.which("pg_config")
        if not config:
            raise RuntimeError("CI_POSTGRES_BINARIES_REQUIRED")
        directory = Path(subprocess.check_output([config, "--bindir"], text=True).strip())
    if not (directory / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")).is_file():
        raise RuntimeError("CI_POSTGRES_BINARIES_REQUIRED")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop"])
    arguments = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("EPHEMERAL_GITHUB_RUNNER_REQUIRED")
    if arguments.action == "stop":
        if not PRIVATE.exists():
            return
        value = json.loads(PRIVATE.read_text("utf8"))
        if Path(value["data"]).resolve() != (LAB / "data").resolve():
            raise RuntimeError("CI_POSTGRES_OWNERSHIP_MISMATCH")
        subprocess.run([str(binaries() / "pg_ctl"), "-D", value["data"],
                        "-m", "fast", "-w", "-t", "30", "stop"], check=True, timeout=40)
        return
    LAB.mkdir(parents=True, exist_ok=False)
    value = {"host": "127.0.0.1", "dbname": "postgres", "user": "companion_test",
             "password": secrets.token_urlsafe(32), "data": str(LAB / "data")}
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        value["port"] = port.getsockname()[1]
    PRIVATE.write_text(json.dumps(value), encoding="utf8")
    PRIVATE.chmod(0o600)
    password = LAB / "init-password.private"
    password.write_text(value["password"], encoding="utf8")
    password.chmod(0o600)
    directory = binaries()
    try:
        subprocess.run([str(directory / "initdb"), "-D", value["data"],
                        "--username=" + value["user"], "--pwfile=" + str(password),
                        "--auth-local=trust", "--auth-host=scram-sha-256",
                        "--encoding=UTF8", "--locale=C"], check=True, timeout=60)
    finally:
        password.unlink(missing_ok=True)
    with (LAB / "data" / "postgresql.conf").open("a", encoding="utf8") as configuration:
        configuration.write("\nlisten_addresses='127.0.0.1'\nunix_socket_directories=''\n"
                            f"port={value['port']}\nmax_connections=40\nshared_buffers='32MB'\n")
    subprocess.run([str(directory / "pg_ctl"), "-D", value["data"],
                    "-l", str(LAB / "postgres.log"), "-w", "-t", "30", "start"],
                   check=True, timeout=40)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf8") as environment:
        environment.write(f"COMPANION_POSTGRES_TEST_CONFIG={PRIVATE}\n")
    with Path(os.environ["GITHUB_PATH"]).open("a", encoding="utf8") as path:
        path.write(str(directory) + "\n")
    print("Fresh PostgreSQL test cluster ready on runner loopback")


if __name__ == "__main__":
    main()
