"""The Bridge's original fsynced hash-chain codec, shared by custody versions."""

import hashlib
import marshal
import os
import stat
from functools import lru_cache

from .event_normalizer import decode
from .spool import encode

# One entry retains at most 16 MiB of input and 16 MiB of serialized records.
# The immutable in-process view follows Core's paid-journal cache: each reader gets
# fresh objects. Larger files reuse a bounded LF-terminated prefix and validate
# the remaining lines, rather than dropping all codec reuse at the size boundary.
# Every read checks the actual bytes; no file metadata or authority is cached.
CACHE_MAX_BYTES = 16 * 1048576


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def stop_marker(path):
    """Publish the original durable OWNER_STOP marker without taking the process lock."""
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("PRIVATE_DATA_DIRECTORY_REQUIRED")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                              | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != b"OWNER_STOP\n":
            raise ValueError("INVALID_WINDOW_STOP_REQUEST") from None
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(b"OWNER_STOP\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read(path, *, maximum=32 * 1048576):
    return _read(path, maximum=maximum, count_only=False)


def read_count(path, *, maximum=32 * 1048576):
    """Revalidate the complete journal without materializing its decoded prefix."""
    return _read(path, maximum=maximum, count_only=True)


def _read(path, *, maximum, count_only):
    try:
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > maximum
                or (os.name != "nt" and info.st_mode & 0o077)):
            raise ValueError("INVALID_WINDOW_JOURNAL")
        raw = path.read_bytes()
        if len(raw) > maximum:
            raise ValueError("INVALID_WINDOW_JOURNAL")
        end = len(raw)
        if end > CACHE_MAX_BYTES:
            end = raw.rfind(b"\n", 0, CACHE_MAX_BYTES) + 1
            if not end:
                _clear()
                records, head = _decode_lines(raw.decode("utf-8").splitlines())
                return (len(records) if count_only else records), head
        # Keep the exact validated prefix across small appends. Rebuilding its
        # serialized records on each RESERVED, IO_STARTED and ACK is expensive.
        prefix = raw[:end]
        if _previous is not None:
            last = _previous[0]
            # Bound repeated tail decoding on both small and large journals.
            if (0 < end - len(last) <= min(8192, len(last) // 64)
                    and last.endswith(b"\n") and prefix.startswith(last)):
                prefix = last
        end = len(prefix)
        payload, head, count = _validated_bytes(prefix)
        if len(payload) > CACHE_MAX_BYTES:
            _clear()
        tail = []
        if end < len(raw):
            tail, head = _decode_lines(raw[end:].decode("utf-8").splitlines(),
                                       head=head, count=count)
        if count_only:
            return count + len(tail), head
        records = marshal.loads(payload)
        records.extend(tail)
        return records, head
    except Exception:
        _clear()
        raise


# The last validated bytes, payload, head and record count. An append leaves those
# bytes as an exact prefix, so only the new lines are decoded and chained.
_previous = None


def _clear():
    global _previous
    _previous = None
    _validated_bytes.cache_clear()


@lru_cache(maxsize=1)
def _validated_bytes(raw):
    global _previous
    previous, _previous = _previous, None
    # A prefix ending in LF ends its last line there in every splitlines() reading,
    # so the whole file's lines are the prefix's lines followed by the tail's.
    if (previous is not None and previous[0].endswith(b"\n")
            and len(raw) > len(previous[0]) and raw.startswith(previous[0])):
        _, old_payload, old_head, count = previous
        lines = raw[len(previous[0]):].decode("utf-8").splitlines()
        tail, head = _decode_lines(lines, head=old_head, count=count)
        records = marshal.loads(old_payload)
        records.extend(tail)
        count += len(tail)
    else:
        lines = raw.decode("utf-8").splitlines()
        records, head = _decode_lines(lines)
        count = len(records)
    # This is process-local serialization of already decoded JSON, never a file format
    # or an untrusted marshal input. A caller cannot mutate the cached records.
    payload = marshal.dumps(records)
    _previous = (raw, payload, head, count)
    return payload, head, count


def _decode_lines(lines, *, head="0" * 64, count=0):
    records = []
    for line in lines:
        record = decode(line, 65536)
        if record.get("previous") != head or record.get("seq") != count + len(records) + 1:
            raise ValueError("INVALID_WINDOW_JOURNAL")
        head = digest(record)
        records.append(record)
    return records, head


def append(path, kind, data, *, at, expected_head, create=False, maximum=32 * 1048576):
    count, previous = ((0, "0" * 64) if create else
                       _read(path, maximum=maximum, count_only=True))
    if previous != expected_head:
        raise ValueError("WINDOW_JOURNAL_RECONCILIATION_REQUIRED")
    record = {"seq": count + 1, "previous": previous, "at": at,
              "kind": kind, "data": data}
    flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if create else os.O_APPEND)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write((encode(record) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())
    if create and os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return digest(record)
