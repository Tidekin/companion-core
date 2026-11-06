# Life topic projection and SQLite contention

An idle Life scan used to update every already-due topic to `DUE` again while
holding `BEGIN IMMEDIATE`. SQLite counts these matched rows as changes. Each
following source check therefore invalidated the existing verified budget view
and replayed the custody journal. Concurrent chat, Console and Bridge writes
could exhaust their lock waits even when no Life decision or model call ran.

`LifeTopics.synchronize` now leaves already-due rows untouched and uses the
existing `Store.read_or_write` boundary. An idle projection runs on a query-only
WAL snapshot. A real transition retries the original operation once under the
native write transaction, repeating source, authority and expiry checks against
that transaction. Supersession, expiry, new evidence and `WAITING` to `DUE`
transitions retain their original atomic writes.

Budget cache identity, cache size, journal validation, erasure replay and final
model/delivery admission remain owned by their existing implementations. No
schema, configuration, scheduler or delivery protocol changes are required.

Regression checks exercise an independent writer during idle source inspection
and repeated native signed-history scans with the real custody validator. The
Life topic, history, consumption, worker and budget-cache suites cover actual
transitions and permission changes. Provider transports are mocked.

On the same isolated native snapshot, one idle Life step fell from 17.39 seconds
with 33 full journal replays to 2.72 seconds with no full replay. Its metadata
scan no longer acquired the writer lock; the other observed native Life lock
scopes stayed below 23 ms. These timings describe that snapshot, rather than a
deadline guarantee for every future workload.
