"""Exercise Core memory with real DeepSeek, synthetic data, and bounded calls.

No QQ messages are sent. All test databases are temporary workspace files; only
the public embedding weights and the JSON verification report are retained.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import httpx

from companion.config import Settings
from companion.contracts import InboundEvent
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import INDEX_VERSION
from companion.store import migrate

ROOT = Path(__file__).resolve().parents[1]


async def verify(directory, report):
    values = tomllib.loads((ROOT / "config.example.toml").read_text("utf-8"))
    environment = {}
    if (ROOT / ".env").exists():
        environment = dict(
            line.split("=", 1)
            for line in (ROOT / ".env").read_text("utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
    key = os.getenv("COMPANION_MODEL_KEY") or environment.get("COMPANION_MODEL_KEY", "")
    values.update(
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token=secrets.token_urlsafe(32),
        model_key=key.strip().strip("\"'"),
        mode="live",
        memory_enabled=True,
        database=str(directory / "core.db"),
        erasure_ledger=str(directory / "erasures.jsonl"),
        embedding_cache_dir=str(ROOT / "data/embedding-cache"),
        proactive_enabled=False,
        max_recent_messages=0,
        extraction_min_events=1,
        extraction_idle_seconds=0,
    )
    cfg = Settings(**values)
    migrate(cfg.database)

    class ObservedModel(ModelClient):
        async def complete(self, messages, scenario):
            response = await super().complete(messages, scenario)
            if scenario == "memory_review":
                report["last_review"] = {
                    "input": json.loads(messages[-1]["content"]),
                    "response": response.text,
                }
            return response

    async with httpx.AsyncClient(timeout=cfg.timeout_seconds, trust_env=False) as http:
        core = Core(cfg, ObservedModel(cfg, http))
        counter = 0

        async def send(text, session="seed"):
            nonlocal counter
            counter += 1
            event_id = f"evt_live_{counter}"
            origin = f"{session}:FriendMessage:10001"
            event = InboundEvent.model_validate(
                {
                    "schema_version": 1,
                    "event_id": event_id,
                    "source": "qq.onebot",
                    "source_event_id": f"10002:{origin}:{counter}",
                    "event_type": "message.received",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "conversation": {"platform_session_key": origin, "scope": "private"},
                    "actor": {"platform_user_id": "10001", "role": "owner"},
                    "payload": {"text": text, "media": []},
                    "trace_id": f"trace_live_{counter}",
                }
            )
            core.ingest(event, f"live-{counter}")
            for _ in range(cfg.max_attempts):
                await core.run_once()
                job = core.store.one("SELECT * FROM jobs WHERE event_id=?", (event_id,))
                if job["state"] != "RETRY_WAIT":
                    break
                delay = (
                    datetime.fromisoformat(job["next_attempt_at"]) - datetime.now(UTC)
                ).total_seconds()
                await asyncio.sleep(max(0, delay))
            if job["state"] != "SUCCEEDED":
                report["failed_input"] = text
                raise AssertionError(job["last_error_code"] or job["state"])
            row = core.store.one(
                "SELECT content_json FROM outbox WHERE origin_event_id=?", (event_id,)
            )
            return json.loads(row["content_json"])["text"]

        def record(name, passed, **details):
            report["checks"].append({"name": name, "passed": bool(passed), **details})
            print(f"{'PASS' if passed else 'FAIL'}: {name}", flush=True)
            if not passed:
                raise AssertionError(name)

        try:
            await send("我最喜欢的颜色是蓝色。")
            extracted = await core.extract_once()
            original = core.store.one("SELECT * FROM memories WHERE subject='user'")
            record(
                "automatic_extraction",
                extracted and original and original["status"] == "HYPOTHESIS",
            )
            answer = await send("/记住这个：我最喜欢的颜色是蓝色。")
            confirmed = core.store.one(
                "SELECT * FROM memories WHERE memory_id=?", (original["memory_id"],)
            )
            record(
                "explicit_confirmation_preserves_memory_id",
                confirmed["status"] == "CONFIRMED"
                and confirmed["confidence"] == 1
                and core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1,
                reply=answer,
            )

            # Older target facts must remain retrievable among newer, unrelated facts.
            for fact in [
                "我最喜欢的饮料是茉莉绿茶。",
                "我通常在周六上午去骑行。",
                "我养的猫叫布丁。",
                "我的项目代号是银杏。",
                "我的键盘用的是茶轴。",
                "我最近在学习吉他。",
                "我会用Python写脚本。",
                "我把备份存放在移动硬盘。",
                "我用铅笔画速写。",
                "我每周日整理书架。",
                "我的雨伞放在玄关。",
                "我用纸质笔记本列购物清单。",
                "我有一个白色的帆布包。",
                "我正在阅读一本天文学入门书。",
            ]:
                await send("/记住这个：" + fact)

            for n, (question, target) in enumerate(
                [
                    ("我平时最爱喝什么？", "茉莉绿茶"),
                    ("休息日一般做什么运动？", "骑行"),
                    ("我的猫叫什么？", "布丁"),
                ]
            ):
                hits = core.memory.search(question)
                answer = await send(question, f"new-session-{n}")
                record(
                    f"paraphrased_recall_{n + 1}",
                    any(target in row["content"] for row in hits) and target in answer,
                    question=question,
                    retrieved_count=len(hits),
                    reply=answer,
                )
            record(
                "unrelated_question_has_no_memory_hits",
                not core.memory.search("今天下午下雨了吗？"),
            )

            positive = await send("/记住这个：我喜欢绿茶")
            positive_id = re.search(r"mem_[a-f0-9]+", positive).group(0)
            negative = await send("/记住这个：我不喜欢绿茶")
            negative_id = re.search(r"mem_[a-f0-9]+", negative).group(0)
            pair = [
                core.store.one("SELECT * FROM memories WHERE memory_id=?", (mid,))
                for mid in [positive_id, negative_id]
            ]
            record(
                "contradiction_is_preserved_and_flagged",
                positive_id != negative_id
                and all(row["needs_review"] for row in pair)
                and "冲突" in negative,
                reply=negative,
            )
            compatible = await send("/记住这个：我喜欢红茶")
            compatible_id = re.search(r"mem_[a-f0-9]+", compatible).group(0)
            record(
                "compatible_preferences_do_not_conflict",
                not core.store.one(
                    "SELECT needs_review FROM memories WHERE memory_id=?", (compatible_id,)
                )["needs_review"],
            )
            answer = await send("现在我到底喜欢绿茶吗？", "clarification")
            record(
                "conflicted_recall_requests_clarification",
                any(word in answer for word in ["冲突", "矛盾", "确认", "澄清", "不确定"]),
                reply=answer,
            )

            await send("/忘记这一条 " + original["memory_id"])
            after_erasure = core.memory.search("我最喜欢什么颜色？")
            answer = await send("我最喜欢什么颜色？", "after-erasure")
            record(
                "forgotten_fact_does_not_return",
                all("蓝色" not in row["content"] for row in after_erasure)
                and all(word not in answer for word in ["蓝色", "红茶", "绿茶"])
                and not core.store.one(
                    "SELECT 1 FROM memory_vectors WHERE memory_id=?", (original["memory_id"],)
                ),
                retrieved_count=len(after_erasure),
                reply=answer,
            )
        finally:
            report["failed_jobs"] = core.store.rows(
                "SELECT state,last_error_code FROM jobs WHERE state IN ('FAILED','RETRY_WAIT')"
            )
            report["review_validation_retries"] = core.store.rows(
                "SELECT safe_detail_json FROM audit_log WHERE action='memory.review_invalid'"
            )
            report["usage"] = core.status()["usage"]
            report["request_count"] = core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"]
            report["estimated_micro_usd"] = sum(
                row["estimated_micro_usd"] for row in report["usage"]
            )
            report["model"] = cfg.model_name
            core.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/live-memory-report.json")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT):
        raise SystemExit("Report must remain inside the workspace")
    report = {
        "at": datetime.now(UTC).isoformat(),
        "mode": "real_model_synthetic_conversations",
        "transport": "local Core outbox; no QQ sends",
        "embedding_index": INDEX_VERSION,
        "checks": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="live-memory-", dir=ROOT / ".local") as name:
            assert Path(name).resolve().is_relative_to(ROOT)
            asyncio.run(verify(Path(name), report))
        report["temporary_database_cleanup"] = "passed"
        report["passed"] = True
        report.pop("last_review", None)
    except Exception as error:
        report["passed"] = False
        # Configuration errors can contain secret input; never dump the exception body.
        report["failure_type"] = type(error).__name__
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                k: report.get(k)
                for k in ["passed", "request_count", "estimated_micro_usd", "failure_type"]
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
