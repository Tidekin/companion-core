import os
from contextlib import contextmanager
from pathlib import Path

from companion.contracts import CoreError


@contextmanager
def single_worker(state_directory, kind="worker"):
    """OS-held lock releases on process death; no stale PID-file recovery needed."""
    if kind not in {"worker", "diary-worker", "life-worker", "executive-worker", "content-worker"}:
        raise ValueError("unknown worker lock")
    directory = Path(state_directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise CoreError("WORKER_ALREADY_RUNNING", 503) from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
