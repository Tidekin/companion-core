"""Durable reply supersession and the sources actually consumed by a job.

The event owns its revision. Jobs and deliveries inherit it rather than keeping
independent version counters. Callers still own admission, accounting and I/O.
"""

from companion import job_queue
from companion.contracts import CoreError

SUPERSEDED = "REPLY_SUPERSEDED"
SOURCE_RECALLED = "REPLY_SOURCE_RECALLED"
CANCELLED = "REPLY_CANCELLED"
OWNER_INPUT = (
    "source IN ('qq.onebot','native.chat') AND actor_id='owner' AND privacy_scope='private' "
    "AND event_type IN ('message.received','message.media_received')"
)


def source_active(store, event_id):
    return event_id in active_sources(store, [event_id])


def active_sources(store, event_ids):
    keys = list(dict.fromkeys(event_ids))
    if not keys:
        return set()
    return {row["event_id"] for row in store.rows(
        "SELECT e.event_id FROM events AS e WHERE e.event_id=ANY(%s::text[]) AND "
        "e.redacted=0 AND NOT EXISTS(SELECT 1 FROM runtime_meta "
        "WHERE key='erased_event:'||e.event_id)", (keys,))}


def owner_input(store, event_id):
    """Only owner inbound events participate in conversational reply supersession."""
    return bool(store.one("SELECT 1 FROM events WHERE event_id=%s AND " + OWNER_INPUT, (event_id,)))


def control_job(store, job):
    """Server-classified controls own an action independently of their reply bubble."""
    return bool(
        job["kind"] == "chat"
        and store.one(
            "SELECT 1 FROM events WHERE event_id=%s AND " + OWNER_INPUT + " AND ("
            "(body_json::json->'control_command')::text IN ('true','1') "
            "OR (body_json::json->'diary_command')::text IN ('true','1'))",
            (job["event_id"],),
        )
    )


_REPLY_STATE = (
    "SELECT e.reply_revision, c.reply_revision AS current_revision, "
    "e.redacted=0 AND NOT EXISTS(SELECT 1 FROM runtime_meta "
    "WHERE key='erased_event:'||e.event_id) AS source_active, "
    f"({OWNER_INPUT}) AS owner_input, "
    "((body_json::json->'control_command')::text IN ('true','1') OR "
    "(body_json::json->'diary_command')::text IN ('true','1')) AS is_control "
    "FROM events AS e JOIN conversations AS c USING (conversation_id) "
)


def _reply_state(store, event_id):
    """Read revocation, owner kind and both revisions at the same native boundary."""
    return store.one(_REPLY_STATE + "WHERE e.event_id=%s", (event_id,))


def _current(row, expected_revision):
    return bool(
        row
        and row["source_active"]
        and row["reply_revision"] == row["current_revision"]
        and (expected_revision is None or expected_revision == row["reply_revision"])
    )


def current(store, event_id, expected_revision=None):
    return _current(_reply_state(store, event_id), expected_revision)


def _require_active(row):
    if not row or not row["source_active"]:
        raise CoreError(SOURCE_RECALLED, 409)


def require_current(store, event_id, expected_revision=None):
    row = _reply_state(store, event_id)
    _require_active(row)
    if not _current(row, expected_revision):
        raise CoreError(SUPERSEDED, 409)


def _require_reply_state(row, expected_revision):
    _require_active(row)
    if row["owner_input"] and not _current(row, expected_revision):
        raise CoreError(SUPERSEDED, 409)


def require_reply_source(store, event_id, expected_revision=None):
    """Keep source revocation universal; only owner input replies have a revision."""
    _require_reply_state(_reply_state(store, event_id), expected_revision)


def check_sources(store, job_id):
    if store.one(
        (
            "SELECT 1 FROM job_sources AS s JOIN events AS e USING "
            "(event_id) WHERE s.job_id = %s AND (e.redacted = 1 OR "
            "EXISTS(SELECT 1 FROM runtime_meta WHERE key = "
            "'erased_event:' || e.event_id)) LIMIT 1"
        ),
        (job_id,),
    ):
        raise CoreError(SOURCE_RECALLED, 409)


_JOB_FIELDS = (
    "SELECT j.state, r.*, EXISTS(SELECT 1 FROM job_sources AS s "
    "JOIN events AS e USING (event_id) WHERE s.job_id=j.job_id AND "
    "(e.redacted=1 OR EXISTS(SELECT 1 FROM runtime_meta WHERE "
    "key='erased_event:'||e.event_id))) AS sources_recalled "
)


def _job_query(*, native_job=False):
    table = "guard_job" if native_job else "jobs"
    event = "j.event_id" if native_job else "%s"
    condition = "" if native_job else " WHERE j.job_id=%s"
    return (_JOB_FIELDS + f"FROM {table} AS j LEFT JOIN LATERAL (" + _REPLY_STATE
            + f"WHERE e.event_id={event}) AS r ON true" + condition)


_JOB_SQL = _job_query()


def check_job(store, job):
    persisted = store.one(
        _JOB_SQL,
        (job["event_id"], job["job_id"]),
    )
    _checked_job(job, persisted)


def _checked_job(job, persisted):
    if not persisted or persisted["state"] == "CANCELLED":
        raise CoreError(CANCELLED, 409)
    if job["kind"] == "chat":
        state = persisted
        if state and state["owner_input"] and state["is_control"]:
            _require_active(state)
        else:
            _require_reply_state(state, job.get("reply_revision"))
    if persisted["sources_recalled"]:
        raise CoreError(SOURCE_RECALLED, 409)


def check_delivery(store, message_id):
    row = store.one(
        (
            "SELECT outbox.message_id AS message_id, "
            "outbox.origin_event_id AS origin_event_id, "
            "outbox.conversation_id AS conversation_id, "
            "outbox.action_type AS action_type, outbox.content_json AS "
            "content_json, outbox.content_sha256 AS content_sha256, "
            "outbox.approval_id AS approval_id, outbox.state AS state, "
            "outbox.delivery_key AS delivery_key, outbox.lease_owner AS "
            "lease_owner, outbox.lease_until AS lease_until, "
            "outbox.attempt_count AS attempt_count, "
            "outbox.provider_message_id AS provider_message_id, "
            "outbox.last_error_code AS last_error_code, outbox.created_at"
            " AS created_at, outbox.updated_at AS updated_at, "
            "outbox.sent_at AS sent_at, outbox.lease_token AS "
            "lease_token, outbox.next_attempt_at AS next_attempt_at, "
            "outbox.reply_group_id AS reply_group_id, "
            "outbox.segment_index AS segment_index, outbox.segment_count "
            "AS segment_count, outbox.reply_group_sha256 AS "
            "reply_group_sha256, outbox.segment_interval_ms AS "
            "segment_interval_ms, outbox.delivery_lane AS delivery_lane, "
            "outbox.native_ack_json AS native_ack_json, "
            "outbox.authority_json AS authority_json FROM outbox AS "
            "outbox WHERE outbox.message_id = %s"
        ),
        (message_id,),
    )
    if row and row["action_type"] in {"qq.private.reply", "qq.private.poke"}:
        require_reply_source(store, row["origin_event_id"])
        for job in store.rows(
            (
                "SELECT job_id FROM jobs WHERE event_id = %s AND kind IN "
                "('chat', 'activity', 'poke')"
            ),
            (row["origin_event_id"],),
        ):
            check_sources(store, job["job_id"])


def advance(store, conversation_id, event_id, now):
    """Accept one new owner turn inside the caller's ingestion transaction."""
    if not store.in_transaction:
        raise RuntimeError("REPLY_REVISION_TRANSACTION_REQUIRED")
    from companion.transport import cancel_superseded_replies
    from companion.voice_jobs import fence_conversation

    fence_conversation(store, conversation_id)
    source = store.one(
        (
            "SELECT events.event_id AS event_id, events.schema_version AS"
            " schema_version, events.source AS source, "
            "events.source_event_id AS source_event_id, "
            "events.conversation_id AS conversation_id, events.actor_id "
            "AS actor_id, events.event_type AS event_type, "
            "events.occurred_at AS occurred_at, events.ingested_at AS "
            "ingested_at, events.privacy_scope AS privacy_scope, "
            "events.body_json AS body_json, events.trace_id AS trace_id, "
            "events.idempotency_key AS idempotency_key, "
            "events.fingerprint AS fingerprint, events.redacted AS "
            "redacted, events.memory_processed AS memory_processed, "
            "events.reply_revision AS reply_revision FROM events AS "
            "events WHERE events.event_id = %s AND events.conversation_id"
            " = %s AND events.source IN ('qq.onebot','native.chat') AND events.actor_id = "
            "'owner' AND events.privacy_scope = 'private' AND "
            "events.redacted = 0"
        ),
        (event_id, conversation_id),
    )
    if not source or source["reply_revision"] != 0:
        raise CoreError("REPLY_REVISION_SOURCE_CHANGED", 409)
    store.execute((
        'UPDATE conversations SET reply_revision=reply_revision+1 WHERE '
        'conversation_id=%s'
    ), (conversation_id,))
    revision = store.one('SELECT reply_revision FROM conversations WHERE conversation_id=%s',
                         (conversation_id,))["reply_revision"]
    store.execute('UPDATE events SET reply_revision=%s WHERE event_id=%s', (revision, event_id))
    job_queue.cancel_active(
        store, (
            "kind='chat' AND event_id IN (SELECT event_id FROM events WHERE "
            'conversation_id=%s AND event_id!=%s AND '
        ) + OWNER_INPUT + " AND "
        "COALESCE((body_json::json->'control_command')::text,'null') NOT IN ('true','1') AND "
        "COALESCE((body_json::json->'diary_command')::text,'null') NOT IN ('true','1'))",
        (conversation_id, event_id), SUPERSEDED, now,
    )
    cancel_superseded_replies(store, conversation_id, event_id, now)
    store.audit("reply.superseded", event_id, source["trace_id"], now, reply_revision=revision)
    return revision


def remember_sources(store, job, event_ids):
    """Record real prompt inputs without creating erasure edges between owner turns."""
    if not job or job.get("event_id") is None:
        return
    sources = set(event_ids) | {job["event_id"]}

    def remember():
        check_job(store, job)
        if active_sources(store, sources) != sources:
            raise CoreError(SOURCE_RECALLED, 409)
        recorded = {
            row["event_id"]
            for row in store.rows(
                "SELECT event_id FROM job_sources WHERE job_id = %s",
                (job["job_id"],),
            )
        }
        missing = sources - recorded
        if missing:
            store.execute(
                "INSERT INTO job_sources (job_id,event_id) SELECT %s,source.event_id "
                "FROM unnest(%s::text[]) WITH ORDINALITY AS source(event_id,position) "
                "ORDER BY source.position ON CONFLICT DO NOTHING",
                (job["job_id"], sorted(missing)))

    # A restored checkpoint usually already owns every source. Revalidate it on
    # a WAL snapshot; only new provenance needs the authoritative writer scope.
    store.read_or_write(remember)
