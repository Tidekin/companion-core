# Native Voice owner authority migration

## Local singing

Optional `voice.singing` adds `sing` to explicitly registered nodes. The SDK uses
the existing Reader's `discover_content(source="bilibili", query=...)` or the
owner's shared-link catalog, then `sing_song(source_event_id, part)`. Only actual
source events already shared with the original chat job are accepted. A settled
SDK tool call binds `agent_tool_calls.voice_job_id` to the native `voice_jobs`
queue; the original Reader `request_id` foreign key remains intact. The SDK waits
without another model request, then a `singing` item in `reply_with_parts` selects its READY result.
The [ordered mixed-reply contract](mixed-replies.md) also allows independent text and pictures
before or after those complete audio parts.
This enables model choice during a private conversation; it adds no idle scanner.

Singing reserves four requests and bounded source/metadata bytes against the
existing Reader quota, plus 56 MiB of private result capacity. Cancelled work that
never released input gives back the source reservation. Started UNKNOWN work
keeps it; a successful receipt records actual requests and bytes. A second tool
call cannot replay the original owner input's singing attempt.

The credential-bearing parent rechecks the live Core lease before each public
request and received chunk. It downloads at most 32 MiB from one API-declared
HTTPS 443 audio locator, without copying Reader cookies. Its supervised child
receives local file paths, a frozen source recipe and fixed Applio resources.
Offline AAC decoding, UVR5 separation and Applio conversion preserve the complete
source timeline. Windows singing children use the existing Job Object with an
16 GiB commit limit; all platforms retain the parent deadline and process cleanup.
Child application/config/cache directories live under the owned temporary path.

The full result is bounded to 56 MiB of JSON, accepted only for an authenticated
node whose registered capabilities include `sing`. Source policy can tighten the
600-second/32-MiB limits. The manifest commits to actual source hashes and a
contiguous PCM16 mono 32-kHz partition, with at most 24 parts, each <=55 seconds
and 4,000,000 bytes. Each modern outgoing binding includes its exact segment
index and the compact whole-song manifest; the independent Bridge verifies the
selected WAV before the existing final send-check. Native ordered Outbox groups
send the next part only after the previous ACK. An uncertain send stops the tail.
Full and retired singing proofs remain valid for historical UNKNOWN resolution;
they never authorize a new delivery.

Configure the operator-owned node profile's `singing` object using
`ApplioRenderConfig`: explicit absolute model/index/source paths, all ten exact
resource hashes and, when needed, a local installed `dependency_overlay`.
`scripts/configure-voice-node.py` retains accepted speech settings and chooses a
600-second execution budget, 30-second HTTP timeout and 300-second result cache
for such profiles. It never downloads missing models. A reverse proxy must allow
the bounded 56-MiB result route; Core still applies the registered-node limit.
New manifests/configuration require the original native STOP, cleanup, authority
revision and Bridge handoff. READY heartbeat and actual QQ acceptance are separate
from offline conversion. The [implementation scope and pinned sources](design-future/applio-singing.md)
record the approved voice, native Applio API and verification boundaries.

This candidate replaces live-window execution checks in `VoiceFlow` and
`VoiceJobs` with the existing OwnerAuthority, signed owner-input and native
Outbox checks. Native Core/API and independent voice-worker acceptance use
synthetic providers. Production activation still requires the separately owned
Bridge consumer integration and its final combined verification.

## Binding and deadlines

New voice jobs retain the exact original `AuthorityRef` in `authority_json`.
Current policy must still approve `voice` and its exact existing VoiceConfig
capability hash. ASR checks the original signed input and current reply revision;
TTS checks the original owner delivery proof, settled source task and exact text.
No window or replacement execution grant is created.

Job TTL, ASR wait and TTS wait are capped by the original grant's expiry when
present. Existing node leases and result retention remain bounded by the native
job deadline. STOP, revision changes, erased sources, configuration drift and
expired grants prevent further input/result use. Chat-only grants do not enable
optional ASR; the authenticated chat can still retain its unsupported record tag.

The local voice-node execution lease remains protocol v2. ASR capture proofs use
the v2 voice-upload signing domain and the original owner reference instead of a
window hash. Outgoing delivery bindings use schema v3 with that reference.
Historical schema-v2 delivery bindings are accepted only by the retirement codec;
they cannot construct a new send envelope. Retired capability verification,
UNKNOWN outcomes, cleanup acknowledgment and node quarantine remain independent
of active execution permission.

## Required shared integration

R1 supplied migration `0044_voice_owner_authority` after `0043_reader_authority`
and the Store schema-capability declaration in `7cc2a48`. The registered contract is
`voice-owner-authority-interface.json` in the shared next-features handoff folder.
The additive column is:

```sql
authority_json TEXT NOT NULL DEFAULT '{}'
  CHECK(json_valid(authority_json) AND json_type(authority_json)='object')
```

Historical rows are not backfilled. The existing nullable `window_sha256` is NULL
for new owner jobs; its historical values are retained until native retirement.
Dropping nonempty authority custody must require explicit reconciliation.

The current Core APIs import the Voice ASR proof DTO directly. Their external
Bridge caller must submit the new proof, compare the exact authority in input
state, and validate schema-v3 voice delivery bindings against its original owner
reference. The native Bridge uses OwnerRuntime directly. Capture selects voice
only when its current policy includes the capability, and checks the voice
purpose before queuing original record descriptors in the existing media journal.
ASR state queries and uploads use the checked private HTTP boundary; decoding
reuses the bounded child process and removes private temporary files before the
final source check. A lost upload ACK recovers only the matching original job.

TTS preparation checks the current voice-policy hash from authenticated authority
metadata, original authority, schema, result expiry, canonical text hash and WAV
bytes. It runs before the delivery journal records the original voice send-check.
The existing sender transmits only the prepared record through its final Core
send-check; an uncertain result cannot become a replacement text attempt.

## Verification so far

`voice_original_proof.validate_original_voice_binding` is a pure historical
validator for UNKNOWN reconciliation. Its keyword arguments are `receipt`, `job`,
`proof`, `outbox`, `content_sha256`, `original_scope`, and `voice_policy_sha256`.
The caller loads the `voice_reply:<message_id>` receipt and the voice job named
by `proof.voice_check.job_id`. Original scope is exactly `{"authority": ref}`
for schema v3 or `{"window_sha256": digest}` for historical schema v2.
The policy hash must come from authenticated original policy evidence.

The caller remains responsible for original grant/policy and accounting custody,
proof signature, delivery attempt, lease, send-check digest, and quiescence.
The validator has no storage, settings, clock, or current-authority access and
does not permit execution. Full bindings require retained content, matching job
material and original result expiry; expiry equality is checked without a
current-time test. Retired custody checks its original identity and capability
verifier (or historical signature verifier) without requiring erased job
authority or payload. Neither path reconstructs a new grant.

Twenty pure evidence tests cover both schema versions, full and retired custody,
expired records, original scope/policy/job/token substitution, missing voice
proof, content/byte/result/expiry changes and historical signature retirement.
The native Bridge reconciliation entry retains this original voice check through
STOP and owner resolution, including the existing explicit successor handoff.

Four native-source checks pass through real signed owner capture and Core ingest:
window-free ASR waiting, original-expiry bound, STOP rejection and chat-only
permission separation. No guard replacement or real voice node is used.

ASR/TTS creation and real TLS API plus independent voice-worker checks pass.
The complete native chain checks ASR evidence, settled SDK wording, large TTS
payloads, unchanged original source/Outbox content, native send-check and ACK.
Original job/flow/retention/retry/maintenance fixtures use signed owner inputs and
real SDK admission/settlement with a synthetic model provider. ASR-only cases
retain a pending source job instead of inventing an already-approved reply.

STOP, replacement attempts, expiry, source withdrawal and restore retain
UNKNOWN and random cleanup custody without replay. Cleanup after restore does
not restore authority to claim new work. Historical v1 migration fixtures freeze
the pre-measurement reservation field set before hashing; they never discard an
existing measurement journal proof. Migration 0044 preserves existing job,
lease, node, Outbox and accounting rows and adds an empty authority reference.
Native UNKNOWN reconciliation reuses the original proof validator via R1's
`e9df94b`, including erasure-retired voice bindings and unchanged usage.

Bridge acceptance additionally covers authenticated WebSocket capture, an
independent SDK worker, two independent voice-node processes over real TLS Core
HTTP, large TTS envelopes and actual checked mock-OneBot acknowledgements.
The failure cases include source replacement, pending owner input, changed
request bodies/paths/streams, lost replies, STOP and original UNKNOWN custody.
Final service activation remains a separate integration step. No real provider,
QQ sender or device was activated by these checks.

## Reuse

The resident fully local GPT-SoVITS/Applio setup and manifest migration are documented
in [Local daily voice replies](local-speech-runtime.md).

- Existing `owner_authority.current/evidence`, `owner_input.source`,
  `owner_delivery.check_delivery` and native voice lease/retirement code own the
  permission, source and cleanup semantics.
- Inspected DSH schedule runtime and delivery-history source at
  `22ddee3792402c3e70ec5f86a63c3e3e895a2f60`: reuse serialized permission-sensitive
  operations and durable receipt identities, without treating a stop request as
  confirmed completion.
- [RFC 7662](https://www.rfc-editor.org/rfc/rfc7662.html), sections 2 and 4:
  current active scope/expiry is checked at the protected resource. This migration
  uses the existing Core authority service; it does not introduce OAuth or a
  second authorization system.
