"""Original Life worker/selector in a separate process; provider HTTP alone is synthetic."""

import asyncio
import json
import os
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from companion import life_worker
from companion.config import Settings
from companion.core import Core


async def run_native(settings, clock, provider, *, stop_on_idle=False):
    units, result = [], {}
    original_selector = life_worker.native_selector

    class Finished(Exception):
        pass

    class ProviderHTTP(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(provider), **kwargs)

    def observe(core, worker_id):
        selector = original_selector(core, worker_id)
        original_step = selector.step
        existing = {row["job_id"] for row in core.store.rows(
            "SELECT r.job_id FROM life_activity_results r JOIN jobs j USING(job_id) "
            "WHERE j.state IN ('SUCCEEDED','FAILED','CANCELLED') OR EXISTS "
            "(SELECT 1 FROM model_usage m WHERE m.job_id=j.job_id AND m.state='UNKNOWN')")}

        async def measured():
            before = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
            selected = await original_step()
            admitted = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] - before
            assert admitted <= 1
            units.append({"lane": selected.lane, "admissions": admitted,
                          "progressed": selected.result.progressed})
            rows = core.store.rows("SELECT r.*,j.state AS job_state,j.last_error_code "
                "FROM life_activity_results r JOIN jobs j USING(job_id)")
            terminal = any(row["job_id"] not in existing and (
                row["job_state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
                or core.store.one("SELECT 1 FROM model_usage WHERE job_id=? AND state='UNKNOWN'",
                                  (row["job_id"],))) for row in rows)
            if terminal or stop_on_idle and (selected.blocked or not selected.more):
                result.update(pid=os.getpid(), units=units, results=rows)
                raise Finished
            return selected

        selector.step = measured
        return selector

    # Observe actual completed units and control time/termination, never admission or dispatch.
    with patch.object(life_worker, "Core", partial(Core, clock=clock)), (
        patch.object(life_worker, "httpx", SimpleNamespace(AsyncClient=ProviderHTTP))
    ), patch.object(life_worker, "native_selector", observe):
        try:
            async with asyncio.timeout(45):
                await life_worker.life_worker(settings)
        except Finished:
            pass
    return result


async def main():
    args = json.loads(await asyncio.to_thread(Path(sys.argv[1]).read_text, encoding="utf-8"))
    settings = Settings.model_validate(args["settings"])
    if settings.model_url != "https://synthetic.invalid/v1":
        raise ValueError("SYNTHETIC_PROVIDER_REQUIRED")
    calls, prompts = [], []

    async def provider(request):
        assert request.url.host == "synthetic.invalid"
        messages = json.loads(request.content)["messages"]
        material = json.loads(messages[-1]["content"])
        assert material["kind"] == ("followup" if args["scenario"] == "followup" else "life")
        calls.append(material)
        prompts.append(messages)
        print("LIFE_PROVIDER_STARTED", flush=True)
        assert await asyncio.to_thread(sys.stdin.readline) == "continue\n"
        scenario = args["scenario"]
        proposal = {"action": "FOLLOW_UP" if scenario in {"bug", "preempt", "pause"} else "SHARE",
            "destination": "QZONE_DRAFT" if scenario.startswith("qzone")
            else "PRIVATE_MESSAGE_DRAFT", "source_event_ids": [args["source_id"]],
            "reason": "仍未结束" if scenario == "bug" else "实际来源",
            "text": "昨天那个导出问题有新线索了吗？" if scenario == "bug"
            else "这段资料留下了一个值得继续探索的想法。"}
        content = ("对了，里面那个配角其实挺出彩的。" if scenario == "followup"
                   else json.dumps(proposal, ensure_ascii=False))
        body = {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}
        if scenario != "unknown":
            body["usage"] = {"prompt_tokens": 180, "completion_tokens": 60}
        return httpx.Response(200, json=body)

    result = await run_native(settings, lambda: args["now"], provider,
                              stop_on_idle=args.get("restart", False)
                              or args["scenario"] == "followup")
    result.update(provider_calls=len(calls), materials=calls, prompts=prompts)
    await asyncio.to_thread(Path(args["result_path"]).write_text,
                            json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
