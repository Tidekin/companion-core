# Memory write reliability and source retention

The owner approved this follow-up on 2026-11-04, including explicit bounded failure
recovery. The v5 campaign and its bound source, inputs, answers and final receipt
remain immutable. This change does not deploy a production version.

## Reviewed writes

The existing relation review receives each candidate's exact quote, speaker and
update intent, plus bounded original excerpts for existing facts. Its strict output
now requires `supported` and `replaces` in addition to `same` and `conflicts`.
Unsupported identity or other claims are rejected with an audit reason. General
conflict preserves both sides; replacement requires an explicit update and a unique
subset of validated conflict references. Rejected candidates cannot be referenced
by later candidates; retained candidate references are remapped before commit.

Only exact `{"ref":"m0"}` wrappers in reference arrays can be normalized to `"m0"`.
The strict schema, duplicate/forward/unknown-reference checks, source kind, subject,
permissions, accounting and post-await provenance validation still apply. Both
empty and nonempty response examples are supplied. No auxiliary model is added.

Replacement targets and their canonical attribute key are resolved before mutation.
Review finding no conflict cannot cause another property with a coarse matching key
to expire. The explicit native direct-add path retains its existing cross-origin
owner update and attributed relayed-update behavior. Confirmed owner corrections
remain protected.

Natural updates already record `valid_to` and a native `memory.replaced` relation.
History now traverses those relations alongside explicit owner correction edges,
without using the owner correction pointer for natural changes. Every returned
version still requires live supported provenance and the caller's full allowlist.
An explicit native replacement relation remains authoritative when older versions
used different attribute-key spellings. A still-pending conflict cannot overwrite
the canonical key selected for the explicit replacement.
Recorded/source timestamps are not invented real-world effective dates.

## Explicit failed-batch recovery

The shared native checkpoint remains the execution owner. Before a failed terminal
checkpoint is stripped, its source-bound payload is retained under
`memory_failed_batch:<batch_id>` in the existing `runtime_meta` store. The scheduler
does not pick these retained failures automatically. The completed-event bit still
means that automatic processing is exhausted; the failure record separately states
that saving failed. No reservation or charge is erased.

An authenticated owner console can inspect `GET /v1/console/memories/failures` and
request `POST /v1/console/memories/failures/{batch_id}/retry` with `{"confirm":true}`,
the existing Origin/CSRF headers and an idempotency key. The native operation checks
the owner inside its transaction, current authority, settled original admissions
(or native proof that a preallocated review intent was never reserved or sent),
timezone, live source proof and absence of active checkpoint work. It schedules at
most one recovery per failed batch, retaining the configured per-phase attempt cap
and all normal budget admissions. A valid extracted payload resumes at review;
extraction failures re-extract. UNKNOWN requests never enter automatic recovery.

Completion clears the retained replayable payload. The existing registered memory
erasure cleanup also removes retained failures when any supporting source is erased,
including replay after restoring an older backup. The API only returns failure
metadata, not candidate text or credentials.

## Context selection

Attachment focus reuses the existing BM25 scorer, retaining whole rows, bounded
headers, original line order and the existing character budget. Current explicit
topic matches outrank unrelated recent history; empty-focus continuation still
uses the configured recency protection. Quotation and owner attribution stay in
the original atomic history group.

Before pruning original context under request pressure, native memory tool results
can omit repeated inspection metadata while retaining every claim, memory/version
identifier, origin, review status, source identifier and source time. Tool calls
and returns are never unpaired. Native source snapshots, authority revalidation,
soft selection and hard paid admission remain in force.

This follows the project's recorded
[DSH compaction research](2026-11-02-memory-inputs.md), with its
[primary implementation](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/index.ts)
rechecked for model-free pruning, cancellation and bounded retry behavior. Existing
native checkpoint and owner-console operations carry recovery; no dependency or
separate worker is introduced.

## Verification and evaluation limits

Related regressions cover exact schema repair, invalid references, unsupported
identity, partial changes, canonical replacement, natural history, source erasure,
restart and once-only recovery, authenticated HTTP recovery and an independent
native worker with mocked provider transport, plus context source retention.

After the candidate and related tests are stable, repository-wide checks may run
once. A fresh full paid campaign, if needed, may run at most once: at most 800
question instances, DeepSeek and Qwen separately capped at 80 CNY for the whole
campaign including all arms, judges, smoke, retries and uncertain calls. Candidate
freeze precedes new bank authorship, using the same two pinned reference benchmarks
as v5. New answers do not authorize changing frozen candidate, criteria or inputs.
The final verification receipt records actual checks and any unrun scope.
