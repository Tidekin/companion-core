# PR-H：有界媒体会话与 Android 只读观察

本阶段实现浏览器中的短时语音转录、摄像头帧分析、文本朗读，以及 Android 无交互的界面文字观察。它们通过 v2 Observation 留下来源与处理器证据，不会生成 QQ 投递任务。HTTP v1 仍是 AstrBot 与 Core 的边界。

## 已参考的现有插件

2026-10-19 通过只读 SSH 检查实际 AstrBot 容器源码及 metadata：

- [astrbot_plugin_tts_emotion_router](https://github.com/muyouzhi6/astrbot_plugin_tts_emotion_router) 3.1.5，MIT：`tts/provider_siliconflow.py` 使用 `/audio/speech` 的 model/input/voice/response_format；`tts/provider_minimax.py` 使用 sync HTTP 的 text/voice_setting/audio_setting/output_format。本实现兼容这两种请求形式，并将处理结果留在内存，不调用插件的 `_send_manual_tts` 或 QQ 发送方法。
- [astrbot_plugin_screen_companion](https://github.com/menglimi/astrbot_plugin_screen_companion) 3.2.2：参考其显式屏幕分析、远程来源和设备状态分层。插件自己的自动监视、浏览历史、磁盘音视频缓存不由本模块启用。

`HTTPMediaProvider` 仅负责提供者协议，不管理存储、授权、预算或投递。STT 为 OpenAI-compatible multipart transcription，图像分析为 OpenAI-compatible chat/completions 的 data:image 输入；TTS 支持 `openai`（包括相同协议的 SiliconFlow）和 `minimax` sync HTTP hex 输出。自定义完整 HTTPS URL、模型、密钥和 voice 在运营配置中指定，模型不能修改。未知协议须新增显式适配器。

## 启动方式

所有开关默认关闭，媒体日成本预算默认 0。启用对应观察能力、独立观察适配器及 `media.enabled`，并配置实际供应商的完整 endpoint。以下为结构示例，价格必须根据供应商当前计费和单块上限填写；不提供未经验证的价格默认值。

```toml
observation_enabled = true
voice_enabled = true
camera_enabled = true
android_enabled = false

[observation_adapters.media_voice]
source = "voice"
media_analysis = true
token = "<独立适配器口令，至少24字符>"
[observation_adapters.media_camera]
source = "camera"
media_analysis = true
token = "<另一独立适配器口令，至少24字符>"

[media]
enabled = true
max_session_seconds = 180
max_session_bytes = 2000000
max_daily_bytes = 10000000
max_chunk_bytes = 64000
max_output_bytes = 256000
speech_voice = "<供应商支持的音色>"

[media.stt]
url = "https://<你的供应商>/v1/audio/transcriptions"
model = "<STT模型>"
estimated_request_micro_usd = 0 # 可选固定调用估算，仅用于费用记录
[media.tts]
url = "https://<你的供应商>/v1/audio/speech"
model = "<TTS模型>"
protocol = "openai" # MiniMax sync HTTP 改为 minimax 并填写对应完整URL
estimated_request_micro_usd = 0
[media.vision]
url = "https://<你的供应商>/v1/chat/completions"
model = "<视觉模型>"
max_request_micro_usd = 0
```

密钥使用部署环境注入；不要把密钥放入提示词、Observation 或版本控制。URL 禁止嵌入账号密码、查询参数、fragment，HTTP 适配器不跟随重定向。

通过 HTTPS 或 localhost 打开 `/media`，输入本次 owner control 口令、选中需要的设备和预算，再点击“授权并开始”。口令输入框随后清空；浏览器只在内存保存本会话 token，不使用 localStorage/cookie。每次会话单独授权，不能自动续期。服务端仅保存 token 哈希。

## 会话与 HTTP 契约

| 接口 | 授权 | 效果 |
| --- | --- | --- |
| `POST /internal/v2/media/sessions` | owner control | 为明确 device_id、modalities、duration_seconds、max_bytes 建立短时 consent/session |
| `GET /internal/v2/media/sessions/{id}` | 本会话 token | 复查 capability、consent、隐私 epoch、到期、剩余流量 |
| `POST /internal/v2/media/sessions/{id}/chunks` | 本会话 token | 有界的一次 STT、图像分析、TTS 或 Android XML 观察 |
| `POST /internal/v2/media/sessions/{id}/close` | 本会话 token | 关闭本会话并取消本进程在途请求 |
| `POST /internal/v2/media/sessions/{id}/revoke` | owner control | 管理端关闭指定会话 |

modalities 为 `voice`、`camera`、`android`，分别绑定 `audio.transcribe`、`camera.observe`、`android.observe`。voice 会话包括明确启用的麦克风转录和文本朗读。请求含独立 request_key、operation、mime_type 和 base64 内容，朗读改用 text。最大原始块 64,000 字节，兼容现有 128 KiB HTTP 请求限制。服务端先校验会话再解码；处理返回后的最终授权检查、Observation、请求关联和预算结算在同一数据库事务中完成，遗忘无法插入其中造成证据与请求关联分离。

浏览器使用标准 `getUserMedia`、`MediaRecorder` 与 JPEG canvas 帧：每段音频为独立完整容器，约 3 秒；摄像头约 5 秒一帧。输入与处理有背压，不积压录制队列。它是有界 HTTP 媒体会话，没有点对点 WebRTC/SDP/TURN，也没有 QQ 原生语音或视频通话。朗读文本由 owner 明确提交，当前页面不自动生成聊天回复。

“立即关闭”先同步停止全部本地 track、录制器与播放器，再发服务端关闭请求。切到后台、离线事件、设备断开和本地到期也执行相同动作。状态轮询间隔约 1 秒，状态网络请求 5 秒超时，服务端拒绝后停止采集。跨进程的在途供应商请求可能无法物理取消，但结果复核会抑制已撤销会话的持久化/返回；原请求的预算不会退款或自动重发。

## 成本、流量、幂等和遗忘

- 每块原始输入计入会话及每日累计流量预留。TTS 额外预留最大输出大小；这统计媒体有效负载，不声称精确计量 TLS/HTTP/IP 开销。
- 有价格的每块请求在网络调用前将运营配置的上限写入 `model_usage`，同时检查媒体日预算和 Core 全局月预算。全局统计包括模型与隔离工具的已预留、已结算和 UNKNOWN 费用。成功保持上限估算，超时、错误、取消保持 UNKNOWN 估算，避免未知计费下重复消费。
- request_key 在 session 内唯一；相同 key 成功或不确定均不再调用，冲突内容返回 409。TTS 音频不缓存，重复请求无法重新下载已丢弃的音频。
- 原始音视频和 Android XML 只在当前请求内存中存在。数据库保留文字结果、原始媒体 SHA-256、处理器标识、设备/会话/consent 来源。图片只记为 image_analysis/FETCHED，不冒充完整视频 WATCHED。
- 观察遗忘执行现有依赖闭包与 erasure ledger，并清除 media request 的关联 event_id/fingerprint。备份恢复会撤销所有旧 consent，旧媒体 token 无法重新授权设备。隐私 epoch 改变使旧会话失效。
- 媒体接口不执行 DSH/Android 点击或输入/Windows MCP/QZone/公共评论，也不会调用 QQ 发送。

## Android 只读接入

环境检查不启动模拟器、不安装组件、不截图、不读取设备内容：

```powershell
.venv/Scripts/python.exe scripts/android_preflight.py
```

有授权设备后，为专用 adapter 设置 `source="android"`，启用 `android_enabled`，owner 创建 modalities `["android"]`、device_id 为 `adb:<设备序列号>` 的媒体会话。将返回的 session_token 通过当前进程环境变量 `COMPANION_MEDIA_SESSION_TOKEN` 注入，执行：

```powershell
.venv/Scripts/python.exe scripts/android_observe.py --core-url https://<Core地址> --session media_<会话ID> --serial <设备序列号>
```

采集器请求 Core 验证会话后，只允许固定 argv：`adb -s SERIAL exec-out uiautomator dump /dev/tty`。采集前与发送 XML 前均要求会话 device_id 精确等于 `adb:` 加本次 SERIAL，不能用设备 A 的授权读取设备 B。Core 忽略 password 标记节点，只提取可见 text/content-desc；拒绝 DTD/entity、无效/空/超限结果。此处没有 shell 文本入口，没有 tap/input/swipe/应用启动能力。设备撤销后正在进行的 dump 可能完成，但结果传输前会再次验证，Core 也在处理前后复核。

## 已验证范围

自动化覆盖授权、scope、到期、撤销、关闭取消、隐私变化、字节/费用预算、并发预留、幂等、不确定成本、无原始媒体落盘、观察证据、HTTP STT/TTS/MiniMax/vision 请求格式和 Android 禁止未授权采集。

Playwright 使用真实浏览器、内存合成的视频/音频 track 和 mock 供应商验证：音频与图像帧形成 10 条 Observation 后费用预算拒绝继续调用，全部 track 自动停止；点击“立即关闭”以及后台可见性事件都使全部 track 进入 ended；localStorage 与 sessionStorage 为空。整页截图已检查，无文字截断、重叠或不可见控件。此验证不涉及真实设备和付费服务。

2026-10-19 本地只读预检：Windows 11 AMD64，32 逻辑核，adb 可用，但授权设备 0。远端开发 VPS（私密目标引用）只读检查：Linux 5.15.0-187-generic x86_64，约 7.75 GiB RAM、5.58 GiB available；存在 `/dev/kvm`，没有 adb 和可见 binder/binder_linux 节点。这些事实不足以认定 Android 模拟环境已经就绪。未安装/启动模拟器，未采集真实摄像头/麦克风，未调用付费供应商，未发起 QQ 通话。

API 参考：[MDN getUserMedia](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)、[MediaRecorder stop](https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/stop)、[Android adb](https://developer.android.com/tools/adb)。
