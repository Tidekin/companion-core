"""Observed Reader overruns remain charged across revocation and restore."""

from datetime import datetime

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from companion.api import create_app
from companion.backup import backup, restore
from companion.core import Core
from companion.reading_protocol import ReadObservation
from source_reader import worker_v1
from source_reader.web import WebHTTP
from tests.conftest import FakeModel, run
from tests.support.reading_service import TOKEN, configure
from tests.support.reply_revision import accept
from tests.test_heybox_reader import mock_pool


def limited(core):
    configure(core)
    core.settings.exploration_max_response_bytes = 4096
    core.settings.exploration_daily_bytes = 8192
    core.settings.control_token = SecretStr("reading-accounting-admin-" * 3)
    accept(core, text="请阅读 https://articles.example/oversized")


def test_actual_worker_overrun_blocks_next_admission_and_remains_visible(core, monkeypatch):
    limited(core)
    raw = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
           + b"x" * 9000)
    pool, backend = mock_pool(raw)
    monkeypatch.setattr(worker_v1, "WebHTTP", lambda hosts: WebHTTP(hosts, pool))
    app = create_app(core.settings)
    app.state.core = core

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            assert await worker_v1.tick(
                client, None, "web", source="web",
                clock=lambda: datetime.fromisoformat(core.clock()),
            )
            measured = core.store.one("SELECT * FROM reading_attempts")
            assert measured["observed_bytes"] > core.settings.exploration_daily_bytes
            assert not core.exploration.budget_available(for_read=True)
            status = await client.get("/internal/v2/exploration", headers={
                "Authorization": "Bearer " + core.settings.control_token.get_secret_value(),
            })
            assert status.status_code == 200
            row = status.json()["items"][0]
            assert row["state"] == "FAILED" and row["last_error_code"] == "READER_BYTE_LIMIT"
            assert row["used_bytes"] is None and row["reserved_bytes"] == 4096
            assert row["accounted_bytes"] == row["observed_bytes"] == measured["observed_bytes"]
            accept(core, 2, text="请阅读 https://articles.example/second")
            response = await client.post("/internal/v3/readers/web/lease", json={})
            assert response.status_code == 200 and response.json() == {"item": None}

    run(scenario())
    assert len(backend.requests) == 1 and not core.model.calls


@pytest.mark.parametrize("status,expired", [("UNKNOWN", False), ("CANCELLED", True)])
def test_uncertain_and_late_observed_overruns_cannot_release_daily_capacity(core, status, expired):
    limited(core)
    item = core.reading.lease("web")
    if expired:
        core.clock.advance(31)
    result = ReadObservation(
        schema_version=1, lease_token=item["lease_token"], status=status, observations=[],
        observed_requests=1, observed_bytes=9000, error_code="READ_CANCELLED",
    )
    core.reading.complete("web", item["request_id"], result)
    assert not core.exploration.budget_available(for_read=True)
    assert core.reading.complete("web", item["request_id"], result)["duplicate"]
    accept(core, 2, text="请阅读 https://articles.example/second")
    assert core.reading.lease("web") is None
    assert core.store.one("SELECT observed_bytes FROM reading_attempts")["observed_bytes"] == 9000


def test_observed_overrun_survives_source_erasure_and_old_backup_replay(core, tmp_path):
    limited(core)
    item = core.reading.lease("web")
    core.reading.complete("web", item["request_id"], ReadObservation(
        schema_version=1, lease_token=item["lease_token"], status="UNKNOWN", observations=[],
        observed_requests=1, observed_bytes=9000, error_code="UPSTREAM_TIMEOUT",
    ))
    archive, destination = tmp_path / "reading.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert not core.exploration.budget_available(for_read=True)
    restore(archive, destination, key, core.settings.erasure_ledger)
    with Core(core.settings.model_copy(update={"database": str(destination)}),
              FakeModel(), core.clock) as restored:
        attempt = restored.store.one("SELECT * FROM reading_attempts")
        assert attempt["context_json"] == "{}" and attempt["observed_bytes"] == 9000
        assert not restored.exploration.budget_available(for_read=True)
        accept(restored, 2, text="请阅读 https://articles.example/second")
        assert restored.reading.lease("web") is None
