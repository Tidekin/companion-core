# Responsive memory retrieval and stable prompt prefixes

## Chat validation and Bridge decoding

Mood source validation uses the same synchronous write-transaction boundary as
`owner_input.history_sources`. Every retained source still passes the original input,
capture-signature, recall, retention and provenance checks. Original authority evidence
is reused only within that batch and while the connection has made no writes. The batch
rechecks current authority, independent journals and expiry before returning; a changed
connection or a caller-owned read transaction retains the per-source path. Each later
context check starts a new batch, including after model I/O.

The SDK response checkpoint relies on `_save`'s source check immediately before its
write, avoiding a second identical check earlier in that transaction. Settlement and
the final job/Outbox transaction retain their existing boundaries. This reduces CPU
work while SQLite's single writer is held; WAL and durability settings are unchanged.

Bridge codec reuse follows `paid_budget.journal`'s immutable process-local `marshal`
view. Only already parsed and hash-checked JSON is serialized, and each caller receives
independent objects. The disk journal remains the original JSON hash chain. Cache state
retains at most 8 MiB of source bytes and 8 MiB of serialized records; a larger serialized
view is evicted. Journals above that input bound reuse an LF-terminated prefix and fully
validate the tail against its head and sequence, preserving the existing 32 MiB file
limit. Rewrites, truncation, permissions, STOP and all runtime projection checks remain
live. Decoding cached records is not authority to send.

These changes leave model generation settings, prompts, output allowances and reply
segmentation unchanged. Production timing comparisons belong to the verification
receipt, with receipt-to-platform-ACK separated from provider transport and phone display.

Reused implementations and primary references:

- `companion/owner_input.py` provides the original synchronous source-validation batch.
- `companion/paid_budget.py` provides immutable serialized views with caller-owned copies.
- [SQLite isolation](https://www.sqlite.org/isolation.html) describes its single writer
  and isolation between connections.
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
the generic card noun in its summary. The extraction prompt does not request mood fields
when mood is disabled, and mood recording behavior is unchanged.

Local query timings measure the retrieval pair with one caller and warm document vectors,
not provider latency, overall chat latency or answer accuracy. Round 5 answer-quality
acceptance must freeze the candidate before authoring fresh questions and running the
existing anchored judges and closed-book control.
