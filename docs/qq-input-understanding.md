# QQ 引用、卡片和多图理解

本页适用于原生 `companion_onebot_bridge` 链路；常驻入站通过 [OwnerAuthority](owner-authority.md) 授权当前主人消息、媒体与记忆用途，原图沿已有签名领取、下载检查和上传链处理。Bridge 将主人当前文字与引用资料分别传入 Core。引用、转发节点、卡片、识图结果及记忆都有各自的来源；其中只有主人当前文字可以触发命令或作为新的主人事实证据。

## 已实现的路径

| 输入 | 处理方式 |
| --- | --- |
| 引用消息 | 优先读取消息链中的正文；缺失时按消息 ID 调用 `get_msg`。 |
| 合并转发 | 按原顺序展开文字节点，保留显示昵称和节点路径，支持有限层数的嵌套。 |
| QQ JSON 卡片 | 提取支持的标题、摘要和链接字段；Bilibili 视频与小黑盒帖子链接交给已有 Reader。 |
| 多张图片 | 按顶层支持图片的顺序独立编号、领取、下载和校验；支持普通图片及收藏/自定义表情 `sub_type=1`、动图表情 `sub_type=11`，数量可配置或设为 `"unlimited"`。 |
| 图片与记忆 | 识图线索参与限定范围的记忆检索；核对候选后，将原图、确认记忆及当前问题一起交给视觉模型回答。 |

例如两张耳机图片配合“结合我以前的需求选哪个”，最终请求同时包含两张原图和与耳机有关的获准记忆。回答规则要求区分图片可见细节、以前明确确认的需求与推测。图中文字不会被自动记成主人偏好。

这些路径复用现有事件、Content、Reader、Memory、预算账本和 Outbox。模型适配器只负责请求与响应。

## 引用正文与卡片

常驻 Bridge 在自身配置和 Core 配置中分别启用 `rich_input_scope = "references_v1"`；受控窗口则在 Bridge 窗口计划的 `live_text.rich_input_scope` 与 Core 的 `live_text_window.rich_input_scope` 中声明该范围。每条事件最多查询 4 次、等待 5 秒、展开 3 层嵌套、保留 32 个文字节点和 6,000 字符，单次 OneBot 响应最多 64 KiB，最终富消息投影不超过 16 KiB。循环引用、过深节点和超量内容会留下明确的缺失或截断标记。昵称不作为已验证身份。

读取成功、部分读取、原文缺失、不支持和截断状态分别进入上下文。只有占位符或媒体段时，不会将其冒充正文；上下文规则要求模型说明缺失范围。读取发生在主人私聊认证和重复消息检查之后；消息已提交 Spool 后的重放，以及受控输入审批，复用已绑定的投影，不再次查询可能变化的原消息。

Bridge 收到 Core 的成功接收或事件已删除回执后，会清除对应收件箱正文与捕获证据文件；重启会补做回执提交后尚未完成的删除。待批准或待上传的数据沿用原收件箱生命周期。Core 内的引用资料随原事件删除，旧备份恢复时仍执行删除日志回放。

被引用的消息如果在本会话、同一协议 epoch 下能对应到本地记录，并且那条主人消息已被删除、遗忘或撤回，或者那条回复的来源已被删除，Core 构造上下文时会把这段引用正文替换为“原文已被主人删除或撤回”。这条规则对当前输入和历史重放都生效，引用里的图片也不会附给模型；Bridge 照常从 QQ 读取。签名捕获保持原样，以便重放校验，因此这段原文仍留在新消息的事件记录里，直到该事件本身被删除。对应不到本地记录的引用按 Bridge 读到的内容保留。

卡片链接仅来自当前卡片的受支持字段。标准分享链接中的跟踪查询参数被丢弃；小黑盒固定分享路由只保留唯一合法的内容 ID。引用正文中的 URL、未知域名和短链不会自动成为 Reader 任务。支持的链接仍受已有 Reader 请求数、响应字节、来源授权和删除规则约束。未配置 Reader、读取失败或到期时，标题和摘要仍明确标为卡片自述。

受控窗口读取卡片正文需在原窗口配置中指定 `link_scope = "owner_links_v1"`，同时启用 `content_enabled`、`observation_enabled` 和所需的 Reader adapter，并运行对应的 [Source Reader 进程](design-v0.6/source-reader.md)。该字段随完整窗口计划、Core 配置摘要和接收签名绑定。Reader 在领取、实际网络请求前和结果落库时重新检查来源与窗口状态。同条输入还带图片时，卡片读取等待图片下载和批量识图结束后再领取。

## 多图与联合记忆的配置

常驻多图沿用 OwnerAuthority 的图片用途检查、独立图片准入与签名上传；`image_scope = "owner_image_v1"` 是内部规范化范围，不是常驻 Bridge 的顶层配置项。旧受控窗口中的同名范围仍属于其窗口计划。Core 同时启用 Content、Observation、`content.use_chat_model_for_vision = true`，并配置 `source = "qq_image"`、`media_analysis = true` 的图片 adapter。调整现有配置时应同时核对：

- `content.max_images_per_event` 与 `paid_model.image_input.max_images_per_request`，均可设正整数或 `"unlimited"`；每张重复图片也占一个位置。取消数量配置上限后，仍受原消息最多 64 个协议段及整体富消息字节限制。
- 每图字节、像素及固定输入 token 估值，以及累计输入、单次、单作业、每日和窗口费用限制。两次视觉请求都按实际图片数量估算；收到 usage 后按原账本结算。
- 普通图片问答的 `per_request_calls = 2`：一次批量 `content_vision`，一次携带原图的 `chat`。
- 联合记忆的 `memory_scope = "approved_facts"`、已确认记忆清单、记忆范围摘要和 `memory_enabled = true`；`per_request_calls = 3`，为可能的 `memory_review` 保留额度。没有候选时只进行两次调用。
- 原批准事实窗口保留 `history_scope = "none"`。显式[私人陪伴范围](qq-beta/private-companion.md)可同时使用近期完整 SENT 回合和原生长期记忆；历史图片保留文字观察，不重新上传旧原图。

联合理解的顺序为：批量识图提取线索 → 在批准事实中检索并用 `memory_review` 核对候选 → 将原图、选中的确认记忆和当前问题交给 `chat`。没有检索候选时跳过核对调用；图片线索本身不作为长期记忆证据。

原图仅通过内嵌 base64 传给同一个获准视觉模型。支持单帧 PNG、JPEG、WebP；GIF、动态 WebP 和 APNG 经已有帧数、像素与字节限制校验后，生成有序抽样拼图并标明抽样范围。图片子类型和 `summary` 不替代实际字节的格式校验，也不成为视觉证据。API 与聊天 worker 之间的原图暂存在数据库旁的私有目录，有效期为上传后 300 秒；事件正文、数据库和账本只保留必要的来源、摘要与状态，不存原图字节或 QQ 签名 URL。最终调用前重新核对原图哈希、隐私版本和到期时间。原生主人原图在上述期限内可供获准的同会话历史使用；其他原图按任务终态清理。启动及 worker 运行时清理到期文件，删除回放也处理旧备份旁残留的原图。

图片请求发出前仍核对原图字节和有效期。请求已发出后，接收结果、保存 SDK 会话及最终回复复用该请求内已校验的图片绑定，并重新核对签名来源；暂存文件在生成期间到期不再使已收到的回答失败。已保存的最终输出可在原图过期后恢复，继续校验原图片引用；新的模型请求仍要求有效原图，撤回、擦除、来源或签名变化仍阻止提交。

原生主人连续输入中，新文字替换前一条的回复任务，原媒体来源仍由其原签名、租约和保留期限约束。
原生图片下载和上传共用一次领取的租约，截止为领取时刻加 60 秒与该图原始处理 deadline 的较早者。
检查和重启均不续租；CDN 下载的 10 秒总截止、上传后原图的 300 秒保留期限分别保持独立。
原生 `image/check` 在同一事务中核对来源和租约，并附带现有完整 authority 检查回执。
Bridge 严格核对该回执的身份、capture、image 用途、预算账本和期限，省去其前置 authority RPC；
连接前、实际 GET headers、下载后和上传前的四次动态检查仍保留，上传前复用较晚的完整检查。
先发图片、再补文字时，后一个任务在获准历史范围内等待前图完成，并把仍有效的原图放回所属的
主人消息中；只为最新输入生成回复。Bridge 不把旧聊天任务的 `CANCELLED` 当成图片或文件来源撤回。
真正撤回、擦除、授权停止和原租约到期仍阻止后续下载或上传。

图片或文件的普通下载失败只结束该附件的处理。Core 对可证明未成功保存原始附件、未启动解析或模型的
过期原生下载使用既有 `FAILED` 状态，保留原租约、流量预留和费用记录；Bridge 根据原 slot 的
Core 终态推进队列，后续附件和文字继续处理。队列 `DONE` 表示本地处理已结束，各附件的成功或
失败仍由 Core 的状态说明。恢复时只查询原状态，不重新领取下载额度、重放 GET 或重复上传。
已经发生但结果不确定的处理保留原责任；鉴权、签名和来源绑定异常仍使 Bridge 停止。
安全日志使用 `media_unavailable`，嵌套异常保留已校验的阶段和原因码，不输出图片 URL 或正文。

常规聊天启用后台记忆提取时，应同时运行 `life` profile，由现有 `companion-life-worker` 执行提取。原批准事实范围只读取批准事实；私人陪伴范围可从主人原始文字自动提取记忆。图片参与检索不会使图中文字成为主人事实证据。

## 出站原生引用

仅限 OwnerAuthority 私聊回复，由 `quote_policy` 控制，默认 `never`。开启后，上下文中主人每条消息和小肥鱼先前每条回复前带一个
本地编号 `[编号 m加四位十六进制]`（主人消息取事件 ID、回复取首个分段 outbox ID 的 SHA-256 前四位，不改变来源证明）。模型想回应较早的
一句时在回复开头写 `【引用 编号】`；Core 解析后从回复中删除所有引用标记和模型照抄的 `[编号 …]`，再用
自己记录的来源解析目标（`companion/reply_quote.py`）。模型给出的只是编号：主人消息的 QQ ID 取自该会话
`events.source_event_id`，小肥鱼回复的 QQ ID 取自已送达首段的 `outbox.provider_message_id`，并要求内容
哈希仍然有效（擦除后不再可引用）。

| 档位 | 行为 |
|---|---|
| `never` | 不显示编号、不引用 |
| `occasionally` | 引用模型点名的较早主人消息；回复合并了多条未答输入时引用最新一条；点名最新一条时不引用 |
| `often` | 在 `occasionally` 基础上，点名最新一条时也引用 |

编号不存在、冲突、已擦除、已撤回、超出历史范围或不属于本会话时，只丢弃引用，正文照常发送。引用只写入
分段回复的第一段 `content.reply_to`，计入 `content_sha256`；重试时目标不同返回 `REPLY_GROUP_CHANGED`，
不会重复发送。带引用的回复不转语音。Bridge 只在带 OwnerAuthority 的首段 `qq.private.reply` 上接受
`reply_to`，并在文本前插入 OneBot `{"type": "reply", "data": {"id": ...}}` 段；带表情图的回复和旧窗口
路径不引用。小肥鱼自己消息的撤回尚未跟踪，只能引用历史范围内仍在记录中的回复。

## 上游依据与验证范围

协议操作参考 [OneBot v11 官方接口](https://github.com/botuniverse/onebot-11/blob/master/api/public.md)，正文解析顺序参考 [AstrBot 固定版本的引用解析器](https://github.com/AstrBotDevs/AstrBot/blob/e5e21b71aec5642f110eb115eb8e9a0d8028b7e5/astrbot/core/utils/quoted_message/extractor.py)及其[测试](https://github.com/AstrBotDevs/AstrBot/blob/e5e21b71aec5642f110eb115eb8e9a0d8028b7e5/tests/test_quoted_message_parser.py)。多图请求采用 [DeepSeek 官方视觉接口](https://api-docs.deepseek.com/guides/vision/)支持的有序文字与 `image_url` 输入。

固定 SnowLuma 的 [`get_msg` 实现](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/actions/message.ts)会刷新消息中的媒体 URL；[`get_forward_msg` 的底层转换](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/modules/message-actions.ts)也会解析节点媒体地址。Bridge 不下载或保留这些附带 URL；Bridge 的次数和时间限制不能约束协议端内部刷新的全部成本。

2026-10-25 核对同一固定 SnowLuma 版本的 [image 消息转换](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/event-converter/element-codecs.ts)：普通图片投影为 `url/file/sub_type/summary`，闪照另带 `type`；[收藏表情测试](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/protocol/tests/highway/image-sticker.test.ts)明确使用 `subType=1`。Bridge 只扩展整数允许集到 `0/1/11`，仍限制字段和 QQ CDN 主机、固定解析到公网 IP 的 HTTPS 请求、禁止重定向、体积和格式。原始段摘要仍绑定完整来源，未支持图片不会获取下载租约。

故障隔离参考 DSH QQ 固定 `c8b9158` 的[逐附件下载](https://github.com/tencent-connect/dsh-qqbot/blob/c8b9158b37817b0dbc1e514e62e16204ad47a6ce/src/transport/attachment.ts)与[正文和附件装配](https://github.com/tencent-connect/dsh-qqbot/blob/c8b9158b37817b0dbc1e514e62e16204ad47a6ce/src/transport/inbound.ts)：单个附件不支持时仍保留当前正文及附件缺失标记。本项目复用既有 `UNSUPPORTED` 投影与 OwnerAuthority 签名队列，下载约束继续由自己的来源、公网地址和签名边界负责。

常驻循环遵循 [Python 3.12 TaskGroup 的取消语义](https://docs.python.org/3.12/library/asyncio-task.html#task-groups)：
子任务未处理的失败会取消同组任务。因此普通附件下载失败在原媒体任务内收口，保留 Core 终态证据；
真正的授权或完整性异常继续向外传播，停止同组执行。

本轮验证使用合成消息、认证本地 HTTP/WebSocket、真实数据库与文件、模拟模型和网络传输，覆盖幂等、费用、来源漂移、删除回放及停止窗口。未调用真实 QQ 或付费模型；实际正文可取得率和视觉回答质量仍需在已配置运行环境验证。
