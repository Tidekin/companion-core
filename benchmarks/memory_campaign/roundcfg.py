"""One benchmark round = one directory with round.json, a bank and generated evidence.

Every campaign script takes the round directory (``--round DIR`` or ``MEMORY_ROUND``);
counts, personas, categories and delays are derived from the bank, never written in code.
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from functools import cached_property
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
RUNNER = TOOLS / "runner"
if str(RUNNER) not in sys.path:
    sys.path.insert(0, str(RUNNER))
# The repository also has a top-level `scripts` package; let both resolve.
if "scripts" in sys.modules and str(RUNNER / "scripts") not in list(
        getattr(sys.modules["scripts"], "__path__", [])):
    sys.modules["scripts"].__path__.append(str(RUNNER / "scripts"))

DEFAULTS = {
    "workspace": "E:/Companion Core",
    "source_checkout": "E:/Companion Core/.local/worktrees/memory-recall-probes-20261102",
    "parser": {"transport": "ssh", "host": "root@154.89.148.204", "port": 60022,
               "known_hosts_file": "C:/Users/admin/.ssh/known_hosts",
               "image": "sha256:f57cbc752e6e828e2ca044509c3e30ff78790d433116f092d2359448280c4aaa",
               "source_sha256": "349cd037867fadfe27e8b7a8a969adb676ec98f908269e4f05be1c2a1a76a1ae"},
    "bank": "bank",
    "embedding_cache_dir": "E:/Companion Core/data/embedding-cache",
    "concurrency": 4,
    "telemetry": True,
    "provider": {"adapter": "deepseek", "base_url": "https://api.deepseek.com",
                 "model": "deepseek-flash"},
    "credentials": {"env_file": "E:/Companion Core/.env", "env_name": "COMPANION_MODEL_KEY"},
    "pricing": {"input_rate": 2.0, "cached_input_rate": 0.04, "output_rate": 8.0,
                "safety_factor": 1.5},
    "native": {"context_soft_tokens": 8000, "max_output_tokens": 768, "chat_thinking": "high",
               "chat_thinking_tokens": 3072, "memory_extract_max_output_tokens": 2048,
               "timeout_seconds": 45, "lease_seconds": 120},
    "history_grant": {"max_turns": 14, "max_utf8_bytes": 32000},
    "budget": {"deepseek_total_cny": 49, "deepseek_inference_cny": 47, "qwen_total_cny": 50},
    "rag": {"observed_arm": "old", "base_url": "https://maas.qianwenaiapi.com",
            "embedding": "qwen3.7-text-embedding", "dimensions": 2560,
            "rerank": "qwen3.7-text-rerank", "top_k": 40, "top_n": 10, "recent_turns": 6,
            "generator": "deepseek-flash", "max_tokens": 3840, "thinking": "high",
            "qwen_rate_cny_per_million": 5, "qwen_key_file": None},
    # Zero-material controls answered by the RAG generator (graded blind, reported apart).
    "controls": [],
    "references": ["rag"],
    "postgres": {"connections_file": ""},
    # judges: one name per vote (default: the provider model for every vote). anchors: a
    # jsonl of answers with known verdicts mixed into every batch; a batch that misjudges
    # one is discarded and its keys re-judged (make_anchors.py builds the file).
    "grading": {"rubric": "v3", "votes": 3, "batch_size": 8, "concurrency": 3, "max_tokens": 2048,
                "judges": None, "anchors": None, "anchors_per_batch": 2, "anchor_retries": 2,
                "anchor_gate_margin": 0.15},
    "judges": {
        "deepseek": {"ledger": "deepseek", "extra": {"thinking": {"type": "disabled"}}},
        "qwen": {"ledger": "qwen", "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
                 "model": "qwen3.8-flash", "extra": {"enable_thinking": False},
                 # Conservative upper bound per million tokens; the invoice is not read.
                 "pricing": {"input_rate": 4.0, "cached_input_rate": 4.0, "output_rate": 24.0,
                             "safety_factor": 1.5}}},
    "review": {"persona": None, "delay_days": 15, "changed_per_family": 2},
    "sensitivity_exclude_categories": [],
    "smoke": {"bank": None, "persona": None, "questions": 6, "budget_cny": 1},
    "limitations": [],
}


def _merge(base, extra):
    out = dict(base)
    for key, value in extra.items():
        out[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(
            base.get(key), dict) else value
    return out


def read_rows(path):
    path = Path(path)
    return [json.loads(s) for s in path.read_text("utf-8").splitlines() if s.strip()] \
        if path.exists() else []


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_judging_context(context):
    """Only original clocks, never an expected verdict or review explanation."""
    assert isinstance(context, dict) and set(context) == {"question_at", "source_message_ats"}, \
        "Judging context must contain original question and source-message clocks"
    assert isinstance(context["source_message_ats"], list) and context["source_message_ats"], \
        "Judging context needs source-message clocks"
    for value in [context["question_at"], *context["source_message_ats"]]:
        assert isinstance(value, str), "Judging clock must be an ISO timestamp"
        assert datetime.fromisoformat(value).utcoffset() is not None, \
            "Judging clock needs a timezone"


def round_dir(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--round" in argv:
        return Path(argv[argv.index("--round") + 1]).resolve()
    if os.environ.get("MEMORY_ROUND"):
        return Path(os.environ["MEMORY_ROUND"]).resolve()
    raise SystemExit("Pass --round <round dir> (or set MEMORY_ROUND)")


class Round:
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root else round_dir()
        raw = json.loads((self.root / "round.json").read_text("utf-8"))
        self.cfg = _merge(DEFAULTS, raw)

    @property
    def name(self):
        return self.cfg.get("name", self.root.name)

    @property
    def bank(self):
        return (self.root / self.cfg["bank"]).resolve()

    @property
    def suite(self):
        return self.bank / "suite-v1"

    @cached_property
    def questions(self):
        return [q for q in read_rows(self.suite / "questions.jsonl")
                if q.get("kind", "semantic_memory") == "semantic_memory"]

    @cached_property
    def criteria(self):
        return {c["id"]: c for c in read_rows(self.suite / "review-only/criteria.jsonl")}

    @cached_property
    def input_times(self):
        return {row["id"]: row.get("now") for path in (self.suite / "inputs").glob("*.jsonl")
                for row in read_rows(path)}

    def judging_context(self, question):
        context = dict(question_at=question["now"], source_message_ats=[
            self.input_times[source] for source in question["source_ids"]])
        validate_judging_context(context)
        return context

    @property
    def total(self):
        return len(self.questions)

    @property
    def personas(self):
        return sorted({q["persona"] for q in self.questions})

    @property
    def categories(self):
        return sorted({q["category_id"] for q in self.questions})

    @property
    def delays(self):
        return sorted({q["delay_days"] for q in self.questions})

    def per_persona(self, persona):
        return [q for q in self.questions if q["persona"] == persona]

    @property
    def controls(self):
        return tuple(self.cfg["controls"])

    @property
    def graded_arms(self):
        """Grade only the selected native runs, requested references and controls."""
        return tuple(self.cfg["arms"]) + self.references + self.controls

    @property
    def references(self):
        return tuple(self.cfg["references"])

    @property
    def answers_total(self):
        return self.total * len(self.graded_arms)

    def arm_dir(self, arm):
        return self.root / "arms" / arm

    def arm_answers(self, arm):
        if arm == "rag" or arm in self.controls:
            return read_rows(self.root / arm / "answers.jsonl")
        return [r for p in sorted((self.arm_dir(arm) / "results").glob("*/answers.jsonl"))
                for r in read_rows(p)]

    @property
    def deepseek_ledger(self):
        return self.root / "deepseek-usage.jsonl"

    @property
    def qwen_ledger(self):
        return self.root / "qwen-usage.jsonl"

    def review_persona(self):
        return self.cfg["review"]["persona"] or self.personas[min(1, len(self.personas) - 1)]


def ledger_total(path):
    latest = {}
    for row in read_rows(path):
        latest.setdefault(row["id"], {}).update(row)
    return sum(v.get("charged_or_held_cny", 0) for v in latest.values()), latest
