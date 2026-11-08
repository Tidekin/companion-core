"""One synchronous conversation boundary's native rows, never a permission cache."""

from dataclasses import dataclass

from companion import owner_authority, owner_input, reply_revision, runtime_config


@dataclass(frozen=True, repr=False)
class ConversationReads:
    authority: dict | None
    job: dict | None
    source: dict
    reply: dict | None
    configuration: dict | None
    image_item_ids: list[str]

    @property
    def privacy_epoch(self):
        value = self.source["source_epoch"]
        return int(value) if value is not None else 0


def _record(query):
    return "(SELECT to_jsonb(guard_row) FROM (" + query + ") AS guard_row)"


def read(store, job):
    """Fetch once under the original locks; callers still run every original validator.

    The object is local to check_conversation and is discarded before it returns.
    No Store state, transaction cache, permission result or cross-await reuse exists.
    """
    if not store.in_transaction:
        raise RuntimeError("CONVERSATION_READS_REQUIRE_TRANSACTION")
    owner_authority._lock_current(store)
    row = store.one(
        "SELECT " + _record(owner_authority._current_query()) + " AS authority, "
        "(SELECT to_jsonb(j) FROM jobs j WHERE j.job_id=%s) AS job, "
        + _record(owner_input._SOURCE_SQL) + " AS source, "
        + _record(reply_revision._JOB_SQL) + " AS reply, "
        + _record(runtime_config._TASK_SQL) + " AS configuration, "
        "COALESCE((SELECT jsonb_agg(item_id ORDER BY position NULLS FIRST) "
        "FROM agent_image_inputs WHERE job_id=%s), '[]'::jsonb) AS image_item_ids",
        (owner_authority.CURRENT, owner_authority.GENERATION, owner_authority.MODE,
         job["job_id"], job["event_id"], job["event_id"], job["job_id"],
         "job:" + job["job_id"], job["job_id"]),
    )
    return ConversationReads(**row)
