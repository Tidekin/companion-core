# 表情入库识图与聊天检索

现有来源审核、Core HTTP v1、原生 caption job、模型预算、本地语义索引、当前轮候选和
Outbox 图片发送已完成集成。保持 MM01 来源绑定、MM02 逐帧媒体限额、冷却、UNKNOWN
和 Outbox 所有权。功能默认关闭，启用须进入已批准的主人私聊窗口并配置独立图片额度。

真实本地 WebSocket、API lifespan、独立 Core worker 和 `ModelClient` 的合成链路已通过，
覆盖第二轮从已入库素材生成候选并经原 Outbox 发送原图。外部来源、模型和 OneBot
transport 使用 Mock；该结果不证明真实视觉模型质量、真实 BGE 召回效果或真实 QQ 选发效果。

## 运行边界

`companion_meme_protocol` 是共用纯包，包含 DTO、严格 JSON、原始媒体验证和确定性采样，
无存储、权限、网络或模型密钥。Core 与 Bridge 使用同一套合同；Bridge 原有
`meme_caption.py` 保留兼容导出。wheel/sdist 与两个运行镜像均包含这个包。
`meme_semantic.py` 在已有 Bridge spool SQLite 内保存待识别状态、caption 缓存、
Core job 绑定和计量回执副本；`meme_semantic_search.py` 保存/查询本地描述向量。
Bridge 不导入 Core、不安装另一个 embedding 模型、不持模型密钥，也不直接访问模型 provider。

Core 的 `MemeCaptionService` 唯一拥有模型 admission、原生 `jobs`、原始 usage、
费用预留/结算与权限验证；`Core.run_once()` 在前台任务让出后推进 caption。
Bridge 的 `MemeRuntime` 通过 `SemanticHTTP`、`CaptionHTTP`、`CaptionEmbeddingHTTP`
访问 Core；本地 usage 是原回执副本。每次 `advance()` 只提交一次或查询一次，
由既有运行循环继续推进，不另建费用账本或 caption 调度系统。

## 开关、授权与费用

Core `meme_semantics_enabled` 和 Bridge 同名开关默认均为 `false`；Bridge 还需启用
`meme_enabled`。从外部补充素材沿用 `meme_discovery_enabled`、可信来源规则及下载/存储额度。
运行范围是 `mode=live`、有效 paid approval 和 `owner_private_v1` 私聊窗口；
启用字段会进入配置绑定，必须按现有窗口批准流程固定配置。

真正识图另需 `paid_model.meme_input`，类型为 `ImageInputPolicy`，以及非零
`meme_caption_daily_micro_usd`（默认 `0`）。policy 必须明确批准每图估算 token、字节、
像素和请求张数，`max_images_per_request` 只能为 1–5，不能为 `unlimited`。
GIF 最多生成 5 张样本，需相应配置样本数；超出实际授权数时直接拒绝。
`meme_caption_max_output_tokens` 默认 2000，范围 128–4096，且不得超过 paid 总输出上限。

`meme_caption` 使用当前主 provider、model、key 和费率，返回 JSON；无单独裸 key 路由。
它使用独立 `meme_input`，不继承 `content_vision` 或主人上传图片的授权。
`meme_caption` job 只能调用该场景，最多建立一次 `model_usage`，并使用原
`model_admissions`、请求/单次/日/月/窗口额度、并发限制、journal 与 STOP 检查。
独立 caption 日额度即使在 `unlimited_period_spend` 窗口也生效；实际费用超过冻结额度
仍保守记为 UNKNOWN 并阻止继续消费。图片按批准的固定估算加文本估算预留，实际 usage
再结算；估算不代表 provider 的 token 或账单保证。

新增默认字段和空 `meme_input` 不改变旧授权快照；仅配置 policy 时，paid snapshot 才
增加该场景日额度与图片估算标识。已有 chat、content_vision、video 的授权边界保持原合同。

## 认证 HTTP v1 接口

以下六个接口均为 `POST /internal/v1/memes/...`，使用原 Bridge Bearer token 认证，
不建立额外 HTTP 服务。除状态回执读取外，操作会校验当前私聊窗口与来源；状态接口保留
停止后读取原费用回执的能力，不因此恢复模型执行权。

| 路径后缀 | 请求与返回 |
| --- | --- |
| `/semantics/profile` | 空对象；返回 Core 权威 caption identity 和 embedding spec。 |
| `/captions` | 原图 base64、hash/MIME/长度、来源 proof、context、identity；返回幂等原生 job 回执。 |
| `/captions/{job_id}` | 空对象；返回既有 caption job 状态、结果与原 usage 副本。 |
| `/embeddings` | caption job ID、当前 event/revision；Core 自行读取成功 caption，返回本地向量。 |
| `/query` | 已接纳的 event/revision；返回当前 chat job、查询摘要、有界语境和可选本地向量。 |
| `/offers` | chat job、event/revision、查询摘要与候选或 `null`；返回不可变 offer 接纳回执。 |

原文件仍受共用媒体验证的 4 MiB 上限。启用 semantics 时，仅 caption 提交接口放宽
请求体到 6 MiB，容纳原图 base64、受限 proof 与 JSON；其他路由保持各自限额。
embedding 接口不接受任意调用方文本；Core 使用持久化成功 caption 生成索引输入。

## 从来源审核到可检索

在来源/许可审核和 MM01/MM02 文件检查完成后创建 `SemanticLibrary(catalog, identity, spec)`。
它只为既有合格素材建立语义门，并使随后 `catalog.add()` 的新素材进入 `PENDING`。
此时 `run_discovery` 把素材列在 `pending_caption` 中，不把尚未识别的图算成 `accepted`。
普通合格图在 caption 通过后自动成为 `READY`，无需逐张询问主人。

空目录的后台发现不依赖可选的前台候选查询成功。查询超时或未能冻结 offer 时，
MemePump 等原回复完成后，从同一条已接纳且摘要匹配的主人输入提取已有七类意图，
通过原 discovery submit 接口补充素材；不补写本轮 offer，也不改变已生成的回复。
无意图、严肃/拒绝图片的输入以及已有活跃素材的目录不会走此空库补充路径。
新输入、撤回、STOP、版本、来源审核和预算继续由原检查拒绝；素材可用于后续轮次。

启用 semantics 后，`TrustedReviewer` 先做来源审核，允许合格但尚无 caption 的素材暂存；
许可文件、固定版本、实际 acquisition/hash 检查保持。caption 的置信度或内容判断
永远不能批准来源。下载费用必须已结算，下载结果 UNKNOWN 不会继续发起付费识图。

社区来源在配置本地可信规则后，按规则固定的仓库和 40 位 revision 获取 manifest、
实际许可文件、类别 contents listing 和最多 3 张候选原图。所有请求沿用
`PublicFetcher` 和原 12 请求、16 MiB 下载、4 MiB 新增素材预算；文件限额及审核细节见
[素材审核与来源证据](next-memes.md#素材审核与来源证据)。此路径不读取上游
`semantic_metadata.json`，类别说明只用于发现；图片描述和标签由 Core caption 产生。

原 catalog 的类别/标签挑图路径跳过所有已托管素材，即使它是启用语义门之前创建的实例。
语义候选、`prepare_selection` 和 `mark_sending` 均检查当前来源、模型版本、成功的 Core
绑定和冻结候选证据。`PENDING/QUARANTINED/INVALID` 素材不能选发，找不到就继续文字。

## Caption 请求、回执和缓存

`MemeRuntime` 先从 profile 接口取得权威版本，再创建 `SemanticLibrary`。
`CaptionHTTP.submit(CaptionRequest)` 与 `CaptionHTTP.get(job_id)` 是识图适配器，
只连接上述 Core HTTP v1。Core 用原始 bytes 复核 hash、格式与限额，持久化原生 job；
独立 worker 按原工作流预留预算并处理。`CaptionRequest` 包含：

- 原始 `data/asset_sha256/mime/byte_length` 与 `source_evidence_sha256`。
- `event_id/reply_revision/discovery_job_id/attempt_key`，只用于授权与幂等，不进入模型提示。
- Core 配置权威提供的 `model_version/prompt_version`。
- 由上述内容、提示词摘要和结果 schema 摘要生成的 `request.sha256`。

Core 同时要求 parent 为当前窗口、同一 event/revision 的成功 `meme_discovery` job，
其下载 reservation 已 `SETTLED` 且 `attempt_key` 一致。调用方不能用新传入的金额、
usage 或描述替代原 admission。worker 先持久绑定预分配的 usage request ID，再 reserve，
从而在预留后、dispatch 前崩溃时仍能找回原费用回执。

来源哈希使用固定对象：`schema_version:1`、`source_page`、`asset_url`、`license`、
`usage_basis`、`fetched_at`、`revision`、`acquisition`（解析后的对象）。时间戳保留来源完成
回执原值；不包含 caption/tags。规范 JSON 使用 UTF-8、`ensure_ascii=False`、排序键、
紧凑分隔符、`allow_nan=False`，拒绝重复键。不得对任意原 JSON 字符串直接做来源指纹。

按文件导入的 `acquisition.fetch` 是实际原图凭证，`archive_member=""`；
`acquisition.source_files` 是严格三键对象，`manifest/license/listing` 各映射到
`FetchEvidence={requested_url,final_url,hops,body_sha256,fetched_at}`。Bridge 核验实际
manifest、许可文件及 listing 字节，并将 listing 的图片路径、长度和 Git blob SHA
绑定到原图。Core 校验四份获取凭证的仓库、固定 revision、预期 URL、单跳 200 和
无重定向关系，核对原图 SHA-256，以及许可摘要与 `usage_basis` 中许可见证的一致性。
本地可信规则和实际文件审核由 Bridge 负责。旧获取路径完全省略 `source_files`，
不能补 `null` 或空对象，以保持原来源指纹及缓存身份。

`CaptionOutcome` 的唯一合法状态为 `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/UNKNOWN`。
它必须绑定 request/asset/source/model/prompt 和 job ID；仅 `SUCCEEDED` 携带结构化结果。
所有字段有长度、枚举和有限数值检查：description/visible_text 各最多 1000 字符，
tags 最多 16×32 字符，suitable/avoid 最多 8 个已知意图，use_when/avoid_when
最多 8×80 字符，tone/target 有固定词表。未知梗、人名、出处不得臆造。

`content_status` 标记清楚、不确定或文字/画面冲突；`temporal_status` 标记静态、
采样一致、含义变化或不确定。默认置信度低于 0.8、冲突、含义反转、对象不明等情况
进入 `QUARANTINED`。明显侮辱文字不会因模型给了“温暖”标签而通过。

`usage` 独立于 caption 内容状态，来自 Core 原 admission：

- 尚未实际 admission 的 `QUEUED/FAILED/CANCELLED` 可以是 `null`。
- `RUNNING/SUCCEEDED/UNKNOWN` 必须携带真实 `usage_request_id` 与回执。
- 已有回执后不能改成 `null`，不能换 request ID、降级已确认费用或丢失已知 token 数。
- `SETTLED` 才有确定的 `billed_micro_usd`；`RESERVED/UNKNOWN` 的未知费用保持 `null`，
  已知 token 数仍保留。语义与费用分别校验；可读的语义结果不能把未知费用改为已结算，
  Core 仍保守保留预留金额，不退款。
- 格式错误、撤回、来源失效或 STOP 后收到的真实 usage 仍先记入本地副本；内容不进入索引。

缓存主键为**原文件 hash + caption 模型/提示词版本 + 提示词/schema 摘要**。
成功/低置信的同版本结果不会自动重复识别；RUNNING 只查 job，UNKNOWN 和提交结果不明
不会重新 submit。`accept(request, outcome, ...)` 接受 Core 对同请求的恢复/结算结果，
本身不调用模型。Core 按 request/attempt 幂等保存 job 和原 usage；超期 RUNNING job
恢复为失败/UNKNOWN，不建立第二次模型调用。UNKNOWN caption 不能作为成功候选入库。

同一图片换了新的合格来源，可由已完成来源审核的调用方使用 `rebind_source()`：
旧候选/索引立即失效，再请求 Core 复核当前来源 proof。Core 返回 `cache_hit:true`、
当前 request/source proof 和原 usage，不因来源时间戳变化再次付费。没有当前来源成功绑定，
旧 caption 缓存不能使图片重新可用。移除的素材不能通过 rebind/重新发现复活。

## GIF 采样与可见范围

`sample_frames(original_bytes, library)` 提供可复用的确定性参考算法：先执行所有 MM02
验证，按实际帧时长选首帧、尾帧及均匀时间位置，最多 5 帧，生成最长边 768 的 PNG。
返回每个 sample 的帧号、相对时间、SHA-256，以及总帧数、时长和 `complete/sampled` 覆盖。
没有显式时长的 GIF 使用 100ms；非法/零/超限时长拒绝。原始文件和其 hash 始终保留。

Core 从原图重算这些信息并附加到 model 输出；模型若自行返回 `sampled_frames` 会被拒绝。
`caption_model_schema()` 只包含模型可填写的语义字段，不含 `sampled_frames`；
`CaptionResult` 则是 Core 附加采样证明后的完整结果，两者共同绑定到 schema 摘要。
Bridge 接受成功 caption 时也用原图复核采样证据。超过 5 帧时只能描述已提供采样的含义；
未采样的瞬时变化仍是实际视觉效果验收的限制，不能声称整段动图已完整理解。

## 增量索引与当前语境

`await semantics.index(sha, embedder, check=..., commit_check=...)` 只对新的/变更的有效
caption 调用 `embedder.embed(texts)`；已有匹配索引不会再次调用。
纯 embedding 合同支持最多 8 条、每条最多 2000 字符；生产适配器每次按一个成功
caption job 请求，接口结果为
`{model_version, dimensions, vectors, local:true}`。当前 Core 中文 BGE 固定版本为 512 维；
模块允许显式 `EmbeddingSpec`，但每次都检查版本、维度、有限非零向量和本地标记。
更换 embedding 版本只重建相关向量，不重新调用视觉模型。

Core 复用固定版本 `BAAI/bge-small-zh-v1.5`（512 维）的现有本地 encoder，以
`local_only=True` 从 `embedding_cache_dir` 加载。caption 和 query embedding 都移到
线程执行，避免阻塞接纳输入；此路径不会自动下载模型、访问远程 embedding 服务或生成
新付费 usage。部署须事先准备完整固定版本权重；缺失时 query 返回无向量，检索只使用
已经验证的 caption 文本。索引失败不把未经确认的向量当作成功索引。

聊天查询由 Core 在主人输入已接纳后，使用当前 event/revision 生成有界语境及本地 query
向量，再交给 Bridge。Core 取当前主人文本的最后 2000 字符，保守判断意图/对象，
严肃话题或没有适用意图时不提供图片候选。Bridge 不修改旧签名入站 offer：

```python
offers = semantics.candidates(
    "meme:" + event_id,
    {"text": bounded_context, "intent": "comfort", "target": "recipient"},
    query_vector=core_local_vector,
    limit=8,
    check=current_event_guard,
)
```

持续私聊循环在 inbox 已接纳后启动一次查询，不等待该轮 ACK；该任务最长 1.5 秒。
Core 对普通文字 chat job 从创建起最多等待 2 秒，以取得独立 job-bound offer；
控制命令、日记命令和空文本不等待。明确的 `null` offer 也会释放等待。
迟到、网络失败或没有候选时继续文字回复，不阻塞下一条主人输入或发送队列。
新输入、撤回和 STOP 会取消旧查询；已开始的查询不因进程重开而重复提交。

仅有 `READY` caption 能参与检索。先按适用/避用意图、语气和说话对象过滤，再结合文字
匹配与有效本地向量排序；无向量时只回退到已验证的 caption 文本。安慰不会选嘲讽/嘲笑图；
`self` 指表情说话者对自己的自嘲，不能替换成对接收者的嘲讽。无匹配、解析失败或过期索引
不会随机补发。私聊 query 文本/向量不保存到本地图库，仅保存冻结查询摘要。

最多 8 个候选沿用 `candidate_id/description/tags/category` 四字段；描述附上可见文字、
语气和对象，全部作为不可信数据交给原聊天模型选一个或 NONE。冻结集合在后续重试中不会
扩展；原图 hash、caption/source/profile 快照在准备和发送前再次核对。发送始终走原 Outbox。

## 失效、撤回与恢复

删除、来源失效或字节变化会使旧 caption/index/冻结候选不可用；`invalidate(sha)`
立即封禁并移除向量，`catalog.remove(sha)` 同时清除原图。换模型/提示词版本不使用旧语义，
换索引版本不混用旧向量。每次 await 后及事务提交前检查来源、STOP、撤回和当前配置。
Core 的 `erase_captions` 已接入既有 ErasureLedger/撤回重放：清除被撤回源的原图、
caption、context/proof/request 内容，取消 caption job，并沿 `reuse_job_id` 递归清除
缓存复用后代。offer 接纳时将被选候选的完整复用祖先事件加入 chat `job_sources`，
使祖先撤回也能阻止后代候选在准备、模型 dispatch 或发送时继续使用。
费用记录和已收到的 usage 保留，不把 erasure 当成退款依据。

公开素材下载使用现有 8 KiB 读取块复核 Core 权限和租约；逐个实际网络碎片仍立即累计
下载字节并检查上限，最后的短块及 EOF 也复核后才能签发获取证据。网络中断、取消和
撤回继续保留已观察流量及原 UNKNOWN 责任。这样 TCP/TLS 碎片数量不会放大远端授权
往返、耗尽同一请求的有限下载时限。流式读取与关闭沿用
[HTTPX async streaming](https://www.python-httpx.org/async/#streaming-responses)。

Meme 私有 HTTP 请求复用现有 `_media_request` 的最终请求边界完成一次当前授权，
返回后仍复核同一 authority 与已确认的过期时间；不跨请求缓存授权。可选检索与后台
采集失败通过既有 metrics 保留固定错误类别和 HTTP 状态，不记录异常原文、私聊或凭据。

Bridge 在收到认证撤回回执后取消该轮候选/发送 pin；查询和来源任务在 await 后及提交前
复查本地撤回、当前输入和窗口状态。图库中的缓存本身不是新的授权，重新绑定仍须由 Core
复核当前来源与其祖先。主动删除素材调用 `catalog.remove()`，同时清除原图和向量。

Core 恢复备份时先重放最新独立 erasure ledger，将原 `RESERVED` 模型费用及发现预留
标为 UNKNOWN，取消未完成的 `meme_discovery/meme_caption` jobs，并按现有规则标记
预算恢复待核对。待发送 Outbox 进入不确定状态；旧快照不能恢复原窗口、lease 或发送权。
Bridge 保留 UNKNOWN/提交结果不明绑定，重开时不重复 submit；READY 标签不能绕过
当前来源、版本、Core 成功绑定与发送检查。未完成的付费工作须按原核对流程处理。

## 容量与退化行为

原图沿用现有资产数量与字节上限；caption 缓存最多 `4 * max_assets` 条，job/source
绑定与候选沿用 `max_bindings`。不要为腾位置静默删掉 UNKNOWN 或实际计量回执。
`MemeRuntime` 在操作和同步 commit guard 中执行原 spool 整体磁盘容量检查，包含
SQLite/WAL 和新增元数据。Core caption 存储另限制最多 2048 行和 64 MiB 原图总量。
达到限额时拒绝新工作，不静默删除 UNKNOWN 或费用回执；当前文字回复继续。

## 固定样本结果与验证

测试中的图像/描述均为确定性夹具，caption client 模拟 Core 的异步状态、缓存、采样和 usage，
embedding 使用显式 512 维 Mock 向量。以下结果是**标注语义进入检索后的质量与约束结果**，
不是视觉识别准确率；这些语义质量样本未调用真实视觉模型或真实 BGE 推理。

| 固定案例 | 期望结果 |
| --- | --- |
| 难过、想要抱抱 | 只返回温暖安慰图 |
| 安慰中出现“嘲笑/搞砸工作”字样 | 排除高词汇匹配的嘲讽图，保留安慰图 |
| 说话者对自己的工作失误自嘲 | 返回 self 图，排除 recipient 嘲讽图 |
| 未指定自嘲对象 | 不把自嘲图强行用于接收者 |
| 和善画面描述但文字为“你真笨活该” | 隔离，不选发 |
| GIF 后帧把抱抱改为嘲笑 | `changes` 隔离，不只信首帧 |
| 完全不相关的天气查询 | 空候选 |
| 无字词重合的同义语境 | 有效 Mock 向量补充召回，视觉调用次数不增加 |

相关回归还覆盖缓存/进程重开、模型/提示/索引版本切换、来源重绑缓存复用、删除后旧实例
准备/发送围栏、await 期间撤回/STOP、UNKNOWN/null usage、错误身份与采样证据、向量异常、
最多 8 个冻结候选、费用回执恢复及无重复 submit。

集成回归位于 [test_meme_semantic_integration.py](../../tests/test_meme_semantic_integration.py)：
认证 WebSocket 入站经过真实 API lifespan、独立数据库连接的 Core worker、真实
`ModelClient` admission/settlement 和原 Outbox；第一轮无素材时文字回复，后台发现、
caption 和索引后第二轮通过当前语境选中并发送原始图片，第三轮不适用时继续文字。
该合成测试使用外部 Mock transport 和 Mock embedding，不调用真实视觉模型或 QQ。

合同与故障回归分别见 [Core service](../../tests/test_meme_core.py)、
[caption admission](../../tests/test_meme_caption_admission.py)、
[Bridge runtime](../../tests/test_meme_semantic_runtime.py)。最终候选的检查与 CI 以集成
验证回执为准；本文不复制临时窗口身份或把固定样本结果计作线上质量验收。

参考 [Meme Manager 固定 caption 实现](https://github.com/anka-afk/astrbot_plugin_meme_manager/blob/88b8a1ba12a8969cab70b31e6cdc258f61620fcd/backend/semantic/caption.py)
的入库描述/少量 GIF 帧思路，以及[固定 query 实现](https://github.com/anka-afk/astrbot_plugin_meme_manager/blob/88b8a1ba12a8969cab70b31e6cdc258f61620fcd/backend/semantic/query.py)
的版本化索引与本轮候选 ID 校验。独立编写、未安装 AstrBot、未复制其消息系统。
