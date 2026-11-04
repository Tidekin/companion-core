"""STOP evidence is bound to the original revision and the current journal head."""

import pytest
from fastapi.testclient import TestClient

from companion import owner_authority, owner_stop_receipt
from companion.api import create_app
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from tests.support.owner_authority import activate
from tests.test_owner_authority import prepare


@pytest.mark.parametrize("change", ["active", "revised", "restored", "reactivated"])
def test_original_stop_receipt_cannot_be_invented_or_revived(core, tmp_path, change):
    policy = prepare(core, tmp_path)
    first, _ = activate(core, policy)
    ref = first["authority"]
    if change in {"restored", "reactivated"}:
        owner_authority.stop(core.store, core.settings, core.clock,
                             AuthorityStop(operation_id="stop-original", authority=ref))
        assert owner_stop_receipt.read(core.store, core.settings, ref)["state"] == "STOPPED"
    if change == "revised":
        activate(core, policy, revision=1, operation="revise-original")
    elif change == "restored":
        with core.store.transaction():
            owner_authority.restore(core.store)
    elif change == "reactivated":
        activate(core, policy, revision=2, operation="reactivate-original")
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_STOP_EVIDENCE_REQUIRED"):
        owner_stop_receipt.read(core.store, core.settings, ref)


def test_stop_receipt_is_authenticated_read_only_original_evidence(core, tmp_path):
    first, _ = activate(core, prepare(core, tmp_path))
    ref = first["authority"]
    stopped = owner_authority.stop(core.store, core.settings, core.clock,
                                  AuthorityStop(operation_id="stop-original", authority=ref))
    app = create_app(core.settings)
    app.state.core = core
    route = "/internal/v1/owner-authority/stop-receipt"
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    with TestClient(app) as client:
        before = core.store.rows("SELECT * FROM owner_authority_revisions")
        assert client.post(route, json={"authority": ref}).status_code == 401
        response = client.post(route, json={"authority": ref}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["core_stop_sha256"] == stopped["record_sha256"]
        assert response.json()["authority"] == ref
        assert response.json()["quiescence_proven"] is False
        assert core.store.rows("SELECT * FROM owner_authority_revisions") == before
