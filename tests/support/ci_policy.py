import subprocess
from pathlib import Path

import pytest

from scripts.ci_policy import FULL_JOBS

ROOT = Path(__file__).resolve().parents[2]


def needs(profile):
    return {"preflight": {"result": "success"}, "quality": {"result": "success"}, **{
        name: {"result": "success" if profile == "full" else "skipped"} for name in FULL_JOBS
    }}


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    commit(tmp_path)
    return tmp_path


def commit(path):
    git(path, "add", "--all")
    git(path, "-c", "user.name=Synthetic CI", "-c", "user.email=ci@example.invalid",
        "commit", "-qm", "fixture")
    return git(path, "rev-parse", "HEAD")
