"""Text-only QZone extraction, credential isolation and actual Reader HTTP boundaries."""

import copy
import json

import httpcore
import pytest
from pydantic import ValidationError

from companion.reading_protocol import ReadObservation
from source_reader.network import ReadFailure
from source_reader.qzone import QZoneHTTP, QZoneReader
from source_reader.reading import ReadingContext, read_observation
from tests.conftest import run
from tests.support.qzone_reader import NOW, URL, account_file, pool_for, response


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
    assert "UNREQUESTED_UI" not in json.dumps(result)
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


def test_js_data_with_undefined_extracts_html_without_executing_values(tmp_path):
    import hashlib

    html = response()["data"]["data"][0]["html"].replace('"', r'\x22').replace('<', r'\x3C')
    raw = ("{code:0,ignored:[undefined],data:{data:[{html:'" + html + "'}]}}").encode()
    result, calls, _ = read(tmp_path, raw)
    assert calls == ["GET"] and result["status"] == "SUCCESS"
    assert result["observations"][0]["evidence"]["response_sha256"] == (
        hashlib.sha256(raw).hexdigest())


def test_server_ignoring_count_keeps_only_newest_owner_body(tmp_path):
    body = response(content="OLDER_PRIVATE_POST", created_time=int(NOW.timestamp()) - 600)
    body["data"]["data"].append(response()["data"]["data"][0])
    result, calls, _ = read(tmp_path, body)
    assert calls == ["GET"] and result["status"] == "SUCCESS"
    assert len(result["observations"]) == 1 and "OLDER_PRIVATE_POST" not in json.dumps(result)


def test_platform_repeated_ui_class_uses_first_html_attribute(tmp_path):
    body = response()
    html = body["data"]["data"][0]["html"]
    body["data"]["data"][0]["html"] = html.replace('class="f-info"',
        'class="f-info" class="platform-duplicate"', 1)
    result, _, _ = read(tmp_path, body)
    assert result["status"] == "SUCCESS"
    assert result["observations"][0]["text"] == "今天看到的星空很美。"


@pytest.mark.parametrize("html", [
    lambda value: value.replace('data-uin="12345678"', 'data-uin="99999999"'),
    lambda value: value.replace('class="f-info"', 'class="txt-box"'),
    lambda value: value.replace('data-tid="synthetic-tid-one"', 'data-tid=""'),
    lambda value: value.replace('_311_0_', '_311_5_'),
    lambda value: value.replace('<i name="feed_data"',
                                '<i data-origuin="99999999" name="feed_data"'),
    lambda value: value.replace('</li>', ''),
    lambda value: value.replace('data-uin="12345678"',
                                'data-uin="12345678" data-uin="99999999"'),
])
def test_timeline_proof_and_body_fail_closed(tmp_path, html):
    body = response()
    body["data"]["data"][0]["html"] = html(body["data"]["data"][0]["html"])
    result, calls, _ = read(tmp_path, body)
    assert calls == ["GET"] and result["status"] == "FAILED" and not result["observations"]


def test_mixed_author_response_is_rejected_even_with_valid_owner_post(tmp_path):
    body = response()
    body["data"]["data"].append(response(uin=99999999)["data"]["data"][0])
    result, _, _ = read(tmp_path, body)
    assert result["status"] == "FAILED" and not result["observations"]


def test_literal_markup_in_owner_body_stays_text_and_scripts_are_discarded(tmp_path):
    body = response(content='文字 <b>不应变成标签</b> & 后文')
    html = body["data"]["data"][0]["html"]
    body["data"]["data"][0]["html"] = html.replace('</div>',
        '<script>UNREQUESTED_SCRIPT</script><style>UNREQUESTED_STYLE</style></div>', 1)
    result, _, _ = read(tmp_path, body)
    assert result["status"] == "SUCCESS"
    assert "UNREQUESTED_SCRIPT" not in json.dumps(result)
    assert "UNREQUESTED_STYLE" not in json.dumps(result)
    assert result["observations"][0]["text"] == '文字 <b>不应变成标签</b> & 后文'


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
