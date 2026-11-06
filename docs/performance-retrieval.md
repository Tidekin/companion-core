# Responsive memory retrieval and stable prompt prefixes

## Foreground claims, maintenance and reply timing

Chat recovery and ContentService construction/polling no longer enumerate media directories.
Native lease/accounting recovery remains before claiming. Media cleanup runs in the existing
idle chat maintenance and Life maintenance lane, consuming at most 32 directory entries per
spool per turn. Unrecognized filenames consume the same allowance. A standard `os.scandir`
iterator continues the next batch and is closed at exhaustion, error or Core shutdown; after
restart a new scan begins. Actual source/expiry checks still happen when media is used.
Expired bytes are excluded immediately; their physical unlink follows the bounded maintenance
scan. Explicit erasure continues to remove its targeted originals synchronously.

Each raw-file decision takes its own write transaction, so an upload that created a file before
committing its row cannot be mistaken for an orphan. Valid originals need only a bounded header
and file-size check during maintenance; `get()` retains full validation at actual use. Generated
partial/final bytes belonging to a DISPATCHED job with its original live lease are retained until
the result commits or that lease ends. Erasure and live Outbox state remain authoritative.
Backup restore deletes copied originals by their erased image/file IDs at the final database
path before publishing the restored database, without a startup directory scan.

`claim_scope` classifies only real SQLite BUSY errors after rollback at explicitly pre-I/O
chat/poke/content claim boundaries. Native chat/content loops yield 50–150ms with jitter and
recheck shutdown/configuration before another scan. Their business attempts, original request
IDs and provider/delivery I/O remain outside that retry scope. Disk-full, I/O, corruption,
LOCKED, cancellation and errors with an active transaction still propagate. Native local idle
maintenance handles rolled-back BUSY separately. This does not add retries after external I/O.

Memory fusion builds rank maps in linear identifier work and async search reuses its fused
result for final ranking. Candidate eligibility, the candidate pool, rank explanations, source
revalidation and final selection remain the same. Owner delivery can reuse a parsed reply group
within one exact DB transaction/snapshot; later requests still check current sources, authority,
STOP, leases, reply revision, previous ACK and interval.

Timing boundaries are distinct:

| Stage | Measured interval |
| --- | --- |
| `t_model_request` → `t_model_done` | Existing logical model operation, including local admission/settlement |
| `model_transport` | Final model transport handoff through response headers |
| `model_http` | Final model handoff through response-body receipt, before JSON decoding |
| `t_onebot_send_result`, `t_onebot_send_ack` | Existing send operation, including local guard/preflight |
| `onebot_http` | Checked OneBot action transport through bounded body receipt, excluding its guard and JSON decoding |
| `t_bridge_send_ack` | Original input reception through first platform ACK |
| `t_reply_group_ack` | Original input reception through final platform ACK |
| `t_reply_segments_ack` | First platform ACK through final platform ACK, including configured spacing |

Transport timings describe local HTTP boundaries, not server-only compute or phone display.
`onebot_http` is emitted for checked send/profile/QZone transports; a guard refusal has no
transport timing. New model/OneBot timings include failed/cancelled attempts and are not success
or billing receipts. The benchmark reader accepts these fixed content-free RPC fields. Group
completion is emitted only after the final ACK; uncertain sends discard the in-memory timing
start. Interrupted/restarted groups without a retained start have no invented elapsed time.

Default write-lock diagnostics still select scopes taking at least 500ms. For a bounded
diagnostic capture, set both `COMPANION_TELEMETRY=1` and `COMPANION_TELEMETRY_ALL_LOCKS=1` on
the measured processes. That includes fast commit/rollback/BUSY scopes; otherwise the samples
cannot establish all-transaction percentiles. Lock records remain on stderr, separate from stage
records. Disable full lock capture after measurement and apply the deployment log limits.

Regression checks use comparison counts rather than wall-clock thresholds for fusion growth,
real SQLite writers for contention, actual WS/HTTP ingress plus an independent native worker,
and checked native segmented delivery. Only the external model and OneBot transport are
synthetic. These maintenance measurements establish no production P95. The bounded retrieval
section below covers the subsequent candidate changes; full-history ledger checkpoints and
sustained 24–72h latency acceptance still need separate evidence.

Implementation references: existing Core/Life maintenance and Store rollback scopes;
[Python scandir lifecycle](https://docs.python.org/3.12/library/os.html#os.scandir);
[SQLite primary/extended result codes](https://www.sqlite.org/rescode.html).

## Chat validation and Bridge decoding

Mood source validation, recent history and episodic continuation share one batch,
`owner_input.history_batch`, read from one DB state: a write transaction or a
`Store.read_or_write` snapshot. Every retained source still passes the original input,
capture-signature, recall, retention and provenance checks. The job authority view and
original evidence are reused only within that batch and while the connection has made
no writes. The batch rechecks current authority, independent journals and expiry before
returning; a changed connection retains the per-source path. Each later context check
starts a new batch, including after model I/O.

## Read snapshots and writer contention

SQLite has one writer. Production telemetry on 2026-11-02 showed Bridge owner checks
(about 1.7 per second), each worker's configuration boundary and per-source authority
checks all taking `BEGIN IMMEDIATE`, so a chat reply waited up to 5 s per transaction
and spent most of 43–125 s outside the model. `Store.read_or_write` runs a read-only
operation on one WAL read snapshot with `PRAGMA query_only`. WAL readers do not block
the writer, and the snapshot is a committed state from within the call. Any write
statement, a nested write transaction, an independent journal or erasure ledger ahead
of the snapshot, or any other failure repeats the operation once under
`BEGIN IMMEDIATE`, whose result is authoritative. A snapshot never creates the budget
journal, and the shared journal view is neither filled nor reused once the database
files differ from those at the snapshot's start. Inside one write transaction or
snapshot, the journal view key reuses its table summary until this connection writes.

The owner check, an unchanged configuration boundary, history sources, mood signals,
original input-state receipts and memory continuation use it. Empty delivery and meme
polls also use a snapshot when no row needs leasing, expiry recovery or cancellation;
they retain their original scope checks. Real leases and every other mutation keep
their transactions. Input-state and Bridge meme-lease HTTP calls use the existing
`ApiStore` executor and independent connections, so a journal or lock wait does not
block the API event loop. The executor still joins a submitted operation on cancellation.
Concurrent query-only readers use the existing journal-view lock while constructing a
cold shared view, then recheck the full byte/database key when they acquire it. The lock
is released before any failed snapshot retries under the SQLite write transaction;
mutation paths retain their original transaction and cache invalidation boundaries.
Maintenance lanes report progress only when they changed rows, so the Life worker no
longer restarts a full sweep after bookkeeping that wrote nothing.

Segments of one reply reuse a ready OneBot probe for up to 10 s (`PROBE_REUSE_SECONDS`).
A failed or unacknowledged send, a different authority or a stopped runtime probes
again, and send-check, lease and binding fences still run for every segment. Context
building rechecks the job's reply revision between stages, so a reply superseded by
newer owner input stops before history and planning instead of finishing its build.

The SDK response checkpoint relies on `_save`'s source check immediately before its
write, avoiding a second identical check earlier in that transaction. Settlement and
the final job/Outbox transaction retain their existing boundaries. This reduces CPU
work while SQLite's single writer is held; WAL and durability settings are unchanged.

Bridge codec reuse follows `paid_budget.journal`'s immutable process-local `marshal`
view. Only already parsed and hash-checked JSON is serialized, and each caller receives
independent objects. The disk journal remains the original JSON hash chain. Cache state
retains at most 16 MiB of source bytes and 16 MiB of serialized records; a larger serialized
view is evicted. Journals above that input bound reuse an LF-terminated prefix and fully
validate the tail against its head and sequence, preserving the existing 32 MiB file
limit. Rewrites, truncation, permissions, STOP and all runtime projection checks remain
live. Decoding cached records is not authority to send.

When only the spool DB changes, `OwnerRuntime` can reuse its existing read-only decoded
records if the complete current journal bytes and owner row still match. A fresh native
count-only read repeats file permissions, size and hash-chain checks and must match the
retained record count and current journal head. Every original replay and DB projection
check still runs, including current results, held input, media, handoff and STOP custody.
Changed bytes or owner rows retain the original full decode path.

Native delivery-resolution verification builds one local prefix index while walking the
journal. Each historical proof still sees only its preceding records, including the
original prior head and latest STOP/confirmation across handoffs. Current result reads,
binding checks, signatures and final DB comparisons run for every proof. Later ACKs or
handoffs cannot change an earlier proof's cutoff. This follows the existing synchronous
handoff table batch; the index is local to that call and caches no authorization decision.

Core's paid-journal codec also retains immutable decoded records, with its existing
1 MiB source and serialized-record bounds. An exact LF-terminated prefix can survive
small appends; every remaining line is decoded and checked against the prefix head.
Every read still reads the complete file. Within the existing frozen-scope key memo,
up to 4 MiB of exact current bytes can reuse their SHA-256 digest. A different scope,
own write, path or byte sequence invalidates that memo; larger files use the original
digest-key path. The separate 1 MiB validated-view bound and all DB/file checks remain.

A rebuilt view still folds all journal records and checks the current DB projection.
Historical renewal baselines read admission windows in one batch, then select the same
requests present at that journal cutoff. Carry-chain validation batches its original
grant/revision range and parses each exact policy JSON once per call, retaining every
revision, erased-policy and transition check. Authority projections compare every stored
revision, but resolve each distinct journal path at the start and end of the call and
hash each distinct policy JSON once within the call. Path drift, missing admissions,
changed policies and invalid erased rows still reject the view. No policy, permission
or filesystem result is reused across these validation calls.

These changes leave model generation settings, prompts, output allowances and reply
segmentation unchanged. Production timing comparisons belong to the verification
receipt, with receipt-to-platform-ACK separated from provider transport and phone display.

Reused implementations and primary references:

- `companion/owner_input.py` provides the original synchronous source-validation batch.
- `companion/paid_budget.py` provides immutable serialized views with caller-owned copies.
- [SQLite isolation](https://www.sqlite.org/isolation.html) describes its single writer
  and isolation between connections.
- [SQLite WAL](https://www.sqlite.org/wal.html) documents that readers and the writer do
  not block each other and that each read transaction sees one end mark.
- [PRAGMA query_only](https://www.sqlite.org/pragma.html#pragma_query_only) refuses every
  database change on the connection, including statements that match no rows.
- [Python marshal](https://docs.python.org/3/library/marshal.html) documents the process-local
  serialization types and the requirement to reject untrusted serialized input.

The reply recall and extraction-review paths previously called synchronous FastEmbed from an
async task. ONNX's own worker threads do not yield the calling event loop. An offline probe with
the actual cached BGE model and 32 uncached synthetic memories observed a 504ms heartbeat gap;
the cached-vector path was much shorter. This is a local observation, not a production percentile.

## Execution boundary

`Memory.search` and `search_async` share eligibility, lexical retrieval, ranking and final source
checks. `SemanticIndex` shares cache lookup, pure inference and cache commit. Only model loading
and fully materialized passage/query inference move through `asyncio.to_thread`. SQLite access,
Ledger checks, proof capture, revalidation and vector writes stay on the original caller.
The existing two-thread encoder/cache is retained. A standard lock serializes memory inference
for a cached model path, including CPU work still finishing after its caller was cancelled.
No new executor pool, index store, task queue, budget or retry policy is introduced.

Before waiting, async retrieval pins the candidate memories and all their source ancestry with
the existing `MemoryContext.pin` proof. On return it checks the caller's current input/scope,
privacy version, current-time source proof and full memory rows before any vector write. The
original conditional cache insertion and final ranking/source fences remain. Cancellation does
not reach cache commit; a completed CPU task is not authority to publish its result.

| Caller | Route |
| --- | --- |
| Reply recall | `recall_async` → `search_async`; current job/history and source proof rechecked |
| Extraction/explicit memory planning | `search_async`; candidate sources and optional job rechecked |
| HTTP memory search | `search_async`; existing actor and live-window restriction retained |
| Console memory listing | Existing synchronous `search("*")`; no embedding |
| Owner `/你记得什么` | Existing synchronous wildcard recall; no embedding |
| Direct synchronous context/search consumers | Same synchronous algorithm, suitable outside async I/O; native reply worker supplies precomputed reviewed hits |

Core construction retains its existing startup warmup. Runtime retrieval moves inference off
the event loop; this change does not move Core construction or its SQLite connection to a thread.

## Prompt assembly

Expression and capability renderers expose static rules separately from current offer/state
data. Core concatenates all static rules first, including image, rich-history and voice rules
discovered later in context assembly, then the dynamic material. Both stay in the original
system role. Offers, capability availability and source-dependent references still refresh for
each call; revoked offers cannot remain in a cached local prompt.

The controlled two-offer probe increased the common system-text prefix from 1,925 to 4,261 UTF-8
bytes, retaining the content/rich rules before the differing candidate IDs. These bytes are not
provider tokens or measured cache hits. DeepSeek persists complete prefix units and cache hits
are best-effort; no cost or remote cache-hit improvement is asserted.

The soft context estimator, paid upper bound and memory relevance-review policy are unchanged.
Empty recall still skips review. Tokenizer changes need separately validated full wire counting;
the presence of official V4.1 material does not establish a counter integrated in this runtime.

## Reused implementations and references

- The local `companion/meme_core.py` pure `_embedding`/`asyncio.to_thread` boundary and its
  before/after source checks provide the existing project pattern.
- [DSH system-prompt at 22ddee3](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/core/system-prompt/src/index.ts)
  separates ordered static sections from dynamic context. Core reuses that ordering principle
  while preserving its own role and authority contracts.
- [Python asyncio.to_thread](https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread)
  supplies the standard thread boundary. It does not imply cancellation of work already running.
- [FastEmbed ONNX embedding](https://github.com/qdrant/fastembed/blob/main/fastembed/text/onnx_embedding.py)
  and the installed 0.9.0 implementation were inspected: embedding is a synchronous iterator.
- [DeepSeek prefix-cache rules](https://api-docs.deepseek.com/guides/kv_cache/) describe complete
  persisted prefixes; [token usage](https://api-docs.deepseek.com/quick_start/token_usage/) distinguishes
  estimates from actual returned usage.

## Verification

Deterministic tests keep inference pending in a worker thread while the event loop runs and
exercise forget, correction, source deletion/edit, expiry, new input and cancellation. They
assert no stale cache writes or model calls. A real HTTP check keeps health responsive during
retrieval, rejects a concurrent search-source withdrawal, and completes an independently opened
Core worker through mocked provider transport and native memory-review/chat settlement.
Source-aware image/history, console, extraction and synchronous ranking callers are checked too.
The exact frozen candidate and checks belong to the single verification receipt.

## Reply query reuse and bounded follow-up

Fact search and raw-turn ranking reuse a query vector only inside one reply, keyed by
encoder instance and query. The existing inference lock still serializes model calls.
A standard `ContextVar` propagates the reply's bounded cache through `asyncio.to_thread`;
the reply clears it on success, error or cancellation. Late CPU work cannot repopulate
a closed cache. Source proofs and SQLite vectors are revalidated as before. The two
raw-turn hops also share their document bigram bags; their query terms stay separate.
The installed Python `asyncio.threads.to_thread` and `contextlib.AsyncContextDecorator`
implementations were inspected for context propagation and coroutine lifetime.

Within a reply, memory tools return full evidence once per version (including evidence
and review-state changes), then an ID marker. Two consecutive calls without new evidence
add a stop hint; later calls return `STOPPED`. An unsuccessful summary lookup can use
one raw-turn lookup, bounded to four turns / 6,000 bytes and the current signed history
allowance. Both memory and history permissions and the original source proofs remain
required; erasure fences tool results and the final answer.

Extraction uses batch-local event, attachment and episode-group refs. Core expands them
before strict schema, quote, group and source checks. One local bracket repair can close
missing delimiters outside strings; it never completes a string or invents evidence.
Unrepairable output retains the original accounted, bounded retry path. Candidate
rejections record content-free reason codes, including sensitive source/summary,
redaction, unsupported self observation and a preceding owner correction. Source and
summary filtering both remain active; an explicit bank-card opening event permits only
the generic card noun in its summary. Combined extraction keeps the interaction-signal
field; native mood enablement still governs its recording.

Local query timings measure the retrieval pair with one caller and warm document vectors,
not provider latency, overall chat latency or answer accuracy. Round 5 answer-quality
acceptance must freeze the candidate before authoring fresh questions and running the
existing anchored judges and closed-book control.


## Reply-local retrieval work limits

A reply owns one work allowance through the existing `retrieval.query_scope`. Prefetch,
raw-turn recall, episode continuation and later memory tools share it, including nested
scopes and CPU threads. Closing or cancelling the reply closes the allowance. Independent
replies have independent counters. This bounds optional retrieval work; original owner,
financial-journal and erasure-ledger custody checks still inspect their complete authorities.
It does not guarantee a fixed end-to-end response time or bound provider latency.

| Resource | Per reply | Per operation |
| --- | ---: | --- |
| Nominated fact/turn/episode/revision records | 256 | 96 facts; 192 raw turns; existing episode/revision heads |
| Optional text materialization and proof reads | 1 MiB | Size checked before reading memory/event/reference bodies |
| Cold passage vectors, shared by facts and turns | 16 | Missing vectors receive no fabricated semantic score |
| Query vectors / fact searches | 8 / 8 | Existing encoder-specific query reuse |
| Cached vectors scanned / bytes | 4,096 / 8 MiB | At most 2,048 cached vectors per nomination |
| Optional source-node visits | 2,048 | At most 512 distinct nodes and 64 positive roots per proof |
| Optional nomination SQLite VM instructions | 3,000,000 | Rounded up in 1,000-instruction units |
| Cross-encoder document pairs | 48 | Existing tokenizer length and batch limits |
| Recalled attachment records | 128 | At most eight file/image items and eight transcripts per turn |

Consumed proofs have independent 512-node / 64-root / 1 MiB limits: exhaustion of optional
nomination cannot authorize a truncated proof or disable final source validation. Full selected
proofs, row identities, current privacy epochs and caller permissions are rechecked after
inference and at the existing dispatch boundaries. Legacy QQ correction matching shares the
same exact command predicate as retention; proof traversal follows indexed adjacency rather
than running the unbounded descendant-erasure closure. Erasure itself retains the full closure.

Fact nomination combines exact memory IDs/fact keys, native FTS5 rank, a bounded cached-vector
scan and a bounded native ordered head. Exact and lexical channels have finite heads so a
broad lexical query leaves room for semantic nominees. Hyphenated identifiers stay intact.
Every nominee is re-read and source-validated on the original Store before use. Confidence
repair and importance refresh touch nominated rows only; there is no foreground global sweep.
An old synonym-only fact outside the cached-vector scan or not yet indexed can be missed.
This limitation is explicit; this is not an ANN index or a claim of complete semantic recall.

Optional SQL runs on its own read-only, query-only connection with its own progress handler.
It never replaces a Store callback, interrupts a live writer, or turns I/O/corruption errors
into empty history. A writable/unrecognized active transaction refuses nomination; a native
read snapshot may nominate on the separate connection and must re-read identities in its
original snapshot. The existing full authority fences remain authoritative. Encoder/reranker
contention returns the available lexical/fused candidates instead of queueing behind an older
CPU task. Cancellation prevents late source/index commits; an already running CPU thread may
finish its bounded batch.

When any head, text, SQL, vector or source allowance limits coverage, recall returns
`retrieval_coverage` with counters, limits and reason codes. An empty result is
`COVERAGE_LIMITED`, with “本轮检索覆盖有限，未命中不代表主人没有说过或没有这段记忆。”
The note also enters native prompt assembly and raw-turn tool fallback. `NO_RECORD` remains
reserved for an exhausted eligible search without a work-limit signal. Counts are local work
evidence, not source authority, delivery receipts, token usage or answer-quality judgments.

The existing Life RoundRobin adds one derived-index unit: scan 16 headers, compute at most
eight missing vectors and read at most 64 KiB of fact text. It uses existing source pins,
conditional cache writes and the `memory_vectors` invalidation trigger. Foreground precedence
prevents starting another unit; a thread already computing does not hold a DB writer.
The cursor lives in existing runtime metadata; an empty index does not write a zero cursor or
report false progress that restarts the idle sweep. There is no second task queue or memory store.

Owner Console catalog pages and complete pending totals use the existing `MemorySources`
validator directly, with the existing final privacy/source recheck. These explicit administrative
catalog reads remain complete; they do not inherit the reply relevance-search allowance.
A 206-record pending total remains 206 when a reply has insufficient nomination allowance.

Migration `0066_chat_retrieval_bounds` adds SQLite indexes for nomination, fact keys, vector
version, evidence adjacency/order, per-origin sent replies and reference owner/kind. Transcript
replay looks up nominated owners through the JSON expression index instead of enumerating
all historical transcripts. Recent dialogue chooses the latest eligible reply per indexed
origin instead of grouping the whole conversation. The native 120/1,200-turn probe changed
from 17,100/167,100 VM instructions to approximately 1,100/900 (100-instruction sampling).
Migration is required before using this code; release coordination treats it as a schema change.

Image-derived facts recheck later image disputes on the same Store used by the checkpoint,
paid admission or delivery transaction. Migration `0068_image_review_target` indexes the
existing review receipts by their target event. This source-adjacency lookup retains the
attachment-row and text-byte limits and sees disputes written in the current transaction;
it does not run optional nomination on a separate snapshot. The migration adds only an index.

Paid dispatch prepares its measurement before entering the writer and reuses that in-memory
measurement only while both the exact wire payload hash and counting-contract hash match.
Reserve and dispatch now count twice in total, both outside the writer; original request,
reservation, configuration, journal/fsync, final transport and actual settlement checks remain.
`dispatch_measure` separates this local counting time from `dispatch_journal`.

Verification separates work bounds from recall: native 120/1,200-fact fixtures check exact IDs
and fact keys, shared scopes, source/SQL/text exhaustion, cancellation, writer survival and
bounded cache warming. The frozen 50-query offline suite keeps at least 42/43 required facts,
10/10 correct empty queries, at most 12 extra candidates and zero forbidden-source leaks.
Fresh 120/1,200-record cases separately cover cold and warm old facts, hyphenated IDs,
Chinese synonyms, dates, owner corrections, expiry, erasure and private scope. The pinned real
BGE encoder and existing 0.5 semantic threshold are used, with no provider calls. The more
abstract phrase “摄影设备的收纳位置” scores 0.4898 against the camera fact in the original
encoder, below the unchanged threshold; the positive synonym case “摄影器材收在哪里” scores
0.5257. These are retrieval checks, not generated-answer or live P95 acceptance.

Additional implementation references: [SQLite FTS5 native rank ordering](https://www.sqlite.org/fts5.html#sorting_by_auxiliary_function_results),
[SQLite indexed query planning](https://www.sqlite.org/optoverview.html),
[SQLite connection-local progress callbacks](https://www.sqlite.org/c3ref/progress_handler.html),
and [DSH bounded search at 22ddee3](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/fs/tool-fs-search/src/search-core.ts).
DSH provides the explicit overflow/abort-versus-no-match principle; Core retains its native
SQLite, proof and delivery contracts. No dependency was added.

### Confirmed relationship context

Ordinary chat also selects a small standing block of current, confirmed relationship facts,
independent of the current topic. `relationship_standing_limit` defaults to 3 and
`relationship_standing_chars` to 300; setting either to zero disables it. Episodes,
hypotheses, unresolved conflicts and unconfirmed observations stay outside this block.
The filters run before bounded nomination so temporary episodes cannot crowd out a lasting
agreement. Eligible records are ordered by existing stored importance, then recency and
memory ID. A partial SQLite index applies that ordering before the candidate cap, so newer
low-importance records cannot push an older important agreement out of the candidate pool.
Importance only ranks candidates; it does not confirm a fact or replace source validation.
Selection reuses the reply-local SQL/candidate/source limits and `MemorySources`
validation, without repairing facts or writing a second memory store. Facts that do not fit
the character allowance are skipped whole.

The block retains native origin and evidence, carries its own source proof, and has required
context priority even when topic retrieval found the same fact. Topic references omit those
duplicates. Current input and full paid admission keep their existing budget rules. Memory
permission is still required, and signed bounded-text windows keep only their reviewed facts.
Erasure, expiry and source changes invalidate the same native proof before generation/delivery.
