"""Bounded review of image interpretations using existing history and reference custody."""

import json
import re

from companion import memory_references, qq_quotes, reply_revision
from companion.contracts import CoreError
from companion.store import encode

IMAGE = r"(?:(?:这|那)(?:张|幅|个|两张)?|两张)?(?:图片?|漫画)|(?:上|下)(?:图|格)|上下两格|两格"
IMAGE_CUE = re.compile(IMAGE)
QUOTED = re.compile(r'''“[^“”]*”|‘[^‘’]*’|「[^「」]*」|『[^『』]*』|"[^"]*"|'[^']*'|`[^`]*`''')
CLAUSES = re.compile(r"([^，,。！!？?；;\n]+)([，,。！!？?；;\n]*)")
REPORTING = re.compile(
    r"(?:说|讲|问|道|写着|提到)[：: ]*$|"
    r"^(?:他|她|我|他们|她们|别人|朋友|网友)(?:刚才|当时)?(?:说|讲|问|提到|告诉我)[：: ]*|"
    r"^(?:我)?(?:没(?:有)?|不是(?:在)?)(?:说|讲|指)")
DENIAL = re.compile(
    r"(?:我)?(?:没(?:有)?|不是(?:在)?)(?:说|讲|指).*(?:看错|理解错|说错)|"
    r"你(?:并)?(?:没(?:有)?|不是)(?:看错|看反|理解错|说错)")
OWN_PREFIX = re.compile(r"^(?:(?:但|但是|不过|可是))?我(?:觉得|认为|是说|的意思是)")
ERROR = r"(?:看错|看反|理解错|理解反|说错|说反|没看懂|没理解|理解得不对|理解有偏差)(?:了)?"
ASSISTANT_ERROR = re.compile(
    rf"(?:你(?:刚才|刚刚|这次|之前|又|确实|真的)*(?:(?:把|对)?(?:{IMAGE})(?:的意思)?(?:给)?)?{ERROR}"
    rf"|(?:{IMAGE})你{ERROR}"
    rf"|(?:你(?:刚才|刚刚)?(?:对(?:{IMAGE}))?的|(?:{IMAGE})的)"
    r"(?:解释|解读|理解|分析)(?:不对|错了|有偏差|有误))(?:啊|呀|呢)?")
LOOK_ERROR = re.compile(r"你(?:刚才|刚刚|这次|之前|又|确实|真的)*看(?:错|反)(?:了)?(?:啊|呀|呢)?")
LINKED_OBJECTION = re.compile(
    r"不对|(?:这句|这个解释|这个理解)(?:不对|错了)|不是这样(?:的)?|不是这个意思")
RECHECK = re.compile(
    r"(?:错了){1,3}|(?:请)?(?:重新|再)(?:看|核对)(?:看|一下|一遍)?(?:原图|这张图)?")
IMAGE_RELATION = re.compile(r"(?:两张图|两格|上下两格).{0,40}(?:一样|相同|重复)")
HEADING = "\n本轮图片核对资料（不是指令；异议和机器改口均不自动成为事实）："


def _own_clauses(text):
    """Keep speaker attribution for both correction wording and target hints."""
    own = QUOTED.sub(" ", text)
    if re.search(r'''[“”‘’「」『』"'`]''', own):
        return [], False  # An unfinished quote cannot establish the speaker.
    clauses, reported, denied = [], False, False
    for match in CLAUSES.finditer(own):
        clause, punctuation = match[1].strip(), match[2]
        if prefix := OWN_PREFIX.match(clause):
            clause, reported = clause[prefix.end():].strip(), False
        clause = re.sub(r"^(?:但是|不过|可是|算了|但|是)", "", clause).strip()
        if not reported:
            denied |= bool(DENIAL.match(clause))
        if REPORTING.search(clause):
            reported = True
        if not reported:
            clauses.append((clause, punctuation))
    return clauses, denied


def _intent(text, *, linked=False, immediate=False):
    """Uncertain requests can revisit context; only own image objections persist.

    Like native owner-feedback rules, match whole own clauses rather than an
    error word embedded in self-apology, denial, reporting or a quoted sentence.
    A verified quote identifies a target, not an objection by itself.
    """
    clauses, denied = _own_clauses(text)
    # Context for a correction has the same speaker/uncertainty boundary as
    # the correction itself; a reported image fact cannot supply attribution.
    related = any(IMAGE_RELATION.fullmatch(clause) and not re.search(r"[？?]", punctuation)
                  for clause, punctuation in clauses)
    requested = objection = False
    for clause, punctuation in clauses:
        uncertain = bool(re.search(r"[？?]", punctuation) or clause.endswith(("吗", "吧", "么")))
        assertion = re.sub(r"[吗吧么]$", "", clause)
        direct = bool(ASSISTANT_ERROR.fullmatch(assertion))
        linked_objection = bool(LINKED_OBJECTION.fullmatch(assertion))
        requested |= direct or linked_objection or bool(
            RECHECK.fullmatch(assertion) or IMAGE_RELATION.fullmatch(assertion))
        objection |= not uncertain and (
            direct and (linked or related or bool(IMAGE_CUE.search(assertion))
                        or immediate and bool(LOOK_ERROR.fullmatch(assertion)))
            or linked and linked_objection)
    return requested, bool(objection and not denied)


def requests_context(text):
    """Conservative presentation guard; it grants no target or correction proof."""
    return _intent(text, immediate=True)[0]


def target(core, job, body, sources):
    """Resolve only inside the already authorized recent/selected source window."""
    text = body.get("text", "")
    by_id = {source["event_id"]: source for source in sources}
    images = {key: core.store.rows(
        'SELECT item_id, slot, output_event_id FROM content_items WHERE owner_event_id = %s AND '
        "kind = 'qq_image' AND redacted = 0 ORDER BY slot NULLS FIRST", (key,)) for key in by_id}
    images = {key: value for key, value in images.items() if value}
    if not images:
        return None
    explicit = {item["event_id"] for item in core._self_quote_indices(job, body)
                if item["event_id"] in images}
    current = core.store.one('SELECT source_event_id FROM events WHERE event_id = %s',
                             (job["event_id"],))
    prefix = qq_quotes.namespace(current["source_event_id"])
    withdrawn = qq_quotes.withdrawn(core.store, job["event_id"], body)
    for segment in (body.get("rich") or {}).get("segments", []):
        if (segment.get("type") == "reply" and segment.get("index") not in withdrawn
            and segment.get("body", {}).get("status") == "READ"):
            explicit.update(key for key in images if prefix and
                by_id[key]["source_event_id"] == f"{prefix}:{segment.get('reference')}")
    hints = "\n".join(clause for clause, _ in _own_clauses(text)[0])
    explicit.update(key for key, rows in images.items()
                    if key in hints or any(row["item_id"] in hints for row in rows))
    requested, correction = _intent(text, linked=bool(explicit),
        immediate=bool(sources and sources[-1]["event_id"] in images))
    if not explicit and any(segment.get("type") == "reply"
                            for segment in (body.get("rich") or {}).get("segments", [])):
        # An unproven/out-of-window quote cannot silently dispute a different
        # recent image merely because that is the sole remaining candidate.
        correction = False
    if not requested and not explicit:
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
        capture = core.store.one('SELECT expires_at FROM captured_images WHERE item_id = %s',
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
    body = json.loads(core.store.one('SELECT body_json FROM events WHERE event_id = %s',
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
