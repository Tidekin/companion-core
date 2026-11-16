"""A carried authority can gain and lose the keepsake grant through the native STOP path."""

from pathlib import Path

import pytest

from companion import owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop, KeepsakePolicy, OwnerGrantPolicy
from tests import owner_model_fixtures
from tests.model_carry_support import migrated
from tests.support.model_carry_lifecycle import stop_current
from tests.support.owner_authority import activate


def carried_memory(core, tmp_path, monkeypatch):
    def policy(**values):
        return OwnerGrantPolicy(**{**values, "purposes": ["chat", "memory"],
                                   "memory_scope": "owner_private"})

    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, "OwnerGrantPolicy", policy)
        return migrated(core, tmp_path)


def test_keepsake_grant_activates_and_revokes_under_a_carry(core, tmp_path, monkeypatch):
    case = carried_memory(core, tmp_path, monkeypatch)
    before = {table: core.store.rows("SELECT * FROM " + table)
              for table in ("model_usage", "model_admissions")}
    journal = Path(core.settings.paid_model.journal_path).read_bytes()
    stop_current(core, case)
    granted = case.policy.model_copy(update={"keepsake": KeepsakePolicy()})
    active, _ = activate(core, granted, revision=4, operation="grant-keepsake")
    assert active["authority"]["revision"] == 5
    assert owner_authority.current(core.store, core.settings, core.clock,
                                   purpose="memory").policy.keepsake == KeepsakePolicy()
    for table, rows in before.items():
        assert core.store.rows("SELECT * FROM " + table) == rows
    assert Path(core.settings.paid_model.journal_path).read_bytes().startswith(journal)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-before-keepsake-revoke", authority=active["authority"]))
    revoked, _ = activate(core, case.policy, revision=6, operation="revoke-keepsake")
    assert owner_authority.current(core.store, core.settings, core.clock,
                                   purpose="memory").policy.keepsake is None
    assert revoked["authority"]["revision"] == 7


def test_keepsake_grant_cannot_smuggle_another_change(core, tmp_path, monkeypatch):
    case = carried_memory(core, tmp_path, monkeypatch)
    stop_current(core, case)
    widened = case.policy.model_copy(update={"keepsake": KeepsakePolicy(),
                                             "source_retention_days": 30})
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_APPROVAL_CHANGED"):
        activate(core, widened, revision=4, operation="reject-keepsake-plus")
