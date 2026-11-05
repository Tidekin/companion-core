"""One in-process turn at a time over native queues; no scheduler persistence."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from companion import budget, owner_authority, social_autonomy_runtime
from companion.contracts import CoreError
from companion.store import encode
from companion.wake import WAKE_PURPOSES
from companion.worker_schedule import UnitResult, run_native_unit
from companion.worker_wakeup import MAINTENANCE_SECONDS

log = logging.getLogger(__name__)
# A failing lane backs off exponentially so it cannot starve later lanes or spin.
FAILURE_BACKOFF_SECONDS = 5
FAILURE_BACKOFF_MAX_SECONDS = 15 * 60


@dataclass
class Lane:
    name: str
    run: Callable[[], Awaitable[UnitResult]]
    interval: float | None = None
    next_at: float = 0
    failures: int = 0
    retry_at: float = 0
    # Local bookkeeping with no model or network I/O; it keeps running while
    # foreground chat has precedence, as it did before the selector.
    maintenance: bool = False


@dataclass(frozen=True)
class Selection:
    lane: str | None
    result: UnitResult
    more: bool
    blocked: bool = False


class RoundRobin:
    """A finite idle sweep; every invoked native operation consumes a turn.

    A failed/fenced operation can have already used its one model attempt, so
    never call another operation within that turn. Only genuine progress starts
    a fresh sweep. Eligibility is checked again before every invocation.
    """

    def __init__(self, lanes: tuple[Lane, ...], can_start: Callable[[], bool]):
        self.lanes, self.can_start = lanes, can_start
        self.cursor = 0
        self.remaining = len(lanes)

    def resume(self):
        self.remaining = len(self.lanes)

    @property
    def next_maintenance(self):
        # A backed-off maintenance lane is not due before its retry, so an overdue
        # next_at cannot turn the idle wait into a spin.
        return self._next_due(lambda lane: lane.interval is not None)

    @property
    def next_blocked_maintenance(self):
        """The only wake-up a foreground-blocked selector needs: local bookkeeping."""
        return self._next_due(lambda lane: lane.maintenance)

    def _next_due(self, selected):
        # A backed-off lane is not due before its retry, so an overdue next_at
        # cannot turn the idle wait into a spin.
        return min((max(lane.next_at, lane.retry_at) for lane in self.lanes
                    if lane.interval is not None and selected(lane)), default=None)

    @staticmethod
    def _due(lane):
        now = monotonic()
        return not ((lane.interval is not None and now < lane.next_at) or now < lane.retry_at)

    async def step(self) -> Selection:
        if not self.can_start():
            self.remaining = 0
            # Foreground precedence pauses model and network work only. Due local
            # bookkeeping (retention, diary scheduling, commitment expiry) still runs.
            lane = next((lane for lane in self.lanes if lane.maintenance and self._due(lane)),
                        None)
            if lane is None:
                return Selection(None, UnitResult(False), False, blocked=True)
            selected = await self._run(lane)
            # Further due bookkeeping runs before the blocked wait, even when this unit
            # changed nothing; the wait then targets the next future maintenance.
            return Selection(selected.lane, selected.result,
                             any(lane.maintenance and self._due(lane) for lane in self.lanes),
                             blocked=True)
        while self.remaining:
            lane = self.lanes[self.cursor]
            self.cursor = (self.cursor + 1) % len(self.lanes)
            self.remaining -= 1
            if not self._due(lane):
                continue
            return await self._run(lane)
        return Selection(None, UnitResult(False), False)

    async def _run(self, lane) -> Selection:
        try:
            result = await lane.run()
        except Exception as error:
            # One domain's failure must not stop diary or the other lanes. The
            # turn is still consumed: the failed operation may have used its
            # one model attempt, and its domain keeps lease/UNKNOWN custody.
            lane.failures += 1
            delay = min(FAILURE_BACKOFF_SECONDS * 2 ** (lane.failures - 1),
                        FAILURE_BACKOFF_MAX_SECONDS)
            lane.retry_at = monotonic() + delay
            log.warning("autonomous lane %s failed (%s); retry in %ss",
                        lane.name, getattr(error, "code", type(error).__name__), delay)
            return Selection(lane.name, UnitResult(False), bool(self.remaining))
        lane.failures, lane.retry_at = 0, 0
        if lane.interval is not None:
            lane.next_at = monotonic() + lane.interval
        if result.progressed:
            self.resume()
        return Selection(lane.name, result, bool(self.remaining))


def foreground_clear(core):
    if not owner_authority.required(core.store, core.settings):
        return not budget.chat_pending(core.store, core.clock())
    try:
        view = owner_authority.current(core.store, core.settings, core.clock)
    except CoreError:
        return False
    # This is a scheduling hint over the native signed input receipt, not a new
    # permission or input queue. Final admission remains owned by the domain.
    pending = owner_authority.owner_waiting(
        core.store, core.settings, core.clock(), authority=view.authority.as_dict(),
    )
    return not pending


def _allowed(core, purposes):
    if not owner_authority.required(core.store, core.settings):
        return True
    try:
        view = owner_authority.current(core.store, core.settings, core.clock)
    except CoreError:
        return False
    return set(purposes) <= set(view.policy.purposes)


def native_selector(core, worker_id) -> RoundRobin:
    """Bind only existing native operations; required single-step APIs fail closed.

    Core and domain owners provide the paid-admission/final-dispatch fences. This
    foreground check is a scheduling hint and grants no model or tool authority.
    """
    if core.settings.consolidation_enabled and _allowed(core, ("consolidation",)) and not callable(
        getattr(core.consolidation, "run_step", None),
    ):
        raise CoreError("NATIVE_CONSOLIDATION_STEP_REQUIRED", 503)
    if (core.settings.memory_enabled and _allowed(core, ("memory",))
        and not callable(getattr(core, "extract_step", None))):
        raise CoreError("NATIVE_MEMORY_STEP_REQUIRED", 503)

    def lane(name, operation, purposes=(), *, interval=None, maintenance=False):
        async def run():
            if not _allowed(core, purposes):
                return UnitResult(False)
            return await operation()

        return Lane(name, run, interval=interval, maintenance=maintenance)

    def maintenance(name, operation, purposes=()):
        async def run():
            changes = core.store.db.total_changes
            operation()
            # Completed a scheduled maintenance unit. Its next monotonic deadline
            # advances once; rescan only to observe work it enqueued on our own Store,
            # whose commits do not change this connection's data_version.
            return UnitResult(core.store.db.total_changes != changes)

        return lane(name, run, purposes, interval=MAINTENANCE_SECONDS, maintenance=True)

    async def wake_goal():
        with core.store.transaction():
            decisions = core.wake.evaluate()
            core.exploration.schedule_wake(decisions)
        return UnitResult(bool(decisions))

    async def schedule_goal():
        return UnitResult(bool(core.exploration.schedule_goal()))

    async def life_topic():
        return UnitResult(core.life_topics.advance(diary=core.diary))

    async def private_share():
        return UnitResult(bool(core.social.flush()))

    async def follow_up():
        from companion import turn_taking

        return UnitResult(turn_taking.flush(core.store, core.settings, core.clock()))

    async def activity():
        kinds, authority = ["life", "share", "commitment", "followup"], None
        if owner_authority.required(core.store, core.settings):
            try:
                view = owner_authority.current(core.store, core.settings, core.clock)
            except CoreError:
                return UnitResult(False)
            authority = encode(view.authority.as_dict())
            kinds = [kind for kind, purpose in (
                ("life", "life"), ("share", "proactive"), ("commitment", "goals"),
                ("followup", "proactive"),
            ) if purpose in view.policy.purposes]
        row = core.store.one(
            "SELECT j.* FROM jobs j JOIN events e USING(event_id) "
            "WHERE j.kind='activity' AND e.redacted=0 "
            "AND json_extract(e.body_json,'$.notice.kind') IN (SELECT value FROM json_each(?)) "
            "AND (? IS NULL OR json(j.authority_json)=json(?)) "
            "AND (j.state IN ('QUEUED','RETRY_WAIT') "
            "OR (j.state='RUNNING' AND j.lease_until<=?)) "
            "AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) "
            "ORDER BY j.rowid LIMIT 1",
            (encode(kinds), authority, authority, core.clock(), core.clock()),
        )
        if row is None:
            return UnitResult(False)
        if authority is not None:
            try:
                owner_authority.check_job(core.store, core.settings, core.clock, row,
                    purpose=owner_authority.model_purpose(core.store, "activity", row["job_id"]))
            except CoreError:
                return UnitResult(False)

        async def execute():
            return await core.run_once(worker_id, job_id=row["job_id"])

        return await run_native_unit(execute)

    async def diary():
        return await run_native_unit(core.diary.generate_once, worker_id)

    async def consolidation():
        if not core.settings.consolidation_enabled:
            return UnitResult(False)
        operation = getattr(core.consolidation, "run_step", None)
        if not callable(operation):
            raise CoreError("NATIVE_CONSOLIDATION_STEP_REQUIRED", 503)
        return await operation(worker_id)

    async def memory():
        if not core.settings.memory_enabled:
            return UnitResult(False)
        operation = getattr(core, "extract_step", None)
        if not callable(operation):
            raise CoreError("NATIVE_MEMORY_STEP_REQUIRED", 503)
        return await operation()

    async def rem():
        if not core.settings.rem.enabled or not core.settings.memory_enabled:
            return UnitResult(False)
        from companion import rem as rem_domain

        # The lane guard checks both purposes. Pin selection to the original
        # current reference too; a stale job never gains its successor's grant.
        try:
            view = owner_authority.current(core.store, core.settings, core.clock, purpose="rem")
        except CoreError:
            return UnitResult(False)
        now = core.clock()
        row = core.store.one(
            "SELECT j.job_id FROM jobs j JOIN events e USING(event_id) "
            "WHERE j.kind='rem' AND e.redacted=0 AND json(j.authority_json)=json(?) "
            "AND (j.state='QUEUED' OR (j.state='RETRY_WAIT' AND j.next_attempt_at<=?) "
            "OR (j.state='RUNNING' AND j.lease_until<=?)) "
            "AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) ORDER BY j.rowid LIMIT 1",
            (encode(view.authority.as_dict()), now, now, now),
        )
        if row is None:
            return UnitResult(bool(rem_domain.schedule_due(core)))

        async def execute():
            return await core.run_once(worker_id, job_id=row["job_id"])

        return await run_native_unit(execute)

    async def social():
        result = await social_autonomy_runtime.run_once(
            core.store, core.settings, core.model, core.clock, worker_id=worker_id,
        )
        return UnitResult(result)

    async def poke():
        if core.settings.mode != "live" or not core.settings.poke_policy.enabled:
            return UnitResult(False)
        return await run_native_unit(core._run_poke_once, worker_id)

    async def watch():
        from companion import watch_ledger

        return await run_native_unit(watch_ledger.run_once, core.store, core.settings,
                                     core.model, core.clock)

    async def style():
        from companion import style_distill

        return await run_native_unit(style_distill.run_once, core.store, core.settings,
                                     core.model, core.clock)

    async def jargon_unit():
        from companion import jargon

        return await run_native_unit(jargon.run_once, core.store, core.settings,
                                     core.model, core.clock)

    async def meme_caption():
        if core.settings.mode != "live" or not core.settings.meme_semantics_enabled:
            return UnitResult(False)
        return await run_native_unit(core.memes.run_once, worker_id)

    return RoundRobin((
        maintenance("retention", core.expire_raw_events),
        maintenance("media_cleanup", core.cleanup_media),
        maintenance("diary_schedule", core.diary.schedule_due, ("diary",)),
        maintenance("consolidation_schedule", core.consolidation.schedule_due, ("consolidation",)),
        maintenance("commitment_expiry", core.goals.expire_commitments, ("goals",)),
        lane("wake", wake_goal, WAKE_PURPOSES, interval=MAINTENANCE_SECONDS),
        lane("goal", schedule_goal, ("goals", "exploration")),
        lane("life_topic", life_topic, ("life",)),
        lane("private_share", private_share, ("proactive",)),
        lane("follow_up", follow_up, ("proactive",)),
        lane("activity", activity),
        lane("poke", poke, ("poke",)),
        lane("meme_caption", meme_caption, ("meme",)),
        lane("watch", watch, ("exploration",)),
        lane("style", style, ("expression",)),
        lane("jargon", jargon_unit, ("memory",)),
        lane("diary", diary, ("diary",)),
        lane("consolidation", consolidation, ("consolidation",)),
        lane("memory", memory, ("memory",)),
        lane("rem", rem, ("rem", "memory")),
        lane("social", social, ("social",)),
    ), lambda: foreground_clear(core))
