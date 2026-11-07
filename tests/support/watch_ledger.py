import hashlib
import json

import httpx

from companion import watch_ledger
from companion.model import ModelClient
from companion.observation import ObservationEvent
from companion.privacy import epoch
from tests.conftest import run
from tests.support.exploration import configure
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare

JUDGEMENT = {"rating": 1, "take": "猫卡在纸箱里那段特别好笑", "topic_tags": ["猫", "日常"]}


class Provider:
    """The paid path needs the real ModelClient; the provider itself is a mock transport."""

    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    def __call__(self, request):
        self.calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": self.texts.pop(0)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


def enable(core):
    core.settings.watch_ledger = core.settings.watch_ledger.model_copy(update={"enabled": True})


def ready(core, tmp_path, *texts):
    """Live paid approval plus an OwnerAuthority grant holding the exploration purpose."""
    configure(core)
    enable(core)
    policy = prepare(core, tmp_path)
    activate(core, policy.model_copy(update={"purposes": ["exploration"]}))
    provider = Provider(*texts)
    core.model = ModelClient(core.settings,
                             httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    return provider


def observe(core, number, *, adapter="bili", source="bilibili", status="FETCHED", text="正文内容",
            title="猫和纸箱", url=None):
    url = url or (f"https://www.bilibili.com/video/BV1{number:08d}a" if source == "bilibili"
                  else f"https://www.xiaoheihe.cn/app/bbs/link/{number:012x}")
    event_id = f"watch_obs_{number}"
    core.observations.append(adapter, ObservationEvent(
        schema_version=2, event_id=event_id, event_type="content.observed", source=source,
        source_kind="title" if status == "DISCOVERED" else "text",
        provider_event_id=f"watch-{number}", occurred_at=core.clock(),
        trace_id=f"watch_trace_{number:08d}", result_status=status,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        privacy_epoch=epoch(core.store),
        payload={"title": title, "text": text, "url": url}))
    return event_id


def step(core):
    return run(watch_ledger.run_once(core.store, core.settings, core.model, core.clock))
