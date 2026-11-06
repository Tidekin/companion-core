"""Acquisition artifacts retain their own imports and frozen code identity."""

import shutil
import tomllib
from pathlib import Path

import pytest

from scripts.verify_package import verify_meme_sources
from tests.test_meme_asset_migration import (
    test_offline_entrypoints_do_not_import_the_opposite_application as isolated_import,
)

ROOT = Path(__file__).resolve().parents[1]


def test_source_artifact_imports_without_apps_storage_or_model_runtime(tmp_path):
    for name in ("companion_meme_protocol", "companion_meme_sources"):
        shutil.copytree(ROOT / name, tmp_path / name)
    verify_meme_sources(tmp_path)


def test_wheel_and_both_images_include_acquisition_sources():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert "companion_meme_sources" in project["tool"]["hatch"]["build"]["targets"][
        "wheel"
    ]["packages"]
    for path in ("Dockerfile", "deploy/onebot-bridge.Dockerfile"):
        assert "COPY companion_meme_sources ./companion_meme_sources" in (
            ROOT / path
        ).read_text("utf-8")


@pytest.mark.parametrize("module,blocked", [
    ("companion.meme_core", "companion_onebot_bridge"),
    ("companion_onebot_bridge.meme_discovery", "companion"),
    ("companion_onebot_bridge.meme_runtime", "companion"),
])
def test_acquisition_consumers_keep_application_import_boundaries(module, blocked):
    isolated_import(module, blocked)


@pytest.mark.parametrize("name", [
    "__init__.py", "fetch.py", "community.py", "review.py", "gallery.py",
])
def test_each_source_file_invalidates_both_runtime_digests(monkeypatch, name):
    import companion_meme_sources
    from companion.live_text import code_sha256
    from companion_onebot_bridge.window import bridge_code_sha256

    target = Path(companion_meme_sources.__file__).parent / name
    original = Path.read_bytes
    before = code_sha256(), bridge_code_sha256()

    def changed(path):
        value = original(path)
        return value + b"\n# synthetic source edit\n" if path == target else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert code_sha256() != before[0]
    assert bridge_code_sha256() != before[1]
