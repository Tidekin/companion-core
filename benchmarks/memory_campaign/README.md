# Memory campaign toolkit

Reusable memory evaluation for selected frozen Core runs, with optional Qwen RAG references
and controls. Every script takes a round directory;
counts, personas, categories and delays come from that round's bank, budgets and settings
from its `round.json`. Nothing about a particular bank is written in code.

## Round directory

```
<round>/
  round.json            settings (below)
  build_bank.py         this round's stories (uses authoring.py)  — optional, any generator
  bank/                 the bank (layout below), frozen by bank.py
  arms/<name>/          frozen source snapshots, run configs, per-persona results
  rag/                  optional RAG reference requests/responses/answers
  grading/              blind mapping, raw grades, review.jsonl, overrides.jsonl
  deepseek-usage.jsonl  qwen-usage.jsonl   conservative spend ledgers
  results.json summary.json memory-report.html evidence-diagnosis.md
```

Minimal `round.json` (all other keys have defaults in `roundcfg.py`):

```json
{
  "name": "memory-round-4",
  "arms": {"core": "<native PostgreSQL commit>"},
  "references": [],
  "postgres": {"connections_file": "E:/Companion Core/.local/private/persona-databases.json"},
  "budget": {"deepseek_total_cny": 49, "deepseek_inference_cny": 47, "qwen_total_cny": 50},
  "smoke": {"bank": "E:/Companion Core/.local/<an already exposed round>/bank"},
  "sensitivity_exclude_categories": [],
  "limitations": ["..."]
}
```

Useful optional keys: `bank` (path, default `bank`), `source_checkout`, `history_grant`
(signed history scope given to both arms), `native` (Core overrides), `rag` (models,
`top_k`, `top_n`, `observed_arm`, `qwen_key_file`), `grading.rubric` (`v1`|`v2`|`v3`),
`review` (`persona`, `delay_days`, `changed_per_family`), `parser`, `telemetry`.

Core runs require one operator-prepared, distinct PostgreSQL database per persona.
`postgres.connections_file` names a private JSON map from persona IDs to libpq connection
strings. Keep this file outside the round and exported artifacts. Allocation and empty-target
checks finish before inference; the run retains its databases and independent journals for
accounting review. `state_directory` lives separately under each persona's result directory.
The isolated PostgreSQL test configuration also supplies the eighteen lifecycle checks.

`arms: {"core": "<commit>"}`, `references: []`, and `controls: []` run and grade one candidate.
Coverage comes from the complete bank, including failed questions. Historical comparisons
can select multiple native revisions and `references: ["rag"]`; reports derive their columns
from that selection. Three independent judge votes and fixed reviewed anchors remain
available for a single candidate.

Judging and controls:
- `controls: ["closed"]` adds a closed-book control: the RAG generator and prompt with no
  history. It is graded blind with the arms; questions it passes are listed as answerable
  without memory and a memory-dependent score excludes them.
- `grading.judges: ["deepseek", "deepseek", "qwen"]` names the judge of each vote
  (definitions under `judges`; Qwen spend goes to the Qwen ledger).
- `grading.anchors: <file>` mixes `anchors_per_batch` answers with known verdicts into
  every batch; a batch that misjudges one is discarded and re-judged (up to
  `anchor_retries`), and each vote must beat the majority-class baseline by
  `anchor_gate_margin` or the grading manifest says `ANCHOR_GATE_FAILED`. Build the file
  from an earlier reviewed round: `python make_anchors.py --round <old round> --out <file>`.

## Bank layout

```
bank/asr-observations.jsonl            one row per WAV: asset, sha256, text, ... (real ASR)
bank/suite-v1/inputs/<persona>.jsonl   turns 1..n, strictly increasing "now"
bank/suite-v1/questions.jsonl          id, persona, group_id, now, cutoff_turn, question,
                                       source_ids, category, category_id, family,
                                       delay_days, kind, input_modalities
bank/suite-v1/review-only/criteria.jsonl  id, required[], forbidden[], source_ids,
                                       optional exact[[alt, ...]] and strata{}
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

`authoring.py` also offers `Ask` (more questions on one story's sources), per-story
`offsets`/`window`, `"{S2}"` date placeholders, `exact` groups checked verbatim beside the
judge, `strata` for breakdowns, and `chain(...)`: one attribute corrected several times by
the owner (optionally a later relayed claim), asked as current value, first value, value
on a given day and full change order. `report.py` adds per-stratum scores and a one-sided
z for "longest chains score below shortest".

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

### Frozen scoring and denominators

Before judging or reporting, the toolkit checks the answer-file inventory and hashes,
the answer-to-corpus freeze link, the original bank manifest and every listed file,
the frozen questions and criteria, and the history-file inventory used for report
evidence. Resuming a grading directory additionally requires the same rubric text, grading settings, anchor
file, judge configuration and resolved vote count (including a `--votes` override).
The final manifest binds the raw grades and grading freeze; duplicate/missing keys
or an unsuccessful anchor gate prevent publication of a normal report.
`--prepare-review` still works after a failed anchor gate so the evidence can be inspected.
Unresolved judgments or verdicts retained from a misjudged anchor batch need an explicit
review override (even when confirming failure), so judge failures do not become semantic
failures silently. A failed whole-vote anchor gate still requires a valid new grading run.

Never retrofit hashes into an old archive. Archives without these bindings remain
readable with their original toolkit and reports; a new grading run uses a fresh
`--out` directory. A changed corpus requires a new round. These checks do not authorize
paid regrading: obtain the round's explicit provider budget before making model calls.

The main `rate` keeps all planned question instances in its denominator, including
execution failures and successful requests with empty answers. Each score also gives
`answered`, `unanswered`, `execution_failures`, `empty_answers`, `semantic_failures` and
`answered_rate` (null when nothing was answered). `all_arms_answered` compares the
same nonempty, successful question IDs in all three arms; the IDs are saved alongside
it. Reports include per-persona scores and counts of instances, personas and fact groups
(scoped by persona). Repeated questions/delays within a fact group are correlated;
the exploratory stratum z values do not correct for this dependence.

Reference: [chat-memory-benchmark methodology](https://github.com/Ornn8/chat-memory-benchmark/blob/2d61dfabd8b62bb3624263102426ad72c0ba98ec/docs/methodology.md)
and [hash-bound grading](https://github.com/Ornn8/chat-memory-benchmark/blob/2d61dfabd8b62bb3624263102426ad72c0ba98ec/grading/README.md),
reviewed at commit `2d61dfabd8b62bb3624263102426ad72c0ba98ec`. We reuse these evaluation
principles within the existing campaign; no upstream dataset or implementation is copied.
Our full-cohort rate remains distinct from its submitted-and-graded denominator.

Spend: every paid call goes through the round's ledgers and caps; failed and uncertain
calls stay counted. Native inference never resumes; a stopped native run is a new round.

Native arms and personas execute serially so their local pre-provider timings do not
include competition from other arms/personas in this harness. The CLI accepts an explicitly
authorized provider total up to80CNY. For a separate smoke ledger, reserve its full cap
outside the main ledger cap so their sum stays within the user's provider allowance.
