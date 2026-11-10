# 原生文本聊天验收记录

日期：2026-11-10。代码候选：`2f5f1b22`，本地分支 `codex/core-performance-native`。本记录对应一次 Core 关键性能修复及 Flutter 多 Bot 私聊文本闭环；完整范围见 [运行说明](native-chat-mvp.md) 和 [产品路线](design-future/native-chat-platform.md)。

## 已验证结果

| 项目 | 结果 |
|---|---|
| Core 原生签名设备、授权、CI 选择/复用及公开容器输入回归 | 291 passed，195.10 秒 |
| Flutter 分析与测试 | 无分析问题；10 passed |
| 干净 Git 导出源码 | 无分析问题；同一组 10 项测试通过，包含所有 Drift 生成代码 |
| 跨语言真实入口 | 1 passed，42.16 秒：Dart HTTPS 客户端 → Core 实际 API → 独立 Python Worker → Dart 重启恢复 |
| 文档检查 | 204 passed，28.50 秒 |
| Windows | 正式 x64 构建成功；分发整个 Release 目录 |
| Android | debug APK 构建成功；最低 API 24；尚无真机验收 |
| Python 分发 | wheel / sdist 构建与源码导出重建通过；仅用解包 wheel 初始化 PostgreSQL，迁移到 pg0003_native_chat |
| 开发 MCP | 官方 Dart/Flutter MCP 启动应用、读 Widget 树、输入、登录、发送、切换 Alpha/Beta 两个合成会话；最终运行错误为 0 |

Flutter 测试覆盖：独立设备密钥/Cookie/CSRF/授权、同远端 ID 隔离、草稿/已读隔离、AES-GCM 认证缓存与原待发包重启恢复、慢 Bot 不阻塞其他 Bot、丢失接受响应不二次生成、断开时停止后续提交、会话清理、宽屏/手机导航。签名字节与独立 Python 测试向量一致。

跨语言测试仅固定外部模型 HTTP，实际经过 TLS 证书验证、Core 路由、数据库、授权和独立 Worker；未启动 QQ 或 Bridge，也未调用真实付费供应商。原生 API 的其他回归包含 native-only 授权、撤销/遗忘、UNKNOWN 和历史分页。

MCP 的首次 Windows 调试会话出现过旧 VM/Driver 状态与关闭窗口延迟；新建应用并显式传入该应用的 appUri 后操作成功。登录后等待弹窗离开，再检查界面与运行错误。正式包不启动调试入口，也不启用 Bot MCP 执行。

## 性能结论的范围

本轮 Core 减少同一同步校验边界内的重复账本解析和无用复制，并对无关提交后的批量重验做一次新鲜批处理重试。撤销、遗忘、跨 await 失效与持续并发下的保守回退仍保留。对应性能与来源校验回归此前已通过；详见 [隔离对照](performance-source-validation.md)。

1000 条无关记录、约 2.27 MB 的合成材料上，9 次读取的解析次数从 9 降至 1；无关提交情形的中位数为 8.90 → 5.91 ms。这是权限材料解析的隔离微基准，不是端到端聊天延迟。超过 16 MiB 的材料仍保留全量解析。财务检查点与完整回放优化另行验收，本轮未实测生产环境的 152 秒请求。

## 交付与剩余范围

本地交付物为 Windows x64 压缩包、Android 调试 APK 和 SHA-256 manifest；构建产物、原始验收日志与 JUnit 保存在工作区 .local，未写入公开源码或 Python 包。客户端和 Core 分别构建。

当前可同时连接多个 Core 实例，每个实例一个独立 Bot 私聊。真正的 token 流式、AstrBot / DSH、群聊、统一多账号服务、Android 后台通知和真机兼容属于后续切片。当前客户端展示已持久化的最终文本与任务状态。

未创建远程 PR、排队远程 CI、合并或部署生产；本次无候选等待队列或 CI 排队时间。新增 flutter-client CI 门禁已与现有最终门禁关联，远程执行结果需在实际提交后产生。
