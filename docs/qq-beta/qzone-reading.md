# QQ 空间授权读取与审阅草稿

2026-10-31：首个切片已实现。Core 记录主人选定的空间账号，隔离 Reader 读取该账号最近一条
说说的正文，再按单独的来源用途许可形成主人可审阅的 Life 空间草稿。配置默认关闭，M0/M1
继续不执行 QQ 空间。当前验收使用模拟平台传输；真实账号与现网兼容性仍需单独验收。

## 启用与目标范围

后续阶段部署须同时具备：

- `qzone_reading_enabled=true`、`observation_enabled=true`，以及现有的 `content_enabled`
  或 `exploration_enabled` 准入。仅增加开关不授予来源或账号动作权限。
- 一个 `source="qzone"`、`source_grant_required=true`、`media_analysis=false` 的独立
  `observation_adapters` 配置和专用 Reader token。
- 当前原生 OwnerAuthority 的 `chat`、`content` 与 `social` 用途；目标须关联当前合法的原始
  主人输入。STOP、恢复代际或授权修订会使原任务失效，旧任务不重绑到新授权。
- 按[来源授权合同](../source-grants.md)注册的有限期 SourceGrant。账号文件和原生目标
  必须绑定同一原始授权修订，不能用另一个合法 grant 领取旧目标。

目标选择只接受规范地址 `https://user.qzone.qq.com/<目标账号>`，每次最多读取最新一条说说。
Core 以原 `exploration_requests` 保存目标、原始来源、授权和任务状态；`reading_attempts`
保存尝试及结果，`audit_log` 仅保存选择摘要。本切片没有自动关注列表或新的社交记忆库。
目标从创建起最多有效五分钟；实际读取租约最多三十秒，并受原来源授权期限约束。

## 账号文件与独立 Reader

复用隔离 Reader 的账号文件检查、来源授权元数据和全文件指纹。账号文件字段为
`enabled`、有时区的 `expires_at`、`cookies`、`upstream_commit` 和 `authorization`。
`authorization` 使用已在 Core 注册的 `grant_id`、`epoch`、`fingerprint`；它们是独立随机标识，
不是 Cookie 摘要。固定 `upstream_commit` 为 `c0d1f293f1bfa32a830473f18e6d50e2d5e070d6`。
Cookie 只接受 `uin`、可选的 `p_uin`、`skey`、`p_skey`；须有账号 `uin` 和至少一个 key。
文件留在隔离 Reader 的私密挂载中，沿用文件权限和禁止符号链接的检查。

```sh
python -m source_reader --source qzone --protocol reading-v1 \
  --account-file /PRIVATE/qzone-account.json --adapter-id qzone \
  --core-url https://core.example --once
```

`COMPANION_READER_TOKEN` 使用该适配器的专用凭据。QZone 不提供无 Core 租约的 probe，
也不接受 legacy 协议或浏览器降级。每次请求使用固定 HTTPS GET、公共地址 DNS 固定、
无重定向、单请求/字节上限和实际发送边界的 Core 复核；凭据及令牌查询参数不进入证据或日志。
公共传输仅解析一次并先检查全部 DNS 答案；若某个公网地址的 TCP 连接失败，在同一总超时内
尝试该快照中的下一个地址，保留原 Host、TLS SNI 和证书验证，不重试已发送的 HTTP 请求。
只解析作者、tid、发表时刻和正文，不获取图片、访客、联系人，不另发评论读取请求。
上游响应附带的评论或媒体字段不进入持久资料或模型上下文。
响应可为纯 JSON，或由单个完整的 `_Callback(...)` / `callback(...)` 包装的 JSON；
包装仅按数据解析，额外脚本和未知回调拒绝，证据仍摘要原始响应字节。

## Core 操作

以下操作使用主人 control Bearer 凭据；Bridge/Reader 凭据不能选择目标或读取审阅资料。

| 操作 | 合同与结果 |
|---|---|
| `POST /internal/v2/qzone/targets` | `operation_id`、`adapter_id`、规范 `url`、原始 `source_event_id`、完整 `source_grant` 快照；返回 `request_id`。重复原请求返回原任务，不重新读取；同操作 ID 更换目标、来源或 grant 拒绝 |
| `GET /internal/v2/qzone/targets` | 最近三十条未擦除目标的状态、有效期、原 grant 修订及结果 `event_ids`；不返回 Cookie 或原始主人输入正文 |
| `GET /internal/v2/qzone/sources/{event_id}` | 验证原生证据与授权归属后，返回正文摘录、作者/tid/发表时刻、来源指纹，供主人审阅和填写精确草稿许可；不触发生成或发送 |
| 原 `/internal/v3/readers/{adapter_id}/lease`、`validate`、`result` | 独立 Reader 复用领取、每次 I/O 复核、类型化证据、真实流量计量和幂等回传 |

读取只形成 `FETCHED / latest_post_text` 证据，包含原授权、原任务与来源、作者、tid、
发表/读取时刻、原响应摘要与字节数。不能据此声称看过图片、读过评论或完整空间历史。
网络中断保留 UNKNOWN 与原流量责任；无权或迟到的正文不入库，原尝试仍可提交用量责任。
关闭读取开关停止新 I/O，不等于删除此前合法历史。

## 草稿与私人生活隔离

主人使用上述 `source_sha256`，在现有 OwnerAuthority 的 `life_shadow` 中为对应事件配置
`LifeQZoneSourceGrant`：`purpose=qzone_draft`、精确事件/指纹、grant 身份/修订、
有限期、`audience=owner_only` 和原主人身份。`source_event_ids` 只包含拟使用的空间来源；
可以将 `purposes` 设为空列表，仅授权这些 QZone 草稿。空用途且无草稿许可仍被拒绝。
沿用现有 Life SHADOW 配置、活动模型预算与原生 Life Worker，未设置自动动作时只生成审阅产物。

空间来源不能与主人私聊、日记或生活记忆混入同一个 Life 决策。空间决策上下文不加载
私人反馈、兴趣、心情或目标，不给模型探索目标或读取权限，只接受 SKIP 或空间草稿。
生成、费用结算、原文/来源摘要、读取展示和源撤回继续走原 Life 合同；
`GET /internal/v2/life/topics` 提供审阅结果。重复读取展示不修改草稿，也不再次调用模型。

读取空间不会自动写入 Memory、Diary、REM、兴趣或人格偏好。来源目录有意不为 qzone
声明通用 `read`、`share`、`discover` 能力，普通聊天 Reader、私人分享与现有公开来源发布
不因增加该来源而获得权限。已有 `public_source` 继续拒绝 QZone 账号来源；
读取权和草稿许可都不包含再发布权。公开分享另走[逐稿公开审批](qzone-publication.md)。

## 撤销、遗忘与后续发布

读取过期或 `revoke` 停止新访问，历史仍可在当前处理用途内审阅。`forget` 通过原独立
ErasureLedger 和来源依赖擦除空间正文、目标中派生的资料及 Life 草稿；迟到结果和旧备份
恢复不能复活它们。原流量、停止与未知责任仍保留必要的无正文记录。

审核后纯文本公开发布已由[下一切片](qzone-publication.md)实现：完整正文、全部来源快照、账号、
受众与有效期冻结，主人逐稿确认来源分享后进入原 Outbox。来源读取不会自动获得公开发布权。
原账号动作的成功码/tid 和 UNKNOWN 合同继续有效。
自动点赞、批量评论和附带资料修改不属于本切片；失败不能改内容或受众重发。

## 依据与验证

协议字段依据此前核对的固定
[qzoneActive 读取请求](https://github.com/Catfish872/astrbot_plugin_qzoneActive/blob/c0d1f293f1bfa32a830473f18e6d50e2d5e070d6/core/api.py#L288-L319)
和[响应字段转换](https://github.com/Catfish872/astrbot_plugin_qzoneActive/blob/c0d1f293f1bfa32a830473f18e6d50e2d5e070d6/core/utils.py#L134-L169)。
不引入插件运行时依赖。执行和停止继续复用[统一接入草案记录的 DSH 对照与本项目机制](../design-future/external-capabilities.md#实现与来源依据)，
没有启动 DSH 或其他高权限执行器。

`tests/test_qzone_reader.py` 覆盖固定 GET、凭据隔离、真实请求边界复核、错作者/字段/覆盖范围、
账号变化和网络 UNKNOWN。`tests/test_qzone_integration.py` 使用签名原始主人输入、真实 HTTP
入口、独立 Reader CLI 和原生 Life Worker，验证目标/回执幂等、默认关闭、授权变化、STOP、
源撤回、遗忘及空间草稿隔离。外部平台和模型传输均为模拟；这些证据不证明现网可用或公开发布已获准。
