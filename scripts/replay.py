"""Deterministic 100-event pipeline benchmark; no real-model quality claim."""

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion.budget import token_upper_bound  # noqa: E402
from companion.config import Settings  # noqa: E402
from companion.contracts import InboundEvent  # noqa: E402
from companion.core import Core  # noqa: E402
from companion.model import ModelResponse  # noqa: E402
from companion.store import encode, migrate  # noqa: E402
from tests.postgres_fixture import isolated_database  # noqa: E402


class FixtureModel:
    def __init__(self):
        self.chat_bounds = []

    async def complete(self, messages, scenario):
        estimate = token_upper_bound(messages)
        if scenario == "memory_extract":
            candidates = []
            for source in json.loads(messages[-1]["content"])["events"]:
                if re.fullmatch(r"我的项目编号\d{2}偏好是蓝色", source["text"]):
                    candidates.append(
                        {
                            "subject": "user",
                            "kind": "preference",
                            "fact_key": source["text"][:9],
                            "content": source["text"],
                            "evidence_event_id": source["ref"],
                            "evidence_quote": source["text"],
                        }
                    )
            text = encode({"memories": candidates})
        elif scenario == "memory_review":
            payload = json.loads(messages[-1]["content"])
            text = encode(
                {"relevant": [row["ref"] for row in payload["memories"]]}
                if payload.get("operation") == "recall"
                else {"relations": [{"supported": True, "update_authorized": False,
                    "claims_replacement": False, "same": None, "conflicts": [], "replaces": []}
                    for _ in payload["candidates"]]}
            )
        else:
            self.chat_bounds.append(estimate)
            text = "[回放模型] 收到。"
        return ModelResponse(text, estimate, len(text.encode()), 0)


async def replay(*, scratch=None):
    scratch = Path(scratch) if scratch is not None else ROOT / ".local/test-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    with isolated_database() as database, tempfile.TemporaryDirectory(
        dir=scratch, prefix="replay-",
    ) as directory:
        model = FixtureModel()
        cfg = Settings(
            owner_qq_id="10001",
            bot_qq_id="10002",
            bridge_token="fixture-token-" * 3,
            database=database,
            state_directory=directory + "/state",
            embedding_cache_dir=os.environ.get(
                "COMPANION_TEST_EMBEDDING_CACHE", str(ROOT / "data/embedding-cache"),
            ),
            erasure_ledger=directory + "/privacy.jsonl",
            memory_enabled=True,
            proactive_enabled=False,
            extraction_idle_seconds=0,
            input_rate=300000,
            cached_input_rate=6000,
            output_rate=1200000,
        )
        migrate(cfg.database)
        core = Core(cfg, model)
        try:
            for i in range(100):
                text = f"我的项目编号{i:02}偏好是蓝色" if i < 30 else f"这是普通问候第{i}次"
                body = dict(
                    schema_version=1,
                    event_id=f"evt_replay_{i}",
                    source="qq.onebot",
                    source_event_id=f"10002:qq:FriendMessage:10001:{i}",
                    event_type="message.received",
                    occurred_at=datetime.now(UTC).isoformat(),
                    conversation={
                        "platform_session_key": "qq:FriendMessage:10001",
                        "scope": "private",
                    },
                    actor={"platform_user_id": "10001", "role": "owner"},
                    payload={"text": text, "media": []},
                    trace_id=f"trace_replay_{i}",
                )
                core.ingest(InboundEvent.model_validate(body), f"replay-{i}")
                await core.run_once()
                core.store.execute("UPDATE outbox SET state='SENT'")
                if (i + 1) % 20 == 0:
                    await core.extract_once()
            recall = sum(
                any(
                    m["content"] == f"我的项目编号{i:02}偏好是蓝色"
                    for m in core.memory.search(f"编号{i:02}")
                )
                for i in range(30)
            )
            extracted = core.store.one("SELECT COUNT(*) AS n FROM memories")["n"]
            report = {
                "mode": "deterministic_fixture_model",
                "events": 100,
                "expected_facts": 30,
                "accepted_extractions": extracted,
                "recall_at_6": recall / 30,
                "average_chat_input_token_upper_bound": round(sum(model.chat_bounds) / 100, 2),
                "max_chat_input_token_upper_bound": max(model.chat_bounds),
                "context_soft_bound": cfg.context_soft_tokens,
                "public_actions": core.store.one(
                    "SELECT COUNT(*) AS n FROM outbox WHERE action_type LIKE %s", ("qzone.%",)
                )["n"],
                "real_model_extraction_accuracy": "not measured",
                "real_model_hallucination_rate": "not measured",
            }
            assert extracted == 30 and recall == 30 and report["public_actions"] == 0, report
            return report
        finally:
            core.close()


if __name__ == "__main__":
    Path(".local").mkdir(exist_ok=True)
    report = asyncio.run(replay())
    Path("docs/replay-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
