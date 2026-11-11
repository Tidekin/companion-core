# 单轮聊天延迟重构（设计稿，待确认）

状态：主人已于 2026-11-11 确认 B1 的语义变化，B1 已在 PR #445 实现。第一片（权限投影摘要、授权链 fold 一次、Mood 批内复用）语义不变；第二片 B1 改变了 Mood 复验的判定方式，详见下文。B2 暂不实施。

## 现状测量

生产环境，2026-11-11，py-spy 采样期间 life-worker 已停：

| 阶段 | 耗时 |
|---|---|
| 组装上下文 | 18–29 s |
| 发请求前的各道检查 | 约 20 s |
| 模型返回后写库收尾（after_model_request、save_result、run_once） | 约 24 s |
| 本轮 worker 主线程合计 | 71 s，其中 `_check_context_snapshots` 47.8 s |

`_check_context_snapshots` 里，`MoodService.revalidate` 占 33.8 s。每轮大约有 15 次复验：会话保存前后、每次模型请求检查、领取和送达前各一次。每次复验都完整重建 snapshot：读 74 个候选、做 55 次 lineage 查询、51 次来源校验、12 次历史授权 evidence。PR #445 之后，单次从约 1.9 s 降到约 1.0 s，每轮仍要约 15 s。

`Core.run_once` 是串行的：一轮聊天进行中，worker 不会运行 meme、poke 或 deadlines 单元。后台单元只会推迟下一条消息的领取（实测 2.6–9 s），不会拉长单轮耗时。所以"聊天优先调度"不列为本轮重点。退出 QQ 后，Bridge 对 `/owner-authority/check` 和 profile 状态的高频轮询也会消失。

## 方案 B1：按 proof 复验 Mood（推荐）

做法与 `MemoryContext.revalidate` 相同：只重新核对 proof 列出的内容，不从全部候选重建。

1. 头部状态照旧重新读取并比较：`enabled`、`paused`、`active`（expression 授权）、`reset_at`、`allowed`、`evaluated_at`。任何一项不同，就判 `PRIVACY_CHANGED`。
2. 对 proof 里的每个 signal，按它的 kind（feedback、interaction、watch、goal、appraisal，以及合并后的 experience）重新跑原有的单候选校验，重算 signal 并要求完全相等（包括 `fingerprint`、`source_event_ids`、`delta`）。实现上，把 `_collect_signals` 循环体拆成 `_signal_for(candidate, ...)`，收集和复验共用同一份校验代码。
3. 用 proof 的 signals 重新 fold，`immediate`、`undertone`、`expression`、`trend` 必须与 proof 一致。
4. `truncated` 为真的 proof 仍走完整重建，也就是保留原路径。
5. 在一次批量读取内完成所有检查，结束时照旧调用 `owner_authority.current` 和 `check_version`。

### 语义变化（需要确认）

原规则是：现在重算的 snapshot 必须和 proof 完全相等。B1 的规则是：proof 用到的每条证据都仍然有效，且头部状态没变。

区别只有一种情况：proof 生成后，某个原本被排除的旧候选变得可以计入。例如：
- 一条 `DELIVERY_UNCERTAIN` 的回复后来确认为 SENT，对应的反馈就能算进去了；
- 授权恢复后，某个来源重新可用。

原规则会把这种情况判为 `PRIVACY_CHANGED` 并中止本轮。B1 会继续使用旧 proof，也就是少用了一条新近可用的证据。这不会多用任何已撤回、已遗忘或越权的数据。撤回、遗忘、erasure、redaction、授权停止或过期、reset、pause，都会让相应的 signal 或头部状态校验失败，与原规则相同。

`at` 是固定的：proof 生成之后新到的事件、评分和目标，本来就不在候选范围内，两种规则一样。

### 预计收益

每次复验只处理 proof 里约 15 个 signal、约 30 个来源。生产数据只读实测：proof 含 15 个 signal 时，复验从 2095 ms（原实现）降到 709 ms（第一片与 B1 合计）。剩余时间主要花在约 116 条查询上，其中来源校验 `_verify` 和历史授权 evidence 约 250 ms。整轮效果需要真实聊天采样确认。

### 测试

- 每种 signal kind 各撤回一次证据（erasure、redaction、遗忘、授权停止、reset、pause、outbox 状态回退、watch 或 goal 失效），B1 都判 `PRIVACY_CHANGED`。
- 新近可用的候选不导致失败（这是新语义，单独写测试固定下来）。
- 新旧实现在同一快照下对同一 proof 的结论，除上述情况外完全一致。生产只读对照沿用 PR #445 的做法。

## 方案 B2：撤销日志（备选）

新增一张只追加的撤销日志表。所有撤回类操作（erasure、redaction、遗忘、授权变更、reset、pause）在同一事务里写入，用触发器保证不漏。复验时只查一条："proof 生成之后，有没有撤销涉及我的 source"。

- 优点：复验几乎是 O(1)，对 Memory、history 等其他快照也适用。
- 缺点：需要迁移；撤回入口分散在十几个模块里（`privacy`、`memory`、`goals`、`content`、`observation`、`owner_authority` 等），漏掉一个就是隐私漏洞；还要单独证明触发器覆盖完整。
- 建议：先做 B1。B1 之后如果复验仍是瓶颈，再评估 B2。

## 不做

- 跨事务缓存"已批准"的结论。
- 减少复验点。每道边界照旧复验；B1 只是让每次复验变便宜。

## 需要主人确认

1. 是否接受 B1 的语义变化：proof 生成后才变得可用的旧证据，不再让本轮失败。
2. B1 完成后，是否继续评估 B2。
