# R1 固定上下文交流指导实验

当前交付是离线实验准备工具。真实模型回复、人工评分、体验改善和 QQ 验收均为 PENDING。
诊断、源码基线与一手参考见 [diagnosis.md](diagnosis.md)。

## 比较对象与冻结条件

`baseline` 保留原有指导；`participation_v1` 仅替换 Expression 的完整回复措辞，
再追加脚本中可审阅的交流指导。这是一个组合策略比较，不能据结果单独归因于其中一句。
它不包含示例答案，也不改变生产 Prompt。

先由原生 `Core.context` 构建上下文，再生成两个系统指导版本。每对的非系统消息、
事实、历史、当前输入、模型和输出上限相同。追加指导发生在上下文选择之后，不重新裁剪，
因此比较不混入历史缺失差异。运行前须检查两侧输入估计均在获批上限内；超限就停止，
不能单独删减一侧历史。计划记录 payload、共享上下文与整个计划的哈希。

使用现有 `deepseek-flash` 非 thinking 配置，输出上限 2048，软上下文 16000。
请求没有 seed 或 temperature；重复结果可能不同，不声称采样可复现。
36 个仓库 case 全是 SYNTHETIC，覆盖 7 类场景。合成事实只用于构建实验输入。

真实 pilot 使用本地受保护的 5 个连续主人 turn 与实际 SENT 片段。前 4 个 turn 构成
可见历史，第 5 个主人输入为比较目标；第 5 个旧回答不进入输入。未发送内容不成为历史。
没有历史完整 provider payload，因此这是可见对话重建，不能声称复现旧请求。

## 离线使用

在本工作树运行 Python；可用仓库已安装的虚拟环境。输出目录必须是本工作树 `.local`
内的全新目录，不能覆盖已冻结计划。已有 pilot 和合成计划应直接复用。

原生 PostgreSQL 版本的 `prepare` 需要按
[迁移说明](../../postgresql-migration.md) 配置隔离测试集群
`COMPANION_POSTGRES_TEST_CONFIG`。
它从空白原生模板为每个 case 创建独立数据库，结束时删除数据库；
合成材料和独立凭证仍写入上述工作区目录。`blind` 不连接数据库。

```powershell
python scripts/evaluate_r1_conversation.py prepare --output .local/r1-synthetic-plan-new
python scripts/evaluate_r1_conversation.py prepare --private-baseline <本地受保护原文路径> --repetitions 3 --output .local/r1-private/pilot-new
python scripts/evaluate_r1_conversation.py blind --plan .local/r1-private/pilot/plan.json --observations .local/r1-private/observations.json --output .local/r1-private/blind
```

`prepare` 不调用模型或发送消息；`blind` 只检查输入绑定并导出已有观测。该脚本没有真实
执行子命令，不能用准备成功替代付费准入。私有输出继承目录的访问控制；`.local` 被 Git
忽略并不等于访问控制，原文、请求、回复、映射和凭据只保存在受保护本地目录。

观测文件是 JSON 数组，每个声明的请求必须恰好有一行：`case`、`repetition`、
`strategy`、`payload_sha256`、`evidence_type`、`state`、`text`。
`evidence_type` 为 `MODEL_RESPONSE` 或 `FAILED_ATTEMPT`；`state` 为 `SETTLED`、
`UNKNOWN`、`FAILED` 或 `NOT_STARTED`。失败项的文本为空；不得用合成回答补齐。
观测身份必须来自实际执行收据，导出器的字段检查本身不证明远端调用发生过。
完整账本另行保留 request ID、usage、成本与错误；盲评输入不暴露策略标签和账本身份。

## 预先登记的盲评规则

先冻结计划与以下尺度，再生成回复。按计划交错运行策略；导出器平衡 A/B 位置，并将
策略对应表单独写到 `mapping.private.json`。评分者只看 `ballot.json`，完成全部评分
并保存后才揭盲。相同 case 的重复不是独立样本，汇总时按 case 分组。

每项 `ratings` 填入 `{"A": 分数, "B": 分数, "reason": "具体依据"}`，使用 1–5 分：

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| naturalness 自然度 | 明显生硬、错接意图 | 能接话但有模板感 | 贴合当下语境、表达自然 |
| desire_to_continue 交流意愿 | 明显降低继续交流意愿 | 无明显影响 | 愿意继续说；收尾时也尊重结束 |
| deliberateness 刻意感 | 无明显表演或迎合 | 有些用力 | 明显说教、表演或机械追问 |
| factual_accuracy 事实准确性 | 编造或严重误归属 | 有不确定或小误读 | 可核对事实、主体、记忆与边界均准确 |

刻意感越低越好，其余越高越好。没有可核对断言时，准确性填 `null` 并注明“不适用”，
不能自动给满分。`overall` 填 `A`、`B`、`TIE`、`BOTH_BAD` 或 `UNSCORABLE`。
`failure_notes` 记录错误记忆、虚构亲历、主体混淆、无必要解释/建议/反问、忽略收尾等。
不得因回复短就判好，也不得因评分者喜欢某种语气而忽略事实错误。

只有两侧均 SETTLED 且有实际文本的 pair 可评分。失败、UNKNOWN、未启动、截断或空回复
保留在总分母及失败表中；不能隐去失败后只展示成功样本。截断由执行收据中的结束原因
人工复核，导出器并不检测截断。UNKNOWN 停止后续真实调用，不自动补跑。

6-call pilot 是同一 case 的 3 对重复，只用于检查策略方向、输出与评分流程。
后续至少 30 个独立 case 的配对（当前计划 36 对、72 请求）需要独立费用授权。
完整报告列出各维度分布、配对差、胜/平/负/两者都差、不可评分数、类别失败与实际费用；
不得将 3 次重复当作 3 个不同真人样本，或将模型自评当作主人反馈。

在看到全部评分前不决定上线。新增事实/权限错误须先解决；自然度与交流意愿改善且刻意感
不恶化才有理由推荐启用候选策略。结果混合则报告具体失败类别并重新登记实验，
不能只挑成功回复。生产策略、部署、主动交流与真实 QQ 验收仍分别待后续决定。

## 六次真实 pilot 的拟授权与执行方式

拟请根聊天统一申请：将上述有限私聊重建输入发送到现有 DeepSeek provider，固定现有
模型，只执行已冻结计划的 6 次请求，顺序执行，无自动重试；估计合计约 $0.0183，
拟批准窗口额度 $0.05。估计不是账单，也不是 provider 的硬费用保证。
当前并未获批；72 次合成计划不包含在这份申请中。

授权后才准备真实 executor，并先用 mock provider 验证。复用 `PaidModelApproval`、
`budget.reserve` → `budget.complete` → `ModelClient`；不直接另写 HTTP 计费旁路。
使用专属实验数据库与追加式 custody journal，不能重置或借用生产账本来绕过累计额度。
若现有账户有累计预算合同，继续遵守并绑定其 custody，不能用新数据库清零已花费用。

具体限制映射：`window_calls=daily_calls=6`、`concurrency=1`、`per_request_calls=1`，
`window_micro_usd=daily_micro_usd=monthly_micro_usd=50000`，
`per_attempt_micro_usd=per_request_micro_usd=5000`，`max_input_tokens=2400`，
`max_output_tokens=2048`。沿用计划的配置费率与 125% 输入估计余量。
批准时间窗、费率有效期、账户累计约束及凭据绑定必须由真实授权材料确定，不伪造已批准字段。

执行前核对 plan hash、每项 payload hash、共同历史 hash 与实时来源有效性；每项用稳定
logical request ID 绑定计划和序号，原样发送冻结 messages。每次结算后检查 usage 和剩余额度，
UNKNOWN、异常 usage、来源撤回、到期、预算拒绝或停止要求均中止，不补偿性重试。
重启时先核账本；已 dispatch 却缺失本地回复的项保留不确定证据，不再次付费生成。
实际回复只写本地观测，不进入 Outbox，也不写回生产记忆。

这段是可审阅的复用方案，不是已实现或已验证的真实执行器。mock 验证与授权材料齐备后
才能安排真实调用；当前离线 PR 不引入新的预算、任务或发送机制。

## P1 离线策略组件与反馈重置

`companion/conversation_policy.py` 复用上述 `participation_v1` 原文及变换。
`ConversationPolicyConfig` 默认 `OFF`，`SHADOW` 只产生可检查的选择，二者不改变模型输入。
显式 `ENABLED` 才增加交流指导。稳定指导和本轮表达参考分别返回，由 Core 原有 prompt
组装器处理；角色、事实、历史、当前输入、输出限额和原生分段规则保持原有职责。
短确认和收尾倾向简短；明确求助认真处理；仍有效的 owner 反馈控制详细程度、分步和
话题继续方式。它不推断隐藏意图、隐私或真实 AI 心理，不设固定人格、称呼和亲密话术，
也不产生主动任务、工具授权或发送决定。

Core 已在组装前调用 `prompt_parts`，并在原有来源校验中调用 `revalidate`。
进入实际上下文的 Affect 快照也须独立重验，以拦截生成期间发生的重置或失效，不依赖策略启用。
配置未提供时序列化保持原形；建议的 Settings 字段为可空 `conversation_policy`，默认
省略。本组件没有模型客户端、数据库、计费或投递入口。当前模式与真实授权、实机可用性
和主观自然度是不同维度。

Affect 仍只有原来的四维明确反馈与 TTL。`AffectEngine.reset` 仅接受 owner，并要求稳定
操作编号和原生 Ledger hook。重置以 `affect_reset` 记录持久化，使用空的事件/记忆删除
集合；`owner_feedback_reset_at` 是可重建的单调截止时间。截止时间及之前的明确反馈
不再进入 Affect 或派生的表达偏好，新反馈可重新生效。同一时刻的反馈保守地视为旧反馈。
聊天原文、事实记忆、兴趣、目标、已接受承诺和费用不被删除；重置也不启用或暂停自主能力。
重复请求和旧记录重放不能推进或倒退截止时间，旧备份通过相同 Ledger 恢复该屏障。
原生 Ledger 的读入、重放与 apply 必须识别此目标并在通用删除分支之前处理；未接入时
reset 返回 `AFFECT_RESET_NOT_INTEGRATED`，不会伪报成功。Core 的显式控制入口应使用
`/重置互动`，沿用 owner/private 认证与既有命令范围，引用、近似文本或带参数文本不执行。

原生 Ledger、Core 控制命令与主人管理 API 已接入。`POST /internal/v2/affect/reset`
仅接受控制令牌及 `request_id`（1–128 个 ASCII 字母、数字或 `_.:-`），返回 `RESET`
和持久化截止时间；Owner runtime OFF 时也能管理。此操作不激活授权、模型或投递。
真实 HTTP 与独立 Core worker 的合成检查覆盖上下文后重置阻断 dispatch、无需模型的
重置命令，以及旧加密备份恢复后重放屏障。UNKNOWN 与原预算 journal 保持原状。

36 个合成 case、v1 策略字节和历史 v1 计划保持原样。导出会重新校验计划 hash、每项
payload hash、共同输入 hash，并检查同一对的历史、当前输入和模型参数完全一致。
已有 JSON 计划不重写；v1 的估算仍按原合同解释，不补写成精确计量。新 `prepare` 默认
生成 [v2 计量计划](measurement-v2.md)，保存完整 payload 的原生 `Measurement.receipt()`、
计量配置与源码身份，保留历史 v1 验证。v2 不改变原生评价用途的六请求执行范围。

实现核对：

- [DSH system-prompt](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/system-prompt/src/index.ts)
  的 blob `45442e3a234e7fe71c482f74196013f06fe51559` 将稳定 section、动态 context 与工具声明分开。
  此处复用该职责划分，并继续使用本仓库原有组装器。
- [KI-CO sessionStateService](https://github.com/Kisera001/KI-CO/blob/main/src/services/sessionStateService.ts)
  的 blob `09cff0b7ff924703a45e44e39de77687777ededc` 提供可清除的有界短期状态卡；
  本项目复用明确的短期边界，持久性由现有 Ledger 提供，不移植其人格配置或 localStorage。
- [OhWhale AstrBot setup](https://github.com/chauko0423/ohwhale-deploy/blob/main/ohwhale/astrbot_setup.py)
  的 blob `ad689fd6af3b521bd40239c215f994c38314daaf` 是部署配置与 persona 注入，未提供可复用的
  对话反馈状态机。本项目继续复用自己的 Affect、Expression 和原生消息分段，没有引入固定人设。

这些离线检查只能证明输入、状态与边界行为，不能证明自然度、幽默或陪伴体验已经改善。
付费模型评价与真实 QQ 体验仍等待单独授权和真人验收。

## participation_v2 候选策略

`participation_v2` 是主人在 2026-10-28 批准的第二个候选措辞，按处境区分参与方式：
暂时没话题时由她用上下文里真实存在的细节起头；吐槽时先站在主人这边，不给未请求的建议；
被问“你是谁”时用自己的话说确实知道的内容，名字只来自记忆或对话，不用“AI 朋友”
“陪你聊天”这类定位语；细节必须有来源。措辞在 `companion/conversation_policy.py`，
v1 原文、变换字节和已冻结的 v1 计划保持不变。

`ConversationPolicyConfig.strategy` 默认 `participation_v1`；只有显式设为
`participation_v2` 且 `mode` 为 `ENABLED` 才改变模型输入。收据记录 `strategy`，
生成期间切换策略会使 `revalidate` 失败。

`prepare --treatment participation_v2` 生成 baseline 对 v2 的计划，计划带
`treatment` 字段；v1 计划不写该字段，因此原有哈希不变。盲评映射随计划的 treatment。
[r1_expression_cases.json](../../../tests/fixtures/r1_expression_cases.json) 收录
冷场、身份和吐槽三类各两个 SYNTHETIC case：一个检查参与方式，另一个检查没有依据时
不编造。主人给出的示例回复只作评分锚点，不进入 Prompt 或数据集。

```powershell
python scripts/evaluate_r1_conversation.py prepare --dataset tests/fixtures/r1_expression_cases.json --treatment participation_v2 --output .local/r1-expression-v2
```

原生评价入口按原有范围接受 v1 或 v2 计划：主人 pilot 为单 case、六请求；合成计划最多
36 个 case、216 次请求。上面的 6 case 计划共 12 次请求，本地估算预留约 0.04 美元。
生产是否启用 v2 由主人在盲评后决定。
