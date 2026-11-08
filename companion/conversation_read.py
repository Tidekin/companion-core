"""Finite original rows for one synchronous conversation guard, never an authority verdict."""

from copy import deepcopy

from companion import retrieval_work


class ConversationRead:
    """Share only native rows in the Store-owned immutable snapshot that created us.

    The guard closes this object before returning to an async caller. Writers and
    unowned transactions never construct it. Every validator still runs, including
    current file custody, clock expiry, signatures and the final job lease check.
    """

    def __init__(self, store):
        self.store, self.scope = store, store.frozen_scope
        if self.scope is None:
            raise RuntimeError("CONVERSATION_READ_SNAPSHOT_REQUIRED")
        self._values, self._sources = {}, {}
        self._closed = False
        self.history = None

    def _check(self):
        if self._closed or self.store.frozen_scope != self.scope:
            raise RuntimeError("CONVERSATION_READ_SNAPSHOT_REQUIRED")

    def require(self, store):
        self._check()
        if store is not self.store:
            raise RuntimeError("CONVERSATION_READ_SNAPSHOT_REQUIRED")

    def one(self, statement, parameters=()):
        """Opt-in projections only: authority and lease rows, not volatile SQL or verdicts."""
        self._check()
        key = statement, tuple(parameters)
        if key not in self._values:
            if len(self._values) >= retrieval_work.SOURCE_NODES:
                return self.store.one(statement, parameters)
            self._values[key] = self.store.one(statement, parameters)
        return deepcopy(self._values[key])

    def sources(self, event_ids):
        from companion.owner_input import _load_sources

        self._check()
        keys = list(dict.fromkeys(event_ids))
        if len(self._sources.keys() | set(keys)) > retrieval_work.SOURCE_NODES:
            return _load_sources(self.store, keys)
        missing = [key for key in keys if key not in self._sources]
        if missing:
            self._sources.update(_load_sources(self.store, missing))
        return {key: deepcopy(self._sources[key]) for key in keys}

    def prepare(self, core, job, snapshots, view):
        """Batch only the current input and the history candidates the guard will consume."""
        from companion.dialogue_history import recent_context_sources

        self._check()
        self.history = None
        keys = [job["event_id"]]
        if ("history" in view.policy.purposes
            and any(item["kind"] == "history" and item["proof"] for item in snapshots)):
            limit = min(view.policy.history.max_turns, core.settings.max_recent_messages // 2)
            self.history = recent_context_sources(
                self.store, job["conversation_id"], job["event_id"], limit)
            keys.extend(item["event_id"] for item in self.history)
        self.sources(keys)

    def proof_sources(self, core, snapshots, now):
        """Nominate all required proof roots once, retaining each proof's original fallback."""
        from companion.memory_context import MemorySources

        self._check()
        sources = MemorySources(core.memory, now, bounded=True)
        sources._snapshot_cache()
        proofs = [item["proof"] for item in snapshots if item["kind"] == "memory"]
        rows = {row["memory_id"]: row for proof in proofs for row in proof["rows"]}
        roots = list(dict.fromkeys(key for proof in proofs for key in proof["event_ids"]))
        try:
            sources.prefetch_facts(list(rows.values()))
            sources.prefetch(list(rows))
            sources.lineages(roots)
        except retrieval_work.WorkLimit:
            return None  # Every required proof keeps its own existing finite allowance.
        return sources

    def close(self):
        self._closed = True
        self._values.clear()
        self._sources.clear()
        self.history = None
