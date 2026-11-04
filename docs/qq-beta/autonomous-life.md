# Autonomous Life: source-bound expression

The feature defaults to OFF. It can inspect actual owner/private input and real Reader results,
record WAITING/DUE/CLOSED/EXPIRED topics, and generate a bounded decision through the original
activity job, model budget and Life worker. There is no new executor, factual memory store or
QQ sender. Real sending, paid experimentation and production deployment require their own
approval and live acceptance; the checks described here use synthetic external transport.

## Configuration and inspection

`Settings.life_loop` accepts `mode = OFF | PREVIEW | SHADOW`, at most eight sources per topic,
a topic lifetime up to seven days, follow-up delay (default one day), at most six daily decisions
by default and a one-hour decision interval. `wake_enabled` must also be on to schedule SHADOW.
Quiet hours, owner pause, current input, idle time, existing budget and uncertain outcomes gate
generation. No eligible source means no Life model call. PREVIEW makes no new tasks or model calls.

`LifeShadowApproval.source_scope` defaults to `selected`, retaining its exact `source_event_ids`
and unchanged serialized policy identity. An explicit `owner_private_history` approval uses an
empty selected list and the current OwnerAuthority private-history contract for the same
conversation. It requires the independent history purpose, original signed input/custody,
`not_before`, retention, recall and current source validity. Its private context also retains
the history turn/UTF-8 bounds, alongside the original Life source, item, time and budget limits.
It grants no QZone source purpose or channel action. Reading results still require the original
successful, settled Life/Goal/Reader proof under this approval; ordinary historical observations
do not inherit Life permission.

Private Web reading material uses the completed Reader v3 receipt, its frozen source context,
original URL hash and result membership. This includes query-bearing pages admitted by that
receipt. The private view validates the typed evidence and text hash and pins the reading
metadata in the Life source fingerprint. Metadata changes suppress retained wording. Public
sharing keeps its independent source rules; QZone remains in its separately approved view.

In this history mode, an actual new owner input that exceeds a topic's source or history-context
bound closes that bounded topic through native invalidation and starts a new topic. Original
inputs and independent execution/accounting records remain. This prevents an old eight-source
topic from permanently excluding every newer input, without dropping a newer message from a
decision or reopening a topic on silence. Selected-source approvals keep their existing bound.
The source check reuses `owner_input.source(history=True)`; topic closure reuses `invalidate`.
There is no new evidence store, executor, timer or message channel.

`life_loop.autonomy_enabled` defaults to false. When enabled, `life_context.decision_context`
adds deterministic guidance from the same approved `self.interest` evidence. Both goal roots
and the final result must belong to the activity's source set. Only completed native self
goals with independent FETCHED/WATCHED receipts contribute; plans, CONTINUE choices, partial
steps, DISCOVERED, FAILED and UNKNOWN do not count as completed exploration. Live context
excludes simulation. Receipt deduplication and retention reuse `interest.exploration_evidence`.
Guidance carries original source IDs and counts, without claiming expertise from session counts.
It has no separate clock, model call, persistent progress store, execution queue or cron job.
Generation and retained-result validation rebuild the same guidance. Native Goals, Reader,
Diary, Reflection, Outbox and their existing budgets/recovery retain ownership of state.
Alembic `0054_life_autonomy_projection` securely drops the five ad-hoc derived tables if a
standalone initializer previously created them. It retains all native evidence and accounting;
normal databases simply advance the revision. Downgrade does not recreate erased copies.

Interest association on a private draft is derived `related_trait_ids` readback metadata.
It requires the draft to cite the entire interest source closure; the strict model proposal
contains only `LifeDecision` fields. `interest_shift` is optional private-diary reflection,
rendered with the decision's citations and checked for sensitive content. It cannot be carried
into a private message, QZone or fictional creation, or become owner preference evidence.
Commit rejects it (`LIFE_INTEREST_SHIFT_UNSUPPORTED`) unless the guidance records a change
of view whose entire source closure the decision cites; model prose alone is not a change.

## Experiences, views and choices

The four stages stay in their existing owners and are never inferred from each other:

| Stage | Owner | Counts as |
| --- | --- | --- |
| Wants to | `life_activity_results` (EXPLORE/CONTINUE) | an intention only |
| Scheduled or started | `goals` / `goal_steps` QUEUED, RUNNING | work in progress only |
| Completed | DONE goal with a FETCHED/WATCHED Reader receipt | one experience |
| New view | JUDGED `watch_ledger` entry for that item | her local judgement |

`interest.exploration_evidence` lists independent experiences (deduplicated by content hash
and URL, retention and lineage checked) and attaches her live judgement of the same item, if
any. A self goal whose target URL appears in its owner source message is `owner_suggested`:
it can start a topic and its judgement shapes her view, but it does not count toward her own
engagement. Owner commitments never create interests; owner preferences remain in
`owner.preference`.

`interest.view` replays judged experiences in reading order over a five-experience window.
A view is `tentative` until at least three judged experiences on two local days agree;
it is `positive`, `negative`, `mixed`, `neutral` or `undecided`. Each change records the time
of the judgement and the experience (event, origin, rating, take) that caused it. `tendency`
counts only her own picks, and marks an interest `FADING` after a settled dislike or 30 days
without a new self-chosen read; FADING keeps its history. `/为什么形成偏好` renders these
causes and the underlying content records with `interest.explain`.

Life sees an interest only when an in-scope completed goal anchors it. Earlier experiences of
the same interest join the view when the same OwnerAuthority admitted their goal; her
judgements need `watch_ledger.enabled` and the `exploration` purpose. Guidance adds the view,
its latest reasons and changes, and options (`wait_for_view`, `pivot`, `pause`, `deepen`,
`continue`, `compare`, `reflect`, `rest`). `rest` is always present. Options carry no score,
quota, permission or budget, and the strict `LifeDecision`, Goal and destination gates are
unchanged. Session counts, depth and ROI are not reported as growth.
Retained-result validation rebuilds the same context, so a draft written on an earlier view
is suppressed (`LIFE_DRAFT_CHANGED`) once a later judgement, withdrawal or erasure changes it.

Diary and Reflection sources include her own takes for the day as `own_take`
(`take:<entry_id>`, evidence event = the read observation). They render as "我当时的看法",
do not count toward minimum sources, and the prompts treat them as views rather than facts or
growth. Owner withdrawal of a judgement (see `docs/watch-ledger.md`) removes it from views and
future sources, returns citing reflection hypotheses to `NEED_MORE_EVIDENCE` and withdraws
their unfinished goals. Erasure, expiry and restore replay act on the underlying evidence;
no view, change history or option is stored, so none can be restored from a backup.

- `GET /internal/v2/life/topics` uses the existing control token and `Cache-Control: no-store`.
  It returns currently valid source previews, topic states, and eligible private SHADOW results.
  Preview does not mutate state. Source fingerprints allow a reviewer to bind an exact source.
- `POST /internal/v2/life/topics/{topic_id}/close`, with `source_event_id` of an owner event in
  that conversation, explicitly closes the topic. The control token is required.
- Owner commands `/结束话题 [topic_id]` and `/不再跟进 [topic_id]` close matching topics and
  withdraw their current drafts; omission applies to the current conversation. The command itself
  is not a candidate.

Topic candidacy means there is an actual experience to consider, not that a model has established
an unresolved fact. Independent items in one conversation keep their own due time and source
set. An explicit update about the preceding item, or a new turn sharing concrete terms with an
active item, updates that item. Unrelated input still preempts in-flight wording. If the latest
input cannot be projected under current source authority, generation waits. Migration
`0064_life_topic_items` removes the one-active-item restriction and keeps the sources and
decisions attached to existing items. A read result binds to an owner conversation
through its source ancestry. Model-derived
memory, fabricated daily routines, quoted/rich-only inputs, private-group crossovers and management
commands are not new Life sources. Model selection can always SKIP.

The new pipeline replaces generic proactive scheduling and new SocialPlanner share scheduling
while PREVIEW/SHADOW is selected. OFF restores those existing configured paths. Already accepted
commitments, existing delivered/uncertain actions and their accounting retain their original
meaning; changing mode does not erase or replay them.

Private notices reuse the configured conversation guidance. Their compact dialogue reference
admits at most two turns, within the current history/source permission and measured context
budget. Reading evidence, required commitment wording and the structured Life decision keep
their own contracts. A prior SENT proactive notice using an admitted source can signal a repeat.
A QZone-capable decision gets its original source-only material.

With current Affect permission, “先别聊这个” pauses the preceding exchange. “我去上课了，晚点聊”
pauses private contact for the configured feedback lifetime. “今天别主动找我” ends at the next
local midnight within retention. Background reads and private records retain their own gates.
The explicit owner control “/暂停自主活动” uses the existing autonomy pause. The internal daily
decision limit covers all attempts, including silent ones. The QQ send cap and the separate
lull-check cap still apply at their original boundaries.

### Offline replay and owner acceptance

The ten timing cases in `tests/test_proactive_replay.py` offer identical wording for the same
export issue. Each case advances an independent clock, ingests only events visible so far, and
checks whether the native scheduler permits that particular private draft. Four checkpoints are
labelled appropriate and six are labelled wait. These labels describe the synthetic fixtures.

| Policy | Appropriate opportunities used (of 4) | Drafts allowed while labelled wait (of 6) |
| --- | ---: | ---: |
| Main at 30628cff | 2 | 1 |
| Wait for 120 seconds of idle time | 2 | 1 |
| Always silent | 0 | 0 |
| Independent items and scoped feedback | 4 | 0 |

At a fixed eligible time the content test inspects actual model input for the configured voice,
a delivered chat turn and the original reading. The provider supplies fixed wording. Further
checks cover independent internal choices, source recall, foreground preemption and billing.
For owner acceptance, review a few actual sent and deferred items together. Judge the
wording, what was repeated or missed, timing and the next two or three turns before tuning
personal intervals. An unanswered share has no implied preference label.

## Actions and destinations

The strict proposal uses SKIP, CONTINUE, EXPLORE, FOLLOW_UP, SHARE or DEFER. Source IDs must belong
to the captured input. CONTINUE must refer to an existing source-linked self goal. EXPLORE can
carry the native `GoalProposal` (`topic`, `source_event_ids`, `scope`, optional `priority`).
The default-false `life_loop.read_goals_enabled` switch submits these choices to the existing
GoalEngine only in SHADOW. It grants no read permission: existing goal, adapter/source, budget,
pause and window checks still apply. Without a concrete goal or with the switch disabled, the
result remains a reviewable intention. Terminal deduplicated goals are not executed again.

Submission happens in the original activity commit transaction. The Life worker schedules the
native goal; the independent Reader v3 worker returns its ordinary evidence and usage receipt.
Migration `0038_life_goal_binding` adds only the nullable native goal ID to the activity result.
Readback exposes the current native goal state, result event and error code. No extra execution
queue or retry policy is introduced. Wake's automatic new-goal selection yields while Life
read-goal submission is enabled; accepted goals and commitments retain their original paths.

Only a completed native step result linked to a successful, settled Life choice under the same
private approval can supply the next private Life decision. Its original goal evidence must
remain in that approval's exact source set, with unchanged fingerprints and current validity.
Unrelated reads and arbitrary descendant events cannot inherit this purpose. QZone draft
permission still requires exact per-result grants. A failed or uncertain read supplies no SHARE
evidence; native recovery and accounting remain authoritative.

`read_plan` proposals choose one to three distinct HTTPS targets from `read_targets`, derived
from the already admitted sources with the existing link parser. Migration `0045_goal_read_plan`
and GoalEngine own the ordered steps, source evidence and parent completion. One step becomes
active at a time; later steps wait for a separate decision after an actual Reader result.
Completing one step does not complete the parent or add an independent interest vote.

For an existing plan, Life's `CONTINUE` carries `goal_id` and the native `continuation` object:
`expected_version`, `action = CONTINUE | WAIT | ABANDON`, and either the complete remaining
`step_order` for CONTINUE or an aware `wait_until` for WAIT. It may reorder only never-started
steps in the original target set. It cannot change executed IDs/targets or omit the preceding
result from its evidence. GoalEngine applies the original authority, source checks and version
comparison in the same activity transaction. WAIT is finite and bounded by topic validity;
its due topic can receive one further decision within the existing per-topic/daily limits.
ABANDON cancels the tail while preserving completed results. UNKNOWN, source withdrawal and
stale proposals never create a replacement step or request.

The source comparison reuses DSH's [bounded todo list](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/todo/tool-todo/src/index.ts)
and [planning/permission separation](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/plan/plan-mode/src/index.ts),
with [persisted execution state](https://docs.langchain.com/oss/python/langgraph/persistence)
held in Core's existing Goal/Reader records. Model-written completion flags are never evidence.

FOLLOW_UP targets only a conversational topic and PRIVATE_MESSAGE_DRAFT. SHARE can choose
PRIVATE_MESSAGE_DRAFT or PRIVATE_DIARY; QZONE_DRAFT additionally needs explicit per-source purpose
grants. A private diary result reuses the original cited reflection validation and is a private
SHADOW draft until consumed through DiaryService. Ordinary closed-day diary generation remains
with DiaryService and the original nightly lanes. SKIP creates no prose. DEFER is bounded by source expiry
and permits one timer recheck against the same evidence. Two superseded drafting attempts also park the item until new evidence, retaining their original
usage and daily slots. A second DEFER waits for a related
source change or expires. The previous reason accompanies a timer recheck. Time and silence
alone never replenish attempts. SKIP closes the item.

A completed expressive choice or SKIP consumes its topic. Explicit closure, expiry, mode changes or restart
cannot reopen that candidate. New independent owner input can form a new candidate. Direct pressure
wording is rejected and the prompt excludes guilt, abandonment and demands for replies. Semantic
appropriateness still needs human evaluation before any live Beta; synthetic outputs do not prove
real model expression quality.

## 2026-10-31 independent Autonomy review

Comparison: `86737e1..4d743aa` (Autonomy commits `2dc4b10`, `4d743aa`), plus the directly
connected changes `94d3a6f`, `6fb4786`, `3624e5f`. The existing owner-jargon and memory-review
implementations remain in place. The following defects were confirmed against native contracts:

| Priority | Original location | Trigger and consequence | Repair |
| --- | --- | --- | --- |
| P1 | `autonomy.py`, `life_autonomy.py`, strategic/coordination imports | Enabling guidance imports absent `companion.protocol` / `companion.util`, preventing admission | Guidance uses the original scoped context; unused standalone layers removed |
| P1 | `autonomy_store.py`, `life_autonomy.py` queries | `Store.all`, `goals.trait_id`, `goals.completed_at`, `events.body` and `events.namespace` do not exist | Reuse native `rows`/`one`, goal evidence and observation contracts |
| P1 | `autonomy_tasks.py`, `db/autonomy_schema.sql` | Standalone SQL is outside Alembic and the wheel's bundled migrations; persisted derived text has no erasure/restore integration | Retire parallel stores through the native migration chain; native evidence supplies rebuilding |
| P1 | `life_autonomy.py` global priority/history/feedback scans | Reads unrelated interests and owner messages outside this activity's sources/purposes | Guidance only sees already validated decision context |
| P1 | `life_topics.py` post-decision hook | CONTINUE/EXPLORE is marked SUCCESS before Reader completion, with no receipt deduplication | Remove decision-time counters; native parent completion records interest once |
| P1 | `life_context.py`, `goals.py` | Unscoped final results leak into context; completion can commit before its interest evidence | Require final-result source membership; commit provenance with completion |
| P1 | `life_topics.py` commit/readback | Injected `related_trait_ids` breaks `LifeDecision(extra='forbid')`; clock-generated guidance differs on validation/readback | Derived metadata outside proposals; shared deterministic context builder |
| P2 | `life_protocol.py`, diary rendering | `interest_shift` accepts other destinations and is silently omitted from the diary body | Constrain, validate and render the private reflection with original citations |

Privacy withdrawal removes native evidence and invalidates retained drafts. A restored older
backup replays the latest independent erasure ledger before guidance can be read. OFF, PREVIEW,
pause, explicit closure, budget denial, model/Reader UNKNOWN, owner STOP and restore fences
continue through existing admission/commit/consume paths. No state in guidance can resume work
or release a budget/delivery reservation. A standalone initializer is not a startup step.

Regression coverage includes `test_autonomy_migration.py`, `test_life_autonomy.py`,
`test_life_decision_context.py`,
`test_life_topics.py`, `test_life_read_plan.py` and the original migration, Goal, consumption,
owner-jargon and memory-review suites. The plan integration uses actual loopback HTTP,
independent native Life/Reader processes and mocked external model/Bilibili HTTP. It checks
that three serial reads yield one interest vote only after parent completion, then save the
original private diary/reflection or enqueue and acknowledge one authorized private share.
Restart must preserve the original evidence, usage and channel receipts without duplication.
These are synthetic acceptance checks; production reading quality and delivery remain a
separate live acceptance under the deployment coordinator.

## Authority and QZone draft handoff

Model generation with live or retained owner-authority custody requires the current
`OwnerAuthorityView.policy.life_shadow`: exact conversation, allowed source IDs and private
purposes, authorization hash, expiry and finite item cap. Native activity jobs pin the complete
owner authority reference in their admission transaction. Current permission and the original
job lease are checked independently; a revision never rebinds an old job. This is additional to
the original paid admission and grants no delivery. STOP, restore, changed sources and expired
approval fence commit and readback even if runtime settings are later switched to mock/OFF.

`life_shadow.qzone_draft_sources` supplies per-source `LifeQZoneSourceGrant`: grant identity and
revision, `purpose=qzone_draft`, event ID, current source fingerprint, expiry, `audience=owner_only`
and exact owner QQ ID. Every source in the model context must have a current matching grant before
QZONE_DRAFT is offered. A grant for a subset of sources does not permit a mixed private prompt to
produce a QZone artifact. No diary or private-message purpose implicitly authorizes QZone.

The output `LifeQZoneDraft` contains schema version 1, draft/job ID, purpose, owner-only audience,
owner ID, text, content hash, source snapshot hash, source grants and effective expiry. Hashes use
the project's canonical `paid_budget.sha`: content is `{"text": text}`; source snapshot is the map
`{event_id: source_sha256}`. The DTO lives in `companion/life_protocol.py`. It is an activity SHADOW
artifact; it must pass the separate native channel consumer before a SocialAutonomy READY plan
and Outbox reservation can exist.
Readback revalidates the DTO, native successful job and settled usage, retained text, both hashes,
exact current unique source grants, audience/owner and effective expiry. A malformed or changed
artifact is hidden individually without writing during GET or failing the whole view.

`LifeTopics.consume` accepts an exact retained draft and an explicit execution action:
`qq.private.proactive`, `diary.save` or `qzone.publish`. The request binds the original owner
authority and `sha({"text": retained_text})`; it cannot supply replacement prose or sources.
This is separate from generating or viewing a draft and never calls a second model to rewrite it.

For `qzone.publish`, all original model-context sources must pass the existing `public_source`
checks. Exact private-source draft grants permit owner review only: private chat, uploads and
fiction cannot become publication evidence, even with owner-only visibility. The current `social`
and `account_actions` purposes and the independently configured QZone action are required.
The consumer freezes the same text into the native SocialAutonomy plan and reserves its original
Outbox once in one transaction. Source roots, exact draft, current reference, STOP/OFF/DND and
native action quotas remain checked before dispatch. UNKNOWN retains the native reservation.
Visibility expansion beyond owner-only is outside this stage.

Private delivery additionally requires `proactive`, the original conversation revision and
native pause/DND/daily/cooldown limits. Life SHADOW/SKIP results reserve no send slot; the common
counter counts a Life-origin private Outbox group once at enqueue. Feedback affects these checks
only with the independent current `affect` purpose. Source-backed prior modules remain the
consumers; the selector does not create expression candidates from idle time or lack of reply.
While Life is PREVIEW/SHADOW, SocialAutonomy does not independently regenerate public-source
wording. Its separately scoped account-profile handling and retained native work remain native.

Shared HTTP/Outbox integration supplies `owner_delivery.record_life_draft` for the completed
activity job; the ordinary running-job `record_replies` contract is unchanged. Diary owns
`DiaryService.save_life_expression` and the daily evidence/revision lifecycle. It saves the retained
wording once, preserves owner edits and deleted days, and does not pay for another generation.
REM remains a separate private FICTION artifact; it cannot become factual Diary or public-source
publication evidence.

## Independently authorized automatic consumption

The owner-approved `life_shadow.automatic_actions` defaults to `[]`. Existing policies keep their
draft-only automatic behavior; the control-token consume API remains an explicit per-item action.
The empty list is omitted during serialization so historical approval fingerprints stay unchanged.
The optional list accepts only `qq.private.proactive`, `diary.save` and `qzone.publish`, without
duplicates and only for corresponding allowed draft destinations. It does not replace the
independent channel purposes, account policy, source grants or budgets described above.
Adding automatic consent creates a new owner-policy revision: earlier drafts are never rebound
to that revision. The policy must authorize the original job when the draft is generated.

The existing Life selector calls `LifeTopics.advance`. In one bounded non-model turn it either
consumes one eligible settled expression or schedules the next native decision. Consumption
scans at most the approved scope's finite item cap, skips existing native Outbox/Social/Diary receipts,
and invokes the same `consume` implementation. A refused candidate does not hide another eligible
candidate. No new queue, model planner, timer or delivery transport is introduced.

OFF/PREVIEW, owner pause, quiet hours, current or approved pre-ingress owner input, unresolved
model/delivery outcomes, source expiry and channel quotas retain their original boundaries.
Private and Life-origin QZone final send checks reuse `owner_authority.owner_waiting`, so a current
signed capture can stop dispatch before its event is ingested. A queued item is not a successful
send; native receipts determine success, rejection and delivery uncertainty. Restart does not
recreate a consumed item or repeat an uncertain send.

## Recovery and erasure

Life decision material includes bounded views of explicit owner feedback, completed self-reading
interests, and native Goal outcomes. It reads only the activity's approved source set. Affect and
memory retain their independent purpose switches. A completed plan contributes one interest
result; execution success is not a positive owner rating, and silence is not negative feedback.
One terminal reading failure may reopen the original topic for one further decision within its
existing decision, cooldown and source limits. An uncertain read cannot trigger continuation.

The original activity event pins the complete material under its existing material hash. Retained
wording revalidates the evidence view before consumption, including feedback reset. Actual Life
reading results carry their original source roots into private/public destination checks and
erasure dependencies. No separate feedback database or action queue is introduced.

Public Social decisions can use up to three original action receipts for exactly matching public
sources and the same owner revision. Only outcome, public source references and native error/code
reach the prompt. A known rejection can inform a later bounded choice; an unresolved native send
blocks new Social model planning. Neither state is interpreted as a person's opinion.

This reuses DSH's [explicit message-feedback evidence](https://github.com/deepseek-ai/deepseek-harness/tree/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/feedback/message-feedback)
principle and [LangGraph's raw-state/derived-view separation](https://docs.langchain.com/oss/javascript/langgraph/thinking-in-langgraph).
Core's original event, authority, Goal, receipt and erasure mechanisms remain authoritative.

Migration `0037_life_topics` adds only topics, topic source links and activity results keyed by
original job ID. Jobs own leases, attempts and unique execution identity. Model usage and the
original independent journal own all actual costs. Failure, UNKNOWN or a lost response never
automatically pays for the same decision again. Budget refusal before admission can use the
existing bounded notice retry path; source expiry still stops it.

Source fingerprints and conversation input identity pin a decision. Readback suppresses a draft
when its source changes, expires or its purpose grant is revoked. The registered derived cleanup
erases result prose, rationale and JSON and cancels pending work; latest-ledger restore applies
the same erasure to old backups. No cleanup refunds an actual model call or resets UNKNOWN.
Owner input supersession and pause cancel only the stale Life job through the existing reply
lifecycle; they do not stop the live conversation window. Actual UNKNOWN and authority failures
retain the original global accounting/authorization guards.

## Validation boundaries

Related tests cover the source/lifecycle failure matrix, original notices and goals, and existing
social autonomy. The native-worker cases use real loopback HTTP input and Reader v3 routes, an
independent Core/store in the original Life worker, native paid admission/settlement and
`httpx.MockTransport`. Foreground reply jobs are explicitly cancelled to isolate background work;
retained synthetic sources are explicitly admitted to the subsequent SHADOW window. They are not
production capture or live QQ acceptance. No external model or QQ call is made.

`test_life_expression_entrypoint.py` also exercises actual consumption HTTP, independent native
selector processes, all three channels, and automatic worker consumption without a consume POST.
It covers unchanged model accounting, repeated calls/restarts, owner edits/deletion, STOP/source
withdrawal and native SENT/UNKNOWN receipts. `test_life_expression_pre_ingress.py` approves a real
synthetic signed capture between begin-send and the final native channel check. These establish
the execution mechanism, not real model quality or actual QQ delivery.

The worker cases verify next-day Bug follow-up, reading-result SHARE, explicitly scoped QZone
draft and in-flight grant revocation. Unit/integration checks also cover owner closure, source
withdrawal/expiry, pause/OFF, new input, restart, duplicate claiming, budget denial, UNKNOWN,
bounded DEFER, purpose isolation and erasure replay. Exact candidate checks and eventual CI are
recorded once in the lane receipt. Finite live Beta still needs explicit dates, source/purpose
scope, request/cost/item caps, destination/audience and stop/reconciliation responsibility.

The execution follow-up additionally verifies owner HTTP input → independent Life/model worker →
native Goal → independent Reader v3 with mocked source transport → actual receipt → independent
Life SHARE, including same-approval private-purpose checks and zero outbox items. Model output
is synthetic; this does not establish live model decision quality.

`test_life_read_plan.py` exercises independent native Core and Reader processes for ordered
three-step completion, reordering, finite wait, abandonment and restart. The companion fence
tests verify unknown outcomes, source erasure, invented targets, stale versions, missing result
evidence and invalid step lists through the original model admission and commit paths.
