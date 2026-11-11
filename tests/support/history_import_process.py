"""Shipped independent worker with only provider transport and clock substituted."""

import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

import httpx

from companion.__main__ import worker
from companion.config import Settings
from companion.conversation_policy import CHARACTER_V1
from companion.core import Core
from companion.model import NATURAL_EXPRESSION
from tests.sdk_reader_fixture import answer, call

COMMON = "不推断健康、身份等敏感属性，遵守主人提出的边界。"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--now", required=True)
    args = parser.parse_args()
    raw = await asyncio.to_thread(Path(args.config).read_text, "utf-8")
    settings = Settings.model_validate_json(raw)
    if settings.model_url != "https://synthetic.invalid/v1":
        raise ValueError("SYNTHETIC_PROVIDER_ONLY")
    calls, finished = [], False

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        returned = any(message["role"] == "tool" for message in body["messages"])
        response = answer("（尾巴尖轻轻晃了晃）是搬家时约好的米饭暗号呀。") if returned else answer(
            calls=[call("recall-imported-agreement", "memory_recall",
                        {"query": "米饭作为搬家后的暗号"})])
        return httpx.Response(200, json=response)

    class SyntheticCore(Core):
        def __init__(self, settings, model):
            super().__init__(settings, model, clock=lambda: args.now)

        def fail_job(self, job, code):
            traceback.print_exception(sys.exception(), file=sys.stderr)
            return super().fail_job(job, code)

        async def run_once(self, *args, **kwargs):
            nonlocal finished
            result = await super().run_once(*args, **kwargs)
            finished = bool(self.store.one("SELECT 1 FROM jobs WHERE state IN "
                                           "('SUCCEEDED','FAILED','CANCELLED')"))
            return result

    original = httpx.AsyncClient

    class SyntheticClient(original):
        def __init__(self, **options):
            options.setdefault("transport", httpx.MockTransport(provider))
            super().__init__(**options)

    with (patch("companion.core.Core", SyntheticCore),
          patch("companion.__main__.httpx.AsyncClient", SyntheticClient),
          patch("companion.shutdown.requested", lambda: finished)):
        await worker(settings)
    serialized = json.dumps(calls, ensure_ascii=False)
    system_texts = ["\n".join(str(message.get("content", "")) for message in call["messages"]
                              if message["role"] == "system") for call in calls]
    print(json.dumps({"calls": len(calls), "character_in_context": "汐米" in serialized,
        "actions_in_context": "尾巴尖轻轻晃了晃" in serialized,
        "memory_in_context": "主人约定把米饭作为搬家后的暗号" in serialized,
        "analysis_replayed": "分析：先理解问题" in serialized,
        "reasoning_fields": any("reasoning_content" in message for call in calls
                                for message in call["messages"]),
        "character_expression_policy": any(CHARACTER_V1 in text for text in system_texts),
        "natural_expression_policy": any(NATURAL_EXPRESSION in text for text in system_texts),
        "common_rules_retained": all(COMMON in text for text in system_texts)
    }), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
