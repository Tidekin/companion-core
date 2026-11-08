"""Exercise real xdist sharing and testmon selection without changing product behavior."""

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts import test_feedback

ROOT = Path(__file__).resolve().parents[1]


def project(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".local/\n__pycache__/\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / ".local").mkdir()
    return tmp_path


def arguments(root, *, impact=False, workers=2, full=False, report="result"):
    return Namespace(workers=workers, impact=impact, full=full, skip_model_preparation=True,
                     pytest_args=["--basetemp=" + str(root / ".local" / (report + "-tmp")),
                                  "-o", "cache_dir=" + str(root / ".local/cache"),
                                  "--junitxml=" + str(root / ".local" / (report + ".xml"))])


def test_native_testmon_with_xdist_selects_changed_function_and_full_can_refresh(tmp_path):
    root = project(tmp_path)
    service = root / "sample_service.py"
    service.write_text("def alpha():\n    return 1\ndef beta():\n    return 2\n")
    for name, number in (("alpha", 1), ("beta", 2)):
        (root / f"test_{name}.py").write_text(
            f"from sample_service import {name}\ndef test_{name}():\n"
            f"    assert {name}()=={number}\n")
    assert test_feedback.run(arguments(root, impact=True, report="initial"), root=root) == 0
    assert test_feedback.run(arguments(root, impact=True, report="unchanged"), root=root) == 0
    assert len(list(ElementTree.parse(root / ".local/unchanged.xml").iter("testcase"))) == 0
    service.write_text("def alpha():\n    return 1 + 0\ndef beta():\n    return 2\n")
    assert test_feedback.run(arguments(root, impact=True, report="changed"), root=root) == 0
    cases = list(ElementTree.parse(root / ".local/changed.xml").iter("testcase"))
    assert [case.attrib["name"] for case in cases] == ["test_alpha"]
    assert test_feedback.run(arguments(root, impact=True, full=True, report="full"), root=root) == 0
    assert len(list(ElementTree.parse(root / ".local/full.xml").iter("testcase"))) == 2


def test_real_schema_is_prepared_once_per_worker_and_copies_stay_private(tmp_path):
    root = project(tmp_path)
    (root / "conftest.py").write_text(
        "from pathlib import Path\nimport tests.conftest as shared\n"
        "pytest_plugins = ['tests.conftest']\noriginal = shared.migrate\n"
        "def counted(path):\n"
        "    with (Path(__file__).parent / '.local/preparations').open('a') as f:\n"
        "        f.write('prepared\\n')\n"
        "    return original(path)\nshared.migrate = counted\n")
    for index in range(4):
        (root / f"test_isolation_{index}.py").write_text(
            "import json,os,socket,psycopg,subprocess,sys\nfrom pathlib import Path\n"
            "def test_copy(core,tmp_path,migrated_database):\n"
            "    sql = (\"SELECT tablename FROM pg_tables WHERE \"\n"
            "           \"schemaname=current_schema() AND tablename='private'\")\n"
            "    assert not core.store.rows(sql)\n"
            "    core.store.execute('CREATE TABLE private(value TEXT)')\n"
            "    with psycopg.connect(migrated_database.get_secret_value()) as db:\n"
            "        assert not db.execute(sql).fetchall()\n"
            "    with socket.socket() as server:\n"
            "        server.bind(('127.0.0.1',0))\n        server.listen()\n"
            "        port = server.getsockname()[1]\n"
            "        code = (\"import json,os,socket,sys; \"\n"
            "                \"peer=socket.create_connection(('127.0.0.1',int(sys.argv[1]))); \"\n"
            "                \"peer.sendall(b'SYNTHETIC'); peer.close(); \"\n"
            "                \"keys=('TMP','TEMP','TMPDIR'); \"\n"
            "                \"print(json.dumps([os.environ[k] for k in keys]))\")\n"
            "        child = subprocess.run([sys.executable,'-c',code,str(port)],\n"
            "                               capture_output=True,text=True,check=True,timeout=10)\n"
            "        peer,_ = server.accept()\n"
            "        with peer:\n            assert peer.recv(64) == b'SYNTHETIC'\n"
            f"    (Path(__file__).parent / '.local/worker-{index}.json').write_text(json.dumps(\n"
            "        {'pid':os.getpid(),'schema':psycopg.conninfo.conninfo_to_dict(\n"
            "         migrated_database.get_secret_value())['dbname'],'tmp':str(tmp_path),\n"
            "         'child_tmp':os.environ['TMP'],'child_env':json.loads(child.stdout),\n"
            "         'port':port}))\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    command = [sys.executable, "-m", "pytest", "-q", "-n", "2", "--dist=loadfile",
               "--basetemp=" + str(root / ".local/pytest"), "-o", "cache_dir=.local/cache"]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / ".local/preparations").read_text().splitlines() == ["prepared"] * 2
    records = [json.loads(path.read_text()) for path in (root / ".local").glob("worker-*.json")]
    assert len(records) == 4 and len({r["pid"] for r in records}) == 2
    assert len({r["schema"] for r in records}) == 2
    assert len({r["tmp"] for r in records}) == 4
    assert len({r["child_tmp"] for r in records}) == 2
    assert all(r["child_env"] == [r["child_tmp"]] * 3 and r["port"] > 0 for r in records)
    assert all(Path(r["child_tmp"]).is_relative_to(root / ".local/pytest") for r in records)


@pytest.mark.parametrize("name", ["uv.lock", "pyproject.toml", "tests/conftest.py",
                                    "companion/core.py", "contracts/data.json",
                                    "companion_voice_node/worker.py"])
def test_unobserved_inputs_and_process_boundaries_invalidate_native_environment(tmp_path, name):
    root = project(tmp_path)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("before")
    before = test_feedback.impact_environment(root, 2)
    path.write_text("after")
    assert test_feedback.impact_environment(root, 2) != before


def test_corrupt_cache_runs_full_scope_without_deleting_evidence(tmp_path, monkeypatch):
    root = project(tmp_path)
    data = root / ".local/testmon/bound.sqlite"
    data.parent.mkdir()
    data.write_bytes(b"synthetic invalid sqlite")
    monkeypatch.setattr(test_feedback, "impact_environment", lambda *a: "bound")
    commands = []
    monkeypatch.setattr(test_feedback.subprocess, "call",
                        lambda command, **kw: commands.append(command) or 0)
    assert test_feedback.run(arguments(root, impact=True), root=root) == 0
    assert "--no-testmon" in commands[0] and "--testmon" not in commands[0]
    assert data.read_bytes() == b"synthetic invalid sqlite"


def test_new_or_deleted_python_paths_invalidate_but_native_coverage_handles_body_changes(tmp_path):
    root = project(tmp_path)
    path = root / "sample.py"
    path.write_text("before")
    subprocess.run(["git", "add", "sample.py"], cwd=root, check=True)
    before = test_feedback.impact_environment(root, 2)
    path.write_text("after")
    assert test_feedback.impact_environment(root, 2) == before
    path.unlink()
    assert test_feedback.impact_environment(root, 2) != before


def test_impact_controller_lock_does_not_launch_a_second_run(tmp_path, monkeypatch):
    from filelock import FileLock

    root = project(tmp_path)
    directory = root / ".local/testmon"
    directory.mkdir()
    monkeypatch.setattr(test_feedback, "impact_environment", lambda *a: "bound")
    monkeypatch.setattr(test_feedback.subprocess, "call", lambda *a, **k: pytest.fail("ran twice"))
    with FileLock(str(directory / "bound.sqlite.lock")):
        assert test_feedback.run(arguments(root, impact=True), root=root) == 2


def test_resources_are_prepared_in_parent_once_before_worker_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(test_feedback, "prepare_model", lambda root: calls.append("prepare"))
    monkeypatch.setattr(test_feedback.subprocess, "call",
                        lambda command, **kw: calls.append(command) or 0)
    args = arguments(tmp_path, workers=2)
    args.skip_model_preparation = False
    assert test_feedback.run(args, root=tmp_path) == 0
    assert calls[0] == "prepare" and len(calls) == 2
    assert calls[1][calls[1].index("-n") + 1] == "2"
    assert "--dist=loadfile" in calls[1]
