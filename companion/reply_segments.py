"""Deterministic, lossless plain-text segmentation; no delivery or authority state."""

import re
from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class SegmentedReplyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_segments: Annotated[int, Field(strict=True, ge=2, le=2**31 - 1)] | Literal["unlimited"] = 3
    min_chars: int = Field(default=12, strict=True, ge=4, le=100)
    interval_ms: int = Field(default=1500, strict=True, ge=0, le=3000)
    mode: Literal["sentence", "semantic"] = "sentence"

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if self.mode == "sentence":
            value.pop("mode", None)  # Preserve already approved legacy scope bytes.
        return value


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_END = frozenset("。！？?!\r\n")
_PAIRS = dict(zip('“「『（([【{', '”」』）)]】}', strict=True))
SEMANTIC_RULES = (
    "\n用换行表示聊天气泡之间的语义停顿，紧密相关的句子放在同一行。"
    "一条完整回复可以只用一个气泡，气泡数量和长短随内容决定。"
    "解释、纠错和讲故事时保留完整意思，只在需要独立停顿处换行。"
    "引用、括号动作、代码和表格保持完整，不为增加气泡补写内容。"
    "只输出要发给主人的回复正文，不输出分段说明。"
)


def _boundaries(lines: list[str], *, newline_only=False, protect_pairs=False) -> Iterator[int]:
    """Scan each character once; an unclosed code span protects the remainder."""
    offset = 0
    fence: tuple[str, int] | None = None
    inline_width = 0
    pairs = []
    endings = frozenset("\r\n") if newline_only else _END
    for line in lines:
        marker = _FENCE.match(line) if not inline_width else None
        if fence is not None:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]
                and not marker[2].strip()):
                fence = None
                yield offset + len(line)
            offset += len(line)
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            offset += len(line)
            continue
        position = 0
        while position < len(line):
            char = line[position]
            if char == "`":
                end = position + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                width = end - position
                if not inline_width:
                    inline_width = width
                elif inline_width == width:
                    inline_width = 0
                position = end
                continue
            if inline_width:
                position += 1
                continue
            url = _URL.match(line, position) if char in "hHwW" else None
            if url:
                position = url.end()
                continue
            if protect_pairs:
                if pairs and char == pairs[-1]:
                    pairs.pop()
                elif char in _PAIRS:
                    pairs.append(_PAIRS[char])
                if pairs:
                    position += 1
                    continue
            if char in endings:
                position += 1
                while position < len(line) and line[position] in endings:
                    position += 1
                yield offset + position
            else:
                position += 1
        offset += len(line)


def split_text(text: str, policy: SegmentedReplyConfig) -> list[str]:
    """Split at safe pauses, retaining every character of nonblank input.

    Semantic mode uses explicit line breaks. Sentence mode joins short openings
    forward and short tails backward, counting non-whitespace characters.
    Excess segments join backward. Blank input has no sendable part.
    A table stays in one message. Time and interval configuration do not affect text.
    Work and temporary storage are linear in the original reply length.
    """
    if not text.strip():
        return []
    lines = text.splitlines(keepends=True)
    if any(line.lstrip().startswith("|") for line in lines):
        return [text]
    semantic = policy.mode == "semantic"
    boundaries = _boundaries(lines, newline_only=semantic, protect_pairs=semantic)
    minimum = 1 if semantic else policy.min_chars
    ends = []
    previous = count = 0
    for end in boundaries:
        count += sum(not char.isspace() for char in text[previous:end])
        previous = end
        if count >= minimum:
            ends.append(end)
            count = 0
        elif not count and ends:
            ends[-1] = end
    # The final unterminated sentence may itself be long enough to stand alone.
    count += sum(not char.isspace() for char in text[previous:])
    if count >= minimum or not ends:
        ends.append(len(text))
    else:
        ends[-1] = len(text)
    if policy.max_segments != "unlimited" and len(ends) > policy.max_segments:
        ends = ends[:policy.max_segments - 1] + [len(text)]
    parts, start = [], 0
    for end in ends:
        parts.append(text[start:end])
        start = end
    return parts
