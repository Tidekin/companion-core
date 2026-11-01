"""Text-only QZone extraction, credential isolation and actual Reader HTTP boundaries."""

import copy
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpcore
import pytest
from pydantic import ValidationError

from companion.reading_protocol import ReadObservation
from source_reader.accounts import QZONE_COMMIT
from source_reader.network import ReadFailure
from source_reader.qzone import ENDPOINT, QZoneHTTP, QZoneReader
from source_reader.reading import ReadingContext, read_observation
from tests.conftest import run

NOW = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
URL = "https://user.qzone.qq.com/12345678"
TOKEN = "qzone-reader-synthetic-" * 3


def account_file(path, now=NOW):
    grant = {"grant_id": "grant_" + "e" * 32, "epoch": 1, "fingerprint": "a" * 64}
    expires = (now + timedelta(hours=1)).isoformat(timespec="microseconds")
    path.write_text(json.dumps({"enabled": True, "expires_at": expires,
        "upstream_commit": QZONE_COMMIT, "cookies": {"uin": "o87654321",
        "p_skey": "synthetic-private-key"}, "authorization": grant}), encoding="utf-8")
    path.chmod(0o600)
    return {**grant, "expires_at": expires}


def response(now=NOW, **changes):
    return {"code": 0, "msglist": [{"uin": 12345678, "tid": "synthetic-tid-one",
        "content": "今天看到的星空很美。", "created_time": int(now.timestamp()) - 60,
        "commentlist": [{"content": "UNREQUESTED_COMMENT"}], **changes}]}


def pool_for(body, calls):
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    wire = (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(raw)).encode()
            + b"\r\nConnection: close\r\n\r\n" + raw)

    class Pool(httpcore.AsyncConnectionPool):
        def stream(self, method, url, **kwargs):
            assert method == "GET" and url.startswith(ENDPOINT + "?")
            query = parse_qs(urlsplit(url).query)
            assert query["num"] == ["1"] and query["uin"] == ["12345678"]
            assert query["qzreferrer"] == ["https://user.qzone.qq.com/87654321"]
            calls.append(method)
            return super().stream(method, url, **kwargs)

    return Pool(network_backend=httpcore.AsyncMockBackend([wire]))


def read(tmp_path, body=None, *, mutate=None, guard=None, item_changes=None):
    path = tmp_path / "account.json"
    grant = account_file(path)
    item = {"operation": "read", "url": URL, "max_items": 1,
            "max_requests": 1, "max_bytes": 524288, "source_grant": grant}
    item.update(item_changes or {})
    calls, validations = [], []

    async def validate(context):
        validations.append(context)
        if guard:
            guard(len(validations), path)

    async def scenario():
        async with QZoneHTTP(path, pool_for(body or response(), calls), lambda: NOW) as public:
            reader = QZoneReader(public, lambda: NOW)
            if mutate:
                mutate(reader)
            return await read_observation(reader, item, ReadingContext(
                "read_synthetic", "qzone", (), 0), validate, clock=lambda: NOW.isoformat())

    return run(scenario()), calls, validations


def test_one_post_text_preserves_provenance_and_discards_comments_and_credentials(tmp_path):
    result, calls, validations = read(tmp_path)
    assert calls == ["GET"] and len(validations) >= 3
    parsed = ReadObservation.model_validate({**result, "lease_token": "synthetic-lease",
                                           "source_grant": account_file(tmp_path / "grant.json")})
    observed = parsed.observations[0]
    assert observed.coverage.scope == "latest_post_text"
    assert observed.evidence.tid == "synthetic-tid-one"
    assert observed.provenance.source == "qzone"
    assert observed.trust == "untrusted_external"
    assert not observed.evidence.comments_read and not observed.evidence.images_analyzed
    assert "UNREQUESTED_COMMENT" not in json.dumps(result)
    assert "synthetic-private-key" not in json.dumps(result)
    assert "g_tk" not in json.dumps(result)
    assert "87654321" not in json.dumps(result)


@pytest.mark.parametrize("callback", [b"_Callback", b"callback"])
def test_platform_jsonp_is_data_only_and_retains_original_response_hash(tmp_path, callback):
    import hashlib

    raw = b" \n" + callback + b"(" + json.dumps(response(), ensure_ascii=False).encode() + b");\n"
    result, calls, _ = read(tmp_path, raw)
    assert result["status"] == "SUCCESS" and calls == ["GET"]
    evidence = result["observations"][0]["evidence"]
    assert evidence["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert evidence["response_bytes"] == len(raw)


@pytest.mark.parametrize("raw", [
    b"callback({});alert(1)", b"unapprovedCallback({});", b"prefix callback({});",
    b"callback({}) callback({})", b"callback(not_json)",
])
def test_jsonp_requires_one_known_complete_envelope(tmp_path, raw):
    result, calls, _ = read(tmp_path, raw)
    assert calls == ["GET"] and result["status"] == "FAILED"
    assert result["error_code"] == "UPSTREAM_RESPONSE_INVALID" and not result["observations"]


def test_jsonp_access_denial_does_not_become_a_format_error(tmp_path):
    result, calls, _ = read(tmp_path, b'_Callback({"code":-3000});')
    assert calls == ["GET"] and result["status"] == "FAILED"
    assert result["error_code"] == "UPSTREAM_ACCESS_LIMIT" and not result["observations"]


@pytest.mark.parametrize("change", [
    {"operation": "discover"}, {"max_items": 2}, {"source_grant": None},
    {"url": URL + "?uin=99999"}, {"url": URL + "/mood/another"},
    {"url": "https://user.qzone.qq.com.evil.example/12345678"},
])
def test_unapproved_scope_never_reaches_platform(tmp_path, change):
    result, calls, _ = read(tmp_path, item_changes=change)
    assert result["status"] in {"FAILED", "CANCELLED"} and not calls
    assert result["observations"] == []


@pytest.mark.parametrize("changes", [
    {"uin": 99999999}, {"uin": True}, {"tid": ""}, {"created_time": True},
    {"content": ""}, {"created_time": int(NOW.timestamp()) + 100},
])
def test_missing_or_foreign_post_proof_is_not_accepted(tmp_path, changes):
    result, calls, _ = read(tmp_path, response(**changes))
    assert calls == ["GET"] and result["status"] == "FAILED"
    assert not result["observations"]


def test_revocation_at_real_http_boundary_discards_content_and_retains_uncertainty(tmp_path):
    def stop(number, path):
        if number >= 3:
            raise ReadFailure("SOURCE_GRANT_REVOKED")

    result, _, _ = read(tmp_path, guard=stop)
    assert result["status"] == "CANCELLED" and not result["observations"]
    assert result["used_requests"] is None and result["used_bytes"] is None


def test_account_rotation_after_response_does_not_commit_text(tmp_path):
    def rotate(number, path):
        if number == 4:
            value = json.loads(path.read_text("utf-8"))
            value["cookies"]["p_skey"] = "changed-synthetic-key"
            path.write_text(json.dumps(value), encoding="utf-8")

    result, calls, _ = read(tmp_path, guard=rotate)
    assert calls == ["GET"] and not result["observations"]
    assert result["status"] == "FAILED"
    assert result["error_code"] == "SOURCE_CREDENTIAL_REVOKED"


@pytest.mark.parametrize("field,value", [
    ("author_uin", "99999999"), ("tid", ""), ("comments_read", 0), ("images_analyzed", 0),
    ("response_bytes", True), ("scope", "article_text"),
])
def test_core_rejects_foreign_identity_and_false_evidence_claims(tmp_path, field, value):
    result, _, _ = read(tmp_path)
    wire = {**copy.deepcopy(result), "lease_token": "synthetic-lease",
            "source_grant": account_file(tmp_path / "grant.json")}
    wire["observations"][0]["evidence"][field] = value
    with pytest.raises(ValidationError):
        ReadObservation.model_validate(wire)


def test_interrupted_transport_retains_unknown_read_usage(tmp_path):
    grant = account_file(tmp_path / "account.json")

    async def guard(context):
        return None

    async def scenario():
        pool = httpcore.AsyncConnectionPool(network_backend=httpcore.AsyncMockBackend([
            b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\nx"]))
        async with QZoneHTTP(tmp_path / "account.json", pool, lambda: NOW) as public:
            return await read_observation(QZoneReader(public, lambda: NOW),
                {"operation": "read", "url": URL, "max_items": 1, "max_requests": 1,
                 "max_bytes": 524288, "source_grant": grant},
                ReadingContext("read_synthetic", "qzone", (), 0), guard,
                clock=lambda: NOW.isoformat())

    result = run(scenario())
    assert result["status"] == "UNKNOWN" and not result["observations"]
    assert result["observed_requests"] == 1 and result["used_requests"] is None
    assert result["used_bytes"] is None


def test_known_account_rejection_stops_further_platform_requests(tmp_path):
    grant = account_file(tmp_path / "account.json")
    calls = []

    async def guard(context):
        return None

    async def scenario():
        async with QZoneHTTP(tmp_path / "account.json", pool_for({"code": -1}, calls),
                              lambda: NOW) as public:
            reader = QZoneReader(public, lambda: NOW)
            outcomes = []
            for _ in range(2):
                outcomes.append(await read_observation(reader,
                    {"operation": "read", "url": URL, "max_items": 1, "max_requests": 1,
                     "max_bytes": 524288, "source_grant": grant},
                    ReadingContext("read_synthetic", "qzone", (), 0), guard,
                    clock=lambda: NOW.isoformat()))
            return outcomes

    first, second = run(scenario())
    assert calls == ["GET"]
    assert first["error_code"] == "UPSTREAM_ACCESS_LIMIT"
    assert second["error_code"] == "SOURCE_DISABLED" and second["observed_requests"] == 0
