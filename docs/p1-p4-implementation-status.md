# P1–P4 后端与控制台差距报告

截至 2026-10-25，本报告记录当前后端实现、控制台差距与验收状态，状态为 **FINAL / CI_SUCCESS**。
UI 核对基线为 console-v2「能力与开关」页面 `0a1b301` 的 41 项能力；日记提示候选 `c0fcbb2` 的静态检查 3 项通过。
生产/测试核对点已冻结为 `765ecb7b42d18c2ea84d957569b7711a0e1d51a2`，已发布到分支 `codex/p1-p4-integration`。
PR #109、#111 仍未合并；部署与真实体验分别验收。
原生 Voice、account、Meme host、嵌套附件与 Life→REM 消费均已集成；原始输入、媒体取消、
Reader 原始结果入 SDK、生成后投递来源检查、最终原图/记忆、整组额度及未结 RPC 领取边界的增量已进入该工作树。

**唯一集成 CI：[完整 workflow_dispatch](https://github.com/yanyuhanyue/companion-core/actions/runs/40151901077) — attempt 2 / COMPLETED / SUCCESS。**
最终证据统一见上述集成 CI：冻结候选的 14 个 job（含最终 `ci`）全部成功。
每平台收集 7160 个唯一用例：Linux 为 7145 passed / 15 skipped，Windows 为 7151 passed / 9 skipped；
另有 8 项浏览器用例在独立 `browser-reader` job 通过。平台跳过项及复用的分片证据均记录在同一回执中。
第一轮 `f44a8fa` 的 132 个失败用例已由纠正 XML 逐项闭合，原始失败与修复证据保留。
当前候选首次运行有 1 项 Linux 等待超时；生产代码和原 20 秒断言未改，本地 4 次与同候选 CI 分片复验通过，根因未确认。
此次仅重跑失败的 Linux 1 分片，复用其余 7 个成功分片及支持 job。完整 CI 成功不替代下述 UI 缺口、部署与真实体验验收。

## 状态如何阅读

| 维度 | 本报告含义 |
| --- | --- |
| 基础后端已实现 | 下表所写的有界原生能力已有实现；不代表完整 P1–P4 扩展、已合入 main 或已部署 |
| UI 已有 / 缺口 | 当前页面能操作、展示什么，以及仍缺少什么；UI 缺口不降低基础后端实现等级 |
| 自主调整接入 | 运行消费者确实读取 `fields` 对应的调整值；可保存授权或提议不等于已接入 |
| OFF / SHADOW / 授权 | 配置与执行约束独立于实现等级；SHADOW 不代表可执行，READY 不授予许可 |
| 真人验收 | 真实模型/QQ 体验等待用户宣布可测试；最终组合版本的体验结果待单独记录 |

以下 41 项均按已有基础后端范围标记“已实现”。部署是否可用、当下是否授权和是否已完成
真人验收，须分别读取真实运行状态与回执，不能由这个标记推断。

## 41 项基础能力与 UI 差距

| # | 能力 ID / 名称 | 基础后端范围（已实现） | console-v2 已有 / 仍缺少 | 自主调整 |
| --- | --- | --- | --- | --- |
| 1 | `memory.manage` 记忆管理 | 查询、确认、纠正、忘记 | 已有管理操作；缺有界来源原文、真实实体关系与召回解释 | 未声明 |
| 2 | `memory.recall` 记忆检索 | 有来源约束的检索 | 已有配置；缺召回依据、热温冷解释与调节视图 | 未接入 |
| 3 | `diary` 日记 | 读取、纠正、忘记、暂停 | 已有管理及即时暂停；授权预算调整仍未消费 | 部分 1/2 |
| 4 | `consolidation` 夜间整理 | N1/N2/N3、语义摘要与用途估计 | 已有配置；缺 N1/N2/N3 阶段、进度、检查点与重试视图 | 未接入 |
| 5 | `reflection` 反思 | 有来源支持的反思建议 | Life 可读建议；缺主人会话内接受/拒绝操作 | 未接入 |
| 6 | `goals` 目标与承诺 | 目标与承诺、有限 1–3 步读取计划及续行 | Life 可读列表；缺接受、取消、步骤及执行回执下钻 | 未接入 |
| 7 | `wake` 自主活动调度 | 受限调度与暂停 | 已有局部 decisions 和部分调整；缺完整认知、执行反馈链 | 部分 3/4 |
| 8 | `affect` 情绪观察 | 明确反馈与衰减 | 已有配置；缺来源、衰减、撤回、重置检查视图 | 未接入 |
| 9 | `expression` 表情回应 | 受限选择及投递流程 | 已有配置；缺每次选择、重复控制与效果评价视图 | 未接入 |
| 10 | `expression_learning` 表达习惯学习 | 有来源的表达偏好 | 已有配置；缺偏好/兴趣证据和跨日变化下钻 | 未接入 |
| 11 | `proactive` 主动私聊 | Life 私聊草稿、获准渠道自动消费与暂停 | 已有模式操作及配置；缺逐次决策与时机反馈追踪 | 未接入 |
| 12 | `social` 社交语义判断 | 有来源的分享候选 | 已有配置；缺候选至表达结果的完整关联 | 未声明 |
| 13 | `reading.web` 网页阅读 | 授权网站读取 | 内容列表可读；缺授权签发/撤回、手动读取；自定义 adapter ID 映射不完整 | 未接入 |
| 14 | `reading.bilibili` B 站读取 | 原有读取与回执 | 内容列表可读；缺授权管理和手动读取；端点由部署保管 | 未接入 |
| 15 | `reading.heybox` 小黑盒读取 | 原有读取与回执 | 内容列表可读；缺授权管理和手动读取；端点由部署保管 | 未声明 |
| 16 | `exploration` 内容发现 | 受限内容发现 | 已有配置和内容列表；缺发现理由、候选及深读回执关联 | 未接入 |
| 17 | `bilibili.video` B 站视频分析 | 受限视频分析 | 已有配置及通用内容证据；缺采样覆盖率和工具结果细节 | 未接入 |
| 18 | `model.understand_video` 视频理解模型 | 仅在本轮实际开放工具时调用 | 共享视频配置；缺开放工具与来源细节；开关不授予付费许可 | 未声明 |
| 19 | `qq.files` 私人文件 | 文件解析与任务记录 | 任务可读；缺上传、重解析与页级证据下钻 | 未声明 |
| 20 | `qq.images` 私聊图片理解 | 认证图片理解 | 已有配置及通用任务视图；缺来源图像证据检查 | 未接入 |
| 21 | `qq.animation` 私聊动图理解 | 受限动图理解 | 通用任务视图；缺帧证据检查，原生无独立动图总开关 | 未声明 |
| 22 | `qq.voice.asr` 语音转写 | 转写任务与节点状态 | 只读节点/任务状态；无转写原文、音频、播放、重试/取消操作 | 未声明 |
| 23 | `qq.voice.tts` 语音回复 | 合成与原投递回执 | 只读节点/任务/投递状态；无音频播放、重试/取消操作 | 未接入 |
| 24 | `qq.poke` 回应拍一拍 | 受限拍一拍回应 | 已有嵌套开关；缺批次和动作检查视图 | 未接入 |
| 25 | `qq.poke.native` 回拍一下 | 独立许可与原生回拍回执 | 已有嵌套开关；缺具体回拍动作及回执下钻 | 未声明 |
| 26 | `qq.account.qzone_publish` QQ 空间发布 | 独立授权发布与回执 | 账号视图只读；缺精确内容/受众/有效期审批及发布对账 | 未声明 |
| 27 | `qq.account.nickname` 改昵称 | 独立许可与回执 | 账号视图只读；缺计划批准、拒绝和撤回 | 未声明 |
| 28 | `qq.account.signature` 改签名 | 独立许可与回执 | 账号视图只读；缺计划批准、拒绝和撤回 | 未声明 |
| 29 | `qq.account.avatar` 改头像 | 已审核素材与回执 | 账号视图只读；缺头像预览及计划批准、拒绝、撤回 | 未声明 |
| 30 | `qq.profile.observe` 资料观测 | 原有资料观测 | 已有配置和只读账号信息；缺证据至后续计划的完整关联 | 未声明 |
| 31 | `unicode.emoji` Unicode 表情 | 保留输入原文 | 输入协议保留原文；无独立开关 | 未声明 |
| 32 | `qq.face_names` QQ 表情名称 | 在富媒体范围内识别原生名称 | 已显示真实富媒体范围；无独立开关，仍须原生输入范围与当前 authority 许可 | 未声明 |
| 33 | `qq.face_images` QQ 表情图像 | 在图片许可内接收认证表情图 | 使用共享 `content_enabled`；捕获、审批及 Bridge 条件仍由后端核对 | 未声明 |
| 34 | `memes.catalog` 表情目录 | 已审核素材目录 | 使用 `expression_enabled`；当前展示理解任务，缺上传、编辑、审核、隔离流程 | 未声明 |
| 35 | `memes.community_import` 社区表情获取 | 受限素材导入 | 已有配置和理解任务投影；缺导入素材的完整审核、隔离界面 | 未声明 |
| 36 | `memes.search` 表情搜索 | 配置搜索服务的发现路径 | 已有配置和理解任务投影；缺搜索候选至采用结果的完整关联 | 未声明 |
| 37 | `memes.semantic` 表情语义理解 | 有来源的理解任务 | 已有任务状态、原因及详情；缺素材编辑、审核、隔离操作 | 未接入 |
| 38 | `media.voice` 网页语音会话 | 独立媒体会话 | 仅能力/配置；控制台没有启动/关闭会话；独立 media_web 不等于已集成 | 未声明 |
| 39 | `media.camera` 摄像头 | 受限摄像头观察 | 仅能力/配置；缺控制台会话操作与观察证据视图 | 未声明 |
| 40 | `media.android` 安卓设备 | 受限设备观察 | 仅能力/配置；缺控制台会话操作；不代表 Computer Use 已完成 | 未声明 |
| 41 | `executive` 执行任务 | 独立审批执行器 | 缺主人控制台提交、审批、撤回、任务详情；READY 不证明 DSH 实机完成 | 未声明 |

表中“未声明”指没有进入这版 18 项可委托调整清单，不表示基础能力不存在。“未接入”
仅指自主调整消费者未接入，不表示主人配置、原暂停机制或安全停止失效。
表情目录等未完全展开的 UI 流程仍需最终候选逐项核对；UI 证据复用既有浏览器回执。

## 18 项自主调整的字段与真实接入

本节记录既有 UI/消费者接入范围。基线来自 console-v2 原生 `DELEGABLE` / `WIRED`，共 **18 项能力、38 个字段条目，4 个已接入**。
`owner`、`propose`、`self` 表示主人保留控制、允许提议、允许在范围内自行调整；新授权默认
`owner` 且不允许暂停。调整只能缩小运行范围，不开启主人关闭的能力，也不提高花费。

| 能力 | 可声明的 fields | 已消费的 wired | 尚未消费 |
| --- | --- | --- | --- |
| `memory.recall` | `pause`、`extraction_min_events`、`extraction_idle_seconds` | 无 | 全部 3 项 |
| `diary` | `pause` | `pause` | 无 |
| `consolidation` | `pause` | 无 | 暂停 |
| `reflection` | `pause`、`reflection_daily_max_suggestions`、`reflection_max_daily_calls` | 无 | 全部 3 项 |
| `goals` | `pause`、`goals_max_active` | 无 | 全部 2 项 |
| `affect` | `pause` | 无 | 暂停 |
| `proactive` | `pause`、`proactive_daily_limit`、`proactive_interval_seconds`、`proactive_idle_seconds` | 无 | 全部 4 项 |
| `wake` | `pause`、`wake_max_daily_runs`、`wake_min_interval_minutes` | 全部 3 项 | 无 |
| `exploration` | `pause`、`exploration_daily_candidate_limit`、`exploration_daily_deep_reads`、`exploration_daily_requests` | 无 | 全部 4 项 |
| `expression` | `pause`、`expression_cooldown_minutes`、`expression_selection_probability` | 无 | 全部 3 项 |
| `expression_learning` | `pause` | 无 | 暂停 |
| `memes.semantic` | `pause` | 无 | 暂停 |
| `reading.web` | `pause` | 无 | 暂停 |
| `reading.bilibili` | `pause` | 无 | 暂停 |
| `bilibili.video` | `pause` | 无 | 暂停 |
| `qq.images` | `pause` | 无 | 暂停 |
| `qq.poke` | `pause` | 无 | 暂停 |
| `qq.voice.tts` | `pause` | 无 | 暂停 |

页面详情已单独显示接入范围；保存授权、提议或调整，以及列表的火花图标，都不能证明
所有字段已影响运行。日记为部分接入 1/2，调度为部分接入 3/4，其余 16 项尚未接入。

## P1–P4 新增范围、默认模式与验收

| 范围 | 本地实现 / 集成状态 | UI 操作或展示缺口 | 默认模式 / 约束 | 验收状态 |
| --- | --- | --- | --- | --- |
| P1 对话策略与 Affect 重置 | 原生策略、重置及来源屏障已接入；实际选中历史快照贯穿模型与发送检查，原始输入与冻结投递责任保持关联，当前来源失效取消任务 | 缺策略效果、Affect 来源/重置检查面板 | `conversation_policy` 默认 ENABLED + `participation_v2`（2026-10-29 起）；SHADOW 不修改策略输出 | 分片及真实 SDK 进程边界已有验证；自然度与真人对照待验收 |
| P1 固定上下文评价 v2 | 原生 evaluation、固定上下文与计量来源检查已有实现 | 缺创建/取消/结果/真人评分的完整流程 | `evaluation_enabled` 默认 false；离线 36-case 文件不扩大原生六请求 pilot | 机制专项已有验证；自然度效果待体验 |
| P2 记忆分层、经历与解释 | MemoryContext 已消费热温冷投影、经历接续与合格模型摘要；来源、修订与擦除约束保留；发送前重核原记忆事实及有效性 | 缺层级、激活原因、Episode/Timeline、摘要及召回解释 | 逻辑层级不授予读取权限；salience 不改变事实真值/置信度 | 已有分片证据；真实模型价值估计与跨日效果待验收 |
| P2 N1/N2/N3 | 原 EXTRACT 产出摘要与 salience 1–10；原事务持久化，MemoryContext 消费；不增加独立模型调用 | cognition 仅返回阶段状态；记忆 API 不提供 importance/摘要，页面没有对应解释 | importance 为有效估计均值/10，仅在同等相关性下排序 | 已有真实 HTTP 与三个独立 nightly 进程证据；摘要质量未做真人验收 |
| P3 认知循环与有限规划 | Wake/Life/Goal/Reader 消费原始执行结果；1–3 步 read_plan 支持 CONTINUE/WAIT/ABANDON、CAS 和逐步证据；父级完成要求连续位置及末步结果；Reader 校验原租约 URL 摘要与签名输入来源 | cognition API 已返回 steps/完成数/原始回执；console-v2 尚未请求该 API，缺操作与观察页面 | UNKNOWN 阻止继续选择；STOP 后仅可读取合格元数据；有限读取计划不等于任意任务规划 | 真实 HTTP、独立 Reader、三步位置与来源篡改负例已有证据 |
| P3 REM | 默认关闭的私人虚构生成、selector、原账本/来源/擦除与生成后 owner 读取均已接入 | 缺 opt-in、记录、未验证问题及来源撤回 UI；cognition 只读元数据 | 私人虚构不成为事实、观察、自动目标或公开来源 | 已有真实签名 HTTP、独立 Life/REM 进程及恢复不重付验证；梦境质量未验收 |
| SDK / DSH 边界与 Bridge | SDK Conversation 使用原租约/usage/Outbox；原生 Voice/account/Meme host 与嵌套附件均已集成，Bili 通过 deferred SDK 工具选择，原视觉分析结果进入 Conversation；Meme 原生目录、发现、选择与最终图片投递贯通 | 缺执行审批、撤回、UNKNOWN 对账及完整工具证据视图 | 原始 authority、付费结算、原图/记忆/Reader 结果及最终 send-check 保持；媒体仅凭绑定来源回执取消，UNKNOWN 与原费用保留 | Node→Core→Bridge、Bili、Meme 及 account 组合已有合成验证；真实 QQ/设备体验待用户宣布可测试 |
| 统一调度与 token Measurement | 原 selector 轮询各 lane、聊天优先、后台单步预算；原 RPC 未结时继续接纳新输入，第二 worker 等原责任结束再领取；工具声明/调用/结果贯通同一 payload 计量 | 缺资源、排队、抢占与计量解释 | estimate / exact_local_render / unsupported 分开；本地精确不等于 hosted 精确计费 | 已有工具两回合、篡改拒发、真实 HTTP burst、独立 Store 领取及原图组合检查 |
| P4 表达意图、草稿与反馈 | Life 保存沉默/延后/研究/日记/私聊/QZone 草稿及 PRIVATE_CREATION；selector 自动消费已同意渠道，rem.save 将创作原文交给原 REM job/day quota；Life/QZone 消费其获准的反馈与执行结果 | 缺渠道同意编辑、意图与结果追踪、精确内容/受众审批及发布对账 | automatic_actions 默认空；公开动作仍需独立授权；REM 独立生成和 Life→REM 保存均保持 FICTION、owner_only 与原来源约束；保存原文不增加第二次模型调用 | Life→REM 已通过签名 HTTP、独立 Life/REM 与 owner cognition 读取组合验证；Meme host 已集成并完成定向组合验证；长期效果与真人体验待验收 |

域能力、host 集成、控制台操作与真人效果分别记录。N2/N3、REM、有限 Goal、
SDK/selector 和 P4 反馈已有实际消费者。真实模型与 QQ 体验等待用户宣布可测试；
账号、设备及长期效果分别记录验收结果，合成测试证明其对应机制和边界。

## 标记移除与最终收尾

console-v2 的临时实现标记由 `companion/console_static/app.js` 中单一
`SHOW_IMPLEMENTATION_STATUS` 控制。设为 `false` 可移除列表和详情中的全部实现标记；
此前浏览器证据已确认 41 项能力 ID/原运行状态保持一致，自主调整接入提示仍显示。
运行状态与接入范围具有操作意义，不随临时实现标记删除。

Unicode/QQ 表情输入及表情目录采用上表所列开关映射，结论适用于指定 console-v2
候选。手机端返回按钮在特定滚动位置仍会被顶部遮挡。

本报告复用既有 console-v2 浏览器证据，本轮生产/测试核对点为上述已冻结候选。
目标 console-v2 的列表及详情按后端能力 ID 渲染，共有 41 项实现标记；当前页面尚缺
cognition、importance 与模型摘要解释。既有 18 项自主调整范围保持原核对结论。

## 最新集成边界

| 边界 | 已集成行为与专项证据 |
| --- | --- |
| 原始输入与责任回执 | 签名捕获、正文/身份/时间、原模型 admission 与冻结投递材料共同校验；`origin_event_id` 为空或改绑不能隐藏原 UNKNOWN 投递；读取不改写费用或原责任 |
| 媒体取消与恢复 | 原 CHAT 取消和整体责任结清分别报告；下载/上传最终 HTTP 边界重核原输入回执，确认取消后清理对应本地 pending；普通 403、回执篡改不能触发清理，真实 UNKNOWN 保留 |
| Reader URL 与来源 | 每次校验保留原租约 URL 摘要与签名输入身份；拒绝来源后不产生观察，晚到已知计量和 UNKNOWN 沿原回执处理 |
| Reader 生成后投递 | `owner_delivery._sources` 调用 `agent_reading.revalidate_results(..., executing=False)`，在回复记录、lease、begin-send 和最终 send-check 重核原结果正文、证据及来源绑定；拒绝发送时保留已结模型费用 |
| Bili/Reader 原始结果入 SDK | 按 `reading_attempts.result_event_ids_json` 读取原 v3 输出，将结果摘要纳入既有 Conversation snapshots；最终模型传输与 Conversation 持久化复用同一重核函数，已结部分证据保留，PENDING/UNKNOWN 阶段不作为完成结果 |
| 分段回复整组额度 | 原 `check_delivery` 预检尚未计入账本的剩余可发送段；窗口/日/月/account 额度不足时首段不发，ACK 前缀只计一次；仍按原 `admit_operation` 逐实际尝试记账 |
| 最终原图与记忆 | 原图证据和已选中记忆事实、有效性在模型/发送边界重核；输入修订、撤回、擦除及恢复继续约束消费 |
| 未结 RPC 与第二 worker | 新输入可以接纳并取消旧业务任务；原模型 RPC 或 SENDING 责任未结时，独立 worker 不抢领；真实 burst 与原图组合检查已有回执 |

Root 的 Reader 最终正文/来源屏障及合法 Bili/ACK 组合 9 项通过；Tools 原始视觉结果与上下文重核的 33 项相关检查通过。
最后的 Bili followup 与历史/额度/分段测试提交已审查并纳入冻结候选，Docs 199 项通过。
这些专项证据保留各自范围；最终完整 CI 结论统一引用顶部摘要与回执。
真实模型/QQ 体验由用户宣布可测试后开展，当前仍为待验收。

读取范围与限制见 [cognition 接口说明](cognition-read.md)，逐项任务书映射见
[任务书覆盖核对](p1-p4-taskbook-coverage.md)。
