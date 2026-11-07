"""Reader receipts must retain their original authority through native Core commits."""

import json
from datetime import datetime

import httpx
import pytest

from companion.api import create_app
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.exploration import ReadResult
from companion.goals import GoalProposal, GoalScope
from companion.privacy import advance_epoch
from companion.reading_protocol import ReadObservation
from companion.source_grants import ReaderReadValidation, SourceGrantRegistration
from companion.store import after
from source_reader import worker_v1
from source_reader.web import WebHTTP
from tests.conftest import ingest, run
from tests.test_exploration import configure as configure_legacy
from tests.test_heybox_reader import mock_pool
from tests.test_next_reading import WEB, web_response
from tests.test_reading_protocol import envelope
from tests.test_reply_revision import accept

TOKEN = "native-core-web-reader-token-" * 3


def configure(core):
    configure_legacy(core)
    core.settings.content_enabled = core.settings.web_reading_enabled = True
    core.settings.web_reading_hosts = ("articles.example",)
    core.settings.observation_adapters["web"] = ObservationAdapterConfig(source="web", token=TOKEN)


def queued(core, source="web", *, owner=True, hosts=None, grant=None):
    configure(core)
    if hosts is not None:
        core.settings.web_reading_hosts = hosts
    adapter = {"web": "web", "bilibili": "bili", "heybox": "heybox"}[source]
    if grant is not None:
        core.settings.observation_adapters[adapter].source_grant_required = True
        core.observations.grants.register(
            SourceGrantRegistration(adapter_id=adapter, **grant.snapshot()))
    body = envelope(source, grant=grant.snapshot() if grant else None)
    if owner:
        accept(core, text="请阅读 " + body["observations"][0]["url"])
    row = core.store.one(
        'SELECT request_id FROM exploration_requests ORDER BY '
        'record_sequence DESC'
    )
    if not row:
        with core.store.transaction():
            request = core.exploration.create(
                adapter, "read", "test_native_read", body["observations"][0]["url"],
                source_event_ids=["evt_test_1"] if owner else [],
            )
    else:
        request = row["request_id"]
    item = core.reading.lease(adapter, grant)
    assert item and item["request_id"] == request
    body["lease_token"] = item["lease_token"]
    body["observations"][0]["provenance"].update(item["context"])
    body["observations"][0]["fetched_at"] = core.clock()
    body["observations"][0]["evidence"]["fetched_at"] = core.clock()
    return adapter, item, body


def validation(item):
    return ReaderReadValidation(lease_token=item["lease_token"])


def test_superseded_queued_read_does_not_block_the_next_owner_request(core):
    configure(core)
    accept(core, text="先读第一篇")
    with core.store.transaction():
        first = core.exploration.create(
            "web", "read", "old_turn", WEB, source_event_ids=["evt_test_1"],
        )
    accept(core, 2, text="改读第二篇")
    with core.store.transaction():
        second = core.exploration.create(
            "web", "read", "new_turn", WEB, source_event_ids=["evt_test_2"],
        )
    item = core.reading.lease("web")
    assert item["request_id"] == second
    assert core.store.one(
        'SELECT state,started_at,reserved_requests FROM exploration_requests WHERE request_id=%s',
        (first,),
    ) == {"state": "CANCELLED", "started_at": None, "reserved_requests": 1}
    assert not core.store.one('SELECT 1 FROM reading_attempts WHERE request_id=%s', (first,))


@pytest.mark.parametrize("source", ["web", "bilibili", "heybox"])
def test_commit_binds_real_observation_ids_and_exact_evidence(core, source):
    adapter, item, body = queued(core, source)
    receipt = ReadObservation.model_validate(body)
    checked = core.reading.validate_read(adapter, item["request_id"], validation(item))
    assert checked == {"valid": True}
    result = core.reading.complete(adapter, item["request_id"], receipt)
    assert result["state"] == "DONE" and len(result["event_ids"]) == 1
    event_id = result["event_ids"][0]
    stored = core.store.one('SELECT * FROM observations WHERE event_id=%s', (event_id,))
    evidence = json.loads(stored["evidence_json"])["read_observation"]
    assert evidence["content_sha256"] == body["observations"][0]["content_sha256"]
    assert evidence["coverage"] == body["observations"][0]["coverage"]
    assert evidence["quotes"] == body["observations"][0]["quotes"]
    assert core.store.one('SELECT * FROM event_dependencies WHERE event_id=%s', (event_id,))[
        "source_event_id"
    ] == "evt_test_1"
    assert core.reading.complete(adapter, item["request_id"], receipt)["duplicate"]
    changed = receipt.model_copy(update={"error_code": "CONFLICT"})
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        core.reading.complete(adapter, item["request_id"], changed)
    assert core.store.one("SELECT COUNT(*) AS n FROM observations")["n"] == 1
    if source == "web":
        context = json.loads(core.content.context_message("evt_test_1", 6000))["shared_content"]
        assert context[0]["event_id"] == event_id
        assert context[0]["coverage"]["scope"] == "extracted_page_text"
        assert context[0]["trust"] == "untrusted_external"
        assert all(context[0]["text"][quote["start"]:quote["end"]] == quote["text"]
                   for quote in context[0]["quotes"])


@pytest.mark.parametrize("mutation", ["root", "source", "original_url", "url", "time", "usage"])
def test_forged_envelope_cannot_commit(core, mutation):
    adapter, item, body = queued(core)
    observation = body["observations"][0]
    if mutation == "root":
        observation["provenance"]["root_task_id"] = "foreign_root"
    elif mutation == "source":
        observation["provenance"]["source_event_ids"] = []
    elif mutation in {"url", "original_url"}:
        observation[mutation] = "https://foreign.example/steal"
    elif mutation == "time":
        observation["fetched_at"] = observation["evidence"]["fetched_at"] = after(core.clock(), -60)
    else:
        body["used_bytes"] = body["observed_bytes"] = 50
    with pytest.raises(CoreError):
        core.reading.complete(adapter, item["request_id"], ReadObservation.model_validate(body))
    assert not core.store.one("SELECT 1 FROM observations")
    assert core.store.one("SELECT used_requests FROM exploration_requests")["used_requests"] is None


@pytest.mark.parametrize("change", ["new_input", "epoch", "expired", "hosts", "disabled"])
@pytest.mark.parametrize("hosts", [("articles.example",), ("*",)])
def test_late_success_settles_usage_without_committing_revoked_content(core, change, hosts):
    adapter, item, body = queued(core, hosts=hosts)
    if change == "new_input":
        accept(core, 2, text="先聊别的")
    elif change == "epoch":
        with core.store.transaction():
            advance_epoch(core.store)
    elif change == "expired":
        core.clock.advance(31)
    elif change == "hosts":
        core.settings.web_reading_hosts = ("other.example",)
    else:
        core.settings.web_reading_enabled = False
    with pytest.raises(CoreError):
        core.reading.validate_read(adapter, item["request_id"], validation(item))
    result = core.reading.complete(
        adapter, item["request_id"], ReadObservation.model_validate(body),
    )
    assert result["state"] == "CANCELLED" and not result["event_ids"]
    assert not core.store.one("SELECT 1 FROM observations")
    counts = core.store.one("SELECT used_requests,used_bytes FROM exploration_requests")
    assert counts == {"used_requests": 1, "used_bytes": 200}


def test_uncertain_receipt_keeps_resource_reservations_and_no_body(core):
    adapter, item, _ = queued(core)
    core.clock.advance(31)
    body = ReadObservation(schema_version=1, lease_token=item["lease_token"], status="UNKNOWN",
                           observations=[], observed_requests=1, observed_bytes=1234,
                           error_code="UPSTREAM_TIMEOUT")
    result = core.reading.complete(adapter, item["request_id"], body)
    assert result["state"] == "UNKNOWN"
    row = core.store.one("SELECT * FROM exploration_requests")
    assert row["reserved_requests"] > 0 and row["reserved_bytes"] > 0
    assert row["used_requests"] is None and row["used_bytes"] is None
    attempt = core.store.one("SELECT * FROM reading_attempts")
    assert attempt["observed_requests"] == 1 and attempt["observed_bytes"] == 1234


def test_legacy_protocol_cannot_validate_or_complete_v3_attempt(core):
    adapter, item, _ = queued(core, "bilibili")
    with pytest.raises(CoreError, match="READER_PROTOCOL_MISMATCH"):
        core.exploration.validate_read(adapter, item["request_id"], validation(item))
    with pytest.raises(CoreError, match="READER_PROTOCOL_MISMATCH"):
        core.exploration.complete(adapter, item["request_id"], ReadResult(
            lease_token=item["lease_token"], status="UNKNOWN",
        ))


def test_web_needs_explicit_hosts_and_v3(core):
    configure(core)
    with pytest.raises(CoreError, match="READER_PROTOCOL_REQUIRED"):
        core.exploration.lease("web")
    with core.store.transaction():
        core.exploration.create("web", "read", "foreign", "https://foreign.example/story")
    with pytest.raises(CoreError, match="CONTENT_URL_NOT_ALLOWED"):
        core.reading.lease("web")
    assert not core.store.one("SELECT 1 FROM reading_attempts")
    assert core.store.one("SELECT state FROM exploration_requests")["state"] == "QUEUED"


def test_actual_worker_through_authenticated_core_http_commits_parser_evidence(core, monkeypatch):
    configure(core)
    ingest(core, text="请阅读 " + WEB)
    assert core.store.one("SELECT 1 FROM exploration_requests")
    pool, backend = mock_pool(web_response())
    monkeypatch.setattr(worker_v1, "WebHTTP", lambda hosts: WebHTTP(hosts, pool))
    app = create_app(core.settings)
    app.state.core = core

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://core",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as client:
            return await worker_v1.tick(client, None, "web", source="web",
                                        clock=lambda: datetime.fromisoformat(core.clock()))

    assert run(scenario())
    assert len(backend.requests) == 1
    row = core.store.one("SELECT * FROM observations")
    evidence = json.loads(row["evidence_json"])["read_observation"]
    assert evidence["parser_version"] and evidence["coverage"]["level"] == "TEXT"
    assert core.store.one("SELECT state FROM reading_attempts")["state"] == "DONE"


def test_authorized_web_redirect_keeps_original_url_and_final_evidence_url(core):
    adapter, item, body = queued(core)
    final_url = "https://articles.example/final"
    body["observations"][0]["url"] = final_url
    result = core.reading.complete(
        adapter, item["request_id"], ReadObservation.model_validate(body),
    )
    assert result["state"] == "DONE"
    row = core.store.one("SELECT canonical_url,evidence_json FROM observations")
    assert row["canonical_url"] == final_url
    assert json.loads(row["evidence_json"])["read_observation"]["original_url"] == WEB


def test_accepted_goal_read_survives_new_turn_but_source_erasure_revokes_it(core):
    configure(core)
    accept(core, text="请持续跟进恒星资料")
    goal_id = core.goals.propose(GoalProposal(
        topic="恒星资料", source_event_ids=["evt_test_1"],
        scope=GoalScope(operation="read_content", url=WEB),
    ))
    core.goals.accept(goal_id)
    assert core.exploration.schedule_goal()
    item = core.reading.lease("web")
    assert item["context"]["root_task_id"] == goal_id
    accept(core, 2, "我们先聊别的，继续原任务")
    assert core.reading.validate_read("web", item["request_id"], validation(item))["valid"]
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    with pytest.raises(CoreError):
        core.reading.validate_read("web", item["request_id"], validation(item))
    receipt = ReadObservation(
        schema_version=1, lease_token=item["lease_token"], status="CANCELLED", observations=[],
        observed_requests=1, observed_bytes=123, error_code="READ_CANCELLED",
    )
    result = core.reading.complete("web", item["request_id"], receipt)
    assert result == {"state": "UNKNOWN", "event_ids": [], "duplicate": False}
    assert not core.store.one("SELECT 1 FROM observations WHERE redacted=0")


def test_web_hosts_are_owner_configuration_not_adapter_credentials(core):
    from pydantic import ValidationError

    from companion.config import Settings

    with pytest.raises(ValidationError, match="web_reading_hosts"):
        ObservationAdapterConfig(source="web", token=TOKEN, allowed_hosts=("articles.example",))
    for hosts in (("*.example.com",), ("192.168.1.1",), ("nas.local",), ("a.example", "a.example"),
                  ("*", "a.example")):
        with pytest.raises(ValidationError):
            Settings.model_validate({**core.settings.model_dump(mode="python"),
                                     "web_reading_hosts": hosts})
