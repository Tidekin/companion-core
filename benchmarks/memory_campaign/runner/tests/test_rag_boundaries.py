"""Exercise new-reference cutoff and shared-failure accounting without network."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import numpy as np
import pytest

@pytest.fixture
def rag(tmp_path, monkeypatch):
    (tmp_path / "round.json").write_text("{}", "utf-8")
    monkeypatch.setattr(sys, "argv", ["rag.py", "--round", str(tmp_path)])
    spec = importlib.util.spec_from_file_location(
        "blind_rag", Path(__file__).resolve().parents[2] / "rag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_answer_keeps_shared_unknown_and_does_not_retry(tmp_path, monkeypatch, rag):
    monkeypatch.setattr(rag, "OUT", tmp_path)

    async def run():
        seen = []

        def transport(request):
            seen.append(request)
            return httpx.Response(502, json={"error": "offline fixture"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            calls = rag.Calls.__new__(rag.Calls)
            calls.client = client
            calls.dkey = calls.qkey = "offline-fixture"
            calls.deep = rag.Budget(
                tmp_path / "shared.jsonl",
                base_url="https://api.deepseek.com",
                pricing={
                    "input_rate": 2,
                    "cached_input_rate": 0.04,
                    "output_rate": 8,
                    "safety_factor": 1.5,
                },
                total_cny=50,
                inference_cny=47.5,
            )
            calls.qwen = None
            calls.semaphore = asyncio.Semaphore(1)
            with pytest.raises(RuntimeError, match="answer_HTTP_502"):
                await calls.call(
                    "answer",
                    {"model": "deepseek-flash", "messages": [], "max_tokens": 3840},
                    "N99-K01/d00",
                )
            held, latest = calls.deep.snapshot()
            assert held > 0 and [r["state"] for r in latest.values()] == ["UNKNOWN"]
            assert len(seen) == 1
            assert rag.read(tmp_path / "errors.jsonl")[0]["local_await_finished"]

    asyncio.run(run())


def test_retrieval_and_generator_never_receive_future_turns(tmp_path, monkeypatch, rag):
    root, out, suite = tmp_path, tmp_path / "rag", rag.R.suite
    out.mkdir()
    suite.mkdir(parents=True)
    native = root / "arms/old/results/N99"
    native.mkdir(parents=True)
    monkeypatch.setattr(rag, "ROOT", root)
    monkeypatch.setattr(rag, "OUT", out)
    monkeypatch.setattr(rag, "SUITE", suite)
    (native / "manifest.json").write_text('{"status":"COMPLETED"}', "utf-8")
    for turn, text in [(1, "past one"), (2, "past two"), (3, "FUTURE_CANARY")]:
        rag.append(
            native / "turns-observed.jsonl",
            {
                "turn": turn,
                "now": "2026-01-01T00:00:00+08:00",
                "text": text,
                "modality": "text",
                "assistant_reply": "ack",
            },
        )
    for i in range(116):
        rag.append(
            suite / "questions.jsonl",
            {
                "id": f"N99-Q{i}",
                "persona": "N99",
                "question": "what was said?",
                "kind": "semantic_memory",
                "cutoff_turn": 2,
                "now": "2026-01-01T00:00:00+08:00",
            },
        )

    class FakeCalls:
        async def embed(self, texts, label):
            return np.ones((len(texts), 3), dtype=np.float32)

        async def call(self, kind, body, label):
            assert "FUTURE_CANARY" not in json.dumps(body)
            if kind == "rerank":
                return {"output": {"results": [{"index": 0}]}}
            return {
                "choices": [{"message": {"content": "past"}, "finish_reason": "stop"}],
                "usage": {},
            }

    asyncio.run(rag.persona(FakeCalls(), "N99"))
    answers = rag.read(out / "answers.jsonl")
    assert len(answers) == 116
    assert all(a["status"] == "SUCCEEDED" and max(a["recent_turns"]) == 2 for a in answers)
