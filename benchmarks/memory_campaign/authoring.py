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
class Story:
    category_id: str
    category: str
    family: str
    sources: list
    question: str
    required: list
    forbidden: list = field(default_factory=list)
    distractors: list = field(default_factory=list)


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
        for k, story in enumerate(stories, 1):
            base = 2 * (5 + window // 2 * k)
            plan = [base + offset for offset in offsets]
            assert len(story.sources) <= len(plan), f"{persona} {story.category_id}: too many sources"
            assert max(plan) - base < window, "slot window overlaps the next story"
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
            free = [s for s in range(base, base + window) if s not in used]
            for d_index, text in enumerate(story.distractors):
                slot = free[d_index]
                used[slot] = {"id": f"{persona}-{story.category_id}-X{d_index + 1}",
                              "now": slot_time(start, slot).isoformat(), "modality": "text",
                              "text": text, "asset": None, "caption": None,
                              "assistant_reply": default_reply}
            last = slot_time(start, plan[len(story.sources) - 1])
            groups.append((story, ids, sorted(set(modes)), last))
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
        for story, ids, modes, last in groups:
            for delay in delays:
                when = last + timedelta(days=delay, minutes=question_minutes)
                assert when <= times[-1] + timedelta(days=1), "question beyond history end"
                qid = f"{persona}-{story.category_id}/d{delay:02}"
                questions.append({"id": qid, "persona": persona,
                                  "group_id": f"{persona}-{story.category_id}",
                                  "now": when.isoformat(),
                                  "cutoff_turn": bisect.bisect_right(times, when),
                                  "question": story.question, "source_ids": ids,
                                  "category": story.category, "category_id": story.category_id,
                                  "family": story.family, "delay_days": delay,
                                  "kind": "semantic_memory", "input_modalities": modes})
                criteria.append({"id": qid, "required": story.required,
                                 "forbidden": story.forbidden, "source_ids": ids})
        write_rows(suite / "inputs" / f"{persona}.jsonl", history)
    write_rows(suite / "questions.jsonl", questions)
    write_rows(suite / "review-only/criteria.jsonl", criteria)
    write_rows(suite / "review-only/source-authoring-provenance.jsonl", provenance)
    write_rows(Path(bank).parent / "audio-to-synthesize.jsonl", audio)
    return {"semantic": len(questions), "groups": len(criteria) // len(delays),
            "audio_pending": len(audio), "authored_at": datetime.now(UTC).isoformat(),
            "modalities": dict(Counter(m for q in questions for m in q["input_modalities"]))}


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
