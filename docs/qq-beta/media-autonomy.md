# 私聊媒体与 QQ 账号自主能力

本文对应本仓库的可选文件、B 站采样、拍一拍、本地语音和账号动作合同。
[`config.example.toml`](../../config.example.toml) 中这些能力的总开关默认关闭。
配置值、部署文件和合成检查不构成生产激活；生产只由唯一部署人冻结候选、配置、
镜像、账号身份和窗口材料后执行。已批准范围内的常规动作由程序执行，
不增加逐条人工审批。发送结果不确定时的主人核对是异常责任处理。

## 共同激活条件

Core、Bridge 使用同一份已批准的 `owner_private_v1` 窗口，绑定真实 owner/bot、
capture 公钥、代码与配置摘要、原 Budget Journal、截止和输入/发送上限。
新能力的配置及范围进入同一计划；运行中改配置、模型、凭据或素材策略会使原摘要失效。
新配置通过 `Settings.load()` 后仍需沿原窗口交接流程生效。
账号来源凭据、Reader token、Bridge token、Console 主人凭据和语音节点凭据分别保管。

| 能力 | Core 开关及必要配置 | 原窗口范围 |
|---|---|---|
| 私聊文件 | `content_enabled`、`observation_enabled`、`private_files.enabled`；固定 parser 镜像与 socket；`source="qq_file"` 的 `content_file` adapter | `file_scope="owner_file_v1"`、`rich_input_scope="references_v1"` 和 capture 公钥 |
| B 站视觉采样 | `content_enabled`、`observation_enabled`、`bili_video.enabled`；Bili Reader、付费图片/视频输入策略及真实可用的视频网关 | `link_scope="owner_links_v1"`、`bili_video_scope="bounded_video_media_v1"`、完整 `bili_video` 配置的 `bili_video_policy_sha256` |
| 拍一拍 | `poke_policy.enabled`；原生回拍另需 `allow_native=true` | `poke_scope="owner_poke_v1"`、`poke_policy_sha256` 和 capture 公钥 |
| 本地语音 | `voice.enabled`、Core 签名私钥、独立节点凭据、实际 manifest 和本机模型 | `voice_scope="owner_voice_v1"`、`voice_policy_sha256` 和 capture 公钥 |
| QQ 账号动作 | `social_autonomy.enabled`、`scope="qq_social_v1"`、授权摘要及各动作开关；资料观测、头像审核目录 | `account_actions_scope="qq_social_v1"`、相同的 `account_actions_authorization_sha256` |

`bili_video_policy_sha256` 使用 `bili_video_protocol.json_sha256(config.model_dump(mode="json"))`；
拍一拍使用完整 `PokePolicy` 的规范摘要；语音使用 `VoiceConfig.authority_sha256()`。
摘要必须从已冻结配置及真实资源生成，不能复制测试中的占位摘要。
付费请求还需原 `paid_model`、窗口次数、并发、单次费用及累计合同均允许。
这些能力没有扩大 M0/M1 的执行范围。

Core 与 Bridge 通过 HTTP 交换原生回执；`companion_delivery_protocol` 只包含无存储、
无网络的回执格式与校验。独立 Bridge 镜像不安装 Core；两个运行时摘要都覆盖共享协议。

## 私聊文件和宿主解析器

只有已认证主人私聊中原始文件段可进入流程。Bridge 使用捕获的原 `file_id` 调用
`get_private_file_url`，按当前文件租约下载、校验字节和 SHA256，再提交签名原件。
Core 先持久化暂存；独立 `content-worker` 执行解析。不能把聊天里的 URL、路径或模型参数
当作文件下载授权。混合图片/文件使用原持久队列；已提交项目重启时先核对原状态。
队列仍在 `DOWNLOADING` 时，可在原图片／文件租约和签名来源仍有效的条件下重新读取，
不重新 claim 或延长期限；语音重新读取前须确认原 ASR 上传仍被允许且没有已存在任务。
持久化 `UPLOADING` 后仅核对原接收结果，不能重复上传或启动第二次 ASR。
完成任务的引用在读取其结果前释放，异常仍向调用者报告。读取恢复依据
[HTTP 幂等语义](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)，
保留原队列阶段、来源检查、费用和不确定效果责任。

支持 `.txt`、`.md`、`.docx`、有文字层的 `.pdf` 和经 LibreOffice 转换的 `.doc`。
无文字层页面不做 OCR；转换、截断和局部结果保留 `PARTIAL` 及原因。
证据包含原文件摘要、解析器/引擎、页码或段落/表格位置与文本摘要，作为不可信文件内容，
不能声称已读到缺失页面。默认单文件 8 MiB、展开 32 MiB、200 页、2,000 条记录、
12,000 字符；CPU 15 秒、墙钟 30 秒、内存 768 MiB、scratch 128 MiB、96 个进程。
Core 的上传/worker 租约和等待、暂存保留时间还需满足 `PrivateFilesConfig` 的关系校验。

宿主拓扑固定为：

```text
Bridge → Core 文件暂存 → content-worker → 私有 parser.sock → ParserHost → 一次性离线容器
```

只有 ParserHost 访问宿主 `/var/run/docker.sock`。Core API、聊天、life、diary、content
等任何 Core 服务均不挂 Docker socket；仅 Content worker 挂只读的 parser socket 目录。
parser 容器没有 Core 数据库、账号/模型凭据或宿主目录挂载，使用固定镜像、`--pull=never`、
无网络、只读根目录、无 capabilities、独立 CPU/内存/PID/文件大小限制和容器内 `timeout`。
解析输入走 stdin；宿主启动器死亡后仍有容器内截止。

使用 [`companion-file-parser.service`](../../deploy/companion-file-parser.service)。
这是待部署的 service 模板，安装前由唯一部署人核对 `/opt/companion-core`、Python、Docker
路径和 worker GID。宿主配置位于 `/etc/companion/file-parser.env`，root 所有、权限 `0600`：

```ini
COMPANION_FILE_PARSER_IMAGE=sha256:894580dbbc57be61d67fcf3745e8980767a821a7624fd276510177365bf42b93
COMPANION_FILE_PARSER_LIMITS_JSON='{"max_file_bytes":8388608,"max_expanded_bytes":33554432,"max_zip_entries":512,"max_compression_ratio":200,"max_pages":200,"max_records":2000,"max_text_chars":12000,"max_table_depth":8,"max_pdf_stream_bytes":8388608,"cpu_seconds":15,"wall_seconds":30,"memory_bytes":805306368,"scratch_bytes":134217728,"max_processes":96}'
```

上面的 digest 是已完成隔离原生验收的 parser 子组件镜像，需在目标 Docker daemon 中
实际存在。唯一验收回执位于工作区根的
`.local/native-private-file-20261024-27db087fc4d2/native-receipt.json`；
它不代表本次总候选、这个新 service 单元或生产功能已验收。
若重建后镜像 ID 变化，应验证新子组件并更新受控配置。

Service 使用代码支持的固定 CLI：

```sh
/opt/companion-core/.venv/bin/python -B -m companion.file_parser_host \
  --socket /run/companion-parser/parser.sock \
  --state-dir /var/lib/companion-parser \
  --image "$COMPANION_FILE_PARSER_IMAGE" \
  --limits-json "$COMPANION_FILE_PARSER_LIMITS_JSON" \
  --docker /usr/bin/docker --docker-host unix:///var/run/docker.sock \
  --max-concurrency 1 --read-timeout 15 --max-history 10000 \
  --socket-mode 0660 --socket-group 10001
```

`/var/lib/companion-parser` 必须保持属主私有、权限 `0700`。宿主创建 socket 为 `0660`、
GID `10001`，其父目录为 `0710`。Content worker 以 UID/GID `10001` 连接。
Core `private_files.parser_image` 与宿主镜像必须一致；`parser_socket` 为上述绝对路径。
宿主接受完整 limits，客户端只能不超过宿主上限；建议由同一份冻结配置生成二者。
生产保留非空 socket 路径，解析失败不会退回宿主普通进程解析。

仅在 `companion-content-worker` 的部署覆盖中增加：

```yaml
services:
  companion-content-worker:
    volumes:
      - type: bind
        source: /run/companion-parser
        target: /run/companion-parser
        read_only: true
        bind:
          create_host_path: false
```

目录挂载保留 socket 重建后的可见性。先启动 parser，再核对实际 socket 属主、组和权限，
然后启动 Content worker。安装时运行 `systemd-analyze verify` 检查单元；
unit 的环境变量、目录与隔离选项遵循
[systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) 和
[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

ParserHost 持久记录请求摘要、实例和处理阶段；同一 request ID 不再启动第二次解析。
不得删除其 state 目录来“重试”。断连、无法确认旧容器终止或历史容量满时保持不可用/未知。
文件来源被撤回或擦除时，结果提交再次核对原来源，不能借迟到的解析结果恢复内容。

## B 站两阶段 Reader

原生 Reader 先取得首页/第一分 P 的 metadata、字幕及可选评论/弹幕，保留每个来源的
摘要和实际范围。Core 冻结 `bvid/cid/part`、来源事件和采样计划后才创建媒体子任务。
Reader 对固定 API 进行身份复核和 WBI playurl 解析，只下载一个批准的完整媒体表示，
无重定向、备用格式轮询或模型指定 URL；CDN 不收到账号 Cookie 或 Core token。

第一阶段最多四帧；视频模型仅返回局部分析及是否需要原定片段。第二阶段最多三段、
单段 20 秒、合计 30 秒。原件最大 32 MiB，素材单项 4,000,000 字节、两阶段合计
8,000,000 字节，输出最长边 512。模型不能增加来源、段数或任意时间范围。
实际帧 PTS、片段范围与来源哈希进入聊天上下文；字幕、评论、弹幕与视觉证据分别标记。
结果保持局部视觉观察，不分析音轨，也不提升为 `WATCHED` 或“完整看过”。

部署 Reader 使用其独立观察凭据，并配置 Bili adapter：`source="bilibili"`、
`media_analysis=true`、`reader_max_requests=8`。原探索请求/字节预算需覆盖元数据及媒体
子任务；子任务最多预留 8 次请求和 33 MiB，默认完整截止 180 秒，并受窗口更早截止约束。
零预算不会因为媒体开关变为正数。

```sh
python -m source_reader --source bilibili --protocol reading-v1 \
  --adapter-id bilibili_source --core-url https://CORE_PRIVATE_ORIGIN \
  --discussion --video-media --media-limits /run/bili-media-limits.json
```

通过私密环境注入 `COMPANION_READER_TOKEN`。`/run/bili-media-limits.json` 必须是完整的
`Settings.load().bili_video.model_dump(mode="json")["media"]` 对象，所有字段与 Core
完全一致；不能只填写改变的字段。示例配置已列出完整对象。使用默认 limits 时可省略
`--media-limits`，但 Core 也必须保持相同默认值。现有 `compose.reader.yaml` 默认命令没有
这些 opt-in 参数，部署人需在私有覆盖里明确设置、挂载 limits 文件并保留 `/scratch` 限制。

启用前实测付费网关对内联 `image_url` 和无声 MP4 `video_url` 的支持；文本连通不能证明
该能力。原 `paid_model.image_input`、`paid_model.video_input`、`video_model_key`、已知价格、
次数和 Token 限制均需配置。preview 和 clips 在原 `model_usage`、原 Budget Journal 记账，
共享同一 `content:<item_id>` 视频任务，默认累计 1 CNY；所有云调用默认每日合计 20 CNY。
`cloud_budget` 使用冻结汇率与配置价格估算，不等同供应商账单或账户全局硬封顶。
原更严的费用与并发合同继续生效。

同一输入的所有视频阶段共用原聊天逻辑请求，始终保留尚未使用的聊天/记忆复核调用次数。
显式启用且冻结 Bili scope/policy 后，可将 `paid_model.per_request_calls` 配为 6，覆盖两个
视频各两阶段及正常回复。4 仍是合法配置，但片段额度不足时保留帧分析、跳过后续片段；
不能为每阶段创建新的逻辑请求来重置限额。每输入金额上限也需覆盖选定的实际费用预留。

prepare/upload 回执丢失只查询原请求摘要的 receipt；不重复下载或重复付费。
模型 UNKNOWN 保留预留及费用责任，重启不重付。来源擦除阻止新事实提交，已发生费用不退还。

## 拍一拍和本地语音

拍一拍只接受签名原生 notice 中主人发送、机器人接收的私聊身份。
策略提供 `auto`、`text_only`、`poke_only`、`none`；默认合并 2 秒、冷却 30 秒、日限 20。
允许原生回拍后，Bridge 通过固定 `friend_poke` 动作作用于原主人身份。
ACK 是原生动作回执，不伪造文本 message ID；结果未知不回退为文字再发。

本地语音使用独立出站节点：节点主动通过固定 HTTPS Core origin 领取已签名任务，
Core 不拨入节点，也不把模型/消息投递责任交给节点。
当前 provider 为本机 faster-whisper ASR 和受管 GPT-SoVITS TTS；配置实际模型文件、
权重、参考音频、服务 revision 及独立凭据，并从本机文件计算 manifest。
`voice.enabled` 与旧 `voice_enabled`/`media` 开关是独立合同，后者不能替代语音节点配置。

`voice.reply.mode="model"` 是默认回复策略。模型可直接输出文字，也可使用同一次生成中的
`reply_with_voice(text)` 最终输出选择语音；它不是额外的模型调用或立即发送工具。
语音选择保存在原 SDK Conversation 的 Core metadata 中，并绑定原 Outbox 发送证明。
选择语音时整段保留为一条，避免文字分条与语音互相抢占；普通文字仍按原分条设置发送。
长度、权限、节点任务期限和合成失败继续使用既有检查与文字回退；已经开始 QQ 发送后出现
UNKNOWN 时不另发文字。仅有聊天授权、语音关闭或没有 TTS 节点时不提供语音最终输出。
显式配置 `voice_input` 保留只跟随语音输入的策略，`auto` 保留自动转换符合条件回复的策略。

这里复用已固定的 Pydantic AI Slim 2.60.0
[ToolOutput](https://github.com/pydantic/pydantic-ai/blob/main/docs/output.md#tool-output)
及现有 [SDK 会话和 DSH 参考](../plans/2026-10-25-sdk-owner-runtime.md#reused-contracts-and-primary-references)。
输出工具的 schema 与普通工具一起计入原上下文规划、付费测量和预留，模型映射不接管存储、权限或投递。

```sh
python -m companion_voice_node.worker --config /PRIVATE/voice-node.json --print-manifest
python -m companion_voice_node.worker --config /PRIVATE/voice-node.json
```

节点配置是 JSON。把实际 manifest、capabilities 和上限登记到 Core 的 `voice.nodes`，
节点固定 Core 公钥；Core 的 `signing_private_key` 和各节点 token 只置于私密配置。
无可用本机 provider 时能力保持关闭，不使用云 ASR/TTS fallback。
本机 manifest 证明本机文件/配置绑定，不等于对任意外部 TTS HTTP 服务的远程证明。
只有 TTS 节点时，主人语音输入会得到明确的“转写不可用”上下文并继续文字回复，
Bridge 不上传音频，也不凭占位记录编造转写。

Bridge 仅处理当前原始 record 段，`get_record` 的返回须与原来源身份一致，经过受限本地
解码生成最多 8,000,000 字节、120 秒的单声道 16 kHz PCM WAV，并提交签名 proof。
达到上限和 SILK 覆盖不确定时明确保留 partial。ASR 接入原输入/任务；
TTS 绑定原 outbox 的 message ID、delivery key、文本、窗口和音频摘要。
发声使用原发送责任，不另建文字尝试；QQ 发送 UNKNOWN 不自动补发文字。
节点失联、取消、源撤回、租约或资源隔离失败时停止，原资源/回执不确定性按节点协议保留。
Core、节点和 Bridge 须同步升级语音 v2；递增领取序号、恢复、离线清理和 Core 清理确认
的操作见[语音清理与恢复](../voice-cleanup-operations.md)。本地清理成功不代表 Core 已确认。

## QQ 账号动作、素材和基线

账号自主动作限定 `qzone.publish`、昵称、签名和头像。它们使用 `qq_account` 投递通道、
原 Outbox 和冻结计划；模型只提议内容或已登记的 asset ID，不能指定权限、账号、URL、
本地路径或平台动作。原自主 QZone 路径固定 `ugc_right=16` 且目标列表只有主人，
不生成公共评论。昵称、签名和头像作用于已绑定的机器人账号。
纯文本公开发布使用单独的[逐稿 Core 审批](qzone-publication.md)，默认关闭。

启用资料动作前须有该字段的真实账号观测及原响应摘要。`observed_at` 表示接口响应观测
时刻，底层接口可能缓存；不能把缺失字段、好友简版 fallback 或动作成功 ACK 当新基线。
默认基线有效期 300 秒，发送后需重新观测。头像基线记录实际头像内容摘要。
基线采集器和目标协议兼容性未验证时，相关资料动作保持关闭。

Bridge 在空闲时通过 `GET /internal/v1/account-actions/profile-baselines/state` 查询当前
窗口允许观测的字段，再进行有界读取和批量提交。只开启空间发布时返回空字段集，
不会附带读取资料。昵称/签名要求完整账号资料且响应 QQ 号与绑定账号一致，头像从固定 QQ CDN 的本账号图像
取实际字节摘要；分别保留响应凭证。读取失败不覆盖已有基线，主人新输入优先打断采集。
已知平台拒绝的完全相同回执允许重放；内容不同的回执仍报冲突，不触发第二次平台动作。

SnowLuma 2.0.3 的完整资料响应可能包含空字符串 `uid`。此时采集器还要求完整格式的
21 个必备字段及正确类型，并核对等级、扩展状态的别名一致；注册时间和会员信息仍可缺省。
完整接口明确返回的空签名按原响应记录，缺失或 `null` 的签名不能补为空串。
格式依据固定版本的[完整资料与降级分支](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/onebot/src/modules/contact-actions.ts#L376-L471)
及[原生资料解码](https://github.com/SnowLuma/SnowLuma/blob/db142db9e6398c779283eab9afc3552da1e2c361/packages/protocol/src/oidb-services/contacts/fetch-user-profile.ts#L111-L180)。

头像只选审核 catalog 中的静态 PNG/JPEG/WebP。Core 的 `ReviewedAsset` 登记包括公开
来源页/下载出处、许可标签及条件、许可摘要、审核回执/策略摘要、尺寸、字节和有效期。
Bridge 配置绝对只读 `account_asset_directory` 和同一 `account_asset_policy_sha256`；
目录中文件为 `SHA256(asset_id).json` 审核记录与 `<asset_sha256>.bin` 原素材。
执行时再次校验文件、摘要、许可策略、有效期及图片格式，不在投递时下载新素材。
目录或素材不能是符号链接或组/其他用户可写文件。

已批准范围内的模型建议经过原生来源、频次、冷却、基线和素材校验后自主执行。
动作 ACK 只陈述协议已接受；QZone 需要真实 tid，资料动作不制造 message ID。
平台拒绝、未知、基线过期或来源擦除各自保留原状态，不以重试另一动作消除责任。

## 发送不确定后的主人核对

按以下顺序操作，使用原 spool、Window Journal、Budget Journal 和最新 ErasureLedger。
这条路径只关闭已存在的发送责任，不执行新的 QQ 动作，也不补记平台 ACK。

1. 使用原私有 Bridge 配置执行 `window-stop`，等原接收/发送进程退出并取得
   `CORE_STOP_CONFIRMED`。`STOP_REQUESTED`/`WINDOW_STOP_PENDING` 或 Core 不可达都不等于
   已确认停止。不要删除 STOP 文件、旧租约、日志或 spool 来恢复。
2. 独立进程获取原 spool 的排他锁，使用原 capture key 生成停机证明：

   ```sh
   python -m companion_onebot_bridge --config /PRIVATE/bridge.toml \
     window-delivery-prepare --message-id msg_ORIGINAL_ID
   ```

   此进程以 reconciliation-only 模式运行，没有接收/发送循环。证明绑定原动作、内容、
   计划、账号、日志 head 和已确认的 Core STOP；只有真实 READY 后控制台允许决定。
3. 主人在已登录的同源 Console 检查实际平台结果，选择 `SENT` 或 `CANCELLED`。
   对应接口为 `POST /v1/console/deliveries/{message_id}/reconcile`，保留同一
   `Idempotency-Key` 与同一 outcome。主人判断独立于 Bridge 停机证明。
   不确定时继续保留 UNKNOWN，不猜测 `CANCELLED`，不人工编辑数据库或日志。
4. Bridge 查询并核对原 Budget Journal 决定，再记录原 Window Journal：

   ```sh
   python -m companion_onebot_bridge --config /PRIVATE/bridge.toml \
     window-delivery-reconcile --message-id msg_ORIGINAL_ID
   ```

   `AWAITING_OWNER` 继续暂停；`OWNER_RESOLVED` 也保持暂停。丢失 HTTP ACK 时查询同一
   journal receipt，不创建新的决定或发送。多条回复须逐一核对，只有全部责任闭合才关闭原输入。
5. 所有未决模型/发送和来源工作按原合同闭合后，才执行原 `window-handoff`，提交后续
   已冻结计划所需的 `--window`、`--inventory`、`--authorization`，然后单独 open。
   核对收据本身不会取消 STOP、刷新窗口或自动恢复后台工作。

人工发送核对不退回已消耗的发送次数，不改变已发生的模型费用，也不解决另一个模型
UNKNOWN。擦除后的来源只保留核对所需摘要，不能从决定、旧备份或迟到回执恢复私密内容。

## 历史数据规模与协议边界

语音节点领取任务时，只协调 `QUEUED`、`LEASED`、`RUNNING`、`READY` 和 `TRANSCRIBED`
状态，复用现有节点/状态索引。已关闭的历史任务继续保留；已完成结果的有效期和来源检查
仍执行，`UNKNOWN` 的节点占用仍须通过原资源握手解除，不能因查询筛选而释放。

视频任务的模型调用余量按真实 request ID 合并直接任务归属与 admission 归属，重叠归属
只计一次。新增的 `model_usage(job_id)` 索引只改变查找路径，不改写账本或重置额度；
当前请求排除、前台回复保留和未知费用处理沿用原规则。

语音投递绑定与音频 envelope 属于无存储职责的 `voice_protocol`；Core 的发送前检查和
停机核对复用独立投递协议的绑定校验。非法 `voice` 仍返回 `400 INVALID_CONTRACT`，
字段位置统一为 `body.voice`，不回显提交值；有效形状但绑定或租约失效仍返回语义错误。

## 验证范围

文件 parser 子组件已有固定镜像的 Linux 隔离原生回执；部署人仍需核对本次完整候选的
配置、service、Content worker socket 权限和入口链。Bili、语音、拍一拍和账号动作的合成
测试覆盖真实本地 HTTP/worker/原生编解码与外部 Mock transport；它们不证明真实 QQ、
供应商视频质量或具体本机模型可用。保留集成负责人的唯一最终 CI summary 与部署验收回执，
不要把各阶段测试数量或重复候选身份当作生产启用证明。
