"""Private config migration plus real HTTP and the independent Reader CLI."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from source_reader.migrate_astrbot import READER_KEYS, export, plan
from tests.conftest import ingest, run
from tests.support.core_http import real_core_http
from tests.support.exploration import configure

ROOT = Path(__file__).resolve().parents[1]


def output_evidence(value):
    if value is None:
        return {"available": False}
    if isinstance(value, bytes):
        return {"available": True, "bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest()}
    return {"available": True, "text_characters": len(value), "raw_bytes_available": False}


def compose_config(command, *, timeout=30):
    """Preserve bounded native rendering while retaining safe timeout-stage evidence."""
    started = time.monotonic()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True) as process:
        try:
            output, error = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as failure:
            observed_returncode = process.poll()
            elapsed = time.monotonic() - started
            before = {"stdout": output_evidence(failure.stdout),
                      "stderr": output_evidence(failure.stderr)}
            process.kill()
            # Match subprocess.run's original platform-specific timeout cleanup.
            if os.name == "nt":
                failure.stdout, failure.stderr = process.communicate()
            else:
                process.wait()
            evidence = {
                "stage": "compose_config", "timeout_seconds": timeout,
                "elapsed_seconds": round(elapsed, 3), "pid": process.pid,
                "returncode_at_timeout": observed_returncode,
                "returncode_after_cleanup": process.returncode,
                "output_at_timeout": before,
                "output_after_cleanup": {"stdout": output_evidence(failure.stdout),
                                         "stderr": output_evidence(failure.stderr)},
                "docker_executable_present": shutil.which("docker") is not None,
                "ambient_environment_present": {name: name in os.environ for name in (
                    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "COMPOSE_FILE",
                    "COMPOSE_PROFILES", "COMPOSE_ENV_FILES", "COMPOSE_PROJECT_NAME")},
            }
            failure.add_note("COMPOSE_CONFIG_TIMEOUT_DIAGNOSTICS=" + json.dumps(evidence))
            raise
        except BaseException:
            process.kill()
            raise
        return subprocess.CompletedProcess(command, process.returncode, output, error)


def legacy(source="bilibili"):
    return {"core_url": "http://companion-api:8765", "bridge_token": "bridge-" * 8,
            "content_enabled": True, source + "_reader_enabled": True,
            source + "_reader_id": "bili" if source == "bilibili" else "heybox",
            source + "_reader_token": source + "-reader-token-" * 3}


def test_export_preserves_authority_and_secret_bytes_without_enabling_capabilities(tmp_path):
    original = legacy() | {"heybox_reader_enabled": True, "heybox_reader_id": "heybox",
        "heybox_reader_token": "heybox-token-" * 3, "heybox_api_url": "http://100.80.1.2:4000",
        "heybox_api_token": "source-$literal'\"-" * 3}
    target = tmp_path / "private"
    path = export(original, target, "companion-internal")
    compose = json.loads(path.read_text())
    for name, service in compose["services"].items():
        assert service["command"][service["command"].index("--protocol") + 1] == "legacy"
        assert not any(flag in service["command"] for flag in (
            "--account-file", "--browser", "--bili-tools", "--video-media", "--discussion"))
        assert service["env_file"] == [{"path": f"./{name}.env", "format": "raw"}]
        assert service["read_only"] and service["user"] == "10003:10003"
        assert original["bridge_token"] not in (target / f"{name}.env").read_text()
    assert original["heybox_api_token"] in (target / "heybox-reader.env").read_text()
    migrated = json.loads((target / "astrbot-config.json").read_text())
    assert migrated == {k: v for k, v in original.items() if k not in READER_KEYS}
    assert not any(key in migrated for key in READER_KEYS)
    with pytest.raises(FileExistsError):
        export(original, target, "companion-internal")
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0
        assert all(path.stat().st_mode & 0o077 == 0 for path in target.iterdir())


@pytest.mark.parametrize("change", ["token", "alias", "adapter", "source", "disabled"])
def test_invalid_migration_writes_nothing(tmp_path, change):
    value = legacy()
    if change == "token":
        value["bilibili_reader_token"] = "private\nINJECT=value"
    elif change == "alias":
        value["bilibili_reader_token"] = value["bridge_token"]
    elif change == "adapter":
        value["bilibili_reader_id"] = "bad/path"
    elif change == "source":
        value = legacy("heybox") | {"heybox_api_url": "http://example.org",
                                    "heybox_api_token": "source-token-" * 3}
    else:
        value["bilibili_reader_enabled"] = False
    with pytest.raises(ValueError):
        export(value, tmp_path / "output", "companion-internal")
    assert not (tmp_path / "output").exists()


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose unavailable")
def test_migration_cli_and_compose_preserve_literal_credentials(tmp_path):
    original = legacy("heybox") | {"heybox_reader_token": "literal-$value'\"=" * 3}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(original))
    target = tmp_path / "private"
    result = subprocess.run([sys.executable, "-B", "-m", "source_reader.migrate_astrbot",
        "--config", str(config_file), "--output", str(target), "--network", "private-test"],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert original["heybox_reader_token"] not in result.stdout + result.stderr
    configured = compose_config(["docker", "compose", "-f", str(target / "compose.json"),
        "config", "--format", "json"])
    assert configured.returncode == 0, configured.stderr
    service = json.loads(configured.stdout)["services"]["heybox-reader"]
    # Compose's config renderer doubles '$' for re-loading the canonical output.
    # cmd/compose/config.go runConfig performs this after resolving raw env files.
    assert service["environment"]["COMPANION_READER_TOKEN"] == (
        original["heybox_reader_token"].replace("$", "$$"))
    assert "--public-discovery" in service["command"]


def test_compose_timeout_retains_process_stage_without_printing_credentials(tmp_path, monkeypatch):
    canary = "SYNTHETIC_PRIVATE_COMPOSE_VALUE"
    program, ready = tmp_path / "controlled_compose.py", tmp_path / "ready"
    program.write_text("import sys,time; from pathlib import Path; "
        f"print({canary!r},flush=True); print({canary!r},file=sys.stderr,flush=True); "
        f"Path({str(ready)!r}).touch(); time.sleep(60)", encoding="utf-8")
    command = [sys.executable, "-B", str(program)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "Controlled subprocess did not reach the output stage"
        # Only startup is injected; communicate/timeout/poll/kill/drain use the real child.
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            compose_config(command, timeout=0.1)
        evidence = json.loads(caught.value.__notes__[0].split("=", 1)[1])
        assert evidence["stage"] == "compose_config" and evidence["pid"] == process.pid
        assert evidence["returncode_at_timeout"] is None
        assert evidence["returncode_after_cleanup"] is not None
        assert caught.value.timeout == 0.1
        assert evidence["timeout_seconds"] == 0.1
        assert evidence["elapsed_seconds"] >= 0
        for value in evidence["output_after_cleanup"].values():
            assert value["available"] is True
            if "bytes" in value:
                assert value["bytes"] == len(canary) + 1
                assert value["sha256"] == hashlib.sha256((canary + "\n").encode()).hexdigest()
            else:
                assert value["text_characters"] == len(canary) + 1
                assert value["raw_bytes_available"] is False
        assert canary not in json.dumps(evidence)
        assert canary not in "".join(caught.value.__notes__)
        assert all(type(value) is bool for value in
                   evidence["ambient_environment_present"].values())
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_compose_diagnostic_preserves_completed_process_output_and_status(
    tmp_path, capsys, exit_code,
):
    canary = "SYNTHETIC_PRIVATE_COMPOSE_VALUE"
    program = tmp_path / "completed_compose.py"
    program.write_text("import sys; " + f"print({canary!r}); "
                       + f"print({canary!r},file=sys.stderr); raise SystemExit({exit_code})",
                       encoding="utf-8")
    result = compose_config([sys.executable, "-B", str(program)])
    assert result.returncode == exit_code
    assert result.stdout == result.stderr == canary + "\n"
    assert capsys.readouterr() == ("", "")


def test_compose_diagnostic_distinguishes_missing_output_from_received_empty_bytes():
    assert output_evidence(None) == {"available": False}
    assert output_evidence(b"") == {"available": True, "bytes": 0,
                                     "sha256": hashlib.sha256(b"").hexdigest()}


@pytest.mark.parametrize("source", ["bilibili", "heybox"])
@pytest.mark.parametrize("outcome", ["success", "erased", "report_lost", "discover"])
def test_exported_reader_cli_real_http_and_restart(core, tmp_path, source, outcome):
    from tests.support.exploration import URL as BILI_URL
    from tests.test_heybox_reader import URL as HEYBOX_URL

    configure(core)
    core.settings.content_enabled = True
    core.settings.observation_adapters["bili"].reader_max_requests = 1
    url = BILI_URL if source == "bilibili" else HEYBOX_URL
    accepted = None
    if outcome == "discover":
        with core.store.transaction():
            core.exploration.create("bili" if source == "bilibili" else "heybox",
                                    "discover", "migration-discover")
    else:
        accepted = ingest(core, text=url)
    if outcome == "erased":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [accepted["event_id"]], core.clock())

    async def scenario():
        async with real_core_http(core) as port:
            config = legacy(source) | {"core_url": f"http://127.0.0.1:{port}"}
            adapter = config[source + "_reader_id"]
            config[source + "_reader_token"] = core.settings.observation_adapters[
                adapter].token.get_secret_value()
            compose, env_files, _ = plan(config, "private-test")
            command = compose["services"][source + "-reader"]["command"] + ["--once"]
            env = dict(os.environ)
            env.update(line.split("=", 1) for line in
                       env_files[source + "-reader.env"].splitlines())
            for attempt in range(2):
                report = tmp_path / f"attempt-{attempt}.json"
                packet = {"source": source, "clock": core.clock(), "report": str(report),
                          "lose_report": outcome == "report_lost", "args": command,
                          "discover": outcome == "discover", "url": url}
                child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                    "tests.test_reader_extraction", "--reader", cwd=ROOT, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(
                        child.communicate(json.dumps(packet).encode()), 30)
                    assert child.returncode == 0, stderr.decode(errors="replace")
                    assert config[source + "_reader_token"].encode() not in stdout + stderr
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
                count = json.loads(report.read_text())["requests"]
                assert count == (1 if attempt == 0 and outcome != "erased" else 0)
    run(scenario())
    row = core.store.one("SELECT state FROM exploration_requests")
    assert row["state"] == {"success": "DONE", "discover": "DONE", "erased": "CANCELLED",
                             "report_lost": "RUNNING"}[outcome]
    if outcome == "success":
        assert core.content.context(accepted["event_id"])[0]["trust"] == "external_untrusted"
    assert not core.store.rows("SELECT * FROM model_usage")


if __name__ == "__main__" and sys.argv[1:] == ["--reader"]:
    import runpy

    import source_reader.bilibili as bili
    import source_reader.heybox as heybox
    import source_reader.network as network
    from tests.support.source_reader import META, response
    from tests.test_heybox_reader import mock_pool

    packet = json.loads(sys.stdin.buffer.read())
    raw = response(META) if packet["source"] == "bilibili" else response(
        b"<title>post</title><article><p>synthetic article evidence</p></article>")
    if packet["discover"]:
        raw = response({"code": 0, "data": {"list": [{
            "bvid": packet["url"].rsplit("/", 1)[1], "title": "public title"}]}}) if (
            packet["source"] == "bilibili") else response(
                ('<a href="' + packet["url"] + '">public title</a>').encode())
    pool, backend = mock_pool(raw)
    public_type = network.PublicHTTP
    bili_type, heybox_type = bili.BilibiliReader, heybox.HeyboxReader
    network.PublicHTTP = lambda: public_type(pool)
    bili.BilibiliReader = lambda *a, **kw: bili_type(*a, **kw, clock=lambda: packet["clock"])
    heybox.HeyboxReader = lambda *a, **kw: heybox_type(*a, **kw, clock=lambda: packet["clock"])
    original_post = httpx.AsyncClient.post

    async def post(client, path, **kwargs):
        if packet["lose_report"] and path.endswith("/result"):
            raise httpx.ReadTimeout("synthetic result report loss")
        return await original_post(client, path, **kwargs)

    httpx.AsyncClient.post = post
    sys.argv = ["source_reader", *packet["args"]]
    runpy.run_module("source_reader", run_name="__main__")
    assert all(row.startswith(b"GET ") and b"authorization:" not in row.lower()
               for row in backend.requests)
    Path(packet["report"]).write_text(json.dumps({"requests": len(backend.requests)}))
