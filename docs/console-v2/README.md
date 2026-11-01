# 主人控制台第二版

第二版控制台替换了 `companion/console_static/` 的界面，接口仍是原有的 `/v1/console`，
会话、Origin、CSRF、幂等键和“结果待核对”语义不变。[prototype.html](prototype.html)
是确认过的设计稿，用演示数据展示全部页面，包括还没有后端支持的部分；直接用浏览器打开即可。

## 页面与接口

| 页面 | 读取 | 写入 |
|---|---|---|
| 今天 | `overview`、`activity`、`diagnostics`、`configuration` | `mode` |
| 活动时间线 | `activity`（分页） | 无 |
| 记忆（含“召回测试”“黑话”标签） | `memories`（`q`、`review`）、`memories/recall`（`q`，按聊天检索排序并给出关键词/语义名次、相似度和融合分，不调用模型）、`jargon` | `memories/{id}/decision`、`jargon/forget` |
| 日记与成长 | `life`、`diary/{day}`、`goals`、`reflections`、`life/topics` | `diary/{day}/correction`、`diary/{day}/forget`、`mode`、目标决定与续行、反思决定、`life/topics/{id}/close` |
| 情绪与偏好 | `affect`、`interests`、`expression-learning`、`style-candidates` | `affect/reset`、`style-candidates/{sha256}/decision`（批准或拒绝表达候选） |
| 阅读与文件（含“她看过的”和来源授权标签） | `content`、`watch-ledger`、`source-grants` | `source-grants/{id}`（撤回或遗忘）、`watch-ledger/{entry_id}/withdraw`（撤回一条看法） |
| 表情素材、语音与节点、QQ 资料行为 | `memes`、`voice`、`account-actions` | 无 |
| 执行任务 | `tools` | `tools/{id}/decision`、`tools/{id}/revoke` |
| 能力与开关 | `overview` 的能力快照、`configuration`、`autonomy` | `configuration`、`mode`、授权与调整决定 |
| 运行诊断 | `diagnostics` | `deliveries/{id}/reconcile` |
| 思考与决策 | `cognition`（`kind`、`before` 分页、`record_id` 定位） | 无 |
| 配置 | `configuration` | `configuration`、`cloud-budget` |

“今天”的时间带由当天的审计记录按动作前缀分行；超过一次读取上限时提示去时间线查看。时间线按日期筛选时会继续向前翻页，最多读取 20 页。
时间线里同一天连续、标题和状态都相同的记录合成一行（例如“连续 13 条”），点开逐条查看时间和编号；正常完成的状态不在每行重复，结果不明、失败等状态照常标出。
记忆页的关系图只用现有字段：主体、记忆类型和来源消息编号，引用同一条消息的记忆互相连接。
属于某项能力的开关和参数只在能力页出现，配置页只放模型、费用总额、密钥和运行等系统级设置；两页共用一份配置草稿，保存为一个新的配置版本。配置页上方始终显示版本状态，下面一次只显示目录里选中的一节；切换小节不会丢失草稿，目录上的圆点标出哪一节有未保存的修改。云端预算单独保存并立即生效。
对象型配置按子项显示为开关、数字和名单，也可以切换为 JSON 编辑。模型账本以美元记账，界面可按云端预算里的汇率换算成人民币显示和填写，保存时换算回微美元。
能力与配置字段的对应关系在 `app.js` 的 `CAP_FIELDS` 中维护，只显示服务端实际返回的字段，
部署保管的项目（例如 `observation_adapters`、`executive`）仍不进入浏览器。
其余每个配置字段都必须在 `operator_config.LABELS` 有中文名（`tests/test_console_catch_up.py` 检查），不按前缀归组的字段写进 `operator_config.GROUPS`。`conversation_policy` 总有值，配置页显示的就是实际生效的策略。网页阅读的网站名单已从读取适配器配置拆出为 `web_reading_hosts`，在网页阅读能力里填写。

“交给她决定”在日记和自主活动调度的详情里设置：只由我控制、她提议我批准、她在范围内自己调整。她的调整只能让能力少运行，经每天的反思提出并引用原话，立即生效、可撤销；引用的原话被遗忘时调整撤回，从备份恢复后全部作废。接口为 `autonomy`、`autonomy/{capability}` 和 `autonomy/adjustments/{id}/decision`。

思考与决策读取原生 `wake/life/goal/diary/reflection/nightly/model/rem` 记录，按原始记录时间排序，统计只涵盖本页已加载的记录。各行独立显示状态；调度选择与执行完成分开。任一类型读取失败时显示同步失败并保留上次成功的数据，首次读取失败则显示错误。推理与未留存的候选集合不暴露，详见 [UI 接口约定](ui-supplement.md)。

“交给她决定”的接口覆盖所有可以安全减少运行或费用的能力，`autonomy` 返回每项能力的 `wired` 字段，列出运行逻辑已经读取她调整的参数；目前只有日记暂停和自主活动调度。其余能力的运行接入按同一接口另行完成，界面会提示尚未接入。

语音页按字幕显示：`voice` 条目的 `detail.transcript`（你发的是转写，她发的是合成原文）和可选的 `detail.transcript_truncated`。后端目前不返回语音内容，字幕处显示“字幕没有在控制台保留”；接入后超过三行的字幕先折叠，可以展开全文。

页面只用 DOM 构建内容，样式通过 CSSOM 设置，与现有 `script-src 'self'`、`style-src 'self'`
策略兼容；`tests/test_console_static.py` 检查这些约束。

`app.js` 是不需要编译的原生 JavaScript，文件开头的 `// @ts-check` 和 JSDoc 类型由 CI 用 `npm exec --yes --package=typescript@5.9.3 -- tsc -p tsconfig.console.json` 检查，不生成任何文件。`tests/test_console_browser.py` 在现有 browser-reader CI 中通过真实 HTTPS、主人会话和隔离数据库验证目标、反思、授权及认知页面；外部模型使用合成传输。

执行任务的列表带 `scope` 投影：只有保存的执行内容仍与 `scope_sha256` 一致、依据的私聊消息仍有效时，才显示工作区、路径、写入内容或任务说明和各项上限，文字再经过一次脱敏检查；执行端地址和凭据不出现。批准时提交同一个 `scope_sha256`，内容变化后原批准失效。

思考与决策可以用 `#thinking/<类型>:<编号>` 打开单条记录，不在第一页时按 `record_id` 单独读取。每种记录可以按 `next_cursor` 继续读更早的一页。首次同步或刷新中的任一读取失败沿用整页错误处理；翻页失败保留已加载记录，显示失败原因。刷新会丢弃旧翻页结果，隐私版本变化会重新读取，避免拼接旧来源。目标记录显示每一步的状态、尝试次数和读取回执，结果不明的步骤单独提示。

表达习惯学习没有单独保存逐日历史：页面显示的是按保留期内原话重新计算的当前结果和支持它的日期；出现相反反馈或重置交流偏好后从头累计。稳定偏好在相关能力开启、当前授权有效时可用作真实回复参考；快照的 `SHADOW` 不表示运行时禁止使用。

“情绪与偏好”页将“你的交流偏好”和“小肥鱼的心情”分开展示。后者提供即时 PAD、近期底色、
来源与衰减趋势，以及暂停、恢复、清除和重新计算操作；心绪手札继续使用日记与反思。
启用及来源规则见[动态心情说明](../qq-beta/dynamic-mood.md)。

## 设计稿中尚未实现的部分

下列功能需要新的后端机制，按仓库规则先单独讨论范围和取舍，再实现：

| 设计稿功能 | 缺少的机制 |
|---|---|
| 交给她决定：日记和自主活动调度以外的能力 | 第一版只开放日记暂停，以及自主活动调度的暂停、每日次数和间隔 |
| 记忆按人物、地点等实体连成关系图 | 记忆提取时增加实体与关系，并保证每条关系都有原话证据、随遗忘一起清除。现在的关系图只按主体、类型和共同来源消息连接 |
| 网站黑名单模式 | 读取程序只支持精确白名单；黑名单会让她接触名单外的任意公开网站，暂不做 |
| 语音回放与她的语音回复 | 语音任务结束即清除音频，回放需要独立的保留期与读取接口 |
| 表情素材编辑、上传、审核与隔离 | 控制台目前只读取理解任务，没有素材写入接口 |
| QQ 资料计划的允许与拒绝、头像预览 | 计划决定接口与审核过的素材读取；外部图片受 CSP 限制不直接加载 |
| 记忆原话展示 | 记忆列表只返回来源编号，需要有界的证据摘录接口 |

关系图与召回调试参考了 AstrBot 插件
[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
（[界面说明](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/webui)：
图谱与记忆双向定位、选中节点列出关联记忆）、
[Memora Connect](https://github.com/qa296/astrbot_plugin_memora_connect)
和 [AI Memory KG](https://github.com/Catfish872/astrbot_plugin_ai_memory_KG)。
