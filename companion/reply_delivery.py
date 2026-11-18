"""Translate a completed reply into native Outbox rows in the caller's transaction."""

from dataclasses import dataclass

from companion import live_text, notices, reply_quote
from companion.agent_output import reply_format, song_choice
from companion.contracts import CoreError
from companion.reply_segments import split_text
from companion.transport import enqueue, enqueue_reply_parts


def prepare(store, settings, job, text, *, generated, authority, text_only, snapshots):
    model_chat = generated and job["kind"] == "chat"
    mode = reply_format(store, job["event_id"]) if model_chat else "text"
    if mode == "voice" and not (settings.voice.enabled and settings.voice.reply.mode == "model"):
        mode = "text"
    preview = any(s["kind"] == "video_preview_reply" for s in snapshots)
    quote = None
    if authority and model_chat and not preview and settings.quote_policy != "never":
        if text_only:
            quote = reply_quote.choice(text)
            text = reply_quote.strip_markers(text)
        else:
            quote, text = reply_quote.extract(text)
    elif generated:
        text = reply_quote.strip_markers(text)
    return PreparedReply(text, mode, model_chat, preview, text_only, quote)


def _newer_input(store, job):
    return store.one(
        "SELECT 1 FROM events WHERE conversation_id=%s AND actor_id='owner' "
        "AND source IN ('qq.onebot','native.chat') "
        "AND event_type IN ('message.received','message.media_received') "
        "AND record_sequence > (SELECT record_sequence FROM events WHERE event_id=%s) LIMIT 1",
        (job["conversation_id"], job["event_id"]),
    )


@dataclass(frozen=True)
class PreparedReply:
    text: str
    mode: str
    model_chat: bool
    preview: bool
    text_only: bool
    quote: str | None

    def enqueue(self, core, job, now, *, authority, bounded_text, notice_parts):
        policy = core.settings.segmented_reply
        interval = policy.interval_ms if policy else 0
        expression_allowed = authority is None or "expression" in authority.policy.purposes
        intent = None
        if (policy and self.model_chat and not self.preview and self.mode == "text"
                and core.settings.expression_enabled and expression_allowed):
            intent = core.expression.intent(job["event_id"], self.text, now)
        if self.preview:
            parts = split_text(self.text, policy) if policy else [self.text]
            ids = enqueue_reply_parts(core.store, job, parts, interval, now)
        elif self.mode == "mixed":
            from companion import reply_composition

            ids = [] if _newer_input(core.store, job) else reply_composition.enqueue(core, job, now)
        elif self.mode == "singing":
            ids = self._singing(core, job, now)
        elif self.mode == "voice":
            ids = enqueue_reply_parts(core.store, job, [self.text], 0, now)
        elif notice_parts is not None:
            action = ("qq.private.reply" if notices.required(core.store, job["job_id"])
                      else "qq.private.proactive")
            ids = enqueue_reply_parts(core.store, job, notice_parts, interval, now,
                                      action=action) if notice_parts else []
            notices.record(core.store, core.settings, job, ids, now)
        elif policy and self.model_chat and (
            intent is None or intent.image is None or authority is not None
            and not core.store.one(
                "SELECT 1 FROM generated_images WHERE job_id=%s AND state='SUCCEEDED' "
                "AND message_id IS NULL", (job["job_id"],),
            )
        ):
            ids = self._segments(core, job, now, policy, intent, authority, bounded_text)
        else:
            quote = reply_quote.native_target(
                core.store, core.settings, core.clock, job, self.quote,
            ) if authority and self.text_only and job["kind"] == "chat" else None
            action = "qq.private.proactive" if job["kind"] == "activity" else "qq.private.reply"
            args = (job["event_id"], job["conversation_id"], self.text, now)
            options = {"action": action}
            if quote:
                options["quote"] = quote
            message_id = (core.expression.enqueue(*args, **options) if expression_allowed
                          else enqueue(core.store, *args, **options))
            ids = [message_id]
        if authority and ids and self.model_chat and not self.text_only and self.mode == "text":
            from companion import image_generation

            image_generation.attach(core.store, job, ids, now)
        return ids

    def _singing(self, core, job, now):
        from companion.agent_singing import selected

        song_id = song_choice(core.store, job["event_id"])
        selected(core.store, core.settings, core.clock, job, song_id)
        song = core.voice.jobs.result(song_id)
        parts = [self.text if index == 0 else f"歌曲（{index + 1}/{len(song.audio)}）"
                 for index in range(len(song.audio))]
        ids = enqueue_reply_parts(core.store, job, parts, 0, now)
        for index, message_id in enumerate(ids):
            core.voice.freeze_song(message_id, song_id, song, index)
        return ids

    def _segments(self, core, job, now, policy, intent, authority, bounded_text):
        parts = split_text(intent.text if intent else self.text, policy)
        image = (intent.image.model_dump(exclude_none=True)
                 if intent is not None and intent.image is not None else None)
        if image is not None and policy.max_segments != "unlimited":
            # The picture occupies one of the existing visible-message slots.
            text_slots = policy.max_segments - 1
            if len(parts) > text_slots:
                parts = parts[:text_slots - 1] + ["".join(parts[text_slots - 1:])]
        if bounded_text:
            approval, _ = live_text.check_job(
                core.store, core.settings, core.clock, job,
            )
            if len(self.text) > approval.max_reply_chars:
                raise CoreError("LIVE_TEXT_SETTLED_REPLY_REQUIRED", 403)
            available = live_text.remaining_send_slots(core.store, approval)
            if available <= 0:
                raise CoreError("LIVE_TEXT_SEND_ATTEMPT_CAP", 403)
            if len(parts) > available:
                parts = [self.text]
        if (len(parts) > 1 or image is not None) and _newer_input(core.store, job):
            return []  # The owner moved on while the model was answering.
        quote_to = reply_quote.target(
            core.store, core.settings, core.clock, job, self.quote,
        ) if authority and parts and not self.text_only else None
        native_quote = reply_quote.native_target(
            core.store, core.settings, core.clock, job, self.quote,
        ) if authority and parts and self.text_only and image is None else None
        return enqueue_reply_parts(
            core.store, job, parts, policy.interval_ms, now, reply_to=quote_to,
            image=image, quote=native_quote,
        ) if parts else []
