"""Python 3.11 path surface and native/synthetic filesystem boundaries."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import meme_asset_migration as migration
from companion_onebot_bridge import meme_export
from tests.support.meme_asset_migration import (
    ROOT,
    logical_database,
    original_work,
)
from tests.support.meme_asset_migration import (
    bundle as bundle,
)

VALIDATORS = (migration.regular_file, meme_export._file)


def without_junction_cli(module, *args):
    script = """
import pathlib, runpy, sys
if hasattr(pathlib.Path, 'is_junction'):
    delattr(pathlib.Path, 'is_junction')
assert not hasattr(pathlib.Path, 'is_junction')
module = sys.argv[1]
sys.argv = [module, *sys.argv[2:]]
runpy.run_module(module, run_name='__main__')
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPANION_")}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-c", script, module, *map(str, args)],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)


def test_real_offline_clis_work_without_python312_path_api(bundle):
    source = Path(bundle.bridge.data_dir) / "spool.sqlite3"
    source_before = logical_database(source)
    work_before = original_work(bundle.core.store)
    result = without_junction_cli("companion_onebot_bridge.meme_export", "--config",
                                   bundle.bridge_config, "--output", bundle.manifest)
    assert result["assets"] == 1
    target_before = logical_database(bundle.core.settings.database)
    def core(operation):
        return without_junction_cli("companion.meme_asset_migration", "--config",
                                    bundle.core_config, operation, bundle.manifest)
    assert core("dry-run")["items"][0]["state"] == "STAGED"
    assert logical_database(bundle.core.settings.database) == target_before
    assert logical_database(source) == source_before
    assert core("import")["items"][0]["state"] == "STAGED"
    assert core("import")["matches"]
    assert core("reconcile")["matches"]
    assert original_work(bundle.core.store) == work_before
    assert logical_database(source) == source_before


@pytest.mark.parametrize("validate", VALIDATORS)
def test_normal_file_and_parents_without_junction_method(tmp_path, monkeypatch, validate):
    monkeypatch.delattr(Path, "is_junction", raising=False)
    path = tmp_path / "original.json"
    path.write_bytes(b"bounded input")
    assert validate(path) == path.absolute()


@pytest.mark.parametrize("module,validate", [
    (migration, migration.regular_file), (meme_export, meme_export._file),
])
@pytest.mark.parametrize("kind", ["symlink", "reparse_file", "reparse_directory", "unknown"])
def test_raw_lstat_rejects_links_all_reparse_types_and_unknown_windows_attributes(
    tmp_path, monkeypatch, module, validate, kind,
):
    monkeypatch.delattr(Path, "is_junction", raising=False)
    path = tmp_path / "untrusted.json"
    path.write_bytes(b"must not be trusted")
    mode = {"symlink": stat.S_IFLNK, "reparse_directory": stat.S_IFDIR}.get(kind, stat.S_IFREG)
    info = SimpleNamespace(st_mode=mode)
    if kind != "unknown":
        info.st_file_attributes = 0 if kind == "symlink" else 0x400
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda p: info if p == path else original(p))
    # Change only the consumer's OS binding, not pathlib's host implementation.
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ValueError, match="MEME_(MIGRATION_FILE|EXPORT_PATH)_INVALID"):
        validate(path)


@pytest.mark.parametrize("validate", VALIDATORS)
@pytest.mark.parametrize("location", ["file", "parent", "dangling"])
def test_native_symbolic_links_remain_refused(tmp_path, monkeypatch, validate, location):
    monkeypatch.delattr(Path, "is_junction", raising=False)
    directory = tmp_path / "original"
    directory.mkdir()
    original = directory / "data.json"
    original.write_bytes(b"original bytes")
    link = tmp_path / "link"
    try:
        link.symlink_to(directory if location == "parent" else
                        tmp_path / "absent" if location == "dangling" else original,
                        target_is_directory=location == "parent")
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Native Windows symlink needs privilege; raw lstat cases still run")
        raise
    with pytest.raises(ValueError, match="MEME_(MIGRATION_FILE|EXPORT_PATH)_INVALID"):
        validate(link / "data.json" if location == "parent" else link)
    assert original.read_bytes() == b"original bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory junction")
@pytest.mark.parametrize("validate", VALIDATORS)
def test_native_junction_ancestor_is_refused_without_junction_api(tmp_path, monkeypatch, validate):
    monkeypatch.delattr(Path, "is_junction", raising=False)
    original = tmp_path / "original"
    original.mkdir()
    (original / "data.json").write_bytes(b"original bytes")
    link = tmp_path / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(original)],
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    try:
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        with pytest.raises(ValueError, match="MEME_(MIGRATION_FILE|EXPORT_PATH)_INVALID"):
            validate(link / "data.json")
    finally:
        link.rmdir()  # Remove only this test-owned junction, never its target tree.
    assert (original / "data.json").read_bytes() == b"original bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows native directory junction")
def test_export_output_junction_parent_is_refused_before_writing(bundle, tmp_path, monkeypatch):
    monkeypatch.delattr(Path, "is_junction", raising=False)
    original = tmp_path / "destination"
    original.mkdir()
    link = tmp_path / "output-junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(original)],
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    source = Path(bundle.bridge.data_dir) / "spool.sqlite3"
    before = logical_database(source)
    try:
        with pytest.raises(ValueError, match="MEME_EXPORT_PATH_INVALID"):
            meme_export.export(bundle.bridge, link / "catalog.json")
    finally:
        link.rmdir()
    assert list(original.iterdir()) == []
    assert logical_database(source) == before


@pytest.mark.parametrize("validate", VALIDATORS)
def test_inaccessible_ancestor_fails_closed(tmp_path, monkeypatch, validate):
    monkeypatch.delattr(Path, "is_junction", raising=False)
    path = tmp_path / "data.json"
    path.write_bytes(b"original")
    original = Path.lstat
    def inaccessible(current):
        if current == tmp_path:
            raise PermissionError("synthetic inaccessible ancestor")
        return original(current)
    monkeypatch.setattr(Path, "lstat", inaccessible)
    with pytest.raises(PermissionError, match="inaccessible ancestor"):
        validate(path)
