# QQ 表情候选与后台找图

本集成已接入 Core HTTP v1、回复规划、Outbox、Bridge `MemeRuntime` 和空闲调度。

原生静态目录的输入审批沿用 `OwnerCapture`，通过独立 `meme_offer` 旁车冻结候选，
不改变原签名或增加授权协议。审批与选择均要求当前 meme/expression 用途，图片最终
发送复核 meme 用途。该接入复用已检查的 DSH QQ
[gateway bootstrap](https://github.com/tencent-connect/dsh-qqbot/blob/c8b9158b37817b0dbc1e514e62e16204ad47a6ce/src/gateway/bootstrap.ts)
中 transport/media 与入口组装的职责分离；当前权限与能力元数据分离参考
[RFC 7662 §2.2](https://datatracker.ietf.org/doc/html/rfc7662#section-2.2)。
候选、预算和不可重放发送继续使用项目既有目录、Core job、Outbox 与 Bridge journal。

原生宿主由 `app.run` 组装 `owner_memes.MemePump`。输入 ACK 后立即启动最多 1.5 秒的
语义检索，使用原签名 input-state 的实际回复版本；输入上传不等待检索。
静态目录在输入审批前冻结候选。后台发现等待原回复完成，并让位于待提交主人输入、
媒体；资料观测和后台发现互相等待已启动的任务完成，定时资料观测不会抢占已经
预留额度的发现任务。新输入、撤回和 STOP 仍可中断发现，已预留的 UNKNOWN 责任
继续保留；已有发现任务的恢复由原 Core 租约及预算控制。
`OwnerOneBot` 在准备阶段固定候选图片字节与消息摘要，最终 transport 边界复核授权、
来源与消息后再记录素材 `SENDING`。实际发送成功／不确定分别保存原 `SENT`／`UNKNOWN`；
只有 Core 确认取消且原发送 journal 已提交 `NOT_SENT` 后才释放未发送素材。
当前状态为 **implemented / mock-verified**；本轮未完成真实搜索、生产素材审核、
QQ 实机选发或线上启用，不能标记为 **configured / live-verified**。
输入中断、撤回和重放合同见[输入生命周期](input-lifecycle.md)。

## 已落地的运行路径

1. 签名主人输入审批前，Bridge 根据有界文字规则确定通用意图，从本地 catalog 取最多
   8 个候选，冻结首次 `meme_offer`，包括空候选和无 offer 的决定。HTTP 审批重放使用
   同一投影；严肃、拒绝表情或不适用场景保持文字。
2. `POST /internal/v1/live-text/inputs/approve` 可携带
   `meme_offer={offer_key,intent,candidates}`。`offer_key` 为 `meme:<event_id>`；候选只含
   `candidate_id/description/tags/category`，不传本地路径、图片字节或下载地址。
3. 同一次聊天模型调用选择一个候选 ID 或不用图。描述与标签是有界的不可信 JSON 数据；
   Core 校验本轮候选集合、回复版本、概率、严肃场景和频率，不增加专门的选图模型调用。
   当前私人陪伴输入没有 offer 时只生成文字，不重新随机选择旧分类素材。
4. Outbox 冻结 `content.image={source:"companion.meme_catalog",offer_key,candidate_id,category}`。
   Bridge 绑定实际 delivery key 并准备固定字节，沿原 begin-send、最终授权和 OneBot
   发送链路，将文字与最多一张图片组成一次请求。准备时发现素材缺失或停用则回退文字，不换候选。
5. 当前文字回复完成、没有前台消息或媒体工作时，`MemePump` 才提交缺素材的发现任务。
   `MemeRuntime.run_once()` 通过 HTTP 领取 Core 租约；新输入、撤回和 STOP 优先。
   本轮回复不等待下载，新素材用于后续轮次。

Core 不导入 Bridge 模块，不下载图片，也不持有搜索密钥。发现任务复用 `jobs` 的
`kind="meme_discovery"`，由 Bridge 执行；聊天 worker 只领取自己的聊天/活动任务。
Bridge 复用现有 spool SQLite、单实例锁、私有目录、身份绑定和磁盘容量检查。
`meme_assets`、`meme_offers`、`meme_pins` 保存素材与占用；`meme_runtime_offers`、
`meme_runtime_bindings`、`meme_runtime_results` 保存审批、投递绑定及待补交结果。

## 配置与默认值

Core 字段位于 `companion.config.Settings`，可从 Core TOML 或对应
`COMPANION_<大写字段名>` 环境变量读取。Bridge 字段位于其独立私密 TOML。
开关默认关闭；改变配置须重新冻结运行计划，不能热改已批准窗口。

| Core 字段 | 默认值与范围 | 作用 |
| --- | --- | --- |
| `expression_enabled` | `false` | 允许回复规划附带表情意图 |
| `expression_selection_probability` | `0.35`，`0..1` | 按事件与 offer 的稳定哈希决定选图机会；0 禁用，1 仍须通过其余检查 |
| `expression_cooldown_minutes` | `30`，`1..1440` | 对话级表情频率；未取消/失败的已安排图片也占用该频率 |
| `expression_meme_categories` | `happy/like/surprised/morning/sleep`，最多 12 项 | 兼容旧分类路径；新路径校验本轮候选 ID |
| `meme_discovery_enabled` | `false` | 允许提交和领取后台发现任务 |
| `meme_discovery_daily_queries` | `2`，`0..100` | 每日本地查询次数预算 |
| `meme_discovery_daily_download_bytes` | `16777216`，`0..268435456` | 每日响应体预算，含索引/API/ZIP/图片 |
| `meme_discovery_daily_storage_bytes` | `4194304`，`0..67108864` | 每日新增素材字节预算 |

| Bridge 字段 | 默认值与范围 | 作用 |
| --- | --- | --- |
| `meme_enabled` | `false` | 启用候选、固定字节与发送回执路径 |
| `meme_discovery_enabled` | `false` | 允许空闲调度执行发现任务；仍须 Core 同意 |
| `meme_directory` | 未配置 | 可配置绝对库路径；Runtime 默认使用 spool 下的 `memes` 路径 |
| `meme_search_provider` | `none`，枚举 `none/brave` | `none` 选择无密钥的社区包源，并非关闭发现；关闭使用 discovery 开关 |
| `meme_search_api_key` | 未配置 | Brave 私密凭据；缺失返回 `MEME_SEARCH_NOT_CONFIGURED` |
| `meme_trusted_sources` | 空列表 | 当前默认审核器的固定社区素材审核规则；实际 Runtime 最多 32 条 |
| `meme_catalog_max_assets` | `512`，`1..4096` | 素材记录数量，含停用记录 |
| `meme_catalog_max_storage_bytes` | `67108864`，`1..1073741824` | catalog 图片字节上限，叠加 spool 总磁盘限制 |
| `meme_catalog_max_bindings` | `10000`，`1..100000` | 固定候选/绑定/结果等有界记录容量；满后不删除凭证腾位 |
| `meme_cooldown_seconds` | `1800`，`0..86400` | 同一图片真实 SENT 后的持久冷却 |

Core 对话频率与 Bridge 图片冷却分别执行，不自动相互换算。实际回复还要求
`automatic_reply=true`、`companion_scope="owner_private_v1"`、有效签名窗口与原发送权限。
启用开关不会补齐窗口批准、搜索订阅或素材许可。
历史候选容量已满时，新输入使用文字路径；已冻结的候选仍按原身份重放。
发现结果或语义来源容量已满时，先补交原待确认结果，再推迟新任务领取，避免取得费用责任后无处保存回执。
容量降级不会删除历史记录、重置冷却或释放 UNKNOWN。

## HTTP v1 与持久预算

接口均沿用 Bridge 内部认证，前缀为 `/internal/v1/memes/discovery`。
Core 迁移 `0023_meme_discovery_reservations` 保存每个 job 的单次消费责任；记录不为空时
禁止直接降级删除该表。Core 日期按 `settings.timezone` 计算，预留归属预留当天。

| POST 路径 | 请求及成功回执 |
| --- | --- |
| `/submit` | `event_id/reply_revision/intent/provider`；返回 `job_id/event_id/reply_revision/state/duplicate` |
| `/lease` | `worker_id`；返回 `item:null` 或含源、版本、provider、`lease_token/lease_until/attempt_key` 的任务 |
| `/{job_id}/check` | `lease_token/attempt_key`；返回 `approved:true` 及同一任务、源、版本和截止 |
| `/{job_id}/reserve` | 租约见证加 `limits`；返回同一任务、尝试、截止、限额与 `state:"RESERVED"` |
| `/{job_id}/result` | 租约见证加 `outcome`（SETTLED 或 UNKNOWN）、`usage`；返回任务、尝试及最终状态 |

`intent` 仅接受 `celebrate/comfort/like/surprised/morning/sleep/playful`；provider 为
`community` 或 `brave`。公网查询来自固定通用关键词，不发送私聊正文或主人身份。
任务领取等待主人回复和现有模型工作；同时最多一个 RUNNING 发现任务。
`usage` 记录 `queries/requests/download_bytes/storage_bytes/model_cost`，均为非负整数，
其中 `model_cost` 固定为 0。

Runtime 每次请求固定默认 `DiscoveryLimits`：1 次查询、12 次 HTTP 请求、16 MiB 下载、
4 MiB 新增素材、最多 3 张图、`model_cost=0`。HTTP 合同允许的最高单次值分别为
1、20、32 MiB、16 MiB、8、0；这些上界不会自动扩大 Runtime 的默认请求。
发现任务本身不调用视觉或 embedding 模型，也没有搜索服务货币账单的自动对账器。
可选的入库识图使用独立 Core job 与费用预留，见[表情语义](memes-semantic.md)。

外部 I/O 前必须预留完整单次上限。因此默认每日 16 MiB 下载预算可能在一次 UNKNOWN
后全部占用，即使查询次数上限为 2。SETTLED 使用实际量；RESERVED/UNKNOWN 按预留与
已观察量的较大值计量。相同 job 不能再次取得消费预留，已有素材不重复计新增存储。
实际超额仍接收并记录，转为 UNKNOWN，不通过拒绝结果回执隐藏责任。

失败、取消或结果丢失不退款。预留后 worker 丢失，Core 将过期责任保持为 UNKNOWN；
未取得消费预留的过期租约可重新排队，但旧租约/尝试见证失效。结果可在 STOP、撤回或
租约过期后按原见证补交，UNKNOWN 不自动降级为 SETTLED。Bridge 持久保存待补交 body，
先原样补交结果，再领取新工作；`SETTLED` 只表示资源用量已结算，不代表图片入库或发送成功。
Bridge 在 Core 确认返回后与图库事务提交前再次检查租约截止；达到截止即拒绝新 I/O 和
入库，不能使用尚未返回时仍有效的旧批准继续工作。

通过 `backup.restore` 恢复时，已有发现 `RESERVED` 转为 `UNKNOWN`，排队和运行中的发现
任务取消并清除工作租约；`runtime_meta` 中的原尝试见证保留用于结果对账。恢复不会重新
授权查询、下载或入库，也不能用旧备份消除已消费责任。

## 素材审核与来源证据

默认 `TrustedReviewer` 已装配，规则来自 `meme_trusted_sources`。每条规则精确包含
`repo/revision/license/license_path/license_sha256/asset_prefix/usage_basis`；revision 为
固定 40 位提交 SHA，许可摘要为实际许可文件的 SHA-256。空规则不会自动接纳素材。

当前审核器只接纳规则匹配的社区素材。启用语义且有本地可信规则时，`CommunityPacks`
从固定索引中选择已批准仓库，直接使用规则中的 40 位 revision，依次获取该版本的
manifest、实际许可文件、匹配类别的 GitHub contents listing 和最多 3 张候选原图。
manifest 上限 512 KiB、许可文件 64 KiB、单个类别 listing 256 KiB；全部经
`PublicFetcher`，仍共用每次 12 请求、16 MiB 下载和 4 MiB 新增素材的预算。
Bridge 校验 manifest 类别、许可摘要、受限图片路径，以及 listing 中的路径、长度和
Git blob SHA 与实际原图字节的绑定。此路径不下载整包或 `semantic_metadata.json`；
通过来源审核的素材进入待识图状态，由 Core caption 生成描述和标签后才可参与选图。

未启用语义时保留原 ZIP 路径：核对真实 ZIP 请求和最终来源、固定版本、实际许可
文件字节、包内成员路径、原图片哈希与语义条目一致性，并要求可用的描述/标签。
只有审核确认后才写入 catalog。Brave 的 SafeSearch、上游 `verified` 或许可字符串均
不能替代这些证据；仅配置 Brave key 不足以让普通搜索结果通过当前审核器，缺证据返回
`SOURCE_EVIDENCE_REQUIRED`。支持更广的来源需要独立实现并验证审核合同。

`ReviewEvidence` 保存实际初始/最终 URL、每跳 URL/公网地址/HTTP 状态、响应体和图片
SHA-256、抓取时间、来源网页/版本声明及 ZIP 成员路径。按文件路径的主 `fetch` 对应
实际原图，`archive_member=""`，另含 `source_files` 对象：严格只有
`manifest/license/listing` 三键，每个值均为 `FetchEvidence`，包含
`requested_url/final_url/hops/body_sha256/fetched_at`。四份获取记录必须对应同一仓库和
固定 revision，均为无重定向的单跳 200；许可文件摘要必须匹配本地规则。
旧路径完全省略 `source_files` 字段，保持历史凭证与缓存哈希不变。

获取凭证由本次 `PublicFetcher` 产生并按对象身份验证，provider 不能伪造凭证或替换
实际获取的字节。审核返回
`ReviewApproval(metadata, evidence.sha256)`，来源、版本、时间与实际获取记录必须一致；
`acquisition_json` 持久保存同一凭证。来源网页仍是来源声明，不因被记录就成为许可证明。

可信规则可自动接纳合格素材，无须逐张人工确认；管理者负责规则维护、纠错和停用。
`catalog.remove(sha)` 清除素材字节并保留停用记录，重复发现不复活；本地导入继续通过
`add_local(path, Metadata, check=...)` 的路径、字节及元数据检查，不自动信任目录中的图片。
若新增联网或付费模型审核，必须先扩展持久预算和对应验收，不能在 reviewer 回调内绕过预留。

公共下载只允许公网 HTTPS；每跳校验全部 DNS 答案、数值地址连接并保留 Host/SNI。
拒绝私网、URL 凭据、非标准端口、压缩 HTTP 响应、超限头部/字节、超时和过多跳转。
Brave 凭据只发往固定搜索主机且禁止重定向；社区索引、提交解析、按 SHA 下载包及
按文件导入的 manifest、许可、listing 和原图请求也禁止重定向。
ZIP 在内存中检查：压缩最多 8 MiB、展开 16 MiB、1024 项、单项 4 MiB及压缩比上限；
拒绝路径穿越、重复大小写路径、反斜杠、盘符、链接、特殊文件和加密项，不执行包内代码。

图片复用 `MemeLibrary.validate_bytes()`：PNG/JPEG/GIF、最多 4 MiB、每帧画布最长边
2048、最多 120 帧、整图累计解码像素最多 1600 万。逐帧检查实际完整画布及偏移扩展，
保留已验证原始字节；不以首帧乘帧数估算动画大小。

## 发送结果与运维处理

| 状态或结果 | 处理 |
| --- | --- |
| 无候选、禁用、缺 key、审核证据不足 | 当前文字回复照常完成；核对配置/规则，不声称已经找到或发出图片 |
| `DEFERRED` / `BUSY` | 前台优先或暂不获准；不绕过 Core 另发网络请求 |
| 图片 `PREPARED` | ID 与字节已绑定，仍须最终授权；不能据此记为已发送 |
| 图片 `SENDING` | 已占用；崩溃残留保守阻止复用，不凭重启猜测发送失败 |
| 图片 `SENT` | 只有实际 provider ACK 才记录成功并开始冷却；重复 ACK 不延长冷却 |
| 图片 `UNKNOWN` | 不重发、不释放占用；后续真实成功证明可记为 SENT，不能以 NOT_SENT 清除 |
| `NOT_SENT` | 仅当前未派发操作加精确 Core 取消回执可确认；保存原发送次数，详见输入生命周期 |
| 发现 `RESULT_PENDING` / `UNKNOWN` | 保留待补交结果与预算责任；不得创建替代任务重复消费 |

`cancel()` 对 PREPARED 取消，对 SENDING 保留 UNKNOWN。最终网络检查和入库事务重新核对
当前输入、版本、撤回与 STOP；同一准备记录或旧 spool 中的图片不能独立授权发送。
可选找图的失败不阻塞已经获准的文字回复，真实投递或模型用量不确定仍执行原窗口对账规则。

## 合成验收与上线交接

模块回归覆盖固定候选/字节、冷却、动画边界、SSRF、来源/许可绑定、包解析、取消、
去重、并发连接、事务失败和崩溃后预算防重放。集成回归覆盖真实 HTTP v1 应用入口、
独立 Core worker、同次模型选图、OneBot 固定字节、缺图回退、UNKNOWN 及新输入/撤回中断。
所有外部 provider 使用 Mock；当前没有本轮真实搜索或 QQ 实机成功凭证。

需要复现时从仓库根目录运行对应检查；相同候选已成功的结果直接复用：

```powershell
./scripts/check.ps1 -TestFiles @(
  'tests/test_meme_service.py',
  'tests/test_meme_runtime.py',
  'tests/test_input_interrupt_bridge_memes.py',
  'tests/test_next_memes_catalog.py',
  'tests/test_next_memes_discovery.py',
  'tests/test_next_memes_packs.py',
  'tests/test_next_memes_provenance.py',
  'tests/test_next_memes_animation.py'
)
```

交接应链接集成负责人保留的最终 CI summary，随后在已批准窗口中分别确认实际公共获取、
素材审核入库、模型选择和 QQ ACK。配置、合成通过、图片入库和实际发出是不同观察结果，
逐项记录，不能互相替代。

固定社区索引与包协议参考 [community-index.json](https://github.com/anka-afk/astrbot-meme-pack-index/blob/2f173ca5dc0b460d9c6408a4f7f0e9a83371677f/community-index.json)
及[包格式协议](https://github.com/anka-afk/astrbot-meme-pack-index/blob/2f173ca5dc0b460d9c6408a4f7f0e9a83371677f/ASTRBOT_MEME_PACK_PROTOCOL.md)。
实现独立编写；读取索引格式不等于获得包内所有素材的许可。
