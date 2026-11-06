# 原文分段续发

主人明确要求补发时，Core 可复制已确认未派发的纯文本回复尾段。先完成原生 STOP、
无 I/O 对账及 Bridge 交接，再由控制台发起新操作。原回复组、已送达前缀、
旧租约、原模型结算与 NOT_SENT 回执均保留；这项操作不运行模型。

同一原回复组只能创建一次续发任务。新的 `reply_recovery` 生产者、事件与 Outbox
身份在同一 SQLite 事务中建立；操作请求与已选原文摘要绑定。重复 HTTP 请求及
提交后丢失控制台响应均读取原操作结果，不创建第二份投递。不同幂等键也不能重复建组。

## 控制台入口

登录原有主人控制台后：

1. `GET /v1/console/deliveries/{原消息ID}/recovery` 返回预览：原文尾段、
   跳过的已送达消息、来源消息及 `expected_sha256`。
2. 用原有 Cookie、Origin、CSRF 与 `Idempotency-Key`，向同一路径 POST
   `{"expected_sha256":"预览摘要"}`。只接受摘要，不接受替换正文。成功返回 `QUEUED`。
3. 再读预览可取得新任务和新消息 ID。沿原 Outbox 租约、begin-send、send-check、
   OneBot ACK 和结果回写路径发送，逐段检查实际送达。

第一条未送达消息必须有已提交的原生 NOT_SENT 回执，或已有签名的过期 preflight
无派发证明。后续尾段只能是顺序门禁下未领取、零尝试的取消记录。未知是否送达、
已派发、缺失证明、非连续 SENT 前缀、内容变化、语音、图片和阅读工具结果均拒绝。
本入口只支持 2–100 段的原纯文本组，已有前缀且至少一段待补；单段尾部沿单消息投递。
新旧责任合计仍须满足原生输入回执的 100 条上限，超限在建组前拒绝。

新任务取得当前主人聊天权限，同时独立核验原签名输入、原模型 admission/结算、
冻结上下文、历史、记忆与正文摘要。历史授权不会恢复旧任务的执行许可。
原输入可以是已结束的历史聊天；发起续发时固定当前会话 revision。之后的新输入、
撤回、擦除、权限变更或来源变化在后续发送检查中阻止未发部分。
新投递保留原分段间隔，每次发送仍走现有累计额度预留；原费用和发送责任不退款。

历史读取将原 ACK 前缀和新 ACK 尾段合成完整原回复，每段保留实际发送组的来源包络。
实际投递记录仍为两个独立组。尚未完成的续发不会被当成完整历史回复。

## 依据与验收

2026-11-06 复核 [DSH jobs-local 原实现](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/jobs/jobs-local/src/index.ts)：
原 producer 负责提交终态，控制者读取保留投影。续发使用独立生产者，保留原终态。
复用现有 SQLite Outbox，而非引入发送旁路；原子提交、顺序和重复处理原则见
[AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)。
供应商已接收而 ACK 丢失仍按现有不确定性对账，不能从超时推断未发送。
Core 预发送 RPC 超时或连接中断均复用原签名的过期预留证明；不重复不明确的 POST。

`tests/test_reply_recovery.py` 经真实认证 WebSocket、Core HTTP、独立 worker 与独立对账
进程覆盖：部分送达后的三段原文续发、原 ACK 不变、重复请求、提交后响应丢失、
缺失/不匹配的无 I/O 证据、重启、擦除中断与历史投影。外部供应商 transport 使用合成响应。
