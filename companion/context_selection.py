"""Trusted, local context priorities and exact evidence-backed fact coalescing.

This selects presentation only. Original proofs, permissions and hard admission
remain the responsibility of Core and the request planner.
"""

import json
import re
import unicodedata
from dataclasses import dataclass

from companion.store import encode

MOOD, BACKGROUND, OLD_HISTORY, RELATED, TOPIC = range(5)
_FILLER = {"这个", "那个", "什么", "怎么", "一下", "今天", "最近", "觉得", "看看", "内容"}
_CONTINUATION = re.compile(
    r"^(嗯+|哈哈+|哦+|好[的吧]?|继续[说呢]?|然后呢|你觉得呢|这个呢)[。！？!? ]*$")
_SHARE_BACKREF = re.compile(
    r"刚才|刚刚|之前|以前|以往|先前|前面|上次|上一[张条个幅]|接着|继续|再看|换了|"
    r"还记得|对比|比较|就是|那[件个张]事?|这个呢|然后呢|"
    r"\b(?:earlier|previous|before|continue|compare)\b", re.IGNORECASE)
_NEW_TOPIC = re.compile(
    r"换[个下]?话题|(?:和|跟).{0,30}(?:无关|没(?:有)?关系)|\bunrelated\b", re.IGNORECASE)
_SHARE_FUNCTION_PREFIX = frozenset("我你他她它这那的一们是在有也就都还又和与个很不么一")
_SHARE_GENERIC = frozenset({
    "图片", "照片", "画面", "看着", "张图", "一起", "想要", "希望", "可以", "应该",
    "the", "and", "you", "your", "this", "that", "with", "for", "are", "look", "at", "of",
    "image", "picture", "photo",
})


def terms(text):
    # Same bounded CJK-bigram/Latin-word approach used by local media selection.
    words = re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", unicodedata.normalize(
        "NFKC", text).casefold())
    result = set()
    for word in words:
        if "\u3400" <= word[0] <= "\u9fff":
            result.update(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                result.add(word)
        elif len(word) > 1:
            result.add(word)
    return result - _FILLER


def focus(current, sources, *, shared=False):
    """Explicit topic wins; short continuations inherit the last actual owner turn."""
    if not shared and sources and (
        not current.strip() or _CONTINUATION.fullmatch(current.strip())
    ):
        return terms(sources[-1].get("user_text", ""))
    return terms(current)


def share_continuation(text):
    """Only current owner text supplies discourse cues, never image/OCR text."""
    return not _NEW_TOPIC.search(text) and bool(_SHARE_BACKREF.search(text))


def current_share(reference, event_id):
    """Keep current-source excerpts; older observations cannot supply its topic."""
    if not reference:
        return "", set()
    data = json.loads(reference)
    items = [item for item in data["shared_content"]
             if item.get("owner_event_id") == event_id]
    return (encode({"shared_content": items}) if items else "",
            terms(" ".join(item.get("text", "") for item in items)))


def share_history(sources, topic):
    """Select source groups by owner content, retaining the current unanswered tail.

    Assistant guesses cannot nominate the old owner turn as related. Original
    storage, relationships and permission/source checks are unchanged.
    """
    completed = max((index for index, source in enumerate(sources)
                     if source.get("reply_text") and source.get("reply_complete") is not False),
                    default=-1)
    # Cancelled/failed inputs from an earlier completed exchange are ordinary
    # history. Keeping them pending forever would reopen unrelated old topics.
    return [source for index, source in enumerate(sources)
            if (index > completed and (source.get("reply_complete") is False
                                       or not source.get("reply_text")))
            or _share_related(topic, source.get("user_text", ""))]


def _share_related(topic, text):
    # Soft ranking may use a weak match; selecting old raw dialogue needs more.
    # Timestamps, pronouns and stock sharing phrases do not name the new topic.
    common = {term for term in topic & terms(text)
              if not term.isdecimal() and term not in _SHARE_GENERIC
              and term[0] not in _SHARE_FUNCTION_PREFIX}
    return len(common) >= 2


def relevance(focus_terms, text):
    return RELATED if focus_terms & terms(text) else BACKGROUND


def drop_order(groups):
    """One stable order for assembly and the final SDK request, oldest ties first."""
    return sorted((group for group in groups if not group["required"]),
                  key=lambda group: (group["priority"], group["wire_start"]))


def protected_recent(settings, sources, turns, current_input, scenario):
    """Keep a bounded tail of complete pairs alongside the current input.

    Pending inputs do not consume the pair count. Oversized pairs remain optional;
    static rules, attached media and actual tools still face full hard admission.
    """
    from companion.token_measurement import measure_request

    pairs = [(source, turn) for source, turn in zip(sources, turns, strict=True)
             if source.get("reply_text") and source.get("reply_complete") is not False]
    retained, messages = set(), list(current_input)
    for source, turn in reversed(pairs[-settings.recent_protected_turns:]):
        # turn_messages prepends source references (cards, quotes or observations)
        # to the pair. Keep that richer group atomic under its normal budget.
        if len(turn) != 2:
            continue
        measured = measure_request(settings, [*turn, *messages], scenario)
        if measured.reserved_input_tokens <= settings.context_soft_tokens:
            retained.add(source["event_id"])
            messages = [*turn, *messages]
    return retained


def _without_fact(value, text):
    if isinstance(value, list):
        return [_without_fact(item, text) for item in value]
    if isinstance(value, dict):
        return {key: _without_fact(item, text) for key, item in value.items()
                if not (key in {"content", "take", "verified_result"}
                        and isinstance(item, str) and item.strip() == text)}
    return value


class PromptRules:
    """Keep the static prefix before turn-specific guidance, including rules added after fitting."""

    def __init__(self, static, dynamic):
        self.static = static
        self.dynamic = dynamic
        self.message = {"role": "system", "content": static + "".join(dynamic)}

    def append(self, text, *, dynamic=False):
        if dynamic:
            self.dynamic.append(text)
        else:
            self.static += text
        self.message["content"] = self.static + "".join(self.dynamic)


class References:
    """Coalesce only exact claims with the same subject and overlapping evidence.

    Distinct claims, conflicting values and each representation's status survive.
    All proofs and sources stay attached to the resulting atomic message so later
    pruning cannot leave dangling fact references or release source responsibility.
    """

    def __init__(self):
        self.messages, self.sources, self.proofs = [], [], []
        self.priorities, self.required = {}, set()
        self._facts, self._evidence = {}, {}

    def add(self, heading, data, sources, *, priority=BACKGROUND, required=False,
            proofs=(), fact=None, module="reference"):
        sources = set(sources)
        key = (fact[0], fact[1].strip()) if fact and fact[1].strip() else None
        message = next((item for item in self._facts.get(key, ())
                        if sources & self._evidence[id(item)]), None) if key else None
        if message is None:
            message = {"role": "user", "content": heading + "\n" + encode(data)}
            self.messages.append(message)
            self._evidence[id(message)] = set(sources)
            self.sources.append((message, set(sources)))
            self.priorities[id(message)] = priority
            if key:
                self._facts.setdefault(key, []).append(message)
        else:
            message["content"] += "\n同一事实的其他来源记录（数据）：\n" + encode({
                "module": module, "record": _without_fact(data, key[1])})
            self._evidence[id(message)].update(sources)
            self.priorities[id(message)] = max(self.priorities[id(message)], priority)
        # Full original lineage stays pinned even when its display is coalesced.
        evidence = next(ids for item, ids in self.sources if item is message)
        evidence.update(sources)
        for proof in proofs:
            if proof["kind"] == "memory":
                evidence.update(proof["proof"]["source_event_ids"])
            elif proof["kind"] == "watch":
                evidence.update(proof["proof"]["sources"]["source_event_ids"])
        if required:
            self.required.add(id(message))
        self.proofs.extend((message, proof) for proof in proofs)
        return message

    def include(self, message, sources, *, proof=None):
        """Attach an already-rendered reference without changing its original wording."""
        self.messages.append(message)
        self.sources.append((message, sources))
        if proof is not None:
            self.proofs.append((message, proof))

    def retained(self, messages, initial_proofs):
        visible = {id(message) for message in messages}
        sources = {key for ref, ids in self.sources if id(ref) in visible for key in ids}
        proofs = list(initial_proofs)
        for ref, proof in self.proofs:
            if id(ref) in visible and proof not in proofs:
                proofs.append(proof)
        return sources, proofs


@dataclass
class FittedReply:
    messages: list
    blocks: list
    dropped: list

    def retained_blocks(self):
        visible = {id(message) for message in self.messages}
        return [(kind, block, required, priority) for kind, block, required, priority in self.blocks
                if id(block[0]) in visible]


def fit_reply(settings, kind, rules, references, history, current_input, related, *, protected):
    """Keep each source group atomic; reuse the same priorities at final SDK admission."""
    from companion.dialogue_history import fit_context

    preserve = bool(protected or history.continuation)
    recent = protected_recent(settings, history.sources, history.turns, current_input, kind)
    blocks = [("rules", [rules], True, TOPIC)]
    for message in references.messages:
        blocks.append(("reference", [message], preserve or id(message) in references.required,
                       references.priorities.get(id(message), BACKGROUND)))
    target_id = history.review_target and history.review_target.get("event_id")
    for source, turn in zip(history.sources, history.turns, strict=True):
        event_id = source["event_id"]
        required = preserve or source.get("reply_complete") is False or event_id in recent
        on_topic = related(source.get("user_text", "")) == RELATED
        priority = TOPIC if on_topic or event_id == target_id or event_id in recent else OLD_HISTORY
        blocks.append(("history", turn, required, priority))
    blocks.append(("current", current_input, True, TOPIC))
    dropped = []
    if protected:
        # A signed window already bounded its reviewed sources. Hard admission still applies.
        messages = [message for _, group, _, _ in blocks for message in group]
    else:
        messages = fit_context(rules, references.messages, history.turns, current_input,
                               settings.context_soft_tokens, settings=settings, scenario=kind,
                               blocks=blocks, dropped=dropped)
    return FittedReply(messages, blocks, dropped)
