# 多图文章与 DeepSeek 视觉分析

2026-10-20，基于 P3 Draft PR #15 的 `a199e25443e34b81c905c802e36fa8a707504b4d`。
本轮落实主人要求的多图读取和真正图片识别；P3 的原始证据仍保留当时“图片只有引用”的状态。

## 已完成

- 每篇最多保留 32 张图片引用和顺序；两篇分享最多 64 张。每批处理两张，每次视觉请求只包含一张图片，逐张记账和保存证据。
- 复用已配置的 `deepseek-flash` URL、模型和密钥，发送真实 base64 图片；提取可辨认的文字、数字和画面描述。DeepSeek 请求明确关闭 thinking，输出最多 300 tokens。
- 图片分析回到文章事件，携带图片序号、正文偏移、媒体哈希和处理模型。图中文字保持 external_untrusted；不是指令或执行授权。
- 图片下载前检查权限、每篇/每天/月费用和流量；调用前再次原子预留。预算或每篇数量上限不足时保留未读引用，可以显式继续；成功或 UNKNOWN 不重试。
- 长正文截断时预留图片标记，避免丢失后续配图。上下文容量不足和模型输出长度截断都会明确标记。
- `0015_article_images` 保留已有内容行、外键、唯一约束和状态约束，将槽位范围扩大到 64；存在旧范围之外的数据时拒绝降级。

来源能力已核对 [DeepSeek 图像理解文档](https://api-docs.deepseek.com/zh-cn/guides/vision/)：当前 `deepseek-flash` 支持图像输入。项目仍采用更小的本地限额：每图默认 1 MB、静态 PNG/JPEG/WebP、单边最多 4096 像素；不会让 Provider 自行下载任意 URL。

## 真实验证

[脱敏结果](article-vision-live.json) 对同一篇小黑盒帖子 `168860244` 的实际来源进行验证：

| 指标 | 结果 |
|---|---|
| 保留的文章图片 | 14 张 |
| 本次识别上限 | 3 张，按 2 + 1 处理 |
| 真实 DeepSeek 请求 | 3 次，均返回图像分析 |
| 图像实际字节数 | 98,648 / 57,663 / 140,635；均为 JPEG |
| 模型返回字符数 | 465 / 413 / 460 |
| Core 上下文 | 三张图片的分析、序号 1/2/3 与正文关联通过 |
| 其余图片 | 11 张保留引用，明确标为本次上限之外未读 |
| 费用 | 总测试上限 0.01 美元；保守费用预留合计 0.006 美元，非实账扣费值 |
| 删除与恢复 | 删除分享源、旧加密备份恢复并重放最新独立 ErasureLedger 后，正文和全部图片派生证据均被擦除 |
| 外部发送 | 真实 QQ 发送 0；真实聊天模型调用 0；生产部署 0 |

验证复用本轮主人提供的独立小黑盒登录会话，没有提取设备/浏览器既有凭据；临时读取 grant 已关闭。
图片内容只经过内存中的验证和 Provider 请求，Core 不落原图。公共报告仅保存长度、哈希和状态；
私密验证输出不随包或 PR 发布。模型识别可能出错或达到输出上限，不代表全量 OCR 或完整理解全篇 14 张图。

## 配置

以下是合并入现有配置的内容片段，费用数字由操作员选择；默认仍关闭。
原有 `content_enabled`、`observation_enabled`、Reader 额度及该 Reader 的 `media_analysis=true` 是前提。
`use_chat_model_for_vision=true` 会在运行时复用既有聊天 URL/model/key，无须复制密钥到这里。

```toml
[content]
use_chat_model_for_vision = true
max_images_per_article = 32
max_daily_images = 32
max_daily_image_bytes = 32000000

[content.vision]
estimated_request_micro_usd = 2000
```

数值单位为微美元，1,000,000 等于 1 美元。运行 `python -m companion content-worker` 即使用现有队列分批读取；
不启用额外 Agent、调度器、自动发表或后台付费任务。原 QQ 图片上传协议仍限定两个槽位。
更新前先备份，使用 `python -m companion init` 升级到 `0015_article_images`；旧槽位约束会使 readiness 返回 `MIGRATION_REQUIRED`。

提高额度后，已有 Bridge 身份可调用 `POST /internal/v2/content/{event_id}/images/resume`。
只重新排队已知预算/数量阻断、从未启动 I/O 的图片；失效来源和 UNKNOWN 仍拒绝。
每次显式继续有新的有限等待窗口，普通聊天不无限等待。暂停只需关闭内容功能或停内容 worker，
保留数据库、费用预留及 ErasureLedger；不通过删表或降级回滚。

## 检查

首轮相关回归 182 项通过；后续增加长正文保图、输出截断与 WebP 检查，最终数量见本分支 PR 当前 HEAD 的 CI。
覆盖 8 图实际 HTTP Provider 格式、2 图分批、32 图上限、正文/图序上下文、成本预检、显式继续与 UNKNOWN 不重放、
认证与源擦除、带数据的 0014→0015 迁移和旧备份最新删除重放。
Win/Linux 全量、真实 Chromium、三类镜像、原生插件、100 事件回放、六进程 smoke 和发行包检查由 CI 完成。
