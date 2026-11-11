# 初始角色与历史对话导入

项目默认从空白角色开始。主人可通过本地操作员 CLI 明确设置初始角色，也可导入带来源的旧对话。
两个入口都只保存资料，不调用模型、不生成 QQ 入站或投递记录，不执行历史指令。
记忆、账号权限、费用、工具和发送状态继续由各自原生机制管理。

## 初始角色

角色 JSON 支持 `name`、`aliases`、`fictional_identity`、`traits`、`speech_style`、
`response_habits` 和最多四组 `examples`（每组 `user`、`assistant`）。
`speech_style` 可以明确保留括号动作、表情、神态和对白穿插；不会被“没有现实身体”的规则
一并移除。动作属于虚构角色表达，实际执行和完成状态仍要求原生回执。
资料有逐字段和整体大小限制；它不接受额外的系统提示、工具或权限字段。

```powershell
$roleHash = (Get-FileHash -LiteralPath .local/imports/example/character.json -Algorithm SHA256).Hash.ToLowerInvariant()
uv run python -m companion preview-character --request .local/imports/example/character.json --request-sha256 $roleHash
uv run python -m companion set-character --request .local/imports/example/character.json --request-sha256 $roleHash
uv run python -m companion clear-character
```

`preview-character` 仅检查并展示资料。`set-character` 使资料成为当前起点，可在真实相处中发展，
不会把指定性格伪装成已经自然形成的稳定特征。`clear-character` 关闭当前设置，保留仍有效的
历史资料。再次明确设置未被遗忘的角色可重新启用它；重放同一历史导入不会重启旧角色。

角色通过原生事件和一个只含事件 ID 的当前指针保存，重启后仍可读取。每次上下文组装、模型
提交和原生 owner 投递重新校验来源、当前角色和权限；中途换角色、清除或遗忘会拒绝旧结果。
当前实现面向聊天上下文，未把角色资料接入独立日记生成提示。

## 每个实例选择表达方式

原实例保持现有配置即可；默认仍为 `participation_v2` 自然交流，默认系统提示原文保持不变。
需要按角色资料表达的实例，在自己的 `config.toml` 中选择：

```toml
[conversation_policy]
mode = "ENABLED"
strategy = "character_v1"
```

环境变量也可设置 `COMPANION_CONVERSATION_POLICY='{"mode":"ENABLED","strategy":"character_v1"}'`。
控制台“对话策略”的策略选择中对应“角色表达”。配置沿用已有的版本、用途授权和重启生效规则，
不在一个运行任务中途切换。`OFF` 或 `SHADOW` 不启用该策略的额外表达指导。

`character_v1` 替换通用的熟人说话方式指导，允许按角色资料调整称呼、对白篇幅、动作神态、
情绪表现以及安慰、陪伴和建议的方式。名字、外观、性格和亲近程度仍来自独立设置的角色资料；
选择该策略本身不会生成或导入角色。导入资料也不会自动改写实例的表达策略。

两种表达方式共用真实身份说明、记忆与执行证据、隐私、权限、关系边界、预算和投递规则。
其他已有配置（如输出 Token 上限、分段回复、心情与表达学习）继续按各实例自己的值生效。
该选择只涉及聊天表达，独立日记等其他生成场景保持各自的既有提示和合同。

## 历史资料包

资料包包含 UTF-8 原始文件、可选的原始图片与人工核对的 JSON 请求。原始文件逐字节保存，
包括换行、分析、草稿和平台提示；请求描述分段、明确接受的记忆、图片关联和可选的初始角色。
不自动猜测无角色标记 TXT 的发言者，不把缺失的图片或日期补造成原始记录。

请求结构如下，完整字段约束见 `companion/history_import.py`：

```json
{
  "schema_version": 1,
  "source_label": "主人提供的网页复制记录",
  "source_sha256": "原始文件的64位小写SHA256",
  "segments": [
    {"role": "user", "kind": "dialogue", "start": 0, "end": 20}
  ],
  "memories": [
    {"subject": "relationship", "kind": "episode", "fact_key": "historical_agreement",
     "content": "主人核对后接受的历史陈述", "segment": 0, "quote": "原文中的连续引用"}
  ]
}
```

该片段展示结构，散列、偏移和引用须按实际原文填写，不能直接作为导入请求使用。
`start/end` 是 UTF-8 解码后 Python Unicode 字符索引，起点包含、终点不包含。
所有分段按顺序完整覆盖原文；不能重叠、留缝或超出原文。
角色为 `user/assistant/unknown`；类型为 `dialogue/analysis/prompt/notice/unknown`。
复制记录不带可靠原始日期时，原始日期明确为未知；事件发生时间只代表本次导入。

记忆必须引用已核对的对白原文。分析、草稿、原提示或未知发言者不能单独成为事实证据。
助手旧对白只能支持主人接受的关系经历陈述，不直接证明用户偏好、项目完成或自然自我特征。
记忆是本地操作员明确接受的陈述（`owner_attested`），不宣称是平台认证的历史消息。
来源包含原始散列、标注角色、类型、字符范围和逐字引用；模型上下文只提供必要的来源元数据。
如原文或角色含可识别凭据，导入拒绝，而不会为保留字节一致性存下凭据。

```powershell
$requestHash = (Get-FileHash -LiteralPath .local/imports/example/request.json -Algorithm SHA256).Hash.ToLowerInvariant()
uv run python -m companion preview-history --source .local/imports/example/source.txt --request .local/imports/example/request.json --request-sha256 $requestHash
uv run python -m companion import-history --source .local/imports/example/source.txt --request .local/imports/example/request.json --request-sha256 $requestHash
```

预览不写事件或记忆。正式导入在一个事务中提交归档、记忆和可选角色；任何部分失败全部回滚。
已有记忆沿用原 `Memory.add` 的同事实合并和冲突标注，不覆盖纠正结果，也不清空原记忆库。
同原文和同请求重复导入返回原回执；同原文换请求拒绝，避免静默改写已核对的来源解释。
原始文件和请求分别校验散列，不能在审阅后悄悄换掉原文。

有原图时，增加 `images` 数组；每项含 `sha256`、`mime`、人工核对的 `description` 与
对应的 `source_segments` 索引列表。图片按散列命名（PNG 为 `<sha256>.png`），在两个
导入命令中加 `--images-dir .local/imports/example/images`。最多 20 张，单张最多
4 MB、总计最多 16 MB；复用原生解码器检查 MIME、静态帧、尺寸和像素上限。
原图随同一归档进入数据库与备份，保留原字节，不自动调用视觉模型或发送到提供商。

## 分析文字与接续

用户提供的分析文字按 `analysis` 原样归档，可从原档导出查看。它不是本项目生成时的内部思考，
不回填为提供商的 `reasoning_content`，也不冒充助手正式发出的回复。
可将其中有用的回应习惯整理为 `response_habits`，例如怎样判断玩笑、认真提问或倾诉，
再配合真实的代表性对白延续角色语气。模型看到的是明确设置的角色参考，不是待执行的旧草稿。

旧经历进入原生长期记忆检索；当前角色每轮提供。没有将整份原档塞入每轮上下文，也没有
建立第二套事实库。后续真实聊天继续走原生对话历史、情景接续、预算和遗忘流程。

## 授权、保留与遗忘

CLI 读取当前部署配置，必须在受信任的本地操作员环境运行；它不接受远程消息作为导入请求。
真实 owner 模式仍要求当前原生授权：导入需要 `history`，含记忆时还需 `memory` 的
`owner_private` 范围，含角色时还需 `expression`。设置或关闭角色需要 `expression`。
旧的有界窗口不支持这个新入口，不会借本地导入扩大旧窗口批准的范围。
原档导出要求 `history` 当前有效；接受的记忆与初始角色分别按记忆和表达用途读取。

```powershell
uv run python -m companion export-history --archive-event-id evt_example --output .local/imports/example/exported.txt
uv run python -m companion forget-history --archive-event-id evt_example
```

导出文件使用独占创建，已存在时拒绝覆写。导入回执给出实际的 `archive_event_id` 和记忆 ID。
导出原图时，在 `export-history` 加 `--image-sha256 <原图散列>`，输出路径选择相应图片扩展名。
同一复制文件是一个不可拆分的原始来源：遗忘该来源，或遗忘由它支持的任一导入记忆，
会保守清除整批原档、关联记忆和由此设置的角色；其他独立来源的无关事实保留。
原生遗忘日志确保从旧备份恢复时重放清除，重新提交同一被遗忘原文也会拒绝。

数据沿用现有 `raw_event_retention_days` 和来源有效性检查，角色的来源到期也会停止使用；
这次没有增加永久保留的例外。需长期使用时，应先按部署合同核对当前保留期与读取授权。
独立保存的原始资料包、手动导出文件属于主人持有的文件，不由数据库内遗忘命令删除。
包含真实对话的资料包应放在 `.local/` 等已忽略目录，不提交到代码仓库。

## 同服务器的第二个 Bot

导入目标可以是全新的独立实例。第二个 Bot 使用自己的 QQ Bot 身份、配置、Bridge 凭据、
Core 数据库、Bridge spool、账号运行目录、遗忘日志和预算日志；不要复制第一个 Bot 的
运行库、日志或账号凭据作为初始化资料。导入只针对第二实例的新库。

部署沿用现有 Compose 机制，使用独立 project 名、私有配置与持久化卷。还需显式隔离
网络名称和宿主端口：当前 `compose.yaml` 与 `deploy/compose.onebot-direct.yaml` 中的
网络名称写死，单独换 `-p` 不足以隔离服务发现。部署时检查最终 `docker compose config`
的数据库卷、凭据路径、Bridge 目标、网络和端口均指向第二实例，再启动它。
本入口不启动、迁移或改写另一套实例。正式部署仍沿用现有[部署合同](deployment.md)。

## 依据与验证

对照 [DSH typed session events](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/session/README.md)
的事件与历史投影区分，以及项目[已有 DSH 持久化研究](qq-beta/next-memory.md)。复用 Core
现有事件、证据、记忆和遗忘日志；没有引入 DSH 的存储或执行器。
[DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
单独定义 `reasoning_content`，历史传递规则取决于调用形式。手工复制文本不具备原生调用状态，
因此这里只将分析保存为有标签的资料。

`tests/test_history_import.py` 覆盖原文字节、角色动作、幂等、证据、事务回滚、权限、
重启、保留期和旧备份遗忘；`tests/test_history_import_entrypoint.py` 通过真实 CLI、
签名 HTTP 入站与独立生产 Worker，并只替换外部模型 transport，验证角色与记忆进入请求、
分析没有冒充原生 reasoning、括号动作保留，以及换角色后旧投递被拦截。
该端到端场景分别验证自然交流和角色表达；`tests/test_instance_expression.py` 另验证
两个实例互不影响、关闭/影子模式与配置加载。配置校验和保存沿用原控制台测试。
