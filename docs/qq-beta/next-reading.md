# 第二阶段：独立阅读候选与集成接口

状态：v3 HTTP worker、Core 租约与证据提交已接通，并通过真实 inbox HTTP 与独立
Reader CLI 的合成验收。生产启用与真实 QQ 验收由部署方统一执行；最终完整 CI 以
集成候选的唯一验证回执为准。

## 已实现的边界

`source_reader.reading.read_observation` 封装已有 BilibiliReader、HeyboxReader 和新建的
WebReader。默认 legacy worker 继续使用 `/internal/v2/readers`；`reading-v1` worker
使用 `/internal/v3/readers` 的 lease、validate、result。两个合同分别验证，旧指纹保持不变。

| 来源 | 实际输出 | 覆盖边界 |
| --- | --- | --- |
| B站 | 复用公开/已授权 API、短链解析、固定元数据和字幕读取 | L1 只有标题/简介；L2 有第 1 分 P 的时间区间与原始字幕响应哈希；空档、截断、登录限制分别保留 |
| 小黑盒 | 复用固定适配器与正文解析，保留文字和图片引用的顺序及字符位置 | 图片引用不代表看过图片；上游单列的图片仍附在正文之后，未知原位置不补造 |
| 通用网页 | WebHTTP 受控 GET，再交给 Trafilatura 纯 HTML 解析 | 只说明提取到的页面文字；无正文、失效页面、限制访问、解析器缺失分别失败 |
| JS 页面 | 复用 BrowserRenderer 的离线 Chromium 会话，解析内联脚本生成的 DOM | 外部脚本、网络请求、WebSocket、iframe 和新窗口仍受隔离；依赖外部加载的 SPA 可能没有正文 |

### 评论与弹幕样本

| 来源 | 新增读取范围 | 模型上下文中的覆盖说明 |
| --- | --- | --- |
| B站 | 显式启用后先读取现有第 1 分 P 字幕，再在同一 Reader 请求/字节预算内读取评论和当前弹幕池的有界样本 | 评论、弹幕与正文/字幕分开；标明实际可用状态与 `partial`，弹幕范围为 `current_pool_sample` |
| 小黑盒 | 保留同一次 tree 响应已经返回的有界评论、楼层和楼中楼回复关系 | 评论与帖子正文分开；保留评论 ID、回复关系及截断信息，上游总数不等于本次读到的条数 |

B站补充读取默认关闭，仅 B站来源可用 CLI `--discussion` 显式开启；
`legacy` 和 `reading-v1` 均支持。字幕优先，随后评论和弹幕各至多发起一次补充请求，
共享原 `ReadBudget`；预算不足标为 `budget_limited`，不扩大原请求或字节额度。
小黑盒不为补齐评论另行抓取；两者均不自动翻页读取全部讨论。

评论中的图片数量只是未分析图片引用，不代表看过图片。当前弹幕池样本不等于完整视频
的全部弹幕或历史弹幕。评论和弹幕继续作为 `untrusted_external` 资料，不能生成权限或
执行指令；既有账号授权、来源撤回、派生清除、预算预留和 UNKNOWN 计量仍然生效。
这次增量的验证单独记录在集成回执中；下述既有字幕回执不构成评论或弹幕的真实验收。

B站结果始终是 `DISCOVERED` 或 `FETCHED`。本模块不会产生 `WATCHED`，也不产生视频
片段/音频分析证据。片段分析由部署方的视频模块负责，需按其真实时间范围另行记录。

既有 [2026-10-19 真实验证](../design-v0.6/source-reader-validation.md) 的 10 个公开视频
样本为 9 个 L1 和 1 个失败。随后部署方 2026-10-23 的独立账号探测回执证明一个实际目标
取得了 L2：49 段 `ai-zh` 字幕、3 次请求、13,548 字节，账号授权已登记；该回执仍将
生产端目标重读标为未验证。本轮仅复用此证据并模拟 49 段兼容验收，没有重做登录或真实读取。

## Reader 输出和取消合同

调用形态：

```python
result = await read_observation(
    reader, leased_item,
    ReadingContext(root_task_id, source, tuple(source_event_ids), privacy_epoch),
    validate_read, cancel_event=cancel_event,
)
```

`ReadingContext` 必须由可信调度器从已认证请求取得。`validate_read(context)` 异步检查
当前许可、lease、STOP、原输入撤回、privacy epoch 和根任务有效性；成功返回 `None`，
拒绝抛出有固定错误码的 `ReadFailure`。回调返回 `False` 等其他值同样拒绝。
账号授权快照另与 lease 中的 `source_grant` 比对，保留原有读取授权边界。

检查发生在开始读取前、每次真正发送 HTTP 头之前（包括 DNS/TLS 等待后）、读取返回后
以及证据封装完成后。读取中设置 `asyncio.Event` 可中止等待中的网络或浏览器任务；取消
会等待子任务清理，返回的 `observations` 为空。源码适配器的可选字幕降级不会吞掉撤回。
上层直接取消 Python task 时仍传播 `CancelledError`，Core 保留未收到回执的预算预留。

v3 worker 每 0.75 秒发起一次后台校验，接受的间隔范围为 0.5–1 秒，单次校验总时限最多
2 秒，并受 lease 截止与 20 秒读取上限约束。检测到撤回、STOP、授权变化或校验失联时
终止当前读取，保留错误码和观察到的用量。已启动请求后的取消不把预算改为零。
结果回执超时不会在本地重发公网请求，恢复和结算仍由 Core 负责。

## v3 HTTP 与 CLI

lease 返回 `{"item": null}` 或包含以下字段的 `item`：`schema_version=1`、`request_id`、
`lease_token`、`lease_until`、`operation`、`url`、`max_bytes`、`max_requests`、`max_items`、
`context`、`allowed_hosts` 和可选 `source_grant`。Reader 严格校验字段、范围、来源与
时区，并把 JSON 来源事件数组转为不可变 ReadingContext。校验请求只携带 lease token
及可选授权快照，响应必须精确等于 `{"valid": true}`。

result 是本节所述 `read_observation` 结果平铺后附加 `lease_token` 和可选 `source_grant`，
使用共享 DTO `ReadObservation`；单项由 `ObservationEvidence` 验证。根任务、来源事件、
epoch、允许主机均来自 Core 租约，最终提交事务仍须重新核验。

```powershell
python -m source_reader --protocol reading-v1 --source web `
  --core-url <private-origin> --adapter-id <web-reader-id> --once

python -m source_reader --protocol reading-v1 --source bilibili `
  --account-file <private-grant.json> --core-url <private-origin> --adapter-id <bilibili-id>
```

`--protocol` 默认为 `legacy`。B站 `--account-file` 继续支持原生 Reader，无需额外启用
关键词工具模式。`web` 必须显式选择 `reading-v1`，不允许 `--probe` 自行授权 URL。
可重复传入 `--allow-host` 作为本地收窄列表；实际主机范围只取它与 Core 租约的交集，
省略时使用租约范围。`--browser` 可用于 B站及 `reading-v1` 网页的离线渲染。

| 封装字段 | 语义 |
| --- | --- |
| `schema_version` | 当前隔离合同版本 1；独立于 Core Observation v2 |
| `status` | `SUCCESS`、`FAILED`、`UNKNOWN` 或 `CANCELLED`；失败结果没有正文 |
| `observations[].original_url / url` | 原请求 URL 与实际结果 URL；重定向不丢原来源 |
| `fetched_at / content_sha256 / parser_version` | 获取时间、实际输出文本 UTF-8 SHA-256、固定解析器版本 |
| `quotes` | 最多 8 段引用，每段最多 500 字符；`start/end` 是输出文本的 Python 字符偏移 |
| `coverage / evidence` | 明确读取范围、截断、字幕区间、原始响应哈希和既有图片位置；附加评论/弹幕样本分别保留可用状态及部分覆盖说明 |
| `provenance` | 根任务、来源类型、来源事件、privacy epoch、可选授权快照 |
| `trust` | 固定 `untrusted_external`；正文不能产生权限、系统指令或模型工具调用 |
| `used_requests / used_bytes` | 可结算的已知用量；UNKNOWN 或已启动请求后的取消返回 `null` |
| `observed_requests / observed_bytes` | 本地已预留请求次数与已观察字节；不证明请求已发送或完整结束 |

同 URL 的正文更新会产生不同内容哈希；重启不会生成新的来源关系或扩大读取范围。
哈希和引用用于溯源，不构成正文可信或执行获准的证明。

## 网络和解析器

`WebHTTP(allowed_hosts)` 要求可信调用者传入明确、非空的精确主机集合，禁止从正文或
模型结果扩充。仅允许 HTTPS 443；每一跳都重新检查主机范围，并复用 PublicNetwork 的
全 DNS 答案检查、数字 IP 固定连接与原主机 TLS 校验。没有 Cookie、共享环境代理或重试。
最多 3 次重定向；重定向和最终响应共用请求、字节限额，头部计入额度；压缩和非 HTML
响应拒绝。读取限制最多 8 次请求、1 MiB 和 20 秒异步超时；正文输出最多 11,500 字符。

解析器仅调用 [Trafilatura 的 extract](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)
处理已取得的字节，关闭评论和元数据提取，不调用 fetch/crawler。可选依赖固定为
`trafilatura==2.3.1`，见 `source_reader/requirements-web.txt`。缺依赖返回
`READER_PARSER_UNAVAILABLE`，版本不符返回 `READER_PARSER_VERSION_UNSUPPORTED`。
集成方已将相同版本装配到 `dev`、`reader` 依赖组和锁文件；Reader 镜像从带哈希的
reader 组安装。本轮使用阶段二独立 `.venv`，未安装到阶段一或根共享环境。

纯解析在有界输入上同步执行；取消能阻止结果返回，但不能抢占正在执行的 Python 解析。
若后续需要严格 CPU/内存上限，应继续使用独立 Reader 进程的宿主资源限制。离线浏览器
DOM 输出另有 1 MiB 上限，超时和取消都关闭 context，后续任务可以继续。

## Core 接线与启用

`companion.reading.ReadingService` 复用 Exploration 的请求队列、请求/字节预算及目标
任务。迁移 `0024_reading_attempts` 保存根任务、原输入来源、epoch、租约、允许主机和
授权快照。直接回复的阅读随原输入版本失效；已接受目标的继续执行不因普通新消息而取消，
但原来源撤回、授权失效、STOP 或恢复备份仍撤销读取权限。
旧直接阅读在排队期间被新输入取代时，会在预留资源前持久取消，继续领取后续有效请求。

提交事务重新检查上述边界及 URL、时间、来源、内容哈希、引用区间、覆盖范围和预留。
通过后原子写入已有 Observation 与 event_dependencies。聊天上下文引用实际 Observation
ID，并携带覆盖范围、解析器、时间、哈希和有界原文引用。来源撤回或忘记账号授权会清除
派生证据和阅读元数据；恢复旧备份会回放同一清理。晚到回执只可结算已知用量或保留
UNKNOWN 预留，不可恢复正文；两版 API 不能互换租约或回执。
预算准入与状态查询按已知结算量或未结算预留量、以及已观察量中的较大值计量；响应超限
仍保留真实观察字节，不因 `used_bytes=null` 而释放已消耗容量。撤回和旧备份回放也保留该计量。

网页读取默认关闭。Core 需要 `web_reading_enabled=true`、已启用的内容/Observation
能力和非零请求/字节限额；对应 adapter 配置 `source="web"` 和独立 token，主人配置
`web_reading_hosts=["articles.example"]`（可在控制台的网页阅读里填写，保存为新配置版本）。
适配器配置不再接受 `allowed_hosts`。`web_reading_hosts=["*"]` 显式允许全部公网域名；
该值必须单独使用，不能和域名混用，也不支持部分域名通配符。普通清单仍为小写精确 DNS 名。
两种范围都拒绝 IP、单标签或私有后缀；Reader 对每次 DNS 解析结果做公网检查并固定连接地址。
HTTPS、每一跳重定向校验、读取租约复核与请求/字节限额始终生效。租约冻结原范围；关闭开关
或从全部域名收紧为精确清单会使原宽范围租约失效。现有 `allowed_hosts` 租约字段和数据库
快照保存这个显式范围；仅 web Reader 接受独立的 `*`，不扩展其他账号来源的读取权限。

部署配置提供显式 `web-reader` Compose profile，凭据来自私有 `privacy/web-reader.env`，
该文件只设置对应 `COMPANION_READER_TOKEN`。Reader 容器使用固定依赖、非 root、只读
文件系统和资源上限；镜像 smoke 会离线执行真实网页解析。受限 B站字幕沿用已有有效
只读账号授权及安全账号文件；小黑盒沿用已配置的固定 native 或 two-GET 来源。

## 相关验证与回执

新增测试覆盖 L1/L2/部分字幕、标题发现、图片顺序、同 URL 更新、引用与哈希、前置/迟到
撤回、DNS 等待后撤回、下载中取消、UNKNOWN、账号快照、恶意正文、公开 GET 范围、
重定向/私网/字节/请求限额、登录与无正文、解析器缺失和真实纯解析。独立子进程模拟
provider 并拒绝导入 Core/存储模块；冷启动重放保持相同来源与正文哈希。

新增 worker 验收通过真实 CLI 子进程和本地 HTTP 模拟 Core，外部 provider transport
保持模拟。覆盖严格 lease 解码、精确主机交集、49 段账号字幕/授权快照、校验期间到期、
下载期间撤回/STOP/校验失联、回执失联不重读、无标题长 URL 的 300 字符标题上限。
`test_reading_integration.py` 另通过真实 inbox HTTP、第二个 Core 实例和独立 Reader CLI
验证原生入队、授权租约、证据提交与重启不重读，只有公网 provider transport 被模拟。
`test_reading_service.py` 和 `test_reading_migration.py` 覆盖真实提交、晚到结算、直接输入
失效与已接受目标继续、来源授权遗忘、空来源闭包、迁移及旧备份回放。

真实 Chromium 检查包含内联 DOM、外部访问阻断、超时/取消清理和随后恢复；既有 B站
浏览器测试继续覆盖 WebRTC 隔离。既有 Reader/小黑盒/来源授权测试验证 legacy HTTP
往返与旧备份遗忘回放；新网页合同由上述 Core 集成测试覆盖。

最终精确候选、完整 CI summary 与部署状态统一记录在集成负责人的验证回执中；模块
状态文件只引用该回执，不作为生产接受证据。
