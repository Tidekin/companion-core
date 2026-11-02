"""Compile a model's ordered choice into the original native Outbox transaction."""

import json

from companion import agent_output, agent_singing, image_generation, reply_quote, voice_authority
from companion.contracts import CoreError
from companion.privacy import redact
from companion.transport import digest, enqueue_mixed_reply
from companion_delivery_protocol.reply_groups import mixed_part


def clean(text, settings):
    return reply_quote.extract(redact(text, settings))[1]


def generated(store, job, image_id, now):
    row = store.one("SELECT * FROM generated_images WHERE image_id=?", (image_id,))
    if (not row or row["job_id"] != job["job_id"] or row["event_id"] != job["event_id"]
        or row["state"] != "SUCCEEDED" or row["expires_at"] <= now
        or not image_generation._path(store, image_id).is_file()):
        raise CoreError("GENERATED_IMAGE_UNAVAILABLE", 410)
    return image_generation.reference(row)


def validate(core, job, output):
    view = voice_authority.current(core.store, core.settings, core.clock,
                                    authority=json.loads(job["authority_json"]))
    for part in output.parts:
        if part.kind == "voice":
            if (not core.settings.voice.reply.enabled
                or core.settings.voice.reply.mode != "model"
                or not any(n.enabled and "tts" in n.capabilities
                           for n in core.settings.voice.nodes.values())):
                raise CoreError("VOICE_REPLY_POLICY", 403)
        elif part.kind == "singing":
            agent_singing.selected(core.store, core.settings, core.clock, job, part.request_id)
        elif part.kind in {"meme", "generated_image"}:
            if "expression" not in view.policy.purposes:
                raise CoreError("OWNER_PURPOSE_REQUIRED", 403)
            if part.kind == "generated_image":
                generated(core.store, job, part.image_id, core.clock())


def enqueue(core, job, now):
    output = agent_output.composition(core.store, job["event_id"])
    validate(core, job, output)
    contents, songs, pictures = [], {}, {}
    for index, part in enumerate(output.parts):
        choice = {"kind": part.kind, "source_index": index}
        if part.kind == "meme":
            intent = core.expression.intent(job["event_id"],
                clean(output.text, core.settings) + "\n⬡" + part.candidate_id + "⬡", now)
            if intent.image is not None:
                contents.append({"text": "", "image": intent.image.model_dump(exclude_none=True),
                                 "reply_part": choice})
        elif part.kind == "generated_image":
            reference = generated(core.store, job, part.image_id, now)
            pictures[len(contents)] = part.image_id
            contents.append({"text": "", "generated_image": reference, "reply_part": choice})
        elif part.kind == "singing":
            song = core.voice.jobs.result(part.request_id)
            if song is None:
                raise CoreError("SINGING_READY_RESULT_REQUIRED", 403)
            for segment in range(len(song.audio)):
                songs[len(contents)] = (part.request_id, song, segment)
                contents.append({"text": clean(part.text, core.settings) if segment == 0
                                 else f"唱歌第{segment + 1}/{len(song.audio)}段",
                    "reply_part": {**choice, "request_id": part.request_id,
                                   "singing_segment_index": segment}})
        else:
            contents.append({"text": clean(part.text, core.settings), "reply_part": choice})
    policy = core.settings.segmented_reply
    if policy and policy.max_segments != "unlimited" and len(contents) > policy.max_segments:
        raise CoreError("OWNER_DELIVERY_GROUP_CHANGED", 403)
    interval = policy.interval_ms if policy else 0
    ids = enqueue_mixed_reply(core.store, job, contents, interval, now)
    for index, (request_id, song, segment) in songs.items():
        core.voice.freeze_song(ids[index], request_id, song, segment)
    for index, image_id in pictures.items():
        row = core.store.one("SELECT message_id FROM generated_images WHERE image_id=?",
                             (image_id,))
        if row["message_id"] not in {None, ids[index]}:
            raise CoreError("GENERATED_IMAGE_ALREADY_BOUND", 409)
        core.store.execute("UPDATE generated_images SET message_id=?,updated_at=? WHERE image_id=?",
                           (ids[index], now, image_id))
    return ids


def check_item(store, settings, job, row):
    output = agent_output.composition(store, job["event_id"])
    content = json.loads(row["content_json"])
    choice = mixed_part(content)
    index = choice["source_index"]
    if output is None or index >= len(output.parts):
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    part = output.parts[index]
    valid = part.kind == choice["kind"]
    if part.kind in {"text", "voice"}:
        valid = valid and content["text"] == clean(part.text, settings)
    elif part.kind == "meme":
        valid = valid and content["image"].get("candidate_id") == part.candidate_id
    elif part.kind == "generated_image":
        valid = valid and content["generated_image"].get("image_id") == part.image_id
    elif part.kind == "singing":
        valid = valid and choice["request_id"] == part.request_id
    if not valid:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403)
    return digest(output.model_dump(mode="json"))


def check_group(store, settings, clock, job, rows):
    output = agent_output.composition(store, job["event_id"])
    choices = [mixed_part(json.loads(row["content_json"])) for row in rows]
    indices = [choice["source_index"] for choice in choices]
    if indices != sorted(indices):
        raise CoreError("OWNER_DELIVERY_GROUP_CHANGED", 403)
    for index, part in enumerate(output.parts):
        selected = [choice for choice in choices if choice["source_index"] == index]
        if part.kind == "singing":
            manifest = agent_singing.selected(store, settings, clock, job, part.request_id)
            valid = [choice["singing_segment_index"] for choice in selected] == list(
                range(len(manifest["segments"])))
        else:
            valid = len(selected) in ({0, 1} if part.kind == "meme" else {1})
        if not valid:
            raise CoreError("OWNER_DELIVERY_GROUP_CHANGED", 403)
