# 私人陪伴 Beta

`companion_scope="owner_private_v1"` 将原生生活能力接入已签名、可停止、有期限的主人私聊
窗口。近期历史、自动长期记忆、主动聊天、日记、交互状态、反思、夜间整合、持久目标、
自主探索和表情包复用现有服务、数据库及 Outbox。

Core 与 Bridge 同一冻结计划须显式声明 `companion_approval_sha256`、
`history_scope="owner_private"`、`history_approval_sha256`、
`memory_scope="owner_private"` 和 `memory_approval_sha256`，并启用签名自动接收。
原有范围及未配置时的行为保持原合同；新配置不能自行扩大既有窗口的截止时间或额度。

近期历史只使用同一主人会话的完整 SENT 回合，最多 15 轮、16,000 UTF-8 字节，
并受最终上下文预算约束。原图和富消息只在当前输入按其独立范围使用，历史保留文字观察。
自动记忆从主人原始文字提取，保留证据、假设/确认状态、语义复核、冲突和删除回放；
图片、引用和网页不自动成为主人事实。`/记住这个`、`/你记得什么`、
`/确认这一条`、`/忘记这一条` 和 `/纠正这一条` 沿用原生命令。

`companion-worker` 处理前台聊天及其原生回复路径；`companion-life-worker` 是自主生成的唯一
选择者，有界轮转原生 Life、Diary、Social、Memory 与 Consolidator 操作。独立的 Content、
Reader、Executive 和 Voice 执行路径继续服务已绑定的前台依赖。完整切换要求 Core 路由与
原生单步接口同时接入，见 [原生调度边界](p10-operations-observation.md#原生调度边界)。

以上能力须启用对应配置，保留模型次数、Token、租约与缺失回复防重放规则。
真实费用与用量照实记录，单纯费用 UNKNOWN 只提示待核对。聊天优先，后台任务使用同一个付费
并发槽；新图片输入等待已在途的后台调用结算后再识图。

在 `local_active_attempts_v2` 模式下，Life 判断未决模型责任时复核原付费 Journal。
已有 `model_closures` 的 UNKNOWN 不再单独暂停生活循环；UNKNOWN 状态、费用及原始证据
保持原值，不因此重放原请求。没有结算证明、Journal 恢复中或仍有不确定投递时继续暂停；
恢复生活循环仍须满足当前用途、来源、预算及主动发送许可。

多图和分条的数量配置可同时设为：

```toml
[content]
max_images_per_event = "unlimited"

[paid_model.image_input]
max_images_per_request = "unlimited"

[segmented_reply]
max_segments = "unlimited"
min_chars = 28
interval_ms = 600
```

省略时沿用旧缺省值：Content 四张、付费图片一次一张、分条三段。取消这些配置上限后，
协议段容量、文本字节、原图字节和像素、Token、超时及窗口总发送额度仍分别生效。
完整片段组只生成一次，每片独立 ACK；UNKNOWN 不重发，未全部 SENT 的组不进入历史。

Bridge 的 `meme_directory` 指向只读的分类图片库，默认分类为 `happy`、`like`、
`surprised`、`morning`、`sleep`。基础素材在 `assets/memes`，可由
`scripts/build_default_memes.py` 重建。准备后的文字与图片合为一次 OneBot 请求，
继续使用原始发送账本与不确定性处理。

停止或到期阻止新的模型、阅读与发送；STOP 撤销排队和租用的后台任务，已经开始的
阅读保留 UNKNOWN。日记、反思和记忆在结果提交前复查窗口和原生隐私/来源租约。
部署须让独立主机定时器停止 API、聊天、life、diary、Bridge 和所有 Reader。

合成组合检查覆盖真实 API 生命周期与独立 worker、连续两回合、自动记忆和重启检索、
跨模拟日的独立日记/夜间 worker、目标来源、自主阅读与主动发送、五图与记忆和历史
联合理解、五段投递、后台在途并发、内容/来源/付费凭据漂移、STOP/到期、删除及旧备份恢复。
模拟时钟验证不能替代真实跨日运行的观察。
