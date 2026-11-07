import json
from datetime import UTC, datetime

from companion.observation import CommentEvidence

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)


def comment(cid, text, parent=None, truncated=False):
    return {"comment_id": cid, "reply_to_id": parent, "floor": None, "author": "SYNTHETIC作者",
            "text": text, "image_count": 0, "truncated": truncated}


def row(event, bvid, items, redacted=0, host="https://www.bilibili.com/video/"):
    evidence = CommentEvidence.model_validate({"items": items, "reported_total": 100,
                                               "partial": True}).model_dump(mode="json")
    return {"event_id": event, "source_kind": "text", "redacted": redacted,
            "canonical_url": host + bvid,
            "evidence_json": json.dumps({"reading": {"comments": evidence}}, ensure_ascii=False)}


VIDEO = "BV1xx411c7mD"


OTHER = "BV1yy411c7mE"
