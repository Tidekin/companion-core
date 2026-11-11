"""Authority reads retain original custody independently of monetary reconciliation."""

from pathlib import Path

import pytest

from companion import owner_authority, paid_budget
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from tests.support.owner_input import captured


@pytest.mark.parametrize("writer", [False, True])
def test_unchanged_journal_material_is_folded_once_but_db_projection_is_always_checked(
    core, tmp_path, monkeypatch, writer,
):
    captured(core, tmp_path)
    paid_budget._clear_codec()
    reads = []
    records = paid_budget._journal_records

    def observed(raw):
        reads.append(len(raw))
        return records(raw)

    monkeypatch.setattr(paid_budget, "_journal_records", observed)
    scope = core.store.transaction if writer else core.store.read_snapshot
    with scope():
        first = owner_authority.current(core.store, core.settings, core.clock)
        second = owner_authority.current(core.store, core.settings, core.clock)
        assert first == second and len(reads) == 1
    with core.store.transaction():
        core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}'")
        with pytest.raises((CoreError, ValueError)):
            owner_authority.current(core.store, core.settings, core.clock)


def test_same_length_changed_journal_never_reuses_verified_material(core, tmp_path):
    captured(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    original = path.read_bytes()
    owner_authority.current(core.store, core.settings, core.clock)
    changed = original.replace(b'"previous":"0', b'"previous":"9', 1)
    assert changed != original and len(changed) == len(original)
    try:
        path.write_bytes(changed)
        with pytest.raises(CoreError):
            owner_authority.current(core.store, core.settings, core.clock)
    finally:
        path.write_bytes(original)
    assert owner_authority.current(core.store, core.settings, core.clock)


def test_erasure_custody_parses_its_original_bytes_without_a_third_file_read(
    core, tmp_path, monkeypatch,
):
    captured(core, tmp_path)
    path = Path(core.settings.erasure_ledger)
    opens = []
    original = Path.open

    def observed(file, *args, **kwargs):
        if file == path:
            opens.append(1)
        return original(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed)
    owner_authority.current(core.store, core.settings, core.clock)
    assert len(opens) == 2  # Original bytes plus the final independent-file fence.


def test_authority_read_does_not_reconcile_financial_tables_but_append_still_does(
    core, tmp_path, monkeypatch,
):
    captured(core, tmp_path)
    expected = owner_authority.current(core.store, core.settings, core.clock)

    def financial_reconciliation(*args, **kwargs):
        raise CoreError("SYNTHETIC_FINANCIAL_PROJECTION_MISMATCH", 503)

    monkeypatch.setattr(paid_budget, "_journal", financial_reconciliation)
    paid_budget.forget_views(core.store)
    assert owner_authority.current(core.store, core.settings, core.clock) == expected
    with pytest.raises(CoreError, match="SYNTHETIC_FINANCIAL_PROJECTION_MISMATCH"):
        with core.store.transaction():
            paid_budget.journal(core.store, core.settings.paid_model,
                                {"kind": "synthetic_append_must_validate"})


@pytest.mark.parametrize("change", ["journal", "policy", "pointer", "identity", "missing"])
def test_authority_check_rejects_original_custody_corruption(core, tmp_path, change):
    captured(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    if change == "journal":
        path.write_bytes(path.read_bytes().replace(b'"previous":"0', b'"previous":"9', 1))
    elif change == "missing":
        path.rename(path.with_suffix('.retained-original'))
    elif change == "policy":
        core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}'")
    elif change == "pointer":
        core.store.execute("UPDATE runtime_meta SET value=%s WHERE key=%s",
                           ('f' * 64, owner_authority.CURRENT))
    else:
        core.store.execute("UPDATE runtime_meta SET value='synthetic-wrong-identity' "
                           "WHERE key='model_budget_journal_id'")
    with pytest.raises((CoreError, ValueError)):
        owner_authority.current(core.store, core.settings, core.clock)


def test_historical_authority_proof_is_independent_of_finance_and_does_not_revive_stop(
    core, tmp_path, monkeypatch,
):
    captured(core, tmp_path)
    original = owner_authority.current(core.store, core.settings, core.clock)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-before-historical-proof", authority=original.authority.as_dict()))

    def financial_reconciliation(*args, **kwargs):
        raise AssertionError("historical authority proof invoked financial reconciliation")

    monkeypatch.setattr(paid_budget, "_journal", financial_reconciliation)
    paid_budget.forget_views(core.store)
    assert owner_authority.evidence(core.store, core.settings,
                                    original.authority.as_dict()) == original
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        owner_authority.current(core.store, core.settings, core.clock)
