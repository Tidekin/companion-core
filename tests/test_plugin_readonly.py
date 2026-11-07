import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import DeliveryResult, LeaseRequest
from companion.exploration import ReadResult
from companion.source_grants import SourceGrantRegistration
from companion.store import Store
from companion.transport import begin_send, delivery_result, lease_deliveries
from source_reader.accounts import BILIBILI_COMMIT, HEYBOX_COMMIT, account_grant
from source_reader.bili_tools import TOOLS, BilibiliAccountHTTP, BilibiliToolReader, BilibiliTools
from source_reader.bilibili import BilibiliReader
from source_reader.heybox import HeyboxReader
from source_reader.heybox_signing import generate_hkey
from source_reader.heybox_source import NativeHeyboxSource
from source_reader.network import PublicHTTP, ReadBudget, ReadFailure, permitted_url
from source_reader.worker import read_result, tick
from tests.conftest import ingest, run
from tests.support.exploration import configure
from tests.test_heybox_reader import IMAGE, ITEM, POST, URL, mock_pool
from tests.test_source_reader import ITEM as BILI_ITEM
from tests.test_source_reader import META, NOW, PLAYER, SUBTITLE, response

NOW_DT = datetime.fromisoformat(NOW)
SECRET = "fixture-only-isolated-account-cookie"


def test_native_bili_account_cli_accepts_explicit_grant_and_fails_before_io_if_missing(tmp_path):
    result = subprocess.run([
        sys.executable, "-B", "-m", "source_reader", "--source", "bilibili",
        "--account-file", str(tmp_path / "absent-account.json"), "--probe", BILI_ITEM["url"],
    ], capture_output=True, text=True, timeout=15, check=True)
    payload = json.loads(result.stdout)
    assert payload["error_code"] == "OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"
    assert payload["used_requests"] == 0 and not payload["items"]


def test_native_bili_account_reader_keeps_popular_discovery(tmp_path):
    async def scenario():
        pool, backend = mock_pool(response({"code": 0, "data": {"list": [{
            "bvid": "BV1xx411c7mD", "title": "真实返回的热门标题",
        }]}}))
        async with BilibiliAccountHTTP(grant(tmp_path, "bilibili"), pool,
                                       clock=lambda: NOW_DT) as public:
            reader = BilibiliReader(public)
            result = await reader.read({"operation": "discover", "max_items": 1},
                                       ReadBudget(4096, 1))
        assert result == [{"url": BILI_ITEM["url"], "title": "真实返回的热门标题", "text": ""}]
        assert b"/x/web-interface/popular?" in backend.requests[0]
        assert SECRET.encode() in backend.requests[0]

    run(scenario())


def grant(tmp_path, source="heybox", now=NOW_DT, **updates):
    data = {
        "enabled": True,
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "cookies": {"pkey" if source == "heybox" else "SESSDATA": SECRET},
        "upstream_commit": HEYBOX_COMMIT if source == "heybox" else BILIBILI_COMMIT,
    }
    if source == "heybox":
        data.update(account_id="123", device_id="a" * 32)
    data.update(updates)
    path = tmp_path / (source + ".json")
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def registered_grant(core, tmp_path, source="heybox"):
    adapter = "bili" if source == "bilibili" else "heybox"
    grant_id = "grant_" + ("1" if adapter == "heybox" else "2") * 32
    previous = core.store.one(
        "SELECT MAX(epoch) n FROM source_grant_revisions WHERE grant_id=?", (grant_id,)
    )
    revision = (previous["n"] or 0) + 1
    authority = {
        "grant_id": grant_id,
        "epoch": revision,
        "fingerprint": hashlib.sha256(f"fixture-{adapter}-{revision}".encode()).hexdigest(),
    }
    path = grant(
        tmp_path, source, now=datetime.fromisoformat(core.clock()), authorization=authority
    )
    core.observations.grants.register(
        SourceGrantRegistration(
            adapter_id=adapter,
            expires_at=json.loads(path.read_text("utf-8"))["expires_at"],
            **authority,
        )
    )
    return path


def native_post():
    post = copy.deepcopy(POST)
    row = post["result"].pop("link")
    row["content"] = row.pop("text")
    row["link_id"] = row.pop("linkid")
    post["result"]["post"] = row
    return post


async def native_read(path, payload=POST, item=ITEM):
    pool, backend = mock_pool(response(payload))
    async with NativeHeyboxSource(path, pool, clock=lambda: NOW_DT) as source:
        result = await read_result(HeyboxReader(None, source, clock=lambda: NOW), item)
    return result, backend


def test_pinned_mit_signer_matches_upstream_vector():
    assert (
        generate_hkey("/bbs/app/user/message", 1700000000, "0123456789ABCDEF0123456789ABCDEF")
        == "YT27P47"
    )


def test_native_get_uses_one_reserved_request_and_retains_raw_provenance(tmp_path):
    payload = native_post()
    result, backend = run(native_read(grant(tmp_path), payload))
    assert result["status"] == "SUCCESS" and result["used_requests"] == 1
    article = result["items"][0]["article"]
    assert (
        article["response_sha256"]
        == hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    )
    assert article["response_bytes"] == result["used_bytes"]
    assert result["items"][0]["image_urls"] == [IMAGE]
    wire = backend.requests[0]
    assert wire.startswith(b"GET /bbs/app/link/tree?")
    assert b"link_id=168860244" in wire and b"hkey=" in wire
    assert SECRET.encode() in wire and b"POST" not in wire
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "updates",
    [
        {"enabled": False},
        {"enabled": "true"},
        {"expires_at": NOW},
        {"expires_at": "2027-01-01T00:00:00"},
        {"cookies": {}},
        {"cookies": {"pkey": "value\r\nHost: evil"}},
        {"account_id": "123&write=true"},
        {"device_id": "bad"},
        {"extra": True},
    ],
)
def test_invalid_account_authority_never_reaches_network(tmp_path, updates):
    result, backend = run(native_read(grant(tmp_path, **updates)))
    assert result["error_code"] == "OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"
    assert result["used_requests"] == 0 and not backend.requests


def test_missing_authority_and_version_drift_fail_closed(tmp_path):
    for path, code in [
        (None, "OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"),
        (grant(tmp_path, upstream_commit="changed"), "PLUGIN_UNAVAILABLE"),
    ]:
        result, backend = run(native_read(path))
        assert result["error_code"] == code and not backend.requests


@pytest.mark.parametrize(
    "payload,status,code",
    [
        ({}, 401, "SOURCE_CREDENTIAL_REVOKED"),
        ({}, 403, "UPSTREAM_FORBIDDEN"),
        ({}, 429, "UPSTREAM_RATE_LIMITED"),
        ({"status": "relogin"}, 200, "SOURCE_CREDENTIAL_REVOKED"),
        ({"status": "show_captcha"}, 200, "UPSTREAM_CAPTCHA_REQUIRED"),
        ({"status": "failed", "msg": "missing hkey"}, 200, "UPSTREAM_SIGNATURE_CHANGED"),
    ],
)
def test_rejection_latches_source_without_retries(tmp_path, payload, status, code):
    async def scenario():
        pool, backend = mock_pool(response(payload, status))
        async with NativeHeyboxSource(grant(tmp_path), pool, clock=lambda: NOW_DT) as source:
            first = await read_result(HeyboxReader(None, source), ITEM)
            second = await read_result(HeyboxReader(None, source), ITEM)
        assert first["error_code"] == code and first["used_requests"] == 1
        assert second["error_code"] == "SOURCE_DISABLED" and second["used_requests"] == 0
        assert len(backend.requests) == 1

    run(scenario())


def test_revoke_during_heybox_fetch_discards_late_body(tmp_path):
    path = grant(tmp_path)
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        if calls == 3:
            grant(tmp_path, enabled=False)
        return NOW_DT

    async def scenario():
        pool, _ = mock_pool(response(POST))
        async with NativeHeyboxSource(path, pool, clock) as source:
            result = await read_result(HeyboxReader(None, source), ITEM)
        assert result["status"] == "FAILED" and not result["items"]
        assert result["used_requests"] == 1

    run(scenario())


@pytest.mark.parametrize(
    "name,args",
    [
        ("bili_reply", {"force": True}),
        ("bili_send_message", {}),
        ("bili_upload", {}),
        ("bili_login", {}),
        ("bili_search", {"keyword": "games", "force": True}),
        ("bili_subtitle", {"bvid": "BV1xx411c7mD", "url": "http://127.0.0.1"}),
        ("bili_search", {"keyword": "games", "num": 100000}),
    ],
)
def test_only_static_read_tools_and_exact_arguments_are_accepted(name, args):
    async def scenario():
        pool, backend = mock_pool()
        async with PublicHTTP(pool) as public:
            tools = BilibiliTools(public)
            assert tools.list_tools() == list(TOOLS)
            with pytest.raises(ReadFailure, match="CAPABILITY_"):
                await tools.call(name, args, ReadBudget(524288, 4))
        assert not backend.requests

    run(scenario())


def test_search_is_one_get_and_only_creates_discovered_candidates():
    async def scenario():
        pool, backend = mock_pool(
            response(
                {
                    "code": 0,
                    "data": {
                        "result": [
                            {"bvid": "BV1xx411c7mD", "title": '<em class="keyword">游戏</em>笔记'},
                        ]
                    },
                }
            )
        )
        async with PublicHTTP(pool) as public:
            budget = ReadBudget(524288, 1)
            rows = await BilibiliToolReader(public, "游戏").read(
                {"operation": "discover", "max_items": 4}, budget
            )
        assert rows[0]["title"] == "游戏笔记" and rows[0]["text"] == ""
        assert budget.used_requests == 1
        assert backend.requests[0].startswith(b"GET /x/web-interface/search/type?")

    run(scenario())


def test_search_skips_course_cards_and_invalid_video_ids():
    async def scenario():
        pool, backend = mock_pool(
            response(
                {
                    "code": 0,
                    "data": {
                        "result": [
                            {"type": "ketang", "bvid": "", "title": "课程卡片"},
                            {"type": "video", "bvid": "https://evil.test/", "title": "无效视频"},
                            {"type": "video", "bvid": "BV1xx411c7mD", "title": "有效视频"},
                        ]
                    },
                }
            )
        )
        async with PublicHTTP(pool) as public:
            rows = await BilibiliTools(public).call(
                "bili_search", {"keyword": "游戏", "num": 1}, ReadBudget(524288, 1)
            )
        assert len(rows) == 1 and rows[0]["title"] == "有效视频"
        assert len(backend.requests) == 1

    run(scenario())


def test_account_subtitles_preserve_timestamps_and_never_send_cookie_to_cdn(tmp_path):
    async def scenario():
        pool, backend = mock_pool(response(META), response(PLAYER), response(SUBTITLE))
        async with BilibiliAccountHTTP(
            grant(tmp_path, "bilibili"), pool, clock=lambda: NOW_DT
        ) as public:
            result = await read_result(BilibiliToolReader(public), BILI_ITEM)
        assert result["status"] == "SUCCESS" and result["used_requests"] == 3
        reading = result["items"][0]["reading"]
        assert reading["method"] == "account_api" and reading["level"] == "L2"
        assert reading["spans"] == [
            {"start_seconds": 0, "end_seconds": 2.5},
            {"start_seconds": 4, "end_seconds": 6},
        ]
        assert SECRET.encode() in backend.requests[0] and SECRET.encode() in backend.requests[1]
        assert SECRET.encode() not in backend.requests[2]
        assert SECRET not in json.dumps(result)

    run(scenario())


def test_bilibili_identity_change_or_expiry_discards_result(tmp_path):
    path = grant(tmp_path, "bilibili")
    http = BilibiliAccountHTTP(path, clock=lambda: NOW_DT)
    budget = ReadBudget(524288, 4)
    http.verify(budget)
    grant(tmp_path, "bilibili", cookies={"SESSDATA": "a-different-test-account"})
    with pytest.raises(ReadFailure, match="SOURCE_CREDENTIAL_REVOKED"):
        http.verify(budget)
    grant(tmp_path, "bilibili", enabled=False)
    with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
        http.verify(ReadBudget(524288, 4))
    run(http.pool.aclose())


def test_bilibili_revoked_json_does_not_fall_back_to_anonymous_l1(tmp_path):
    async def scenario():
        pool, backend = mock_pool(response(META), response({"code": -101}))
        async with BilibiliAccountHTTP(
            grant(tmp_path, "bilibili"), pool, clock=lambda: NOW_DT
        ) as public:
            result = await read_result(BilibiliToolReader(public), BILI_ITEM)
        assert result["error_code"] == "SOURCE_CREDENTIAL_REVOKED"
        assert not result["items"] and len(backend.requests) == 2

    run(scenario())


def test_native_reader_http_core_chat_and_erasure_replay(core, tmp_path):
    configure(core)
    core.settings.content_enabled = True
    ingest(core, text=URL)
    app = create_app(core.settings)
    app.state.core = core

    async def scenario():
        pool, backend = mock_pool(response(native_post()))
        authority = {"grant_id": "grant_" + "1" * 32, "epoch": 1, "fingerprint": "a" * 64}
        path = grant(tmp_path, now=datetime.fromisoformat(core.clock()), authorization=authority)
        expires = json.loads(path.read_text("utf-8"))["expires_at"]
        core.observations.grants.register(
            SourceGrantRegistration(adapter_id="heybox", expires_at=expires, **authority)
        )
        core.settings.observation_adapters["heybox"].source_grant_required = True
        async with NativeHeyboxSource(
            path, pool, clock=lambda: datetime.fromisoformat(core.clock())
        ) as source:
            token = core.settings.observation_adapters["heybox"].token.get_secret_value()
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + token},
            ) as client:
                reader = HeyboxReader(None, source, clock=core.clock)
                assert await tick(client, reader, "heybox")
                assert not await tick(client, reader, "heybox")
        assert token.encode() not in backend.requests[0]

    run(scenario())
    context = core.content.context_message("evt_test_1", 4000)
    assert "第一段介绍了手绘地图" in context and "external_untrusted" in context
    assert SECRET not in context
    core.clock.advance(31)
    assert run(core.run_once())
    key, archive = Fernet.generate_key(), tmp_path / "before.enc"
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    restore(archive, tmp_path / "restored.db", key, core.settings.erasure_ledger)
    restored = Store(str(tmp_path / "restored.db"))
    try:
        assert restored.one("SELECT evidence_json FROM observations")["evidence_json"] == "{}"
        assert all(row["redacted"] for row in restored.rows("SELECT * FROM content_items"))
        assert not restored.rows("PRAGMA foreign_key_check")
    finally:
        restored.close()


def test_account_file_is_bounded_and_permission_checked(tmp_path):
    path = grant(tmp_path)
    path.write_text(" " * 8193)
    with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
        account_grant(path, NOW_DT)
    if os.name != "nt":
        path = grant(tmp_path)
        path.chmod(0o644)
        with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
            account_grant(path, NOW_DT)


def test_real_ai_subtitle_cdn_shape_is_allowed_without_opening_arbitrary_paths():
    path = "/bfs/ai_subtitle/prod/113383059752423264988299519abc6147dc42edc1a09306845e085a10"
    permitted_url("https://aisubtitle.hdslb.com" + path + "?auth_key=transient")
    for target in [
        "https://aisubtitle.hdslb.com.evil.test" + path,
        "https://aisubtitle.hdslb.com/private/" + "a" * 64,
        "https://aisubtitle.hdslb.com" + path + "/../../secret",
        "https://aisubtitle.hdslb.com:444" + path,
    ]:
        with pytest.raises(ReadFailure):
            permitted_url(target)


@pytest.mark.parametrize("mismatch", ["none", "player", "track", "duration"])
def test_subtitle_matches_requested_video_part_and_duration(mismatch):
    meta = copy.deepcopy(META)
    meta["data"].update(aid=77, bvid=BILI_ITEM["url"].rsplit("/", 1)[1], duration=10)
    track = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/771234" + "a" * 32
    player = {
        "code": 0,
        "data": {
            "aid": 77,
            "cid": 1234,
            "subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": track}]},
        },
    }
    if mismatch == "player":
        player["data"]["cid"] = 5678
    elif mismatch == "track":
        player["data"]["subtitle"]["subtitles"][0]["subtitle_url"] = track.replace(
            "771234", "881234"
        )
    elif mismatch == "duration":
        meta["data"]["duration"] = 4

    async def scenario():
        pool, backend = mock_pool(response(meta), response(player), response(SUBTITLE))
        async with PublicHTTP(pool) as public:
            result = await read_result(BilibiliToolReader(public), BILI_ITEM)
        if mismatch == "none":
            assert result["status"] == "SUCCESS" and result["items"][0]["reading"]["level"] == "L2"
        else:
            assert result["error_code"] == "READER_SUBTITLE_TARGET_MISMATCH"
            assert not result["items"]
        assert b"aid=77&cid=1234" in backend.requests[1]
        assert b"GET /x/player/wbi/v2?" in backend.requests[1]
        assert len(backend.requests) == (2 if mismatch in {"player", "track"} else 3)

    run(scenario())


def test_current_player_keeps_account_scope_and_chooses_available_chinese_track(tmp_path):
    player = copy.deepcopy(PLAYER)
    english = copy.deepcopy(player["data"]["subtitle"]["subtitles"][0])
    english.update(lan="en", subtitle_url=english["subtitle_url"].replace("example", "english"))
    chinese = copy.deepcopy(player["data"]["subtitle"]["subtitles"][0])
    chinese["lan"] = "ai-zh"
    player["data"]["subtitle"]["subtitles"] = [
        {"lan": "zh-CN", "subtitle_url": ""}, None, english, chinese,
    ]

    async def scenario():
        pool, backend = mock_pool(response(META), response(player), response(SUBTITLE))
        async with BilibiliAccountHTTP(grant(tmp_path, "bilibili"), pool,
                                       clock=lambda: NOW_DT) as public:
            result = await read_result(BilibiliReader(public), BILI_ITEM)
        evidence = result["items"][0]["reading"]
        assert result["status"] == "SUCCESS" and result["used_requests"] == 3
        assert evidence["level"] == "L2" and evidence["language"] == "ai-zh"
        assert b"GET /x/player/wbi/v2?" in backend.requests[1]
        assert SECRET.encode() in backend.requests[1]
        assert b"/bfs/subtitle/example.json?" in backend.requests[2]
        assert SECRET.encode() not in backend.requests[2]

    run(scenario())


@pytest.mark.parametrize("restricted", [False, True])
def test_empty_player_track_does_not_fetch_cdn_or_retry_old_endpoint(restricted):
    player = {"code": 0, "data": {"need_login_subtitle": restricted, "subtitle": {
        "subtitles": [{"lan": "ai-zh", "subtitle_url": ""},
                      {"lan": "zh-CN", "subtitle_url": "   "}],
    }}}

    async def scenario():
        pool, backend = mock_pool(response(META), response(player))
        async with PublicHTTP(pool) as public:
            result = await read_result(BilibiliReader(public), BILI_ITEM)
        evidence = result["items"][0]["reading"]
        assert result["status"] == "SUCCESS" and result["used_requests"] == 2
        assert evidence["level"] == "L1" and not evidence["spans"]
        assert evidence["subtitle_status"] == ("restricted" if restricted else "missing")
        assert len(backend.requests) == 2

    run(scenario())


def test_both_sources_unavailable_preserves_ordinary_chat(core):
    configure(core)
    core.settings.content_enabled = True
    ingest(core, 1, URL)
    ingest(core, 2, BILI_ITEM["url"])

    async def fail_sources():
        async with NativeHeyboxSource() as heybox, BilibiliAccountHTTP(None) as bili:
            for adapter, reader in [
                ("heybox", HeyboxReader(None, heybox)),
                ("bili", BilibiliToolReader(bili)),
            ]:
                lease = core.exploration.lease(adapter)
                result = await read_result(reader, lease)
                assert result["error_code"] == "OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"
                assert result["used_requests"] == 0
                core.exploration.complete(
                    adapter, lease["request_id"], ReadResult.model_validate(result)
                )

    run(fail_sources())
    ingest(core, 3, "继续聊今天的计划")
    core.clock.advance(31)
    for _ in range(3):
        assert run(core.run_once())
        # Honor the ordinary QQ FIFO by simulating each transport receipt in order.
        # No QQ transport or external sending is involved in this test.
        delivery = lease_deliveries(
            core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
        )["items"][0]
        begin_send(core.store, delivery["message_id"], delivery["lease_token"], core.clock())
        delivery_result(
            core.store,
            core.settings,
            delivery["message_id"],
            DeliveryResult(lease_token=delivery["lease_token"], result="SENT"),
            core.clock(),
        )
    assert not core.store.rows("SELECT * FROM observations")
    assert "继续聊今天的计划" in json.dumps(core.model.calls, ensure_ascii=False)
    assert core.store.rows("SELECT * FROM outbox")
