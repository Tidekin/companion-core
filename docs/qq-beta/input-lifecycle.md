# 主人输入中断与撤回

本集成在 `live_text`、`automatic_reply=true`、`companion_scope="owner_private_v1"`
的已批准窗口内处理连续输入与撤回。实现已接入 Core HTTP v1、独立聊天 worker 和
OneBot Bridge；当前验收使用合成输入与 Mock 外部传输，未完成本轮 QQ 实机上线验收。
启用范围、截止、账号绑定、STOP 与额度仍由冻结计划控制。

## 新输入与已开始的责任

Bridge 持续接收并持久保存带签名的主人私聊输入，在等待模型、图片处理和分段间隔时
仍接收下一条消息。新输入优先于后台找图与下一段回复；不等待旧回复全部 ACK 才接收。

Core 每接受一个新的主人输入，在同一事务内推进会话与事件的 `reply_revision`，
取消旧普通聊天任务以及旧回复中尚未发送的 `PENDING/LEASED/RETRY_WAIT` 尾段。
相同输入的幂等重放不推进版本。结果发布、投递租用和最终派发都重新检查版本及来源。
Bridge 已收到但 Core 尚未入库的新消息，也会在最终派发边界阻止旧回复开始发送。
回复版本门禁只适用于主人入站事件产生的回复。活动消息和已接受任务的通知继续执行原有
来源与权限检查，不把活动事件的 revision 0 误判为过期主人回复。

已由服务端识别的记忆、日记与运行控制动作按原队列执行；新输入只抑制其过期回复，
不静默丢弃已经接纳的遗忘、暂停等操作。真正撤回控制命令来源时，未完成动作仍被取消。
控制动作的模型调用继续执行原预算、来源与租约检查；派发后的用量必须结算。

已进入外部传输的请求保留原责任：

| 当前阶段 | 中断后的处理 |
| --- | --- |
| 排队或尚未派发 | 取消旧任务/尾段；不把取消记录为成功发送 |
| 模型已预留、能够证明本次未进入 provider | 记录零用量结算及审计证据，保留已占用的调用次数 |
| 模型已派发 | 等待原请求返回并结算真实用量；旧结果不得发布为新回复 |
| 模型 worker 丢失或结果未知 | 预留到期后保留 `UNKNOWN` 与费用责任，不能以取消退款 |
| OneBot 已派发 | 等待真实 ACK；超时或断联保持 `DELIVERY_UNCERTAIN`，不自动重发 |

普通中断与撤回不主动 STOP 整个聊天窗口。已有模型或发送责任未结算时，新任务等待并发槽；
真实 UNKNOWN、独立账本失效或窗口到期仍执行原停止/对账约束。零用量结算也必须通过
独立付费账本；账本不可验证时保留 UNKNOWN，不能只修改数据库伪造结算。

上下文保留仍有效、因新输入被替代的未回答主人消息，以及经过完整性检查的连续 SENT
回复前缀。未发送尾段不作为助手已经说过的话。只有实际保留在最终提示中的来源写入
`job_sources`；当前输入也始终作为来源。

## 已签名撤回与删除重放

OwnerAuthority 与 live-text 两个撤回入口都使用 ApiStore 的独立连接。等写锁时，
其他 HTTP 请求可继续执行。撤回回执在原事务完成后返回，重复回执沿用同一删除记录。

Bridge 仅从认证 OneBot 接收链路处理当前机器人的主人私聊 `friend_recall`。
签名回执绑定窗口、账号身份摘要、`protocol_epoch`、源消息、发生/接收时间和捕获证据摘要。
Core 校验签名和源身份；标准 notice 不含消息方向，因此必须匹配已签名的主人入站捕获，
且不得与已知 outgoing `provider_message_id` 冲突。

先到达的撤回持久保存为 `PENDING_TARGET`。后续输入须与签名捕获和原消息身份匹配，
才能转为 `RECALLED`；入库入口返回认证的 `410 EVENT_ERASED`，不创建聊天任务。
已入库输入撤回后清除对应正文和派生内容，取消依赖该来源的未完成工作。
多个合法重复 notice 不重复删除或追加同一撤回责任。

撤回沿实际来源关系影响记忆、内容观察和任务输出；读取过某条历史的任务被取消，
不代表另一条主人原文自动成为待删除副本。旧窗口来源只能沿已接受的交接链回溯，
要求前驱为 `RETIRED`、后继指向正确、主人/机器人身份和协议 epoch 一致，使用原窗口
公钥验证原始捕获。

独立 `ErasureLedger` 先持久写入，再投影到数据库。恢复旧备份时重放墓碑与派生删除；
不能通过恢复旧 Core 数据库、旧 spool 或本地候选绑定使已撤回内容重新获得权限。
预算、实际发送凭证及 UNKNOWN 责任保留用于对账。Bridge 恢复后仍须与 Core 的输入状态、
撤回回执和最终派发检查一致；本地状态不能替代 Core 授权。

## HTTP v1 运维接口

以下接口沿用 Bridge 的内部认证，不是公开控制接口。事件 ID、租约及回执必须来自当前
持久记录；不要手工构造取消证明或在公开文档、日志中保存身份、密钥和私聊正文。

| 方法与路径 | 合同 |
| --- | --- |
| `POST /internal/v1/live-text/inputs/approve` | 原有输入审批与签名捕获；精确重放保留首次审批的内部隐私 epoch，无关撤回不改变审批正文 |
| `POST /internal/v1/live-text/inputs/recall` | 接收签名 `RecallReceipt`；返回 `recall_id/target_event_id/state/duplicate/reply_revision`，state 为 `PENDING_TARGET` 或 `RECALLED` |
| `GET /internal/v1/live-text/inputs/{event_id}/state` | 返回当前窗口内已批准输入的状态与未结算责任；未找到对应审批返回 404 |
| `POST /internal/v1/live-text/cancel-before-send` | 未派发主人回复提交窗口、消息、租约和 `reply_revision`；必需任务通知改用原 `content_sha256`，由 Core 核对原通知授权及来源失效；主动消息仅适用下述新输入抢占分支 |
| `POST /internal/v1/live-text/send-check` | 在原发送合同上核对 `reply_revision`；成功回执不越过最终本地 STOP/租约/新输入检查 |

输入状态回执固定包含 `window_sha256`、`event_id`、`input_sha256`、`reply_revision`、
`state`、`unsettled_model`、`unsettled_delivery` 和 `reason`。

| `state` | 运维含义 |
| --- | --- |
| `QUEUED` / `RUNNING` | 等待或生成中；审批已保存但尚未入库时也可显示 QUEUED，revision 为 0 |
| `REPLIED` | 回复已生成，不能据此宣称已发送 |
| `ACK` | 该输入所有回复投递均为 SENT |
| `SUPERSEDED` | 回复已被后续输入替代；还需检查两个 unsettled 字段 |
| `RECALLED` | 来源已经撤回；同样不代表已开始的外部责任已经结算 |
| `FAILED` | 任务失败，结合 `reason` 和原窗口停止/对账状态处理 |

Bridge 只有持有当前未进入传输 delegate 的操作，且操作、内容哈希与租约一致时，才能
调用取消接口。对于尚未 Core 入库的新输入，额外提交 `cause="OWNER_INPUT_PENDING"`
和 `pending_event_id`；Bridge 先重新验证排队输入的签名、主人来源及重复消息指纹。
这个原因只取消当前 delivery，不推进回复版本，不计 ACK，也不退发送次数。
对于主动消息，该分支省略 `reply_revision`，要求当前租约仍为 LEASED/SENDING，且原主动
投递凭证的窗口、消息、来源事件、内容摘要及投递键一致。没有经过验证的排队新输入，
主动消息不能用这条路径将未知结果改为 NOT_SENT。

成功取消回执为 `cancelled=true`、原 `message_id`、`state="CANCELLED"`、
`send_attempt_retained=true`。Bridge 将其写入 journal 和 spool 的 `NOT_SENT` 结果后才
继续处理新输入，并终结同一旧回复的所有未发尾段。替代输入自身在入库前被撤回时，
旧回复凭持久取消证明进入 SUPERSEDED，不等待新版本、不恢复旧尾段；已发送前缀保留。
进入实际发送后的异常不能走这条路径；缺少有效证明时仍保留不确定性。

必需任务通知没有主人输入版本。Bridge 必须证明本次 delegate 尚未调用，Core 必须匹配
原冻结通知的窗口、消息、租约、内容摘要和交付键，并确认来源已失效。来源擦除产生的
保守不确定投影可以凭此证明转为 NOT_SENT；已有实际发送结果或租约超时的不确定记录
不能降级。原尝试和模型用量继续保留，普通新输入也不能撤销已接受任务的通知责任。

## 验收与接续

Core 迁移 `0022_input_reply_revision` 保存回复版本及来源关系；已有使用记录禁止直接
降级丢弃这些证据。部署接续沿用[停止窗口交接](p10-stopped-window-handoff.md)和
[运维流程](operations.md)，配置/代码变更须重新冻结计划。

相关回归使用真实 API 生命周期和独立打开状态的 worker，外部模型、OneBot 和公共下载
使用 Mock。覆盖连续输入、已发送前缀、在途用量结算、撤回先于输入、历史交接、
发送前新输入/撤回、来源擦除和备份重放。需要复现时从仓库根目录运行：

```powershell
./scripts/check.ps1 -TestFiles @(
  'tests/test_reply_revision_api.py',
  'tests/test_reply_cancellation_accounting.py',
  'tests/test_input_protocol.py',
  'tests/test_reply_boundary_review.py',
  'tests/test_input_interrupt_bridge.py',
  'tests/test_owner_recall_bridge.py',
  'tests/test_owner_recall_bridge_media.py'
)
```

最终交接只链接同一冻结候选的 CI summary；已成功的相同候选检查无需重复执行。
合成通过不代表真实 provider 已结算、QQ notice 语义已在目标实例确认或线上窗口已启用。
实机验收须单独记录实际观察及回执，不能用本页替代线上成功凭证。
