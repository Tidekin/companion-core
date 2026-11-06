# 六请求无投递实验

此用途在原 Core 数据库中执行固定上下文比较，使用原 worker、model_usage、budget
journal 和 cloud/global 预算。`evaluation_enabled=false` 为默认值。部署配置变更、真实
付费调用与私密对话向 provider 传输仍需明确授权；离线检查通过不等于已获授权。

## 控制流程

若当前已接续的授权保留历史 UNKNOWN，可在停止且当前输入全部终态后，通过原
OwnerAuthority preview/activate 单独新增 `evaluation` 用途和新的授权摘要。
付费合同、窗口、期限、carry 引用及其他用途和来源范围保持不变；Bridge 使用普通
prepare/handoff 完成交接。具体约束见[独立授权修订](../qq-beta/p0-model-carry-contract.md#independent-evaluation-permission-revision)。
此步骤与启用 `evaluation_enabled`、下述每份计划的预览和执行同意分别核对。
授权修订不提高额度：每次调用沿用现有 paid 合同的 `per_attempt_micro_usd` 和
`max_output_tokens`；同 job 的累计额度仍按下表限制，并与日/月/窗口限制共同生效。
提高模型价格或输出长度必须有匹配的原 paid 合同，不能仅修改实验计划绕过准入。
主人 pilot 额外保留每次 5000 microUSD 和输出 2048 tokens 的更严子上限。

以下接口均使用现有 owner `control_token`，Bridge token 无权调用。服务不提供公共模型
代理。操作材料与结果只在原私有运行目录或经检查的本地私有目录保留；不进入 Git、
共享终端日志或公共附件。所有 endpoint 地址均相对当前 Core。

1. 准备 manifest。`plan` 是现有离线 prepare 工具生成的原始六请求 JSON（含自身
   `plan_sha256`），不修改它的 provider payload。另外给出 `source_event_ids`（1–12
   个仍有效且属于同一 owner/private 会话的原始事件）、`baseline_sha256`（本地基线
   receipt 文件字节 SHA256）和 `source_document_sha256`（原始可见对话文件字节 SHA256）。
   这两个文件哈希是审阅材料的身份，不冒充历史完整 provider 请求或自动证明所有事实。
2. `POST /internal/v2/evaluations/preview`，body 为 manifest。返回整个 manifest 的
   `manifest_sha256`、6 次/$0.05 限制和 `consent_scope`。此步骤不授予权限、不预留费用、
   不执行模型，也不新建实验 job。审阅者同时核对私聊范围、payload 与事实/历史一致性。
3. 获得具体真实执行授权后，通过原 `POST /internal/v2/consents` 提交返回的
   `consent_scope` 加 `duration_seconds`（不超过3600）。其能力为 `model.evaluate`，
   device 为 `core.evaluation`，session 为精确 manifest hash。不要拿另一个 session、
   旧 consent、旧 paid window 或仅有“代码实现授权”代替本次实付/私密传输授权。
4. `POST /internal/v2/evaluations`，body 为 `{"manifest": ..., "consent_id": ...}`。
   返回 `evaluation_id` 与原 `job_id`。同一 manifest 重复提交复用该 job，不重新生成。
   原 worker 的 `Core.run_once` 处理 evaluation job，普通聊天优先。
5. `GET /internal/v2/evaluations/{evaluation_id}` 读取状态和六项结果，响应带
   `Cache-Control: no-store`。没有调用的项明确为 NOT_STARTED；已计费但缺失结果的项
   保留原 usage 与空文本，不重新付费补齐。
6. 取消使用原 `POST /internal/v2/consents/{consent_id}/revoke`。它取消关联实验 job，
   每次准入及最终 dispatch fence 也复核当前 consent、来源、租约和窗口。全局 LiveText
   STOP、到期或窗口变更仍有权停止实验；这个用途不能绕过原 guard。

标准 manifest 结构：

```json
{
  "plan": {"完整原准备计划": "在私有材料中替换整个plan对象"},
  "source_event_ids": ["evt_原主人事件"],
  "baseline_sha256": "基线receipt的64位小写SHA256",
  "source_document_sha256": "原始对话文件的64位小写SHA256"
}
```

示意值不可直接运行。预览会拒绝不完整、超过120KB、包含凭据、模型/费率不匹配、
非纯文本、共享上下文不同或改写 payload 的计划。主人 pilot 限定一个 case 的三组重复，
每组 baseline 与 participation_v1。

## 合成计划（`kind: "synthetic"`）

合成计划只包含仓库中 SYNTHETIC case，由现有 `prepare` 以 schema v2 生成，manifest 写
`"kind": "synthetic"`，`source_event_ids` 必须为空；主人 pilot 不写 `kind`，其 manifest
哈希不变。两类计划共用同一 consent、OwnerAuthority `evaluation` 用途、原 paid 合同与
日/月/窗口限制，以及 paid 合同的每次费用和输出上限。

| 限制 | 主人 pilot | 合成计划 |
|---|---|---|
| 请求数 | 6 | ≤216（每个 case 的全部重复×两种策略，缺一对即拒绝） |
| case 数 | 1 | ≤36，每个 case 一个共享上下文 |
| 合计规划额度 | 50000 microUSD | 1000000 microUSD，预估超过即拒绝 |
| 每次输入估计 | ≤2400 tokens | ≤8000 tokens |
| manifest 大小 | 120KB | 1.5MB |

合成计划没有主人来源和会话修订绑定，因此主人新消息不会中止它；事件挂在主人私聊会话
下仅用于原 job 结构。worker 每次领取最多执行 6 个请求，之后把 job 放回队列，普通聊天
优先领取；续跑前要求此前每次 usage 都已 SETTLED 且结果条数一致，否则以
`EVALUATION_PREVIOUS_ATTEMPT_REVIEW_REQUIRED` 停止，绝不重放已发出的请求。
迁移 `0049_evaluation_ordinals` 把 `evaluation_results.ordinal` 上限从 6 放宽到 216。

## 预算和不确定性

原 paid 合同、月/日/窗口限制、并发限制和 CNY 全局政策先后在同一权威 Store 的原事务中
校验；主人 pilot 另外最多6次、合计50000 microUSD、输入估计最多2400 tokens；
合成计划沿用上表的次数、总额和输入限制。每次估计和输出长度分别不能超过现有 paid
合同的 `per_attempt_micro_usd` 和 `max_output_tokens`。没有第二个余额、wallet、预算同步
服务或独立 executor。
如果已用费用、其他任务的 RESERVED/UNKNOWN、FX 到期或原窗口限制不允许，实验失败。

次数、顺序和子费用上限与原 reserve 同事务校验。usage 按原计费规则结算；实际超过实验
金额限制会保留更大的实际责任并记为 UNKNOWN，停止后续调用。这里的金额是配置费率下的
规划额度，不是 provider 对账单保证。绝不为了满足“6次完整”自动重试或新建另一份计划。

进程退出、租约到期、结果落盘失败后，已尝试的 request ID 保留。旧 job 不续跑；过期
RESERVED 通过原 cancellation drain 结为 UNKNOWN。普通 owner 新消息使已冻结会话
状态变化，后续实验请求停止；不让实验抢占新对话的响应职责。

## 来源、输出与擦除

提交时记录原 owner 事件摘要、已送达回复身份、会话状态、隐私 epoch 与原 paid/window
绑定。每次请求再核对；未送达片段不能被描述为历史。payload 是经审阅的可见历史重建，
服务不会声称它等同于当时的历史完整模型请求。

实验事件标记 memory_processed，输出只保存于可擦除的 evaluation_results；不会新增
Outbox、memories 或对话回复。来源失效禁止取回；源擦除清掉 manifest 和所有结果文本，
保留原财务记录。注册 DerivedCleanup 后，旧备份也通过原 erasure ledger 重放清除。
恢复备份会取消未完成实验，不恢复过期授权或重新发起已尝试请求。

结果区分 `MODEL_RESPONSE` 与 `SYNTHETIC_MODEL_RESPONSE`，mock 数据不能变成真人反馈。
只有 SETTLED、非空且未经凭据脱敏、finish_reason 为 stop 的项标记 `complete_response=true`。
输出若被脱敏会标记 `text_redacted=true`，不能冒充未修改的完整模型原文。
截断/缺失结束原因、失败、UNKNOWN 和未启动项都留在完整报告中。现有离线盲评工具的
文本配对规则还需人工遵循此完整性标记；不能将截断项按普通完整回复评分。

## 离线证据与发布边界

`tests/test_evaluation.py` 覆盖 scope、同库预算、撤回、UNKNOWN、重启和旧备份擦除；
`tests/test_evaluation_process.py` 使用真实 TCP 控制 API、两个独立进程原 worker、
同一 Store/journal 和 HTTPX MockTransport，验证原样 payload、跨 worker 竞争、
派发后进程退出、LiveText 与 owner-private 组合及零实验投递。模拟用凭据均为合成值。
这些证据不构成生产部署或真实收费授权。

本次复用依据、模块归属与批准范围见
[准入设计记录](../plans/2026-10-24-evaluation-admission.md)。

## 官方 OpenAI 图像评测

官方 `https://api.openai.com/v1` 路由使用 `max_completion_tokens`，请求测量、
冻结 payload 与实际 dispatch 保持同一个输出额度；其他兼容服务保持其原字段。
官方 `prompt_tokens_details.cached_tokens` 进入原 usage 结算；缺失时仍按原不确定性规则处理。
DeepSeek 的 evaluation 复用已有 `chat_thinking`，可与部署中的推理档位一致；
后台 JSON 任务保持其原推理设置。对比报告须列明各模型推理档位与输出额度。

依据 [OpenAI Chat Completions 官方参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
和[官方价格](https://developers.openai.com/api/docs/pricing)。本改动不启用付费评测或变更聊天路由；
实际评测仍要求匹配的主人授权、原生 consent、固定 manifest 和原 budget journal。
