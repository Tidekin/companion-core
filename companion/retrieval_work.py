"""Reply-local work accounting; nomination is never source or delivery authority."""

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Lock

from companion.contracts import CoreError

FACT_CANDIDATES = 96
TURN_CANDIDATES = 192
SOURCE_NODES = 512
EVIDENCE_ROOTS = 64
LIMITS = {"candidates": 256, "text_bytes": 1048576, "cold_vectors": 16,
          "query_vectors": 8, "searches": 8, "source_nodes": 2048, "sql_steps": 3000000,
          "rerank_pairs": 48, "vector_rows": 4096, "vector_bytes": 8388608,
          "attachment_rows": 128}
NOTE = "本轮检索覆盖有限，未命中不代表主人没有说过或没有这段记忆。"
_work = ContextVar("retrieval_work", default=None)


class WorkLimit(CoreError):
    def __init__(self, reason):
        super().__init__("MEMORY_RETRIEVAL_LIMIT", 409)
        self.reason = reason


class Work:
    def __init__(self):
        self.counts = dict.fromkeys(LIMITS, 0)
        self.limited, self.closed, self.lock = set(), False, Lock()

    def mark(self, reason):
        with self.lock:
            self.limited.add(reason)

    def remaining(self, name):
        with self.lock:
            return max(0, LIMITS[name] - self.counts[name]) if not self.closed else 0

    def take(self, name, count=1):
        with self.lock:
            if self.closed or self.counts[name] + count > LIMITS[name]:
                self.limited.add(name)
                return False
            self.counts[name] += count
            return True

    def receipt(self):
        with self.lock:
            return {"counts": dict(self.counts), "limits": dict(LIMITS),
                    "coverage_limited": bool(self.limited), "reasons": sorted(self.limited)}

    def rows(self, store, sql, params=(), *, limit):
        """Bound optional SQL on its own committed read snapshot, never a live writer.

        Results only nominate identities. The caller re-reads every selected row and
        source on its original Store. The progress handler belongs to this connection.
        """
        if store.db.in_transaction and not store.in_read_snapshot:
            self.mark("nomination_requires_committed_snapshot")
            raise WorkLimit("nomination_requires_committed_snapshot")
        if not self.take("sql_steps", 1000):
            raise WorkLimit("sql_steps")
        interrupted = False

        def progress():
            nonlocal interrupted
            interrupted = not self.take("sql_steps", 1000)
            return int(interrupted)

        try:
            db = sqlite3.connect(Path(store.path).resolve().as_uri() + "?mode=ro",
                                 uri=True, isolation_level=None, timeout=0.05)
            try:
                try:
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA query_only=ON")
                    db.set_progress_handler(progress, 1000)
                    cursor = db.execute(sql, params)
                    try:
                        return [dict(row) for row in cursor.fetchmany(limit)]
                    finally:
                        cursor.close()
                finally:
                    db.set_progress_handler(None, 0)
            finally:
                db.close()
        except sqlite3.OperationalError as error:
            if interrupted:
                raise WorkLimit("sql_steps") from error
            if getattr(error, "sqlite_errorcode", 0) & 0xff == sqlite3.SQLITE_BUSY:
                self.mark("sql_busy")
                raise WorkLimit("sql_busy") from error
            raise


def current():
    return _work.get()


@contextmanager
def slot(lock, reason):
    """A foreground retrieval does not queue behind CPU work still finishing elsewhere."""
    work = current()
    if not lock.acquire(blocking=work is None):
        work.mark(reason)
        raise WorkLimit(reason)
    try:
        if work is not None and work.closed:
            raise WorkLimit("closed")
        yield
    finally:
        lock.release()


@contextmanager
def scope():
    existing = current()
    if existing is not None:
        yield existing
        return
    work = Work()
    token = _work.set(work)
    try:
        yield work
    finally:
        with work.lock:
            work.closed = True
        _work.reset(token)
