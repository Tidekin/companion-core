"""Actual Reader transport boundaries; native encoding is covered by the worker tests."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from companion.bili_video_protocol import DEFAULT_LIMITS, json_sha256
from source_reader.bili_media import MediaHTTP, media_locator, signed_query
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, ReadBudget, ReadFailure
from source_reader.protocol import decode_lease
from source_reader.reading import ReadingStopped
from tests.conftest import run
from tests.support.bili_media import BVID, CDN, NAV, NOW, PLAY, VIEW, allowed, lease, pool
from tests.support.source_reader import response


async def resolve(*responses, media=None, limits=DEFAULT_LIMITS):
    value, backend = pool(*responses)
    budget = ReadBudget(33554432 + 1048576, 8)
    budget.validate_account_read = allowed
    async with PublicHTTP(value) as public:
        result = await MediaHTTP(public, budget, allowed).resolve(
            media or lease()["media"], limits, NOW,
        )
    return result, budget, backend


def test_resolver_binds_first_part_exact_duration_and_pins_only_three_fixed_apis():
    (identity, proof, locator), budget, backend = run(resolve(
        response(VIEW), response(NAV), response(PLAY),
    ))
    assert identity == {"bvid": BVID, "cid": 123, "part": 1, "duration_seconds": 3.0}
    assert locator == CDN and proof["duration_milliseconds"] == 3000
    assert proof["locator_sha256"] == hashlib.sha256(CDN.encode()).hexdigest()
    assert "bilivideo" not in json.dumps(proof) and "token=" not in json.dumps(proof)
    assert proof["playinfo_response_sha256"] == hashlib.sha256(
        json.dumps(PLAY, ensure_ascii=False).encode(),
    ).hexdigest()
    assert budget.used_requests == 3 and backend.hosts == ["93.184.216.34"] * 3
    assert backend.server_names == ["api.bilibili.com"] * 3
    request = backend.requests[-1].split(b" ")[1].decode()
    params = parse_qs(urlsplit(request).query)
    assert params["bvid"] == [BVID] and params["cid"] == ["123"]
    assert params["fnval"] == ["4048"] and len(params["w_rid"][0]) == 32
    assert set(params) == {"bvid", "cid", "fnval", "wts", "w_rid"}


@pytest.mark.parametrize("replacement", [
    {"bvid": "BV0000000000"}, {"cid": 124}, {"pages": []},
    {"pages": [{"page": 2, "cid": 123, "duration": 3}]},
    {"pages": [{"page": 1, "cid": 124, "duration": 3}]},
    {"pages": [{"page": 1, "cid": 123, "duration": 4}]},
])
def test_view_mismatch_fails_before_playinfo_or_cdn(replacement):
    changed = {**VIEW, "data": {**VIEW["data"], **replacement}}
    with pytest.raises(ReadFailure, match="READER_TARGET_MISMATCH"):
        run(resolve(response(changed)))


@pytest.mark.parametrize("change", [{"timelength": True}, {"timelength": 5000},
                                     {"cid": 124}, {"bvid": "BV0000000000"}])
def test_playinfo_identity_and_exact_duration_must_refine_original_part(change):
    changed = {**PLAY, "data": {**PLAY["data"], **change}}
    with pytest.raises(ReadFailure, match="READER_TARGET_MISMATCH"):
        run(resolve(response(VIEW), response(NAV), response(changed)))


@pytest.mark.parametrize("value", [
    "http://cn-test.bilivideo.com/movie.mp4", "https://bilivideo.com.attacker.test/movie.mp4",
    "https://localhost/movie.mp4", "https://127.0.0.1/movie.mp4",
    "https://user@cn-test.bilivideo.com/movie.mp4", CDN + "#fragment",
    "https://cn-test.bilivideo.com:444/movie.mp4", CDN + "\r\nCookie:secret",
])
def test_media_locator_is_exact_https_bili_domain_without_credentials(value):
    with pytest.raises(ReadFailure):
        media_locator(value)


@pytest.mark.parametrize("code", [-101, -352, -401, -412])
def test_upstream_access_denial_has_no_representation_fallback(code):
    with pytest.raises(ReadFailure, match="UPSTREAM_ACCESS_LIMIT"):
        run(resolve(response(VIEW), response({"code": code, "data": {}})))


def test_signing_uses_only_bounded_upstream_keys_and_removes_reserved_characters():
    params = parse_qs(signed_query({"text": "!'()*accepted", "cid": 123}, NAV["data"], NOW))
    assert params["text"] == ["accepted"]
    assert params["wts"] == [str(round(NOW.timestamp()))]
    changed = copy.deepcopy(NAV["data"])
    changed["wbi_img"]["img_url"] = "https://example.test/not-a-key.png"
    with pytest.raises(ReadFailure):
        signed_query({}, changed, NOW)


def test_media_lease_requires_explicit_capability_and_identical_local_limits():
    item = lease()
    with pytest.raises(ReadFailure, match="READER_LEASE_INVALID"):
        decode_lease({"item": item}, "bilibili", NOW)
    with pytest.raises(ReadFailure, match="READER_LEASE_INVALID"):
        decode_lease({"item": item}, "bilibili", NOW,
                     media_limits=replace(DEFAULT_LIMITS, max_preview_frames=1))
    parsed, _, _, _ = decode_lease({"item": item}, "bilibili", NOW, media_limits=DEFAULT_LIMITS)
    assert json_sha256(parsed) == json_sha256(item)


@pytest.mark.parametrize("change", [
    {"max_bytes": 33554432 + 1048577}, {"max_items": 2}, {"operation": "discover"},
    {"url": "https://www.bilibili.com/video/BV0000000000"},
    {"lease_until": (NOW + timedelta(seconds=181)).isoformat()},
    {"context": {"root_task_id": "root_media", "source": "bilibili",
                 "source_event_ids": ["another_source"], "privacy_epoch": 1}},
])
def test_media_lease_cannot_expand_or_rebind_its_scope(change):
    with pytest.raises(ReadFailure):
        decode_lease({"item": lease(**change)}, "bilibili", NOW, media_limits=DEFAULT_LIMITS)


def test_download_revalidates_after_response_and_before_writing_bytes(tmp_path):
    async def scenario():
        value, backend = pool(response(b"123456"))
        budget = ReadBudget(1024, 1)
        checks = 0

        async def revoke_after_headers():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise ReadingStopped("SOURCE_GRANT_REVISION_CHANGED")

        budget.validate_account_read = revoke_after_headers
        output = tmp_path / "owned.media"
        with output.open("xb") as sink:
            async with PublicHTTP(value) as public:
                with pytest.raises(ReadingStopped, match="SOURCE_GRANT_REVISION_CHANGED"):
                    await MediaHTTP(public, budget, revoke_after_headers).transfer(
                        CDN, maximum=1024, sink=sink, mime="video/mp4",
                    )
        assert output.read_bytes() == b"" and budget.used_requests == 1
        assert budget.used_bytes == 6 and len(backend.requests) == 1

    run(scenario())


def test_metadata_exports_real_first_part_identity_and_timed_text_without_media_io():
    async def scenario():
        value, backend = pool(response(VIEW), response({"code": 0, "data": {"subtitle": {
            "subtitles": [{"lan": "zh", "subtitle_url":
                           "https://aisubtitle.hdslb.com/bfs/subtitle/example.json"}],
        }}}), response({"body": [{"from": 0, "to": 2, "content": "actual timed words"}]}))
        async with PublicHTTP(value) as public:
            rows = await BilibiliReader(public).read(lease(), ReadBudget(1048576, 4))
        evidence = rows[0]["reading"]
        assert evidence["video"] == {"bvid": BVID, "cid": 123, "part": 1, "duration_seconds": 3}
        assert evidence["duration_source"] == "view_first_part"
        assert evidence["subtitles"] == [{"start_seconds": 0, "end_seconds": 2,
                                           "text": "actual timed words", "partial": False}]
        assert evidence["level"] == "L2" and len(backend.requests) == 3
        assert all(b"playurl" not in request for request in backend.requests)

    run(scenario())
