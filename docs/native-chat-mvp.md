# 原生文本聊天最小版本

状态：原生签名设备合同已由主人确认；当前切片采用 Flutter 实现 Android / Windows 共用的多 Bot 私聊客户端。Core 基线为 PostgreSQL 主线 a3e51241，新增结构修订 pg0003_native_chat。

首期交付原生文本客户端：连接自己的 Core、登录设备、发送文字、沿用原有记忆与人格、读取持久回复和历史、断网后恢复。真机与后台能力、多端完善、流式和外部 Bot 兼容随后分项交付。Core 共同性能修复只做一轮关键改动，不以全面优化为客户端开发的前置条件。

## 复用现有授权的原生输入合同

当前 OwnerCapture 只识别签名的 QQ 输入，原生客户端需要有可核验的独立来源。本切片复用现有 Ed25519 输入证据、OwnerGrantPolicy、权限账本、累计准入和任务租约，增加一种来源及其设备公钥登记。原生消息明确记录为 `native.chat`。

| 项目 | 已确认合同 |
|---|---|
| 主人身份 | 继续使用 Core 的内部 owner 主体；客户端不提交 QQ 号或选择其他主体 |
| 设备登录 | 复用现有主人控制台会话、有效期、撤销、Origin 和 CSRF；首次连接输入主人凭据，客户端不保存该凭据 |
| 设备证明 | 每设备 Ed25519 密钥；公钥经主人会话预览、批准后进入独立设备凭据修订，运行用途仍由 OwnerGrantPolicy 决定 |
| 原生会话 | 同一 Core 主人的一个 `native:owner` 逻辑会话；保留平台独立来源与历史出处 |
| 输入身份 | 稳定的设备与提交身份组成 source_event_id；event_id 继续使用确定性摘要，重试沿用原签名与原授权引用 |
| 输入证据 | 复用原签名域和已定义字段，增加 `authenticated_native_device` 类型；签名绑定正文摘要、原授权、设备身份与时间 |
| 准入 | 登记公钥只允许该设备提交输入；实际模型、记忆、工具权限仍由现有目的授权、预算和来源校验决定 |
| 恢复与撤销 | 保留旧来源证据；恢复代际、撤销、遗忘和过期使旧执行权限失效；离线请求不自动改签到新授权 |

设备登记由独立的凭据修订保存，不改变主人运行授权版本、预算或 QQ capture。主人登录后预览并批准即可在线添加；其他设备和正在执行的任务继续使用原授权。每次重新登记都有独立签名代际，撤销只取消对应设备的未完成工作，保留已发送历史、原账务和不确定责任。历史授权中的设备字段仅保留为原签名证据及一次性迁移输入。

备选方案是仅凭登录会话写入原生消息。它可以减少客户端签名，但需要新增另一套可恢复的来源证明，并重新界定数据库外证据的保证。首期建议沿用已有签名证据模型。

## 数据与执行

客户端将会话 Cookie 与 CSRF 保存在现有系统凭据库中，按 Bot 隔离并校验服务地址，
不保存主人密码。重启后先核验服务端会话，再复用原设备登记同步历史和继续原任务；
会话失效或设备撤销不会自动重新登记。网络暂时不可用时保留会话并继续尝试同步。
“退出登录”注销服务端会话并清除本机保存的登录状态；关闭应用保留登录状态和本地聊天记录。
默认有效期一年，显式短有效期的部署需按[控制台配置](owner-console.md#配置与运行)调整并重新登录一次。

平台和客户端使用稳定提交身份。Core 保存真实原生事件并创建一个现有 chat 任务；独立聊天 Worker 继续运行原记忆、人格、模型账务与恢复流程。

原生回复以服务端持久化可读取为交付点，设备通过有界历史接口同步；首期不记录已读回执。QQ 传输不能领取原生回复。未确认模型结果继续保留 UNKNOWN；保存到自己的数据库不代表外部工具效果已经确认。

最小读取接口支持有界历史分页、按原提交查询状态和最终回复、删除标记。客户端重连查询已有任务与历史，不为补齐界面重新生成。原生聊天与现有 QQ 聊天的历史合并作为显式后续迁移选择。

## Flutter 双端客户端

采用 Flutter 3.47.9 / Dart 3.13.7，Riverpod 管理连接与界面状态、Drift/SQLite 保存本地记录、Dio 调用 Core HTTPS、go_router 处理宽屏双栏和手机会话导航。`clients/chat` 与 Python 服务端分别构建、分发。模型适配器继续只负责模型调用。

从第一版开始保存多个 Bot 连接。每个连接有自己的名称、后端类型、HTTPS 地址、设备签名密钥、缓存加密密钥、会话 Cookie、CSRF、授权引用、草稿、待发队列、已读位置和网络队列。相同远端消息 ID 以 Bot ID 作命名空间；一个 Bot 的连接等待不会阻塞另一个 Bot。

当前一个 Core 实例对应一个 Bot，多个实例可以同时在线。每个实例内部仍为一个主人的 `native:owner` 私聊；这不表示单个 Core 已增加多个人格或群聊。`ChatBackend` 是客户端的连接接口，后续 AstrBot / DSH 接入在这个边界实现，服务端 transport 继续保持 HTTP v1 独立。

设备种子和缓存密钥由 flutter_secure_storage 使用平台安全存储保护；聊天正文、草稿、原授权和待发包以 AES-GCM 加密后写入 SQLite，AAD 绑定 Bot 与记录身份。主人控制凭据、Cookie、CSRF 只在运行内存中，重启后重新登录。Android 禁用自动备份和明文网络。TLS 正常验证证书、拒绝跨站重定向。

## 开发与界面调试 MCP

直接复用官方 Dart/Flutter MCP，入口是 `scripts/flutter-mcp.ps1`；服务端提供分析、测试、设备发现、Widget 树、控件操作、运行错误与热重载。它属于开发工具，不是聊天 Bot 的工具执行权限，也不会改变 M0/M1 的外部能力限制。

`lib/main_debug.dart` 是带 Flutter Driver 扩展的开发入口，只使用内存中的合成 Bot，界面有明确开发提示；正式 `lib/main.dart` 使用真实连接和平台安全存储，不导入该入口。正式构建保留 `lib/main.dart` 为目标。

## 验收

1. 未运行 QQ 协议端、Bridge 或 AstrBot 时，客户端通过真实 Core HTTP 和独立 Worker 完成文字回复。
2. 同一提交重试只产生一份事件和任务；不同内容使用同一身份被拒。
3. 未登记设备、错误签名、错误会话、失效授权和越权历史查询被拒。
4. 原记忆和人格进入原生对话上下文，历史在重启、断网和客户端缓存恢复后保持一致。
5. 原生回复不能进入 QQ 投递；旧 QQ 来源保持原类型。
6. 遗忘、撤销、模型 UNKNOWN、Worker 崩溃与恢复仍遵守原有保证。

原生来源及本轮流式展示合同已按 AGENTS.md 完成讨论并获得确认。通用后端绑定和外部 Bot 适配继续按独立切片交付。


## 构建与运行

服务端先升级到本分支并运行现有数据库迁移入口，再使用正常的独立聊天 Worker。配置 `console.enabled=true`、`console.public_origin` 为真实 HTTPS 根地址，经原反向代理开放 `/v1/console/`。全新安装可以仅登记 native_devices、capture 为 null，不需要 QQ 密钥；复用既有 Core 时保留原授权中的记忆和历史来源范围。

Windows 工作区安装固定 SDK，并准备客户端：

```powershell
./scripts/install-flutter.ps1
Set-Location clients/chat
../../scripts/flutter.ps1 -Tool flutter -ToolArguments @('pub','get','--enforce-lockfile')
../../scripts/prepare-flutter-links.ps1
../../scripts/flutter.ps1 -Tool flutter -ToolArguments @('run','-d','windows')
```

打开后添加多个 Bot 的名称和 HTTPS 地址，分别登录主人凭据，预览并批准登记。Core 已有 chat 目的授权时，设备在线登记立即生效。若尚未批准 chat，仍须先用原主人授权 status / preview / revisions 流程授予该目的；设备登记不扩大用途或预算。设备密钥与 QQ capture 独立。

`pg0012_native_devices` 将当前已经批准的设备导入独立凭据表，原授权和历史签名字节保持不变。后续登记不需要 STOP、Bridge 交接或清除 UNKNOWN；运行权限、模型结算和投递责任仍沿用原流程。`POST /v1/console/chat/devices/{device_id}/revoke` 由主人控制台或该设备自己的主人会话调用，撤销后的旧会话不能重新登记，须重新输入主人凭据。全局授权内的历史设备字段不再作为修改设备的入口。

聊天历史的 SENT 状态只证明原先发送成功；若正文已按删除指令清空，接口返回 `content_erased=true`，客户端显示“这条回复已清除”，并阻止迟到的缓存帧恢复正文。新设备会从服务端同步主人消息和仍保留的完整回复；每次打开对话定位最新消息，上翻阅读时保持当前位置。

凭据边界参考固定 DSH 实现的[独立凭据记录](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/credentials/credentials/src/types.ts)与[授权完成后原子提交](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/credentials/authorization/src/index.ts)：复用本项目原主人会话、CSRF、操作回执、修订记录和 Ed25519 字段，不引入 DSH 运行依赖。新代际使用原签名的 `protocol_epoch` / `identity_sha256` 绑定，历史证明不因撤销而消失。

正式 Windows 构建使用 `flutter build windows --release`；需分发整个 `build/windows/x64/runner/Release` 目录，不能只复制 exe。Android 使用同一源码 `flutter build apk --debug` 进行开发安装；公开发布前另行配置自己的 release 签名、升级与分发渠道。Android 最低 API 24。Android 构建还需 JDK 17、Android SDK 36，以及各插件要求的 NDK；下载和构建缓存放在工作区 `.local`。

默认数据目录为系统应用支持目录；开发可用 `--dart-define=COMPANION_DATA_DIR=<工作区内目录>` 指定 SQLite 位置。`scripts/flutter.ps1` 将工具缓存限制在工作区；需要使用已有 Android/JDK 工具时按本机配置准备 `.local/android-sdk` 与 JAVA_HOME。含空格路径遇到原生 hook 的 SDK 兼容问题时，可以给同一工作区设置临时 `subst` 别名，并设 `COMPANION_FLUTTER_WORKSPACE` 为该别名，保持所有文件仍落在原工作区。目录联接辅助脚本用于当前 Windows 未启用开发者模式的环境，不修改系统设置。

验证入口：`scripts/check-flutter.ps1` 执行锁文件、格式、分析与 Flutter 测试；`-WindowsBuild` / `-AndroidBuild` 增加构建；`-Wire` 用配置好的测试 PostgreSQL 运行真实 TLS + 独立 Worker 的跨语言验收，外部模型 HTTP 固定返回。该测试的临时证书与测试专用密钥仅保存在 `.local/test-temp`，不替代正式平台安全存储。

调试界面可运行 `flutter run -d windows -t lib/main_debug.dart`，再让开发工具连接 `scripts/flutter-mcp.ps1`。先读取实时 Widget 树，再按实际控件操作。Windows 应用需要处于可绘制状态；MCP、SDK 和界面验收均只用于开发。

## 当前能力与后续切片

文本提交先在本机持久入队，沿用同一签名、同一请求 ID 和原授权重试。丢失接受响应只补查或重投同一请求；已接受任务不重新生成。旧授权请求被拒绝后明确显示原因，不自动改签。UNKNOWN 显示结果待核对，保留 Core 的不重复付费语义。

登录及“同步全部历史”执行分页核对，日常同步最近页和未完成任务；界面显示最近 200 个输入轮次。本地已同步历史按 Bot 保存，远端删除标记在收到同步时清除正文。离线设备和未进入日常最近页的旧记录要在恢复连接并执行完整同步后完成核对。

原生文本现已贯通供应商 SSE、原有 Agent/Worker、持久预览、Core HTTPS SSE 与 Flutter 界面。模型未结束即可显示正文，最终事务完成后由正式回复替换预览。每个 Bot 各自订阅；断开订阅不取消 Worker，重新登录恢复原任务。后台通知、多媒体、AstrBot / DSH 和群聊后续扩展。Android 后台常驻、真机兼容及公开发布验收单独进行。

服务端需迁移到 `pg0004_native_stream`。反向代理应关闭 `/v1/console/chat/events` 的响应缓冲与缓存，并允许长连接；服务端发送约 10 秒心跳，客户端在 30 秒无数据时重连。订阅初始返回最近 100 轮快照，其后按变化推送；旧任务仍由原状态/历史接口补查。活跃预览每次读取都重新校验来源与租约，静止历史不会被每 250ms 全量重新投影。

## 表情面板

输入框旁的表情按钮打开面板：“收藏”页是主人收藏的图片，点一下立即单独发出，长按删除；
“+”从相册添加，长按聊天里的任意图片也可以“收藏为表情”。其余几页是常用的 Unicode 表情，
点一下插入到光标处。收藏只保存在本机（应用数据目录 `stickers/`，最多 300 张，单张不超过
4 MB），发出时走普通图片消息，Core 不需要新接口；她能否看懂图片取决于授权里的读图用途。

## 她的动态

对话页顶栏的“她的动态”打开动态面板：她用第一人称发的、只给主人看的说说，最新在前，可以点赞、
隐藏或忘记；那年今日在顶部列出往年同一天的动态。接口都在 `/v1/console/chat/moments` 下：列表
（`GET moments?before=`，下一页原样传回 `next_cursor`，为空时已读完；同一时刻按动态 ID 倒序）、那年今日（`GET moments/memories`）、点赞（`POST moments/{id}/like`）、
隐藏（`POST moments/{id}/visibility`）和忘记（`POST moments/{id}/forget`，经原遗忘日志）。
事件流在动态有变化时推送 `event: moment`（只含变化标记），客户端据此重新读取当前列表。

主人也可以发自己的动态（文字和图片），并在任意一条动态下评论。帖子和评论都是普通的签名原生输入，
只多带一个 `moment` 引用：`{"kind":"post"}` 或 `{"kind":"comment","moment_id":...}`。这个引用和
文字一起计入 `input_sha256`，不能被改指向别的动态；需要授权里有 `moments` 用途。它们走原聊天
流程：她的回复照常计费、核对来源和记忆提取，只是不出现在对话历史里，而是显示在那条动态下面
（`GET moments/{id}/comments`）。删除主人自己的帖子走原生事件遗忘。对话页有她新动态时，在输入框
上方显示一张预览卡片（`GET moments/latest?after=`），点开进入动态面板；本机记住看过的时间。
授权与发布规则见[动态设计稿](plans/2026-11-16-native-moments.md)。

她的动态可以带一张生成的配图（`GET moments/{id}/images/0`，只在动态仍公开时返回）。列表项的
`images` 是已经生成好的图，`image_pending` 表示还在画；画好后事件流照常推送变化标记。规则与费用见
[动态配图](plans/2026-11-17-moment-images.md)。

## 复用依据

- [Flutter 官方 MCP](https://docs.flutter.dev/ai/get-started)、[工具说明](https://docs.flutter.dev/ai/tools)：复用 SDK 的 MCP 与 Widget/Driver 能力。
- [Flutter SDK 归档](https://docs.flutter.dev/install/archive)：SDK 固定版本和官方 SHA-256；[Drift 平台支持](https://drift.simonbinder.eu/platforms/)：Android / Windows 的 NativeDatabase。
- [Riverpod](https://pub.dev/packages/flutter_riverpod)、[Dio](https://pub.dev/packages/dio)、[flutter_secure_storage](https://pub.dev/packages/flutter_secure_storage)：版本固定在 pubspec.yaml / pubspec.lock，采用各平台现成实现。
- DSH 本地 Cordis MCP bundle 研究复用了 stdio command / args / env / cwd 的现成边界，不新建自有界面 RPC 协议。
- 设备签名、撤销、预算和 UNKNOWN 复用现有 owner capture / OwnerGrantPolicy / paid journal。Python 与 Dart 的共同签名字节已用独立测试向量核对。
- [Pydantic AI Agent 事件](https://pydantic.dev/docs/ai/core-concepts/agent/)、[DeepSeek 流式协议](https://api-docs.deepseek.com/api/create-chat-completion/)、[HTTPX-SSE](https://github.com/florimondmanca/httpx-sse)、[Starlette StreamingResponse](https://starlette.dev/responses/)：保留原 Agent.run 工具循环、费用结算及 Hooks；新增锁定 `httpx-sse==0.4.3` 处理供应商 SSE 帧。DSH 本地 `dsh-llm-deepseek` 与 `dsh-llm-pi-ai` 实现用于核对文本/推理/工具事件边界，未接入或执行 DSH。


## 本轮流式切片：已确认的展示合同

等待合并续句时，消息视图会返回 `can_reply_now`。主人可在该条消息上点击“立即回复”。已授权设备向 `POST /v1/console/chat/messages/{event_id}/reply-now` 提交请求，只释放该条消息首次执行前的等待；重复点击返回当前消息状态。新消息仍可按原规则取代旧请求，重试退避、模型预算与发送前校验继续生效。旧服务不返回该字段时，新客户端使用普通发送流程。

范围仅 Core 与 Flutter 原生私聊；现有签名输入、owner 授权、chat Worker、最终 Outbox 和费用证据保持原职责。实际供应商 token 进入已锁定 Pydantic AI 的原生流式接口，保留原 Agent 工具循环和 Hooks。

现有历史只返回已持久确认的最终文字。要在模型结束前展示内容，需要一个可恢复但尚未最终确认的预览；仅内存转发无法保证 Worker 重启后的恢复，直接提前标成 SENT 则破坏费用、来源和交付确认。建议在原生 SDK 会话旁保存有界正文快照，经普通 HTTPS SSE 推送；首块及时发布，后续按约100ms合并写入，不逐 token 写数据库。

- 继续使用原输入轮次 event_id；历史、预览、最终回复在客户端同一轮次内更新。SSE 事件复用原生 turn JSON，增加可选 preview：单调 version、正文 text、生成/中断/撤回状态。最终回复仍使用原 Outbox 证据，替换该轮预览，不增加另一份相同答案。
- SSE 建立时读取最新快照，重连补读已有任务与历史；不新增全局可靠游标或逐 token 重放协议，不为补字重新调用模型。
- 对外仅投影 SDK 的普通文字；推理、原始工具参数、凭据和调试数据不发布。多次模型请求时，预览可以由下一次请求的回答替换；查询工具自身按原授权执行。
- 每次持久发布复核原租约、权限与来源；读取/订阅再次复核。撤销、来源遗忘或租约失效停止增量并清除不可继续展示的预览。
- 正常断网不取消 Worker；重新连接恢复同一轮。异常中断保留明确的未完成/结果待核对标记；从不把部分文字写成成功的记忆或日记。
- 模型结束后继续原费用结算、来源复核与最终事务；只有这些步骤完成才展示为完成。UNKNOWN 继续沿用原不重复付费规则。
- 每个 Bot 保留独立连接与状态。AstrBot、DSH 接入和群聊不在本轮。

主人已明确确认上述预览、最终确认与恢复合同，本轮据此实现。


### 私聊图片上传

客户端通过原有的 `POST /v1/console/chat/messages` 提交图片。使用标准 `multipart/form-data` 时，`message` 是不含 `images` 的 JSON 消息，文件用重复的 `images` 字段按签名时的顺序传递。文字消息继续使用 JSON。读取本地待发队列时沿用原签名和请求 ID。服务端根据收到的真实字节核对格式、额度和签名中的 SHA-256，旧客户端的 JSON 消息仍可提交。

自研客户端上传头像时，同一表单中的 `profile` 包含原有操作 ID 和预期修订号，`image` 包含一张头像文件；会话、CSRF 和素材保留期限沿用原接口。
