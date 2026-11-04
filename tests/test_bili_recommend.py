"""Account discovery preserves grants, evidence, metering and the isolated Reader boundary."""

import asyncio
import json
import os
import sys
from contextlib import closing
from datetime import datetime

import pytest
from pydantic import ValidationError

from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.reading_protocol import ReadObservation
from companion.source_grants import SourceGrantSnapshot
from companion.store import Store, migrate
from source_reader.accounts import source_grant
from source_reader.bili_tools import BilibiliAccountHTTP, BilibiliToolReader
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, ReadBudget, ReadFailure
from source_reader.protocol import decode_lease
from source_reader.reading import ReadingContext, read_observation
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.test_bili_media import NAV as MEDIA_NAV
from tests.test_exploration import configure
from tests.test_heybox_reader import mock_pool
from tests.test_next_reading_worker import lease
from tests.test_plugin_readonly import SECRET, grant, registered_grant
from tests.test_source_reader import NOW, response

NOW_DT = datetime.fromisoformat(NOW)
NAV = {"code": 0, "data": {**MEDIA_NAV["data"], "isLogin": True}}
VIDEO = {"goto": "av", "bvid": "BV1xx411c7mD", "title": "<em>星空</em> &amp; 光谱"}
FEED = {"code": 0, "data": {"item": [
    {**VIDEO, "goto": "live"}, {**VIDEO, "goto": "bangumi"},
    {**VIDEO, "ad_info": {}}, {**VIDEO, "is_ad": True},
    {**VIDEO, "bvid": "not-a-video"}, VIDEO, VIDEO,
]}}
AUTH = {"grant_id": "grant_" + "a" * 32, "epoch": 1, "fingerprint": "a" * 64}


def item(reader, **changes):
    return {"operation": "discover", "url": "", "max_items": 20,
            "max_requests": 2, "max_bytes": 50000, "discovery_mode": "recommend",
            "source_grant": reader.grant_snapshot(required=True), **changes}


@pytest.mark.parametrize("reader_type", [BilibiliReader, BilibiliToolReader])
def test_account_recommendations_filter_cards_and_keep_title_only_evidence(tmp_path, reader_type):
    async def scenario():
        pool, backend = mock_pool(response(NAV), response(FEED))
        path = grant(tmp_path, "bilibili", authorization=AUTH)
        async with BilibiliAccountHTTP(path, pool, clock=lambda: NOW_DT) as public:
            reader = reader_type(public)
            reader.clock = lambda: NOW

            async def validate(context):
                assert context.source == "bilibili"

            result = await read_observation(reader, item(reader),
                ReadingContext("root", "bilibili", (), 0), validate, clock=lambda: NOW)
        parsed = ReadObservation.model_validate({"lease_token": "synthetic-lease", **result,
            "source_grant": result["observations"][0]["provenance"]["source_grant"]})
        assert parsed.status == "SUCCESS" and parsed.used_requests == 2
        assert len(parsed.observations) == 1
        found = parsed.observations[0]
        assert found.title == found.text == "星空 & 光谱"
        assert found.result_status == "DISCOVERED" and found.coverage.scope == "link_and_title"
        assert found.evidence.discovery_mode == "recommend"
        assert found.provenance.source_grant == parsed.source_grant
        assert SECRET not in json.dumps(result)
        assert len(backend.requests) == 2
        assert b"/x/web-interface/nav" in backend.requests[0]
        assert b"/wbi/index/top/feed/rcmd?" in backend.requests[1]
        assert b"w_rid=" in backend.requests[1]
        assert all(SECRET.encode() in request for request in backend.requests)
    run(scenario())


@pytest.mark.parametrize("payload,code", [
    ({"code": -101}, "SOURCE_CREDENTIAL_REVOKED"),
    ({"code": 0, "data": {"isLogin": False}}, "SOURCE_CREDENTIAL_REVOKED"),
    ({"code": 0, "data": {"isLogin": True}}, "UPSTREAM_RESPONSE_INVALID"),
])
def test_account_failure_stops_before_feed_without_anonymous_fallback(tmp_path, payload, code):
    async def scenario():
        pool, backend = mock_pool(response(payload))
        async with BilibiliAccountHTTP(grant(tmp_path, "bilibili", authorization=AUTH), pool,
                                       clock=lambda: NOW_DT) as public:
            reader = BilibiliReader(public, clock=lambda: NOW)
            with pytest.raises(ReadFailure, match=code):
                await reader.read(item(reader), ReadBudget(50000, 2))
        assert len(backend.requests) == 1
    run(scenario())


def test_recommendations_refuse_public_transport_before_network():
    async def scenario():
        pool, backend = mock_pool()
        async with PublicHTTP(pool) as public:
            with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
                await BilibiliReader(public).read({"operation": "discover", "max_items": 1,
                    "discovery_mode": "recommend"}, ReadBudget(50000, 2))
        assert not backend.requests
    run(scenario())


@pytest.mark.parametrize("change", [
    {"source": "heybox"}, {"source_grant_required": False}, {"reader_max_requests": 1},
])
def test_recommendation_configuration_requires_bounded_account_reader(change):
    with pytest.raises(ValidationError):
        ObservationAdapterConfig.model_validate({
            "source": "bilibili", "token": "fixture", "bilibili_discovery": "recommend",
            "source_grant_required": True, "reader_max_requests": 2, **change})


def configure_recommend(core):
    configure(core)
    core.settings.observation_adapters["bili"] = ObservationAdapterConfig(
        source="bilibili", token="bili-reader-token-" * 3, bilibili_discovery="recommend",
        source_grant_required=True, reader_max_requests=2)


def test_request_freezes_source_and_cannot_lease_via_legacy_or_without_grant(core, tmp_path):
    configure_recommend(core)
    request = core.exploration.create("bili", "discover", "recommend")
    row = core.store.one("SELECT * FROM exploration_requests WHERE request_id=?", (request,))
    assert row["discovery_mode"] == "recommend"
    with pytest.raises(CoreError, match="SOURCE_GRANT_REQUIRED"):
        core.reading.lease("bili")
    path = registered_grant(core, tmp_path, "bilibili")
    snapshot = SourceGrantSnapshot.model_validate(source_grant(
        path, datetime.fromisoformat(core.clock()), "bilibili", True))
    with pytest.raises(CoreError, match="READER_PROTOCOL_REQUIRED"):
        core.exploration.lease("bili", snapshot)
    core.settings.observation_adapters["bili"].bilibili_discovery = "popular"
    assert core.reading.lease("bili", snapshot) is None
    assert core.store.one("SELECT state FROM exploration_requests WHERE request_id=?",
                           (request,))["state"] == "CANCELLED"


def test_query_still_uses_search_in_recommendation_configuration(core):
    configure_recommend(core)
    request = core.exploration.create("bili", "discover", "interest", query="恒星光谱")
    assert core.store.one("SELECT discovery_mode FROM exploration_requests WHERE request_id=?",
                           (request,))["discovery_mode"] == "default"


@pytest.mark.parametrize("change", [{"source_grant": None}, {"query": "恒星"},
                                  {"operation": "read"}, {"max_requests": 1},
                                  {"discovery_mode": "arbitrary"}])
def test_invalid_recommendation_lease_is_rejected_before_io(change):
    packet = lease("bilibili", operation="discover", url="", max_requests=2,
                   discovery_mode="recommend", source_grant={**AUTH,
                   "expires_at": "2027-01-01T00:00:00+00:00"})
    packet.update(change)
    with pytest.raises(ReadFailure, match="READER_LEASE_INVALID"):
        decode_lease({"item": packet}, "bilibili", NOW_DT)


@pytest.mark.parametrize("tools", [False, True])
def test_real_http_and_independent_cli_commit_recommendations_once(core, tmp_path, tools):
    configure_recommend(core)
    path = registered_grant(core, tmp_path, "bilibili")
    request = core.exploration.create("bili", "discover", "independent-recommend")

    async def scenario():
        async with real_core_http(core) as port:
            for expected_calls in (2, 0):
                process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                    "tests.bili_recommend_reader_process", f"http://127.0.0.1:{port}", str(path),
                    core.clock(), str(tools).lower(), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "COMPANION_READER_TOKEN": "bili-reader-token-" * 3})
                try:
                    output, error = await asyncio.wait_for(process.communicate(
                        json.dumps({"responses": [NAV, FEED]}).encode()), 40)
                    assert process.returncode == 0, (output, error)
                    result = json.loads(output.decode().splitlines()[-1])
                    assert result["pid"] != os.getpid() and result["calls"] == expected_calls, (
                        output, error, core.store.rows("SELECT * FROM exploration_requests"))
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        row = core.store.one("SELECT * FROM exploration_requests WHERE request_id=?", (request,))
        assert row["state"] == "DONE" and row["used_requests"] == 2, (
            row["state"], row["last_error_code"], row["used_requests"])
        observed = core.store.rows("SELECT * FROM observations")
        assert len(observed) == 1 and observed[0]["result_status"] == "DISCOVERED"
        evidence = json.loads(observed[0]["evidence_json"])["read_observation"]
        assert evidence["evidence"] == {"discovery_mode": "recommend"}
        assert evidence["provenance"]["source_grant"]["grant_id"] == "grant_" + "2" * 32
        assert not core.store.rows("SELECT * FROM outbox")
    run(scenario())


def test_live_revocation_between_gets_keeps_uncertain_reservation_and_discards_content(tmp_path):
    async def scenario():
        pool, backend = mock_pool(response(NAV), response(FEED))
        path = grant(tmp_path, "bilibili", authorization=AUTH)
        async with BilibiliAccountHTTP(path, pool, clock=lambda: NOW_DT) as public:
            reader = BilibiliReader(public, clock=lambda: NOW)

            async def validate(context):
                if backend.requests:
                    raise ReadFailure("SOURCE_GRANT_REVOKED")

            result = await read_observation(reader, item(reader),
                ReadingContext("root", "bilibili", (), 0), validate, clock=lambda: NOW)
        assert result["status"] == "CANCELLED"
        assert result["error_code"] == "SOURCE_GRANT_REVOKED"
        assert result["observations"] == [] and result["used_requests"] is None
        assert len(backend.requests) == 1
    run(scenario())


def test_core_binds_discovery_evidence_to_original_request_and_rejects_late_config_change(
    core, tmp_path,
):
    configure_recommend(core)
    path = registered_grant(core, tmp_path, "bilibili")
    current = datetime.fromisoformat(core.clock())
    snapshot = SourceGrantSnapshot.model_validate(source_grant(path, current, "bilibili", True))
    request = core.exploration.create("bili", "discover", "bound-evidence")
    leased = core.reading.lease("bili", snapshot)

    async def scenario():
        pool, _ = mock_pool(response(NAV), response(FEED))
        async with BilibiliAccountHTTP(path, pool, clock=lambda: current) as public:
            reader = BilibiliReader(public, clock=core.clock)

            async def validate(context):
                assert context.source == "bilibili"

            return await read_observation(reader, leased,
                decode_lease({"item": leased}, "bilibili", current)[1], validate, clock=core.clock)

    result = ReadObservation.model_validate({**run(scenario()),
        "lease_token": leased["lease_token"], "source_grant": snapshot.snapshot()})
    changed = result.model_copy(deep=True)
    changed.observations[0].evidence.discovery_mode = None
    with pytest.raises(CoreError, match="READING_DISCOVERY_MISMATCH"):
        core.reading.complete("bili", request, changed)
    assert not core.store.rows("SELECT * FROM observations")
    core.settings.observation_adapters["bili"].bilibili_discovery = "popular"
    outcome = core.reading.complete("bili", request, result)
    assert outcome["state"] == "CANCELLED" and not outcome["event_ids"]
    assert core.store.one("SELECT used_requests FROM exploration_requests WHERE request_id=?",
                           (request,))["used_requests"] == 2


def test_migration_preserves_prior_discovery_source_and_query(tmp_path):
    path = tmp_path / "discovery.sqlite3"
    migrate(path, "0057_expression_configuration")
    with closing(Store(path)) as store:
        for key, query in (("popular", None), ("search", "恒星光谱")):
            store.insert("exploration_requests", request_id=key, request_key=key, adapter_id="bili",
                operation="discover", query=query, url="", state="QUEUED", captured_privacy_epoch=0,
                created_at=NOW, updated_at=NOW)
        before = store.rows("SELECT * FROM exploration_requests ORDER BY request_id")
    migrate(path)
    migrate(path)
    with closing(Store(path)) as store:
        after = store.rows("SELECT * FROM exploration_requests ORDER BY request_id")
        assert all(row["discovery_mode"] == "default" for row in after)
        assert [{k: v for k, v in row.items() if k != "discovery_mode"} for row in after] == before
