# 原生文本聊天最小版本

状态：原生签名设备合同已由主人确认；当前切片采用 Flutter 实现 Android / Windows 共用的多 Bot 私聊客户端。Core 基线为 PostgreSQL 主线 a3e51241，新增结构修订 pg0003_native_chat。

首期交付原生文本客户端：连接自己的 Core、登录设备、发送文字、沿用原有记忆与人格、读取持久回复和历史、断网后恢复。真机与后台能力、多端完善、流式和外部 Bot 兼容随后分项交付。Core 共同性能修复只做一轮关键改动，不以全面优化为客户端开发的前置条件。

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

上述原生来源合同已按 AGENTS.md 完成讨论并获得确认。流式、Android、通用后端绑定和外部 Bot 适配继续按独立切片交付。


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

打开后添加多个 Bot 的名称和 HTTPS 地址，分别登录主人凭据，预览并批准登记。首次登记会更新授权版本。若 Core 尚无允许 chat 的主人授权，需先用原主人授权 status / preview / revisions 流程批准该设备公钥与 chat 目的；后续撤销也走同一流程。设备密钥与 QQ capture 独立。

正式 Windows 构建使用 `flutter build windows --release`；需分发整个 `build/windows/x64/runner/Release` 目录，不能只复制 exe。Android 使用同一源码 `flutter build apk --debug` 进行开发安装；公开发布前另行配置自己的 release 签名、升级与分发渠道。Android 最低 API 24。Android 构建还需 JDK 17、Android SDK 36，以及各插件要求的 NDK；下载和构建缓存放在工作区 `.local`。

默认数据目录为系统应用支持目录；开发可用 `--dart-define=COMPANION_DATA_DIR=<工作区内目录>` 指定 SQLite 位置。`scripts/flutter.ps1` 将工具缓存限制在工作区；需要使用已有 Android/JDK 工具时按本机配置准备 `.local/android-sdk` 与 JAVA_HOME。含空格路径遇到原生 hook 的 SDK 兼容问题时，可以给同一工作区设置临时 `subst` 别名，并设 `COMPANION_FLUTTER_WORKSPACE` 为该别名，保持所有文件仍落在原工作区。目录联接辅助脚本用于当前 Windows 未启用开发者模式的环境，不修改系统设置。

验证入口：`scripts/check-flutter.ps1` 执行锁文件、格式、分析与 Flutter 测试；`-WindowsBuild` / `-AndroidBuild` 增加构建；`-Wire` 用配置好的测试 PostgreSQL 运行真实 TLS + 独立 Worker 的跨语言验收，外部模型 HTTP 固定返回。该测试的临时证书与测试专用密钥仅保存在 `.local/test-temp`，不替代正式平台安全存储。

调试界面可运行 `flutter run -d windows -t lib/main_debug.dart`，再让开发工具连接 `scripts/flutter-mcp.ps1`。先读取实时 Widget 树，再按实际控件操作。Windows 应用需要处于可绘制状态；MCP、SDK 和界面验收均只用于开发。

## 当前能力与后续切片

文本提交先在本机持久入队，沿用同一签名、同一请求 ID 和原授权重试。丢失接受响应只补查或重投同一请求；已接受任务不重新生成。旧授权请求被拒绝后明确显示原因，不自动改签。UNKNOWN 显示结果待核对，保留 Core 的不重复付费语义。

登录及“同步全部历史”执行分页核对，日常同步最近页和未完成任务；界面显示最近 200 个输入轮次。本地已同步历史按 Bot 保存，远端删除标记在收到同步时清除正文。离线设备和未进入日常最近页的旧记录要在恢复连接并执行完整同步后完成核对。

本轮先交付原生文本闭环。真正的 token 流式仍需贯通供应商、Core Worker、持久消息状态与客户端；当前没有把已完成文本拆成动画来宣称流式。下一切片接这一链路，再扩展后台通知、多媒体、AstrBot / DSH 和群聊。Android 后台常驻、真机兼容及公开发布验收单独进行。

## 复用依据

- [Flutter 官方 MCP](https://docs.flutter.dev/ai/get-started)、[工具说明](https://docs.flutter.dev/ai/tools)：复用 SDK 的 MCP 与 Widget/Driver 能力。
- [Flutter SDK 归档](https://docs.flutter.dev/install/archive)：SDK 固定版本和官方 SHA-256；[Drift 平台支持](https://drift.simonbinder.eu/platforms/)：Android / Windows 的 NativeDatabase。
- [Riverpod](https://pub.dev/packages/flutter_riverpod)、[Dio](https://pub.dev/packages/dio)、[flutter_secure_storage](https://pub.dev/packages/flutter_secure_storage)：版本固定在 pubspec.yaml / pubspec.lock，采用各平台现成实现。
- DSH 本地 Cordis MCP bundle 研究复用了 stdio command / args / env / cwd 的现成边界，不新建自有界面 RPC 协议。
- 设备签名、撤销、预算和 UNKNOWN 复用现有 owner capture / OwnerGrantPolicy / paid journal。Python 与 Dart 的共同签名字节已用独立测试向量核对。
