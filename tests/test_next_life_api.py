"""Authenticated API boundaries for versioned reading and private life projections."""

import copy
import json
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from companion.api import create_app
from companion.api_store import ApiStore
from companion.store import after
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.diary import DAY, generate, seed_day
from tests.support.exploration import URL, configure
from tests.support.reading_protocol import envelope


def credentials(core):
    configure(core)
    core.settings.control_token = SecretStr("owner-control-api-token-" * 3)
    core.settings.diary_admin_token = SecretStr("diary-admin-api-token-" * 3)
    return {
        "missing": {},
        "bridge": {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        "control": {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
        "diary": {"Authorization": "Bearer " + core.settings.diary_admin_token.get_secret_value()},
        **{name: {"Authorization": "Bearer " + adapter.token.get_secret_value()}
           for name, adapter in core.settings.observation_adapters.items()},
    }


def client_for(core):
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    return httpx.AsyncClient(base_url="http://core", transport=httpx.ASGITransport(app=app))


def failed_read(token="lease_synthetic"):
    return {
        "schema_version": 1, "lease_token": token, "status": "FAILED", "observations": [],
        "used_requests": 0, "used_bytes": 0, "observed_requests": 0, "observed_bytes": 0,
        "error_code": "SYNTHETIC_READ_FAILURE",
    }


@pytest.mark.parametrize("authority", ["missing", "bridge", "control", "diary", "heybox"])
@pytest.mark.parametrize("suffix,body", [
    ("lease", {}),
    ("request_synthetic/validate", {"lease_token": "lease_synthetic"}),
    ("request_synthetic/result", failed_read()),
])
def test_v3_reader_requires_its_own_adapter_token_before_writing(core, authority, suffix, body):
    tokens = credentials(core)

    async def scenario():
        async with client_for(core) as client:
            result = await client.post("/internal/v3/readers/bili/" + suffix, json=body,
                                       headers=tokens[authority])
            assert result.status_code == 401

    before = logical_state(core.store)
    with core.store.read_snapshot(), patch.object(
        ApiStore, "run", side_effect=AssertionError("invalid reader request opened an API session")
    ):
        run(scenario())
    assert logical_state(core.store) == before
    assert core.store.rows("SELECT * FROM reading_attempts") == []
    assert core.store.rows("SELECT * FROM exploration_requests") == []


@pytest.mark.parametrize("version,body", [
    (2, failed_read()),
    (3, {"lease_token": "lease_synthetic", "status": "FAILED", "items": [],
         "used_bytes": 0, "error_code": "SYNTHETIC_READ_FAILURE"}),
    (3, failed_read() | {"schema_version": True}),
    (3, failed_read() | {"authorization": "private-token-must-not-echo"}),
])
def test_reader_versions_reject_other_contracts_without_echo_or_writes(core, version, body):
    tokens = credentials(core)

    async def scenario():
        async with client_for(core) as client:
            result = await client.post(
                f"/internal/v{version}/readers/bili/request_synthetic/result", json=body,
                headers=tokens["bili"],
            )
            assert result.status_code == 400 and result.json()["error"] == "INVALID_CONTRACT"
            assert "private-token-must-not-echo" not in result.text

    before = logical_state(core.store)
    with core.store.read_snapshot(), patch.object(
        ApiStore, "run", side_effect=AssertionError("invalid reader request opened an API session")
    ):
        run(scenario())
    assert logical_state(core.store) == before


def test_v3_reader_http_commits_evidence_once_and_rejects_changed_receipt(core):
    tokens = credentials(core)
    core.settings.content_enabled = True
    ingest(core, text=URL)

    async def scenario():
        async with client_for(core) as client:
            leased = await client.post("/internal/v3/readers/bili/lease", headers=tokens["bili"])
            assert leased.status_code == 200
            item = leased.json()["item"]
            assert item["schema_version"] == 1
            assert item["context"]["source_event_ids"] == ["evt_test_1"]
            assert item["context"]["root_task_id"] == core.store.one(
                "SELECT job_id FROM jobs WHERE event_id='evt_test_1' AND kind='chat'",
            )["job_id"]
            path = "/internal/v3/readers/bili/" + item["request_id"]
            validated = await client.post(path + "/validate", headers=tokens["bili"],
                                          json={"lease_token": item["lease_token"]})
            assert validated.status_code == 200 and validated.json() == {"valid": True}
            body = envelope("bilibili", level="L1")
            body["lease_token"] = item["lease_token"]
            evidence = body["observations"][0]
            evidence["provenance"] = item["context"]
            evidence["fetched_at"] = evidence["evidence"]["fetched_at"] = core.clock()
            completed = await client.post(path + "/result", headers=tokens["bili"], json=body)
            assert completed.status_code == 200
            receipt = completed.json()
            assert receipt["state"] == "DONE" and receipt["duplicate"] is False
            assert len(receipt["event_ids"]) == 1
            replay = await client.post(path + "/result", headers=tokens["bili"], json=body)
            assert replay.status_code == 200 and replay.json() == receipt | {"duplicate": True}
            changed = copy.deepcopy(body)
            changed["observations"][0]["title"] = "changed receipt"
            conflict = await client.post(path + "/result", headers=tokens["bili"], json=changed)
            assert conflict.status_code == 409
            assert conflict.json()["error"] == "IDEMPOTENCY_CONFLICT"
            assert core.store.one("SELECT COUNT(*) n FROM observations")["n"] == 1
            stored = core.store.one("SELECT evidence_json FROM observations")
            assert json.loads(stored["evidence_json"])["read_observation"]["provenance"] == (
                item["context"] | {"source_grant": None}
            )
            assert (await client.post("/internal/v3/readers/bili/lease",
                                      headers=tokens["bili"])).json() == {"item": None}

    run(scenario())


@pytest.mark.parametrize("version", [2, 3])
def test_reader_lease_cannot_be_settled_through_other_version(core, version):
    tokens = credentials(core)
    core.settings.content_enabled = True
    ingest(core, text=URL)

    async def scenario():
        async with client_for(core) as client:
            leased = await client.post(f"/internal/v{version}/readers/bili/lease",
                                       headers=tokens["bili"])
            assert leased.status_code == 200
            item = leased.json()["item"]
            before = core.store.one("SELECT * FROM exploration_requests")
            other = 3 if version == 2 else 2
            body = (failed_read(item["lease_token"]) if other == 3 else {
                "lease_token": item["lease_token"], "status": "FAILED", "items": [],
                "used_bytes": 0, "error_code": "SYNTHETIC_READ_FAILURE",
            })
            rejected = await client.post(
                f"/internal/v{other}/readers/bili/{item['request_id']}/result",
                headers=tokens["bili"], json=body,
            )
            assert rejected.status_code == 409
            assert rejected.json()["error"] == (
                "STALE_READER_LEASE" if version == 2 else "READER_PROTOCOL_MISMATCH"
            )
            assert core.store.one("SELECT * FROM exploration_requests") == before
            assert core.store.rows("SELECT * FROM observations") == []

    run(scenario())


@pytest.mark.parametrize("authority", ["missing", "bridge", "diary", "bili"])
def test_expression_learning_view_requires_control_authority(core, authority):
    tokens = credentials(core)

    async def scenario():
        async with client_for(core) as client:
            response = await client.get("/internal/v2/expression-learning",
                                        headers=tokens[authority])
            assert response.status_code == 401

    before = logical_state(core.store)
    with core.store.read_snapshot():
        run(scenario())
    assert logical_state(core.store) == before


def test_expression_learning_http_is_uncached_read_only_shadow(core):
    tokens = credentials(core)
    ingest(core, text="简短一点")

    async def scenario():
        async with client_for(core) as client:
            first = await client.get("/internal/v2/expression-learning", headers=tokens["control"])
            again = await client.get("/internal/v2/expression-learning", headers=tokens["control"])
            assert first.status_code == again.status_code == 200
            assert first.headers["Cache-Control"] == again.headers["Cache-Control"] == "no-store"
            assert first.json() == again.json()
            assert first.json()["mode"] == "SHADOW"
            assert first.json()["items"][0]["value"] == "concise"

    before = logical_state(core.store)
    with core.store.read_snapshot():
        run(scenario())
    assert logical_state(core.store) == before
    for table in ("model_usage", "outbox", "memories", "traits"):
        assert core.store.rows("SELECT * FROM " + table) == []


def invalidated_diary(core):
    model = seed_day(core)
    assert generate(core)["state"] == "COMMITTED"
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert core.diary.get_day(DAY, core.settings.owner_qq_id) is None
    return model


@pytest.mark.parametrize("authority", ["missing", "bridge", "control", "bili"])
def test_diary_recompute_requires_distinct_diary_authority(core, authority):
    tokens = credentials(core)
    invalidated_diary(core)

    async def scenario():
        async with client_for(core) as client:
            response = await client.post(f"/internal/v1/diary/{DAY}/recompute",
                                         headers=tokens[authority])
            assert response.status_code == 401

    before = logical_state(core.store)
    with core.store.read_snapshot():
        run(scenario())
    assert logical_state(core.store) == before


def test_diary_recompute_http_is_idempotent_and_returns_current_queue_state(core):
    tokens = credentials(core)
    model = invalidated_diary(core)
    usage = core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        async with client_for(core) as client:
            path = f"/internal/v1/diary/{DAY}/recompute"
            first = await client.post(path, headers=tokens["diary"])
            assert first.status_code == 202
            receipt = first.json()
            assert receipt["local_date"] == DAY.isoformat() and receipt["state"] == "PENDING"
            assert receipt["run_id"]
            for state in ("PENDING", "RUNNING", "RETRY_WAIT"):
                claimed = None
                if state == "RUNNING":
                    claimed = core.diary.repo.claim("http-independent-diary-worker")
                elif state == "RETRY_WAIT":
                    row = core.store.one("SELECT * FROM diary_runs")
                    assert core.diary.repo.finish(row, "RETRY_WAIT", "SYNTHETIC_RETRY",
                                                  retry_at=after(core.clock(), 60))
                before = core.store.one("SELECT * FROM diary_runs")
                repeated = await client.post(path, headers=tokens["diary"])
                assert repeated.status_code == 202
                assert repeated.json() == receipt | {"state": state}
                assert core.store.one("SELECT * FROM diary_runs") == before
                if claimed is not None:
                    assert before["lease_token"] == claimed["lease_token"]

    run(scenario())
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert len(model.calls) == 1
    assert core.store.one("SELECT COUNT(*) n FROM diary_runs")["n"] == 1


@pytest.mark.parametrize("condition", ["unchanged", "disabled", "deleted"])
def test_diary_recompute_http_reports_when_nothing_is_scheduled(core, condition):
    tokens = credentials(core)
    if condition == "unchanged":
        seed_day(core)
        generate(core)
    else:
        invalidated_diary(core)
        if condition == "disabled":
            core.settings.diary_enabled = False
        else:
            core.diary.forget(DAY, core.settings.owner_qq_id)

    async def scenario():
        async with client_for(core) as client:
            response = await client.post(f"/internal/v1/diary/{DAY}/recompute",
                                         headers=tokens["diary"])
            assert response.status_code == 202
            assert response.json() == {
                "local_date": DAY.isoformat(), "run_id": None, "state": "NOT_SCHEDULED",
            }

    run(scenario())


@pytest.mark.parametrize("path", ["not-a-date", "2026-10-20"])
def test_diary_recompute_http_rejects_invalid_or_unclosed_day(core, path):
    tokens = credentials(core)

    async def scenario():
        async with client_for(core) as client:
            response = await client.post(f"/internal/v1/diary/{path}/recompute",
                                         headers=tokens["diary"])
            assert response.status_code == 400
            assert response.json()["error"] == (
                "INVALID_CONTRACT" if path == "not-a-date" else "DIARY_DAY_NOT_CLOSED"
            )

    run(scenario())
    assert core.store.rows("SELECT * FROM diary_runs") == []
