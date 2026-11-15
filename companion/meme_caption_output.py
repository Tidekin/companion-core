"""Bring a caption model's answer into the strict caption contract, or say why not.

Only model-authored fields change, and only in ways that cannot add meaning:
Core-known facts win (a one-frame image is static), labels outside the fixed
vocabularies are dropped, repeats are removed, an intent marked both suitable
and to-avoid is kept only as to-avoid, and over-long list items are cut to
the field limit. Anything else still fails the unchanged contract.
"""

import json
import re
from typing import get_args

from pydantic import ValidationError

from companion_meme_protocol import Intent

INTENTS = frozenset(get_args(Intent))
LIST_LIMITS = {"use_when": 80, "avoid_when": 80, "tags": 32}
FENCE = re.compile(r"\s*```(?:json)?[ \t]*\n(.*?)\n?```\s*", re.S)


def unfence(text):
    found = FENCE.fullmatch(text) if isinstance(text, str) else None
    return found.group(1) if found else text


def normalize(raw, sampling):
    """A copy of `raw` with deterministic fixes; the input is never mutated."""
    value = dict(raw)
    for key in ("suitable", "avoid"):
        items = value.get(key)
        if isinstance(items, list):
            seen = []
            for item in items:
                if item in INTENTS and item not in seen:
                    seen.append(item)
            value[key] = seen
    if isinstance(value.get("suitable"), list) and isinstance(value.get("avoid"), list):
        value["suitable"] = [item for item in value["suitable"] if item not in value["avoid"]]
    for key, limit in LIST_LIMITS.items():
        items = value.get(key)
        if isinstance(items, list):
            cut = []
            for item in items:
                if isinstance(item, str) and item.strip():
                    item = item.strip()[:limit]
                    if item not in cut:
                        cut.append(item)
            value[key] = cut
    status = value.get("temporal_status")
    if sampling.total_frames == 1:
        value["temporal_status"] = "static"
    elif status == "static":
        # Several sampled frames the model saw as identical are consistent, not static.
        value["temporal_status"] = "consistent"
    if isinstance(value.get("confidence"), int) and not isinstance(value["confidence"], bool):
        value["confidence"] = float(value["confidence"])
    return value


def failure_detail(error):
    """Where the answer broke the contract: field paths and error types, never values."""
    if isinstance(error, ValidationError):
        return {"errors": [{"loc": ".".join(str(part) for part in item["loc"])[:120],
                            "type": item["type"][:60]} for item in error.errors()[:12]]}
    if isinstance(error, json.JSONDecodeError):
        return {"errors": [{"loc": "", "type": "json_invalid"}]}
    return {"errors": [{"loc": "", "type": type(error).__name__[:60],
                        "code": str(error)[:60] if str(error).isupper() else ""}]}
