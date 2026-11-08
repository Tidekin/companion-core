# 主人私聊生图

本文对应 backlog [F8](../design-future/p8b1-backlog.md) 的通用生图 Provider 离线合同。ComfyUI 自托管
workflow 是另一份协议，不在本轮范围内。`[image_generation]` 默认关闭；配置和合成检查都不构成生产激活。

## 入口与范围

模型在主人私聊回复中调用 `generate_image(prompt, size)`。只有同时满足以下条件，Core 才向模型提供该工具：

- `image_generation.enabled=true`，且 `owner_runtime.mode="ENABLED"`；
- 当前主人授权包含 `expression` 用途；
- 生图传输可用。真实 Provider 调用要等独立批准后才放开，在此之前
  `image_generation.available` 只接受注入的合成 `httpx.MockTransport`，与付费媒体输入保持同一道门。

每个聊天 job 最多调用一次。`size` 的 schema 只列出 `sizes` 中配置的尺寸。工具返回 `REFUSED`、`FAILED`
或 `UNKNOWN` 时不会发图片，模型改用文字回答。以下情况在请求前拒绝，不预留费用：
近六条主人消息或提示词命中 `expression.SERIOUS`、本日张数达到 `max_daily_images`、同会话仍在
`cooldown_seconds` 内、job 租约剩余时间不够覆盖 `provider_timeout_seconds`。

## 在后台切换地址和密钥

`image_generation` 可以在 Console 配置页修改，包括接口地址、模型、密钥、尺寸和额度，保存后不需要重启。
新配置在 job 领取时随运行时快照绑定（`runtime_config` 快照格式 3，密钥用 Console 凭据加密保存），
因此下一次对话生效，正在进行的回复继续使用原配置。改动 `endpoint.url` 时必须同时填写密钥；换到另一个
主机时不能沿用旧密钥。地址不变时，密钥留空表示保留原值。必须使用 HTTPS。
这些规则与 `content.vision` 的接口相同。快照只在配置与启动配置不同时才保存生图配置。

## Provider 协议

`HTTPImageGenerator` 调用 OpenAI 兼容的 `POST .../images/generations`，请求 `n=1`。
`gpt-image*` 模型不发送 `response_format`，其他模型要求 `b64_json`。只接受单个内联 `b64_json`，
不下载返回的 URL，因此只返回 URL 的服务（例如 SiliconFlow）不受支持。连接设置为不跟随重定向、
不读环境代理，并限制响应字节。结果必须通过 `companion_meme_protocol.validate_bytes` 校验，即
PNG 或 JPEG、不超过 4 MiB、单边不超过 2048 像素，与 Bridge 发送端使用同一界限。

## 费用与不确定结果

预留与现有媒体调用相同：在同一事务内写 `cloud_budget.reserve`、一行 `model_usage`
（`scenario="image_generation"`，按 `endpoint.max_request_micro_usd` 估价）和一行 `generated_images`；
I/O 之前调用 `mark_dispatched`。

| 结果 | `generated_images.state` | `model_usage.state` |
|---|---|---|
| 有效图片 | `SUCCEEDED` | `SETTLED` |
| 收到完整应答但不是有效图片 | `FAILED` | `SETTLED` |
| 超时、断连、非 200 | `UNKNOWN` | `UNKNOWN` |

`UNKNOWN` 不释放预留，也不重试。工具调用按 Pydantic AI 的持久化记录；重启后遇到 `STARTED` 的调用
直接报告结果未知，不会再次请求。与其他未结算付费调用一样，`UNKNOWN` 会挡住窗口的模型结转，直到核账完成。

## 投递与留存

图片字节只写入数据库旁边的 `<db>.generated-images/`，不进 SQLite、事件日志或备份。
Core 在最终回复事务中把引用 `{image_id, sha256, mime, byte_length}` 附到该 job 最后一段
`qq.private.reply` 的 `content.generated_image` 上。同时去掉同一条的表情图片，因为每条 QQ 消息只带一张图。
语音回复和过期图片不附加。

Bridge 只在 `OwnerOneBot` 路径接受 `generated_image`（`generated_image_scope`）。它持有该条 Outbox 租约时，
调用 `POST /internal/v1/generated-images/deliveries/{message_id}/asset` 取图；Core 核对租约、授权、内容哈希、
回复版本和图片哈希后才返回字节。Bridge 再次校验后，把文字和图片合成一个 OneBot 消息发送。
Core 返回 410（图片已过期或已被遗忘）时只发文字。开始发送之后的失败按现有规则记为 `DELIVERY_UNCERTAIN`，不补发。

遗忘源事件时，`generated_images` 改为 `ERASED`，清空提示词摘要和图片标识。费用记录保留在 `model_usage`。
`Core.recover` 调用的 `image_generation.cleanup` 删除不再可能发送的文件：已遗忘、已过期
（`retention_seconds`）或对应 Outbox 已离开待发状态。

## 验证

`tests/test_image_generation.py` 全部使用合成传输，覆盖以下情况：配置校验；重定向、只返回 URL、超大、
多图和连接丢失；端到端链路（工具调用、生图、结算、附加到 Outbox、租约读取、Bridge 校验和遗忘后拒绝）；
结果未知且重启不重放；各类拒绝不触达 Provider；缺少授权、开关或合成传输时不暴露工具。
通过原生 Bridge 发送路径验证图文合并、图片过期后只发文字，以及 ACK 丢失后保留投递不确定状态并停止发送。
