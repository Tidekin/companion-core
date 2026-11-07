"""Read-only deadline projections for native domains; no claims or recovery writes."""

from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from companion import owner_authority
from companion.contracts import CoreError

CHAT_WAIT_RESCAN_SECONDS = 1

JOBS = (
    ("jobs", "next_attempt_at", "state IN ('QUEUED','RETRY_WAIT')"),
    ("jobs", "lease_until", "state='RUNNING'"),
)
DELIVERIES = (("outbox", "lease_until", "state IN ('LEASED','SENDING')"),)
SOCIAL = (("social_autonomy_plans", "expires_at", "state IN ('READY','PLANNING')"),)
CONTENT = (
    ("content_items", "lease_until", "state IN ('FETCHING','RUNNING')"),
    ("content_items", "deadline", "state IN ('WAITING','QUEUED','FETCHING','RUNNING')"),
)
TOOLS = (("tool_tasks", "lease_until", "state IN ('LEASED','RUNNING')"),)
DIARY = (
    ("diary_runs", "next_attempt_at", "state IN ('PENDING','RETRY_WAIT')"),
    ("diary_runs", "lease_until", "state='RUNNING'"),
)
LIFE = (
    ("consolidation_runs", "next_attempt_at", "state='PARTIAL'"),
    ("consolidation_runs", "lease_until", "state='LEASED'"),
    ("reflection_runs", "lease_until", "state='RUNNING'"),
    ("reflection_runs", "next_attempt_at", "state='RETRY_WAIT'"),
    ("goal_steps", "lease_until", "state='RUNNING'"),
    ("goals", "next_due_at", "state IN ('ACCEPTED','QUEUED','RETRY_WAIT')"),
    ("commitments", "deadline", "state IN ('ACKNOWLEDGED','IN_PROGRESS')"),
    ("life_topics", "due_at", "state IN ('WAITING','DUE')"),
    ("life_topics", "expires_at", "state IN ('WAITING','DUE')"),
)


def _next(store, settings, scan_started_at, boundaries, *, local_hours=()):
    # Use the scan horizon, not wait entry: deadlines crossed during native work
    # need one rescan. Older fenced rows must not produce an immediate busy loop.
    parts = [
        f"SELECT {field}::timestamptz AS at FROM {table} WHERE {condition}"
        for table, field, condition in boundaries
    ]
    parts.append(
        "SELECT a.lease_until::timestamptz AS at FROM model_admissions a "
        "JOIN model_usage m USING(request_id) WHERE m.state='RESERVED'"
    )
    row = store.one(
        "SELECT MIN(at) AS at FROM (" + " UNION ALL ".join(parts) + ") WHERE at>=%s::timestamptz",
        (scan_started_at.isoformat(),),
    )
    dates = [row["at"] + timedelta(milliseconds=2)] if row["at"] else []
    local = scan_started_at.astimezone(ZoneInfo(settings.timezone))
    dates.append(
        (local + timedelta(days=1))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    for local_hour in local_hours:
        due = local.replace(hour=local_hour, minute=0, second=0, microsecond=0)
        if due <= local:
            due += timedelta(days=1)
        dates.append(due.astimezone(UTC))
    if owner_authority.required(store, settings):
        try:
            end = owner_authority.current(
                store,
                settings,
                lambda: scan_started_at.isoformat(),
            ).policy.expires_at
        except CoreError:
            end = None
        if end is not None and end > scan_started_at:
            dates.append(end)
    return min(dates)


def chat(store, settings, scan_started_at):
    deadline = _next(store, settings, scan_started_at, JOBS + DELIVERIES)
    # A due chat job can stay unclaimed behind short waits whose expiry writes
    # nothing (meme offer, voice ASR, image/content pending). Rescan on the former
    # polling cadence while one is waiting, instead of the 60 s maintenance horizon.
    if store.one(
        (
            (
                "SELECT 1 FROM jobs WHERE kind='chat' AND "
                "state='QUEUED' AND (next_attempt_at IS NULL OR "
                'next_attempt_at::timestamptz<=%s::timestamptz) LIMIT 1'
            )
        ),
        (scan_started_at.isoformat(),),
    ):
        deadline = min(deadline, scan_started_at + timedelta(seconds=CHAT_WAIT_RESCAN_SECONDS))
        # The meme-offer wait ends at a known time; rescan right after it instead of
        # up to a full rescan interval later.
        from companion.meme_core import OFFER_WAIT_SECONDS

        row = store.one(
            (
                (
                    'SELECT MIN(created_at::timestamptz) AS at FROM jobs '
                    "WHERE kind='chat' AND state='QUEUED' AND "
                    "created_at::timestamptz>%s::timestamptz-%s*INTERVAL '1 "
                    "second'"
                )
            ),
            (scan_started_at.isoformat(), OFFER_WAIT_SECONDS),
        )
        if row["at"]:
            ends = row["at"] + timedelta(milliseconds=2)
            deadline = min(deadline, ends + timedelta(seconds=OFFER_WAIT_SECONDS))
    return deadline


def content(store, settings, scan_started_at):
    return _next(store, settings, scan_started_at, CONTENT)


def executive(store, settings, scan_started_at):
    return _next(store, settings, scan_started_at, TOOLS)


def life(store, settings, scan_started_at):
    hours = {settings.diary_generate_hour, settings.consolidation_local_hour}
    if settings.quiet_start != settings.quiet_end:
        hours.add(settings.quiet_end)
    return _next(store, settings, scan_started_at, LIFE + DIARY + JOBS + SOCIAL, local_hours=hours)


PROVIDERS = {
    "worker": chat,
    "content-worker": content,
    "executive-worker": executive,
    "life-worker": life,
}
