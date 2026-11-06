# Core 素材运行时合同

## 梗鲸的主人选定清单

`meme_catalog_owner=core` 时可以配置 `meme_gallery_source`，使用原生 discovery、caption、
预算、素材 BLOB、向量和 Outbox。Bridge 只保留取图及投递职责。新字段默认为空，未启用时
不改变原策略、来源证明或已签名运行配置的默认形状。

清单包含 `version=1`、原始 `gallery-index.json` 的 `index_sha256`、上游说明的
`terms_revision`（40 位提交）、`terms_sha256`、`usage_basis` 和最多 512 个
`images=[{path,intents}]`。`path` 只接受数字编号及 PNG/JPEG/GIF 扩展名，`intents` 只接受
既有七类发现意图。清单由运营方依据主人确认的来源准备；站点文字不是授权或识图结果。
清单摘要进入现有 meme capability；修改清单须使用现有原生配置续期流程。

运行时不下载可变的站点全量索引，也不声称它是本次网络获取证据。原索引摘要保留运营选图
来源；实际每次原生获取先下载 pinned README 并核对已批准摘要，再从
`https://img.aigengtu.com/meme/<编号>.<扩展名>` 获取原图，均要求单跳 200、原公网 DNS/Host/SNI
和预算检查。一次仍为最多 3 张、12 请求、16 MiB 下载、4 MiB 新写入，不增加付费额度。
已保管且属于当前清单的 URL 不重复导入；识图失败或结果不明保留原责任，不借重新导入重付费。

新证明使用显式 `gallery_selection`，保存原索引、选图清单摘要、编号与实际说明文件的
FetchEvidence。它与旧 `source_files` 互斥。Core 在识图入队、资产保管、检索冻结、取图与
最终发送时仍验证原 capture、来源、费用和当前策略；只有合格且费用已结算的 caption 可选。
修改说明、重定向或来源不匹配不能入库。完整已读的本地审核拒绝只结算实际获取计数；
部分响应、取消和不明结果仍为 UNKNOWN。

`meme_excluded_sha256` 是最多 4096 个物理原图摘要的隔离列表，只允许 Core 模式。
它进入原策略摘要，在获取、保管、排序及冻结复核中拒绝相同字节，不能换来源绕过。
隔离不清除既有 Core 原图、caption、来源关联或费用；历史撤回和删除仍通过独立 Ledger 重放。

来源研究：梗鲸的 [固定版本使用说明](https://github.com/the-beating-light-of-the-nail/deepseek-chan-meme-pack/blob/c82a0fb1c8958c2f0d6ea7fa7c12128027cfd7c6/README.md)
提供个人聊天用途与原图/索引地址；其声明不被改写为通用开源许可证。
已检查 [DSH meme 的目录与选图实现](https://github.com/yyh-001/dsh-meme/blob/32e0282b9a3aa4fb4793ad836e3c17b4180dc2c4/memes.js)：
沿用明确选定图库、原图保管与识图后检索的分离；项目自身原生授权、计量及投递合同继续负责执行。

合成验证覆盖实际签名 WS、私有 API、独立 Core worker、下载/模型 Mock 和原生 Bridge，
第二轮从新素材发送相同原图并取得 OneBot ACK；不代表生产或真人 QQ 已验收。

Core 运行时以 B1b 原生来源、费用和投递合同为基础，B2a 增加有界本地向量排序，
B2b 增加已批准 community FILE 来源的后台获取。
当前候选只作本地合成验证；完整 PR CI、生产切换、真人 QQ 和检索质量验收分别记录。
更多来源格式、历史副本退休与责任闭环、独立镜像仍由后续切片负责。

## 单主与兼容

临时 `meme_catalog_owner` 仅允许 `bridge` / `core`，默认 `bridge`。
默认值不进入旧 Settings / Bridge 快照，默认 paid、owner、meme 摘要及投递材料保持原语义。
实际非默认来源规则和 cooldown 必须序列化并进入 meme 策略摘要，即使 selector 是 bridge。
Core 模式摘要包含 selector、exact 来源规则、cooldown 及固定 retrieval profile；旧授权不能取得新策略权限。
开启 Core discovery 时还包含固定 acquisition profile：community、repository-files-v1、
原 index revision、原 DiscoveryLimits 和 exact-local-TrustedSource-v1。默认 Bridge 及
Core discovery 关闭时保留原摘要形状；开启或改变 acquisition profile 必须重新授权。
该 selector 是迁移窗口，最终退休 Bridge 业务写入，不永久维持两套可写目录。

BridgeConfig.snapshot 基于 asdict 的新拷贝，仅在实际 selector 为 bridge 时删去新增字段。
window 历史 owner_authority 缺项分支不删除 core selector。回归使用从冻结 B1a 原代码
生成的默认及历史形状 golden hash；不得将真实含凭据快照打印或另行留存。

Core 模式禁止 Bridge 候选 sidecar、offer、caption 创建、embedding 和 discovery 执行；
旧 caption 状态及 discovery result 接口保留原结算职责。Bridge MemePump 不启动业务任务，
lazy factory 直接构造 CoreMemeTransport，只创建原 pin 和 delivery binding 表。
旧历史内容副本存在本身不是已完成擦除；真实切换仍受 B3 门禁约束。

## Core 后台 FILE 获取

当前 chat 首次冻结 offer 时，有意图但候选为空会在同一事务写入 `meme.offer_empty`
审计，理由为 NO_ASSETS（无可用素材）、NOT_RELEVANT（有合法素材但都不相关）、COOLDOWN、
INDEX_<fallback> 或 SOURCE_CHANGED（排序后、冻结前来源变化）。只有 NO_ASSETS 或
NOT_RELEVANT、满足原 paid meme_input、expression、meme、exact 来源规则，且没有排队或
运行中的 `meme_discovery` job 时，才创建原 discovery job；每日查询、下载和存储配额不变。
相同冻结 offer 不重复入队。前台不联网、不等待、不回填本轮候选。

发现器在下载前跳过当前 STAGED 资产已持有的同一批准 raw URL，因此每轮图片上限计算新增
素材，下一轮会到达同目录后面的文件；不会重新下载或重复保存已持有的原图。ERASED 记录
不参与跳过，也不会被复用或恢复。
实际后台工作沿用 `MemeCaptionService.run_once` 和原 selector 的 meme_caption 调度入口，
进展返回布尔值；数据库 job kind、租约、attempt、预算、来源检查仍为原 discovery 合同。
有主人待处理、有效模型预留时让出执行。原 Outbox 为 PENDING/LEASED/SENDING 时，
在 native lease 原事务内、claim/reservation 之前让出，保留原 QUEUED job 和未消耗的
attempt；ACK 完成后仍领取该原 job。已开始 I/O 后的 preempt 继续使用原结算路径。

`companion_meme_sources` 的四个平铺文件是 stdlib-only 获取、解析和审核实现，Core 与
Bridge 复用同一函数和类型。Core 仅接受原 repository FILE、固定 repo/revision 和本地
批准的许可证文件 hash；不采用档案、搜索提示或模型输出作为来源许可。每次请求继续
执行原公网 DNS/redirect/Host/SNI、次数、字节和 lease/source fencing。获取字节暂存 RAM。

完成下载后，在原 Store 事务中原子写入原 discovery SETTLED、原 native caption request、
STAGED asset 及完整 `(event_id, caption_job_id)` 来源元组。pending caption 不可检索；
只有原 caption 模型费用 SETTLED 且结果有效后，后续轮次才经 B2a 冻结排序和投递检查使用。
同 SHA 的不同 exact 来源分别保管 BLOB、分别计入本次新写入字节；重复 exact 记录新增字节为零。
acquisition storage_bytes 计数的是本事务新增 asset BLOB，不能解释为总磁盘占用。
原 caption 原始字节上限为 2048 行/64 MiB，asset 上限为 4096 行/64 MiB，分别约束。

复用先验证所有匹配缓存的原 capture、完整祖先、原 acquisition 与费用责任。不同 capture
key/epoch、已擦除、UNKNOWN 或无法核验的匹配缓存会拒绝内容接入，已知下载可仅结算获取
计数、storage=0；不能跳过旧责任另起 caption 模型。原 UNKNOWN 仍阻止新 grant 激活。
无费用失败祖先的接管只在原事务中更新原 reuse 边并补齐所有受影响 STAGED 来源元组，
保留旧元组、资产记录和 capture；深层费用、缺失、坏链或遍历超限在模型预留前拒绝。

已知拒绝或确认回滚时只结算已观察的获取计数、storage=0；部分流、取消或未确认结果保留
原 UNKNOWN/预留责任。提交结果不明时先读取同一原数据库：已实际 SETTLED 的结果必须
精确匹配并保留；确认回滚后才允许 accounting-only 结算。不能重下载、重造内容，或以
猜测 UNKNOWN 覆盖已提交结果。写入不可用时保留原预留，由原 lease recovery 接管。
擦除与旧备份恢复沿用原独立 Ledger，清除派生内容且保留原费用、获取记录和来源归属。

## 集成接口

Context 集成人独占 `companion/config.py` 和 `companion/core.py`。精确合同：

- `meme_catalog_owner: Literal['bridge','core']` 默认 bridge，仅默认值 exclude。
- `meme_trusted_sources: list[companion.meme_policy.TrustedSource]`，最多 32 条，空列表 exclude；
  Settings.load 按 JSON 解析对应环境变量。规则字段与原 TrustedReviewer 一致，精确绑定
  repo/revision/license/license_path/license_sha256/asset_prefix/usage_basis。
- `meme_cooldown_seconds` 严格整数 0..86400，默认 1800，仅默认值 exclude。
- `await self.memes.prepare_offer(job)`：命令判定后，原 agent/context 构造前；
  恢复旧 agent conversation 时只验证原冻结值。非 Core、非 chat、已分类控制命令为 no-op。
- `self.memes.check_offer(job)`：原 `_check_context_snapshots` 中复核；原 agent/budget 的
  source_check 每次实际模型请求前、await 后及 Outbox 提交前均经过它。错误必须传播。
- Core 模式 waiting_offer=False，原 Bridge 模式的两秒等待合同保持不变。

领域生成只扫描已成功且费用已结算的原生 caption。沿用原 caption_eligible、
context_allows、分词与词法评分，再使用本地固定模型的 cosine；最多八个候选。
STAGED 表示内容保管；当前 owner/private、目的、原始来源、usage 与租约分别复核。
STOP、来源撤回、epoch、授权和费用错误必须传播。

原生 chat-only 窗口保留原文本能力：先验证当前 chat 的来源、revision、authority、租约及
所有原绑定策略。仅在该 job 完全没有既存 meme_offer 记录时，明确关闭 semantics/expression
或原 grant 明确没有 meme/expression purpose 才可 no-op，返回空可选 offer。此路径不检索素材、
创建 caption/embedding/其他工作或额外模型调用。已有记录包括 null、空候选、损坏/未知绑定
一律严格检查；disabled 或缺 scope 不能抹掉既有责任。旧 grant 的 flags/mode/来源规则变化
必须先由原 check_job/current 拒绝，STOP/撤回/epoch/paid/source/lease 错误仍原样传播。

## 有界向量与冻结绑定 v2

`meme_vectors` 是独立、可重建的 SQLite 派生表，Alembic `0048_meme_vectors`
接在 `0047_meme_assets` 后。每行绑定 exact asset/custody/source/record、原 caption job、
caption identity/content、固定模型 revision 和 512 维；不共享同字节的其他来源向量。
缓存身份取现存 canonical SHA256。只存有限、非零范数的 512 个 float32 little-endian
值（2048 字节），摘要与评分使用相同字节。最多 4096 行、8MiB 向量；满额时可使用
本次合法新结果生成冻结证明而不新增缓存行，同一来源/caption slot 可替换。
旧身份或坏缓存精确删除为 miss；新模型返回的坏向量是硬错误。

固定 profile 绑定 BAAI/bge-small-zh-v1.5 的现有 pinned revision、f32 编码、caption_text/
query_for 的 2000 字符边界、词法上限 0.49/除数 20、cosine 门槛 0.5、score 降序与物理
SHA 升序、4096 扫描、8 offer、32 cold batch、2000ms 可选预算，以及
`coverage_policy=per-candidate-bounded-v2`（query_for/ranker 为 v3，含 ambient 菜单上限 4 与 light-tone-category-diverse-v1）。Core capability hash 包含 profile，变更须重新授权；Bridge
模式的策略输入和默认/历史 golden 保持原形状。

query 由 `expression.meme_query` 从当前主人原文生成：意图线索跳过紧邻否定（“不开心”）
和“表扬+挫折”的反讽，明确索图在没有主题时以 request=any 进入合法目录（不含只适合
早晚问候的素材），自嘲按第三方/本人区分对象；严肃或拒绝表情的语境不产生意图。
没有任何线索（含被否定或反讽的线索）、非严肃、非命令或纯链接的轻松轮次得到 intent=ambient：
先用原有确定性选用概率抽签，未抽中时按无意图处理且不扫描；抽中时只取 warm/neutral/
celebratory/playful 且对象为 neutral/接收者的素材，低落语气（加班、累、困等）只取
warm/neutral，非低落时不给以安慰为首要用途的素材。按分类各取最高分一张，最多 4 张，
词法固定为 0.01、cosine 只影响顺序；提示说明这些是可选点缀，由聊天模型决定用或不用。
ambient 未命中不写空 offer 审计，也不触发补库。原始文本与 caption 的向量相似度在合成
基准上无法区分无线索语境的合适与否，因此不用它作 ambient 门槛。
明确索图时，指向接收者的嘲讽/讥讽/愤怒素材不入候选；合法候选即使无字面重合也有 0.01
词法下限。候选卡给最终选图模型附上前两条 use_when 与 avoid_when；avoid_when 不参与召回。

扫描阶段不解码原图，只核验来源、caption、费用与策略；冻结阶段对入选与新算向量的条目
完整重解析并解码。尚未冻结的库存条目若来源已过期、原始收据已清理、来源撤回、caption
未结算或内容变化，只被跳过或移出本轮候选，不使当前聊天失败；当前聊天自身的权限、来源、
revision 或 privacy epoch 变化仍按原规则停止。已冻结 offer 的严格复核不变。扫描到达
两秒可选预算时保留已核验条目，并以 LOCAL_DEADLINE 记录部分覆盖。

每个物理 SHA 先按 asset_id/caption_job_id 选定合法来源，再检查该来源的语境与自身缓存。
有 query 和自身 passage vector 时沿用词法分数加 cosine（仅 cosine>=0.5 时加入）；
语义可引入词法零重合的合适候选。单次只补首批 32 个 miss；其余本轮按词法并记录
BATCH_LIMIT。此策略不声称完整语义覆盖。来源撤回后不能换用同字节其他来源。

现存 `meme_offer:<event_id>` 严格写入 job/event/revision/query/offer、catalog_owner=core、
binding_version=2、meme_policy_sha256、privacy_epoch、catalog_bindings 与 ranking。
AssetBinding v2 保留原 source/custody/record/caption/epoch 字段，增加 ranking_sha256，
其摘要覆盖本次 ranking header 和本候选证明。owner_delivery 从已严格校验的 selected
binding 取得版本，并摘要整个绑定。HTTP 取图 DTO 和原费用、ACK/UNKNOWN 责任保持原合同。

Ranking v1 保存 exact profile、query 摘要、SEMANTIC/HYBRID/LEXICAL 模式、真实覆盖计数与
fallback 理由，以及一份 query 和最多八份选中 passage 的 canonical base64 f32 字节、
SHA、词法/cosine/总分。ranking 上限 48KiB，整个 offer 上限 192KiB；原 JSON 字节在解码前
设限，所有证明类型严格、拒绝多余字段、NaN/Inf、非 canonical base64 和不一致算术。
没有意图或 eligible=0 且未发起 query 时允许空结果 fallback=null；其他缺 query 情形必须
记录 LOCAL_WEIGHTS_MISSING、INFERENCE_BUSY 或 LOCAL_DEADLINE。

后续每次模型请求、取图与最终发送只复核冻结来源、当前权限/revision/epoch/usage/lease、
原 caption 重建内容及证明的单项算术（绝对容差 1e-12），不重新 embedding/rank 或换源。
合法删除缓存、缺本地权重后仍可验证自含证明；来源、caption、profile 或证明损坏则拒绝。
证明摘要只检测原材料内的不一致，不证明全目录最优、模型真实性或授予新权限。
所有旧 Core v1/null/opaque/未知记录是显式执行阻断，保留原费用、Outbox 与恢复责任，
不能当缺项重建或自动补签。

## CPU 生命周期与遗忘

扫描及冻结各用原 Store 短事务，await 前释放。持久 MemeCaptionService 的 index 仅保留
一个强 future 引用；复用 `_inference_lock(str(Path(cache_dir).resolve()))` 非阻塞锁。
使用原默认线程池的同步 run_in_executor 提交，再 shield 等待剩余可选预算；没有新增队列。
仅 immutable query/最多32段 caption、固定模型事实与 finally 还锁对象进入 CPU，
不传 Store、service、权限回调或凭据。encoder(local_only=True) 使用现有 CPU threads=2，
不下载、不新增 provider/caption/付费工作。输出在线程内完全物化为最多33份有界 f32。

无意图时不启动 query，也不因原权限检查耗尽可选预算而记录 query 超时。
提交线程池前重查剩余预算；已过期则释放尚未提交的锁，不创建 future 或排队。
取消向调用方传播；等待期限实际到达时允许已核验词法结果。CPU 自身抛出的 TimeoutError
仍是 MEME_INDEX_INFERENCE_FAILED。成功提交后只由 CPU finally 还锁，排队中
取消也不提前解锁；迟到结果不写缓存或 offer，异常被消费。忙锁或未完成 future 直接
INFERENCE_BUSY，不积压工作。不能强制中止 native 模型或承诺两秒 CPU 退出；若 native
调用卡住，锁持续忙，后续请求保持词法。提交前失败才由调用方还锁。

await 后重查原 job、policy、epoch、revision、lease、费用及所有拟缓存/选中来源的 exact
快照，缓存与首个 offer 同事务提交。竞争连接已冻结 offer 时只验证原结果，不覆盖。
每个选中来源/reuse 祖先及自身 chat root 登记到原 job_sources。

`meme_assets.erase` 在原 Ledger 事务内按资产来源及 caption job_sources/root 删除缓存；
原 meme-captions handler 清除整份 offer（含 query/选中向量证明）。现有 erasure contract
revision 从2变3，保留 registry/order/Ledger 格式；旧 COMPLETE 通过签名差异做定向修复，
不重清后来无关回复。失败时 DB 回滚、已 fsync 的独立 Ledger 保留并要求 replay。
实际 pre0048 加密备份恢复先正常迁移再重放最新匹配 Ledger，之后才暴露数据库。
正常 downgrade 只丢弃派生缓存；任意旧 binary 配新 schema 不是支持的自动回退。

线程生命周期复用 Python 的 [shield 取消语义](https://docs.python.org/3.11/library/asyncio-task.html#shielding-from-cancellation)
与 [primitive Lock 规则](https://docs.python.org/3.11/library/threading.html#lock-objects)。
等待超时使用 [asyncio.Timeout.expired](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.Timeout.expired)
区分上下文的期限到达和 CPU 自身异常，不改变 shield 与原共享锁的责任。
固定模型和原始来源/费用研究继续复用现存 original-receipts 文档中的来源；未引入新依赖或模型版本。

原 Outbox 是按物理 byte SHA 的占用及 cooldown 权威，不按许可别名绕过；PENDING、
RETRY_WAIT、LEASED、SENDING、DELIVERY_UNCERTAIN 保持占用，SENT 使用原 sent_at。
在原 enqueue 事务中原子检查；并发验证必须是独立连接/进程争同一原权威 SQLite。

## 租约取图与最终发送

`POST /internal/v1/memes/deliveries/{message_id}/asset` 使用原 Bearer 与 OwnerAuthority。
请求严格为 authority/event_id/reply_revision/lease_token/content_sha256，不接受自由资产
选择、URL、路径或新费用。服务验证原 Outbox、owner_delivery、private 目标、当前
LEASED/SENDING 租约、revision/source/epoch/策略及冻结来源后读取相同原图；无新工作或
累计发送 admission。响应字段严格为：

`message_id, delivery_key, event_id, reply_revision, authority, content_sha256, lease_until,
asset_id, asset_sha256, source_sha256, custody_sha256, record_sha256, caption_sha256,
binding_sha256, mime, byte_length, data_base64`。

运输硬上限为原图 4MiB、JSON 6MB；实际还取当前 paid meme_input 字节/像素上限的较小值，
并保持原 PNG/JPEG/GIF、2048 尺寸、120 帧、总像素限制。4MiB 不会扩大当前 4,000,000
字节授权。Bridge 在独立 meme_asset_http 中复用受检 HTTP/private destination/最终 seal、
bounded_post，保留普通 images.py 的 64KiB 边界。对 echoed identities、租约、asset 组合、
canonical base64、实际 SHA/MIME/长度逐项验证，await 后重读 STOP/recall/新输入。
来源和 caption 的最终执行权限由 Core 的原发送检查复核，摘要不代替授权。

Bridge 原图仅在准备好的单次发送 RAM 中，原 deliver_item 外层唯一 finally 调用同步、
无 I/O 的可选 release_prepared；OwnerOneBot 仅 Core 模式清空 prepared_message。
覆盖 prepare→reserve STOP、probe、CancelledError、成功及 UNKNOWN，保留原异常和所有
持久责任；无此 hook 的适配器维持原行为。不新建原图、
缩略图、caption、向量或 query 持久副本。PinCustody 复用原 PREPARED/SENDING/UNKNOWN/
SENT/CANCELLED 转移、原 NOT_SENT 证明及晚到 ACK；重复 ACK 不改原 sent_at。
原窗口 journal、IO_STARTED、租约、fence 和实际传输 seal 不变。

真实切换先用原 STOP/锁/handoff 闭环原模型、discovery、ACK/UNKNOWN 和原 SENT cooldown，
并通过 B3。Core 失败不自动启用旧目录。已有 Core 发送后，旧快照缺少后来来源/cooldown
事实，立即回退仅限对账后的兼容 text-only 状态；旧候选重启须另审逆向对账或原 cooldown
完全到期并完成擦除。不得覆盖旧快照、放行 UNKNOWN、退款或制造新成功。

## 已知 caption 的责任读取

`POST /internal/v1/memes/captions/{job_id}/responsibility` 复用私有 Bearer 边界，
请求必须为严格空对象 `{}`，成功响应标记 `Cache-Control: no-store`。共享
`CaptionResponsibility` v1 DTO 保留原 job/event/request/asset/source/cache/authority 摘要、
原数据库 `job_state`、`body_erased`、责任类型、实际费用 owner 与原 UsageReceipt，以及
完整有界复用链、ErasureLedger、预算 journal 身份和 head 的摘要。响应不含 caption、
图片、URL、query 或 context 正文。

`USAGE` 要求唯一、相互一致的原 model_usage/admission 与费用根节点；同时按真实
job_id、admission.logical_request_id 和所有保留费用指针核对。缓存后代的空指针不会
隐藏祖先费用。`NO_ADMISSION` 只适用于目标及全部复用祖先均为 FAILED/CANCELLED，
且整条链不存在实际 U/admission；预分配 UID 本身不是费用。非终态、断链、循环、
越界、多个费用 owner 或身份/指针不一致均拒绝。

Core 在单个原 Store 事务内读取 jobs.authority_json 所指的精确原 revision，校验
AuthorityRef、DB journal 身份及该 revision 的原 journal_path。预算校验使用
`record=None, initialize=False, allow_settlement=True`，前后固定 Ledger 与 journal。
当前设置里的新路径不能替代原证据。合法 policy_redacted 通过保留摘要与原 journal
投影验证；STOP、过期、撤回或擦除不消除原费用，CANCELLED + UNKNOWN 保持原样。
若原 journal 含 OWNER_RECONCILED v2 且其原 revision 后来合法 policy_redacted，
现有 journal 校验仍要求未 redacted 的原 policy，故保守拒绝读取；该历史组合尚未验证。
旧快照缺少最新 Ledger 重放、证据缺失和备份恢复标记继续拒绝；读取不重放、不建文件、
不修复、不清标记，不进行 SQL 写入、审计、工作提交、模型调用或结算。

Bridge `CaptionHTTP.get_responsibility` 仅对已保留绑定提供同一元数据读取，使用原
native capture、authority、journal 及费用单调性校验，并在实际私有 HTTP 前后重查
冻结绑定。它不读取 caption context/outcome 正文。既有正文 getter、semantic advance、
费用写入、retirement preflight 及 pin/ACK/cooldown 路径维持原契约；consumer 接入另行评审。

专用测试使用真实私有 TCP、独立 Core/Bridge SQLite、原签名 capture 与 Mock provider，
核对读取前后完整逻辑 DB、预算 journal 和 Ledger；SQL authorizer 额外拒绝所有被测写操作
及 caption/event 正文列。仅合成备份克隆清空两个 caption 正文列，验证元数据仍绑定原
capture/U，同时旧正文 getter 拒绝。损坏图及请求/响应篡改均明确作为合成故障注入。

## 本地验证边界

已执行来源冻结/撤权/费用/epoch、同字节双来源、双连接竞争同一权威 SQLite、固定词法/GIF
语料、实际租约 API、原 catalog/pin/回执回归、Core transport 故障注入、RAM finally、
默认及历史快照 golden、包边界、当前 media 限额、实际 native poke/memory-control 签名路由，
并验证受影响的文本、语音、账号和旧表情发送入口。

真实签名 WS→API→独立 Core Worker 进程→实际 Bridge daemon→OneBot Mock→Core SENT/
Bridge ACK 验证了原图一致、单次原聊天模型选择、原 caption 费用保留和 Bridge 无业务表。
实际模型 HTTP hook 处撤回原 root，验证 provider 零调用、无 Outbox 和原费用保留。
恢复冻结投递旧备份时先用最新独立 Ledger 重放，原图及派生 offer 擦除、旧租约拒读，
费用逐行保留。合法缺可选能力继续文本，已有冻结 null/opaque 记录后撤权仍严格拒绝。

B1b 历史回执保留原 fixture/进程等待失败与修复；B2a 唯一
`.local/b2a-validation/receipt.json` 记录当前源树、逐项最新检查、已修失败及复用范围。
合成、真人及发布门禁分列；不声明真人 QQ、生产切换、付费模型、独立镜像或完整 CI
已通过。原 ACK/UNKNOWN/fence/独立遗忘账本仍由既有机制负责。

## 候选阶段基准

`scripts/evaluate_meme_retrieval.py` 读取 `tests/fixtures/meme_quality_cases.json`
（合成主人消息与合成 caption 库，不含真实聊天），用生产 query 解析与候选规则离线计算：
明确索图候选率、候选召回 @5/@8、拒绝语境出图率、否定/反讽误触发率、含不可接受候选的
offer 比例，以及 ambient 菜单是否含可接受项、是否不含不可接受项，均报告分子与分母。`--embeddings <cache_dir>` 用本地 BGE 权重加入 cosine，
并输出标注对的 cosine 分布与该库上的最佳 F1 门槛，仅作调参线索。它不测最终模型选图、
图文一致性或 QQ 送达；这些需要主人真实图库与真实模型的另行评测。
