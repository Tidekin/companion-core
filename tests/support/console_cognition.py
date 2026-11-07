import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.api import create_app
from companion.retrieval import SemanticIndex

ORIGIN = "https://console.example.test"


PASSWORD = "owner-console-cognition-password-" * 2


@pytest.fixture
def client(core, monkeypatch):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    app = create_app(core.settings)
    with TestClient(app, base_url=ORIGIN) as http:
        app.state.core.clock = core.clock
        app.state.core.mood.clock = core.clock
        app.state.core.goals.clock = core.clock
        app.state.core.executive.clock = core.clock
        yield http


def login(client):
    csrf = client.post("/v1/console/session", json={"password": PASSWORD},
                       headers={"Origin": ORIGIN}).json()["csrf_token"]
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}
