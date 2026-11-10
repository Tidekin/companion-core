# 原生流式聊天验收记录

日期：2026-11-10。代码候选：`e35127340db455ac6c2a54f23d471a39da198756`，本地分支 `codex/core-performance-native`。本记录统一覆盖一轮 Core 关键性能修复、Flutter 多 Bot 私聊文本闭环，以及本轮真正的 Core 到客户端流式回复。完整范围见 [运行说明](native-chat-mvp.md) 和 [产品路线](design-future/native-chat-platform.md)。

## 已验证结果

| 项目 | 结果 |
|---|---|
| Core 原生入口、模型流、SDK、计量、迁移及预算回归 | 94 passed，195.92 秒 |
| 授权、租约、遗忘回放、恢复、上下文、准入和结算边界 | 102 passed，231.36 秒；与首组有部分重叠 |
| CI 选择/复用、公开容器输入及共享包回归 | 233 passed，91.05 秒 |
| Flutter 分析与测试 | 无分析问题；14 passed |
| 干净 Git 导出客户端源码 | 无分析问题；同一组 14 项测试通过，包含所有 Drift 生成代码 |
| 跨语言真实流式入口 | 1 passed，44.42 秒：真实 TLS、实际 API、独立 Python Worker、首段后断开重连及 Dart 重启恢复 |
| 文档检查 | 204 passed |
| Windows | 正式 x64 构建成功，75.0 秒；分发整个 Release 目录 |
| Android | JDK 17 debug APK 构建成功，24.4 秒；最低 API 24；尚无真机验收 |
| Python 分发 | wheel / sdist 及源码导出重建通过；仅用解包 wheel 初始化 PostgreSQL，迁移到 pg0004_native_stream |
| 开发 MCP | 沿用上一切片已验证的官方 Dart/Flutter MCP；支持 Widget 树、控件操作、运行错误查询；正式入口不导入合成调试入口 |

Flutter 测试覆盖：独立设备密钥/Cookie/CSRF/授权、同远端 ID 隔离、草稿/已读隔离、AES-GCM 缓存和原待发包恢复、慢 Bot 不阻塞其他 Bot、丢失接受响应不二次生成、断开时停止后续提交、会话清理、宽屏/手机导航；新增 UTF-8 分块及多行 SSE、授权失效、Bot 间预览隔离、不计未完成预览为未读、旧帧不能复活撤回/删除/已完成内容，以及最终气泡替换预览。签名字节与独立 Python 测试向量一致。

跨语言测试仅模拟外部供应商 HTTP，实际经过 TLS 证书验证、Dart CoreBackend/BotSession、Core 路由、PostgreSQL、原授权和独立 Worker。供应商输出首段后暂停，直到客户端确认看见 RUNNING 状态的正文预览且没有最终回复；客户端先断开、重新登录恢复同一轮，再允许供应商结束。最终仍是一轮、一个正式答复、一次供应商调用，重启读取原消息。自动历史轮询在此验收中关闭，增量来自真实 SSE 订阅。未启动 QQ/Bridge，未调用付费供应商。

Core 流式回归还覆盖模型未结束时可读首段、推理不公开、EOF 保留 UNKNOWN/未完成且不重新生成、撤销清除持久预览、原遗忘账本清除及回放、过期租约停止正文投影、在线订阅在撤销后报告 session 结束，以及静止历史不被重复投影。原有最终来源、费用和 Outbox 事务仍是成功标记的前提。

本机首次 Android 构建误用了系统 JDK 26，触发 secure-storage 插件的 JDK image transform 失败；指定已有 JDK 17 后构建通过。分发 APK 另外检查了内含的真实 SSE 端点和“生成中预览”界面代码。

## 性能结论的范围

本轮 Core 减少同一同步校验边界内的重复账本解析和无用复制，并对无关提交后的批量重验做一次新鲜批处理重试。撤销、遗忘、跨 await 失效与持续并发下的保守回退仍保留。对应性能与来源校验回归此前已通过；详见 [隔离对照](performance-source-validation.md)。

1000 条无关记录、约 2.27 MB 的合成材料上，9 次读取的解析次数从 9 降至 1；无关提交情形的中位数为 8.90 → 5.91 ms。这是权限材料解析的隔离微基准，不是端到端聊天延迟。超过 16 MiB 的材料仍保留全量解析。财务检查点与完整回放优化另行验收，本轮未实测生产环境的 152 秒请求。

## 交付与剩余范围

本地交付物位于 `.local/deliverables/native-chat`：Windows x64 压缩包、Android 调试 APK、Core wheel、Core 源码包及 SHA-256 manifest。旧客户端包保留在相邻的 `native-chat-2f5f1b22`。原始验收日志与 JUnit 位于 `.local/verification` 及 `.local/verification-*.log`，未写入公开源码或 Python 包。

当前可同时连接多个 Core 实例，每个实例一个独立 Bot 私聊。每个 Bot 独立订阅并显示生成中、完成、中断/不确定或撤回状态。服务端需先迁移到 `pg0004_native_stream`；代理需关闭 `/v1/console/chat/events` 的缓冲及缓存并允许长连接。AstrBot / DSH、群聊、统一多账号服务、Android 后台通知和真机兼容属于后续切片。

未创建远程 PR、排队远程 CI、合并或部署生产；本次无候选等待队列或 CI 排队时间。新增 flutter-client CI 门禁已与现有最终门禁关联，远程执行结果需在实际提交后产生。
