# 聊天来源校验性能修复

2026-11-10，本轮以主线 a3e51241 为基线，修复共享 Core 校验热路径，随后进入原生文本客户端开发。

## 改动

来源批处理遇到其他事务提交时，先丢弃原材料并在新快照上重建一次批处理。持续变化时仍使用原逐来源保守校验。重试不复用原权限、部分结果或 guard；自身写入、事务替换和跨异步边界继续失效。原提交、模型调用和投递处的复验保留。

持续提交耗尽两次批处理尝试后，Mood 在汇总完成、发布前对实际输出来源再次运行原有 lineage 校验，拒绝在首次读取后已撤回或遗忘的材料。回归用真实 PostgreSQL 提交稳定复现这一回退路径，不替换数据库快照或授权检查。性能断言观察原生快照，区分一次提交与并行测试产生的持续提交；一次提交的四次 custody 上限保持不变。

权限日志从原有完整字节校验结果生成有界、不可变的材料索引。每次调用仍读取当前文件并检查结尾一致性，每次仍校验当前数据库权限与原投影。复用的是字节解析与索引，不是已批准的权限或财务结果。原始字节变化、派生结果超限和错误不会沿用旧材料。财务追加、结算和完整对账继续走原 validator。

Bridge 的待回报结果路径保留完整验证，省去未使用的历史记录深复制。

## 验证与范围

真实 PostgreSQL 测试覆盖一个无关提交后的新批次、连续提交后的保守回退、自身写入拒绝、旧 guard 失效、权限和来源修改、遗忘及模型返回后的复验。八条真实合成历史遇到一次无关提交时，权限 custody 检查不超过四次，不随来源数量逐条重建。

独立的权限解析微基准固定同一数据，以九次调用比较原实现与候选。在约 2.27 MB、1000 条无关合成记录下，日志物化次数从九次降为一次；存在无关提交时，中位数从 8.90 ms 降为 5.91 ms。约 22.7 MB、10000 条记录超过既有 16 MiB 解析缓存上限，按设计继续完整解析。该对照只测权限读取，不代表完整财务回放、Core 聊天或手机客户端端到端时延。

相关检查：`tests/test_authority_material.py`、`test_source_validation_batch.py`、`test_owner_history_transaction.py`、`test_paid_journal_cache.py`、`test_bridge_owner_resolution.py`、`test_onebot_owner_resolution.py`，共 121 项通过。历史场景包含真实 HTTP、独立 Worker 和模拟外部模型传输。

完整财务回放、较大日志冷启动、持续事务争用和 Bridge 整体恢复仍有后续优化空间。平台首段延迟的目标需要真实链路采样；本轮不将局部优化换算为生产聊天加速比例。

PostgreSQL 的 [事务隔离](https://www.postgresql.org/docs/current/transaction-iso.html) 和 [快照信息](https://www.postgresql.org/docs/current/functions-info.html) 是批次新旧材料的依据；新批次仍按原来源与权限 validator 运行。

## 2026-11-11 权限投影与 Mood 复验

生产环境实测：一轮聊天里，worker 事件循环花 71 秒处理本轮。其中 `Core._check_context_snapshots` 占 47.8 秒，主要是 `MoodService.revalidate` 每次都完整重建 snapshot（约 2 秒，338 条 SQL）。`owner_authority.current()` 在 4 分钟采样里合计占 82.6 秒，每次调用都要聚合全部 167 条修订和约 347 KB 的 policy 文本。采样方式为 py-spy，采样期间 life-worker 已停。

改动：

- 授权读取只取 projection 字段和 head 指针的 SHA-256 摘要。同一份 journal 链、同一路径、同一摘要的组合，第一次在同一语句里读完整 projection，要求摘要一致后运行原 `_validate_projection`；之后的调用只复核 journal 路径别名。当前状态、过期、目的、journal 字节和生命周期比较每次仍然检查。任何 custody 失败都会清空缓存。复用的是"相同输入的校验结论"，不是已批准的权限。
- 授权链和 live lifecycle 在已有的 `_material` 索引里，每份 journal 字节只 fold 一次。每次检查拿 marshal 拷贝。链无效时记为空，admission 检查不受影响。
- Mood 在一次未变化的 `batch_sources` 读取内，按事件复用 lineage 和来源校验结果。快照或自身写入变化时，batch_sources 仍丢弃整批结果；批外不复用。

生产数据只读对照（新旧代码在同一事务快照里运行）：view、evidence 和 mood snapshot 完全一致，旧 proof 用新代码复验通过。`current()` 中位数从 58 ms 降到 28 ms，`evidence()` 从 44 ms 降到 23 ms，mood snapshot 从约 1.9 s 降到约 1.0 s。未做真实聊天端到端复测，不换算成整轮加速比例。

后续：每轮仍有约 10 次完整上下文复验，后台单元也和聊天共用同一个事件循环。这两项需要新机制（撤销日志式复验、聊天优先调度），按 AGENTS.md 先单独讨论设计。
