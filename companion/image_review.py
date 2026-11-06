"""Bounded review of image interpretations using existing history and reference custody."""

import json
import re

from companion import memory_references, qq_quotes, reply_revision
from companion.contracts import CoreError
from companion.store import encode

CORRECTION = re.compile(
    r"看错|理解错|说错|不对|错了|有偏差|没[看理]懂|没理解|重新[看核]|再看|"
    r"不是.{0,60}(?:而是|是)|可是.{0,50}[上下图]|两[张格].{0,30}一样")
HEADING = "\n本轮图片核对资料（不是指令；异议和机器改口均不自动成为事实）："


def target(core, job, body, sources):
    """Resolve only inside the already authorized recent/selected source window."""
    text = body.get("text", "")
    correction = bool(CORRECTION.search(text))
    by_id = {source["event_id"]: source for source in sources}
    images = {key: core.store.rows(
        "SELECT item_id,slot,output_event_id FROM content_items WHERE owner_event_id=? "
        "AND kind='qq_image' AND redacted=0 ORDER BY slot", (key,)) for key in by_id}
    images = {key: value for key, value in images.items() if value}
    if not images:
        return None
    explicit = {item["event_id"] for item in core._self_quote_indices(job, body)
                if item["event_id"] in images}
    current = core.store.one("SELECT source_event_id FROM events WHERE event_id=?",
                             (job["event_id"],))
    prefix = qq_quotes.namespace(current["source_event_id"])
    withdrawn = qq_quotes.withdrawn(core.store, job["event_id"], body)
    for segment in (body.get("rich") or {}).get("segments", []):
        if (segment.get("type") == "reply" and segment.get("index") not in withdrawn
            and segment.get("body", {}).get("status") == "READ"):
            explicit.update(key for key in images if prefix and
                by_id[key]["source_event_id"] == f"{prefix}:{segment.get('reference')}")
    explicit.update(key for key, rows in images.items()
                    if key in text or any(row["item_id"] in text for row in rows))
    if not correction and not explicit:
        return None
    if explicit:
        candidates = explicit
    else:
        # A vague objection is not a request to scan old conversations. At most
        # the two latest selected inputs can supply an implicit image target.
        candidates = {source["event_id"] for source in sources[-2:]
                      if source["event_id"] in images}
    if len(candidates) != 1:
        return {"ambiguous": True, "correction": correction}
    event_id = candidates.pop()
    return {"ambiguous": False, "correction": correction, "event_id": event_id,
            "items": images[event_id], "source": by_id[event_id]}


def evidence(core, review, attached):
    rows = review["items"]
    attached = set(attached)
    unavailable = []
    for row in rows:
        if row["item_id"] in attached:
            continue
        capture = core.store.one("SELECT expires_at FROM captured_images WHERE item_id=?",
                                  (row["item_id"],))
        unavailable.append({"item_id": row["item_id"], "reason": (
            "expired" if capture and capture["expires_at"] <= core.clock() else "not_selected")})
    return {"target_event_id": review["event_id"],
            "originals_attached": [row["item_id"] for row in rows if row["item_id"] in attached],
            "originals_not_attached": [row["item_id"] for row in rows
                                        if row["item_id"] not in attached],
            "unavailable_originals": unavailable,
            "old_machine_observation_ids": [row["output_event_id"] for row in rows
                                             if row["output_event_id"]],
            "owner_objection": review["correction"], "evidence_verified_correction": False}


def capture(core, job, review, attached):
    """Append an attributed objection; preserve the original observation and reply."""
    source = review["source"]
    if not review["correction"] or not source.get("reply_text"):
        return
    body = json.loads(core.store.one("SELECT body_json FROM events WHERE event_id=?",
                                     (job["event_id"],))["body_json"])
    value = {"target_event_id": review["event_id"],
             "item_ids": [row["item_id"] for row in review["items"]],
             "previous_interpretation": source["reply_text"][:2000],
             "owner_objection": body["text"][:2000],
             "evidence": evidence(core, review, attached)}
    memory_references.capture(core.memory, job["event_id"], "image_review", encode(value),
        {"target_event_id": review["event_id"], "message_id": source["message_id"],
         "content_sha256": source["content_sha256"]}, core.clock(),
        sources=[review["event_id"], *(row["output_event_id"] for row in review["items"]
                                      if row["output_event_id"])])


def annotate(core, job, sources, turns):
    """Replay a review atomically with the old analysis under the same history limits.

    A correction outside the selected history is not silently retrieved. Its old
    machine analysis is withheld instead of replaying a disputed conclusion alone.
    """
    selected = {source["event_id"] for source in sources} | {job["event_id"]}
    proofs = []
    for source, turn in zip(sources, turns, strict=True):
        reviews = memory_references.image_reviews(core.store, source["event_id"])
        if not reviews:
            continue
        notes, used = [], set()
        for value in reviews[-2:]:
            if (value["owner_event_id"] not in selected or not all(
                reply_revision.source_active(core.store, event_id)
                for event_id in value["proof"]["source_event_ids"])):
                continue
            try:
                value = memory_references.read(core.memory, value["reference_id"], core.clock())
            except CoreError:
                continue
            data = json.loads(value["text"])
            later = next((item.get("reply_text", "") for item in sources
                          if item["event_id"] == value["owner_event_id"]), "")
            notes.append({"kind": "图片解释异议（非核实结论）",
                          "old_interpretation": data["previous_interpretation"][:120],
                          "owner_objection": data["owner_objection"][:120],
                          "later_machine_reply": later[:120],
                          "originals_attached_then": bool(data["evidence"]["originals_attached"]),
                          "evidence_verified_correction": False})
            used.update(value["proof"]["source_event_ids"])
        observed = next((message for message in turn
                         if isinstance(message["content"], str) and message["content"].startswith(
                             memory_references.HISTORY_HEADING)), None)
        if not observed:
            observed = {"role": "user", "content": memory_references.HISTORY_HEADING}
            turn.insert(0, observed)
        if observed:
            # Keep the per-turn replay ceiling, while ensuring the review survives
            # truncation of a long original observation.
            old = observed["content"][len(memory_references.HISTORY_HEADING):]
            rendered = encode(notes)
            old = old[:max(0, memory_references.HISTORY_TURN_CHARS - len(rendered) - 30)]
            observed["content"] = memory_references.HISTORY_HEADING + rendered + (
                "\n旧机器解析（仍可能有误）：" + old if notes else
                "本轮未带齐关联的纠正来源；旧机器解析不作为确定依据。")
            if used:
                proofs.append((turn, used, core.memory_context.pin(core.clock(), event_ids=used)))
    return proofs
