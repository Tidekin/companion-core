# 消息分条

普通聊天可以把一次模型生成的文本按句拆成多条 QQ 消息。2026-10-29 起默认开启，缺省配置等同 `mode="semantic"`、`max_segments=3`、`interval_ms=1500`。要恢复单条回复，在配置文件第一个表之前写 `segmented_reply = false`（环境变量写 `false` 或 `null`）；控制台把该项设为空也会关闭。调整参数时在 Core 配置文件末尾加入：

```toml
[segmented_reply]
max_segments = "unlimited"
min_chars = 12
interval_ms = 1500
mode = "semantic"
```

`max_segments` 可取不小于 2 的整数或 `"unlimited"`；省略时兼容旧配置，缺省为 3。
`mode="semantic"` 让当前聊天模型直接以换行标记自然语义停顿，不增加模型调用。
此模式以模型给出的安全换行为分段边界；整段没有安全换行时作为一个完整气泡。
`min_chars` 仅用于按句分段模式。语义分段提示只指导气泡组织，
表达风格与对话参与沿用 `PRINCIPLES` 和当前参与指导。引用、括号动作和代码块受到保护。省略 `mode` 时继续使用
兼容旧配置的 `"sentence"` 模式及短句合并规则；旧审批的序列化字段保持不变。

`"unlimited"` 不合并超出三条的自然片段，完整文本仍受 20,000 字合同、窗口发送额度和
`max_reply_chars` 约束。`min_chars` 为 4–100 个非空白字符；`interval_ms` 为 0–3000 毫秒。
间隔从上一条成功回执开始计算，实际时间还受 Bridge 轮询影响。删除这张配置表会回到默认分条；写 `segmented_reply = false` 才恢复单条生成；已冻结的待发回复保留原分条结果。

也可通过 `COMPANION_SEGMENTED_REPLY` 环境变量提供同样字段的 JSON 对象，它优先于 TOML 配置。

按句末标点或换行拆分，保留原文字、标点和空白，合并短开头、短尾句及超出数量上限的部分。链接、行内代码、代码块受到保护；表格保持整条。没有安全断点时仍发送一条，不额外调用模型。命令回复、受托任务反馈和生成图片的表达使用原发送方式。主人输入图片后生成的纯文本回答可以分条，也可同时使用已批准记忆。

主动跟进、主动分享和已授权的 Life 私聊草稿也使用同一分条配置。分享链接仍单独放在
末尾，占用 `max_segments` 的一条；达到上限时合并末尾正文，保留完整内容。Life 草稿
发送前按整组还原原文并核对来源，每片仍有独立发送校验。分条不增加模型调用，主动联系
次数按原逻辑计数，实际气泡各自占用发送额度；前片成功回执、间隔、新输入打断、来源删除
和投递不确定停止后片的规则保持一致。

原生主人私聊在开启分条时，模型选中的已审核表情包作为最后一个独立图片气泡，
前面的文字沿用上述分段规则。图片占用原有 `max_segments` 的一条；达到上限时合并
末尾文字，不截断正文或增加发送额度。每条仍使用独立 Outbox 租约与原始投递回执，
前条成功并经过现有间隔后才发送下一条。素材不可用时不发送空消息或重发前面的文字。

带表情尾段的组使用 `reply_segment.version=2`，`part_sha256s` 固定每条完整 content
（包括选图身份和首条引用），组摘要包含版本。纯文字组保持既有 v1 字节及回执语义；
旧的已冻结图文单条也保持原投递责任。图片尾段只能用于原生主人回复，不能借此扩展
旧文本窗口或主动发送用途。新输入、撤回、遗忘、STOP、前条失败或 UNKNOWN 均沿用
原有中断与不可重放规则，不建立第二个发送队列。

有界 OneBot 窗口发送分条时，仅移除气泡首尾的空行，保留段内换行、代码缩进和行尾空格。原始片段、组摘要与历史仍保存完整文本；最终派发分别校验原始批准和整理后的实际请求，不能在发送前改回带空行的版本或替换正文。

## 投递与打断

同一回复的全部片段一次性写入原 Outbox；每片有独立幂等键，组摘要固定其顺序和内容。前片收到成功回执并经过间隔后，后片才可租用和发送。普通 Bridge 沿用原轮询流程。

Core 接受同一会话内新的、通过主人身份验证的输入时，取消尚未开始发送的旧片段。重复输入不触发打断；已经开始的网络发送仍可能完成。模型生成过程中收到新输入，也不再创建旧的多条回复。

任一片发送失败、投递不确定或发送租约过期后，其余片段停止。迟到的成功回执可以记入该片，但不会恢复已停止的后续片段。全部片段都为 `SENT` 才作为一个完整对话回合进入历史；`SENT` 表示投递回执，不等于手机已显示。

普通主人文字回复和主动私聊文字的 `begin-send` / `send-check` 在连接建立失败、连接建立超时或等待连接池超时时，
沿用原租约有限重试同一绑定请求；每次重试前重新检查 STOP、撤回、新输入和租约，不延长原有效期。
该分类采用 [HTTPX 的连接重试约定](https://www.python-httpx.org/advanced/transports/)
和[异常含义](https://www.python-httpx.org/exceptions/)。
读取、写入或协议异常仍按可能已提交处理，保留既有等待、原始 RESERVED 签名证明和未派发收口，
不重复未知结果的 POST，也不新增模型调用或发送尝试。
主动私聊沿用相同的原租约绑定和未发送证明；已确认发送的前段保持原回执，
未派发的当前段收口后取消剩余片段。取消回执丢失时，重启只重报原签名证明。
图片、语音、账号操作以及已开始平台发送的结果不明责任继续走原有核对流程。

## 有界真实文本窗口

已有单条审批保持原含义。启用分条的窗口还需在 Core 审批和 Bridge `live_text` scope 中显式包含相同的 `segmented_reply` 配置，并包含正整数 `max_send_attempts`。Bridge 要求它等于窗口 `max_attempts` 与 `max_visible_replies` 的较小值；这不会增加外层批准额度。次数使用[统一试用配置](../trial-limits.md)，与是否启用分条独立。

`max_reply_chars` 针对一次完整回复，缺省 500 字，可在现有 HTTP 合同内配置；每片各占一次实际发送额度。冻结前若剩余额度不足以容纳整组，保留完整内容并回退为一条。每片继续使用原 `window-send-once` 审批流程；间隔未到时返回 `delivery_wait: SEGMENT_NOT_READY`，不消耗发送次数。旧窗口数据库只在显式准备新分条窗口时迁移，保留已有尝试记录与 UNKNOWN；只读状态检查不迁移。

多图、记忆与分条可组合：图片观察、记忆复核、最终回复各使用原有付费阶段；分条只拆分最后一次生成，不重复调用模型。每片绑定同一组已结算调用、原图来源和记忆证据；证据改变或任一片投递不确定时，停止后续片段。原图在生成后照常清理，不为后续片段延长原始数据保留期。

原生主人运行时发送已冻结的文字时，复核图片的签名上传记录、来源事件与当前授权，不再读取原图缓存。缓存正常到期或清理不取消后续文字；模型调用仍要求原图未过期且字节完整，撤回、遗忘、来源变化和新输入打断仍生效。

Inspector 为分条组提供不含正文的摘要。手机反馈可填写 `expected_reply_count` 和 `reply_group_sha256`；多条预期必须匹配同一 trace 的 Inspector 证据。超过预期数量才按重复发送处理，少于预期的实际观察保持原值。

Bridge 在核验原始日志链后，以最近一次完成交接的链摘要和该历史范围内的全部数据库证据，复用进程内已通过的交接核验。历史输入、收件箱、发送尝试、回执和自动任务行都会参与比较。当前授权下新增气泡可以复用此前历史；历史记录变化、STOP、新终态凭据或下一次交接会重新核验。每次发送仍执行当前权限、租约、撤回和 QQ 回执检查。该复用替换了逐次历史交接摘要的缓存，沿用本项目的原始日志及证据行比较方式。

## 数据兼容与验证

迁移 `0019_segmented_replies` 在 Outbox 增加组字段；`0020_reply_segment_capacity` 扩展片段容量，
保留已有记录、索引和外键。已有超过三条的组时禁止降级容量；存在分条记录时禁止自动
降级到旧结构。删除源事件会清除全部片段正文；旧加密备份恢复时仍重放最新独立删除账本。组摘要没有另存正文副本。

离线回归覆盖分句保护、原子冻结、并发租用、发送间隔、打断、迟到回执、额度、历史、旧窗口迁移、反馈证据及删除恢复。配置与测试不构成真实 QQ 发送或付费调用授权。

## 参考实现

独立图片消息沿用 OneBot v11 的
[图片消息段](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md#图片)
和 [send_private_msg](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#send_private_msg-发送私聊消息)，
发送一个仅含 image 段的消息数组；未增加 QQ 协议或外部依赖。DSH 的 transport/media
职责分离继续复用[已记录的固定实现](next-memes.md)，版本与接入假设未变。

参考以下固定版本的行为后在 Core 与独立 Bridge 内实现，未引入 AstrBot 运行依赖，也未复制插件代码：

- [AstrBot result_decorate](https://github.com/AstrBotDevs/AstrBot/blob/e5e21b71aec5642f110eb115eb8e9a0d8028b7e5/astrbot/core/pipeline/result_decorate/stage.py)：分句与分段处理。
- [AstrBot respond](https://github.com/AstrBotDevs/AstrBot/blob/e5e21b71aec5642f110eb115eb8e9a0d8028b7e5/astrbot/core/pipeline/respond/stage.py)：分条发送与间隔。
- [astrbot_plugin_splitter](https://github.com/nuomicici/astrbot_plugin_splitter/blob/dffce04449922e510dd72fd494f99b150b908e6d/splitter/splitter.py)：代码片段保护、短段合并及数量上限。
- [astrbot_plugin_pacer](https://github.com/Lansheen3096/astrbot_plugin_pacer/blob/74d5c45ef6b0702de8ef5765a614b86a90d86af4/main.py)：新输入到达时停止继续分条。
- [astrbot_plugin_smart_segmentation](https://github.com/exynos967/astrbot_plugin_smart_segmentation/blob/2fa230fa4a1456ce6d0addf8ecd0052bc8506ce0/segmentation.py)：按自然语义和表达意图分段，保留相关分句。本项目将停顿要求放入原聊天调用，未采用额外分段调用。
