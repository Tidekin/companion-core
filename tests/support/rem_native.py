import json
from contextlib import asynccontextmanager

import httpx

from companion import paid_budget
from companion.rem_protocol import RemConfig
from tests.support.owner_memory import environment

FACT = "青禾项目下一步检查配色"


def response(value):
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


@asynccontextmanager
async def prepared(core, tmp_path, *, seed=True, diary=False, **config):
    core.settings.rem = RemConfig.model_validate({"enabled": True,
                                                 **config})
    core.settings.diary_enabled = diary
    core.settings.diary_min_source_events = 1
    async with environment(core, tmp_path, history=False,
        additional_purposes=("rem", "diary") if diary else ("rem",),
        capability_policies={"rem": paid_budget.sha(core.settings.rem.model_dump())}) as (
            admit, _, reference):
        if seed:
            await admit(1, "/记住这个 " + FACT)
            assert await core.run_once()
            assert core.store.one("SELECT content FROM memories") == {"content": FACT}
        yield reference, admit


def usage(core):
    return core.store.rows("SELECT * FROM model_usage WHERE scenario='rem'")
