"""Durable reply supersession and the sources actually consumed by a job.

The event owns its revision. Jobs and deliveries inherit it rather than keeping
independent version counters. Callers still own admission, accounting and I/O.
"""

from companion.contracts import CoreError

SUPERSEDED = "REPLY_SUPERSEDED"
SOURCE_RECALLED = "REPLY_SOURCE_RECALLED"
CANCELLED = "REPLY_CANCELLED"
OWNER_INPUT = (
    "source='qq.onebot' AND actor_id='owner' AND privacy_scope='private' "
    "AND event_type IN ('message.received','message.media_received')"
)


def source_active(store, event_id):
    return bool(store.one(
        "SELECT 1 FROM events e WHERE e.event_id=? AND e.redacted=0 "
        "AND NOT EXISTS (SELECT 1 FROM runtime_meta WHERE key='erased_event:' || e.event_id)",
        (event_id,),
    ))


def owner_input(store, event_id):
    """Only owner inbound events participate in conversational reply supersession."""
    return bool(store.one("SELECT 1 FROM events WHERE event_id=? AND " + OWNER_INPUT,
                          (event_id,)))


def control_job(store, job):
    """Server-classified controls own an action independently of their reply bubble."""
    return bool(job["kind"] == "chat" and store.one(
        "SELECT 1 FROM events WHERE event_id=? AND " + OWNER_INPUT + " AND ("
        "json_extract(body_json,'$.control_command')=1 "
        "OR json_extract(body_json,'$.diary_command')=1)", (job["event_id"],),
    ))


def current(store, event_id, expected_revision=None):
    row = store.one(
        "SELECT e.reply_revision,c.reply_revision AS current_revision FROM events e "
        "JOIN conversations c USING(conversation_id) WHERE e.event_id=?", (event_id,),
    )
    return bool(row and source_active(store, event_id)
                and row["reply_revision"] == row["current_revision"]
                and (expected_revision is None or expected_revision == row["reply_revision"]))


def require_current(store, event_id, expected_revision=None):
    if not source_active(store, event_id):
        raise CoreError(SOURCE_RECALLED, 409)
    if not current(store, event_id, expected_revision):
        raise CoreError(SUPERSEDED, 409)


def require_reply_source(store, event_id, expected_revision=None):
    """Keep source revocation universal; only owner input replies have a revision."""
    if not source_active(store, event_id):
        raise CoreError(SOURCE_RECALLED, 409)
    if owner_input(store, event_id):
        require_current(store, event_id, expected_revision)


def check_sources(store, job_id):
    if store.one(
        "SELECT 1 FROM job_sources s JOIN events e USING(event_id) WHERE s.job_id=? "
        "AND (e.redacted=1 OR EXISTS (SELECT 1 FROM runtime_meta "
        "WHERE key='erased_event:' || e.event_id)) LIMIT 1", (job_id,),
    ):
        raise CoreError(SOURCE_RECALLED, 409)


def check_job(store, job):
    persisted = store.one("SELECT state,lease_token FROM jobs WHERE job_id=?", (job["job_id"],))
    if not persisted or persisted["state"] == "CANCELLED":
        raise CoreError(CANCELLED, 409)
    if job["kind"] == "chat":
        if control_job(store, job):
            if not source_active(store, job["event_id"]):
                raise CoreError(SOURCE_RECALLED, 409)
        else:
            require_reply_source(store, job["event_id"], job.get("reply_revision"))
    check_sources(store, job["job_id"])


def check_delivery(store, message_id):
    row = store.one("SELECT * FROM outbox WHERE message_id=?", (message_id,))
    if row and row["action_type"] in {"qq.private.reply", "qq.private.poke"}:
        require_reply_source(store, row["origin_event_id"])
        for job in store.rows("SELECT job_id FROM jobs WHERE event_id=? "
                              "AND kind IN ('chat','activity','poke')",
                              (row["origin_event_id"],)):
            check_sources(store, job["job_id"])


def advance(store, conversation_id, event_id, now):
    """Accept one new owner turn inside the caller's ingestion transaction."""
    if not store.db.in_transaction:
        raise RuntimeError("REPLY_REVISION_TRANSACTION_REQUIRED")
    from companion.transport import cancel_superseded_replies
    from companion.voice_jobs import fence_conversation

    fence_conversation(store, conversation_id)
    source = store.one(
        "SELECT * FROM events WHERE event_id=? AND conversation_id=? AND source='qq.onebot' "
        "AND actor_id='owner' AND privacy_scope='private' AND redacted=0",
        (event_id, conversation_id),
    )
    if not source or source["reply_revision"] != 0:
        raise CoreError("REPLY_REVISION_SOURCE_CHANGED", 409)
    store.execute("UPDATE conversations SET reply_revision=reply_revision+1 "
                  "WHERE conversation_id=?", (conversation_id,))
    revision = store.one("SELECT reply_revision FROM conversations WHERE conversation_id=?",
                         (conversation_id,))["reply_revision"]
    store.execute("UPDATE events SET reply_revision=? WHERE event_id=?", (revision, event_id))
    store.execute(
        "UPDATE jobs SET state='CANCELLED',lease_token=NULL,last_error_code=?,updated_at=? "
        "WHERE kind='chat' AND state IN ('QUEUED','RUNNING','RETRY_WAIT') AND event_id IN "
        "(SELECT event_id FROM events WHERE conversation_id=? AND event_id!=? AND "
        + OWNER_INPUT + " AND COALESCE(json_extract(body_json,'$.control_command'),0)!=1 "
        "AND COALESCE(json_extract(body_json,'$.diary_command'),0)!=1)",
        (SUPERSEDED, now, conversation_id, event_id),
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
        if any(not source_active(store, event_id) for event_id in sources):
            raise CoreError(SOURCE_RECALLED, 409)
        recorded = {row["event_id"] for row in store.rows(
            "SELECT event_id FROM job_sources WHERE job_id=?", (job["job_id"],),
        )}
        missing = sources - recorded
        if missing:
            store.db.executemany("INSERT OR IGNORE INTO job_sources(job_id,event_id) VALUES (?,?)",
                                 [(job["job_id"], event_id) for event_id in sorted(missing)])

    # A restored checkpoint usually already owns every source. Revalidate it on
    # a WAL snapshot; only new provenance needs the authoritative writer scope.
    store.read_or_write(remember)
