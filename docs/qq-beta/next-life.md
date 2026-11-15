# 第二阶段：表达学习与日记重算

本阶段复用 P11/P12/P13 的 Affect、Interest、Diary、Reflection、Goals、WakePlanner、
NightlyConsolidator。新增表达学习的可重建投影，以及撤回来源后的私人日记重算，
已接入 Core、配置、HTTP 和原独立 worker。表达学习默认关闭；生产启用与真实 QQ
验收由部署方统一执行，完整 CI 以集成候选的唯一验证回执为准。

## 分开的状态与来源

| 状态 | 服务和证据 | 生命周期 |
| --- | --- | --- |
| 主人的表达偏好 | `ExpressionLearning.snapshot(requester)`；原始私聊中的明确反馈 | 跨本地自然日形成候选，每次读取重建，版本随有效证据变化 |
| 短时互动状态 | 原 `AffectEngine`；同一套明确反馈识别 | 现有 TTL 后回到 neutral/unknown，短时暂停不写入长期表达候选 |
| 自主兴趣 | 原 `interest.profile` 的 `self.interest` | 已完成自身目标及实际独立 FETCHED/WATCHED 回执；内容和目标双重去重 |
| 主人偏好/稳定事实 | 原记忆服务的 `owner.preference` 等命名空间 | 当前有效原始来源、冲突与纠正合同 |
| 反思 | 原 `ReflectionService` | 逐条带引文的可否决假设，不成为事实或稳定人格 |
| 计划与执行 | 原 `GoalEngine`、Reader | 候选、排队、执行、实际结果分别记录；通知仍使用 Outbox |

Affect 不推断心理诊断，也没有 PAD 数值。表达候选是主人对沟通方式的偏好，不能作为
“伙伴天生喜欢某种事物”的证据。模型日记、模型自述、网页指令和推测均不参与表达学习。

## 表达候选的窄接口

```python
from companion.growth import ExpressionLearning

view = ExpressionLearning(store, settings, clock).snapshot(requester)
```

`requester` 必须等于当前 owner；返回私有 `SHADOW` 视图，包括 `privacy_epoch`、
`policy_version`、`timezone`、`version`、`truncated` 与 `items`。每条候选包含维度、值、
状态、来源 event ID/原因/原始时间、本地日期、独立日数、置信度、强度和自己的版本。
版本是规范化投影的 SHA-256，同一证据在重启、重复读取、清除 Affect 缓存后保持一致。
它用于比较内容变化，不是单调修订序号，也不应作为授权凭据。

- 只复用现有明确反馈规则中的 `interaction_tone` 和 `cognitive_load`。
  engagement/novelty 的短时信号不参与长期习惯；引用、外部来源、控制消息、未来时间、
  非主人/非私聊及被清除的来源退出投影。
- `expression-feedback-2` 支持明确的情境反馈，例如“我吐槽时简短一点”、
  “讨论时详细解释”“正式求助时分步骤说”；分别按维度和情境累计独立日数。
  支持倾诉/吐槽、玩笑、讨论、正式求助四种显式情境，其他语境不猜测。
  当前原文有明确情境时优先选择对应偏好；“嗯”等短句仅从调用方已获准的最近原始
  前文继承，前文来源也进入回复证明。最多选两条、同一维度优先情境项，当前明确指令
  与短时 Affect 优先。表达学习仍默认关闭，不增加选择模型或聚类存储。
- 同一本地日多次反馈只计一天。最新相反反馈开始新的学习区间；旧的多数票不能压过
  新更正。同日相反再相反也要重新积累独立天数。
- 默认三个独立日为 EMERGING、七日为 STABLE，复用现有
  `emerging_min_sessions` / `stable_min_days` 阈值。STABLE 仅描述证据统计。
  `confidence` 是达到稳定日数的覆盖比例，上限 0.9，并非心理学概率；`strength`
  仅在 STABLE 时大于零，上限 0.5，是未来表达提示的上限。
- 最多检查最近 500 条合格原始事件；超过则 `truncated=true`，该轮不产生 STABLE。
  过期和撤回可使候选降级或消失；没有第二份持久来源或派生正文需要清理。

`GET /internal/v2/expression-learning` 使用原控制端认证，返回 `Cache-Control: no-store`
的私有 SHADOW 视图。聊天仅在 `expression_learning_enabled=true`、原表达功能开启且
当前范围为 owner/private 时采用未截断的 STABLE 候选。同一维度仍在 TTL 内的明确
Affect 反馈优先，表达强度最多 0.5；只为实际保留在上下文中的候选登记全部来源。
延迟使用会重新核对版本、epoch 和当前来源。候选不能改写事实、权限、费用、承诺、
目标或消息次数。实际跨天表达效果仍需主人反馈，合成测试不代表效果已在生产验证。

## 来源失效后的日记重算

```python
run_id = diary.recompute_day(day, requester)
# 调度后仍由原 DiaryService.generate_once / 独立 diary worker 推进。
```

阶段二接线提供主人指令 `/重算日记 YYYY-MM-DD`，以及使用既有 diary admin 认证的
`POST /internal/v1/diary/{day}/recompute`。HTTP 返回 `local_date`、`run_id` 和实际队列
`state`；未安排时为 `run_id=null`、`state=NOT_SCHEDULED`。命令按实际 PENDING、RUNNING
或 RETRY_WAIT 状态说明排队、整理或等待重试。日期未闭合、用量未确认和尝试耗尽都有
对应说明，排队成功不代表新日记已经生成。消费队列的服务是统一 `companion.life_worker` 内的原生 Diary lane。
新普通输入可取代命令的回复气泡，已经接受的重算仍保留；来源撤回继续阻止派生结果提交。

此入口仅为因来源失效而成为 REDACTED 的已存在日记重新排队；正常日记和主人修订
不会被重写。未闭合的本地日期或非 owner 被拒绝。暂停、关闭、明确删除日期墓碑
继续生效。重复排队返回同一个 run ID，重复 worker tick 不会重复生成。

重算复用原 run/逻辑请求 ID、预算及尝试上限。已经 SETTLED 的调用保留原费用；
UNKNOWN/RESERVED 或丢失的 usage 凭证拒绝重算。原尝试额度耗尽时返回
`DIARY_RECOMPUTE_ATTEMPTS_EXHAUSTED`，不重置上限；已经发生的 admission 竞争仍遵守
原有延期恢复。调度本身无外部 I/O，生成和提交仍检查开关、租约、epoch 和当前来源 hash。

新正文仅来自当前来源包。来源不足则 SKIPPED，旧正文保持不可见。成功时复用 entry ID，
增加 version，并只关联新来源；原文及修订已经由账本擦除，不建立包含撤回原文的历史副本。
来源再次变化时拒绝迟到结果，保留实际 usage；删除整天后不能重算，也不能从旧备份复活。
此方法不会新增事实、兴趣、目标或 Outbox 消息。

## 与记忆、Reader 和 worker 的接线约定

1. 集成方独占新的账本 `input_recall` 操作及最小 canonical source marker。它先记录账本，
   再清除撤回来源和真实派生闭包，保留独立有效证据。这里读取当前有效事件和 epoch；
   显式 forget/source_grant 的原严格闭包不变。独立主人 B 引用了历史 A，不在本模块
   创建 `event_dependencies(B, A)`；引用关系由集成方的 job/reply evidence 负责。
2. Reader 仍通过原 Observation 接收合同写入真实 event 和依赖关系，保留原 URL、
   内容 hash、覆盖、抓取时间、来源与解析器证据。Diary 的来源提供器继续区分
   DISCOVERED 与 FETCHED/WATCHED，live 排除 simulation。读取失败、计划和链接发现
   不能被重算变成已经完成的经历；日记不反向成为记忆事实来源。
3. 原 WakePlanner/Goals/Reader 与 private_life admission 负责睡眠、空闲、冷却、
   主人聊天优先、预算、资源延期、STOP 和恢复。重算只进入原 diary 队列，不能绕过这些
   检查。当前重算由明确命令或认证 HTTP 触发，长期表达另有默认关闭的配置开关。
4. 计划 DONE、读取结果、Outbox SENT、手机看到分别保留原有含义。表达偏好不影响承诺
   履行；反思否决仍只撤销相关未来意图，已经发生的执行和 UNKNOWN 保留。

## 验证与交付范围

新增 `tests/test_next_life_growth.py` 和 `tests/test_next_life_diary.py`，覆盖跨天/同日
反馈、短时衰减、相反反馈、来源修订/撤回、无索引重建、强度上限、时区、扫描截断、
独立 worker 重算、重复、预算/UNKNOWN、来源竞争、暂停、崩溃租约、整天删除和旧备份回放。
模块撤回测试通过既有账本制造真实来源失效，入口测试另通过认证 friend_recall HTTP。
既有 Diary admission、P12/P13、Goals 和 Consolidation 回归继续验证复用路径。

阶段二新增 `tests/test_next_life_integration.py`：命令与 HTTP 状态/认证的合成检查，
以及 loopback HTTP → 已认证输入/撤回 → 重算入口 → 独立 `life_worker` 的三条场景。
模型传输使用 mock，实际 private-companion feature/config/paid gate 保持开启并验证；
通过事件等待 worker 完成，不替换其调度、预算或提交逻辑。分别核对 API 重算、命令回复
被取代后的重算保留，以及调用中撤回后的迟到结果拒绝。最终集成检查由集成负责人统一运行。

本候选的相关检查结果与精确提交保存在专属交付状态文件；最终跨模块 CI 回执由集成方
统一维护。合成时钟不证明真实自然日，FakeModel/mock provider 不证明真实阅读或表达质量。
本候选没有新 QQ 发送、付费调用或部署；implemented、configured、live-verified 分开验收。
