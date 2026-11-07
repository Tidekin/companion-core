"""Shared source changes invalidate the original Core code-bound approval."""

from pathlib import Path
from shutil import copytree

import pytest

import companion_meme_sources
from companion import live_text
from companion.contracts import CoreError
from tests.support.live_text_handoff import initial, journal_bytes


@pytest.mark.parametrize("changed", [
    "__init__.py", "fetch.py", "community.py", "review.py", "nested/new_module.py",
])
def test_shared_source_mutation_invalidates_original_approval(
    core, tmp_path, monkeypatch, changed,
):
    # Redirect only the module location. All writes stay in this test's owned
    # workspace temp directory; no installed or frozen checkout file is changed.
    copied = tmp_path / "companion_meme_sources"
    copytree(Path(companion_meme_sources.__file__).parent, copied)
    monkeypatch.setattr(companion_meme_sources, "__file__", str(copied / "__init__.py"))
    approval = initial(core, tmp_path)
    baseline = live_text.code_sha256()
    assert approval.core_code_sha256 == baseline
    assert live_text.guard(core.store, core.settings, core.clock)[0] == approval
    before, journal = list(core.store.db.iterdump()), journal_bytes(core)

    target = copied / changed
    original = target.read_bytes() if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((original or b"") + b"\n# synthetic source revision\n")
    assert live_text.code_sha256() != baseline
    with pytest.raises(CoreError, match="LIVE_TEXT_RUNTIME_CHANGED"):
        live_text.guard(core.store, core.settings, core.clock)
    assert list(core.store.db.iterdump()) == before and journal_bytes(core) == journal
    assert not core.store.rows("SELECT * FROM model_admissions")
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")

    if original is None:
        target.unlink()
    else:
        target.write_bytes(original)
    assert live_text.code_sha256() == baseline
    assert live_text.guard(core.store, core.settings, core.clock)[0] == approval
