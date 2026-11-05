"""In-process scan/results boundary around the original native worker operations."""

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime


class ClaimBusy(Exception):
    """A rolled-back native claim, before provider or delivery I/O began."""


@contextmanager
def claim_scope(store):
    """Classify BUSY only around an explicitly pre-I/O native claim/recovery scope."""
    try:
        yield
    except sqlite3.OperationalError as error:
        if (getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY
            or store.db.in_transaction):
            raise
        raise ClaimBusy from error


@dataclass(frozen=True)
class ScanContext:
    started_at: datetime
    data_version: int
    monotonic_started: float


@dataclass(frozen=True)
class UnitResult:
    """A native operation's result, not permission to claim, retry or dispatch.

    Recovery remains inside the native operation. A domain may report bounded
    recovery progress as progressed=True when another scan is needed. A fenced
    overdue row alone is not progress. next_deadline is an optional native hint;
    it never replaces the persisted deadline projection or an admission check.
    """

    progressed: bool
    next_deadline: datetime | None = None


async def run_native_unit(
    operation: Callable[..., Awaitable[bool | int | UnitResult]], *args,
) -> UnitResult:
    """Call exactly once; native recovery/claim/commit and errors stay with its owner.

    Existing services return a work flag or a completed batch count. Domains can
    adopt UnitResult without a second runner, state store or recovery policy.
    In particular, cancellation/UNKNOWN must not turn into an automatic retry.
    """
    result = await operation(*args)
    if isinstance(result, UnitResult):
        return result
    if type(result) not in (bool, int) or result < 0:
        raise TypeError("NATIVE_UNIT_RESULT_REQUIRED")
    return UnitResult(progressed=bool(result))
