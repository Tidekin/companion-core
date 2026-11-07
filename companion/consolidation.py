"""One recoverable coordinator per local day, reusing Memory and Diary state machines."""

import json
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from companion import owner_authority, private_life
from companion.contracts import CoreError
from companion.diary import source_hash
from companion.privacy import epoch
from companion.reflection import retryable_admission
from companion.store import after, encode, uid
from companion.worker_schedule import UnitResult

CONFIG_VERSION = "v05-1"


def erase_consolidation(store, event_ids):
    if not store.one(
        'SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace=current_schema()::regnamespace AND '
        "relname='consolidation_runs' AND relkind IN ('r','p')"
    ):
        return
    for event_id in event_ids:
        store.execute(
            "UPDATE consolidation_runs SET state = 'CANCELLED', lease_token = NULL, steps_json = "
            "'{}', source_set_sha256 = '', last_error_code = 'PRIVACY_CHANGED' WHERE run_id IN "
            '(SELECT run_id FROM consolidation_evidence WHERE event_id = %s)',
            (event_id,),
        )
        store.execute('DELETE FROM consolidation_evidence WHERE event_id = %s', (event_id,))
    store.execute(
        "UPDATE consolidation_runs SET state = 'CANCELLED', lease_token = NULL, last_error_code = "
        "'PRIVACY_CHANGED' WHERE state IN ('LEASED', 'PARTIAL')"
    )


def enabled(store, settings, clock):
    try:
        if owner_authority.required(store, settings):
            owner_authority.current(store, settings, clock,
                                    purpose="consolidation")
        else:
            private_life.ensure_active(store, settings, clock)
    except CoreError:
        return False
    paused = store.one("SELECT value FROM runtime_meta WHERE key = 'life_paused'")
    return settings.consolidation_enabled and not (paused and paused["value"] == "1")


class NightlyConsolidator:
    def __init__(self, store, settings, diary, reflections, goals, extract_step, clock):
        self.store, self.settings, self.clock = store, settings, clock
        self.diary, self.reflections, self.goals = diary, reflections, goals
        self.extract_step = extract_step

    def enabled(self):
        return enabled(self.store, self.settings, self.clock)

    def schedule_day(self, day):
        if not self.enabled():
            return None
        self.reflections.validate_day(day)
        with self.store.transaction():
            existing = self.status(day)
            if existing:
                steps = json.loads(existing["steps_json"])
                reflection = self.store.one(
                    'SELECT reflection_runs.run_id AS run_id, reflection_runs.identity_id AS '
                    'identity_id, reflection_runs.local_date AS local_date, '
                    'reflection_runs.timezone AS timezone, reflection_runs.state AS state, '
                    'reflection_runs.source_set_sha256 AS source_set_sha256, '
                    'reflection_runs.captured_privacy_epoch AS captured_privacy_epoch, '
                    'reflection_runs.lease_token AS lease_token, reflection_runs.lease_until AS '
                    'lease_until, reflection_runs.model_request_id AS model_request_id, '
                    'reflection_runs.last_error_code AS last_error_code, '
                    'reflection_runs.created_at AS created_at, reflection_runs.updated_at AS '
                    'updated_at, reflection_runs.next_attempt_at AS next_attempt_at FROM '
                    'reflection_runs AS reflection_runs WHERE reflection_runs.local_date = %s AND '
                    'reflection_runs.timezone = %s',
                    (day.isoformat(), self.settings.timezone),
                )
                if (
                    (existing["state"] == "PARTIAL" or
                     (existing["state"] == "CANCELLED"
                      and existing["last_error_code"] == "LIVE_WINDOW_STOPPED"))
                    and steps.get("reflection") in {"FAILED", "RETRY_WAIT", "CANCELLED"}
                    and retryable_admission(reflection, self.clock())
                    and existing["source_set_sha256"] == reflection["source_set_sha256"]
                    == source_hash(self.diary.repo.sources(day, purpose="consolidation"))
                ):
                    steps["reflection"] = "WAITING"
                    steps.pop("goals", None)
                    self.store.execute(
                        "UPDATE consolidation_runs SET state = 'SCHEDULED', steps_json = %s, "
                        'next_attempt_at = NULL, last_error_code = NULL, updated_at = %s WHERE '
                        'run_id = %s',
                        (encode(steps), self.clock(), existing["run_id"]),
                    )
                    self.store.audit(
                        "consolidation.resumed", existing["run_id"], existing["run_id"],
                        self.clock(), reason="UNADMITTED_REFLECTION",
                    )
                return existing["run_id"]
            run_id, now = uid("nightly"), self.clock()
            steps = {}
            if owner_authority.required(self.store, self.settings):
                steps["authority"] = owner_authority.current(
                    self.store, self.settings, self.clock, purpose="consolidation",
                ).authority.as_dict()
            self.store.insert(
                "consolidation_runs",
                run_id=run_id,
                identity_id="companion",
                local_date=day.isoformat(),
                timezone=self.settings.timezone,
                config_version=CONFIG_VERSION,
                state="SCHEDULED",
                steps_json=encode(steps),
                captured_privacy_epoch=epoch(self.store),
                created_at=now,
                updated_at=now,
            )
            return run_id

    def schedule_due(self):
        if not self.enabled():
            return []
        local = datetime.fromisoformat(self.clock()).astimezone(ZoneInfo(self.settings.timezone))
        latest = local.date() - timedelta(
            days=1 if local.hour >= self.settings.consolidation_local_hour else 2
        )
        return [
            self.schedule_day(local.date() - timedelta(days=offset))
            for offset in reversed(range(1, self.settings.consolidation_catchup_days + 1))
            if local.date() - timedelta(days=offset) <= latest
        ]

    def status(self, day):
        return self.store.one(
            'SELECT consolidation_runs.run_id AS run_id, consolidation_runs.identity_id AS '
            'identity_id, consolidation_runs.local_date AS local_date, consolidation_runs.timezone '
            'AS timezone, consolidation_runs.config_version AS config_version, '
            'consolidation_runs.state AS state, consolidation_runs.steps_json AS steps_json, '
            'consolidation_runs.source_set_sha256 AS source_set_sha256, '
            'consolidation_runs.captured_privacy_epoch AS captured_privacy_epoch, '
            'consolidation_runs.lease_token AS lease_token, consolidation_runs.lease_until AS '
            'lease_until, consolidation_runs.next_attempt_at AS next_attempt_at, '
            'consolidation_runs.last_error_code AS last_error_code, consolidation_runs.created_at '
            'AS created_at, consolidation_runs.updated_at AS updated_at FROM consolidation_runs AS '
            'consolidation_runs WHERE consolidation_runs.local_date = %s AND '
            'consolidation_runs.timezone = %s AND consolidation_runs.config_version = %s',
            (day.isoformat(), self.settings.timezone, CONFIG_VERSION),
        )

    def recent(self):
        return self.store.rows(
            'SELECT local_date, state, steps_json, last_error_code FROM consolidation_runs ORDER '
            'BY local_date DESC NULLS LAST LIMIT 7'
        )

    def claim(self):
        s, now = self.store, self.clock()
        with s.transaction():
            if not self.enabled():
                s.execute(
                    "UPDATE consolidation_runs SET state = 'CANCELLED', lease_token = NULL, "
                    "last_error_code = 'CONSOLIDATION_PAUSED' WHERE state IN ('SCHEDULED', "
                    "'LEASED', 'PARTIAL')"
                )
                return None
            row = s.one(
                'SELECT consolidation_runs.run_id AS run_id, consolidation_runs.identity_id AS '
                'identity_id, consolidation_runs.local_date AS local_date, '
                'consolidation_runs.timezone AS timezone, consolidation_runs.config_version AS '
                'config_version, consolidation_runs.state AS state, consolidation_runs.steps_json '
                'AS steps_json, consolidation_runs.source_set_sha256 AS source_set_sha256, '
                'consolidation_runs.captured_privacy_epoch AS captured_privacy_epoch, '
                'consolidation_runs.lease_token AS lease_token, consolidation_runs.lease_until AS '
                'lease_until, consolidation_runs.next_attempt_at AS next_attempt_at, '
                'consolidation_runs.last_error_code AS last_error_code, '
                'consolidation_runs.created_at AS created_at, consolidation_runs.updated_at AS '
                'updated_at FROM consolidation_runs AS consolidation_runs WHERE '
                "consolidation_runs.timezone = %s AND (consolidation_runs.state = 'SCHEDULED' OR "
                "(consolidation_runs.state = 'PARTIAL' AND consolidation_runs.next_attempt_at <= "
                "%s) OR (consolidation_runs.state = 'LEASED' AND consolidation_runs.lease_until <= "
                '%s)) ORDER BY consolidation_runs.local_date NULLS FIRST LIMIT 1',
                (self.settings.timezone, now, now),
            )
            if not row:
                return None
            steps = json.loads(row["steps_json"])
            if owner_authority.required(s, self.settings):
                view = owner_authority.current(s, self.settings, self.clock,
                                                purpose="consolidation")
                if steps.get("authority") != view.authority.as_dict():
                    s.execute("UPDATE consolidation_runs SET state = 'CANCELLED', lease_token = "
                        "NULL, steps_json = '{}', last_error_code = "
                        "'OWNER_AUTHORITY_REVISION_CHANGED' WHERE run_id = %s", (row["run_id"],))
                    return None
            sources = self.diary.repo.sources(date.fromisoformat(row["local_date"]),
                                              purpose="consolidation")
            # Erasure cancels leased/partial coordinators in the same DB transaction.
            # Benign Memory writes may advance the epoch before a crashed coordinator
            # checkpoints. Resume against fresh state only when its sources are unchanged;
            # each generative subtask separately rejects its own old model result.
            if row["state"] != "SCHEDULED" and row["source_set_sha256"] != source_hash(sources):
                s.execute(
                    "UPDATE consolidation_runs SET state = 'CANCELLED', lease_token = NULL, "
                    "steps_json = '{}', last_error_code = 'CONSOLIDATION_SOURCES_CHANGED' WHERE "
                    'run_id = %s',
                    (row["run_id"],),
                )
                return None
            token = uid("nightly_lease")
            s.execute(
                "UPDATE consolidation_runs SET state = 'LEASED', lease_token = %s, lease_until = "
                '%s, source_set_sha256 = %s, captured_privacy_epoch = %s, updated_at = %s WHERE '
                'run_id = %s',
                (
                    token,
                    after(now, self.settings.lease_seconds),
                    source_hash(sources),
                    epoch(s),
                    now,
                    row["run_id"],
                ),
            )
            for source_id in {source["event_id"] for source in sources}:
                s.execute(
                    'INSERT INTO consolidation_evidence VALUES (%s, %s) ON CONFLICT DO NOTHING',
                    (row["run_id"], source_id),
                )
            return {
                **row,
                "lease_token": token,
                "captured_privacy_epoch": epoch(s),
                "sources": sources,
                "steps": steps,
                "erasure_version": s.one(
                    'SELECT COALESCE(MAX(restore_min_replay_version), 0) AS n FROM '
                    'privacy_erasure_ledger'
                )["n"],
            }

    def after_memory_commit(self, run):
        # Memory.add advances the context epoch. Accept our own successful write only
        # if no erasure intervened and the exact collected source set is still intact.
        with nullcontext() if self.store.in_transaction else self.store.transaction():
            current = self.store.one(
                'SELECT consolidation_runs.run_id AS run_id, consolidation_runs.identity_id AS '
                'identity_id, consolidation_runs.local_date AS local_date, '
                'consolidation_runs.timezone AS timezone, consolidation_runs.config_version AS '
                'config_version, consolidation_runs.state AS state, consolidation_runs.steps_json '
                'AS steps_json, consolidation_runs.source_set_sha256 AS source_set_sha256, '
                'consolidation_runs.captured_privacy_epoch AS captured_privacy_epoch, '
                'consolidation_runs.lease_token AS lease_token, consolidation_runs.lease_until AS '
                'lease_until, consolidation_runs.next_attempt_at AS next_attempt_at, '
                'consolidation_runs.last_error_code AS last_error_code, '
                'consolidation_runs.created_at AS created_at, consolidation_runs.updated_at AS '
                'updated_at FROM consolidation_runs AS consolidation_runs WHERE '
                'consolidation_runs.run_id = %s', (run["run_id"],)
            )
            erasure_version = self.store.one(
                'SELECT COALESCE(MAX(restore_min_replay_version), 0) AS n FROM '
                'privacy_erasure_ledger'
            )["n"]
            if (
                not current
                or current["state"] != "LEASED"
                or current["lease_token"] != run["lease_token"]
                or current["lease_until"] <= self.clock()
            ):
                raise CoreError("CONSOLIDATION_STALE_LEASE", 409)
            if erasure_version != run["erasure_version"] or source_hash(
                run["sources"]
            ) != source_hash(self.diary.repo.sources(date.fromisoformat(run["local_date"]),
                                                     purpose="consolidation")):
                raise CoreError("PRIVACY_CHANGED", 409)
            run["captured_privacy_epoch"] = epoch(self.store)
            self.store.execute(
                'UPDATE consolidation_runs SET captured_privacy_epoch = %s WHERE run_id = %s',
                (run["captured_privacy_epoch"], run["run_id"]),
            )

    def checkpoint(self, run, state="LEASED", code=None, waiting=False, next_attempt_at=None):
        s, now = self.store, self.clock()
        if owner_authority.required(s, self.settings):
            owner_authority.current(s, self.settings, self.clock, purpose="consolidation",
                                    authority=run["steps"].get("authority"))
        with nullcontext() if s.in_transaction else s.transaction():
            current = s.one('SELECT consolidation_runs.run_id AS run_id, '
                'consolidation_runs.identity_id AS identity_id, consolidation_runs.local_date AS '
                'local_date, consolidation_runs.timezone AS timezone, '
                'consolidation_runs.config_version AS config_version, consolidation_runs.state AS '
                'state, consolidation_runs.steps_json AS steps_json, '
                'consolidation_runs.source_set_sha256 AS source_set_sha256, '
                'consolidation_runs.captured_privacy_epoch AS captured_privacy_epoch, '
                'consolidation_runs.lease_token AS lease_token, consolidation_runs.lease_until AS '
                'lease_until, consolidation_runs.next_attempt_at AS next_attempt_at, '
                'consolidation_runs.last_error_code AS last_error_code, '
                'consolidation_runs.created_at AS created_at, consolidation_runs.updated_at AS '
                'updated_at FROM consolidation_runs AS consolidation_runs WHERE '
                'consolidation_runs.run_id = %s', (run["run_id"],))
            if (
                not current
                or current["state"] != "LEASED"
                or current["lease_token"] != run["lease_token"]
                or current["lease_until"] <= now
            ):
                raise CoreError("CONSOLIDATION_STALE_LEASE", 409)
            if not self.enabled() or epoch(s) != run["captured_privacy_epoch"]:
                raise CoreError("PRIVACY_CHANGED", 409)
            s.execute(
                'UPDATE consolidation_runs SET state = %s, steps_json = %s, lease_token = %s, '
                'lease_until = %s, next_attempt_at = %s, last_error_code = %s, updated_at = %s '
                'WHERE run_id = %s',
                (
                    state,
                    encode(run["steps"]),
                    run["lease_token"] if state == "LEASED" else None,
                    after(now, self.settings.lease_seconds),
                    (next_attempt_at or after(now, 60)) if waiting else None,
                    code,
                    now,
                    run["run_id"],
                ),
            )

    async def run_step(self, worker_id="life-worker") -> UnitResult:
        from companion.nightly_stages import run_step

        return await run_step(self, worker_id)

    async def run_once(self, worker_id="life-worker"):
        """Batch convenience over the same durable steps; selectors use run_step.

        No separate implementation, leases or checkpoint authority are retained.
        A future native wait stops this drain; it never sleeps inside a work unit.
        """
        progressed = False
        for _ in range(256):
            result = await self.run_step(worker_id)
            progressed = progressed or result.progressed
            if not result.progressed or (result.next_deadline is not None
                and result.next_deadline > datetime.fromisoformat(self.clock())):
                break
        return progressed
