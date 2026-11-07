"""Background calls yield to the owner where non-dispatch is provable, not in between."""

import pytest

from companion import budget, model_scope, owner_authority
from companion.contracts import CoreError
from tests.test_watch_ledger import ready


def test_the_pre_http_recheck_does_not_yield_but_the_boundary_check_still_can(
        core, tmp_path, monkeypatch):
    ready(core, tmp_path)
    request = budget.reserve(core.store, core.settings, None, "watch_review",
                             [{"role": "user", "content": "x"}], core.clock(), clock=core.clock)
    # Production 2026-10-29: an owner message arrived after I/O custody began; the
    # intermediate recheck raised BACKGROUND_YIELD_TO_CHAT and left the call UNKNOWN.
    monkeypatch.setattr(owner_authority, "owner_waiting", lambda *a, **k: True)
    assert model_scope.check(core.store, core.settings, core.clock, request, "watch_review",
                               yield_to_chat=False)
    with pytest.raises(CoreError, match="BACKGROUND_YIELD_TO_CHAT"):
        model_scope.check(core.store, core.settings, core.clock, request, "watch_review")
