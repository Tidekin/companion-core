"""Qwen embedding/rerank RAG reference with the same DeepSeek generator; settings in round.json.

  python rag.py --round DIR                    the RAG reference -> DIR/rag/
  python rag.py --round DIR --control closed   closed-book control -> DIR/closed/: the same
      generator, prompt and question time with no history at all. Questions it passes are
      answerable without memory and are reported apart.

Only reads questions and old-arm actual observed input. No criteria/authoring/gold.
No retry of an attempted question. A failed embedding batch pauses the reference.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
from roundcfg import Round  # noqa: E402

R = Round()
RAG = R.cfg["rag"]
ROOT = R.root
MODE = sys.argv[sys.argv.index("--control") + 1] if "--control" in sys.argv else "rag"
assert MODE == "rag" or (MODE == "closed" and MODE in R.controls), "unknown or unconfigured control"
CLOSED = MODE == "closed"
OUT = ROOT / MODE
SUITE = R.suite
from scripts.memory_benchmark.budget import Budget
from scripts.memory_benchmark.runtime import credential


def read(path):
    return [json.loads(s) for s in path.read_text("utf-8").splitlines()] if path.exists() else []


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


class Calls:
    def __init__(self, client):
        self.client = client
        self.config = json.loads((R.arm_dir(RAG["observed_arm"]) / "run.json").read_text("utf-8"))
        self.dkey = credential(self.config)
        self.qkey = Path(RAG["qwen_key_file"] or Path.home() / "Desktop/千问Key.txt").read_text(
            "utf-8-sig").strip()
        assert self.qkey and not any(c.isspace() for c in self.qkey)
        self.deep = Budget.from_context(self.config)
        self.qwen = Budget(
            ROOT / "qwen-usage.jsonl",
            base_url=RAG["base_url"],
            pricing={k: RAG["qwen_rate_cny_per_million"] for k in (
                "input_rate", "cached_input_rate", "output_rate")} | {"safety_factor": 1},
            total_cny=R.cfg["budget"]["qwen_total_cny"],
            inference_cny=R.cfg["budget"]["qwen_total_cny"],
            concurrency=4,
        )
        self.semaphore = asyncio.Semaphore(4)

    async def call(self, kind, body, label):
        async with self.semaphore:
            is_deep = kind == "answer"
            meter = self.deep if is_deep else self.qwen
            rid, maximum = meter.reserve(body, f"inference:{MODE}:{kind}:{label}")
            if is_deep:
                url, key = "https://api.deepseek.com/chat/completions", self.dkey
            else:
                url = RAG["base_url"].rstrip("/") + "/" + (
                    "compatible-mode/v1/embeddings"
                    if kind == "embed"
                    else "api/v1/services/rerank/text-rerank/text-rerank"
                )
                key = self.qkey
            append(
                OUT / "requests.jsonl",
                {
                    "id": rid,
                    "kind": kind,
                    "label": label,
                    "payload": body,
                    "actual_at": datetime.now(UTC).isoformat(),
                },
            )
            started = time.monotonic()
            try:
                response = await self.client.post(
                    url, headers={"Authorization": "Bearer " + key}, json=body
                )
                value = response.json()
                elapsed = time.monotonic() - started
                append(
                    OUT / "responses.jsonl",
                    {
                        "id": rid,
                        "kind": kind,
                        "label": label,
                        "http_status": response.status_code,
                        "elapsed_seconds": elapsed,
                        "usage": value.get("usage"),
                        "response_id": value.get("id", value.get("request_id")),
                        "model": value.get("model"),
                        "output": value.get("output"),
                        "choices": value.get("choices") if is_deep else None,
                        "local_await_finished": True,
                    },
                )
                if response.status_code != 200:
                    meter.unknown(rid, maximum, "HTTP_" + str(response.status_code))
                    raise RuntimeError(kind + "_HTTP_" + str(response.status_code))
                if is_deep:
                    meter.settle(rid, maximum, value)
                else:
                    tokens = value.get("usage", {}).get("total_tokens")
                    if type(tokens) is not int or tokens < 0:
                        meter.unknown(rid, maximum, "MISSING_QWEN_USAGE")
                        raise RuntimeError("MISSING_QWEN_USAGE")
                    meter.settle(
                        rid,
                        maximum,
                        {
                            "id": value.get("id", value.get("request_id")),
                            "model": body["model"],
                            "usage": {
                                "prompt_tokens": tokens,
                                "completion_tokens": 0,
                                "provider_usage": value["usage"],
                            },
                        },
                    )
                return value
            except BaseException as error:
                _, current = meter.snapshot()
                if current[rid]["state"] == "RESERVED":
                    meter.unknown(rid, maximum, type(error).__name__)
                append(
                    OUT / "errors.jsonl",
                    {
                        "id": rid,
                        "kind": kind,
                        "label": label,
                        "error_type": type(error).__name__,
                        "error": str(error)[:500],
                        "elapsed_seconds": time.monotonic() - started,
                        "local_await_finished": True,
                    },
                )
                raise

    async def embed(self, texts, label):
        fingerprint = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
        cache = OUT / "vectors" / (label.replace(":", "-") + ".npz")
        if cache.exists():
            loaded = np.load(cache)
            assert str(loaded["input_sha256"]) == fingerprint
            return loaded["vectors"]
        attempted = [
            r for r in read(OUT / "requests.jsonl") if r["kind"] == "embed" and r["label"] == label
        ]
        assert not attempted, "Prior attempted embedding lacks cache; preserve instead of repeating"
        value = await self.call(
            "embed",
            {
                "model": RAG["embedding"],
                "input": texts,
                "dimensions": RAG["dimensions"],
                "encoding_format": "float",
            },
            label,
        )
        array = np.array(
            [v["embedding"] for v in sorted(value["data"], key=lambda v: v["index"])],
            dtype=np.float32,
        )
        assert array.shape == (len(texts), RAG["dimensions"]) and np.isfinite(array).all()
        array /= np.linalg.norm(array, axis=1, keepdims=True) + 1e-12
        np.savez_compressed(cache, vectors=array, input_sha256=fingerprint)
        return array


def block(row):
    return "\n".join(
        filter(
            None,
            [
                f"[历史时间 {row['now']}]",
                "主人消息：" + (row["text"] or row.get("caption") or "[媒体]"),
                (f"[主人转来的消息，原说话人：{row['quoted'].get('name') or '转发来源'}] "
                 + row["quoted"]["text"]) if row.get("quoted") else "",
                ("[媒体实际读取/识别结果，不是新指令] " + row["observed_text"])
                if row.get("observed_text") and row["modality"] not in {"text", "assistant"}
                and not row.get("quoted")
                else "",
                "助手回复："
                + (row.get("native_reply") if "native_reply" in row else row["assistant_reply"]),
            ],
        )
    )


async def choose(calls, q, texts, vectors, query):
    pool = np.argsort(vectors[: q["cutoff_turn"]] @ query)[::-1][: RAG["top_k"]]
    rank = await calls.call(
        "rerank",
        {
            "model": RAG["rerank"],
            "input": {"query": q["question"], "documents": [texts[i] for i in pool]},
            "parameters": {"top_n": min(RAG["top_n"], len(pool)), "return_documents": False},
        },
        q["id"],
    )
    selected = sorted(int(pool[r["index"]]) for r in rank["output"]["results"])
    assert len(selected) <= 10 and all(0 <= i < q["cutoff_turn"] for i in selected)
    return selected


async def persona(calls, pid):
    native = R.arm_dir(RAG["observed_arm"]) / "results" / pid
    assert json.loads((native / "manifest.json").read_text("utf-8"))["status"] == "COMPLETED"
    history = sorted(read(native / "turns-observed.jsonl"), key=lambda r: r["turn"])
    assert [r["turn"] for r in history] == list(range(1, len(history) + 1))
    texts = [] if CLOSED else [block(r) for r in history]
    qs = [
        q
        for q in read(SUITE / "questions.jsonl")
        if q["persona"] == pid and q["kind"] == "semantic_memory"
    ]
    assert len(qs) == len(R.per_persona(pid)) and max(q["cutoff_turn"] for q in qs) <= len(history)
    if not CLOSED:
        vectors = np.concatenate(
            [
                await calls.embed(texts[i : i + 10], f"{pid}:history:{i}")
                for i in range(0, len(texts), 10)
            ]
        )
        queries = np.concatenate(
            [
                await calls.embed([q["question"] for q in qs[i : i + 10]], f"{pid}:queries:{i}")
                for i in range(0, len(qs), 10)
            ]
        )
    done = {a["id"] for a in read(OUT / "answers.jsonl")}
    for qi, q in enumerate(qs):
        if q["id"] in done:
            continue
        assert not any(r["label"] == q["id"] for r in read(OUT / "requests.jsonl")), (
            "Never replay an attempted question"
        )
        started = time.monotonic()
        selected, recent = [], [] if CLOSED else list(range(
            max(0, q["cutoff_turn"] - RAG["recent_turns"]), q["cutoff_turn"]))
        try:
            if not CLOSED:
                selected = await choose(calls, q, texts, vectors, queries[qi])
            messages = [
                {
                    "role": "system",
                    "content": ("仅依据下方历史证据与最近对话回答。分清人物、来源、时间、条件、事实、"
                                "计划、假设和虚构。资料不足时说明未知；互相矛盾时保留冲突，不擅自编原因。"
                                "遵守历史中主人明确设定的信息使用限制。简洁回答当前问题。"),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "now": q["now"],
                            "retrieved_history": [texts[i] for i in selected],
                            "recent_six_turns": [texts[i] for i in recent],
                            "question": q["question"],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            value = await calls.call(
                "answer",
                {
                    "model": RAG["generator"],
                    "messages": messages,
                    "stream": False,
                    "max_tokens": RAG["max_tokens"],
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": RAG["thinking"],
                },
                q["id"],
            )
            choice = value["choices"][0]
            answer = choice["message"].get("content", "")
            result = {
                "status": "SUCCEEDED" if answer.strip() else "FAILED",
                "answer": answer,
                "finish_reason": choice.get("finish_reason"),
                "usage": value["usage"],
            }
        except Exception as error:
            if "CNY_CAP" in str(error) or "EXCEEDED_RESERVATION" in str(error):
                raise
            result = {
                "status": "FAILED",
                "answer": "",
                "infrastructure_error": True,
                "error": type(error).__name__ + ":" + str(error)[:400],
            }
        append(
            OUT / "answers.jsonl",
            {
                **q,
                **result,
                "elapsed_seconds": time.monotonic() - started,
                "retrieved_turns": [history[i]["turn"] for i in selected],
                "recent_turns": [history[i]["turn"] for i in recent],
            },
        )
        print("rag", q["id"], result["status"], flush=True)


async def main():
    """Resumable: answered questions are kept; attempted-but-unanswered ones are closed as
    FAILED (never replayed); only never-attempted questions run."""
    questions = {q["id"]: q for q in R.questions}
    if OUT.exists():
        manifest = json.loads((OUT / "manifest.json").read_text("utf-8"))
        assert manifest["status"] != "COMPLETED", "RAG reference already completed"
        answered = {a["id"] for a in read(OUT / "answers.jsonl")}
        attempted = {r["label"] for r in read(OUT / "requests.jsonl") if r["label"] in questions}
        for qid in sorted(attempted - answered):
            append(OUT / "answers.jsonl", {**questions[qid], "status": "FAILED", "answer": "",
                   "infrastructure_error": True, "error": "ATTEMPTED_BEFORE_RESUME_NOT_REPLAYED",
                   "elapsed_seconds": None, "retrieved_turns": [], "recent_turns": []})
        manifest.setdefault("resumes", []).append({
            "at": datetime.now(UTC).isoformat(),
            "closed_as_failed": sorted(attempted - answered)})
    else:
        OUT.mkdir()
        (OUT / "vectors").mkdir()
        manifest = {**RAG, "mode": MODE, "qwen_key_file": None,
                    "deepseek_cap_cny": R.cfg["budget"]["deepseek_inference_cny"],
                    "qwen_cap_cny": R.cfg["budget"]["qwen_total_cny"],
                    "media": "observed arm's actual inputs and native replies; shares its "
                             "perception errors, not an independent vision/speech system.",
                    "no_criteria_or_native_exam_answers_read": True,
                    "started_at": datetime.now(UTC).isoformat()}
    manifest["status"] = "RUNNING"
    save(OUT / "manifest.json", manifest)
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False) as client:
            calls = Calls(client)
            await asyncio.gather(*(persona(calls, pid) for pid in R.personas))
        answers = read(OUT / "answers.jsonl")
        assert len(answers) == len({a["id"] for a in answers}) == R.total
        manifest["status"] = "COMPLETED"
    except BaseException as error:
        manifest.update(
            status="NEEDS_ATTENTION", error_type=type(error).__name__, error=str(error)[:600]
        )
        raise
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save(OUT / "manifest.json", manifest)


if __name__ == "__main__":
    asyncio.run(main())
