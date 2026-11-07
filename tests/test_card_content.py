"""Supported QQ card links reuse the ordinary Reader task and its limits."""

import json

from companion import qq_rich
from companion.store import encode
from tests.support.content import POST, enable, owner_share
from tests.support.qq_rich_input import rich_event


def test_card_urls_schedule_reader_once_with_limit_and_ignore_quoted_urls(core):
    service, _ = enable(core)
    event_id = owner_share(core)
    urls = [POST, POST, POST.replace("123456", "123457"), POST.replace("123456", "123458")]
    payload = rich_event([
        {"type": "json", "data": {"data": json.dumps({"meta": {"news": {"jumpUrl": url}}})}}
        for url in urls
    ] + [{"type": "reply", "data": {"id": "456"}}])["payload"]
    payload["rich"]["segments"][-1]["body"] = {
        "source": "INLINE", "status": "READ", "issues": [],
        "nodes": [{"path": [], "nickname": "转发昵称", "text": POST.replace("123456", "999999")}],
    }
    qq_rich.validate(payload["rich"])
    core.store.execute(
        "UPDATE events SET body_json=? WHERE event_id=?", (encode(payload), event_id),
    )
    service.schedule(event_id)
    service.schedule(event_id)
    rows = core.store.rows("SELECT kind,target_url FROM content_items ORDER BY slot")
    assert rows == [
        {"kind": "link", "target_url": POST},
        {"kind": "link", "target_url": POST.replace("123456", "123457")},
    ]
    assert core.store.one("SELECT COUNT(*) n FROM exploration_requests")["n"] == 2
    assert core.store.rows("SELECT * FROM model_usage") == []
