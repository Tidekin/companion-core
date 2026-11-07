"""Native owner authentication and fresh, bounded projection reads."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import api
from companion.core import Core
from tests.support.confirmed_memory import remember
from tests.support.next_memory_context import episode

PATH = "/internal/v2/memory/projection"


@pytest.fixture
def projection_client(core, monkeypatch):
    core.settings.control_token = SecretStr("synthetic-projection-owner-" * 2)
    monkeypatch.setattr(api, "Core", lambda settings, model=None:
                        Core(settings, core.model, core.clock))
    app = api.create_app(core.settings)
    assert any(getattr(route, "path", None) == PATH for route in app.routes)
    with TestClient(app) as client:
        yield client, {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}


@pytest.mark.parametrize("credential", ["missing", "bridge", "wrong"])
def test_projection_requires_control_owner_not_transport_token(core, projection_client, credential):
    client, _ = projection_client
    token = core.settings.bridge_token.get_secret_value() if credential == "bridge" else "wrong"
    response = client.post(PATH, json={}, headers={} if credential == "missing" else {
        "Authorization": "Bearer " + token})
    assert response.status_code == 401


@pytest.mark.parametrize("body", [
    {"memory_ids": ["x"] * 101}, {"episode_ids": [""]}, {"memory_ids": "all"},
    {"used_memory_ids": ["invented-use"]}, {"actor": "owner"},
])
def test_projection_rejects_unbounded_or_invented_usage_scope(projection_client, body):
    client, headers = projection_client
    response = client.post(PATH, json=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_CONTRACT"
    assert set(response.json()) == {"error", "fields"}


def test_owner_reads_metadata_and_forgetting_retracts_view(core, projection_client):
    content = "合成项目的封面采用松绿色"
    mid = remember(core, 1, content, confirmed=True)
    eid = episode(core, 1)
    client, headers = projection_client
    selection = {"memory_ids": [mid], "episode_ids": [eid]}
    response = client.post(PATH, json=selection, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["memories"][0]["temperature"] == "Warm"
    assert response.json()["episodes"][0]["state"] == "discussed"
    assert content not in response.text
    forgotten = client.post(f"/internal/v1/memories/{mid}/decisions", json={
        "actor_id": "owner", "decision": "forget"}, headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()})
    assert forgotten.status_code == 200, forgotten.text
    response = client.post(PATH, json=selection, headers=headers)
    assert response.status_code == 200
    assert response.json()["memories"] == []
    assert response.json()["episodes"] == []
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.model.calls == []
