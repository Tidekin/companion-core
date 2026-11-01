# Applio 自主唱歌与 B 站点歌接入

状态：主人已批准本文范围；原生 voice 修订已合并，唱歌实现与整曲本地验收已完成，等待完整 CI 及统一发布。唱歌尚未启用。2026-11-02。

主人已要求模型自行决定何时唱歌、按歌名或链接点歌、自行从 B 站找歌曲，并明确允许整首歌拆成多个语音气泡顺序发送。沿用已认可的 Applio 音色与参数。

## 现有实现与缺口

以下记录设计时的基线。当前实现已补齐查询、独立 `sing` 操作、来源绑定、完整结果与逐段投递；日常 v1 已在 ACTIVE75/config9 启用，原生节点 READY。唱歌仍需独立登记新 manifest、迁移原节点日志并由同一协调人上线。

- SDK 已能选择文字或 `reply_with_voice`，原生 Reader 已支持有界 B 站搜索、来源授权及延迟工具恢复；`BilibiliToolReader` 已接受 Core 选定的 `query`。当前聊天发现工具没有向模型暴露查询参数。
- 原语音节点 wire 只有 `asr` 与 `tts`，数据库同样约束这两种操作。普通朗读的每段上限为 60 秒、4,000,000 字节，不能把整首翻唱伪装成朗读结果。
- 已验证的 Applio 3.7.1 本地脚本可进行人声换声、伴奏混合与完整性检查；尚未被 Core 派发，也没有来源绑定的结果验收和唱歌投递。
- B 站现有媒体合同服务视觉帧/短视频采样，不交付完整歌曲音频。需要明确公开歌曲音轨的读取和处理范围，不能把视觉采样许可当成歌曲读取许可。
- 生产仍为 ACTIVE69/config6；原生 `model_carry.purpose_revision` 拒绝变更既有 voice 能力摘要，错误为 `explicit private purpose addition required`。已选 v1 的登记也因此未完成；没有执行生产 STOP 或修改 ACTIVE 数据。

## 方案比较与建议

| 方案 | 范围与代价 |
|---|---|
| 仅发送已有歌曲 | 可快速使用缓存，但不能完成新歌点歌与自主 B 站选曲 |
| 外部 Agent 自行下载、换声并直接发送 | 会在外部形成另一套来源、任务和发送责任，失去 Core 的原生授权与 UNKNOWN 约束 |
| 扩展现有 Reader / Voice / Outbox | 建议。Core 作出决定和准入，隔离节点只处理获准歌曲，QQ Bridge 保留原发送事务与逐段回执 |

不建立通用插件总线、第二套任务调度、独立人格/记忆库或任意 shell/URL 工具。

## 拟实施合同

1. 模型可查询歌曲候选、核对视频版本并选择歌曲。歌名点歌复用 Reader 搜索；链接点歌固定到真实 `bvid/cid/part`。模型只能引用实际发现/读取的候选，不能自行编造音轨 URL。视频多分 P 时保留实际分 P 身份，不把合计时长当作单首歌时长。
2. 唱歌是独立、类型化的语音操作。原始任务绑定 owner 私聊来源、AuthorityRef、voice 配置、歌曲来源与读取授权、已选音色及原生处理器摘要。只把需要的歌曲参数交给节点，不交完整聊天历史或平台 Cookie。
3. 固定 B 站音频读取复用 Reader 的公网地址校验、WBI 身份核对、配额和执行现场授权检查。仅处理当前获准的视频音轨，不增加关注、评论、点赞或账号写操作。账号 Cookie 保留原隔离 Reader 环境；不能为方便而复制到本地模型。
4. 本地处理用已验证的 UVR5 HP5 分离主唱、Applio 原生换声，再混回对应伴奏，保留原旋律与完整时间轴。模型、索引、ContentVec、RMVPE、分离器和相关代码进入实际资源清单。无自动下载模型、换音色或联网 TTS。
5. 初始硬上界：一首不超过 10 分钟、一次仅处理一首；原始压缩音轨不超过 32 MiB。总时间、磁盘/内存、下载次数和输出字节均有界，实际部署前按真实硬件验证。超界返回具体原因，不能静默裁成短版。自主触发复用现有 private/proactive 准入及额度，不新增后台扫描循环。
6. 完整作品生成后统一转成 QQ 语音可用的 PCM16、单声道 32 kHz；每个气泡不超过 55 秒和 4,000,000 字节。优先在接近上限的静音处切开，源帧必须连续、不遗漏、不重复；每段包含曲目、序号、总段数与精确时间范围的内部绑定。
7. 逐段投递复用已有有序回复与 Outbox 机制：一段确认后才允许下一段；原发送结果 UNKNOWN 时停止该组后续派发，保留原责任，不重做整首或重发前段。生成完成不等于已经发送。STOP、来源遗忘和权限变化阻止后续处理/发送；原 UNKNOWN、清理和擦除回放保持。
8. 已生成且来源/配置仍有效的同版本作品可复用，缓存与原素材有明确留存和删除归属；不能通过跨任务缓存绕过来源撤回、音色变更或旧恢复代际。

## 上线所需的原生授权修订

建议单独增加仅针对既有 voice 能力的配置修订路径，以精确主人确认绑定旧/新 voice 摘要及候选。仍走原 STOP、封存、Bridge 交接和当前授权激活；保留其他用途、私聊目的地、付费审批/预算、源任务和全部 UNKNOWN 责任。歌曲音轨读取范围同时进入相应受控配置与来源批准。

替代方案是完成离线工具与测试，暂缓生产登记。不能借 model renewal 更改 voice 摘要，也不能修改 ACTIVE policy、重置日志或伪装旧 manifest。该修订已按主人确认的范围实现并合并于 PR282；唱歌上线仍须使用同一原生配置流程绑定本次新摘要。

## 验证与交付切片

1. 原生本地处理及分段：真实已知歌曲，检查模型与源摘要、分离/换声、音轨对齐、活跃人声完整性、削波、逐段上限、重组帧一致；不发送 QQ。
2. 来源与执行合同：迁移保留旧 ASR/TTS/UNKNOWN，真实 Core 入口与独立节点，模拟 B 站/供应商传输，覆盖搜索、点歌、失效授权、目标/分 P 替换、断网、超时与取消清理。
3. 模型选择与有序发送：真实 SDK 延迟工具恢复和模拟 OneBot，覆盖自主选择、指定歌曲、缓存有效性、发送 UNKNOWN 中止、重启不重发、STOP/遗忘及恢复回放。
4. 独立原生权限修订与唯一协调人上线，记录当前 manifest、READY 心跳和真实 owner 输入验收。不得用离线产物或部署文件声称线上可用。

## 已核对来源

- [Applio 原生推理文档](https://docs.applio.org/getting-started/inference/)；实际代码沿用固定 3.7.1 / `6f69726b140c615c8cc6831a7db8b7c5e48ba8af` 的 `core.run_infer_script`。新文档不能替换已验证版本的实际 API。
- 本地 GPT-SoVITS `d7c2210da8c013e81a94bfc7b811a477c99fd506` 的 `tools/uvr5/vr.py`：`AudioPre._path_audio_`；已有分离回执和实际模型资源沿用原实验。
- [DSH jobs-local](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/jobs/jobs-local/src/index.ts)：`killJob` 的停止请求和真实终态分开，不复制进程内任务注册表。
- [DSH agent-loop](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/core/agent-loop/src/index.ts)：停止接纳后等待已跟踪工作的 teardown，不把取消等同于回滚。
- 本仓库 `agent_reading.py`、`bili_media.py`、`voice_jobs.py`、`voice_protocol.py`、`voice_media.py`、`reply_segments.py`、`owner_delivery.py`、`model_carry.py` 及 [外部能力合同草案](external-capabilities.md)。草案不代表已有可执行通用协议。
