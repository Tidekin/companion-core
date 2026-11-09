"""Owner input debounce: a burst of private messages is answered once.

The tiers follow MaiBot's "natural" debounce (src/chat/debounce/controller.py at
c6429547af711555e3d4d1b8d04b413572cb616c): a batch opens with a short wait when the chat
was quiet and a longer one when it was active, each further message extends it a little,
a bare image waits longer, and the whole batch is bounded. No model call is involved:
the new chat job only gets a later `next_attempt_at`, and the existing reply revision
already supersedes the earlier job and carries its unanswered input into the next context.
"""

import json
from datetime import datetime

from pydantic import Field

from companion.contracts import StrictModel
from companion.store import after, encode

BATCH_KEY = "input_debounce_batch:"


class DebounceTiming(StrictModel):
    quiet_seconds: float = Field(default=2.0, ge=0, le=10)
    active_seconds: float = Field(default=5.0, ge=0, le=15)
    continue_seconds: float = Field(default=2.0, ge=0, le=10)
    media_seconds: float = Field(default=4.0, ge=0, le=15)
    batch_seconds: float = Field(default=8.0, ge=0, le=30)
    active_window_seconds: float = Field(default=30.0, ge=0, le=600)


class InputDebounceConfig(DebounceTiming):
    enabled: bool = True

    def timing(self):
        return DebounceTiming(**self.model_dump(include=set(DebounceTiming.model_fields)))


def eligible(body):
    """Commands answer at once; only ordinary chat text and media are batched."""
    return not (body.get("control_command") or body.get("diary_command")
                or (body.get("text") or "").lstrip().startswith("/"))


def due(store, settings, conversation_id, event_id, body, now):
    """When the new chat job may first be claimed, or None for no delay.

    Call inside the ingestion transaction after the event row exists and before the
    reply revision supersedes the earlier job, so a waiting batch is still visible.
    """
    config = settings.input_debounce
    if not config.enabled or not eligible(body):
        return None
    key = BATCH_KEY + conversation_id
    waiting = store.one(
        'SELECT 1 FROM jobs AS j JOIN events AS e USING (event_id) WHERE e.conversation_id = %s '
        "AND e.event_id <> %s AND j.kind = 'chat' AND j.state = 'QUEUED' AND j.attempts = 0 AND "
        'j.next_attempt_at > %s', (conversation_id, event_id, now))
    start = store.one('SELECT value FROM runtime_meta WHERE key = %s', (key,))
    if waiting and start:
        opened = start["value"]
        if opened.startswith("{"):
            batch = json.loads(opened)
            opened = batch["opened"]
            timing = DebounceTiming.model_validate(batch["timing"])
        else:
            # A batch opened before this upgrade stored only its opening time.
            timing = config.timing()
        wait = timing.continue_seconds
    else:
        from companion import runtime_config

        timing = (runtime_config.boundary(store, settings, lambda: now, worker="inbox").debounce
                  if settings.operator_hot_reload else config.timing())
        since = after(now, -timing.active_window_seconds)
        active = store.one(
            'SELECT 1 FROM events WHERE conversation_id = %s AND event_id <> %s AND actor_id = '
            "'owner' AND ingested_at >= %s LIMIT 1", (conversation_id, event_id, since),
        ) or store.one(
            'SELECT 1 FROM outbox WHERE conversation_id = %s AND sent_at >= %s LIMIT 1',
            (conversation_id, since),
        )
        wait = timing.active_seconds if active else timing.quiet_seconds
        opened = now
        store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO '
            'UPDATE SET value = excluded.value, record_sequence = DEFAULT', (key, encode({
            "opened": now, "timing": timing.model_dump()})))
    if not (body.get("text") or "").strip():
        wait = max(wait, timing.media_seconds)
    limit = after(opened, timing.batch_seconds)
    result = min(after(now, wait), limit, key=datetime.fromisoformat)
    return result if datetime.fromisoformat(result) > datetime.fromisoformat(now) else None
