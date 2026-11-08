# P13-F3：只读上下文选择诊断

沿用 `companion inspect-context --trace-id`，只在受鉴权本地材料范围读取现有 SQLite。
本轮未执行任何真实数据库读取；测试使用合成资料与本地认证协议。
没有 Console、新索引、检索重跑、模型调用、日志回填或运行授权变化。

原 Inspector 已展示冻结历史/记忆/图片的匿名引用、模型尝试、实际 Token 与费用责任。
P13 新增每个聊天 job 的 `memory_selection`，使用原 `live_text_memory:<job>` 记录，
核对事件、privacy epoch、候选/选择/历史摘要及对应已结算 memory_review/payload。
最终 chat context 仍须与同事件、计划、epoch 和选中记忆一致；所有校验在一个只读事务中。

`trace.recorded_window` 从事件原输入记录的 window 摘要找到原历史批准，核对保存的批准
摘要、计划和 privacy epoch，仅投影候选/计划/批准摘要与执行模式。它与报告顶部明确标为
`configured_*` 的当前配置分别展示；旧事件不能被当前窗口重新归属。缺失、损坏、过期或
redacted 状态都不补猜历史批准，仍不验证外部授权原件。

| 状态 | 准确含义 |
| --- | --- |
| NOT_RECORDED | 没有持久选择记录；不能推断没检索或没记忆 |
| REVIEW_PENDING | 已冻结非空候选，尚无完成选择记录 |
| NO_CANDIDATES | 记录中的候选为空；不推断整个事实库为空 |
| SELECTION_RECORDED | 有同 job 已结算复核与一致的选择摘要；不证明供应商或真实授权 |
| STALE_EPOCH / REDACTED | 已撤销或版本变化，隐藏候选、旧上下文引用及内容 |
| INCONSISTENT | 记录损坏、超界、摘要/事件/复核/上下文不一致，不展示候选 |

每个有效候选仅导出 memory ID 的 SHA-256 与记录内的去向：复核待定、复核未选择、
已选择但 chat context 尚未记录、进入最终 chat context、或未进入最终 chat context。
候选/选中/最终数量分别记录；尚无最终上下文时最终数量为 null，不冒充零。
选择模型的语义理由没有持久化，`selection_reason=NOT_RECORDED`；未保存的历史淘汰
列表同样 `discarded_history=NOT_RECORDED`。不能猜成 Token 超限、相关性低或来源无效。
历史最终引用仍由原 `context.history` 展示，模型 Token 与尝试数由原 usage 展示。

`source_validity=NOT_REVALIDATED` 始终保留：诊断不重新验证授权、来源或独立账本，
不替代运行准入。UNKNOWN/RESERVED 仍只显示责任金额，不伪称实际 Token 或供应商账单。
输出不含明文、旧签名、URL、账户或路径；散列仍是可关联标识，对外分享前需再匿名化。
遗忘后当前输出抑制旧引用；已有私人导出不会自动删除，应按既有保留/删除合同处置。

## 原生 SDK 请求计量投影

每个 `model_calls` 项复用 Console cognition 的 `measurement` 投影，读取该次
`model_admissions.measurement_json`，仅导出计数身份、输入计数、预留裕量和输出预留。
工具循环的每次模型请求分别显示。Inspector 不重算历史 Token、不读取 prompt 正文，
不根据当前 tokenizer 配置升级旧回执；旧行 `{}` 为 `NOT_RECORDED`，损坏或超长 JSON
为 `INCONSISTENT`。计量记录不证明供应商实际用量或独立账本已经重新核验。

`exact_local_render` 是固定渲染器的本地计数，`estimate` 是本地估算；媒体和未知路由
继续保留其原计量身份。UNKNOWN/RESERVED 仍可展示本地计量和费用责任，但实际 Token
为空。没有保存的 `context_regions`、`trimming_reason` 均为 `NOT_RECORDED`，不能从最终
payload 大小反推裁剪原因。原生 SDK 尚未保存旧窗口的 context 记录时，顶层 context
同样保持 `NOT_RECORDED`，不把调用计量误称为完整 Context Planner。

Console 的“思考与决策 → 模型调用”显示同一回执，并将有效已结算、已记录发送的用量
与本地计量分开。demo、未发送、UNKNOWN 的实际用量显示为未记录。原有主人会话、来源
过滤、隐私版本校验及 no-store 规则继续生效；沿用既有表与诊断记录。

缓存用量另读取该 request 唯一的 `model.usage_diagnostic` 原回执。适配器在归一化前记录
`cache_usage_present`：收到 usage 对象且含受支持的 `prompt_cache_hit_tokens` 为 true，
该对象缺字段为 false；无 usage 对象、旧行或人工响应未记录为 null。存在标记不证明数值有效。
只有有效原用量、已发送 admission、唯一诊断及各用量/金额一致且标记严格为 true 时，
共享后端 `cache_usage` 投影为 `RECORDED`，返回明确的 0 或正数。其余为 `NOT_RECORDED`/null；
有效的实际输入、输出、配置费率金额仍照常返回。重复、损坏、超长、
缺失或不匹配诊断不挑选最新一条。原财务列、SDK 数值计数及结算回退保留；不回填历史。
这份观察不证明 provider 缓存隔离、命中策略或真实账单。

缓存三态已由 Console 的请求计量界面消费。集中字段说明、合成响应与认证/版本/错误处理见
[前端对接文档](../console-v2/ui-supplement.md#缓存用量契约与请求计量界面)。

补充验证使用 100/1000 轮公开合成候选请求，复用原裁剪和固定 tokenizer，包含完整工具
声明、两项工具调用/结果及过大的必需结果。它逐项测量并对超限候选执行原 reserve
拒绝，每组仅发送一项 Mock provider 样本，不能称为千次模型调用或真实模型质量评估。
真实 HTTP 入站后的独立 Core worker 另验证两次 SDK 请求和原回执，外部 transport
保持 Mock；原生撤回后保留原模型费用并隐藏来源。真实 QQ 和长期体验仍待独立验收。

回归覆盖空/待定/选中/最终差异、被遗忘/epoch变化、错误摘要/事件/重复候选/外来选择、
未结算或错误 payload、损坏/超长记录及数据库不变；另在原本地认证记忆召回链中验证
实际冻结记录的投影，仍使用 Mock provider，不是付费或 QQ 真人证据。

## 原生请求规划记录

原生 Conversation 有 v1 规划记录、且没有旧窗口上下文记录时，`context` 展示实际保存的
区域数量及逐请求选择：原 request ID 的 SHA-256、payload/measurement 摘要、选中区域数，
以及 `SOFT_LIMIT / INPUT_LIMIT / CONTEXT_WINDOW_LIMIT` 原因。只投影已有记录，不重计 Token，
不推测模型语义理由。未准入候选单列 pending，不计为已发生模型请求。

读取先核对 job、privacy epoch、authority 引用、来源快照前缀及原 admission/measurement。
缺项或未知版本为 `NOT_RECORDED`，损坏或请求绑定不一致为 `INCONSISTENT`；旧 epoch 与擦除后
隐藏规划详情。`source_validity=NOT_REVALIDATED` 保留，不替代执行时的权限和来源检查。
规划 JSON 沿原 Conversation 擦除闭包删除，历史费用和 UNKNOWN 责任不变。

原 assembler 仍先执行既有初始软容量选择，再登记实际选中的来源；未选入的候选不被 pin。
v2 的初始 fit 原因记录在 `assembly_dropped`；逐请求原因覆盖其后 planner 实际执行的裁剪。
请求时 planner 可继续裁剪已选入的可选区域，但不撤销既有来源责任；未完成的主人输入、
原图及其完整 turn 保持必需。记录中的 epoch 是布局捕获代次；无关 epoch 前进时，执行仍由
原授权、实际来源和最终输出核验决定，诊断继续把旧代次标为 `STALE_EPOCH`。

## 近期来源、检索工作量与恢复边界

明确问题下，近期保护依据已授权的主人原文及展开后的引用、附件内容；普通文本和富文本
以完整来源组计量。完整规则、必需引用和未完成输入先占用请求预算，无关尾轮不因最近性
获得必留或最高优先级。简短续聊继续沿已有主题继承规则处理。

相关原文召回可选择近期来源。完整近期来源能放入必需请求时优先保留全文，否则选中并
固定有界原文表示，替换同一来源的近期表示，随后再裁剪，
因此去重依据实际选中内容。两种表示共用原历史轮数及 UTF-8 上限；摘录仍按完整回放成本
准入，原文召回比例应用于其他历史组占用之后的剩余额度。后续 SDK 请求和保存恢复沿用
同一必需来源组及有效性核对。

候选正文、事实复核、证据根、源正文和依赖边按有界批次读取。验证缓存只在 Store 当前的 repeatable-read
只读快照内复用；写入、离开快照或异步计算后重新验证。来源正文、依赖和引用的数据库
读取计入 `sql_milliseconds`，包括原生超时及 savepoint 开销。字节、节点和候选上限继续生效；
后续候选超限时只保留已经完成全部证明的候选。最终必要校验失败继续阻断使用。
异步评分的来源准备最多使用剩余字节的三分之一，为 await 后重新验证和最终来源读取留下
空间；仅实际读取扣量。新快照清除准备阶段的局部额度和验证缓存，检索内重新读取继续计入
同一 Work。执行边界必需复核保留各自的有界预算，不共享可选检索已消耗的额度。

PostgreSQL 两字查询参与有限词法提名，候选内复用现有中文 bigram BM25；精确身份匹配
继续使用 ASCII `translate`。`pg0002_fact_key_ascii` 将原 `lower(fact_key)` 索引替换为
与查询相同的表达式，原基线迁移保持冻结。

`chat_restore_timeout_seconds`（默认 5 秒）限制 checkpoint 恢复、来源复核及争用退让的
总时间。每条 SQL 使用剩余绝对时间和原服务器超时中更小的值；内层不能延长外层。
超限通过既有 RETRY_WAIT/lease 恢复让出 worker，保留请求 ID、工具结果、费用和 UNKNOWN
责任。模型及外部工具执行位于恢复重试之外。

v6 runner 在合成考题擦除之前，将既有 planner 回执写入人物目录的
`context-plans.jsonl`；`requests.jsonl`/`responses.jsonl` 保留实际请求与响应。题目、判据
和来源标签只用于离线核查；生产选择不读取它们。

实现依据：已核对 DSH AgentLink 的 `src/file-lock.ts` 原始绝对截止时间循环及
`src/dsh-client.ts` 取消组合；这里复用原生 PostgreSQL 会话和调度器。数据库行为依据
[PostgreSQL statement_timeout](https://www.postgresql.org/docs/16/runtime-config-client.html)
及[表达式索引](https://www.postgresql.org/docs/16/indexes-expressional.html)。
