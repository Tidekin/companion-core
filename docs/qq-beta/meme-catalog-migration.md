# Core catalog staging migration

B1a adds an offline, staged catalog. Bridge remains the running catalog owner until
the separately reviewed B1b cutover. Importing never creates a model job, admission,
usage, Outbox item, execution permission or replacement transport receipt.

## Records and identity

Migration `0047_meme_assets` follows `0046_capability_delegation`.

- `meme_assets` has `asset_id` (primary key), `custody_sha256`, `asset_sha256`,
  `source_sha256`, `record_sha256`, `state`, nullable `mime`, `data` and
  `metadata_json`. The unique key is `(custody_sha256, source_sha256, asset_sha256)`.
  `asset_id` hashes those three values. Custody hashes the original spool identity
  `[owner, bot, origin, protocol_epoch]` and capture public key; it excludes mutable
  journal heads. Equal bytes from different custody or source/license remain distinct.
- `state` is `STAGED`, `QUARANTINED`, `REMOVED` or `ERASED`. None is executable in
  B1a. Unattributed records retain hashes only, with no bytes, metadata or captions.
  `REMOVED` and `ERASED` never return to `STAGED`. `record_sha256` preserves the
  original immutable bytes/source/metadata digest after erasure; the active flag
  is separate so a later removal can only narrow availability. Changing the same
  key's metadata or bytes is refused.
- `meme_asset_sources` relates an asset to existing `events` and original
  `meme_captions` jobs. It records every source of the caption reuse ancestry.
  It has no nullable/invented event or job and creates neither. An unattributed
  asset has no source rows. Sources survive as minimal erasure attribution.

Core verifies the original owner capture against its original authority revision,
event payload, existing job/source relation and discovery attempt. The manifest's
identity, checksum and claimed STOP are transfer evidence only. They cannot grant
permission. Content must match Core's retained original asset/source proof; missing
provenance is quarantined rather than copied. Public static assets without existing
Core provenance require a later onboarding slice.

## Transfer format

One private JSON file has `schema_version=1`, `custody`, `assets`, and `sha256`.
`custody` contains `identity`, `capture_public_key`, original `authority` and
`journal_head`. Each asset has `asset_sha256`, `source_sha256`, `active`,
`metadata`, `mime`, `data_base64` and `sha256`. Item and envelope hashes use the
existing shared canonical JSON/digest, omitting only their own `sha256` field.
Removed items have null bytes and MIME. There are no filesystem member paths,
transport tables, capture bodies, model credentials, captions or usage copies.

Export uses the original instance lock and existing OwnerRuntime read-only custody
verification, requires STOPPED, and opens SQLite with `mode=ro`/`query_only`.
It does not construct a writable Spool or stop/recover a running instance. Legacy
custody lacking the native proof is refused. Original limits apply: 4096 records,
64 MiB aggregate original bytes, 4 MiB per PNG/JPEG/GIF and the shared decoded-media
limits. The complete JSON file is bounded to 100 MiB; metadata stays individually
bounded. Both item and envelope hashes are checked before any import.

Core dry-run and reconcile operate on an in-memory SQLite backup of the target.
They replay the supplied existing independent ErasureLedger there, leaving actual
source/target business data unchanged. Import requires the original database's
matching Ledger identity, replays it before insertion and rechecks the Ledger and
privacy epoch before commit. No missing Ledger is initialized. The operator must
supply the latest independently retained Ledger; existing truncation checks remain.
Prepare the target through Core's normal Alembic initialization first; the CLI
refuses a pre-0047 schema rather than mutating it during a dry-run.

The new `meme-assets` derived cleanup handler uses the existing registry signature
to repair older COMPLETE snapshots. It removes bytes, MIME and metadata and retains
only hashes, state and source attribution. Existing restore already migrates before
replaying that registry, so `backup.py` needs no new restore mechanism. Original
model fees, ACK timestamps, attempts and UNKNOWN holds are never modified.
Read revocation/expiry stops future acquisition; explicit historical withdrawal
and existing retention erasure follow their original Ledger semantics.

## Rehearsal and rollback

Export: `python -m companion_onebot_bridge.meme_export --config <private.toml>
--output <new-private.json>`.
Core: `python -m companion.meme_asset_migration --config <private-core.toml>
<dry-run|import|reconcile> <private.json>`.

Commands print only hashes, states and counts; errors use fixed codes, never raw
metadata or configuration. Use synthetic copies first. Import twice must reconcile
without adding financial or delivery work. A changed item under the same key fails
the entire transaction. Pausing or abandoning staging leaves Bridge's original
authority and transport custody in place. Rollback never restores an old catalog
over the latest erasure history, clears UNKNOWN, resends an ACK, or refunds charges.
Production migration and ownership cutover require separate release review.

The design reuses recorded unchanged DSH attachment/vision separation and this
project's original receipt/erasure contracts in [caption semantics](memes-semantic.md)
and [original receipts](../plans/2026-10-25-meme-original-receipts.md).
Coherent rehearsal copies use the existing
[SQLite backup API](https://www.sqlite.org/backup.html) and transactional writes use
[SQLite transactions](https://www.sqlite.org/lang_transaction.html). Read-only file
connections use SQLite's existing [URI mode=ro](https://www.sqlite.org/uri.html).
Path checks use `lstat()` for every component, reject symbolic links, and on Windows
reject every `FILE_ATTRIBUTE_REPARSE_POINT`, including directory junctions. Missing
Windows file attributes or filesystem errors refuse the operation. This follows the
existing raw-stat approach in voice-node state custody and uses the
[Python 3.11 file attributes](https://docs.python.org/3.11/library/os.html#os.stat_result.st_file_attributes)
and [stat constants](https://docs.python.org/3.11/library/stat.html#stat.FILE_ATTRIBUTE_REPARSE_POINT),
preserving the project's Python 3.11 minimum without `Path.is_junction()`.

## Verification status

Local implementation and synthetic checks cover the independent export/dry-run/
import/reconcile processes, exact original-work preservation, identity/source
isolation, hash conflicts, process death, pending erasure, old COMPLETE repair and
pre-0047 backup restoration followed by reimport. The existing restore migrates
before replay and needs no change. Tests are in
[migration checks](../../tests/test_meme_asset_migration.py) and
[erasure coordination](../../tests/test_erasure_coordination.py).
The B0 baseline's 84 targeted tests remain evidence for unchanged portions.
B1a's [final main validation](https://github.com/yanyuhanyue/companion-core/actions/runs/40224533506/job/121263068243)
reuses the verified identical-tree full PR checks. Ownership cutover, SHADOW,
production migration, real QQ delivery and human acceptance remain separate stages.

## B3a: read-only historical retirement preflight

Run `python -m companion_onebot_bridge.meme_retirement --config <private.toml>`
only against the stopped native Bridge store. The report diagnoses local blockers;
it never authorizes a cutover, clears content, changes a pin, settles fees or changes
the independent Core Ledger. Exit 0 means only that these local checks found no
blocker. Exit 2 means a local blocker. Both report `state=BLOCKED` and
`cutover_authorized=false`: latest independent Core Ledger/live reconciliation,
explicit managed-copy retirement and backup revalidation remain external gates.
No arbitrary JSON report or old hash substitutes for those current checks.

The CLI reuses the export's component-by-component regular-file/reparse refusal,
the original nonempty instance lock and read-only native owner verifier. The
`existing_only` lock option opens that same file without creating, initializing or
chmodding it; default callers retain their original behavior. The original OS lock
is held throughout. Reading can update filesystem access times, which is distinct
from product or custody writes.

SQLite [read-only WAL access](https://www.sqlite.org/wal.html#read_only_databases)
can still create sidecars. This preflight therefore refuses any nonempty `-wal` or
`-journal`, or residual nonempty `-shm` with unproven recovery state, as
`WAL_RECOVERY_REQUIRED`, without checkpointing, repairing, removing
files or copying the database. Only a known clean, stopped baseline is opened with
standard [mode=ro and immutable=1](https://www.sqlite.org/uri.html#uriimmutable)
and `query_only` in one transaction. Immutable skips SQLite's own locking/change
detection; it is limited to this offline tool under the original OS lock. The
database, all known sidecars, owner journal, STOP marker and lock are fingerprinted
before and after, including absence, SHA256, size, modification and change times.
Any observed change rejects the report. This is not a running-reader strategy.

The report contains counts, known schema fingerprints, fixed reasons and necessary
custody hashes. It emits no caption, image bytes, URL, query, private path, credential
or raw authority. Unknown/missing table shapes, unverified source/receipt links,
unsettled caption/discovery usage and unfinished original handoff custody block.
`PREPARED`, `SENDING` and `UNKNOWN` pins block independently even if an owner
resolution exists: that existing resolution does not project the meme pin. No
timeout or elapsed cooldown releases such a pin.

Historical `SENT` pins need their original binding, ACK and trustworthy timestamps.
The original journal does not bind the historical cooldown value to each pin;
current config, a default, maximum, image or ACK time alone does not supply that
missing policy. These rows report `ORIGINAL_COOLDOWN_UNVERIFIED`; `wait_until` stays
null. Neither an old timestamp nor a shorter new config grants release.

For the next cleanup slice, the following content and responsibility columns must
be reviewed separately. This inventory grants no cleanup permission:

| Existing Bridge table | Content to consider retiring | Responsibility to preserve |
| --- | --- | --- |
| `meme_assets` | `data`, `metadata` | physical `sha`; source verification |
| `meme_offers` | `intent`, `category`, `ids` | original offer key |
| `meme_runtime_offers` | `offer_json` | original event/window hashes |
| `meme_runtime_semantic_offers` | `query_binding`, `offer_json` | event/window/revision/state |
| `meme_runtime_sources` | `claim_json` | original SHA/attempt and settlement relation |
| `meme_caption_cache` | `identity_json`, `result_json` | original `usage_json` and cache/asset keys |
| `meme_caption_bindings` | `context_json`, `outcome_json` | original request/source/job/usage/state |
| `meme_semantic_vectors` | `vector_json` | asset/source/cache/caption/model identity |
| `meme_semantic_offers` | `proof_json` | original key/query/profile hashes |
| `meme_pins`, `meme_runtime_bindings` | no cleanup in B3a | original immutable delivery identity/state/time |
| `meme_runtime_results` | no cleanup in B3a | original retry body, state and incurred usage |
| owner journal, attempts, results and resolutions | no cleanup in B3a | original ACK/UNKNOWN/NOT_SENT and fee evidence |

`meme_catalog.remove()` clears original bytes and vectors but leaves other content
listed above. It does not establish full retirement. The B1a manifest itself also
contains private bytes/metadata. Enumerate each managed artifact explicitly with
`--managed-copy <file>` (maximum 32 files, 100 MiB each); only its path hash, content
hash and size are reported. The CLI neither discovers nor traverses an operator's
`meme_directory`. Old Bridge backups/binaries have no automatic anti-resurrection
guarantee: restoration must repeat current STOP/authority validation, latest Core
Ledger/reconcile and the reviewed retirement procedure. B3b cleanup, compaction,
pin receipt projection and production acceptance are separate work.


## 已确认的旧图隔离与 Core 切换

当主人明确选择隔离旧图而保留账本时，使用独立的 `meme_isolation` 操作。
它不把 B3a 的诊断报告变成授权，也不把旧 UNKNOWN 改成 SETTLED。

1. 按原生发布流程停止、完成实际进程静止证明与 handoff prepare。所有原 caption 费用须
   已结算；待回报的 discovery 或未完成 native handoff 继续阻断。
2. 使用上述 `meme_export` 导出同一停止态目录，并通过 Core `import` 和 `reconcile`
   核对最新独立 ErasureLedger、原 capture、模型费用及来源。原图保留在 Core 中。
3. 把导出中的全部物理原图摘要加入 Core 的 `meme_excluded_sha256`，通过已有 meme
   capability revision 批准精确配置。历史 SENT 的冷却不猜测、不清零；这些图片保持隔离。
4. 在原 Bridge 实例锁下运行：

   `python -m companion_onebot_bridge.meme_isolation --config <stopped-bridge.toml> --manifest <export.json>`

5. 配置两侧 `meme_catalog_owner=core` 后完成原生激活/handoff。再核对 Core 原始工作、
   Bridge 原费用/UNKNOWN/发送记录、实际选图与取图链路。删除这次传输文件等已列明的
   临时内容副本；原备份按既有隐私保留与恢复规则管理。

隔离命令要求同一原生停止态及精确导出。它在原 owner journal 追加一次
`MEME_CATALOG_RETIRED`，并在同一 SQLite 事务清除 Bridge 的图片、caption、向量、候选和
上下文投影。旧费用、来源身份、任务状态、下载回执、原 UNKNOWN、pin 和 native 发送回执
保留。记录绑定原内容摘要、保留责任摘要与隔离图片集合，不包含私聊或图片正文。
重复命令仅核对同一原记录；后续 Core transport 可以增加其他图片的原 pin/binding。

恢复旧业务内容、修改保留责任或尝试重新启用 Bridge 图库均被拒绝。旧 binary 不认识新的
原生 journal 记录，也不能恢复业务执行。journal 已 fsync 而 SQLite 未提交时沿用原生
不一致拒绝规则，保留原 journal，必须在停止态对账；不得删除记录后重试。
此命令不处理图库外的任意文件，也不自动把私有备份标成已擦除。

### 停止态 caption 财务恢复命令

已停用旧 Bridge 业务执行、完成原 native STOP 并等 daemon 退出释放原实例锁后，可显式运行：

```powershell
python -m companion_onebot_bridge --config /path/to/bridge.toml owner-meme-accounting-drain
```

该命令有独立停止态入口。普通 daemon 的原 active gate、Pump 和启动顺序不变，停止后普通
重启仍拒绝。ACTIVE daemon 持锁时财务命令拒绝；它不会主动停止进程或启用自动财务轮询。
不构造 Spool、OwnerRuntime、MemeRuntime 或业务 catalog，不创建 schema、不刷新 profile、
不领取 discovery、不取图、不运行模型、不启动 OneBot/WS/probe，不执行 handoff 或清理。
只使用既有 Bridge 配置的私有 Core URL/Bearer，通过已知 job 的 responsibility 接口读原责任；
配置、token、私钥、原始 ID、路径与正文均不输出。

原 `existing_only` 实例锁、精确原 schema/身份、STOP 标记、native capture/journal 必须有效。
只打开原库 `mode=rw`；非空 WAL/journal/SHM 需要恢复时保守拒绝，不做恢复或 checkpoint。
每次 HTTP 前后及提交事务内重验原 custody 和完整绑定快照。唯一允许的 SQL 写入是既有
`meme_caption_bindings.usage_json` 单调费用投影；与活跃内容路径共用一个 writer，原
retain_usage 的 job/state/None-usage 内容保护仍在原 atomic 内。不会把原 Core CANCELLED
改写为本地 FAILED，不覆盖本地 SUCCEEDED，不制造 CaptionOutcome、NO_ADMISSION 表或成功。

每个命令对有限完整 inventory 扫描一遍，最多 10000 行；缺 job、重复 job、超限或证据缺失
均为未验证。每 pass 最多 8 个不同 job，每次 await 最多 0.5 秒，每 pass 协作预算 2 秒；
失败、超时、未变 UNKNOWN 或 NO_ADMISSION 后游标也前移，后续费用不会被第一行饿死。
游标仅在 RAM，不循环等待 SETTLED；取消后重新运行会重新验证。同步 native 验签、SQLite、
文件 I/O 和取消收尾不能被 asyncio 抢占，因此 2 秒不是硬墙钟保证，也没有新增子进程强杀机制。

JSON 输出 `version=1`，含固定 `state`、`reasons`、`custody_sha256`、`cutover_authorized=false`，
以及 inventory/attempted/updated/settled/unknown/reserved/no_admission/cancelled/unverified/
remaining/result_pending 计数。计数按绑定行，不累计或重复收取共享原 U 的金额。
启动阶段未能验证库存时 inventory/remaining/result_pending/custody_sha256 为 null，
unverified 至少为 1。解析器只拒绝参数；财务拒绝作为固定 JSON 状态返回，不能仅凭进程退出码
判定已完成。前端如将来展示该摘要，应按以下状态与计数对接，仍需独立前端验证；本片不含 UI。

- COMPLETE：本轮已知 job 财务均验证、原工作终态，费用 SETTLED 或完整 NO_ADMISSION。
- PENDING：保留 RESERVED/UNKNOWN、非终态工作或 RESULT_PENDING；原责任不能释放。
- UNVERIFIED：原锁、schema、custody、接口、库存或证据无法验证；保留旧投影，不能推定无费用。

UNKNOWN 不退款，原 RESULT_PENDING/CANCELLED 与取消异常不被成功恢复掩盖；取消只回滚当前
未提交事务，先前合法提交保留。旧备份的最新 Ledger 重放/restore 标记以及原 v2 owner decision
journal 后续 policy_redacted 的保守拒绝不变，不恢复 policy、不清标记、不扩大 journal 权限。
COMPLETE 也只是本轮财务摘要，原 preflight、ACK/pin/cooldown、拷贝退役与切换门禁继续独立。
正文读取仍服务活跃 caption/索引消费者；本命令不允许清正文或重启旧内容 worker。
