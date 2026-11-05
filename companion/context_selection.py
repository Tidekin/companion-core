"""Trusted, local context priorities and exact evidence-backed fact coalescing.

This selects presentation only. Original proofs, permissions and hard admission
remain the responsibility of Core and the request planner.
"""

import re
import unicodedata

from companion.store import encode

MOOD, BACKGROUND, OLD_HISTORY, RELATED, TOPIC = range(5)
_FILLER = {"这个", "那个", "什么", "怎么", "一下", "今天", "最近", "觉得", "看看", "内容"}
_CONTINUATION = re.compile(
    r"^(嗯+|哈哈+|哦+|好[的吧]?|继续[说呢]?|然后呢|你觉得呢|这个呢)[。！？!? ]*$")


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


def focus(current, sources):
    """Explicit topic wins; short continuations inherit the last actual owner turn."""
    if sources and (not current.strip() or _CONTINUATION.fullmatch(current.strip())):
        return terms(sources[-1].get("user_text", ""))
    return terms(current)


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
