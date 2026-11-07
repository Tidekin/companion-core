"""Writes to the Core's durable jobs queue, within the owning operation's transaction.

Callers choose eligible work, verify domain authority, and settle their own evidence.
Worker results carrying a lease are conditional on that lease still owning the job.
"""

from companion.store import encode, uid

ACTIVE = "state IN ('QUEUED','RUNNING','RETRY_WAIT')"
_UNSET = object()


def create(store, job_id, event_id, kind, key, now, *, max_attempts=1, state="QUEUED",
           next_attempt_at=None, reason=None, authority=None):
    extra = {"authority_json": encode(authority)} if authority is not None else {}
    store.insert("jobs", job_id=job_id, event_id=event_id, kind=kind, state=state,
                 idempotency_key=key, max_attempts=max_attempts, context_tracked=1,
                 next_attempt_at=next_attempt_at, last_error_code=reason, created_at=now,
                 updated_at=now, **extra)


def enqueue(store, event_id, kind, key, now, *, max_attempts, next_attempt_at=None):
    job_id = uid("job")
    create(store, job_id, event_id, kind, key, now, max_attempts=max_attempts,
           next_attempt_at=next_attempt_at)
    if event_id is not None:
        store.insert("job_sources", job_id=job_id, event_id=event_id)
    return job_id


def claim(store, job_id, worker, token, until, now, *, single_attempt=False):
    attempts = "1" if single_attempt else "attempts+1"
    store.execute("UPDATE jobs SET state='RUNNING',lease_owner=%s,lease_token=%s,lease_until=%s,"
                  f"attempts={attempts},updated_at=%s WHERE job_id=%s",
                  (worker, token, until, now, job_id))


def _owned(job_id, token):
    if token is _UNSET:
        return 'job_id=%s', (job_id,)
    return "job_id=%s AND state='RUNNING' AND lease_token=%s", (job_id, token)


def _outcome(state, now, reason):
    updates, values = ['state=%s'], [state]
    if now is not None:
        updates.append('updated_at=%s')
        values.append(now)
    if reason is not _UNSET:
        updates.append('last_error_code=%s')
        values.append(reason)
    return updates, values


def release(store, job_id, state, now=None, *, reason=_UNSET, token=_UNSET, condition=None):
    """End an attempt. Omitted metadata survives normal completion and erasure replay."""
    scope, params = _owned(job_id, token)
    if condition is not None:
        scope += " AND " + condition
    updates, values = _outcome(state, now, reason)
    updates.append("lease_token=NULL")
    return store.execute(f"UPDATE jobs SET {','.join(updates)} WHERE {scope}",
                         (*values, *params))


def finish_unclaimed(store, job_id, state, now=None, *, reason=_UNSET, condition="TRUE"):
    """Apply a rejection or an existing result before any worker lease was acquired."""
    updates, values = _outcome(state, now, reason)
    return store.execute(f"UPDATE jobs SET {','.join(updates)} WHERE job_id=%s AND {condition}",
                         (*values, job_id))


def cancel_active(store, condition, params, reason, now=None, *, clear_deadline=False):
    """Cancel active work in a feature's static scope. Parameters carry all input values."""
    updates, values = _outcome("CANCELLED", now, reason)
    updates.append("lease_token=NULL")
    if clear_deadline:
        updates.append("lease_until=NULL")
    store.execute(f"UPDATE jobs SET {','.join(updates)} WHERE {ACTIVE} AND ({condition})",
                  (*values, *params))


def retry_outcome(store, job_id, state, now, next_at, *, reason=_UNSET, token=_UNSET,
                  refund=False, max_attempts=None):
    """Record the retry decision; a capacity yield returns its unspent attempt."""
    scope, params = _owned(job_id, token)
    updates, values = _outcome(state, now, reason)
    updates.extend(('next_attempt_at=%s', "lease_token=NULL"))
    values.append(next_at)
    if refund:
        updates.append("attempts=GREATEST(0,attempts-1)")
    if max_attempts is not None:
        updates.append('max_attempts=%s')
        values.append(max_attempts)
    return store.execute(f"UPDATE jobs SET {','.join(updates)} WHERE {scope}",
                         (*values, *params))


def requeue(store, job_id, now, *, token=_UNSET, unadmitted=False):
    """Yield a chunked run, or recover a never-admitted single-attempt job."""
    scope, params = _owned(job_id, token)
    attempts = "0" if unadmitted else "GREATEST(0,attempts-1)"
    store.execute("UPDATE jobs SET state='QUEUED',lease_token=NULL,lease_until=NULL,"
                  f"attempts={attempts},updated_at=%s WHERE {scope}", (now, *params))


def retry_unadmitted(store, job, now, next_at, reason):
    store.execute((
        "UPDATE jobs SET state='QUEUED',lease_token=NULL,attempts=0,next_attempt_at"
        "=%s,last_error_code=%s,updated_at=%s WHERE job_id=%s AND state='RUNNING' "
        'AND lease_token=%s'
    ),
                  (next_at, reason, now, job["job_id"], job["lease_token"]))


def renew(store, job, until):
    store.execute('UPDATE jobs SET lease_until=%s WHERE job_id=%s AND lease_token=%s',
                  (until, job["job_id"], job["lease_token"]))


def reader_ready(store, request_id, now):
    store.execute((
        "UPDATE jobs SET next_attempt_at=%s,updated_at=%s WHERE state='RETRY_WAIT' "
        'AND job_id IN (SELECT a.job_id FROM agent_tool_calls a JOIN '
        'agent_conversations c USING(job_id) WHERE a.request_id=%s AND '
        "a.state='DEFERRED' AND c.phase='WAITING')"
    ),
                  (now, now, request_id))


def mark_context_untracked(store, job_id):
    store.execute('UPDATE jobs SET context_tracked=0 WHERE job_id=%s', (job_id,))


def bind_authority(store, job_id, authority):
    store.execute('UPDATE jobs SET authority_json=%s WHERE job_id=%s', (encode(authority), job_id))
