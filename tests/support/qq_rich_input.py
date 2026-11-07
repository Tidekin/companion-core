import json

from companion_onebot_bridge.event_normalizer import normalize
from tests.support.onebot_events import config, raw_event


def rich_event(message, **changes):
    return normalize(
        json.dumps(raw_event(message=message, **changes)),
        config(),
        now=raw_event()["time"],
        rich_input_scope="references_v1",
    )


def card(text="引用自述", url="https://m.bilibili.com/video/BV1234567890"):
    return {
        "type": "json",
        "data": {
            "data": json.dumps(
                {
                    "meta": {"detail_1": {"title": text, "desc": "摘要不是正文", "qqdocurl": url}},
                    "unknown": {"secret": "unapproved raw card field"},
                }
            )
        },
    }
