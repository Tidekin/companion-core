import copy
import hashlib
import json
import subprocess
import sys
import zlib
from datetime import datetime

import httpcore
import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion.api import create_app
from companion.backup import backup, restore
from companion.observation import CommentEvidence, DanmakuEvidence, ReadingEvidence, SourceComment
from companion.store import Store
from source_reader.bili_discussion import danmaku
from source_reader.bili_tools import BilibiliAccountHTTP
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, ReadFailure, permitted_url
from source_reader.worker import read_result, tick
from tests.conftest import ingest, run
from tests.support.exploration import configure
from tests.test_heybox_reader import mock_pool
from tests.test_source_reader import ITEM, META, NOW, PLAYER, SUBTITLE, response

METADATA = {"code": 0, "data": {**META["data"], "aid": 77, "cid": 1234, "duration": 12}}
COMMENTS = {"code": 0, "data": {"page": {"count": 500}, "replies": [{
    "rpid": 101, "floor": 8, "content": {"message": "这是评论观点。", "pictures": [{}]},
    "member": {"uname": "读者甲"}, "replies": [{
        "rpid": 102, "parent": 101, "content": {"message": "这是楼中楼。"},
        "member": {"uname": "读者乙"},
    }],
}]}}
XML = b'<i><chatid>1234</chatid><d p="1.25,1,25,16777215,0,0,hash,1">sample</d></i>'
WIRE = zlib.compress(XML)


def replies(comments=COMMENTS, xml=WIRE):
    return [response(METADATA), response(PLAYER), response(SUBTITLE), response(comments),
            response(xml, headers={"Content-Encoding": "deflate"})]


async def read(*, requests=5, rows=None):
    pool, backend = mock_pool(*(rows if rows is not None else replies()))
    async with PublicHTTP(pool) as source:
        result = await read_result(BilibiliReader(source, clock=lambda: NOW, discussion=True),
                                   {**ITEM, "max_requests": requests})
    return result, backend


def test_subtitles_comments_and_compressed_danmaku_keep_distinct_raw_evidence():
    result, backend = run(read())
    assert result["status"] == "SUCCESS" and result["used_requests"] == 5
    item = result["items"][0]
    evidence = ReadingEvidence.model_validate(item["reading"])
    assert evidence.level == "L2" and evidence.subtitle_status == "available"
    assert evidence.comments.items[1].reply_to_id == "101"
    assert evidence.comments.partial and evidence.comments.reported_total == 500
    assert evidence.danmaku.cid == 1234 and evidence.danmaku.items[0].at_seconds == 1.25
    assert evidence.danmaku.partial and evidence.danmaku.scope == "current_pool_sample"
    assert "这是评论观点" not in item["text"] and "sample" not in item["text"]
    source = evidence.sources[-1]
    assert source.role == "danmaku" and source.bytes == len(WIRE)
    assert source.sha256 == hashlib.sha256(WIRE).hexdigest()
    assert result["used_bytes"] == sum(row.bytes for row in evidence.sources)
    assert len(backend.requests) == 5 and b"oid=1234" in backend.requests[-1]


def test_account_reader_accepts_danmaku_binary_without_treating_it_as_json(tmp_path):
    from tests.test_plugin_readonly import grant

    async def scenario():
        pool, backend = mock_pool(*replies())
        path = grant(tmp_path, "bilibili")
        async with BilibiliAccountHTTP(
            path, pool, clock=lambda: datetime.fromisoformat(NOW)
        ) as source:
            result = await read_result(BilibiliReader(source, clock=lambda: NOW, discussion=True),
                                       {**ITEM, "max_requests": 5})
        assert len(backend.requests) == 5
        assert b"Cookie:" not in backend.requests[-1]
        return result

    result = run(scenario())
    assert result["status"] == "SUCCESS"
    assert result["items"][0]["reading"]["discussion_status"]["danmaku"] == "available"
    assert result["items"][0]["reading"]["danmaku"]["items"][0]["text"] == "sample"


@pytest.mark.parametrize("maximum", [3, 4])
def test_discussion_cannot_consume_the_subtitle_budget_or_dispatch_over_limit(maximum):
    result, backend = run(read(requests=maximum))
    item = result["items"][0]
    assert item["reading"]["subtitle_status"] == "available"
    assert result["used_requests"] == maximum and len(backend.requests) == maximum
    assert item["reading"]["discussion_status"]["danmaku"] == "budget_limited"
    assert "danmaku" not in item["reading"]
    if maximum == 3:
        assert item["reading"]["discussion_status"]["comments"] == "budget_limited"


def test_danmaku_target_mismatch_is_not_accepted_as_a_partial_video_read():
    result, backend = run(read(rows=replies(xml=zlib.compress(XML.replace(b"1234", b"9999")))))
    assert result["status"] == "FAILED" and not result["items"]
    assert result["error_code"] == "READER_DANMAKU_TARGET_MISMATCH"
    assert len(backend.requests) == 5


def test_uncertain_discussion_transport_keeps_usage_unknown_without_retry():
    result, backend = run(read(rows=[*replies()[:4], httpcore.ReadTimeout()]))
    assert result["status"] == "UNKNOWN" and result["used_requests"] is None
    assert not result["items"] and len(backend.requests) == 4


@pytest.mark.parametrize("raw,code", [
    (b'<!DOCTYPE i [<!ENTITY x SYSTEM "file:///secret">]><i><chatid>1234</chatid></i>',
     "UPSTREAM_RESPONSE_INVALID"),
    (zlib.compress(b"x" * 10000), "READER_DECODE_LIMIT"),
    (zlib.compress(XML) + b"tail", "UPSTREAM_RESPONSE_INVALID"),
    (zlib.compress('<!DOCTYPE i [<!ENTITY x "expanded">]><i><chatid>1234</chatid></i>'.encode(
        "utf-16")), "UPSTREAM_RESPONSE_INVALID"),
])
def test_danmaku_decode_rejects_dtd_bombs_and_trailing_streams(raw, code):
    with pytest.raises(ReadFailure, match=code):
        danmaku(raw, 1234, maximum=2048)


@pytest.mark.parametrize("url", [
    "https://api.bilibili.com/x/v1/dm/list.so?oid=1&oid=2",
    "https://api.bilibili.com/x/v1/dm/list.so?oid=../private",
    "https://api.bilibili.com/x/v1/dm/list.so?oid=1&extra=2",
    "https://api.bilibili.com.evil.test/x/v1/dm/list.so?oid=1",
])
def test_danmaku_endpoint_requires_one_fixed_numeric_target(url):
    with pytest.raises(ReadFailure, match="CONTENT_URL_NOT_ALLOWED"):
        permitted_url(url)


@pytest.mark.parametrize("field,value", [("floor", True), ("image_count", "1"),
                                        ("truncated", 1), ("comment_id", 101)])
def test_comment_new_scalar_contracts_are_strict(field, value):
    with pytest.raises(ValidationError):
        SourceComment.model_validate({"comment_id": "101", field: value})


@pytest.mark.parametrize("payload", [
    {"cid": True, "items": []}, {"cid": "1234", "items": []},
    {"cid": 1234, "partial": 1}, {"cid": 1234, "partial": False},
    {"cid": 1234, "items": [{"at_seconds": True, "text": "x"}]},
    {"cid": 1234, "items": [{"at_seconds": "1.2", "text": "x"}]},
])
def test_danmaku_new_scalar_contracts_are_strict(payload):
    with pytest.raises(ValidationError):
        DanmakuEvidence.model_validate(payload)


def test_optional_discussion_defaults_do_not_change_legacy_receipt_dump():
    baseline, _ = run(read(requests=3))
    reading = baseline["items"][0]["reading"]
    reading.pop("discussion_status")
    assert set(ReadingEvidence.model_validate(reading).model_dump(mode="json")) == set(reading)
    with pytest.raises(ValidationError):
        CommentEvidence.model_validate({"items": [], "reported_total": True, "partial": True})
    with pytest.raises(ValidationError):
        CommentEvidence.model_validate({"items": [], "reported_total": 100, "partial": False})


def test_cli_discussion_flag_rejects_other_source_before_io():
    result = subprocess.run([sys.executable, "-B", "-m", "source_reader", "--source", "heybox",
                             "--discussion", "--probe", "https://www.xiaoheihe.cn/app/bbs/link/1"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "--discussion requires --source bilibili" in result.stderr


@pytest.mark.parametrize("secret_field", [None, "comment_text", "comment_author", "danmaku_text"])
def test_http_worker_context_privacy_and_credential_gate(core, tmp_path, secret_field):
    configure(core)
    core.settings.content_enabled = True
    core.settings.observation_adapters["bili"].reader_max_requests = 5
    ingest(core, text=ITEM["url"])
    app = create_app(core.settings)
    app.state.core = core
    comments = copy.deepcopy(COMMENTS)
    xml = XML
    if secret_field == "comment_text":
        comments["data"]["replies"][0]["content"]["message"] = "token=synthetic-secret"
    elif secret_field == "comment_author":
        comments["data"]["replies"][0]["member"]["uname"] = "token=synthetic-secret"
    elif secret_field == "danmaku_text":
        xml = XML.replace(b"sample", b"token=synthetic-secret")

    async def scenario():
        pool, backend = mock_pool(*replies(comments, zlib.compress(xml)))
        async with PublicHTTP(pool) as source:
            reader = BilibiliReader(source, clock=core.clock, discussion=True)
            async with httpx.AsyncClient(base_url="http://core", transport=httpx.ASGITransport(app),
                    headers={"Authorization": "Bearer " + core.settings.observation_adapters[
                        "bili"].token.get_secret_value()}) as client:
                if secret_field:
                    with pytest.raises(httpx.HTTPStatusError) as failure:
                        await tick(client, reader, "bili")
                    assert failure.value.response.json()["error"] == (
                        "OBSERVATION_CONTAINS_CREDENTIALS"
                    )
                else:
                    assert await tick(client, reader, "bili")
        assert len(backend.requests) == 5

    run(scenario())
    if secret_field:
        assert not core.store.rows("SELECT * FROM observations")
        return
    projected = json.loads(core.content.context_message("evt_test_1", 4000))["shared_content"]
    assert {row["kind"] for row in projected} >= {"text", "comments", "danmaku"}
    assert len({row["event_id"] for row in projected}) == 1
    assert all(row["trust"] == "external_untrusted" for row in projected)
    assert "这是评论观点。" in next(row["text"] for row in projected if row["kind"] == "comments")
    assert "sample" in next(row["text"] for row in projected if row["kind"] == "danmaku")
    assert len(core.content.context_message("evt_test_1", 1000).encode()) <= 1000
    archive, key = tmp_path / "discussion.enc", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    restore(archive, tmp_path / "restored.db", key, core.settings.erasure_ledger)
    restored = Store(str(tmp_path / "restored.db"))
    try:
        assert all(row["evidence_json"] == "{}" and row["redacted"] for row in restored.rows(
            "SELECT evidence_json,redacted FROM observations"
        ))
        assert not restored.rows("PRAGMA foreign_key_check")
    finally:
        restored.close()
