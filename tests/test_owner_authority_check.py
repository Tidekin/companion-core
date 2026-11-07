"""Bridge may check an exact grant purpose, but cannot create or widen authority."""

import pytest
from fastapi.testclient import TestClient

from companion import owner_authority
from companion.api import create_app
from companion.api_store import ApiStore
from companion.owner_authority_protocol import AuthorityStop
from tests.postgres_fixture import logical_state
from tests.support.owner_input import captured


@pytest.mark.parametrize("boundary", ["active", "stop", "restore", "purpose", "revision"])
def test_bridge_current_check_is_exact_and_does_not_mint_an_execution_lease(
    core, tmp_path, boundary,
):
    capture, _ = captured(core, tmp_path)
    authority = dict(capture.authority)
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="check-stop-authority", authority=authority))
    elif boundary == "restore":
        owner_authority.restore(core.store)
    elif boundary == "revision":
        authority["revision"] += 1
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    client = TestClient(app)
    body = {"authority": authority, "purpose": "memory" if boundary == "purpose" else "chat"}
    path = "/internal/v1/owner-authority/check"
    assert client.post(path, json=body).status_code == 401
    response = client.post(path, json=body, headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()})
    if boundary == "active":
        assert response.status_code == 200, response.text
        view = owner_authority.current(core.store, core.settings, core.clock)
        assert response.json() == {"approved": True, **body, "expires_at": None,
            "capability_policies": view.policy.capability_policies,
            "budget_journal_id": view.journal_id,
            "capture": view.policy.capture.model_dump(mode="json")}
        assert response.headers["Cache-Control"] == "no-store"
    else:
        assert response.status_code in {403, 409}, response.text
    assert not core.store.rows("SELECT * FROM jobs")
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("boundary", ["active", "purpose", "drift"])
def test_capability_metadata_keeps_independent_purpose_and_policy_fences(core, tmp_path, boundary):
    from tests.voice_owner_fixtures import native_voice

    capture, _, _ = native_voice(core, tmp_path)
    expected_hash = core.settings.voice.authority_sha256()
    if boundary == "drift":
        core.settings.voice = core.settings.voice.model_copy(update={
            "max_pending_jobs": core.settings.voice.max_pending_jobs + 1})
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    before = logical_state(core.store)
    response = TestClient(app).post("/internal/v1/owner-authority/check", json={
        "authority": capture.authority,
        "purpose": "account_actions" if boundary == "purpose" else "voice"}, headers={
            "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()})
    if boundary == "active":
        assert response.status_code == 200, response.text
        assert response.json()["capability_policies"] == {"voice": expected_hash}
    else:
        assert response.status_code in {403, 409}, response.text
    assert logical_state(core.store) == before
