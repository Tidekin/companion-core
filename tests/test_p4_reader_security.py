import copy
import io
import json
import os
from pathlib import Path

import pytest

from source_reader.accounts import account_grant
from source_reader.heybox import HeyboxReader
from source_reader.heybox_source import NativeHeyboxSource
from source_reader.network import ReadFailure
from source_reader.worker import read_result
from tests.conftest import run
from tests.support.heybox_reader import ITEM as HEYBOX_ITEM
from tests.support.heybox_reader import POST, mock_pool
from tests.support.source_reader import ITEM, META, SUBTITLE, read, response
from tests.test_plugin_readonly import NOW_DT, grant


@pytest.mark.parametrize("changed", ["none", "bvid", "aid", "cid"])
def test_fallback_page_cannot_replace_requested_video_identity(changed):
    meta = copy.deepcopy(META)
    requested = ITEM["url"].rsplit("/", 1)[1]
    meta["data"].update(bvid=requested, aid=77, cid=1234, desc="")
    rendered = {**meta["data"], "desc": "Fallback description"}
    if changed != "none":
        rendered[changed] = {"bvid": "BV2xx411c7mD", "aid": 88, "cid": 5678}[changed]
    page = (
        "<script>window.__INITIAL_STATE__=" + json.dumps({"videoData": rendered}) + "</script>"
    ).encode()
    caption = (
        "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/"
        + str(rendered["aid"])
        + str(rendered["cid"])
        + "a" * 32
    )
    player = {
        "code": 0,
        "data": {
            **{key: rendered[key] for key in ("bvid", "aid", "cid")},
            "subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": caption}]},
        },
    }
    result, backend = run(
        read(response(meta), response(page), response(player), response(SUBTITLE))
    )
    if changed == "none":
        assert result["status"] == "SUCCESS"
        assert result["items"][0]["reading"]["level"] == "L2"
        assert len(backend.requests) == 4
    else:
        assert result["status"] == "FAILED"
        assert result["error_code"] == "READER_TARGET_MISMATCH"
        assert not result["items"] and len(backend.requests) == 2


def test_grant_replaced_between_validation_and_open_is_rejected(tmp_path, monkeypatch):
    path = grant(tmp_path)
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    replacement = grant(replacement_dir, account_id="456")
    original_io_open, original_os_open = io.open, os.open
    swapped = False

    def swap(file):
        nonlocal swapped
        if not swapped and isinstance(file, (str, os.PathLike)) and Path(file) == path:
            os.replace(replacement, path)
            swapped = True

    def io_open(file, *args, **kwargs):
        swap(file)
        return original_io_open(file, *args, **kwargs)

    def os_open(file, *args, **kwargs):
        swap(file)
        return original_os_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", io_open)
    monkeypatch.setattr(os, "open", os_open)
    with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
        account_grant(path, NOW_DT)
    assert swapped


def test_grant_revoked_during_file_read_is_rejected(tmp_path, monkeypatch):
    path = grant(tmp_path)
    revoked = json.loads(path.read_bytes())
    revoked["enabled"] = False
    original_open = io.open

    class RevokeAfterRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            raw = self.stream.read(size)
            with original_open(path, "wb") as writer:
                writer.write(json.dumps(revoked).encode())
            return raw

    def open_and_revoke(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        return RevokeAfterRead(stream) if mode == "rb" else stream

    monkeypatch.setattr(io, "open", open_and_revoke)
    with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
        account_grant(path, NOW_DT)


def test_grant_symlink_is_rejected(tmp_path):
    target = grant(tmp_path)
    link = tmp_path / "grant-link.json"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable: {error}")
    with pytest.raises(ReadFailure, match="OWNER_ACCOUNT_AUTHORIZATION_REQUIRED"):
        account_grant(link, NOW_DT)


@pytest.mark.parametrize("source", ["heybox", "bilibili"])
def test_valid_grant_accepts_distinct_creation_and_modification_times(tmp_path, source):
    path = grant(tmp_path, source)
    os.utime(path, (NOW_DT.timestamp(), NOW_DT.timestamp()))
    data, _ = account_grant(path, NOW_DT, source)
    assert data["enabled"] is True


def test_local_budget_limit_does_not_latch_native_account(tmp_path):
    async def scenario():
        pool, backend = mock_pool(response(POST), response(POST))
        async with NativeHeyboxSource(grant(tmp_path), pool, clock=lambda: NOW_DT) as source:
            reader = HeyboxReader(None, source)
            first = await read_result(reader, {**HEYBOX_ITEM, "max_bytes": 1})
            second = await read_result(reader, HEYBOX_ITEM)
        assert first["status"] == "FAILED" and first["error_code"] == "READER_BYTE_LIMIT"
        assert second["status"] == "SUCCESS"
        assert len(backend.requests) == 2

    run(scenario())


def test_native_discovery_rotates_operator_topics_with_one_get_each(tmp_path):
    feed = {"status": "ok", "result": {"links": [
        {"linkid": 168860244, "title": "分区帖子"}, {"linkid": 168860244, "title": "重复"}]}}
    item = {**HEYBOX_ITEM, "operation": "discover"}

    async def scenario():
        pool, backend = mock_pool(response(feed), response(feed), response(feed))
        async with NativeHeyboxSource(grant(tmp_path), pool, clock=lambda: NOW_DT,
                                      topics=["100", " 200", "100", ""]) as source:
            reader = HeyboxReader(None, source)
            results = [await read_result(reader, item) for _ in range(3)]
        assert [result["items"] for result in results] == [[{
            "url": "https://www.xiaoheihe.cn/app/bbs/link/168860244", "title": "分区帖子"}]] * 3
        paths = [request.split(b" ")[1] for request in backend.requests]
        assert all(path.startswith(b"/bbs/app/topic/feeds?") for path in paths)
        assert [b"topic_id=100&" in paths[0], b"topic_id=200&" in paths[1],
                b"topic_id=100&" in paths[2]] == [True, True, True]
        assert all(b"limit=4&" in path and b"hkey=" in path for path in paths)

    run(scenario())


def test_native_discovery_without_topics_keeps_the_main_feed(tmp_path):
    feed = {"status": "ok", "result": {"links": []}}

    async def scenario():
        pool, backend = mock_pool(response(feed))
        async with NativeHeyboxSource(grant(tmp_path), pool, clock=lambda: NOW_DT) as source:
            await read_result(HeyboxReader(None, source), {**HEYBOX_ITEM, "operation": "discover"})
        assert backend.requests[0].split(b" ")[1].startswith(b"/bbs/app/feeds?")

    run(scenario())


@pytest.mark.parametrize("topics", [["abc"], ["1" * 33], ["12-3"], [str(i) for i in range(21)]])
def test_native_topics_reject_invalid_or_excess_selection(topics):
    with pytest.raises(ValueError, match="topic"):
        NativeHeyboxSource(topics=topics)
