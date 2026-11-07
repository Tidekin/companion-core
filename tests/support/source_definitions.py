import hashlib

from companion.config import ObservationAdapterConfig
from companion.observation import ObservationEvent
from companion.privacy import epoch
from tests.conftest import ingest

BILI = "https://www.bilibili.com/video/BV1xx411c7mD"


HEYBOX = "https://www.xiaoheihe.cn/app/bbs/link/123456"


def accepted_text(core, source="bilibili", number=1, **changes):
    core.settings.observation_enabled = True
    core.settings.observation_adapters["reader"] = ObservationAdapterConfig(
        source=source, token="synthetic-reader-token"
    )
    text = "这是已经成功读取的真实证据文本。"
    return ObservationEvent(
        schema_version=2,
        event_id=f"source_definition_{number}",
        event_type="content.observed",
        source=source,
        source_kind="text",
        provider_event_id=str(number),
        occurred_at=core.clock(),
        trace_id=f"source_trace_{number}",
        result_status="FETCHED",
        privacy_epoch=epoch(core.store),
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        payload={
            "title": "来源材料",
            "text": text,
            "url": BILI if source == "bilibili" else HEYBOX,
        },
        **changes,
    )


def enable_sharing(core):
    core.settings.social_enabled = core.settings.proactive_enabled = True
    core.settings.proactive_idle_seconds = 0
    core.settings.quiet_start = core.settings.quiet_end = 0
    ingest(core)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
