"""Conservative explicit situations; no emotion diagnosis or extra model selector."""

import re

from companion.affect import NOT_OWN_REQUEST, _feedback_clause, _owner_feedback, owner_feedback_text

NAMES = {"倾诉": "venting", "吐槽": "venting", "开玩笑": "joking", "讨论": "discussion",
         "认真求助": "help", "正式求助": "help"}
SCOPED = re.compile(r"(?:我)?(倾诉|吐槽|开玩笑|讨论|认真求助|正式求助)(?:时|的时候)"
                    r"[，,：: ]*(.+?)[。！! ]*$")
CONTINUATION = re.compile(
    r"(?:嗯+|哈哈+|哦+|好[的吧]?|继续[说呢]?|然后呢|你觉得呢|这个呢)[。！？!? ]*")
MARKERS = (
    (r"^(?:我)?(?:想|要|在)?(?:倾诉|吐槽)", "venting"),
    (r"^(?:我)?(?:只是)?(?:在|想)?开玩笑", "joking"),
    (r"^(?:我们|我想|想)?讨论", "discussion"),
    (r"^(?:我)?(?:想|要)?(?:认真|正式)求助|^(?:请)?(?:帮我|教我|解释一下|怎么|如何)", "help"),
)


def feedback(store, settings, event_id, now):
    value = owner_feedback_text(store, settings, event_id, now)
    if value is None:
        return None
    event, text = value
    if general := _owner_feedback(event_id, event, text):
        return general
    match = SCOPED.fullmatch(text)
    if not match or NOT_OWN_REQUEST.search(text.replace("开玩笑", "")):
        return None
    parsed = _feedback_clause(match[2])
    if not parsed or parsed[1][1] not in {"interaction_tone", "cognitive_load"}:
        return None
    _, (_, dimension, state, cause) = parsed
    return {"event_id": event_id, "dimension": dimension, "new_state": state,
            "situation": NAMES[match[1]], "reason_code": cause,
            "created_at": event["ingested_at"], "occurred_at": event["occurred_at"]}


def situation(text):
    if NOT_OWN_REQUEST.search(text.replace("开玩笑", "")) or SCOPED.fullmatch(text):
        return None
    return next((name for pattern, name in MARKERS if re.search(pattern, text)), None)


def context(store, settings, job, now, sources):
    value = owner_feedback_text(store, settings, job["event_id"], now)
    if value is None:
        return None, []
    _, text = value
    if current := situation(text):
        return current, []
    if CONTINUATION.fullmatch(text) and sources:
        # Only caller-authorized history can resolve a short continuation. A new
        # ordinary turn never silently inherits a situation from an older topic.
        key = sources[-1]["event_id"]
        prior = owner_feedback_text(store, settings, key, now)
        if prior and (current := situation(prior[1])):
            return current, [key]
    return None, []
