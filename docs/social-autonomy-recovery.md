# Social planning recovery and historical quota reads

Owner chat can read up to three recent nickname, signature and avatar execution receipts
through the existing Core context reference and snapshot machinery. This requires the current
native `history`, `social` and `account_actions` purposes and an owner-private history scope;
the signed history time, count and UTF-8 bounds still apply. Only matching original frozen
plans, Outbox content, owner identity, lease-bound native receipts and source fingerprints
are included. Source-only MemoryContext pins preserve erasure and source-change checks after
the model await, without adding a fact, model call, delivery or database representation.

The bounded view distinguishes success, rejection and uncertainty. A current PAUSED or
UNAVAILABLE capability cannot establish that an earlier action never happened, and a cached
profile observation cannot establish who changed it. Missing history is not an exhaustive
denial. Historical native authority evidence may survive a successor revision for this read;
it never re-authorizes an old action. Receipt times and values describe that operation, rather
than guaranteeing that the current external profile is unchanged.

Social planning keeps the existing plan, model request, source snapshot, native budget and
Outbox identities. Recovery does not reconstruct an unstored model response or repeat an
admitted request. Revision `0036_social_plan_jobs` adds a one-to-one plan/job mapping; execution
uses the existing `jobs` queue, leases, source ledger and Core worker.

## Recovery boundaries

| Retained state | Background action |
| --- | --- |
| Expired PLANNING, no model usage, no active native admission lease | Mark CANCELLED with SOCIAL_PLAN_EXPIRED_UNADMITTED; retain candidate/source/audit identities |
| Expired PLANNING, SETTLED social usage, no active admission lease | Mark CANCELLED with SOCIAL_PLAN_RESULT_LOST; retain actual cost and do not request replacement text |
| PLANNING with RESERVED or UNKNOWN usage | Preserve the original liability and reconciliation block |
| PLANNING with an unexpired plan or native admission lease | Leave the other worker's state intact |
| REJECTED by an allowlisted pre-admission quota or foreground-yield refusal | Reconsider the same request only while its original plan/source/authority remain valid and no usage, admission, cloud cost or action reservation exists |
| Paid/uncertain/interrupted/skipped/delivered candidate | Never regenerate it as a retry |

`budget.reserve` revalidates social scope, original messages, source/authority and expiry inside
its existing transaction. A successful readmission changes REJECTED to PLANNING and reserves
the original request together. A still-refused admission rolls back that transition and its
audit. The mapped native job waits 60 seconds in QUEUED or RETRY_WAIT before another admission
check, retaining the original plan, source snapshot and model request. Polling before the due
time does not change the job or append refusal logs; each due refusal records its deferral.
Repeated API plan calls return the stored result. Only the native worker may claim the deferred
job, and its lease token is checked inside reservation and again after the provider returns.
Concurrent workers cannot both reserve the same request; a stale worker cannot finish or fail
the winner's job.

The job has one admitted attempt. A provably unadmitted refusal returns that unused attempt;
expiry of a worker lease may reclaim only that same unadmitted request. RESERVED, SETTLED,
UNKNOWN or any admission/cost evidence prevents replay. STOP, erasure and restore cancel mapped
jobs using existing native cancellation and source-ledger mechanisms. Jobs also bind the original
owner authority reference, so a successor revision cannot reauthorize an old deferral. Console plan results expose
the native job state and next due time. A populated mapping blocks downgrade for explicit data
review, preserving original jobs and accounting history.

The original expiry is never extended. If an equivalent Reader refetch receives a new event ID,
the background worker retries the original frozen references, not the newer observation. Each
reference is checked again; refetch cannot repair revoked authority or erased original evidence.
Source deletion keeps using the existing erasure ledger. No model adapter owns storage or delivery.

Social planning requires the current `social` purpose. Account execution additionally requires
`account_actions` and the original independently configured action permission. Plans pin the owner
reference in their source snapshot; native retry jobs and Outbox rows retain that same reference.
The full social policy hash still protects configured action limits and payload rules. A grant
revision is excluded from content-candidate identity so it cannot manufacture another candidate.
Account send/probe DTOs carry `authority`, not a window hash. STOP may clear an unstarted Outbox
lease; the retained lease receipt preserves its exact token for matching no-I/O cancellation.
An old token, different authority or any recorded dispatch cannot use that cancellation path.

The native Bridge host discovers the account policy fingerprint through its current authority
check, then checks both `account_actions` and `social` before leasing the account lane. It uses
the existing frozen account preparation and exact account send-check at the final HTTP boundary.
Private replies retain priority. Profile observations use the same native runtime, checked
private HTTP transport, public-avatar DNS pinning and durable 60-second cooldown; pending owner
input interrupts the idle probe. Profile journal records retain hashes, not observed text.

An ambiguous account write retains the original account payload, lease and send-check hashes
in the existing attempt journal. After STOP, reconciliation validates the signed original
authority revision, frozen social plan, reservation and UNKNOWN account receipt. It reuses
the existing manual SENT/CANCELLED decision and explicit handoff; reconciliation does not
consult a successor grant to authorize the old write.

Host assembly follows the inspected DSH QQ plugin
[gateway bootstrap](https://github.com/tencent-connect/dsh-qqbot/blob/c8b9158b37817b0dbc1e514e62e16204ad47a6ce/src/gateway/bootstrap.ts)
separation of protocol sender and lifecycle. Current permission checks reuse
[RFC 7662 section 2.2](https://datatracker.ietf.org/doc/html/rfc7662#section-2.2);
the account plan, cancellation receipt, Spool journal and reconciliation protocol remain the
existing Core/Bridge mechanisms.

This migration reuses the existing Core journal, native jobs and Outbox. The boundary follows
[RFC 7662 current-state and scope checks](https://www.rfc-editor.org/rfc/rfc7662.html#section-2.2)
and [SQLite transaction isolation](https://sqlite.org/lang_transaction.html): pin under the native
write transaction, then re-read current authority at execution boundaries. DSH's
[schedule implementation](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/schedule/schedule/src/runtime.ts)
also separates durable inbox acknowledgement from task state; it does not supply QQ delivery proof.

Domain migration checks cover owner revision/STOP, restored authority, native lease substitution,
source erasure, charged UNKNOWN and account-purpose separation. The shared Core/API/transport
cutover and enabled independent-worker acceptance must be verified on the integrated candidate;
earlier window-based process receipts do not certify the new owner-authority boundary.

## Quota reads and local dates

Poke claims use indexed conversation/date COUNT and latest claimed-at MAX, rather than loading
the complete claim history into Python. Local-day boundaries use the configured IANA timezone
and convert to UTC for the persisted timestamp range, including DST-length days.

Social capacity loads the relevant action pool and the current-day/cooldown horizon, plus every
potentially unresolved reservation regardless of age. Original owner-reconciliation proof still
determines whether an ambiguous cancelled action is resolved. Known completed history outside
that horizon stays stored but is not materialized for each check. This is not permission to
truncate unknown liabilities, delete receipts, reset cooldowns or discard model charges.

Social's persisted timezone must match the current configured budget timezone. A mismatch now
fails before consuming a candidate with SOCIAL_TIMEZONE_BINDING_CHANGED. This prevents silent
calendar divergence; it does not migrate historic quotas. An intentional timezone transition
still requires a reviewed migration policy preserving the original budget/accounting semantics.

## Reused implementations and evidence

- DSH `0.2.1-rc.3`, fixed `22ddee3792402c3e70ec5f86a63c3e3e895a2f60`:
  [jobs-local](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/jobs/jobs-local/README.md)
  is explicitly process-local. Its terminal-result ownership informs fencing; Core continues
  to use its own durable native ledger rather than copying that executor.
- [Temporal activity lifecycle](https://docs.temporal.io/activity-execution) separates task
  timeouts from retry policy; at-most-one attempt can time out without replay.
- [AWS idempotent API guidance](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
  motivates atomic identity/admission and preserving ambiguous results, not blind retries.
- [Python zoneinfo](https://docs.python.org/3/library/zoneinfo.html) supplies the existing aware
  local-calendar conversion; no fixed-offset approximation is introduced.

Regression checks cover actual child-process death before reservation and after settlement,
valid native admission leases, a stale process resuming after expiry, same-request concurrent
readmission, native budget refusal, midnight/refetch recovery, source/authority invalidation,
old UNKNOWN responsibility and indexed poke day boundaries. Existing real HTTP and independent
worker tests exercise deferred native admission through the real HTTP window and delivery
lease endpoints with mocked provider transport; child-process tests cover lease recovery before
reservation, after reservation and after settlement. Cancellation, stale tokens, console due
times and populated downgrade protection have dedicated checks.
These are synthetic evidence, not real QQ acceptance or permission for paid calls/deployment.

## Profile-purpose sources

Nickname and signature generation use a separate `owner_confirmed_profile` input. Public
Reader material, summaries derived from it, reviewed avatar descriptions and the current
account-profile text do not enter that prompt. QZone retains its public-source purpose;
avatar selection retains its reviewed-asset contract. Mixed public/profile-text requests are
rejected instead of silently changing their meaning.

`social_profile_sources` reuses native Memory source closure, pinned evidence and ledger custody.
Eligible material is a current, conflict-free self fact/preference or owner preference, with a
native `memory.owner_decision` audit matching either the actual direct-owner QQ command or a
typed `memory.console_decided` event from the authenticated owner Console. Console confirms and
corrections recheck the control credential, session, CSRF and Origin inside the native Memory
transaction. The event binds the request, exact before/after memory hashes, content, decision,
origin and original evidence roots; the matching audit and source dependencies commit with the
memory change. It contains no session cookie, CSRF token or control credential. Idempotency uses
the existing Console operation receipt. A model-produced CONFIRMED flag, `user_confirmed` origin
or generic `console.operation` audit is insufficient. Historical Console decisions without this
native receipt and ordinary bridge-token memory API decisions remain excluded. Public/Reader or
granted-source ancestors, missing/erased evidence, invalid conversations and expired material
fail closed, including when other valid evidence remains. No new approval queue or authority
store is created.

Before generation, after model completion and before dispatch, Core reconstructs the same
material and verifies its original snapshot. The complete consumed event closure, including
the confirmation event, is attached to `social_autonomy_sources`; existing erasure hooks cancel
pending jobs/Outbox actions and remove the stored material. A change after payment preserves
actual fees. An old nickname/signature plan without the profile-purpose proof cannot pass final
dispatch; unreserved READY plans are cancelled by the native runtime. Previously dispatched or
uncertain deliveries retain their liability and are never revived.

The implementation reuses [Memory source pinning](../companion/memory_context.py),
[native owner decisions](../companion/memory.py) and the existing profile-probe text rules.
The prior DSH execution/approval separation remains applicable. Primary guidance from
[OWASP prompt-injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
supports enforcing permissions and parameters at the operation boundary. This change enforces
a specific source-purpose boundary; it is not a claim that character filters or prompts eliminate
all semantic injection. Owner-confirmed material remains subject to the existing private-text
and sensitive-memory restrictions.

Synthetic checks cover automatically confirmed memories, real owner confirmation/correction,
Reader-derived ancestry, missing and withdrawn evidence, forget during generation and before
send, old public-profile plans, unchanged uncertain accounting, and a real HTTP profile-baseline
API with an independent Core worker process and mocked provider transport. The same real HTTP
check confirms and forgets material through the owner Console. Dedicated checks cover Console
authentication, credential rotation before the native transaction, receipt/version tampering,
transaction rollback, idempotency, correction ancestry and ledger cancellation.
