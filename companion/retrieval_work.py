"""Reply-local work accounting; nomination is never source or delivery authority."""

import math
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import monotonic

import psycopg

from companion.contracts import CoreError

FACT_CANDIDATES = 96
TURN_CANDIDATES = 192
SOURCE_NODES = 512
EVIDENCE_ROOTS = 64
LIMITS = {"candidates": 256, "text_bytes": 1048576, "cold_vectors": 16,
          "query_vectors": 8, "searches": 8, "source_nodes": 2048, "sql_milliseconds": 1000,
          "rerank_pairs": 48, "vector_rows": 4096, "vector_bytes": 8388608,
          "attachment_rows": 128}
NOTE = "本轮检索覆盖有限，未命中不代表主人没有说过或没有这段记忆。"
_work = ContextVar("retrieval_work", default=None)
_stages = ContextVar("retrieval_stages", default=())


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
            if reason in LIMITS:
                self.limited.update("stage:" + label + ":" + reason
                    for work, label, _ in _stages.get() if work is self)

    def _ceiling(self, name):
        return min([LIMITS[name], *(ceiling.get(name, LIMITS[name])
            for work, _, ceiling in _stages.get() if work is self)])

    @contextmanager
    def stage(self, name, *, reserve):
        """Leave shared reply allowance for later stages; never create a new budget."""
        ceiling = {key: max(0, LIMITS[key] - value) for key, value in reserve.items()}
        token = _stages.set((*_stages.get(), (self, name, ceiling)))
        try:
            yield
        finally:
            _stages.reset(token)

    def exhausted(self):
        """Hard exhaustion only: a soft phase boundary still permits later fallback."""
        with self.lock:
            return self.closed or any(self.counts[key] >= LIMITS[key] for key in (
                "sql_milliseconds", "text_bytes", "source_nodes", "candidates", "searches"))

    def remaining(self, name):
        with self.lock:
            return max(0, self._ceiling(name) - self.counts[name]) if not self.closed else 0

    def take(self, name, count=1):
        with self.lock:
            if count < 0:
                raise ValueError("negative retrieval work")
            if self.closed or self.counts[name] + count > self._ceiling(name):
                self.limited.add(name)
                self.limited.update("stage:" + label + ":" + name
                    for work, label, _ in _stages.get() if work is self)
                return False
            self.counts[name] += count
            return True

    def receipt(self):
        with self.lock:
            return {"counts": dict(self.counts), "limits": dict(LIMITS),
                    "coverage_limited": bool(self.limited), "reasons": sorted(self.limited)}

    def rows(self, store, sql, params=(), *, limit):
        """Bound optional native reads; the caller still validates every nominated source."""
        if store.in_transaction and not store.in_read_snapshot:
            self.mark("nomination_requires_committed_snapshot")
            raise WorkLimit("nomination_requires_committed_snapshot")
        return self._read(store.bounded_read, sql, params, limit=limit)

    def source_rows(self, store, sql, params=(), *, limit):
        """Meter complete provenance reads, including required checks inside a writer."""
        return self._read(store.bounded_source_read, sql, params, limit=limit)

    def one(self, store, sql, params=()):
        rows = self.source_rows(store, sql, params, limit=1)
        return rows[0] if rows else None

    def _read(self, operation, sql, params, *, limit):
        remaining = self.remaining("sql_milliseconds")
        if not remaining:
            self.mark("sql_milliseconds")
            raise WorkLimit("sql_milliseconds")
        started = monotonic()
        try:
            return operation(sql, params, limit=limit, timeout_ms=remaining)
        except psycopg.errors.QueryCanceled as error:
            self.mark("sql_milliseconds")
            raise WorkLimit("sql_milliseconds") from error
        except psycopg.errors.LockNotAvailable as error:
            self.mark("sql_busy")
            raise WorkLimit("sql_busy") from error
        finally:
            self.take("sql_milliseconds", min(remaining, max(1, math.ceil(
                (monotonic() - started) * 1000))))


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
