# v0.5 bounded content reads and private sharing

The life worker schedules a persistent discovery intention, then authenticated readers fetch
titles and content. Discovery creates evidence-bound goal candidates; a completed read can advance
a goal and propose an owner-only QQ share. Core retains all goals, observations, interests and
delivery state when a reader is replaced.

## Enabling and budgets

`exploration_enabled`, `observation_enabled`, `goals_enabled`, `wake_enabled` and `social_enabled`
default to false. HTTP request/day, request/month and byte/day budgets default to zero. Set a
distinct observation token for each source; the Bridge QQ token cannot call the reader endpoints.
`exploration_daily_deep_reads` defaults to 5 and accepts an explicitly configured limit from 0
through 30. It limits detail reads independently; discovered candidates have a
shared daily limit. Each lease reserves the maximum response bytes before I/O. Unknown results
retain that reservation. Readers use one bounded request, no redirects, and a 20-second total
timeout. Unclaimed work waits for the reader without exhausting goal attempts; started failures
use the goal's bounded retries. Requests and reservations survive restarts.

QQ replies, timed greetings and content shares use the existing Outbox. Private shares share the
same proactive cooldown/day limit and respect quiet hours, owner inactivity and pause. Exact URLs
and normalized content hashes are deduplicated across retained candidates; Chinese BGE embeddings
also screen the latest 100 candidates in 30 days for paraphrases. This similarity check is an
approximation: an uncertain/unavailable local index suppresses a share. It does not consume an LLM
call or alter owner preferences.

## Reader boundaries

`POST /internal/v2/readers/{adapter_id}/lease` returns a single source-scoped GET request.
`POST /internal/v2/readers/{adapter_id}/{request_id}/result` accepts its immutable lease receipt.
Result cardinality, target, byte reservation, source provenance and privacy epoch are checked in
the commit transaction. An adapter cannot promote a title into `WATCHED`.

The bundled Bridge reader supports Bilibili video metadata and static public pages. A video
description is recorded as `FETCHED` text, never watched audiovisual content. Small static Heybox
articles can be read directly. A JavaScript shell, login wall or access restriction produces an
explicit unavailable result; it does not produce a fictional post.

An optional independently configured Heybox source service may implement this two-route contract:

- `GET /api/feeds?page=1&limit=N`: `{status: "ok", result: {links: [{linkid,title}]}}`.
- `GET /api/post/detail/{id}?page=1&index=1&limit=1&is_first=1&owner_only=1`:
  `{status: "ok", result: {link: {title,text}}}`. Text can be a string or JSON block array.

The connector is compatible with the read routes inspected in
[xhh-workshop](https://github.com/Sakura520222/xhh-workshop/tree/3f1ca9aee836691f134a2c744ff64fe6f3534ef6).
It is an HTTP client, not a bundled server or Agent dependency. Configure a private service address
and separate bearer token; plain HTTP public addresses are rejected. The account stays in that
service. Only the two GET routes above are exposed to the reader; login, comments, likes, posts and
Agent routes are never called. The upstream API is not platform-guaranteed and requires a valid
account/session. Fixture compatibility does not establish account or production availability.

Bridge configuration uses `bilibili_reader_*`, `heybox_reader_*`, and optional
`heybox_api_url`/`heybox_api_token`. Public HTTP clients carry no Core credentials. No browsing
content can change a capability or execute a tool.

## Erasure, recovery and verification

`0009_exploration` adds read requests, source links and share candidates. Erasure clears URLs,
comparison text, lease bodies and source-derived shares, including replay into restored backups.
Restore makes outstanding requests `UNKNOWN`; delivery reconciliation retains its existing rules.
Downgrade refuses to drop populated request/share tables. Disable the feature flags to stop new
work; preserve the database and independent erasure ledger.

Automated coverage exercises a discovery-to-read-to-goal path, receipt replay, source/target
isolation, budgets and unknown usage, reader outages, prompt injection, erasure during a read,
old-backup restore, private Outbox limits, semantic duplicates, bounded HTTP and source API parsing.
No authenticated Heybox account or real QQ send was used in these checks.
