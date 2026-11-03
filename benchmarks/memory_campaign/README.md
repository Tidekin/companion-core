# Memory campaign toolkit

Reusable three-arm memory comparison: old Core, new Core and a Qwen embedding/rerank RAG
reference, all answered by the same DeepSeek model. Every script takes a round directory;
counts, personas, categories and delays come from that round's bank, budgets and settings
from its `round.json`. Nothing about a particular bank is written in code.

## Round directory

```
<round>/
  round.json            settings (below)
  build_bank.py         this round's stories (uses authoring.py)  — optional, any generator
  bank/                 the bank (layout below), frozen by bank.py
  arms/old|new/         frozen source snapshots, run configs, per-persona results
  rag/                  RAG reference requests/responses/answers
  grading/              blind mapping, raw grades, review.jsonl, overrides.jsonl
  deepseek-usage.jsonl  qwen-usage.jsonl   conservative spend ledgers
  results.json summary.json memory-report.html evidence-diagnosis.md
```

Minimal `round.json` (all other keys have defaults in `roundcfg.py`):

```json
{
  "name": "memory-round-4",
  "arms": {"old": "<commit>", "new": "<commit>"},
  "budget": {"deepseek_total_cny": 49, "deepseek_inference_cny": 47, "qwen_total_cny": 50},
  "smoke": {"bank": "E:/Companion Core/.local/<an already exposed round>/bank"},
  "sensitivity_exclude_categories": [],
  "limitations": ["..."]
}
```

Useful optional keys: `bank` (path, default `bank`), `source_checkout`, `history_grant`
(signed history scope given to both arms), `native` (Core overrides), `rag` (models,
`top_k`, `top_n`, `observed_arm`, `qwen_key_file`), `grading.rubric` (`v1`|`v2`),
`review` (`persona`, `delay_days`, `changed_per_family`), `parser`, `telemetry`.

## Bank layout

```
bank/asr-observations.jsonl            one row per WAV: asset, sha256, text, ... (real ASR)
bank/suite-v1/inputs/<persona>.jsonl   turns 1..n, strictly increasing "now"
bank/suite-v1/questions.jsonl          id, persona, group_id, now, cutoff_turn, question,
                                       source_ids, category, category_id, family,
                                       delay_days, kind, input_modalities
bank/suite-v1/review-only/criteria.jsonl  id, required[], forbidden[], source_ids
bank/suite-v1/assets/                  png / txt / wav referenced by input rows
```

Input row: `id, turn, now, modality, text, asset, caption, assistant_reply` and, for
`quote`/`forward`, optional `quoted: {"text", "name", "uin"}`. With `quoted`, `text` is
the owner's own words around it; the quoted node keeps its original speaker (`uin`
10001 = the owner quoting themself, any other uin = a third party). Rows without
`quoted` keep the old behaviour (whole text inside a third-party node).

Authoring rules that keep answers well defined:
- one event per story: never reuse a project/activity name across stories, or a later
  story silently changes an earlier story's correct answer;
- no message may describe something that has not happened yet at its own time;
- answers that depend on "now" (e.g. "next month") must state which time they refer to;
- test the owner's own corrections and relayed third-party corrections separately.

## Workflow

```
python prepare.py source --round R            # freeze arm commits before writing the bank
python R/build_bank.py                        # author the bank (or any generator)
powershell -File synthesize_audio.ps1 -Round R
<voice venv python> transcribe_audio.py --round R
python bank.py freeze --round R               # validates, then hashes everything
python smoke.py --round R                     # optional, on an already exposed bank
python prepare.py arms --round R              # parser probe, frozen snapshots, execution freeze
python run.py --round R                       # lifecycle, native arms, RAG, answer freeze
python run.py --round R --resume              # after a RAG stop: closes attempted, continues
python grade.py --round R                     # blind judging with the configured rubric
python report.py --round R --prepare-review   # writes grading/review-selection.json
#   review: write grading/review.jsonl (every selected key) and overrides.jsonl (changes)
<python with matplotlib> report.py --round R  # results.json, summary.json, charts, HTML
python diagnose.py --round R                  # evidence chain by required fields
```

`grade.py --rubric v2 --out grading-v2` regrades a finished round into a new directory;
`report.py --grading grading-v2` and `diagnose.py --grading grading-v2` read it.

Spend: every paid call goes through the round's ledgers and caps; failed and uncertain
calls stay counted. Native inference never resumes; a stopped native run is a new round.
