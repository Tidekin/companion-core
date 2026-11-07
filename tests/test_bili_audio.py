import copy
import hashlib
import json

import pytest

from source_reader.bili_audio import download_song, representation
from source_reader.bili_media import media_locator
from source_reader.network import PublicHTTP, ReadFailure
from tests.conftest import run
from tests.support.bili_media import BVID, NAV, NOW, VIEW, allowed, pool
from tests.support.source_reader import response

CDN = "https://song.mcdn.bilivideo.cn/audio.m4s?token=transient"
PLAY = {"code": 0, "data": {"timelength": 3000, "dash": {"audio": [{
    "base_url": CDN, "mime_type": "audio/mp4", "codecs": "mp4a.40.2", "bandwidth": 64000,
}]}}}


async def download(tmp_path, *, view=None, nav=None, play=None, guard=allowed, status=200):
    backend_pool, backend = pool(response(view or VIEW), response(nav or NAV),
        response(play or PLAY), response(b"native audio", status=status))
    async with PublicHTTP(backend_pool) as public:
        result = await download_song(public, BVID, 1, tmp_path / "source.m4a",
                                     guard=guard, now=NOW)
    return result, backend


@pytest.mark.parametrize("anonymous", [False, True])
def test_audio_download_pins_transport_and_exact_part_without_cookies(tmp_path, anonymous):
    view = copy.deepcopy(VIEW)
    view["data"]["pages"].append({"page": 2, "cid": 456, "duration": 3})
    nav = copy.deepcopy(NAV)
    if anonymous:
        nav.update(code=-101)
        nav["data"]["isLogin"] = False
    result, backend = run(download(tmp_path, view=view, nav=nav))
    assert result.identity == {"bvid": BVID, "cid": 123, "part": 1, "duration_seconds": 3.0}
    assert result.path.read_bytes() == b"native audio"
    assert result.sha256 == hashlib.sha256(b"native audio").hexdigest()
    assert result.proof["used_requests"] == 4
    assert backend.hosts == ["93.184.216.34"] * 4
    assert b"Cookie:" not in b"".join(backend.requests)
    assert CDN not in json.dumps(result.proof) and "transient" not in json.dumps(result.proof)


def test_second_part_keeps_its_own_cid(tmp_path):
    view = copy.deepcopy(VIEW)
    view["data"]["pages"].append({"page": 2, "cid": 456, "duration": 3})

    async def scenario():
        value, backend = pool(response(view), response(NAV), response(PLAY),
                              response(b"part two"))
        async with PublicHTTP(value) as public:
            result = await download_song(public, BVID, 2, tmp_path / "second.m4a",
                                         guard=allowed, now=NOW)
        assert result.identity["cid"] == 456 and result.identity["part"] == 2
        assert b"cid=456" in b"".join(backend.requests)
    run(scenario())


def test_audio_vendor_suffix_does_not_widen_visual_contract():
    with pytest.raises(ReadFailure, match="MEDIA_LOCATOR_NOT_ALLOWED"):
        media_locator(CDN)
    assert media_locator(CDN, audio=True) == CDN


@pytest.mark.parametrize("change", ["private_host", "http", "codec", "size", "nav_denied",
                                    "wrong_cid", "wrong_duration", "denied"])
def test_invalid_identity_or_access_does_not_download_or_retry(tmp_path, change):
    nav, play = copy.deepcopy(NAV), copy.deepcopy(PLAY)
    row = play["data"]["dash"]["audio"][0]
    if change == "private_host":
        row["base_url"] = "https://127.0.0.1/private.m4a"
    elif change == "http":
        row["base_url"] = CDN.replace("https:", "http:")
    elif change == "codec":
        row["codecs"] = "unknown-codec"
    elif change == "size":
        row["bandwidth"] = 2**40
    elif change == "nav_denied":
        nav["code"] = -412
    elif change == "wrong_cid":
        play["data"]["cid"] = 999
    elif change == "wrong_duration":
        play["data"]["timelength"] = 9000
    status = 403 if change == "denied" else 200
    with pytest.raises(ReadFailure):
        run(download(tmp_path, nav=nav, play=play, status=status))
    target = tmp_path / "source.m4a"
    assert not target.exists() or target.stat().st_size == 0


def test_stop_before_external_io_is_respected(tmp_path):
    async def stopped():
        raise ReadFailure("SINGING_STOPPED")
    with pytest.raises(ReadFailure, match="SINGING_STOPPED"):
        run(download(tmp_path, guard=stopped))
    assert not (tmp_path / "source.m4a").exists()


def test_audio_selection_uses_bounded_highest_quality():
    data = copy.deepcopy(PLAY["data"])
    data["dash"]["audio"].append({**data["dash"]["audio"][0], "bandwidth": 192000})
    row, locator = representation(data)
    assert row["bandwidth"] == 192000 and locator == CDN


@pytest.mark.parametrize("dash", [None, [], "untrusted"])
def test_malformed_upstream_representation_fails_with_a_bounded_reader_error(dash):
    with pytest.raises(ReadFailure, match="SINGING_AUDIO_UNAVAILABLE"):
        representation({"dash": dash, "timelength": 3000})


def test_api_declared_https_locator_is_selected_before_any_io(tmp_path):
    play = copy.deepcopy(PLAY)
    row = play["data"]["dash"]["audio"][0]
    row.update(base_url="https://song.mcdn.bilivideo.cn:8082/peer.m4s",
               backup_url=[CDN])
    result, backend = run(download(tmp_path, play=play))
    assert result.proof["used_requests"] == 4
    assert result.proof["locator_sha256"] == hashlib.sha256(CDN.encode()).hexdigest()
    assert b":8082" not in b"".join(backend.requests)
