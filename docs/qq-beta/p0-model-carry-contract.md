# Retained UNKNOWN model liability handoff

Core owns `companion_delivery_protocol/model_carry.py`, the Core API, configuration,
authority, original budget journal, cumulative budget and offline controller. Bridge
owns `owner_handoff.py`, its CLI and journal reader. No other Bridge files are owned
by the Core slice. Base is main `90bc5b3`; factory is parked separately.

The frozen read endpoint is `POST /internal/v1/owner-authority/model-carry-receipt`,
using the existing Bridge Bearer credential and `Cache-Control: no-store`.
Its exact request is `{authority, carry_sha256, successor_policy_sha256}`. Authority
is the stopped ACTIVE AuthorityRef v2, not its STOP revision. The two digests must
match the original journal-backed prepared carry. It is readable only at the exact
STOP successor (r+1) or the exact ACTIVE successor (r+2, matching policy), same grant
and restore generation. The immutable response is validated by `model_carry.receipt`.
All fields and bounds are defined in that shared dependency-free module.

The first migration returns `owner_model_unknown_carry_v1`, version 1, for the
original legacy authority. Later STOP/ACTIVE cycles reuse the exact successor
policy, paid approval, window, interval and full predecessor budget scope. Core
checks the uninterrupted native revision chain under that same grant and restore
generation. Every UNKNOWN must still have verified closure in the original journal;
an outstanding transport, pending send, changed approval, restore or missing
projection blocks activation. This continuation does not append another carry or
create a financial window. A cycle with only settled current inputs uses ordinary
Bridge `owner-handoff-prepare`; its historical carried facts remain in custody.

When the current authority has a locally closed UNKNOWN, the same endpoint returns
`owner_model_unknown_carry_v2`, version 2. It has all v1 field names plus
`origin_authority` and `local_closures`. `authority` identifies this cycle's stopped
ACTIVE ref and `core_stop_sha256` its immediate native STOP. `origin_authority`,
`carry_sha256`, `record_sha256`, `successor_policy_sha256` and `successor_window_id`
retain the original approved migration anchor. `inputs` includes every current
source linked by native job responsibility to the selected UNKNOWN requests,
including history sources. `request_ids` and `retained_micro_usd` cover those current
requests; historical migration liability remains separately retained.
`local_closures` is a sorted list of exact `{request_id, record_sha256}` pairs, one
per selected request. Each hash must identify that request's existing validated
`model_local_close` record under the current authority. In this explicit version,
`closed_executors_sha256` hashes `local_closures`; it is not a Docker exit claim.
There is no new journal control kind, remote attestation or financial record.

The v2 response is derived read-only from original custody at STOP r+1 or its exact
same-policy ACTIVE r+2. Bridge uses its existing carry prepare and handoff commands,
requires the same authenticated response before and after activation, and retains
the v2 proof inside the existing version-1 Bridge record wrappers. Shared validation
dispatches by the explicit kind/version. Original input-state booleans and prior
stored v1 receipts retain their existing meanings. In particular, local closure
never makes UNKNOWN settled or permits replay of its original logical request.

Hashes use existing wire `canonical`/`digest` (sorted compact UTF-8 JSON, SHA256).
`carry_sha256` hashes the Core-computed carry manifest; `record_sha256` is the
original budget journal record hash; `receipt_sha256` hashes the response excluding
itself. There is no new signature domain: authentication is the direct Core lookup,
as for existing input-state receipts. Existing signatures and STOP receipts retain
their exact meaning. No offline caller-supplied receipt substitutes for the lookup.

Bridge prepare requires its original exclusive stopped Spool, original Core STOP
confirmation (`quiescence_proven=false`), and a direct carry lookup. Ordinary inputs
continue through existing `terminal()`. Only listed nonterminal model inputs may
use `model_carry.carried(original_state,capture,receipt)`; retain the complete
original false terminal/true unsettled_model values in `OWNER_INPUT_CARRIED` data:
`{version:1, authority, event_id, input_state, carry_receipt}`. The new preparation
record kind is `OWNER_CARRY_HANDOFF_PREPARED`, with exact data
`{version:1, authority, core_stop, custody_sha256, carry_receipt}`. Ordinary
`OWNER_HANDOFF_PREPARED` remains unchanged. Include carried records and the carry
receipt in the existing custody digest. Re-fetch the same carry receipt after
building custody, and require exact equality with the first lookup before append.
The new CLI returns state `CARRY_HANDOFF_PREPARED`.
`verify/closed` validate stored carried evidence; preserve all original input refs,
source capture bytes, attempts, provider ACKs and reconciliation requirements.

Existing OWNER_REBOUND shape remains unchanged. For carry preparation its
successor must additionally have `policy_sha256 == receipt.successor_policy_sha256`.
The original same grant, revision r+2, generation, journal-ID, active Core check and
stop-marker rules continue to apply. No original tool state, input or model request
is retried. The original pending inbox may close locally after the explicit carried
record is durable, as ordinary prepare closes terminal inbox entries; no ACK is made.

Core errors are `OWNER_MODEL_CARRY_REQUIRED` (403),
`OWNER_MODEL_CARRY_CHANGED` (409), `OWNER_MODEL_CARRY_NOT_QUIESCENT` (409), or existing
budget custody/restore errors (503). Shape failures remain HTTP 422. Shared wire
validation raises `OWNER_MODEL_CARRY_RECEIPT_INVALID` or
`OWNER_MODEL_CARRY_INPUT_CHANGED`. Bridge must retain STOP on every failure.

Policy uses PaidModelApproval.concurrency_basis=`local_active_attempts_v2` and
OwnerGrantPolicy.unknown_carry_sha256. Optional fields are omitted for old policies.
The reviewed target has neither cumulative approval nor its runtime projection.
This urgent slice explicitly rejects either one with
`MODEL_CARRY_CUMULATIVE_UNSUPPORTED`; it does not change cumulative v1 behavior.
Cumulative v2 is deferred. Its later review must cover both overwrite directions
of OWNER_PAUSE/DELIVERY_UNCERTAIN versus UNKNOWN, and closure after later timeouts;
checking only the final displayed pause reason is insufficient.
Historical UNKNOWN and STARTED remain present. New window counters include the
predecessor's full budget scope; cumulative financial identities are never rewritten.

Capability readiness uses the same verified `model_closures` and predecessor window
scope as native model admission. A carried or locally closed UNKNOWN does not occupy
an execution slot or prevent a new authorized Reader request. Its original logical
request still cannot retry, and its amount remains in all applicable totals. Missing
or invalid journal proof, an unclosed UNKNOWN, STOP, expired source grants and exhausted
budgets still block readiness. This projection performs no settlement or I/O.

Integration order is original STOP -> real API/all-model-worker exits -> controlled
offline carry append -> new API only -> authenticated Bridge carry prepare -> exact
authority preview/activate -> Bridge handoff -> workers/new independent signed input
and real ACK, external transports mocked. Core provides the full prepared receipt
from the approved offline request, including the target policy hash. The deployment
controller verifies actual inspect/wait evidence before executing the offline command;
the product neither mounts docker.sock nor pretends the supplier stopped execution.

The original append-only budget journal retains only the successor policy and paid
contract hashes, not the private policy JSON. Activation checks those exact hashes.
It must not defeat native policy redaction or source erasure. `retained_micro_usd`
is the frozen original liability floor; later larger known responsibility remains
accounted by the original ledger. New v2 admissions reject any logical request
which already has UNKNOWN, including after local closure. A new independently
authenticated input has a different native job/logical identity.

## Controlled operator execution

Use the reviewed Python environment on the trusted deployment host, with the same
absolute DB, journal and erasure paths as the frozen custody. This is not an API or
worker container permission. If that environment or path binding is unavailable,
stop at preflight; do not mount docker.sock into the product or rewrite custody paths.

First collect the reviewed deployment inventory before STOP, retaining the full
container ID, image ID, source artifact SHA256, service, original process ID and
StartedAt. After the original Core and Bridge STOP, the deployment operator disables
restart for those exact retired containers and stops/waits for all of them. The
controller independently performs local `inspect`, full project `ps -a --no-trunc`,
`wait` and a second `inspect`. It refuses a missing API/worker, omitted executor,
running/restarting process, changed image/start identity or enabled restart policy.
Existing OS worker locks are held while inspecting and committing native custody.
The private request's `executors` entries contain exactly `container_id`, `image_id`,
`source_sha256`, `service`, `started_at`, `finished_at`, `process_id`.

The exact private request fields are `version=1`, `operation_id`,
`authorization_sha256`, `expected_head_sha256`, original `authority`,
`successor_window_id`, `manifest_sha256`, `successor_policy`, `executors`.
The new paid configuration is loaded separately through existing Settings. Both the
paid approval and owner authorization evidence must be new; all other limits,
prices, keys, permissions and original approval intervals remain unchanged.

```sh
python -m companion preview-model-carry --request PRIVATE_REQUEST --request-sha256 REVIEWED_SHA256
python -m companion carry-closed-model-attempts --request PRIVATE_REQUEST --request-sha256 REVIEWED_SHA256
```

Preview computes the exact manifest/digest for review and does not append a carry.
Fill that digest and the exact successor policy, then review the complete request
bytes before commit. The commit re-observes the real stopped executors and recomputes
the whole manifest; a caller cannot submit a list that omits an UNKNOWN or an amount.
Only the identical approved request can complete an interrupted journal/projection
commit; normal readers remain blocked. It fsyncs the existing record again before
restoring that exact projection and never replays a provider attempt.

These native Docker operations follow the primary CLI contracts:
[inspect](https://docs.docker.com/reference/cli/docker/container/inspect/),
[wait](https://docs.docker.com/reference/cli/docker/container/wait/) and
[container listing/filtering](https://docs.docker.com/reference/cli/docker/container/ls/).

## Approval renewal

An already-carried historical UNKNOWN can enter a new explicitly approved period
through the same offline preview/commit commands. The private request uses
`version=2` and adds `previous_paid_contract`: the exact previous paid snapshot,
whose hash must match the stopped authority. Existing executor inspection, five
worker locks, exact request/head binding and interrupted fsync recovery still apply.
This reuses the append-only approval/predecessor pattern from
[cumulative renewal](p13-cumulative-limits.md); it does not enroll a cumulative
account or remove the existing cumulative/carry compatibility guard.

Renewal may run before or after the previous approval expires, so a renewal does not
leave a gap without model access. Liabilities stay fenced by the native STOP, the
verified stopped executors, quiescence and the exact UNKNOWN inventory; none of these
depends on the clock. A new window ID, paid approval and owner authorization are
required. The new approval is either open-ended (no `expires_at` and no
`rates_valid_until`) or has a finite expiry that is in the future and later than any
previous finite expiry, with a rate-validity end at or after it that does not move
earlier. Prices, starts-at, credentials, provider/model, per-attempt/request limits,
daily/monthly/window amounts and call limits remain identical. The per-call
`max_output_tokens` ceiling may stay or rise, never fall: every money bound is unchanged,
so a larger generation allowance cannot raise any liability ceiling (2026-10-30, for chat
thinking, which shares `max_tokens`). The owner expiry must
equal the new paid expiry, including both being absent. Every predecessor window,
including settled calls in the immediately preceding window, remains in the budget scope.

An open-ended approval is still bounded by its daily, monthly and window amounts and
call limits, the per-attempt and per-request limits, owner STOP and revocation. Window
amounts and call limits accumulate across the whole renewal chain and are not raised by
renewal. An open-ended approval cannot sit inside a cumulative account scope.

The original journal receives `model_approval_renew`, version 1. Its manifest binds
the preceding carry, current native STOP, exact historical UNKNOWN inventory,
full window scope, financial baseline and newly verified closed executors. It
changes no original admission, charge, closure, tool or input. Current inputs must
be terminal or have the existing source-bound local-close proof for every remaining
UNKNOWN. The latter uses the existing local carry validation: original admission,
settlement, authority, source-work hash and completed transport must match, with no
outstanding work or uncertain delivery. The new approval explicitly retains these
additional charges alongside every predecessor charge. Missing closure still blocks
renewal. Original carried inputs retain their original Core and Bridge proofs.

The commit's `owner_model_approval_renewal_v1` response is an offline preparation
summary, not a Bridge input receipt. Its retained amount covers the full historical
liability. When this renewal includes locally closed current inputs, the authenticated
input-carry endpoint returns the existing `owner_model_unknown_carry_v1` wire proof
for those inputs and their charges only. The offline executor witness, current STOP,
new approval and full renewal manifest bind that proof. Bridge uses the existing
`owner-carry-handoff-prepare`, exact preview/activate and `owner-handoff` commands;
it records carried inputs without changing their nonterminal financial state or
replaying them. If every current input is terminal, ordinary `owner-handoff-prepare`
continues to apply. Later same-policy STOP cycles retain the existing local-close
continuation rules, including when the predecessor is itself a renewal. Older journal
readers must remain stopped after the renewal append; forward repair preserves the
whole journal.

A retained request can refer to several historical inputs. Each input keeps its own
native source-work proof, while the request and its charge occur only once in the
carry receipt. A Life source with its own settled responsibility may still be cited
by a later UNKNOWN chat. Renewal accepts that history only when the freshly verified
manifest explicitly retains the later, closed request and the source has its own
authority-bound primary chat. The source's own UNKNOWN, missing primary custody,
unfinished work and uncertain delivery still block approval. This does not change
the original input's financial state or authorize replaying it.

The only optional permission addition in this renewal is bounded private Life:
the old policy must already grant proactive messages and private history. A fresh
`life_shadow` authorization may select private owner messages in that same retained
history, conversation, grant and restore generation. It allows only
`PRIVATE_MESSAGE_DRAFT` and `qq.private.proactive`, excludes sources with uncertain
model responsibility, and cannot add goals, public posting or other actions. Later
renewals keep the exact Life source set and limits. Source expiry and erasure still
apply independently of the new authorization expiry.

Life requires both journal-proven execution closure and explicit retention under
the current approval to distinguish an old financial UNKNOWN from an outstanding
execution. A new UNKNOWN, missing closure or journal/projection mismatch still pauses
Life. Uncertain delivery always pauses it.
The UNKNOWN charge remains included in all applicable budgets, and its old logical
request cannot be retried. Daily limits, cooldown, owner-idle and quiet-hour checks
remain in their existing scheduling and delivery paths.

## Independent evaluation permission revision

After the current authority is stopped and all its inputs are terminal, the owner
may use the existing authority preview/activate endpoints to add `evaluation` with
a new authorization digest. Only that purpose and the authorization digest change.
The paid contract, window, expiry, concurrency, carry reference, source scopes and
all other policy fields remain identical. This operation uses the existing
`authority_revision` v2 journal record; it neither renews the approval nor changes
any original UNKNOWN, charge, closure, carry record or source reference.

Preview and activation both verify current stopped custody. Unclosed work,
uncertain delivery, a current-authority UNKNOWN, budget blocks, pending accounting,
restore barriers and missing current policy evidence reject the revision. Historical
UNKNOWNs already retained in the original carry remain financial responsibility.
Bridge uses ordinary `owner-handoff-prepare`, then owner activation and
`owner-handoff`; historical carried inputs retain their existing receipts.

Later same-policy resumes and normal paid renewals validate the continuous
STOP/ACTIVE authority chain. If a new UNKNOWN occurs under the revised authority,
its existing v2 LOCAL receipt names that actual authority and policy; it does not
rewrite the original v1 carry proof. A directly following renewal may retain that
current-authority responsibility through the existing manifest. The existing
restriction on renewing fresh responsibility belonging to an earlier, subsequently
resumed authority remains: it fails closed rather than relabeling the old input.

Source erasure keeps only the original financial hashes and chain evidence. It
does not restore erased policy bodies; a new activation requiring a missing current
policy is refused. This permission alone does not enable execution: the deployed
`evaluation_enabled` setting, each exact plan's preview and consent, original
worker leases and all native per-call and aggregate budget limits still apply.
The current evaluation settlement guard retains its 5000 microUSD per-call and
50000 microUSD per-job limits, including when a synthetic preview admits a larger
plan. An owner's higher spending ceiling does not raise those runtime limits.

## Candidate image and migration boundary

The feature was authored on main `90bc5b3`; final integration normally inherits
main `6107b2d`, including its separately reviewed bounded animation preview. Relative
to the running c8 image, main also merged optional routed context limits and native
request planning/checkpoints; read-only context/activity inspection; Core-owned meme
asset/runtime/erasure support; backend cache-usage presence; the private temporary
backup staging fix; native image-transfer source-check repairs; and HTTP test tooling.
The image contains the previously merged console inspection code; this P0 adds no UI.

Schema `0047_meme_assets` is part of that base and must follow the original reviewed
init/migration procedure with a preserved latest backup. Its empty tables do not
authorize an asset import or ownership transfer. Keep `meme_catalog_owner=bridge`,
`meme_trusted_sources=[]`, and do not enable discovery/captioning or add paid meme
input approval as part of P0. Preserve the original disabled/absent Life shadow and
all existing owner permission scopes. Do not add `max_context_tokens`, routing or
token-counting approval; the optional new fields remain omitted at their defaults.
Native planning and the inherited source-check fixes are active code changes and
must not be represented as disabled by an invented feature flag. Their already
verified main evidence and the final combined native check accompany release review.

After a new control record is appended, old c8 is not a compatible journal reader.
Do not roll back to an old database or journal prefix: new responsibilities and the
84 microUSD UNKNOWN must remain. Stop/OFF on the new reader and forward repair are
the conservative rollback. Returning to old bytes requires proving that no new
responsibility or control record has crossed that boundary. Source retention and
original image expiry are never extended by this operation.

## Adding private REM to an existing carried approval

The existing native owner preview/activate path can add one `rem` purpose under the
same paid approval after STOP and quiescence. It requires an already approved
`memory` purpose with `memory_scope=owner_private`, a new authorization digest and
exactly one added `capability_policies.rem` fingerprint. Native owner policy
validation checks that fingerprint against the enabled REM configuration. All other
policy fields, existing purposes in order, capability fingerprints, source grants,
paid/window identity, limits, carry digest and expiry remain byte-equivalent.
The existing evaluation-only addition remains supported by the same finite policy
revision validator; no general capability-extension rule or new journal kind is added.

Historical authority-chain validation recognizes this same explicit REM transition,
so later STOP/activation and paid approval renewals can retain the granted purpose.
Renewing a paid window still uses the original renewal checks; this change does not
combine a new REM grant with a paid-contract change.

Current-authority nonterminal inputs and UNKNOWN requests continue to block a policy
change, including locally closed UNKNOWN rows. First complete the existing same-policy
handoff, preserving the original liability; only a quiescent successor can receive the
new purpose. No usage is settled, refunded, rebound or replayed by purpose revision.
The source-erasure, original Ledger and credential/paid-contract checks are unchanged.

This reuses the recorded native STOP/ACTIVE, exact-scope and journal research above,
including the original Docker inspection contracts and existing SQLite transaction
boundaries. The unchanged producer/terminal responsibility split follows the pinned
DSH QQ research recorded in [owner authority](../owner-authority.md); no DSH transport
or producer protocol changes. `tests/test_model_carry_rem.py` verifies narrow scope rejection, retained
UNKNOWN accounting, later paid renewal, and real owner HTTP preview/activation followed
by an independent REM worker with mocked provider transport.

## Closing an attempt that never started I/O

A refusal after admission but before `io_started` (for example a background yield,
fixed in #212) used to settle the attempt `UNKNOWN` with `io_started_at` null. No local
closure follows, so the attempt keeps the single `local_active_attempts_v2` execution
slot (every later call fails `MODEL_PROVIDER_CONCURRENCY_EXHAUSTED`) and every resume
fails `OWNER_MODEL_CARRY_NOT_QUIESCENT`.

```sh
python -m companion close-unstarted-model-attempt --request-id REQUEST_ID
python -m companion close-unstarted-model-attempt --request-id REQUEST_ID --commit
```

The command runs offline with the five worker locks, requires the latest authority to
be `STOPPED`, and accepts only an `UNKNOWN` whose admission and settle record both lack
`io_started_at` and whose original journal has no `io_started` record for it. Without
`--commit` it only prints the record. With it, it appends the existing
`model_local_close` kind. The outcome stays `UNKNOWN` at its retained amount in every
total, and the logical request still cannot retry. Older journal readers reject this
closure, so run it only with the old Core stopped and start the new image afterwards.
