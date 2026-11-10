# Companion Core

面向单一主人私密聊天的虚拟朋友核心。Python 模块化单体，支持签名设备的 Flutter 多 Bot 文本客户端与原生 OneBot Bridge；记忆和表达风格来自有证据的互动，初始人格为空。

Flutter 双端共用的原生文本 MVP 的设备登记、启动、开发 MCP 和验收见 [原生聊天说明](docs/native-chat-mvp.md)。它通过 Core 的真实原生入口聊天；可同时接入多个 Core 实例、分别私聊；流式、Android 真机与后台完善、AstrBot / DSH 和群聊按后续切片交付。

源码仓库：[Tidekin/companion-core](https://github.com/Tidekin/companion-core)。

当前交付 **v0.5 + M2 内容理解**：在 M0/M1 与私人日记基础上，新增可追溯观察、短期表达状态、夜间整合与反思、持久目标、有限探索、表情表达、受限工具和短时媒体会话。主人分享的链接、QQ 卡片和图片可经配置后读取并进入对话。新增能力默认关闭，真实 QQ、平台账号、日记样本及物理设备启用门禁见 [实现与启用记录](docs/design-v0.5/status.md)。

## 能力与边界

- 主人身份、私聊范围、机器人回声校验；入站事务、幂等冲突检测、按会话 FIFO。
- Worker 限次重试；QQ 发送使用 `LEASED → SENDING → SENT/DELIVERY_UNCERTAIN`。不确定消息不自动重发，后续聊天可继续。原生回复以持久化可读取为交付点。
- DeepSeek Flash/OpenAI-compatible 模型适配；保存真实输入、输出、缓存 Token 与按冻结费率计算的费用，缺少用量时保留 UNKNOWN 提示。
- 主动私聊有冷却、每日次数、免打扰、暂停指令；不伪造现实生活或浏览记录。
- M1：批量记忆候选、逐字证据核验、语义冲突核对、确认/纠正/忘记、中文词法与本地语义混合检索、相关性筛选、空白起步的自我观察。
- 加密热备份；恢复前强制回放独立遗忘日志；隐私变更期间的旧模型结果被丢弃。
- M1.5：独立日记 Worker、自然日调度、真实来源核验、私人保存、主人查看/修订/删除、用量记录和完整遗忘回放。
- QQ 协议端通过原生 OneBot v11 Bridge；批次隔离消息 ID，回滚禁止复用旧批次。旧 AstrBot 入口已退休，配置、状态核对与会话迁移边界见部署文档。

M0/M1 普通聊天不进入工具执行循环。v0.5 的 DSH、Windows 与媒体会话使用独立身份、额度和明确授权；公开空间与评论没有执行入口。内容理解仅将外部文字作为不可信证据，不能改变权限或人格，也不会把标题或封面当作已观看完整视频。

## 本地启动

需要 Python 3.11+（已验证 3.12）、PostgreSQL 16+ 和 `uv`。结构迁移与旧备份导入见 [PostgreSQL 说明](docs/postgresql-migration.md)。

```powershell
# 在仓库根目录执行。把缓存和临时数据限制在工作区。
$env:UV_CACHE_DIR = "$PWD/.local/uv-cache"
uv sync --frozen
Copy-Item config.example.toml config.toml
$env:COMPANION_OWNER_QQ_ID = '你的QQ号'
$env:COMPANION_BOT_QQ_ID = '机器人QQ号'
$env:COMPANION_BRIDGE_TOKEN = '你生成的至少24位随机令牌'
$env:COMPANION_MODEL_KEY = '模型API密钥'
uv run python -m companion init
uv run python -m companion serve
# 第二个终端：设置同样的环境变量后
uv run python -m companion worker
# 第三个终端：同样的配置；日记、生活与自主活动由统一 Life Worker 执行，默认关闭，完成门禁后再开启
uv run python -m companion life-worker
```

演示可设 `COMPANION_MODE=demo`，无需模型密钥；回复明确标记为演示。`.env` 仅由 Docker Compose 读取，本地命令需通过环境变量注入。不要提交真实配置、密钥或数据库。

默认 `memory_enabled=true`，同时默认开启 `participation_v2` 对话策略、语义分段回复和聊天 thinking（见 [拟人化默认值](docs/humanlike-defaults.md)）；需要只验收 M0 链路时在 `config.toml` 关闭。主模型、费率、调用次数、时区和频率均可配置，未知字段和非法值会使启动失败。费用口径见 [费用记录与运行边界](docs/model-cost-policy.md)。生产配置使用 TOML；环境变量覆盖文件值。

## QQ 指令

内置指令必须以 ASCII `/` 开头（允许消息首尾空白），命令名和参数用空白或中英文冒号分隔。
未加 `/` 的文字、引用正文中的指令和未知的 `/` 文本按普通聊天处理。

| 指令 | 作用 |
|---|---|
| 普通文字 | 聊天，唯一主回复模型由 Core 调用 |
| `/记住这个：我喜欢茉莉绿茶` | 明确确认一条用户事实（M1） |
| `/记住这个` | 记住上一条用户消息（M1） |
| `/你记得什么` | 查看近期可检索记忆及 ID（M1） |
| `/确认这一条 mem_…` | 将候选确认为事实 |
| `/纠正这一条 mem_… 新内容` | 创建替代记录，保留修订关系 |
| `/忘记这一条 mem_…` | 清除关联记忆、证据及衍生内容 |
| `/暂停主动分享` / `/恢复主动分享` | 控制自主私聊 |
| `/发送状态` | 列出待人工核对的发送 ID |
| `/核对发送 msg_… 已发送` / `/核对发送 msg_… 取消` | 核对后解除不确定状态，不重发 |
| `/今天写日记了吗` / `/日记列表` | 查看日记状态和最近日期 |
| `/读日记` / `/读日记 YYYY-MM-DD` | 主动请求查看私人日记 |
| `/修改日记 YYYY-MM-DD 新正文` | 有界修订，保留版本 |
| `/删除日记 YYYY-MM-DD` | 清除正文、修订及恢复时的旧副本 |
| `/暂停写日记` / `/恢复写日记` | 控制运行时开关，不能绕过配置硬禁用 |
| `/目标列表` / `/承诺列表` | 查看持久目标与已接受的承诺 |
| `/接受目标 goal_…` / `/取消目标 goal_…` | 接受或取消指定目标 |
| `/安排任务 https://… 主题` | 建立有期限的阅读承诺 |
| `/反思列表` / `/当前交互状态` / `/为什么形成偏好` | 查看反思、临时沟通偏好与来源 |
| `/暂停自主活动` / `/恢复自主活动` | 控制自主探索，已接受的任务仍继续处理 |

“忘记”会清空关联原始事件，同时失效所有可能复述该信息的近期回复、摘要和自我特征投影。其他独立事实仍保留。这是首版的保守策略；已发到 QQ 的消息无法由本地遗忘操作撤回。

## 部署、验证与目录

- [P11 核心体验前置交付与后续验收](docs/qq-beta/p11-unattended-delivery.md)、[私人 Beta 观测与反馈](docs/qq-beta/p11-beta-observation.md)
- [72 小时私人连续运行](docs/qq-beta/continuous-private-beta.md)
- [停止窗口的交接恢复](docs/qq-beta/handoff-recovery.md)
- [VPS / Bridge 部署](docs/deployment.md)
- [主人控制台、配置版本与统一云预算](docs/owner-console.md)
- [当前运行拓扑、存储归属与模块边界](docs/architecture.md)
- [验收结果与限制](docs/validation.md)、[回放数据](docs/replay-report.json)
- [原始设计合同](docs/design-v0.3/implementation-m0-m1.md)、[接口合同](contracts/openapi.v1.yaml)
- [日记实施合同](docs/design-v0.4/diary-m1.5.md)、[协议切换与回滚](docs/design-v0.4/qq-protocols.md)、[语音视觉接口规划](docs/design-v0.4/multimodal.md)
- [v0.5 状态与启用](docs/design-v0.5/status.md)、[主人分享内容](docs/design-v0.5/content.md)、[受限执行器](docs/design-v0.5/executive.md)、[媒体会话](docs/design-v0.5/media.md)

```powershell
./scripts/check.ps1                     # 检查并清理测试临时文件
./scripts/check.ps1 -TestFiles tests/test_api_image_lifespan.py  # 开发期定向验证
./scripts/check.ps1 -Docs               # 文档隐私、引用与证据检查
```

回放、进程冒烟、构建和打包验证由最终 CI 执行，开发期按变更选择相关检查。
需要真实 DeepSeek 合成场景验证时，使用 `scripts/verify_memory_live.py` 或
`scripts/verify_diary_live.py`；它们读取本地 `.env` 的模型 Key，按调用次数和 Token 限制执行并保留实际费用。

Linux 检查：`uv run ruff check companion migrations astrbot_plugin_companion_bridge tests scripts` 与 `uv run pytest -q --basetemp=.local/test-temp`。CI 同时运行 Windows/Linux 测试和 Linux 容器构建。

CI 在 Windows/Linux 上各运行两个互斥分片，按测试文件分配，新增测试自动纳入；本地
`./scripts/check.ps1` 仍运行完整测试。各平台的回放、冒烟和打包检查执行一次，分片 JUnit
记录保留 7 天。纯文档 PR 及其 main 合并运行隐私、引用和证据校验；已知证据 JSON 的
白名单位于 `scripts/ci_policy.py`，未知文件、运行代码、依赖、迁移、标签和合并队列仍运行
完整检查。uv 缓存按锁文件与平台复用，测试数据库和预算/遗忘账本始终独立创建。
证据白名单直接关联对应检查，包含 P12/P13；公开 Markdown/JSON 隐私扫描自动覆盖新增阶段。
本地 `-Docs` 使用同一组检查；独立 worktree 可用 `-Python <已有解释器路径>` 复用依赖环境。
开发、审查、集成与验收按[开发流程](docs/development.md)推进，最终 CI 摘要自动记录
检查配置、实际提交、代码树和各项结果，交付材料直接链接该回执。

代码 PR 仍运行完整 CI。合并到 main 后，若同仓库 PR 最新一次 CI 在 24 小时内完整成功，
且其验证记录中的实际 Git tree 与合并结果完全一致，main 只运行记录核验与最终门禁，
并在任务摘要链接原全量检查。记录绑定运行 ID 和重试次数，仅全量成功的运行生成，
不会连续复用轻量运行。记录缺失、过期、API 查询失败、最新检查未成功或代码树不同，
均回退到完整检查；无对应 PR 的代码直接推送、手动运行、标签和合并队列继续完整验证。

`companion/` 是核心包；`astrbot_plugin_companion_bridge/` 可单独打包安装；`migrations/` 维护版本化数据库；`tests/` 按 M0/M1 编号验收；`scripts/` 提供可复现回放和运行检查。运行数据、缓存与交付包不会被加入 Git。

## 许可证

自有源码与文档以 [GNU Affero General Public License v3.0 或更高版本](LICENSE)
（`AGPL-3.0-or-later`）发布。通过网络向他人提供修改版时，需要按 AGPL 第 13 条向这些用户提供对应源码。
第三方组件、模型、图片／表情、字体和外部服务仍适用各自条款，见
[source_reader/THIRD_PARTY_NOTICES.txt](source_reader/THIRD_PARTY_NOTICES.txt) 与
[许可核查](docs/qq-beta/p11-license-review.md)。
