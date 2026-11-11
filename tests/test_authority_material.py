"""Reuse parsing, never a permission, across unrelated database commits."""

from pathlib import Path

import psycopg
import pytest

from companion import owner_authority, paid_budget
from companion.contracts import CoreError
from tests.support.owner_input import captured


def test_repeated_custody_checks_do_not_rebuild_the_journal_after_an_unrelated_commit(
    core, tmp_path, monkeypatch,
):
    captured(core, tmp_path)
    calls = []
    original = paid_budget._journal_records

    def decode(raw):
        calls.append(len(raw))
        return original(raw)

    monkeypatch.setattr(paid_budget, "_journal_records", decode)
    with psycopg.connect(core.settings.database.get_secret_value(), autocommit=True) as other:
        expected = owner_authority.current(core.store, core.settings, core.clock)
        other.execute("INSERT INTO runtime_meta (key,value) "
                      "VALUES ('synthetic_voice_heartbeat','1')")
        assert owner_authority.current(core.store, core.settings, core.clock) == expected
        assert owner_authority.current(core.store, core.settings, core.clock) == expected
    assert len(calls) <= 1

    # Original DB evidence is still checked on every call, regardless of exact bytes.
    core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}'")
    with pytest.raises((CoreError, ValueError)):
        owner_authority.current(core.store, core.settings, core.clock)


def test_warm_material_never_substitutes_for_current_journal_bytes(core, tmp_path):
    captured(core, tmp_path)
    owner_authority.current(core.store, core.settings, core.clock)
    path = Path(core.settings.paid_model.journal_path)
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"previous":"0', b'"previous":"9', 1))
    with pytest.raises(CoreError):
        owner_authority.current(core.store, core.settings, core.clock)


def test_authority_material_is_not_a_financial_reconciliation(core, tmp_path, monkeypatch):
    captured(core, tmp_path)
    expected = owner_authority.current(core.store, core.settings, core.clock)

    def forbidden(*args, **kwargs):
        pytest.fail("authority proof attempted financial reconciliation")

    monkeypatch.setattr(paid_budget, "_journal", forbidden)
    assert owner_authority.current(core.store, core.settings, core.clock) == expected
