# 部署与运行

排查主人消息时，从 OneBot Bridge 的接收凭证跟到 Core 的事件、聊天任务与投递回执。服务关系见[当前运行拓扑](architecture.md)。

## 性能观测的容量与保留规则

发布前按实际挂载和镜像引用核对容量。可用空间须覆盖本次镜像归档下载及加载、必要备份、
数据库迁移或恢复副本、WAL 增长和失败收尾；不能仅用一个剩余百分比放行发布。

容器运行日志应配置大小及份数上限。例如在实际部署 Compose 覆盖文件中为受管服务配置
`logging.driver: json-file`、`logging.options.max-size: "10m"` 和
`logging.options.max-file: "3"`，或采用默认轮转的 `local` 驱动。部署时检查最终展开的
Compose 和容器 `HostConfig.LogConfig`；已有容器需按原发布流程重建后才应用新日志设置。
完整锁计时只用于有界观测窗口，结束后恢复默认慢事务日志。

保留当前运行镜像及回退回执明确引用的镜像。已加载的运行归档在内容身份核验和回执保存后，
可按发布保留清单移除额外副本；发布目录只清理确认未被运行容器挂载、当前或回退计划引用
的版本。核验必须使用真实镜像 ID、挂载和回执，不能按目录日期直接删除。

媒体文件继续由原状态、来源、期限和上传保护决定清理。数据库、独立预算/遗忘账本、
ACK 和 UNKNOWN 责任不作为主机垃圾清理对象。此文不更改生产主机或自动执行删除。

依据：[Docker 日志驱动与轮转](https://docs.docker.com/engine/logging/configure/)。

## 先准备

需要 Linux VPS、Docker Compose、可用的 OneBot v11 连接、主人 QQ、机器人 QQ，以及模型 API Key。代码默认单主人、私聊、文字消息。不要将数据库放在 NFS、SMB 或 Tailscale 文件共享目录。

旧宿主兼容性核查对象为 AstrBot 4.29.1，源码提交 `e5e21b71aec5642f110eb115eb8e9a0d8028b7e5`。当前主人消息统一使用原生 OneBot Bridge；AstrBot 插件仅用于旧队列与投递回执核对。固定已验收的协议后端版本，避免自动升级。

2026-10-19 的运行版本记录为 AstrBot 4.29.1 / NapCat 4.18.38；镜像固定方式和 NapCat 构建步骤见 [运行版本说明](../deploy/README.md)。

## 加载已验证的 CI 镜像

CI 为 Core、OneBot Bridge、Source Reader、Console UI 保留已通过容器检查的原始 Docker 归档。
在源码仓库中使用已有 `gh` 只读连接，指定成功的主分支或 full PR workflow run：

```bash
python -B scripts/ci_container_artifacts.py consume --repository Tidekin/companion-core --run-id <successful-run-id> --destination .local/verified-images
```

消费者核对原始 full run 的全部十五项成功 job（含 Flutter 客户端）、源码 tree、attempt、制品身份和 SHA256，
然后下载、`docker image load` 并核对实际 image ID 与 `linux/amd64` 平台。
主分支复用回执指向原 full run 的原制品；证据缺失、过期或不一致会直接失败，不回退重建。
`consumed-images.json` 保存实际 image ID、下载及加载耗时。可加 `--previous-run`
比较两次已验证镜像的真实 ID，源码标签不用于判定组件相同。

公开仓库的这些制品标记为 `PUBLIC_SOURCE_RUNTIME_ARTIFACT`，只包含公共源码和锁定依赖。
本轮没有推送 registry，因此 `config_digest` 是 Docker 配置 ID，不能填作 registry
manifest digest。私密配置、数据库和密钥继续由现有部署机制提供。
UI 也加载本次已验证的原归档；运行时不重新构建。其依赖固定在官方 Nginx 基础镜像 digest，
仅复制四项公共静态资源和公开 Nginx 配置。
详细证据与保留期限见 [CI 镜像制品](ci.md#retained-public-runtime-images)。

该命令仅准备本机镜像，不启动、替换或迁移服务。正式激活仍按现有停写、授权、
备份、迁移和交接步骤执行，并保留费用及投递结果不明记录。开发环境继续沿用
[隔离开发 Compose](../deploy/README.md) 的源码挂载流程。

## 发布预检

镜像加载后、停写前，在部署主机上运行只读预检：

```bash
python3 scripts/release_plan.py --project companion-main \
  --target core=<镜像ID> --target bridge=<镜像ID> \
  --probe-config <隔离副本.toml> --probe-network <内部网络> --output <私有目录>
```

预检不会停止、启动、迁移或写入运行中的服务和数据。流程如下：

1. 先复用[原生备份/恢复流程](postgresql-migration.md)准备独立 PostgreSQL 副本、最新账本和遗忘日志副本。
   私有配置仅指向该副本，内部网络仅连接副本数据库和预检容器。配置中的文件路径对应下列复制后的挂载位置。
2. 读取运行中的容器，把 API 可写挂载复制到 `/dev/shm` 下的私有临时目录。
   边缘 SQLite 文件仍用 backup API 复制已提交 WAL；Core 数据库由上述独立原生副本提供。
3. 用目标 Core 镜像在指定的内部网络运行 `companion.release_probe`，显式加载副本配置，检查副本：
   - 需要迁移时，在副本上执行并计时；
   - 判断当前 OwnerAuthority policy 在新代码和现有配置下是否仍然有效；
   - 统计未结任务、待发送消息和 UNKNOWN 模型调用。
4. 删除临时挂载副本，输出 `plan.json` 和 `plan.md`。输出只含代码、计数和镜像 ID，不含消息内容或配置值。
   操作员随后清理独立数据库副本。预检拒绝包含生产 API 的网络，也不继承生产容器的环境覆盖值。

预检结论分为以下几类：

| 类型 | 含义 |
| --- | --- |
| `NO_CHANGE` | 没有需要部署的变化 |
| `EDGE` | 只有 Bridge、Reader 或 UI 镜像变化：不停写，用 `scripts/edge_release.py` 逐个替换 |
| `R` | 加 `--rolling` 且只有 Core 镜像变化、无迁移、policy 有效：用 `scripts/rolling_worker_release.py` 逐个替换 worker |
| `A` | Core 代码变化，不需要迁移，policy 仍然有效：停写，换镜像，用同一份 policy 恢复，交接 |
| `B` | 在 A 的基础上需要迁移：另需备份，并执行一次 `companion init` |
| `C` | policy 失效或要求变更（续期、purpose）：需要主人批准新的 policy；有 UNKNOWN 时走 carry |
| `NOT_READY` | 副本存在发布无法自行解决的阻断项，先处理再重新预检 |

`plan.md` 按类型列出发布步骤。要续期或增加 purpose 时，加 `--policy-change <原因>`。
预检还只读地查看 Bridge 的 Spool（授权状态、未结发送、暂存输入、待上传输入），并记录每个服务的
镜像、部署标签上的代码版本和只读配置文件的 SHA-256。

### 各服务的更新条件

| 服务 | 不停写更新的条件 | 依赖 | 失败时 | 仍需 STOP 发布 |
| --- | --- | --- | --- | --- |
| Core worker（`R`） | 只有 Core 镜像变化；副本上无需迁移；policy 在新代码下有效；没有活跃模型预留；费用 UNKNOWN 作为提示保留 | API 继续跑旧代码 | 该 worker 回到旧镜像，后续 worker 不再处理 | 迁移、policy 失效、付费合同或能力配置变化 |
| Core API | 不单独滚动；随 A/B/C 停写更新 | worker、Bridge、Reader 都连它 | 按 A/B/C 的恢复步骤 | 每次 API 代码变化 |
| Bridge（`EDGE`） | 授权 ACTIVE 且没有 STOP 标记；没有 `RESERVED/IO_STARTED/UNKNOWN` 发送；新 Bridge 的 `companion_delivery_protocol` 与运行中的 Core 相同；配置文件不变 | Core API、SnowLuma | 回到旧镜像；旧镜像读不了新进程写下的记录时会拒绝启动，回执写 `STOP_RELEASE_REQUIRED` | 协议包不同、Bridge 配置变化、授权已 STOP |
| Reader、UI（`EDGE`） | 只有镜像变化，配置不变 | Core API | 回到旧镜像 | 配置变化 |

脚本在任一步失败时立即停止，不再处理后面的服务。回退只换回旧镜像：新进程已经写入的账本、
发送确认、隐私删除和 Bridge journal 记录保持原样，不会被删除或改写。

### 替换 Bridge、Reader 和 UI

```bash
python3 scripts/edge_release.py --project companion-main --plan <私有目录>/plan.json --target bridge=<镜像ID> --output <新的私有目录>
```

顺序固定为 UI、Reader、Bridge。Bridge 先用 90 秒宽限平滑停止：不再领取新的接收、上传、发送和
媒体单元，当前单元做完后退出。退出时 Bridge 输出 `bridge_shutdown_report`，列出未结发送、
待上传输入、待回报结果和暂存输入；有开始了 I/O 却没有平台结果的发送时退出码为 3，脚本拒绝继续，
这条发送只能走 reconcile，不会自动重发。新 Bridge 必须重新持有 `instance.lock` 并连上 WebSocket，
再稳定 20 秒。新旧 Bridge 不会同时运行，因此不会同时写 Spool 或 owner journal。

回执 `receipt.json` 记录每个服务的新旧镜像、镜像自带和部署标签上的代码版本、停止开始、退出、
启动和就绪时间，以及 Bridge 的退出报告、补收报告（`owner_backfill`）和暂存输入的处理结果。
`delivery_evidence` 固定为 `PLATFORM_ACK_ONLY`：发送成功只表示 OneBot 返回了 `message_id`，
客户端是否显示、主人是否已读都无法从 OneBot v11 得知，系统不记录这两种状态。

## 启动核心容器

```bash
cp config.example.toml config.toml
cp .env.example .env
# 编辑 .env，填写 QQ 账号、随机 bridge token 和模型 Key。
# 编辑 config.toml，核对模型、费率与预算。先保持 memory_enabled=false。
chmod 600 .env
# config.toml 只读挂入以 UID/GID 10001 运行的 Core，须由该身份可读。
sudo chown 10001:10001 config.toml
sudo chmod 600 config.toml
docker compose up -d --build
docker compose exec companion-api /app/.venv/bin/python -m companion status
```

Compose 默认启动一次性迁移容器、API 和聊天 Worker。生活 Worker（含日记、目标、探索、分享与社交）、工具、内容 Worker 分别通过 `--profile life`、`--profile tools`、`--profile content` 启用；`diary` profile 已移除，日记需启用 `life`；对应功能还需配置开关。数据库和遗忘日志分别放在 `core-data`、`erasure-ledger` 卷中。API **没有宿主机端口映射**，通过 Docker 网络 `companion-internal` 访问；网络保留模型 API 所需的出网能力。不得给 8765 添加公网端口或公网反向代理。容器以非 root 用户运行、只读根文件系统、删除 Linux capabilities。

`.env` 由宿主 Compose 读取，保留宿主部署用户或 root 所有；不要将它交给容器用户。
后续编辑替换 `config.toml` 时重新核对其 UID/GID 与 `0600` 权限。
可选媒体节点和解析器的部署边界见[私聊媒体与 QQ 账号自主能力](qq-beta/media-autonomy.md)。

现有 AstrBot 容器接入相同网络：

```bash
docker network connect companion-internal <你的AstrBot容器名>
```

网络接入也应写进现有 AstrBot 的 Compose 配置，以免重建后失效。若 AstrBot 运行在宿主机，可采用本机受控网络接入；不要为方便接入而暴露公网端口。

## 主人入口与旧 AstrBot 数据迁移

新入站统一由 `companion_onebot_bridge` 的 OwnerAuthority `daemon` 接收，文字、图片、引用、
文件及语音均沿用原签名捕获、用途校验和持久队列，配置和授权见
[QQ 输入理解](qq-input-understanding.md) 与 [媒体运行配置](qq-beta/media-autonomy.md)。
`daemon` 要求绑定当前 OwnerAuthority；启动进程本身不会创建媒体用途授权、签名密钥或预算。
未知图片类型、畸形描述符或不允许的原图地址只使该图片保持 `UNSUPPORTED`，原文及其他有效
图片继续处理；纯不可用图片通过原生聊天回复说明本次无法读取。原始帧摘要、消息身份和签名
仍保留，授权、签名或来源完整性错误仍拒绝。下载和上传开始后的原租约、UNKNOWN 及费用责任
继续沿用既有恢复规则。

Bridge 的部署默认保持 `restart: "no"`，持久 STOP 或未闭合投递需要按原生流程核对。
生产需要容忍短暂进程故障时，在实际 Compose 覆盖链的最后对 Bridge 设置
`restart: "on-failure:3"`，限制非零退出后的自动重试；不要用无限重启替代故障处理。
使用实际 Bridge 服务名，并在重建后检查容器的 `HostConfig.RestartPolicy.Name=on-failure`
和 `MaximumRetryCount=3`。正在核对的停止实例仍保持 `restart: "no"`，修复、原生交接及
镜像验收完成后再启动。该策略不会因 Docker daemon 重启自动拉起容器，启动由既有运维
流程执行；不要同时叠加另一个反复拉起容器的守护管理器。
策略语义见 [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)。

AstrBot 插件 v0.6.0 删除 ImageQueue、旧图片下载/上传以及宿主新发送入口。
主人事件仍 `stop_event()`，阻止旧宿主默认 Agent 回答，但不写入新 inbox。
`drain_existing=false` 为默认值，不连接 Core；显式启用时只重报已有投递回执、上传原 inbox，
不领取 Outbox、不下载图片、不运行 Reader。先停止旧版本宿主及其主人自动回复，再升级核对插件
或运行下述 CLI。其他机器上的旧版本不使用新的锁，必须由部署操作者停止，不能靠配置文件证明已停机。

```powershell
uv run python scripts/package_bridge.py
```

核对插件包为 `dist/astrbot_plugin_companion_bridge-v0.6.0.zip`，包含原生共享回执实现，不依赖
Core 包。保留旧 `core_url/owner_qq_id/bot_qq_id/bridge_token/protocol_backend/protocol_epoch`
和原数据目录。旧 Reader 启用字段先按下节迁移；不要删除原 `inbox.sqlite3` 或锁文件。

迁移 CLI 从源码仓库运行，输入私密的原插件 JSON、已经配置好的原生 TOML 和旧 spool 的绝对路径。
原生配置必须沿用 owner、bot、Core 地址及 bridge token，并使用全新的 protocol epoch；
OneBot 地址和令牌由已配置的原生连接提供。选择一个**未来**的切换 Unix 秒，在该秒前停好旧宿主。

```powershell
uv run python -m companion_onebot_bridge.migrate_astrbot prepare --plugin-config privacy/astrbot.json --native-config privacy/native-input.toml --spool /absolute/private/astrbot/inbox.sqlite3 --output privacy/native-cutover --cutover-after <planned-unix-second>
```

输出目录必须不存在。`native.toml` 保留原生设置，默认 `delivery_enabled=false`，绑定旧 spool 路径、
交接摘要和切换时间；`astrbot-config.json` 关闭旧核对循环；`handoff.json` 是不含凭据的本地库存与
能力缺口报告。POSIX 目录 0700、文件 0600，Windows 另用 NTFS ACL 限制访问。prepare 不启动服务、
不连接网络，允许在尚有旧队列时准备；它仅升级旧 spool 的核对表，不删除原正文或回执。

停好旧宿主后，可用原凭据核对 Core 中的旧任务。下述 `drain` 只调用 Core 的原 HTTP inbox/result：

```powershell
uv run python -m companion_onebot_bridge.migrate_astrbot drain --plugin-config privacy/astrbot.json --spool /absolute/private/astrbot/inbox.sqlite3
uv run python -m companion_onebot_bridge.migrate_astrbot seal --bundle privacy/native-cutover --host-stopped
```

`drain` 保留原幂等键和来源；严格验证 202 或 `EVENT_ERASED` 后才清除已核对正文，同时保存
不含正文的 `retired_inbox` 回执。拒绝项仍保留正文供诊断；发送未知结果仍为 `DELIVERY_UNCERTAIN`，
报告失败可以重报但不能重新发送。seal 要求本地 pending inbox 和 pending result 均为空，
在原 SQLite 事务中封存交接。成功后的旧 spool 禁止追加和上传新入件。重复 seal 核对同一记录。

达到切换时间后，才能用生成的配置启动原生进程。原生进程同时持有自身单实例锁和旧 spool 的锁，
并核对交接摘要、原生身份及空 pending 状态；换摘要、删交接字段或并发旧核对进程均不能接管已有原生 spool。
来源时间小于等于切换秒的帧不进入新来源，重连/重启仍使用同一新 epoch。手动 `capture_once` 收到此类帧会按原规则停止窗口；核对原因后重新准备窗口，不删除 STOP 标记绕过门禁。请在切换秒之后发送新消息；
这一秒的消息不会被悄悄改造成新来源。迁移后保留旧 spool 及其锁所在目录，原生用户须能读取数据库并写锁。
容器部署要把该目录挂载到配置指定的绝对路径；锁仅适用于同一主机本地文件系统。

```powershell
# 仅基础文字；保持原 Core 门禁和生成配置中的 delivery_enabled=false。
uv run python -m companion_onebot_bridge --config privacy/native-cutover/native.toml daemon
# 媒体入口沿用既有受控计划，计划需按最终配置摘要重新准备和授权。
uv run python -m companion_onebot_bridge --config privacy/native-cutover/native.toml window-run --window privacy/window.json --inventory privacy/inventory.json --authorization privacy/authorization.json
```

这些是操作步骤，不表示已执行上线或真人验收。新 OwnerAuthority 的共享 DTO/final-fence 由集成流程
单独接入；本迁移不添加任何绕开原生门禁的发送或上传路径。

### 旧媒体与会话的实际边界

- 原 ImageQueue 的签名 URL 只在内存中，停机后无法恢复。pending inbox 的图片槽位会随原事件核对，
  但不会重新下载；Core 的 WAITING/FETCHING/RUNNING/UNKNOWN 仍由原期限和恢复逻辑处理。
  `drain` 输出已知图片事件 ID，供检查 `/internal/v2/content/{event_id}`；只检查状态，不能重领 UNKNOWN。
- 旧版本已删除的 accepted inbox 没有完整事件索引，无法从空 spool 证明旧 Core 媒体全部终态。
  本地 seal 只证明本地队列已核对。切换前仍须检查 Core 的内容任务与旧 Outbox；不把它显示为全链路已完成。
- 新入口使用 `onebot-direct:FriendMessage:<owner>`；不改写旧来源、签名或 Outbox 目的地。
  旧来源待发送任务不能由新来源冒领；原生目的地检查会拒绝。旧会话自动衔接、历史待发送的跨来源迁移
  不在本片提供，须由 Core 原核对/取消流程处理后再启用发送。账本、证据、擦除与 UNKNOWN 保留。
- 新媒体仍使用原 `MediaPump`：签名原件清单、来源撤回/新输入抢占、下载租约、上传凭证和持久恢复状态。
  已确认上传只读回执；不确定上传不重新执行效果。引用/转发嵌套能力以当前原生捕获合同为准。

## 将旧内置 Reader 迁出 AstrBot

此版本删除插件内的 Bilibili/Heybox Reader poller。先停止旧插件，私密导出其配置对象为 JSON，
再在仓库内运行以下迁移命令（输出目录必须尚不存在）：

```powershell
uv run python -m source_reader.migrate_astrbot --config privacy/astrbot-config.json --output privacy/readers --network companion-internal
```

命令不连接 QQ、Core 或来源站点，不修改原配置，也不启动服务。它生成：

- `compose.json`：复用 `deploy/source-reader.Dockerfile` 与独立 Reader CLI，沿用原 Core origin 和 adapter ID。
- 每来源一个私密 `.env`：仅携带该 Reader 的原令牌；配置过 Heybox 来源服务时保留独立来源令牌。
- `astrbot-config.json`：保留其他插件设置，移除旧 Reader 字段，供回填 AstrBot 插件配置。

使用 Docker Compose 2.30 及以上版本；`env_file.format: raw` 保留令牌内 `$`、引号和等号的原值。
输出目录应位于私密位置；POSIX 系统创建为目录 0700、文件 0600。不要将配置或令牌提交到 Git。
Core 中的 observation adapter ID/source/token 必须与原配置一致，迁移命令不创建新授权或扩大限额。
默认的 `http://companion-api:8765` 只用于已配置的私有容器网络；其他明文地址仍需是 localhost 或私网 IP。

```powershell
docker compose -f privacy/readers/compose.json build
docker compose -f privacy/readers/compose.json up -d
```

确认独立 Reader 在原队列完成一次受控读取后，把生成的插件配置回填并安装本版本插件。
不要同时运行旧内置 poller。新插件发现旧启用字段会在初始化前报告迁移要求，不会默默丢弃读取能力。
在途租约仍由 Core 保存；失去结果 ACK 时只依赖原租约恢复，不能删除队列或直接重新执行外部读取。
既有 `/internal/v2/readers/{id}/lease`、`validate`、`result` 合同不变；来源证据和擦除继续由 Core 维护。

迁移只保留原公开读取能力。小黑盒未配置来源服务时，生成的 `--public-discovery` 明确启用旧插件的
公开首页标题/链接提取；默认独立 Reader 不受影响。仅提取标题的结果不能作为已读正文，SPA 空壳仍失败。
不会启用账号授权、浏览器、评论、视频媒体或额外预算。Bilibili 元数据读取仍受原请求数约束；不足以取得字幕时明确标注层级。

Reader 部署依据为现有独立 Reader 入口及 [Compose env_file 的 raw 格式](https://docs.docker.com/reference/compose-file/services/#env_file)。

## 开启记忆

先在真实 QQ 上测试正常消息、重复消息和故障恢复，确认仅一条主回复，再将 `memory_enabled=true`。停止 API/Worker 后运行迁移初始化，再启动服务：

```bash
docker compose --profile life stop companion-worker companion-life-worker companion-api
docker compose build
docker compose run --rm migrate
docker compose --profile life up -d companion-api companion-worker companion-life-worker
```

聊天 Worker 只处理即时回复；自动记忆提取由 `companion-life-worker` 执行，因此开启记忆时需一并启动 `life` profile。常规提取不依赖夜间整理开关；有排队、执行中或到期重试的聊天时，后台记忆会让位。

初始化会迁移至当前 schema；开启 M1 时还会准备固定版本的中文嵌入模型，首次需要下载约 90 MB 的公开模型文件。Compose 将缓存放在 `/data/embedding-cache`，后续离线启动可复用；本地默认目录为工作区 `data/embedding-cache`。模型无法加载时会报 `MEMORY_INDEX_UNAVAILABLE`，应修复缓存或下载问题后重启。该缓存只存公开模型权重，个人记忆向量仍在受备份与遗忘机制管理的 SQLite 内。

使用“/记住这个：…”验证新会话的不同问法、冲突提示、确认、纠正和忘记。相似检索返回的是候选，最终回复前还会检查它是否直接对应问题。语义核对调用使用同一个模型适配器并保留原费用记录；金额额度不拒绝核对。来源、授权或调用次数不足时仍明确说明核对暂不可用。

自动记忆默认为待确认假设，有逐字引用证据，冲突内容标为待审查。敏感内容采用规则过滤加模型约束，仍应检查候选；这并非对所有自然语言敏感信息的完美分类器。Core 不将部署凭据加入上下文，并过滤已知密钥和常见凭据格式。外部内容始终作为数据。

## 备份和恢复

日记沿用同一个数据库和遗忘日志；不另建独立且无法回放删除的备份。启用要求及 QQ 指令见 [M1.5 合同](design-v0.4/diary-m1.5.md)。只有完成目标环境与真实样本审核后，才在所有 Core 进程的同一配置中设置 `diary_enabled=true` 并重启。可通过主人私聊立即暂停。HTTP 管理另设独立 `COMPANION_DIARY_ADMIN_TOKEN`，不要将该密钥配置到 Bridge。

协议后端切换请按 [切换与回滚合同](design-v0.4/qq-protocols.md) 执行。先保留 NapCat，实机验证后再切换；Core 数据库和最新遗忘日志不随协议端回滚。

生成 Fernet 密钥，并在部署环境单独保管：

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# 把结果放入环境变量 COMPANION_BACKUP_KEY。
uv run python -m companion backup backups/core.enc
uv run python -m companion restore backups/core.enc data/restored.sqlite3
```

容器内使用同样的命令，但备份目录需挂载可写的独立备份卷。热备份使用 SQLite backup API，包含已提交的 WAL 页面，随后加密。恢复只写入全新路径，回放当前配置指向的最新遗忘日志并检查数据库完整性，再原子放置目标文件；现有数据库不会被覆盖。

生产应由你现有的备份服务定时调用该命令，把加密文件和**最新遗忘日志**分别复制到受限存储。首版没有静默创建系统 cron，也没有替你配置异地存储或自动保留策略。旧数据库备份和旧遗忘日志一起回滚会丢失后来的删除意图，禁止这样恢复。

升级前先备份，停止所有已启用 Worker 和 API，再执行一次迁移，随后启动服务。当前 Alembic 链支持空库和同版升级；含日记记录的 `0004_diary` 拒绝破坏性降级。`0003_memory_index → 0002_m1` 仅移除可重建的向量缓存，但不要直接降级删除事实或 v0.5 业务表。应用回退前验证 schema 兼容性与最新遗忘回放，不能仅把数据库和日志都换成旧版本。恢复时会撤销旧媒体会话，并将未完成投递、执行和内容读取置为需要核对的状态。

`0021_reflection_admission_retry` 为反思保留 `RETRY_WAIT` 和下次尝试时间。历史任务若因调用次数或并发不足而在模型请求预留前失败，会在来源集合一致、当前授权有效时续跑；已完成的记忆和日记步骤继续复用。日调用次数耗尽等到配置时区的次日零点，并发占用短暂等待。已发出或计费不明的模型请求保持原状态，不自动重放。可按实际回补天数配置 `reflection_max_daily_calls`（默认 1，最大 5），调用次数、并发与授权窗口继续生效，金额额度按当前模型费用规则作统计。有等待任务时拒绝降级此迁移。

独立 B 站 Reader 的公开发现使用固定的热门列表 API，每次至多取得 20 个去重视频标题；取得标题后仍需独立的读取任务才能形成正文证据。访问受限、跳转或响应异常会记录失败，不切换接口规避限制。

## 常见运行状态

原生 OneBot Bridge 的引用、卡片正文及签名受控窗口的多图与记忆配置，见 [QQ 输入理解说明](qq-input-understanding.md)。

- `MODEL_RETRYABLE`：最多 3 次指数退避，保留未知计费预留。
- `MODEL_COST_UNKNOWN`：运维费用提示；保留原未知费用，其他独立请求继续运行。
  金额额度和授权校验边界见[当前模型费用规则](model-cost-policy.md)。
- `DELIVERY_UNCERTAIN`：不自动重发；查看 QQ 后使用“/核对发送”指令。新普通聊天仍可继续。
- `ERASURE_LEDGER_MISSING/MISMATCH/TRUNCATED`：停止服务，找回最新独立日志再恢复。
- `OWNER_IDENTITY_MISMATCH`：已有数据库属于另一主人，禁止直接更换 owner；新身份使用新数据库。
- `WORKER_ALREADY_RUNNING`：已有 Worker 持有 OS 锁，正常关停该实例；不要删除锁文件绕过检查。
- 反思 `RETRY_WAIT / SCENARIO_CALL_LIMIT`：尚未发出模型请求，等待次日可用额度后由生活 Worker 续跑；不会重复生成已完成的日记。
- 日记尚未取得模型请求 ID 时，并发槽拒绝延后 30 秒；模型日调用次数拒绝延后到下一个本地周期，金额超额作为费用提示。领取次数和错误原因保留，重试仍复核授权窗口、来源、调用次数和并发。已产生 UNKNOWN 的请求停止自动重试；STOP 取消的日记不会因重复调度重新启动。

原始事件按 `raw_event_retention_days` 到期清空，派生原文一同失效。自然到期仅移除过期证据：仍有独立有效正向证据的记忆保留；失去有效来源的记忆降为待审查、置信度归零且不进入检索。纠正命令生成的副本不算独立证据。主人主动遗忘和来源授权删除仍清除整个关联事实及派生闭包。长期保留策略应在实际使用后明确配置。

自然到期在独立 ErasureLedger 中记录为 `raw_retention`，同时保存当次 `retention_cutoff`，恢复旧备份时按相同边界重放。既有删除记录继续执行原删除语义。写入新记录后，应用回退须使用支持该记录类型的版本，保留最新独立账本。

在线 `/纠正这一条` 将原始命令事件与新事实一起提交。Worker 在事实提交后、成功回执登记前退出时，可恢复同一个新记忆 ID；租约、STOP、截止和遗忘仍在恢复与提交时复核。


自主调度切换时，须先使用旧版本 compose 配置停止独立的 `companion-diary-worker`，
再应用统一 Life Worker 的 Core／领域／部署改动。新版本只启动 `companion-life-worker` 消费
自主任务；不得保留旧日记进程并行生成。新版 Life Worker 同时持有旧 `diary-worker` 文件锁：
遗留的旧日记容器仍在运行时，它会以 `WORKER_ALREADY_RUNNING` 启动失败，而不是并行写日记。
使用 `docker compose up -d --remove-orphans` 移除旧容器。原先只启用 `diary` profile 的部署须改为
`--profile life`，否则升级后不会生成日记。此步骤需要独立部署授权，代码验收不会自动执行。

### Bridge 分层依据与验证

2026-10-25 检查 DSH QQ 插件 `c8b9158b37817b0dbc1e514e62e16204ad47a6ce` 的
[inbound](https://github.com/tencent-connect/dsh-qqbot/blob/c8b9158b37817b0dbc1e514e62e16204ad47a6ce/src/transport/inbound.ts)
和 [bootstrap](https://github.com/tencent-connect/dsh-qqbot/blob/c8b9158b37817b0dbc1e514e62e16204ad47a6ce/src/gateway/bootstrap.ts)：
附件处理交给传输层、入站与生命周期集中组装。本项目复用该职责划分，实际媒体授权与恢复复用已有
`capture`、`pending_media.MediaPump`、Core claim/check/upload，不复制 DSH 的直接 followup 权限。

单实例锁提取自本项目原生 spool，并复用 Python
[flock](https://docs.python.org/3/library/fcntl.html#fcntl.flock) /
[msvcrt.locking](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)；交接封存使用 SQLite
[显式事务](https://www.sqlite.org/lang_transaction.html)。摘要用于本地交接一致性核验，不是新的业务授权。

`test_astrbot_media_retirement.py` 验证旧状态、并发实例、配置/身份/时间边界和独立迁移 CLI 的真实 HTTP；
`test_native_media_cutover_process.py` 验证迁移后签名捕获、真实 HTTP、独立媒体/聊天进程、上传回执丢失、
擦除及重启。现有原生撤回、抢占、UNKNOWN 和预算测试继续覆盖原 MediaPump。隔离插件打包测试确认
不依赖 Core，且包内没有被退休的 ImageQueue 和宿主发送入口。测试使用模拟外部传输，不是真人验收。
