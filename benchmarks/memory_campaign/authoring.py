"""Helpers for writing a bank: a round's builder only defines personas and stories.

A story is one fact group: its sources (turns), one question, required/forbidden criteria
and optional distractor turns. Each story gets its own slot window and its own event
names (the builder must not reuse an event across stories, or later stories silently
change an earlier story's answer). The helper places sources, distractors and fillers,
derives cutoffs and question times, and writes the suite-v1 layout that bank.py checks.
"""

import bisect
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
MEDIA_SUFFIX = {"image": "png", "file": "txt", "voice": "wav", "relative_voice": "wav"}


@dataclass
class Source:
    text: str
    mode: str = "text"            # text|file|image|voice|relative_voice|quote|forward|assistant
    reply: str | None = None       # assistant's recorded reply (mode=assistant)
    visual: dict | None = None     # image spec for a drawer (mode=image)
    quoted: dict | None = None     # {"text", "name", "uin"}: who originally said it


@dataclass
class Ask:
    """A further question on a story's sources; its category id is the story's + suffix."""
    suffix: str
    category: str
    question: str
    required: list
    forbidden: list = field(default_factory=list)
    exact: list = field(default_factory=list)


@dataclass
class Story:
    category_id: str
    category: str
    family: str
    sources: list
    question: str
    required: list
    forbidden: list = field(default_factory=list)
    distractors: list = field(default_factory=list)
    # Optional. exact: [[alternative, ...], ...]: each group must appear verbatim in an
    # answer (a mechanical check reported beside the judge). strata: {name: value} for
    # breakdowns such as chain length. offsets/window: this story's own source spacing.
    # asks: more questions on the same sources. "{S2}" in any question or criterion text
    # becomes the local date (M月D日) of source 2.
    exact: list = field(default_factory=list)
    strata: dict = field(default_factory=dict)
    offsets: tuple | None = None
    window: int | None = None
    asks: list = field(default_factory=list)


def _dated(value, dates):
    if isinstance(value, list):
        return [_dated(item, dates) for item in value]
    for index, date in enumerate(dates, 1):
        value = value.replace(f"{{S{index}}}", date)
    assert "{S" not in value, value
    return value


def chain(category_id, family, values, *, first, change, current_q, first_q, on_q, all_q,
          relayed=None, distractors=(), gap=4, interference="none"):
    """A version chain: the owner states a value, then corrects it len(values) - 1 times.

    first(v) / change(v) give the owner's messages; the *_q are the questions (on_q may
    use "{S2}" etc.). relayed=(name, value, text) adds a later third-party forward that
    the owner never confirms: the answer must give it as the newer, attributed claim and
    keep the owner's last value. Sources sit `gap` slots apart (two slots per day), so
    longer chains also span more time. Strata record chain length and interference.
    """
    sources = [Source(first(values[0]))] + [Source(change(v)) for v in values[1:]]
    last = values[-1]
    current_required = [f"现在是{last}"]
    current_forbidden = [f"现在是{v}" for v in dict.fromkeys(values[:-1]) if v != last]
    current_exact = [[last]]
    if relayed:
        name, value, text = relayed
        sources.append(Source(f"{name}转来的，你看看。", "forward",
                              quoted={"text": text, "name": name, "uin": "10008"}))
        current_required = [f"较新的说法是{value}，来自{name}转来的消息", f"主人自己最后说的是{last}",
                            "主人没有确认这个改动"]
        current_forbidden = [f"主人自己确认改成了{value}"]
        current_exact = [[value], [last]]
    middle = len(values) // 2
    asks = [Ask("H", "版本链·最早的值", first_q, [f"最早是{values[0]}", "后来改过"],
                [f"最早就是{last}"] if last != values[0] else [], [[values[0]]]),
            Ask("T", "版本链·某天的值", on_q.replace("{K}", f"{{S{middle + 1}}}"),
                [f"那天定的是{values[middle]}"], [], [[values[middle]]]),
            Ask("C", "版本链·变更顺序", all_q,
                ["依次是" + "→".join(values) + (f"，之后{relayed[0]}转来说是{relayed[1]}"
                                                if relayed else ""),
                 f"主人自己改过{len(values) - 1}次"], ["顺序颠倒"], [[v] for v in dict.fromkeys(values)])]
    return Story(category_id, "版本链·当前值", family, sources, current_q, current_required,
                 current_forbidden, list(distractors), exact=current_exact,
                 strata={"chain_len": len(values), "interference": interference,
                         "relayed": bool(relayed)},
                 offsets=tuple(range(0, gap * len(sources), gap)),
                 window=gap * len(sources) + len(distractors) + 1, asks=asks)


def slot_time(start, slot):
    """Two owner turns a day: 09:20 and 20:00 local."""
    return start + timedelta(days=slot // 2, hours=9 if slot % 2 == 0 else 20,
                             minutes=20 if slot % 2 == 0 else 0)


def write_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), "utf-8")


def build(bank, personas, *, start, slots=360, window=10, offsets=(0, 3, 5, 7),
          delays=(0, 3, 15, 45), question_minutes=15, fillers=(), replies=("好的。",),
          draw_image=None, default_reply="收到，我记下了。",
          caption="这是我补充的一份资料，请按实际内容读取。"):
    """personas: {persona_id: [Story, ...]}. Returns a summary; writes bank/suite-v1."""
    suite = Path(bank) / "suite-v1"
    for directory in ("assets", "inputs", "review-only"):
        (suite / directory).mkdir(parents=True, exist_ok=False)
    questions, criteria, audio, provenance = [], [], [], []
    for p_index, (persona, stories) in enumerate(personas.items()):
        used, groups = {}, []
        # Story k starts after its predecessors' windows; with one shared window this is
        # the original spacing 2 * (5 + window // 2 * k).
        base = 10
        for k, story in enumerate(stories, 1):
            span = story.window or window
            base += (story.window or window) if k == 1 else (stories[k - 2].window or window)
            plan = [base + offset for offset in (story.offsets or offsets)]
            assert len(story.sources) <= len(plan), f"{persona} {story.category_id}: too many sources"
            assert max(plan) - base < span, "slot window overlaps the next story"
            ids, modes = [], []
            for j, (slot, source) in enumerate(zip(plan, story.sources, strict=False)):
                sid = f"{persona}-{story.category_id}-S{j + 1}"
                asset = None
                if source.mode in MEDIA_SUFFIX:
                    asset = f"assets/{sid}.{MEDIA_SUFFIX[source.mode]}"
                    if source.mode == "image":
                        draw_image(suite / asset, source.visual)
                    elif source.mode == "file":
                        (suite / asset).write_text(source.text + "\n", "utf-8")
                    else:
                        audio.append({"asset": asset, "text": source.text, "source_id": sid})
                item = {"id": sid, "now": slot_time(start, slot).isoformat(),
                        "modality": source.mode, "text": "" if asset else source.text,
                        "asset": asset, "caption": caption if asset else None,
                        "assistant_reply": source.reply or default_reply}
                if source.quoted:
                    item["quoted"] = source.quoted
                assert slot not in used
                used[slot] = item
                ids.append(sid)
                modes.append("voice" if source.mode == "relative_voice" else source.mode)
                provenance.append({"source_id": sid, "author_text": source.text,
                                   "quoted": source.quoted, "visual": source.visual,
                                   "inference_uses_author_text_for_media": False})
            free = [s for s in range(base, base + span) if s not in used]
            for d_index, text in enumerate(story.distractors):
                slot = free[d_index]
                used[slot] = {"id": f"{persona}-{story.category_id}-X{d_index + 1}",
                              "now": slot_time(start, slot).isoformat(), "modality": "text",
                              "text": text, "asset": None, "caption": None,
                              "assistant_reply": default_reply}
            last = slot_time(start, plan[len(story.sources) - 1])
            dates = [f"{t.month}月{t.day}日" for t in (slot_time(start, slot)
                                                      for slot in plan[:len(story.sources)])]
            groups.append((story, ids, sorted(set(modes)), last, dates))
        history = []
        for slot in range(slots):
            item = used.get(slot) or {
                "id": f"{persona}-D{slot // 2:03}-{slot % 2}",
                "now": slot_time(start, slot).isoformat(), "modality": "text",
                "text": fillers[(slot * 7 + p_index * 11) % len(fillers)],
                "asset": None, "caption": None,
                "assistant_reply": replies[(slot + p_index) % len(replies)]}
            history.append({"turn": slot + 1, **item})
        times = [datetime.fromisoformat(r["now"]) for r in history]
        for story, ids, modes, last, dates in groups:
            asks = [Ask("", story.category, story.question, story.required, story.forbidden,
                        story.exact), *story.asks]
            for ask in asks:
                cid = story.category_id + ask.suffix
                for delay in delays:
                    when = last + timedelta(days=delay, minutes=question_minutes)
                    assert when <= times[-1] + timedelta(days=1), "question beyond history end"
                    qid = f"{persona}-{cid}/d{delay:02}"
                    questions.append({"id": qid, "persona": persona,
                                      "group_id": f"{persona}-{story.category_id}",
                                      "now": when.isoformat(),
                                      "cutoff_turn": bisect.bisect_right(times, when),
                                      "question": _dated(ask.question, dates), "source_ids": ids,
                                      "category": ask.category, "category_id": cid,
                                      "family": story.family, "delay_days": delay,
                                      "kind": "semantic_memory", "input_modalities": modes})
                    row = {"id": qid, "required": _dated(ask.required, dates),
                           "forbidden": _dated(ask.forbidden, dates), "source_ids": ids}
                    if ask.exact:
                        row["exact"] = [_dated(group, dates) for group in ask.exact]
                    if story.strata:
                        row["strata"] = story.strata
                    criteria.append(row)
        write_rows(suite / "inputs" / f"{persona}.jsonl", history)
    write_rows(suite / "questions.jsonl", questions)
    write_rows(suite / "review-only/criteria.jsonl", criteria)
    write_rows(suite / "review-only/source-authoring-provenance.jsonl", provenance)
    write_rows(Path(bank).parent / "audio-to-synthesize.jsonl", audio)
    return {"semantic": len(questions), "groups": len({q["group_id"] for q in questions}),
            "audio_pending": len(audio), "authored_at": datetime.now(UTC).isoformat(),
            "modalities": dict(Counter(m for q in questions for m in q["input_modalities"]))}


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
