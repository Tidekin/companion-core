import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.api import create_app
from companion.contracts import Candidate
from companion.retrieval import SemanticIndex
from tests.conftest import ingest

ORIGIN = "https://console.example.test"


PASSWORD = "owner-console-test-password-" * 2


@pytest.fixture
def client(core, monkeypatch):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)
    core.settings.memory_enabled = True
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    app = create_app(core.settings)
    with TestClient(app, base_url=ORIGIN) as http:
        app.state.core.clock = core.clock
        http.app_core = app.state.core
        yield http


def login(client):
    response = client.post("/v1/console/session", json={"password": PASSWORD},
                           headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"],
            "Idempotency-Key": "console-test-operation"}


def stored_memory(core, number=1, *, source=None, content="主人喜欢龙井茶", review=False,
                  confirmed=True):
    if source is None:
        source = ingest(core, number, "这是主人确认的项目目录")
    with core.store.transaction():
        memory_id = core.memory.add(Candidate(
            subject="project", kind="fact", fact_key=f"console_{number}", content=content,
            evidence_event_id=source["event_id"], evidence_quote="项目目录",
        ), core.clock(), confirmed=confirmed)
        if review:
            core.store.execute("UPDATE memories SET needs_review=1 WHERE memory_id=?", (memory_id,))
    return memory_id, source
