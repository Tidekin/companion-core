"""The Windows launcher preserves the real offline worker's report and lock result."""

import json
import os
import shutil
import site
import subprocess
import venv

import pytest

from companion_voice_node.state import NodeState
from tests.support.voice_node_retention_v2 import scope
from tests.support.voice_node_worker import node as node  # noqa: F401 -- shared fixture
from tests.support.voice_node_worker import write_config


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime launcher")
def test_powershell_cleanup_launcher_runs_native_worker_and_preserves_pending(node, tmp_path):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell 7 is unavailable")
    config, _, lease = scope(node)
    runtime = tmp_path / "private runtime"
    runtime.mkdir()
    # A real isolated interpreter; reuse installed dependencies without downloads.
    venv.EnvBuilder(with_pip=False).create(runtime / "venv")
    packages = runtime / "venv/Lib/site-packages"
    (packages / "test-dependencies.pth").write_text(
        "\n".join(site.getsitepackages()) + "\n", encoding="utf-8"
    )
    path = runtime / "node.json"
    write_config(path, config)
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        state.phase(lease, "UNKNOWN")

    command = [shell, "-NoProfile", "-NonInteractive", "-File",
               str(config.workspace / "scripts/cleanup-voice-node.ps1"),
               "-RuntimeRoot", str(runtime), "-NodeConfig", str(path)]

    def run():
        return subprocess.run(command, cwd=config.workspace, capture_output=True,
                              text=True, encoding="utf-8", timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    completed = run()
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["pending_ack"] == 1 and report["core_cleanup_confirmed"] is False
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "UNKNOWN"
        locked = run()
        assert locked.returncode == 1
        assert "VOICE_NODE_ALREADY_RUNNING" in locked.stderr
