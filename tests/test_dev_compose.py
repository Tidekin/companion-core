"""Native Compose merge contracts; pytest never starts containers or an engine."""

import asyncio
import base64
import copy
import json
import secrets
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest
from psycopg.conninfo import conninfo_to_dict

from scripts import smoke_dev_compose as smoke
from scripts import verify_dev_compose as dev_compose
from scripts.verify_dev_compose import (
    ROOT,
    SYNTHETIC_DB_ENV,
    SYNTHETIC_TOKEN,
    compose_command,
    environment,
    render,
    validate,
    verify,
)
from tests.native_probe_worker import NativeWatch
from tests.postgres_fixture import isolated_database

NATIVE_WORKER_HOST = "tests.native_probe_worker"
NATIVE_TRACE_SIGNAL = getattr(signal, "SIGUSR1", None)


async def capture_native_probe_workers(data, children, child_logs, redact):
    """Append failure-only observations, preserving the original error and exit states."""
    captures = data["worker_captures"] = []
    for child in children:
        role, path, confirmed_host = child_logs[child.pid]
        if role != "worker":
            continue
        before = next(row for row in data["children"] if row["pid"] == child.pid)
        capture = {"pid": child.pid, "natural_returncode": before["returncode"],
                   "returncode_before_capture": child.returncode,
                   "returncode_after_capture": child.returncode,
                   "started_at": monotonic(), "traceback_observed": False}
        captures.append(capture)
        if child.returncode is not None:
            capture["status"] = "already_exited"
            continue
        if not confirmed_host or NATIVE_TRACE_SIGNAL is None:
            capture["status"] = "unavailable"
            continue
        offset = None
        try:
            offset = path.stat().st_size
            capture["signal_attempted_at"] = monotonic()
            child.send_signal(NATIVE_TRACE_SIGNAL)
            capture["signal_sent"] = True
            await asyncio.wait_for(child.wait(), 2)
            capture["status"] = "exited_after_signal"
        except Exception as error:
            capture["status"] = (
                "capture_timeout" if isinstance(error, TimeoutError) else "capture_error"
            )
            capture["error_type"] = type(error).__name__
        finally:
            capture["returncode_after_capture"] = child.returncode
            capture["finished_at"] = monotonic()
        if offset is not None:
            try:
                with path.open("rb") as output:
                    output.seek(offset)
                    text = redact(output.read().decode("utf-8", errors="replace"))
                capture["output_tail"] = text[-8192:]
                capture["output_truncated"] = len(text) > 8192
                capture["traceback_observed"] = "(most recent call first):" in text
            except OSError as error:
                capture["log_error_type"] = type(error).__name__
    return data


@pytest.mark.parametrize("outcome,status", [
    ("captured", "exited_after_signal"), ("exited", "already_exited"),
    ("unconfirmed", "unavailable"), ("race", "capture_error"),
    ("signal_error", "capture_error"), ("timeout", "capture_timeout"),
    ("missing_log", "capture_error"), ("log_removed", "exited_after_signal"),
])
def test_native_probe_capture_preserves_failure_and_separates_exit_states(
    tmp_path, monkeypatch, outcome, status,
):
    # Only a fake process uses SIGTERM here; Windows never signals a real worker.
    monkeypatch.setattr(sys.modules[__name__], "NATIVE_TRACE_SIGNAL", signal.SIGTERM)
    log = tmp_path / "worker.log"
    log.write_text("original worker output\n", encoding="utf-8")
    secret = "SYNTHETIC_CAPTURE_SECRET"
    signals, cancelled = [], []

    class Child:
        pid = 1234
        returncode = 7 if outcome == "exited" else None

        def send_signal(self, signum):
            signals.append(signum)
            if outcome == "race":
                self.returncode = 7
                raise ProcessLookupError
            if outcome == "signal_error":
                raise OSError
            with log.open("a", encoding="utf-8") as output:
                output.write("x" * 9000 + secret + "\nThread (most recent call first):\n"
                             '  File "tests/native_probe_worker.py", line 20 in watch\n')
            if outcome == "log_removed":
                log.unlink()

        async def wait(self):
            if outcome == "timeout":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            self.returncode = -int(signal.SIGTERM)
            return self.returncode

    child = Child()
    data = {"stderr": "INDEPENDENT_WORKER_DID_NOT_COMPLETE original projection",
            "returncode": 1, "timed_out": False,
            "children": [{"role": "worker", "pid": child.pid,
                          "returncode": child.returncode, "output_tail": "original output"}]}
    original = copy.deepcopy(data)
    if outcome == "missing_log":
        log.unlink()
    result = asyncio.run(capture_native_probe_workers(
        data, [child], {child.pid: ("worker", log, outcome != "unconfirmed")},
        lambda text: text.replace(secret, "[REDACTED]"),
    ))
    assert result is data
    assert {key: data[key] for key in original} == original
    capture, = data["worker_captures"]
    assert capture["status"] == status
    assert capture["natural_returncode"] == original["children"][0]["returncode"]
    assert capture["returncode_before_capture"] == capture["natural_returncode"]
    assert capture["returncode_after_capture"] == child.returncode
    assert signals == ([] if outcome in {"exited", "unconfirmed", "missing_log"}
                       else [signal.SIGTERM])
    if outcome in {"captured", "log_removed"}:
        assert capture["natural_returncode"] is None
        assert capture["returncode_after_capture"] == -int(signal.SIGTERM)
    if outcome == "captured":
        assert capture["traceback_observed"] and capture["output_truncated"]
        assert len(capture["output_tail"]) == 8192
        assert secret not in str(data) and "[REDACTED]" in capture["output_tail"]
        assert "original worker output" not in capture["output_tail"]
    if outcome == "timeout":
        assert cancelled == [True] and child.returncode is None
    if outcome == "log_removed":
        assert capture["log_error_type"] == "FileNotFoundError"


def test_native_probe_watch_forwards_original_native_call_and_close():
    result, stop = object(), object()
    calls = []

    class Native:
        def watch(self, *args):
            calls.append(args)
            return result

        def close(self):
            calls.append("close")
            return result

    native = Native()
    watch = NativeWatch(native)
    assert watch.native is native
    assert watch.watch(50, 50, 60000, stop) is result
    assert watch.close() is result
    assert calls == [(50, 50, 60000, stop), "close"]


@pytest.mark.skipif(sys.platform != "linux", reason="Native SIGUSR1 capture requires Linux")
def test_native_probe_linux_signal_captures_named_watch_and_reaps_own_child(tmp_path):
    async def run():
        watched = tmp_path / "watched"
        watched.mkdir()
        log = tmp_path / "worker.log"
        code = '''import faulthandler, signal, sys, threading
from watchfiles._rust_notify import RustNotify
from tests.native_probe_worker import NativeWatch
from tests.postgres_fixture import isolated_database
signal.signal(signal.SIGUSR1, signal.SIG_DFL)
faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
native = RustNotify([sys.argv[1]], False, False, 300, False, False)
entered = threading.Event()
class EnteredNative:
    def watch(self, *args):
        entered.set()
        return native.watch(*args)
    def close(self):
        return native.close()
watch = NativeWatch(EnteredNative())
thread = threading.Thread(target=watch.watch, args=(50, 50, 60000, threading.Event()))
thread.start()
assert entered.wait(5)
print("READY", flush=True)
thread.join()
'''
        with log.open("wb") as output:
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-c", code, str(watched), cwd=ROOT, env=environment(),
                stdout=asyncio.subprocess.PIPE, stderr=output,
            )
        try:
            assert await asyncio.wait_for(child.stdout.readline(), 5) == b"READY\n"
            data = {"stderr": "original business failure", "returncode": 1,
                    "children": [{"role": "worker", "pid": child.pid, "returncode": None}]}
            await capture_native_probe_workers(
                data, [child], {child.pid: ("worker", log, True)}, lambda text: text,
            )
            capture, = data["worker_captures"]
            assert data["stderr"] == "original business failure"
            assert data["children"][0]["returncode"] is None
            assert capture["natural_returncode"] is None
            assert capture["status"] == "exited_after_signal"
            assert capture["returncode_after_capture"] == -int(signal.SIGUSR1)
            assert capture["traceback_observed"]
            assert "native_probe_worker.py" in capture["output_tail"]
            assert " in watch" in capture["output_tail"]
            assert child.returncode == await child.wait()
        finally:
            if child.returncode is None:
                child.kill()
            await asyncio.wait_for(child.communicate(), 5)

    asyncio.run(run())


@pytest.fixture(scope="module")
def native_config(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI required; native configuration coverage not performed")
    env_file = tmp_path_factory.mktemp("dev-compose-env") / "dev.env"
    env_file.write_text(f"COMPANION_DEV_BRIDGE_TOKEN={SYNTHETIC_TOKEN}\n" + SYNTHETIC_DB_ENV,
                        encoding="utf-8")
    return render(ROOT, env_file, "companion-dev-test")


def test_native_merge_rejects_host_and_root_pollution_and_separates_projects(native_config):
    assert verify()["native_compose_config"] == "PASS"
    assert len(validate(native_config, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)) == 5


@pytest.mark.parametrize("service", ["migrate", "companion-api", "companion-worker",
    "companion-life-worker", "companion-executive-worker", "companion-content-worker"])
def test_optional_services_cannot_inherit_live_environment(native_config, service):
    changed = copy.deepcopy(native_config)
    changed["services"][service]["environment"]["COMPANION_MODEL_KEY"] = "synthetic-live-key"
    with pytest.raises(ValueError, match="DEV_COMPOSE_ISOLATION_FAILED"):
        validate(changed, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)


@pytest.mark.parametrize("change", ["root-config", "writeable-source", "missing-source-created",
    "extra-mount", "host-port", "external-network", "fixed-network-name", "external-volume",
    "shared-volume-name", "automatic-migration", "root-user", "paid-mode", "extra-env"])
def test_unsafe_rendered_configuration_is_rejected(native_config, change):
    config = copy.deepcopy(native_config)
    api = config["services"]["companion-api"]
    mounts = {entry["target"]: entry for entry in api["volumes"]}
    if change == "root-config":
        mounts["/app/config.toml"]["source"] = str(ROOT / "config.toml")
    elif change == "writeable-source":
        mounts["/app/companion"]["read_only"] = False
    elif change == "missing-source-created":
        mounts["/app/companion"]["bind"]["create_host_path"] = True
    elif change == "extra-mount":
        api["volumes"].append({"type": "bind", "source": "/var/run/docker.sock",
                               "target": "/var/run/docker.sock"})
    elif change == "host-port":
        api["ports"] = [{"target": 8765, "published": "8765"}]
    elif change == "external-network":
        config["networks"]["companion"]["external"] = True
    elif change == "fixed-network-name":
        config["networks"]["companion"]["name"] = "companion-internal"
    elif change == "external-volume":
        config["volumes"]["core-data"]["external"] = True
    elif change == "shared-volume-name":
        config["volumes"]["core-data"]["name"] = "production-data"
    elif change == "automatic-migration":
        api["depends_on"] = {"migrate": {"condition": "service_completed_successfully"}}
    elif change == "root-user":
        api["user"] = "0:0"
    elif change == "paid-mode":
        api["environment"]["COMPANION_MODE"] = "live"
    else:
        api["environment"]["COMPANION_CONTROL_TOKEN"] = "synthetic-host-poison"
    with pytest.raises(ValueError, match="DEV_COMPOSE_ISOLATION_FAILED"):
        validate(config, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)


def replay_render(monkeypatch, config, version, *, failed_stage=None):
    calls = []
    synthetic_env = {"SYNTHETIC_ENV": "dedicated"}
    command = compose_command(ROOT, ROOT / ".local/synthetic.env", "companion-dev-test",
                              context="synthetic-local")

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["cwd"] == ROOT and kwargs["env"] is synthetic_env
        assert kwargs["capture_output"] and kwargs["text"]
        assert args[:len(command)] == command
        if args[len(command):] == ["--profile", "*", "config", "--format", "json"]:
            stage, output = "config", json.dumps(config)
        else:
            assert args[len(command):] == ["version", "--short"]
            stage, output = "version", version
        return subprocess.CompletedProcess(args, int(stage == failed_stage), output,
                                           "synthetic-private-stderr")

    monkeypatch.setattr(dev_compose.subprocess, "run", run)
    result = render(ROOT, ROOT / ".local/synthetic.env", "companion-dev-test",
                    context="synthetic-local", env=synthetic_env)
    assert len(calls) == 2
    return result


@pytest.mark.parametrize("version,legacy", [
    ("2.30.0", True), ("v2.38.2", True), ("2.40.3", True),
    ("2.29.9", False), ("2.40.4", False), ("2.40.3-custom", False),
    ("5.0.0", False), ("5.5.0", False), ("6.0.0", False), ("unknown", False),
])
def test_render_missing_bind_false_uses_only_known_native_schema(
    native_config, monkeypatch, version, legacy,
):
    config = copy.deepcopy(native_config)
    for service in config["services"].values():
        for mount in service["volumes"]:
            if mount["type"] == "bind":
                del mount["bind"]["create_host_path"]
    rendered = replay_render(monkeypatch, config, version)
    if legacy:
        assert len(validate(rendered, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)) == 5
    else:
        with pytest.raises(ValueError, match="DEV_COMPOSE_ISOLATION_FAILED"):
            validate(rendered, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)


@pytest.mark.parametrize("version", ["2.40.3", "5.5.0"])
@pytest.mark.parametrize("value", [False, True, None, 0, 1, "false", [], {}])
def test_render_preserves_explicit_bind_values(native_config, monkeypatch, version, value):
    config = copy.deepcopy(native_config)
    mount = config["services"]["companion-api"]["volumes"][0]
    mount["bind"]["create_host_path"] = value
    rendered = replay_render(monkeypatch, config, version)
    actual = rendered["services"]["companion-api"]["volumes"][0]["bind"]["create_host_path"]
    assert type(actual) is type(value) and actual == value
    if value is False:
        validate(rendered, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)
    else:
        with pytest.raises(ValueError, match="DEV_COMPOSE_ISOLATION_FAILED"):
            validate(rendered, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)


@pytest.mark.parametrize("version", ["2.40.3", "5.5.0"])
@pytest.mark.parametrize("change", ["missing-bind", "source", "readonly"])
def test_render_does_not_hide_other_bind_failures(native_config, monkeypatch, version, change):
    config = copy.deepcopy(native_config)
    mount = config["services"]["companion-api"]["volumes"][0]
    if change == "missing-bind":
        del mount["bind"]
    elif change == "source":
        mount["source"] = str(ROOT / "config.toml")
    else:
        mount["read_only"] = False
    rendered = replay_render(monkeypatch, config, version)
    with pytest.raises(ValueError, match="DEV_COMPOSE_ISOLATION_FAILED"):
        validate(rendered, ROOT, "companion-dev-test", SYNTHETIC_TOKEN)


@pytest.mark.parametrize("stage,code", [
    ("config", "DEV_COMPOSE_RENDER_FAILED"), ("version", "DEV_COMPOSE_VERSION_FAILED"),
])
def test_render_rejects_failed_native_command(monkeypatch, stage, code):
    with pytest.raises(RuntimeError, match=code):
        replay_render(monkeypatch, {}, "2.40.3", failed_stage=stage)


def test_smoke_marks_render_phase_before_render_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "require_local_context", lambda context: None)

    def fail(*args, **kwargs):
        raise RuntimeError("DEV_COMPOSE_RENDER_FAILED")

    monkeypatch.setattr(smoke, "render", fail)
    proof = {}
    with pytest.raises(RuntimeError, match="DEV_COMPOSE_RENDER_FAILED"):
        smoke.smoke("synthetic-local", root=tmp_path, proof=proof)
    assert proof["phase"] == "compose_render" and proof["checks"] == []


@pytest.mark.parametrize("error,code", [
    (RuntimeError("DEV_COMPOSE_RENDER_FAILED"), "DEV_COMPOSE_RENDER_FAILED"),
    (RuntimeError("DEV_COMPOSE_VERSION_FAILED"), "DEV_COMPOSE_VERSION_FAILED"),
    (subprocess.CalledProcessError(7, "DEV_SMOKE_COMMAND_FAILED",
                                  "synthetic-private-output", "POSTGRES_READ_ONLY"),
     "DEV_SMOKE_COMMAND_FAILED"),
    (subprocess.CalledProcessError(2, "DEV_SMOKE_EXPECTED_DIAGNOSTIC_MISSING",
                                  "synthetic-private-output", "synthetic-private-stderr"),
     "DEV_SMOKE_EXPECTED_DIAGNOSTIC_MISSING"),
    (RuntimeError("synthetic-private-exception"), None),
    (subprocess.CalledProcessError(7, ["synthetic-private-command"],
                                  "synthetic-private-output", "synthetic-private-stderr"), None),
])
def test_smoke_report_exposes_only_fixed_error_codes(monkeypatch, tmp_path, capsys, error, code):
    for name in ("compose.yaml", "deploy/compose.dev.yaml", "scripts/verify_dev_compose.py",
                 "scripts/smoke_dev_compose.py", "tests/test_dev_compose.py",
                 "docs/dev-environment.md", "Dockerfile", "uv.lock",
                 "deploy/qq-beta-core.example.toml", "deploy/dev-postgres-init.sql"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture", encoding="utf-8")

    def fail(context, *, image, proof):
        proof["phase"] = "compose_render"
        raise error

    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "smoke", fail)
    monkeypatch.setattr(smoke, "run", lambda command: "synthetic-source-identity")
    monkeypatch.setattr(sys, "argv", ["smoke", "--context", "synthetic-local"])
    with pytest.raises(SystemExit) as stopped:
        smoke.main()
    assert stopped.value.code == 2
    text = (tmp_path / ".local/verification/dev-compose-smoke.json").read_text(encoding="utf-8")
    proof = json.loads(text)
    assert proof["phase"] == "compose_render" and proof["container_smoke"] == "FAIL"
    assert proof.get("error_code") == code
    if isinstance(error, subprocess.CalledProcessError) and code:
        assert proof["command_exit_code"] == error.returncode
        assert proof["stderr_kind"] in smoke._STDERR_KINDS
    else:
        assert "command_exit_code" not in proof and "stderr_kind" not in proof
    captured = capsys.readouterr()
    assert "synthetic-private" not in text + captured.out + captured.err


@pytest.mark.parametrize("stderr,kind", [
    ("psycopg.errors.ReadOnlySqlTransaction: private query", "POSTGRES_READ_ONLY"),
    ("psycopg.errors.InsufficientPrivilege: private relation", "POSTGRES_PRIVILEGE"),
    ("psycopg.OperationalError: private DSN", "POSTGRES_OPERATIONAL"),
    ("BACKUP_DESTINATION_EXISTS", "BACKUP_DESTINATION_EXISTS"),
    ("POSTGRES_BACKUP_TOOL_FAILED", "POSTGRES_BACKUP_TOOL_FAILED"),
    ("AssertionError: synthetic-private-text", "PYTHON_ASSERTION"),
    ("PermissionError: synthetic-private-path", "PYTHON_PERMISSION"),
    ("synthetic-private-unrecognized", "UNCLASSIFIED"),
    ("AssertionError\n" + "synthetic-private-text" * 1000, "UNCLASSIFIED"),
])
def test_command_failure_keeps_only_bounded_fixed_stderr_kind(monkeypatch, stderr, kind):
    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 7, "synthetic-private-output", stderr)

    monkeypatch.setattr(smoke.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as stopped:
        smoke.run(["synthetic-private-command", "synthetic-private-token"])
    error = stopped.value
    assert error.returncode == 7 and error.cmd == "DEV_SMOKE_COMMAND_FAILED"
    assert error.stderr == kind and error.output is None
    assert "synthetic-private" not in str(error) + repr(vars(error))


def test_expected_stderr_mismatch_has_its_own_fixed_error_code(monkeypatch):
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 2, "synthetic-private-output", ""))
    with pytest.raises(subprocess.CalledProcessError) as stopped:
        smoke.run(["synthetic-private-command"], expected=2, contains="BACKUP_DESTINATION_EXISTS")
    assert stopped.value.cmd == "DEV_SMOKE_EXPECTED_DIAGNOSTIC_MISSING"
    assert stopped.value.returncode == 2 and stopped.value.stderr == "UNCLASSIFIED"


@pytest.mark.parametrize("returncode,expected,contains,stderr", [
    (0, 0, None, "synthetic-private-stderr"),
    (2, 2, "BACKUP_DESTINATION_EXISTS", "BACKUP_DESTINATION_EXISTS"),
])
def test_command_success_keeps_expected_exit_and_output_contract(
    monkeypatch, returncode, expected, contains, stderr,
):
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, returncode, " PASS\n", stderr))
    assert smoke.run(["synthetic-command"], expected=expected, contains=contains) == "PASS"


@pytest.mark.parametrize("project", ["companion", "production", "companion-dev-",
                                     "companion-dev-../other", "companion-dev-X"])
def test_commands_require_a_distinct_dev_project(project, tmp_path):
    with pytest.raises(ValueError, match="DEV_PROJECT_NAME_REQUIRED"):
        compose_command(ROOT, Path(tmp_path) / "dev.env", project)


@pytest.mark.parametrize("endpoint", ["ssh://example.invalid", "tcp://127.0.0.1:2375",
                                      "https://example.invalid"])
def test_container_smoke_refuses_remote_context_before_engine_access(monkeypatch, endpoint):
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}])

    monkeypatch.setattr(smoke, "run", inspect)
    with pytest.raises(smoke.PrerequisiteUnavailable, match="LOCAL_CONTEXT_REQUIRED"):
        smoke.require_local_context("synthetic")
    assert calls == [["docker", "context", "inspect", "synthetic"]]


@pytest.mark.parametrize("endpoint,accepted", [
    ("npipe:////./pipe/docker_engine", True),
    ("npipe:////./pipe/dockerDesktopLinuxEngine", True),
    ("unix:///var/run/docker.sock", True),
    ("unix:///run/user/1000/docker.sock", True),
    ("unix:///home/dev/.docker/desktop/docker.sock", True),
    ("npipe:////remote-host/pipe/docker_engine", False),
    ("npipe:////localhost/pipe/docker_engine", False),
    ("npipe://remote-host/pipe/docker_engine", False),
    (r"npipe://\\remote-host\pipe\docker_engine", False),
    (r"\\remote-host\pipe\docker_engine", False),
    ("npipe://./pipe/docker_engine", False),
    ("npipe:////./pipe/", False),
    ("npipe:////./pipe/docker_engine/extra", False),
    ("npipe:////./pipe/docker_engine?target=remote", False),
    ("npipe:////./pipe/docker_engine#fragment", False),
    ("npipe:////./pipe/docker engine", False),
    ("unix://remote-host/var/run/docker.sock", False),
    ("unix:////remote-host/var/run/docker.sock", False),
    ("unix://relative.sock", False),
    ("unix:///var/run/docker.sock?target=remote", False),
    ("unix:///var/run/docker.sock#fragment", False),
    ("unix:///", False),
    (None, False),
])
def test_container_smoke_queries_engine_only_for_canonical_local_endpoint(
    monkeypatch, endpoint, accepted,
):
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}])
        return json.dumps({"OSType": "linux"})

    monkeypatch.setattr(smoke, "run", inspect)
    expected = [["docker", "context", "inspect", "synthetic"]]
    if accepted:
        smoke.require_local_context("synthetic")
        expected.append(["docker", "--context", "synthetic", "info", "--format", "{{json .}}"])
    else:
        with pytest.raises(smoke.PrerequisiteUnavailable, match="LOCAL_CONTEXT_REQUIRED"):
            smoke.require_local_context("synthetic")
    assert calls == expected


def test_native_one_off_options_parse_without_engine(native_config):
    result = subprocess.run(
        ["docker", "compose", "run", *smoke.RUN_OPTIONS, "--entrypoint", smoke.PYTHON, "--help"],
        cwd=ROOT, env=environment(), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, "DEV_COMPOSE_ONE_OFF_OPTIONS_REJECTED"


@pytest.fixture
def probe_databases():
    with isolated_database() as source, isolated_database() as before, isolated_database() as after:
        yield source, before, after


def test_container_probe_contracts_through_native_api_and_independent_worker(
    tmp_path, probe_databases,
):
    asyncio.run(native_probe_contracts(tmp_path, probe_databases))


async def native_probe_contracts(tmp_path, databases):
    """Exercise the probe bodies natively; Docker mount/network evidence stays separate."""
    database, before, after = databases
    ledger, archive = tmp_path / "erasures.jsonl", tmp_path / "pre-init.enc"
    env = {**environment(), "COMPANION_CONFIG": str(tmp_path / "absent.toml"),
           "COMPANION_MODE": "demo", "COMPANION_OWNER_QQ_ID": "10001",
           "COMPANION_BOT_QQ_ID": "10002", "COMPANION_BRIDGE_TOKEN": secrets.token_urlsafe(32),
           "COMPANION_DATABASE": database.get_secret_value(),
           "COMPANION_ERASURE_LEDGER": str(ledger),
           "COMPANION_STATE_DIRECTORY": str(tmp_path / "core-state"),
           "COMPANION_MEMORY_ENABLED": "false", "COMPANION_SEGMENTED_REPLY": "false",
           "COMPANION_MODEL_KEY": "", "COMPANION_MODEL_URL": "https://synthetic.invalid/v1",
           "COMPANION_BACKUP_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
    targets = {}
    for phase, target in (("before", before), ("after", after)):
        config = tmp_path / f"restore-{phase}.toml"
        values = {"database": target.get_secret_value(), "state_directory": str(tmp_path / phase),
                  "erasure_ledger": str(ledger), "owner_qq_id": "10001", "bot_qq_id": "10002",
                  "bridge_token": env["COMPANION_BRIDGE_TOKEN"]}
        config.write_text("".join(f"{k}={json.dumps(v)}\n" for k, v in values.items()), "utf8")
        config.chmod(0o600)
        targets[phase] = str(config)
    children, child_logs, stages = [], {}, []
    log_dir = tmp_path / "child-logs"
    log_dir.mkdir()

    def redact(text):
        for name in ("COMPANION_BRIDGE_TOKEN", "COMPANION_BACKUP_KEY"):
            text = text.replace(env[name], "[REDACTED]")
        password = conninfo_to_dict(env["COMPANION_DATABASE"]).get("password")
        return text.replace(password, "[REDACTED]") if password else text

    def safe_text(text):
        return redact(text)[-8192:]

    async def failure_data(process, output=b"", error=b"", *, timed_out=False):
        stages.append({"stage": "failure_snapshot", "at": monotonic()})
        data = {
            "pid": process.pid, "returncode": process.returncode, "timed_out": timed_out,
            "stdout": safe_text(output.decode(errors="replace")),
            "stderr": safe_text(error.decode(errors="replace")),
            "stages": list(stages),
            "children": [
                {"role": child_logs[child.pid][0], "pid": child.pid,
                 "returncode": child.returncode,
                 "output_tail": safe_text(child_logs[child.pid][1].read_text(
                     encoding="utf-8", errors="replace"))}
                for child in children
            ],
        }
        try:
            return await capture_native_probe_workers(data, children, child_logs, redact)
        except Exception as error:
            data["capture_error_type"] = type(error).__name__
            return data

    async def start(*args, role=None):
        if role is None:
            return await asyncio.create_subprocess_exec(
                sys.executable, "-B", *args, cwd=ROOT, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        confirmed_host = role == "worker" and sys.platform == "linux"
        if confirmed_host:
            args = ("-m", NATIVE_WORKER_HOST)
        log = log_dir / f"{len(child_logs)}-{role}.log"
        with log.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", *args, cwd=ROOT, env=env,
                stdout=output, stderr=asyncio.subprocess.STDOUT,
            )
        child_logs[process.pid] = role, log, confirmed_host
        stages.append({"stage": role + "_spawned", "at": monotonic(), "pid": process.pid})
        return process

    async def command(*args, expected=0, probe=None):
        stages.append({"stage": "command_started", "at": monotonic(), "probe": probe})
        process = await start(*args)
        try:
            try:
                output, error = await asyncio.wait_for(process.communicate(), 30)
            except TimeoutError:
                raise AssertionError(await failure_data(process, timed_out=True)) from None
            if process.returncode != expected:
                raise AssertionError(await failure_data(process, output, error))
            return output.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            stages.append({"stage": "command_finished", "at": monotonic(), "probe": probe})

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    await command("-m", "companion", "init")
    try:
        children.append(await start("-m", "companion", "serve", "--port", str(port), role="api"))
        children.append(await start("-m", "companion", "worker", role="worker"))
        http_code = smoke.HTTP_PROBE.replace("127.0.0.1:8765", f"127.0.0.1:{port}")
        for number in (1, 2, 3):
            result = json.loads(await command("-c", http_code, str(number), probe=number))
            assert result["independent_worker"] == "PASS"
        assert all(process.returncode is None for process in children)
    finally:
        for process in children:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), 15)
    assert (await command("-c", smoke.SEED_RESERVATION)).strip() == "PASS"
    await command("-m", "companion", "backup", str(archive))
    await command("-m", "companion", "backup", str(archive), expected=2)
    code = smoke.RESTORE_PROBE.replace("/data/pre-init.enc", archive.as_posix())
    code = code.replace("/privacy/erasures.jsonl", ledger.as_posix())
    await command("-m", "companion", "restore", str(archive), targets["before"])
    before_result = json.loads(await command("-c", code, "before", targets["before"]))
    assert before_result["pending_state"] == "DELIVERY_UNCERTAIN"
    assert (await command("-c", smoke.ERASE_AFTER_BACKUP)).strip() == "PASS"
    await command("-m", "companion", "restore", str(archive), targets["after"])
    after_result = json.loads(await command("-c", code, "after", targets["after"]))
    assert after_result["latest_ledger_replay"] == "PASS"
    await command("-m", "companion", "init")
    children = []
    try:
        children.append(await start("-m", "companion", "serve", "--port", str(port), role="api"))
        ready = smoke.HTTP_READY_PROBE.replace("127.0.0.1:8765", f"127.0.0.1:{port}")
        assert (await command("-c", ready + "\nprint('PASS')")).strip() == "PASS"
        children.append(await start("-m", "companion", "worker", role="worker"))
        result = json.loads(await command("-c", http_code, "4", probe=4))
        assert result["event"] == 4 and result["duplicate_job_usage_delivery"] == "PASS"
        assert all(process.returncode is None for process in children)
    finally:
        for process in children:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), 15)


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING"])
def test_native_probe_failure_keeps_worker_exit_and_safe_state(tmp_path, monkeypatch, state,
                                                             probe_databases):
    spawn = asyncio.create_subprocess_exec
    captured = {}

    async def fail_worker(*args, **kwargs):
        if args[2:] in (("-m", "companion", "worker"), ("-m", NATIVE_WORKER_HOST)):
            captured["token"] = kwargs["env"]["COMPANION_BRIDGE_TOKEN"]
            captured["backup_key"] = kwargs["env"]["COMPANION_BACKUP_KEY"]
            args = (*args[:2], "-c", "import os, sys; "
                    "print('SYNTHETIC_WORKER_BOOT_FAILURE ' + "
                    "os.environ['COMPANION_BRIDGE_TOKEN'] + ' ' + "
                    "os.environ['COMPANION_BACKUP_KEY'], file=sys.stderr, flush=True); "
                    "raise SystemExit(7)")
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_worker)
    probe = smoke.HTTP_PROBE
    if state == "RUNNING":
        before = 'with psycopg.connect(os.environ["COMPANION_DATABASE"], autocommit=True,'
        controlled = '''with psycopg.connect(os.environ["COMPANION_DATABASE"]) as controlled:
    controlled.execute("UPDATE jobs SET state='RUNNING' WHERE job_id=%s", (first["job_id"],))
    controlled.execute("INSERT INTO runtime_meta (key,value) VALUES ('worker_loop',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps({"session": "a" * 32, "sequence": 2,
                     "completed_at": datetime.now(UTC).isoformat(),
                     "stage": "WAITING_AFTER_POLL"}),))
'''
        assert before in probe
        probe = probe.replace(before, controlled + before)
    monkeypatch.setattr(smoke, "HTTP_PROBE", probe)
    with pytest.raises(AssertionError) as failure:
        asyncio.run(native_probe_contracts(tmp_path, probe_databases))
    data = failure.value.args[0]
    assert isinstance(data, dict), "Native probe failure discarded child diagnostics"
    projection = json.loads(data["stderr"].split("INDEPENDENT_WORKER_DID_NOT_COMPLETE ")[1])
    assert projection["job_state"] == state
    assert projection["model_usage"] == projection["outbox"] == {}
    assert projection["worker_loop"] == (
        {"state": "UNOBSERVED"} if state == "QUEUED" else
        {"state": "STALE", "stage": "WAITING_AFTER_POLL", "sequence": 2}
    )
    worker = next(child for child in data["children"] if child["role"] == "worker")
    assert worker["returncode"] == 7 and worker["pid"] > 0
    assert "SYNTHETIC_WORKER_BOOT_FAILURE" in worker["output_tail"]
    assert "[REDACTED]" in worker["output_tail"]
    assert captured["token"] not in str(data)
    assert captured["backup_key"] not in str(data)
    capture, = data["worker_captures"]
    assert capture["status"] == "already_exited"
    assert capture["natural_returncode"] == capture["returncode_after_capture"] == 7
    assert "signal_sent" not in capture
    assert data["stages"][-1]["stage"] == "failure_snapshot"
    assert data["stages"][-2]["stage"] == "command_started"
    assert data["stages"][-2]["probe"] == 1
    assert [row["at"] for row in data["stages"]] == sorted(
        row["at"] for row in data["stages"]
    )
