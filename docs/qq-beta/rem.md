# 私人 REM 创作

REM 使用已有 Memory 事实生成明确标为虚构的私人创作，默认开启。
Life 也可把已结算的私人创作原文保存到同一 REM 记录中。
本模块已经接入原生 Core job、预算、擦除、Life selector 与主人 cognition 读取接口。
合成入口验收通过不表示已启用后台运行，也不表示真实模型或 QQ 已验收。

## 配置和授权

`Settings.rem` 使用 `RemConfig`。默认 `enabled=true`，每日预算为 `10000` micro USD
（0.01 美元），每月为 `100000` micro USD（0.10 美元），同时受原有全局费用上限约束。
可显式设置 `enabled=false` 关闭；已有配置中的显式关闭不会被新默认覆盖。
开启配置必须同时具备正的 `daily_micro_usd` 与 `monthly_micro_usd`。
执行还要求 live 模式、Memory 开启、当前主人授权包含 `rem` 与 `memory`，
以及与完整 REM 配置相符的 `capability_policies.rem` 指纹。
配置变更后必须重新完成原有授权流程，不能沿用旧指纹。

默认本地时间 04:00 后可调度，每个本地日期和时区最多创建一条记录。
从最多 20 条近期候选中选取最多 3 条有效、已确认、无冲突的私有 Memory，
并固定事实内容与完整来源证明。素材不足时安静跳过。
输入上限默认 2400 token，输出上限默认 600 token；来源与记录保留期默认均为 30 天。
有效来源保留期取 REM、主人授权、原始事件保留策略中最短值。

## Life 表达选择与保存

`LifeDecision.destination=PRIVATE_CREATION` 表示私人虚构创作或梦境。
主人 Life 源授权必须明确包含 `purposes=["PRIVATE_CREATION"]`，同时具备独立
`rem`、`memory` 用途、有效 REM 配置指纹以及已启用的 REM/Memory 配置，模型才会获得该目的地。
普通私人日记或聊天用途不能推导出创作权限。

原有主人控制接口 `POST /internal/v2/life/results/{job_id}/consume` 接受
`action=rem.save`、原 authority 和 `expected_content_sha256`。
该摘要仍是保留原文的 `sha({"text": text})`。自动消费需要 Life 源授权的
`automatic_actions` 显式包含 `rem.save`；默认列表为空。

消费只把原 Life 结果引用与来源闭包登记为原生 REM job，独立 REM worker
再次核验原 Life 结算、当前授权、内容摘要、来源、租约和主人输入优先级后原样保存。
此路径已在 Life 选择时结算费用，因此保存阶段不发起第二次模型调用，REM 记录的
`model_request_id` 为空；原 Life job 的账目继续保留。
作品标为 `FICTION/DREAM`，标题为“私人创作”，问题列表为空。

Life 保存与夜间生成共用本地日期/时区的唯一 REM 记录和开始时刻，不能另开每日配额。
重复消费返回原 REM job；终态失败、撤回与重启均不会创建替代付费任务。
记录保留期还受原 Life topic 的到期时间限制。来源通过原 Life 证明及原生
`job_sources`、`event_dependencies` 关联，擦除和旧备份重放继续使用原 ErasureLedger。

## 执行与记账

`rem.schedule_due(core)` 创建原生 `jobs(kind='rem')`，由
`Core.run_once(job_id=...)` 执行。原有预算预留、请求校验、来源检查、
主人输入优先级与 ErasureLedger 共同约束执行。
REM 不提供模型工具，每次生成最多一次模型调用。

模型可选择 `DREAM` 或 `SKIP`。服务器为结果标注 `kind=FICTION`，
探索问题标注 `state=UNVERIFIED`。结果不写入事实、观察、目标、日记或消息 Outbox。
`SKIP` 也保留已发生调用的原生账目。

无效结果失败后不重复支付；超时与调用后进程退出保留 UNKNOWN 责任，
重启不会重试该任务。UNKNOWN 仍受共享预算的对账限制。
已结算的无效 REM 结果不会阻止独立 Diary 任务执行。
发送前确认没有 provider I/O 的主人输入抢占，使用原有 `model.not_dispatched`
证据结算为零费用；预派发时间戳本身不证明 provider 已收到请求。

## 读取、遗忘与恢复

`rem.records(core, limit=20)` 返回任务状态、明确的虚构标记、来源 ID、
有效创作及原始 `model_usage` 摘要。外层控制接口必须验证主人身份。
`POST /internal/v2/cognition` 的 `kind=rem, record_id=<rem_id>` 在主人认证后读取精确记录；
该接口只返回 FICTION/UNVERIFIED 元数据、有效来源 ID 和原费用引用，隐藏标题、正文与问题文本。
它不会触发调度或模型调用，也不是作品正文查看入口。
来源失效、撤回或记录到期后不再返回正文、问题或来源列表。

来源遗忘通过原有依赖与擦除注册表清除 REM 私有内容。
记录自身到期时，`Runtime.expire_raw_events()` 只对 REM 派生事件写入
ErasureLedger；原始 Memory 与其来源不因 REM 到期而被删除。
恢复旧备份仍重放擦除，保留模型费用和不确定责任。

## 复用依据与验证边界

实现前检查了 DSH 的
[jobs-local 实现](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/jobs/jobs-local/src/index.ts)
以及 [Letta Memory 文档](https://docs.letta.com/configuration/memory)。
DSH 的执行前容量检查与终态结算约束用于对照检查；其进程内 job 存储不用于本项目持久化。
后台记忆整理的分离方式用于对照边界；本项目的 REM 产物始终是虚构作品。
持久化、恢复、权限、费用与擦除均复用 Companion Core 的既有机制。

Life 接入前另检查了 DSH
[schedule runtime](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/schedule/schedule/src/runtime.ts)
的持久化交付后提交、管理操作串行化与拒绝后不自动重试，以及 SQLite 官方
[事务语义](https://www.sqlite.org/lang_transaction.html)与
[唯一约束冲突处理](https://www.sqlite.org/conflict.html)。
对应复用本项目既有事务、REM 每日唯一约束、原 job 幂等键和终态责任；不复制 DSH 定时器。

`tests/test_rem_native.py` 使用真实签名输入审批和 TCP ingest、原生 Core worker、
原生 SDK/预算/擦除流程，仅模拟 provider HTTP。
`tests/rem_process.py` 在独立进程验证正常完成、调用后退出及恢复不重付。
`tests/test_rem_life_entrypoint.py` 补充真实签名 HTTP 输入到独立 Life worker、
原 selector 创建及执行 REM 的连续证据。
成功生成测试还验证 DREAM/SKIP 原生记录经真实 owner-control HTTP 按精确 ID 读取，
费用引用相同且没有正文泄漏。测试回执由集成交接文件统一记录；真实环境验收仍未执行。

`tests/test_life_rem_consumption.py` 验证签名 HTTP 输入、独立 Life/REM worker、
精确内容消费、独立用途拒绝、来源撤回和备份恢复不重付。
