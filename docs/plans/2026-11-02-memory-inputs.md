# 有来源的资料记忆与上下文预算

用户于 2026-11-02 明确同意扩展现有来源契约。实现继续使用 SQLite、Memory、
Observation、原生 SDK 工具、付费账本及擦除闭包，不建立第二套检索或执行队列。

上下文默认软预算由 3200 调为 8000；显式配置不改。696 个冻结请求的离线重放中，
系统、工具及本轮输入的保守计量中位数为 4980；8000 与 16000 均能装下提名的六轮。
这只是历史保留实验，不代表答案准确率。现有 MOOD→BACKGROUND→OLD_HISTORY→RELATED→TOPIC
顺序、硬输入上限、来源证明和计费预留继续生效。

资料记忆沿用 owner 原事件作为正证据，另外固定资料引用及其完整来源证明：

- 文件使用已有成功解析 Observation；图像使用原生 SDK 的独立可见内容记录回执，
  只接受当前请求实际附带、签名验证过的原图，不能用助手最终回复代替解析。
- 语音只能从验证过的成功 ASR 回执形成转写快照，记录音频及结果哈希、片段和不完整标记；
  不重写 owner 的正文，不把机器转写当作用户逐字输入或授权。
- 引用、转发保留原始说话人和覆盖状态；助手原话只接受已经 SENT 的完整回复。
- 抽取候选可指定 evidence_reference_id，逐字引文必须属于那个经验证的资料引用。
  资料候选固定为 HYPOTHESIS、event_observed，内容带机器来源标记；不能与 owner
  自述自动合并或自动确认为用户事实。显式确认仍由原有主人操作负责。
- 输入引用及记忆引用为 runtime_meta 的可失效投影，绑定现有 MemoryContext 证明；
  保存、审核、读取重新核对来源，擦除注册随原有 Ledger 闭包清理。费用和失败记录保留。

研究沿用项目已记录的实现，并重新核对
[DSH compaction-basic](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/index.ts)
的原请求整体计量、来源范围与独立辅助调用回执，及
[DSH agent-loop](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-loop/src/agent.ts)
的模型/工具准入边界。复用现有框架能力，不引入依赖。

原714项、答案、标准与判卷规则保持冻结。分别报告抽取输入、召回、最终请求保留和答案；
新样例与原题库分开。累计 DeepSeek/Qwen 各30元，真实调用前核对账本并保守预留。

受控实测发现资料进入抽取输入后，结构化 JSON 仍会被原聊天的 768 token 输出额度截断。
沿用已有日记、反思的分场景输出配置，新增 memory_extract_max_output_tokens，默认2048，
范围128–4096，且取已批准 paid_model.max_output_tokens 的较小值；聊天额度不变。
预留仍按真实请求完整计量，严格 JSON 校验、失败重试和 UNKNOWN 账务语义不变。
修复前新样例12题仅通过3题，保留原答案和账本；后续同题重跑标作回归，另冻结新例。
第二轮仍有模型漏填引用ID、拼接引文而被校验舍弃的候选。抽取提示因此给出完整JSON字段结构，
要求先选择正文或资料证据，再复制引用ID及连续原文；解析器继续拒绝无支持的候选。
