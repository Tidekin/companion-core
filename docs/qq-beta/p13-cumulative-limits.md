# P13 E2：原责任账本上的累计限额

当前金额和费用 UNKNOWN 的运行规则见[模型费用规则](../model-cost-policy.md)。下文保留
原累计批准与责任账本的结构；其中金额只作统计，费用 UNKNOWN 不造成全局暂停，
实际并发及其他非金额控制继续生效。

本包实现离线可验证的累计限额，当前默认关闭。启用必须另有主人批准的账户、费用、
候选、配置与窗口材料；本次实现和合成回归不构成真实运行授权。
原窗口最多五次模型尝试、五个输入及五次发送的限制继续有效。
不得用自动创建或续签小窗口替代较长日常 Beta 的独立批准。

这是**本地责任上限**：无法观测同一供应商账户在其他程序中的费用，也不保证供应商硬封顶。
`account_ref`、账户和 Key 归属依据均是主人批准的匿名引用，代码没有向供应商验证账户。

## 批准合同

[累计批准模板](p13-cumulative-policy.template.json) 的空身份、空时间、零上限不能启用服务。
完整对象必须同时出现在 `paid_model.cumulative` 与 `live_text_window.cumulative`，并进入原
Bridge `live_text.cumulative` 批准范围；三者使用相同规范 JSON。没有累计范围的旧窗口
序列化不增加空字段，原批准摘要保持原义。已经登记累计账户后删除范围不能退回旧路径。

| 字段 | 含义 |
|---|---|
| `version=1`、`policy_id`、`approval_sha256` | 本次累计合同及独立新批准依据；续签 ID 和批准依据均不得复用 |
| `account_ref`、`provider`、`currency`、`timezone` | 稳定匿名账户域，登记后不得改绑；当前金额单位为 micro-USD |
| `account_evidence_sha256`、`key_ownership_sha256` | 私有归属材料摘要；换 Key 必须增加新的 Key 归属依据 |
| `credential_sha256` | `cumulative.credential(settings, account_ref)` 生成的账户域 HMAC，跨窗口不变；不把 Key 写入账本 |
| `owner_identity_sha256` | 固定主人与机器人身份摘要；实际收件人仍由原 owner/private 投递合同约束 |
| `starts_at`、`expires_at` | 有时区绝对时间，包含每个原 paid/window 的授权区间 |
| `expected_journal_head_sha256` | 独立审阅时原 Budget Journal 的准确最新 head；有新责任后原批准失效 |
| `predecessor_policy_sha256` | 首次登记为 null，续签必须指向当前完整 policy 摘要 |
| `legacy_liabilities_sha256` | 首次登记时准确旧责任库存摘要；续签保留原已折叠责任，不重新导入 |
| `legacy_adoption_evidence_sha256`、`legacy_window_inventory` | 旧责任归属批准与原 Window Journal 时间库存，见下文 |
| `request` | 逻辑请求级金额、模型尝试、输入 Token、输出 Token 上限 |
| `window`、`day`、`month`、`account` | 各范围同时限制上述四项，以及主人输入、发送尝试数 |
| `concurrency` | 模型 RESERVED/UNKNOWN 共同占用的并发上限；原更严并发限制仍有效 |

单次模型原上限、逻辑请求、窗口、自然日、自然月、账户、并发和输入输出同时检查，最严者生效。
Vision、memory review 和 chat 在各自原批准允许时共用这些计数。不存在累计范围自动开启功能的路径。
自然日/月由登记后固定的 IANA 时区划分；责任记录保存 UTC 原预留时间。
跨午夜/月后的迟到 usage 结算仍归原预留周期。模型/发送若在实际 HTTP 派发前跨越预留的自然日，
最终边界拒绝 I/O，保留已占用责任；捕获收到至持久化之间跨日同样拒绝。

## 首次登记与旧责任迁移

先停止并结清原窗口。原 Budget Journal、完整 Window Journal 链、Spool 和最新 ErasureLedger
须共同保管；不使用数据库复原动作代替责任对账。UNKNOWN、未完成预留、发送不确定或无法归属的旧责任
使登记/续签失败。此前 provider 不同的模型记录也不能静默计入新账户。

旧输入和发送的时间必须来自原 Window Journal，不能使用最后的 Outbox `updated_at`、ACK 时间或
导出时间猜测原尝试属于哪一天。原 Bridge 已停止、所有输入和发送均 ACK 后，可以在原私有配置下离线运行：

```powershell
python -m companion_onebot_bridge --config PRIVATE_CONFIG window-quota-inventory
```

该命令不联网、不调用模型、不发送消息；输出原完整 Window Journal/Spool 的匿名源摘要、
窗口、原预留时间、记录/head 摘要以及现有 Bridge capture key 的签名。没有原 capture key、
历史缺失、任何未决尝试、丢失/多出的 Spool 投影均拒绝。输出仍是私有审批材料。
Core 使用批准的 `capture_public_key`、owner/bot 摘要和原 Budget Journal ID 验证库存；
每个旧输入和每次旧 Outbox 尝试必须恰好匹配，不允许漏项、多项或重复项。
没有足够时间依据的旧数据需要独立恢复/归属处理，本实现不猜测或自动清账。

审批工具可用 `cumulative.LegacyWindowInventory.model_validate(...)` 读取签名库存，
再用 `cumulative.legacy_liabilities(store, provider, inventory)` 获取规范旧责任对象，
`cumulative.digest(...)` 得到 `legacy_liabilities_sha256`。仅包含旧模型责任时可省略 Window 库存。
审批者必须审阅归属材料、库存、原 head 和所有上限后才物化完整合同；库存签名本身不授权扩大额度。

首次 `quota_enroll` 和以后 `quota_renew` 只追加到原 Budget Journal。新的窗口交接仍走原
Core prepare → Bridge retired/prepared → 单独 open，不自动复活已停止窗口。
旧 Window Journal/Spool 计数继续参与账户、日、月上限，当前窗口为零不代表累计额度为零。

## 准入、暂停与控制面

模型在原 SQLite `BEGIN IMMEDIATE` 内核对账本，原 reserve/fsync 完成后才允许实际 provider I/O。
原 Budget Journal 的 reserve/io_started/settle/blocked 记录携带累计维度；
Core 的输入批准和 final send-check 追加保守、幂等的输入/发送责任。
Bridge 仍是输入捕获、Spool 和实际发送的原责任边界，读取完整 Window Journal 链核对所有对应 Spool 行。
Core 的影子计数不授权 Bridge 发信，也不替代任何原签名、单条批准、收件人、租约或发送状态检查。

原账本新记录包括 `quota_enroll`、`quota_renew`、`quota_pause`、`quota_operation`、
`quota_send_result`，均 `version=1`。SQLite 只保留 policy/路径绑定与已提交控制 head 的见证，
不保存另一套余额。恢复合法旧日志前缀若丢失已提交见证会停止；fsync 已发生而 SQLite 回滚时，
较新的暂停/保守占用继续生效。原 Store 会先完整回滚业务，再单独保存已 fsync 暂停的只增 head 见证，
包括嵌套调用触发到期/时钟错误的情形，不提前提交外层业务。
只读状态显式禁止初始化账本，缺文件/身份时仅报告缺失，不生成新 header。
跨账本中断不退还次数或估算上界。

UNKNOWN 保留金额、输入/输出 Token 和并发责任，不因廉价迟到 usage、Key 轮换、新窗口或新周期退款。
确认的较大费用仍可在暂停后记录。超过累计 Token/金额上限的实际 usage 导致 UNKNOWN 和停止。
发送未知保留原尝试数并暂停；本包没有清除 UNKNOWN 或发送不确定的自动恢复接口。

以下入口沿用原内部 HTTP v1 鉴权：

| 入口 | 行为 |
|---|---|
| `GET /internal/v1/cumulative/status` | 只读 policy、账户、固定时区/币种、各范围 used/limits、暂停原因；不返回 Key 或本地路径 |
| `POST /internal/v1/cumulative/pause` | 请求携带当前 `account_ref`、`policy_sha256` 和 `evidence_sha256`，只追加暂停 |
| `POST /internal/v1/live-text/stop` | 原 STOP 继续可用，并暂停当前累计 policy；旧 STOP 重放不暂停 successor |

暂停、STOP、到期、时钟倒退、身份不明确时拒绝新输入/模型/发送；捕获在 awaited receive 之后、
持久化之前再次核对 Core。实际 provider/QQ HTTP 边界在用户 hook 后重新核对，不靠较早的状态快照放行。
已停止时仍可读取累计状态、执行原 STOP、记录已有责任的结算；撤去运行批准不撤去会计保管责任。
续签必须新批准、新 policy ID、新批准依据、正确 predecessor 和准确最新 head，不设自动续签入口。

## 离线故障验证与发布边界

`tests/test_cumulative_budget.py` 覆盖所有模型维度/范围、固定时区午夜与跨月、Key/账户绑定、
双进程争抢、fsync 后中断、UNKNOWN/迟到 usage、实际 Token 超额、合法旧前缀、到期后倒时钟、
旧数据库配最新/缺失账本、最终 HTTP hook 暂停及跨日派发。
`tests/test_cumulative_window.py` 覆盖所有输入/发送范围、原责任幂等、当前暂停、控制面、
Spool 丢行、显式续签、完整两窗口交接、带签名旧时间库存的迁移，以及真实 loopback HTTP 和
认证 WebSocket 收到后暂停。所有身份、模型、协议回执和日期推进均为合成证据。

```powershell
./scripts/check.ps1 -TestFiles tests/test_cumulative_budget.py,tests/test_cumulative_window.py
```

还必须对准确最终 HEAD 运行原 paid gate、图片、capture、handoff、恢复/隐私回归和完整 CI，
经独立代码审查及主人专项批准后，才考虑新的较长私人 Beta。
这些测试不证明手机可见、真实账户费用、真实日记审阅或真实 3/7/14/30 日持续体验。
恢复要求最新三个原责任链和当前隐私/身份/schema 同时验证；全套账本及数据库一起被回退的情况
仍需外部保管的最新 head/备份清单发现，不能宣称单机代码可识别所有外部副本回退。
