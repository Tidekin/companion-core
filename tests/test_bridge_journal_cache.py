"""Exact-byte codec reuse must never reuse mutable state or a permission decision."""

import marshal
import os
import stat
from types import SimpleNamespace

import pytest

from companion_onebot_bridge import journal
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_events import raw_event
from tests.test_bridge_owner_runtime import config as config


@pytest.fixture(autouse=True)
def empty_cache():
    journal._validated_bytes.cache_clear()
    yield
    journal._validated_bytes.cache_clear()


def chain(*data):
    rows, head = [], "0" * 64
    for value in data:
        row = {"seq": len(rows) + 1, "previous": head, "at": "2026-10-26T12:00:00+00:00",
               "kind": "SYNTHETIC", "data": value}
        rows.append(row)
        head = journal.digest(row)
    return ("".join(encode(row) + "\n" for row in rows)).encode("utf-8"), rows, head


def write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def test_every_read_loads_current_bytes_but_reuses_only_successful_codec_work(
    tmp_path, monkeypatch,
):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain({"nested": {"values": ["茶", {"number": 1.25}]}})
    write(path, raw)
    original_read, original_decode = type(path).read_bytes, journal.decode
    reads, decodes = [], []

    def read_bytes(self):
        reads.append(self)
        return original_read(self)

    def decode(*args, **kwargs):
        decodes.append(True)
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(type(path), "read_bytes", read_bytes)
    monkeypatch.setattr(journal, "decode", decode)
    first, first_head = journal.read(path)
    first[0]["data"]["nested"]["values"][1]["number"] = 999
    first.append({"changed": True})
    assert journal.read(path) == (rows, head) and first_head == head
    assert reads == [path, path] and len(decodes) == 1
    info = journal._validated_bytes.cache_info()
    assert info.maxsize == info.currsize == info.hits == info.misses == 1
    payload, _, count = journal._validated_bytes(raw)
    assert count == len(rows)
    assert isinstance(payload, bytes) and len(payload) <= journal.CACHE_MAX_BYTES
    assert marshal.loads(payload) == rows


def test_same_length_rewrite_with_preserved_mtime_uses_new_complete_bytes(tmp_path):
    path = tmp_path / "journal.jsonl"
    first, _, first_head = chain({"value": "old"}, {})
    second, rows, head = chain({"value": "new"}, {})
    assert len(first) == len(second) and first_head != head
    write(path, first)
    stamp = path.stat()
    journal.read(path)
    path.write_bytes(second)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert journal.read(path) == (rows, head)
    assert journal._validated_bytes.cache_info().currsize == 1


def test_append_and_valid_prefix_truncation_never_return_old_records(tmp_path):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain({"value": "old"})
    write(path, raw)
    assert journal.read(path) == (rows, head)
    new_head = journal.append(path, "NEXT", {"new": True}, at="later", expected_head=head)
    extended, found = journal.read(path)
    assert len(extended) == 2 and extended[0] == rows[0] and found == new_head != head
    path.write_bytes(raw)
    assert journal.read(path) == (rows, head)


def test_small_appends_do_not_materialize_or_reserialize_a_large_verified_prefix(
    tmp_path, monkeypatch,
):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain(*({"text": "x" * 40000} for _ in range(4)))
    write(path, raw)
    assert journal.read(path) == (rows, head)

    def unexpected(*args):
        pytest.fail("append rebuilt the verified prefix")

    with monkeypatch.context() as patch:
        patch.setattr(marshal, "loads", unexpected)
        patch.setattr(marshal, "dumps", unexpected)
        for i in range(3):
            head = journal.append(path, "NEXT", {"part": i}, at="later", expected_head=head)
        changed = path.read_bytes().replace(b'"part":0', b'"part":9')
        path.write_bytes(changed)
        with pytest.raises(ValueError, match="INVALID_WINDOW_JOURNAL"):
            journal.append(path, "NEXT", {}, at="later", expected_head=head)
        assert path.read_bytes() == changed


@pytest.mark.parametrize("damage", ["body", "previous", "seq", "duplicate", "truncated", "utf8"])
def test_corruption_after_a_warm_read_is_rejected_and_clears_cache(tmp_path, damage):
    path = tmp_path / "journal.jsonl"
    raw, _, _ = chain({"value": "old"}, {})
    write(path, raw)
    journal.read(path)
    if damage == "body":
        changed = raw.replace(b'"old"', b'"new"')
        assert len(changed) == len(raw)
    elif damage == "previous":
        changed = raw.replace(b'"previous":"0', b'"previous":"9', 1)
    elif damage == "seq":
        changed = raw.replace(b'"seq":2', b'"seq":9')
    elif damage == "duplicate":
        changed = raw.replace(b'"seq":1', b'"seq":1,"seq":1', 1)
    elif damage == "truncated":
        changed = raw[:-5]
    else:
        changed = raw + b"\xff"
    path.write_bytes(changed)
    with pytest.raises(ValueError):
        journal.read(path)
    assert journal._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("damage", ["permissions", "type", "symlink", "reported_size", "maximum"])
def test_fresh_metadata_and_requested_limit_are_checked_before_cache_reuse(
    tmp_path, monkeypatch, damage,
):
    path = tmp_path / "journal.jsonl"
    raw, _, _ = chain({})
    write(path, raw)
    journal.read(path)
    info = path.lstat()
    limit = len(raw) - 1 if damage == "maximum" else 32 * 1048576
    mode = stat.S_IFREG | (0o644 if damage == "permissions" else 0o600)
    if damage == "type":
        mode = stat.S_IFDIR | 0o700
    if damage == "symlink":
        mode = stat.S_IFLNK | 0o600
    size = limit + 1 if damage == "reported_size" else info.st_size
    original = type(path).lstat

    def lstat(self):
        return SimpleNamespace(st_mode=mode, st_size=size) if self == path else original(self)

    monkeypatch.setattr(type(path), "lstat", lstat)
    # Simulate the existing POSIX permission rule on Windows without changing os globally.
    monkeypatch.setattr(journal, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="INVALID_WINDOW_JOURNAL"):
        journal.read(path, maximum=limit)
    assert journal._validated_bytes.cache_info().currsize == 0


def test_growth_after_lstat_cannot_bypass_maximum_and_evicts_warm_cache(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    raw, _, _ = chain({})
    write(path, raw)
    journal.read(path)
    monkeypatch.setattr(type(path), "read_bytes", lambda self: raw + b" " * 10)
    with pytest.raises(ValueError, match="INVALID_WINDOW_JOURNAL"):
        journal.read(path, maximum=len(raw))
    assert journal._validated_bytes.cache_info().currsize == 0


def test_large_journal_reuses_a_bounded_prefix_and_validates_the_tail(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    small, _, _ = chain({"value": "small"})
    write(path, small)
    journal.read(path)
    large, rows, head = chain(*({"text": "x" * 60000} for _ in range(290)))
    assert journal.CACHE_MAX_BYTES == 16 * 1048576 < len(large) < 32 * 1048576
    path.write_bytes(large)
    decodes = []
    native = journal._decode_lines

    def decode(lines, **kwargs):
        decodes.append(len(lines))
        return native(lines, **kwargs)

    monkeypatch.setattr(journal, "_decode_lines", decode)
    for _ in range(2):
        assert journal.read(path) == (rows, head)
        assert journal._validated_bytes.cache_info().currsize == 1
        raw, payload, _, _ = journal._previous
        assert len(raw) <= journal.CACHE_MAX_BYTES and len(payload) <= journal.CACHE_MAX_BYTES
    assert decodes[0] > 100
    assert 0 < decodes[-1] < 20


@pytest.mark.parametrize("side", ["prefix", "tail"])
def test_bounded_prefix_never_hides_tampering_on_either_side(tmp_path, monkeypatch, side):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain(*({"text": "x" * 1000} for _ in range(8)))
    monkeypatch.setattr(journal, "CACHE_MAX_BYTES", len(raw) // 2)
    write(path, raw)
    assert journal.read(path) == (rows, head)
    changed = raw.splitlines(keepends=True)
    index = 0 if side == "prefix" else -2
    changed[index] = changed[index].replace(b"xxx", b"yyy", 1)
    stamp = path.stat()
    path.write_bytes(b"".join(changed))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match="INVALID_WINDOW_JOURNAL"):
        journal.read(path)
    assert journal._validated_bytes.cache_info().currsize == 0


def test_marshaled_view_preserves_nested_caller_ownership(tmp_path):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain({"nested": [{"value": [1, 2, 3]}]})
    write(path, raw)
    found, _ = journal.read(path)
    found[0]["data"]["nested"][0]["value"].append("caller-owned")
    assert journal.read(path) == (rows, head)


def test_successful_different_content_evicts_previous_entry_and_read_failure_clears_it(tmp_path):
    path = tmp_path / "journal.jsonl"
    first, _, _ = chain({"value": "first"})
    second, _, _ = chain({"value": "second"})
    write(path, first)
    journal.read(path)
    path.write_bytes(second)
    journal.read(path)
    path.write_bytes(first)
    journal.read(path)
    info = journal._validated_bytes.cache_info()
    assert info.maxsize == info.currsize == 1 and info.misses == 3 and info.hits == 0
    path.unlink()
    with pytest.raises(FileNotFoundError):
        journal.read(path)
    assert journal._validated_bytes.cache_info().currsize == 0


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n", b"\r"])
def test_original_unicode_line_boundaries_and_empty_journal_are_preserved(tmp_path, line_ending):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain({"text": "茶"}, {})
    write(path, raw.replace(b"\n", line_ending))
    for _ in range(2):
        assert journal.read(path) == (rows, head)
    path.write_bytes(b"")
    assert journal.read(path) == ([], "0" * 64)


@pytest.mark.parametrize("damage", ["db_state", "db_head", "stop", "rewrite", "append", "truncate"])
def test_warm_codec_never_caches_runtime_authority_or_projection(config, damage):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        with runtime.db:
            runtime.append("PROFILE_PROBE_STARTED", {"authority": runtime.authority})
        runtime.verify()
        raw = runtime.path.read_bytes()
        if damage == "db_state":
            with runtime.db:
                runtime.db.execute("UPDATE owner_runtime SET state='STOPPED' WHERE id=1")
        elif damage == "db_head":
            with runtime.db:
                runtime.db.execute("UPDATE owner_runtime SET journal_head=? WHERE id=1",
                                   ("f" * 64,))
        elif damage == "stop":
            runtime.stop_path.write_bytes(b"OWNER_STOP\n")
        elif damage == "rewrite":
            changed = raw.replace(b'"policy_sha256":"' + b"d" * 64,
                                  b'"policy_sha256":"' + b"e" * 64)
            assert len(changed) == len(raw) and changed != raw
            runtime.path.write_bytes(changed)
        elif damage == "append":
            journal.append(runtime.path, "PROFILE_PROBE_STARTED", {"authority": runtime.authority},
                           at=runtime.clock().isoformat(),
                           expected_head=runtime.row()["journal_head"])
        else:
            runtime.path.write_bytes(raw.splitlines(keepends=True)[0])
        with pytest.raises(ValueError):
            runtime.verify()


def test_unchanged_cached_journal_still_checks_the_current_captured_source(config):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        now = runtime.clock()
        raw = encode(raw_event(text="original", time=int(now.timestamp()), self_id=67890,
                               user_id=12345, sender={"user_id": 12345}))
        event = normalize(raw, config, now=now.timestamp())
        runtime.persist_capture(event, raw, received_at=now, peer=("127.0.0.1", 1))
        runtime.verify()
        runtime.verify()
        before = journal._validated_bytes.cache_info()
        event["payload"]["text"] = "changed"
        with spool.db:
            spool.db.execute("UPDATE inbox SET body=?", (encode(event),))
        with pytest.raises(ValueError, match="OWNER_CAPTURE_PROJECTION_CHANGED"):
            runtime.verify()
        assert journal._validated_bytes.cache_info().hits == before.hits + 1


def test_journal_past_the_former_one_mebibyte_bound_stays_cached(tmp_path):
    path = tmp_path / "journal.jsonl"
    raw, rows, head = chain(*({"text": "x" * 40000} for _ in range(28)))
    assert 1048576 < len(raw) < journal.CACHE_MAX_BYTES
    write(path, raw)
    for _ in range(2):
        assert journal.read(path) == (rows, head)
    info = journal._validated_bytes.cache_info()
    assert info.currsize == 1 and info.hits == 1
