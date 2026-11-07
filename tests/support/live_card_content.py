import json
from contextlib import asynccontextmanager

import httpx

from companion.config import ObservationAdapterConfig
from companion.media_config import ContentConfig
from companion.reading_protocol import ReadObservation
from source_reader.reading import ReadingContext, observation
from tests.support.native_consumer import source_window
from tests.support.onebot_events import raw_event
from tests.support.qq_rich_input import card
from tests.support.reading_followup import select_read

URL = "https://www.xiaoheihe.cn/app/bbs/link/123456"


TOKEN = "synthetic-card-reader-token-12345"


def options():
    return {
        "link_scope": "owner_links_v1",
        "core_options": {
            "content_enabled": True,
            "observation_enabled": True,
            "content": ContentConfig(),
            "observation_adapters": {
                "reader": ObservationAdapterConfig(source="heybox", token=TOKEN),
            },
            "exploration_daily_requests": 8,
            "exploration_monthly_requests": 16,
            "exploration_daily_deep_reads": 4,
            "exploration_max_response_bytes": 50000,
            "exploration_daily_bytes": 400000,
        },
    }


@asynccontextmanager
async def captured(core, tmp_path, message=None):
    message = message if message is not None else [
        {"type": "text", "data": {"text": "请总结这份分享"}}, card(url=URL),
    ]

    async def source(ws):
        await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()), message=message)))
        await ws.wait_closed()

    async with source_window(
        core, tmp_path, source, scope_options=options(),
    ) as (window, client, protocol, handshakes, sends):
        core.content.options = core.settings.content
        await window.capture_once(client, protocol)
        try:
            assert await window.runtime.flush_one(client)
            if any(segment["type"] == "json" for segment in message):
                await select_read(core, URL)
        except httpx.HTTPStatusError as error:
            raise AssertionError(error.response.text) from error
        assert handshakes == ["Bearer " + window.config.onebot_token]
        event = core.store.one("SELECT * FROM events WHERE source='qq.onebot'")
        yield window, client, event, sends


def result(work, core):
    context = ReadingContext(**{**work["context"],
        "source_event_ids": tuple(work["context"]["source_event_ids"])})
    row = {"title": "实际读取文章", "text": "作者比较两款耳机的佩戴体验。", "url": URL,
        "article": {"method": "public_html", "fetched_at": core.clock(),
            "response_sha256": "b" * 64, "response_bytes": 300,
            "images": [], "truncated": False}}
    return ReadObservation.model_validate({"schema_version": 1, "lease_token": work["lease_token"],
        "status": "SUCCESS", "used_requests": 1, "used_bytes": 300,
        "observed_requests": 1, "observed_bytes": 300,
        "observations": [observation(row, work, context, "heybox/1",
            lambda: row["article"]["fetched_at"])]})
