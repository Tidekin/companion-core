# Account source grants

The owner confirmed these rules on 2026-10-20: natural login expiry and revoking future
reads retain previously authorized history. Explicit withdrawal of historical processing
erases evidence attributable to the selected grant and its derived closure. Source grants
are separate from device consent and from QQ login or message-delivery permission.

## Data and authority

Core stores an owner-issued `grant_id`, immutable revisions (`epoch`, `fingerprint`,
`expires_at`), adapter/source binding, revocation timestamps, and event attribution.
`fingerprint` is an independently generated opaque revision identifier, not a cookie hash.
Use a fresh random `grant_<32 lowercase hex>` ID, a positive integer epoch and 64 random
lowercase hex characters for a revision fingerprint. The owner chooses an expiry timestamp or, for a registered Bilibili account,
explicit `"expires_at": null` to allow reads until revoked. Both the registration and the
private Reader file require this field. Heybox and QZone require a timestamp.
To renew an expired Bilibili grant, use the same grant ID with a higher epoch and a fresh
fingerprint in both places. Each read still has a finite lease and requires a valid login.
Increasing the epoch replaces the revision used for new reads; previous revisions remain
available to explain historical provenance.

Cookies, SESSDATA, account/device IDs and the existing whole-file integrity digest remain
inside isolated Reader storage. They are never registered with Core, copied into evidence,
sent to a model, or included in API responses. The same private account file retains its
existing `enabled`, `expires_at`, `cookies`, pinned `upstream_commit` and source-specific
fields; merge this non-credential object into that file:

```json
{
  "authorization": {
    "grant_id": "grant_11111111111111111111111111111111",
    "epoch": 1,
    "fingerprint": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

These values are fixtures. Generate independent values for a real authorization. The outer
`expires_at` is the revision expiry and must match the owner registration. Preserve the
existing private-file ownership/0600 requirements. A legacy file can still support an
explicit isolated read-only probe, but cannot start a Core account-reader lease without
the new metadata. Public anonymous readers retain their existing lease/result contract.

## Owner and Reader operations

Keep all Reader/autonomous capabilities disabled while preparing or reviewing grants.
Only the existing owner control Bearer credential can register, inspect, revoke or forget
grants. Reader and QQ Bridge credentials cannot perform these operations.

| Method/path | Body or result |
|---|---|
| `POST /internal/v2/source-grants` | `adapter_id`, `grant_id`, `epoch`, `fingerprint`, `expires_at`; rejects credentials/extra fields |
| `GET /internal/v2/source-grants` | Latest revision and lifecycle metadata for the most recent 100 grants; no account credentials |
| `POST /internal/v2/source-grants/{grant_id}/revoke` | Stops future reads and cancels outstanding leases; retains stored history |
| `POST /internal/v2/source-grants/{grant_id}/forget` | Records explicit processing withdrawal in the independent ErasureLedger and erases the related closure |
| `GET /internal/v2/source-grants/legacy?offset=0` | Owner-only review inventory, 100 records/page, no bodies and no inferred grant attribution |

Register the metadata for the configured adapter before starting its account reader. Set
`source_grant_required=true` on that account adapter. The Reader sends the same metadata
when leasing work; Core validates the owner registry and captures the exact revision.
The Reader verifies the returned binding before any public I/O. Before every account GET,
it calls the adapter-authenticated
`POST /internal/v2/readers/{adapter_id}/{request_id}/validate` with the captured
`lease_token` and `source_grant`. Core rechecks the running lease, its deadline, privacy
epoch, exact captured revision and current grant authority. Failure or uncertainty stops
that GET. The Reader also checks the local revision around this request and rechecks the
whole private file after the response, before using its credentials. Anonymous readers do
not use this endpoint. The Reader rechecks its local grant after the read and includes the
binding in its result. Core verifies the captured revision,
current authorization, lease and privacy epoch in the same write transaction before
committing observations. Each accepted event is linked to its immutable grant revision.

To stop access, revoke in Core first and disable the private Reader file. Only explicit
owner `forget` withdraws processing of prior data; an upstream login error or a local
file change cannot be interpreted as an instruction to delete history. A revoked/forgotten
grant cannot be reactivated; a new owner authorization uses a fresh ID. For withdrawal
covering several prior grants, the owner must select every applicable ID explicitly.
Do not infer account relationships from URLs, text, fingerprints or source names.

## Erasure, recovery and budgets

`forget` uses the established dependency closure for observations, memory evidence,
diary evidence/revisions, goals and social candidates. Existing conservative clearing of
shared summaries, traits and pending reply content remains in force; SENDING replies
retain delivery uncertainty. It does not retry QQ sends or refund uncertain reservations.
For an unused grant, forgetting only records the tombstone/cancels its work and preserves
unrelated history.

The new ledger target stores the selected grant ID and known root event IDs, with fsync
before database changes. Replay also resolves explicitly attributed roots in the restored
database and enforces a grant tombstone even for a backup taken before registration.
Complete ledger projections cannot authorize new writes for a forgotten grant. Restoring
any backup revokes all restored future-read authorizations while retaining authorized
history unless the latest independent ledger requires erasure. Use the supported encrypted
restore procedure; do not replace a live DB or restore an old ledger alongside it.

Started reads rejected after revocation retain their reservations/unknown traffic bounds.
The result is discarded rather than retried or silently accepted under a new revision.
Pure future-read revocation does not advance the global privacy epoch or erase prior
observations. Explicit processing withdrawal does, preserving the existing late-result guards.

Core revalidation and the upstream GET are separate network operations. Revocation after
a successful validation cannot recall a permitted or in-flight GET; Core rejects its late
result at commit and the next GET requires another validation. Stopping an active Reader
and disabling its isolated file also prevent subsequent local requests. This protocol does
not claim atomic distributed revocation across an upstream server.

Grant withdrawal also follows explicit memory supersession links and owner correction
events derived from those memories. It clears their vectors, later dependent observations
and diary evidence. The ledger records the event/memory closure before applying erasure so
replay can clear recorded stale successors even when a COMPLETE projection and the grant
tombstone are already present. Independent owner history outside that closure is retained.

An older source-grant COMPLETE record may contain only root event IDs, after already
deleting the predecessor memory's evidence. If an active successor still points to a
FORGOTTEN predecessor, replay cannot safely reconstruct that grant attribution. Startup
and encrypted restore stop with `SOURCE_GRANT_LEGACY_ERASURE_REVIEW_REQUIRED`; restore does
not publish its destination, and the original data and independent ledger remain intact.
Keep processing disabled and review an isolated recovery copy with the owner. Prefer a
trusted backup from before that erasure together with the latest independent ledger, so
the preserved evidence can establish the full closure. Do not replace or truncate the
ledger to bypass this review gate. Restore never appends a new erasure record under the
temporary restored database's lock.

## Migration and historical review

Migration `0016_source_grants` adds the registry/revisions/event links and captured lease
columns. It does not attach existing events to invented grants or delete historical data.
It inventories old Bilibili/Heybox account or opaque source evidence, including title-only
records whose acquisition mode cannot be proved. A known public reading method is kept
distinct from an account method; URLs and model text are never used to assign a grant.

Before upgrading a database with historical source data, inspect an isolated copy and keep
Reader, memory, consolidation, reflection, diary, goals, sharing and content processing
disabled until the owner reviews the inventory and its derived closure. This is the
conservative freeze procedure; this migration does not automatically quarantine every
existing derived projection. A nonempty inventory is an unresolved release gate.
Preserve the original records, obtain non-credential provenance from the owner, and prepare
an explicit reviewed mapping or continued freeze. Explicit deletion can use the existing
owner event-forget/Ledger path. Neither a new login nor a new registration proves an old
grant; never run an inferred bulk delete or a blanket historical reassignment.

Back up DB and the latest independent ledger before migration. Downgrade refuses when any
grant exists; export/reconcile in a separate recovery target first. Older binaries reject
the new ledger target, so do not roll them back over current data or discard newer ledger
records to make startup pass. Rollback means stop account readers, preserve their isolated
credentials and all current data/ledger evidence, and use a compatible verified recovery
environment. This PR does not migrate or enable the real QQ runtime.

## Validation

The regression suite covers expiry/read-revocation retention; exact adapter/revision
binding; owner-only registration/erasure; local/remote late-result rejection; pre-I/O
rejection when the Core drops a binding; memory/diary/social dependency erasure; encrypted
restore both before registration and after evidence; restored grant revocation; and legacy
inventory without deletion or invented attribution. Final local/CI results are recorded in
the independent G1 PR and its private validation manifest. Real Reader accounts remain off.
