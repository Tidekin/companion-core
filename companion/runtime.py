"""Shared durable state and identity bootstrap, independent of semantic services."""

from pathlib import Path

from companion.postgres import Database
from companion.privacy import ErasureLedger
from companion.store import Store, after, utcnow


class Runtime:
    def __init__(self, settings, clock=utcnow):
        self.settings, self.clock = settings, clock
        Path(settings.state_directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        # One persistent Runtime lease plus at most four finite API operations.
        self.database = Database(settings.database, max_connections=5).__enter__()
        self.store = None
        try:
            self.store = Store(self.database, settings.state_directory)
            self.store.bootstrap(settings.owner_qq_id, self.clock())
            self.ledger = ErasureLedger(settings.erasure_ledger)
            self.ledger.initialize(self.store)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        try:
            if self.store is not None:
                self.store.close()
        finally:
            self.database.close()

    def expire_raw_events(self, *, include_rem=False):
        """Return expired raw events, optionally counting expired REM records as well."""
        from companion import keepsakes
        from companion.rem import expire as expire_rem

        now = self.clock()
        cutoff = after(now, -86400 * self.settings.raw_event_retention_days)
        with self.store.transaction(control_write=True):
            rem_count = expire_rem(self.store, self.ledger, now)
            policy = keepsakes.grant(self.store, self.settings, self.clock)
            # Without a current grant, every former keepsake expires like any message.
            protected = ("" if policy is None else " AND " + keepsakes.protected_sql())
            rows = self.store.rows(
                'SELECT event_id FROM events WHERE redacted = 0 AND ingested_at < %s'
                + protected + ' ORDER BY record_sequence NULLS FIRST LIMIT 1000',
                (cutoff,) if policy is None else (cutoff, now),
            )
            if rows:
                ids = [e["event_id"] for e in rows]
                kept = keepsakes.plan(self.store, self.settings, policy, ids, cutoff, now)
                chosen = {entry["event_id"] for entry in kept}
                self.ledger.expire_events(self.store, [key for key in ids if key not in chosen],
                                          now, retention_cutoff=cutoff, keepsakes=kept)
            return len(rows) + (rem_count if include_rem else 0)

    def status(self):
        from companion.ops_status import runtime_snapshot

        return runtime_snapshot(self.store, self.settings, self.clock)
