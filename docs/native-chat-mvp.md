# 原生文本聊天最小版本

状态：原生签名设备合同已由主人确认；当前切片实现 Windows 文本聊天。Core 基线为 PostgreSQL 主线 a3e51241，新增结构修订 pg0003_native_chat。

首期先交付 Windows 桌面客户端：连接自己的 Core、登录设备、发送文字、沿用原有记忆与人格、读取持久回复和历史、断网后恢复。Android、多端完善、流式和外部 Bot 兼容随后分项交付。Core 共同性能修复只做一轮关键改动，不以全面优化为客户端开发的前置条件。

## 复用现有授权的原生输入合同

当前 OwnerCapture 只识别签名的 QQ 输入，原生客户端需要有可核验的独立来源。本切片复用现有 Ed25519 输入证据、OwnerGrantPolicy、权限账本、累计准入和任务租约，增加一种来源及其设备公钥登记。原生消息明确记录为 `native.chat`。

| 项目 | 已确认合同 |
|---|---|
| 主人身份 | 继续使用 Core 的内部 owner 主体；客户端不提交 QQ 号或选择其他主体 |
| 设备登录 | 复用现有主人控制台会话、有效期、撤销、Origin 和 CSRF；首次连接输入主人凭据，客户端不保存该凭据 |
| 设备证明 | 每设备 Ed25519 密钥；公钥通过现有主人授权预览与批准流程进入 OwnerGrantPolicy 的原生设备映射 |
| 原生会话 | 同一 Core 主人的一个 `native:owner` 逻辑会话；保留平台独立来源与历史出处 |
| 输入身份 | 稳定的设备与提交身份组成 source_event_id；event_id 继续使用确定性摘要，重试沿用原签名与原授权引用 |
| 输入证据 | 复用原签名域和已定义字段，增加 `authenticated_native_device` 类型；签名绑定正文摘要、原授权、设备身份与时间 |
| 准入 | 登记公钥只允许该设备提交输入；实际模型、记忆、工具权限仍由现有目的授权、预算和来源校验决定 |
| 恢复与撤销 | 保留旧来源证据；恢复代际、撤销、遗忘和过期使旧执行权限失效；离线请求不自动改签到新授权 |

设备登记会更新原主人授权版本，尚未完成的旧授权任务可能失效；界面需要在批准前明确这一影响。原有 QQ capture 和原生设备映射分别保留，登记原生设备不替换 QQ 密钥。新增字段的默认序列化保持现存授权记录的原始摘要可读。

备选方案是仅凭登录会话写入原生消息。它可以减少客户端签名，但需要新增另一套可恢复的来源证明，并重新界定数据库外证据的保证。首期建议沿用已有签名证据模型。

## 数据与执行

平台和客户端使用稳定提交身份。Core 保存真实原生事件并创建一个现有 chat 任务；独立聊天 Worker 继续运行原记忆、人格、模型账务与恢复流程。

原生回复以服务端持久化可读取为交付点，设备通过有界历史接口同步；首期不记录已读回执。QQ 传输不能领取原生回复。未确认模型结果继续保留 UNKNOWN；保存到自己的数据库不代表外部工具效果已经确认。

最小读取接口支持有界历史分页、按原提交查询状态和最终回复、删除标记。客户端重连查询已有任务与历史，不为补齐界面重新生成。原生聊天与现有 QQ 聊天的历史合并作为显式后续迁移选择。

## Windows 客户端

采用标准库 Tk 桌面界面完成首个客户端，HTTP 使用项目已有 httpx，离线记录使用 SQLite。网络在后台线程处理，界面持续显示连接、待发送和任务状态；已入队文本保留稳定身份。

设备签名密钥通过 Windows DPAPI 保护；模型密钥、Bridge 密钥和主人控制凭据不写入客户端缓存。设备会话可仅保存在运行内存，重新启动后再次登录，避免扩大凭据留存范围。

首期客户端支持登录、设备身份导出与登记提示、文字发送、历史查看、重连和明显的错误状态。已发送、已接受、处理失败与结果不确定分别展示。流式在这一闭环稳定后贯通供应商、Core、服务端和客户端。

## 验收

1. 未运行 QQ 协议端、Bridge 或 AstrBot 时，客户端通过真实 Core HTTP 和独立 Worker 完成文字回复。
2. 同一提交重试只产生一份事件和任务；不同内容使用同一身份被拒。
3. 未登记设备、错误签名、错误会话、失效授权和越权历史查询被拒。
4. 原记忆和人格进入原生对话上下文，历史在重启、断网和客户端缓存恢复后保持一致。
5. 原生回复不能进入 QQ 投递；旧 QQ 来源保持原类型。
6. 遗忘、撤销、模型 UNKNOWN、Worker 崩溃与恢复仍遵守原有保证。

上述原生来源合同已按 AGENTS.md 完成讨论并获得确认。流式、Android、通用后端绑定和外部 Bot 适配继续按独立切片交付。


## 运行 Windows 客户端

1. 服务端升级到本分支代码并运行原有数据库迁移入口；使用正常的独立聊天 worker。迁移只扩展原生 Outbox 类型，不改写历史消息。
2. Core 的现有主人授权已启用 chat；需要原有记忆和历史时，继续开启 memory / history 的独立来源范围。已有 QQ capture 可以保留。全新安装也可以在原主人授权中仅登记 native_devices，capture 为 null，无需 QQ 协议端或 QQ 密钥。
3. 配置 console.enabled=true、console.public_origin 为真实 HTTPS 根地址，通过现有反向代理开放 /v1/console/。主人控制凭据只在自己的 Core 与客户端之间使用。
4. 在已安装项目依赖的 Windows 工作区运行：

```powershell
./scripts/chat-client.ps1 -Server https://your-core.example
```

也可以运行 `.venv/Scripts/python.exe -m companion_chat_client --server https://your-core.example`。这是当前可运行的 Python/Tk 桌面版本；安装包和双端共享界面留到后续客户端切片。

首次打开后可复制设备公钥。已启用 chat 的 Core 上，登录后预览并批准登记；设备尚未成为任何 chat 授权的一部分时，管理员先通过原主人授权 status / preview / revisions 接口登记该公钥。设备登记成功后保留 QQ capture 原值。撤销设备时，通过同一原主人授权流程删除 native_devices 中对应项；在途旧授权任务可能失效。

客户端只接受 HTTPS 根地址，验证证书，不跟随登录重定向；可使用受信任证书或 HTTPX 支持的系统证书配置。客户端不持久化主人凭据、CSRF 或会话 cookie。设备密钥、正文缓存和待发包在写入 SQLite/文件前使用当前 Windows 用户的 DPAPI 加密，数据目录默认为工作区 .local/native-chat-client。

发送后保留原签名、原时间和原授权；丢失接受响应也只重试同一请求。旧授权请求被拒后显示原因，不能自动改签为新消息。新消息使用同步得到的当前授权。Ctrl+Enter 发送，Enter 换行；上次本机入队未完成的消息在重新登录后查询原任务。

历史恢复采用分页查询：启动/登录及“同步全部历史”执行全量核对，日常轮询最近页和未完成任务。远端遗忘通过删除标记清除缓存正文；离线设备需恢复连接并同步才能收到更新。界面显示最近 200 条，本地缓存保留已同步的历史。当前没有主动通知、跨端实时推送或流式 token 展示。

## 复用依据

- [Tkinter 线程模型](https://docs.python.org/3.12/library/tkinter.html#threading-model)：界面由主线程操作，网络和 SQLite 由单个后台线程拥有。
- [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)、[CryptUnprotectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata)：使用当前用户保护及 UI_FORBIDDEN，不使用 LOCAL_MACHINE。
- 设备证明、权限撤销、预算和 UNKNOWN 复用仓库已有 owner capture / OwnerGrantPolicy / paid journal，原生客户端仅依赖共享 wire 包，不导入 Core 内部存储。
