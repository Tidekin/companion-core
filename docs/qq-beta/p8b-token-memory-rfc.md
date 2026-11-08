# P8-B B4：分级上下文、派生记忆图与表达参考 RFC

状态：`DRAFT / OFFLINE_SYNTHETIC / DESIGN_ONLY_NO_PRODUCTION_BENEFIT_PROVEN`。
审查基线 `a33d3cb77f348828b17e4bb48c621c53cfcdc017`。本 PR 仅含文档、合成夹具、
独立基准脚本和测试；未修改运行源码、生产检索、数据库 schema、模型、表达或功能开关。
本基准未使用 API、QQ、VPS、真实聊天、账号资料或新下载的模型/tokenizer。

## 1. 已有路径与需要测量的开销

| 组件 | 基线代码实际行为 | 本 RFC 的约束 |
| --- | --- | --- |
| [ModelClient.complete](../../companion/model.py) | 唯一模型 HTTP 适配；发送场景 `max_tokens`，解析 prompt/completion/cache usage；demo 为固定响应 | 保持无存储、授权、投递职责；模型回包 usage 与估算分栏 |
| [budget.reserve/settle](../../companion/budget.py) | I/O 前事务预留，未知结果保留 `UNKNOWN`；输入是 UTF-8 字节保守界，费用仍是配置估算 | 不把字节界叫真实 token；本 RFC 不替代 B1 的硬准入或供应商账单核对 |
| [Core.context](../../companion/core.py) | system + 可选目标/affect/至多 6 记忆及 5 traits + 近期历史 + 当前输入；默认近期上限 12；仅 `SENT` assistant 入历史 | 已经有限载入，不能描述成“当前把所有历史都塞入 prompt” |
| Core.context 的裁剪 | 保留 system、完整当前输入，超 soft bound 时从第 2 条开始裁剪；参考资料可能先于较新历史被删除 | 压力样本展示这一取舍，不据此直接修改生产优先级 |
| Core.recall_for_reply | memory 启用且搜索有候选时，用 `memory_review` 做至多 6 项相关性筛选，然后另做 chat | 一条聊天可能有 2 次模型调用；无候选不调用 review；本基准未统计 candidate relation review 的真实发生频率 |
| Core.prepare_memories / extract_once | 提取批次最多 20 个已成功 job 的 owner 事件，达到最小批量和空闲条件；候选可能再走 relation review，完全相同事实有本地捷径 | 不为每个消息新建提取或 LLM 路由；批量失败、重复和隐私检查沿用原事务 |
| [Memory.search](../../companion/memory.py) / [SemanticIndex](../../companion/retrieval.py) | owner/private、有非删证据且有效候选；SQLite FTS + 固定 BGE + reciprocal rank fusion；SQLite 存向量；当前为精确向量扫描 | 本轮夹具替代搜索，不声称测过真实 FTS/BGE 精度、编码 CPU 或规模性能 |
| [AffectEngine](../../companion/affect.py) | 明确沟通反馈、有来源、独立 TTL；不推断情绪或长期人格，默认关闭 | 不是 PAD，也不作为新 PAD 已完成证据 |
| [NightlyConsolidator](../../companion/consolidation.py) | 以已有 run/step/lease 串联 memory、diary、reflection，按来源集验证；不是新的记忆主库 | 沿用幂等检查点，不增设后台队列或重复日记系统 |
| [DiaryService](../../companion/diary.py) / [ReflectionService](../../companion/reflection.py) | 独立规则、来源与 lease/epoch 验证，统一 budget 预留；日记不自动发信，reflection 为待审假设 | 本次仅模拟这两种 payload，不声称跑过真实日记/夜间 worker |
| activity / [SocialPlanner](../../companion/social.py) | 已有 activity job，最终仍由 Core 的 context → reserve → complete → settle；独立主动性/quiet/budget 门禁 | 本次模拟一份 activity payload，不启用分享、不触发外发 |

以上是固定基线的代码审查，不是已部署版本或 P8-B B1 的完成声明。原门禁见
[P8 历史证据](p8-gates.json) 与 [模型预算合同](p8-model-budget.md)。

## 2. 指标定义与证据边界

按 `chat / memory_review / memory_extract / diary / reflection / activity` 逐项计：

- `calls` 是本次 harness 对 stub 的调用次数，不是供应商请求或真人使用频率。
- `actual_input_tokens / actual_output_tokens / prompt_cache_hit_tokens / actual_cost`
  均为 `null`。没有经过验证的 tokenizer 或供应商 usage，不补造实际 token。
- `synthetic_input_units / synthetic_output_units` 为 `ceil(Unicode 字符数 / 2) + 每条 8 + 总计 3`
  的确定性工作量代理，标记 `synthetic_estimate`。它不是 tokenizer，**也不是硬 token 上界**。
- `conservative_reservation_byte_units` 单独保存原 budget 字节界，避免与上述代理或实际 token 混合。
  stub 把代理数送入隔离账本仅为会计流测试；不把该 `SETTLED` 当真实 usage。
- `configured_real_rates / currency / actual_cost` 均 `null`。另列无币种的演示费率
  input=1、cache=0.25、output=2 单位/token-proxy；`illustrative_cost_units = input + 2*output`。
  该数字只能检查分解与舍入，不能换算成人民币、美元或实际节省百分比。
- 缓存命中假设为 0，真实 cache 为 `null`；`potential_stable_prefix_units` 仅是稳定前缀长度代理。
  预留不得依赖未来缓存命中，不能把前缀相同推断为供应商实际缓存。
- `UNKNOWN` 是显式注入的一份 chat usage 缺失。账本保留非零上界，次数和比例另列；
  不能因模型文本存在就判零费用或成功核账。
- `local_stub_latency_ms` 只计进程内 stub；`context_local_ms` 包含 context/recall 以及 review
  的 SQLite 预留/结算，不含建库、seed、chat 调用、模型计算或网络。分别保留 n/P50/P95；
  P50 为中位数，P95 为 nearest rank `ceil(.95*n)`。供应商延迟和手机 E2E 均 n=0、P50/P95=`null`。
- `context_cpu_ms` 是进程 CPU 时钟，Windows 粒度较粗，0 不代表无 CPU；
  `python_tracemalloc_peak_bytes` 不含所有 native/ONNX/SQLite 内存，**不等于 RSS**，RSS=`null`。

主基准先执行 baseline 再 experiment，没有随机化/预热分层，样本很小。
本地延迟差异不能用于宣布生产加速，后台单场景 n=1 仅验证字段齐备。

## 3. 可复现 A/B 与失败样本

[数据集](../../tests/fixtures/p8b_context_cases.json)、
[脚本](../../scripts/benchmark_p8b_context.py)、
[原始明细与聚合](p8b-context-benchmark.json)、
[确定性回归](../../tests/test_p8b_context_benchmark.py)。数据全部公开合成，无真实人或账号对应关系。
原始 JSON 保存基线 SHA、实际被读源文件及脚本 SHA256、Python 版本和 canonical 数据集 SHA256。
后续源码发生变化时必须重跑并比较文件哈希，不能沿用此结果宣布新版本通过。

baseline 调用未修改的 `Core.recall_for_reply` 和 `Core.context`；隔离 DB 使用真实迁移和
`budget.reserve/settle`。用 fixture candidate IDs 替换 `Memory.search`，review stub 按显式 fixture
答案选择 ref，因此这是**上下文保持/调用分解实验**，不是独立语义评测。两边用相同 fixture
过滤 owner、epoch、证据、已遗忘/失效/模拟经历；该过滤仅为设计模型，不证明生产 G1 行为。
真实 G1 遗忘与恢复应由既有专用测试及未来单独获批的运行验收证明。

experiment 仅在独立脚本里跑小规模本地规则与一次有界参考装配，没有新增生产路由调用。
两种布局都保留完整当前输入、原 PRINCIPLES 和 `SENT` 历史约束。

| 分组 | 合成样本 | 需要观察的结果 |
| --- | --- | --- |
| 普通闲聊 | 相关词产生候选但不需要记忆 | baseline 仍 review，试验 L0 跳过 |
| 明确回忆 | 直接“还记得”及间接“饮料还是老样子吗” | 直接回忆保留；间接请求暴露本地规则漏检 |
| 事实纠正 | old SUPERSEDED + owner 明确新值 | 只保留有证据的纠正后值 |
| 跨会话 | 新 session 回忆同一项目 | 保持项目来源，歧义交给 L2 |
| 上下文压力 | 长近期历史与相关记忆竞争 soft bound | baseline 参考被裁掉；实验先裁旧 turn |
| 冲突 | 两条 needs_review 的相反偏好 | 两边一起保留，不能随意定论 |
| 隐私遗忘 | FORGOTTEN 与旧 epoch 节点 | 不进入候选或上下文 |
| 来源撤销 | 停止后续读取、保留历史；另有 forgotten | 有效历史保留，forgotten 拒绝 |
| 误触发 | 引用中出现回忆词和指令 | 不升权，实验不为引用开 review |
| 假经历 | 明确创作的“月球散步” | 不作为真实自传经历 |
| 无相关记忆 | 空候选 + 注入 usage 缺失 | 不虚构来源；UNKNOWN 额度保留 |

12 个逻辑样本、11 组，每组至少 1 个。每个样本仅执行一次，无总体分布或显著性主张。
后台四场景各一份相同 payload，以隔离 chat 布局差异，不宣称该模拟代表实际后台批次。

冻结结果和实际命令记录见 [本轮基准读数](p8b-context-benchmark-results.md)。
原始失败必须保留：baseline 压力样本缺失 `tea`；experiment 间接回忆缺失 `tea`。
合计保持率相同掩盖了不同样本的退化，不能只看总分。实验**未达到逐项不退化标准**，不实施。

### 复现入口

在现有锁定依赖环境运行；这组脚本无需 FastEmbed 模型、供应商密钥或供应商网络。
原生 PostgreSQL 版本使用[隔离测试集群配置](../postgresql-migration.md)
`COMPANION_POSTGRES_TEST_CONFIG`，每个 case 从空白原生模板复制独立数据库并在结束时删除。
新的运行结果保存在新的输出目录；冻结读数继续保留其原始源码和环境标识。

```powershell
python scripts/benchmark_p8b_context.py --help
python scripts/benchmark_p8b_context.py --output .local/p8b-benchmark/result.json --work-dir .local/p8b-benchmark/new-run-1
New-Item -ItemType Directory -Force .local/test-temp | Out-Null
python -m pytest tests/test_p8b_context_benchmark.py -q --basetemp=.local/test-temp/p8b-benchmark-new-1 -o cache_dir=.local/pytest-cache/p8b-benchmark
python -m ruff check scripts/benchmark_p8b_context.py tests/test_p8b_context_benchmark.py --cache-dir=.local/ruff-cache/p8b-benchmark
```

`work-dir` 必须不存在且在本工作区；pytest 也用新的 basetemp。不得把真实 DB 或受保护诊断目录
代入。新建的可再生合成材料可按仓库清理规则逐个核对后移除，不能扩展到既有目录。
数据与结构读数可重现；壁钟/CPU/内存采样允许变化。

## 4. 后续四级加载合同（全部未启用）

数据流为 owner/private 校验 → 当前隐私/来源状态 → 本地分类和候选过滤 → 有界上下文 →
现有 B1 准入/持久预留 → ModelClient → 同 epoch/lease 的结果校验 → 原 Outbox。
分类只改变加载量，不授予权限。读取状态和最新 Ledger 无法核实时不读取派生内容；
需要历史而无法证明时保留“不知道/当前无可靠记录”，不能把 L0 当作确认没有历史。

| 层 | 触发/加载 | 调用与上限（拟议实验预算，非已实现设置） |
| --- | --- | --- |
| L0 | 普通寒暄/当前问题不需历史；稳定规则、当前输入、近期已发送对话 | 1 chat，0 额外 LLM router；近期最多 6 turn，先满足硬输入门禁；不用无关 traits/目标/日记/PAD |
| L1 | 明确有界事实回忆，owner 纠正和时间定位先处理；本地 FTS+BGE | 候选最多 6、最终参考最多 6、派生邻居最多 2；无 review 只限可证明无歧义、无冲突的情况 |
| L2 | 歧义、间接指代、跨时段或冲突且本地不能安全处理 | 最多 1 次 relevance review + 1 chat；不把 review 输出升为事实；预算拒绝时说明暂不可核对 |
| L3 | 现有提取与夜间 run 达到批量、空闲和证据条件 | 沿用最多 20 event 的提取批次、daily scenario cap、lease/checkpoint；diary/reflection 继续各自开关与来源校验 |

本次间接回忆反例说明 L0 的安全判定尚未成熟。后续可先让不确定的本地路由保守进入 L2，
再测成本；不能强行全部降为 L0 来达到节省目标。未开 memory 的正常聊天仍走现有 demo/关闭合同。

每场景输入硬 token/费用上限来自 B1 已验证的准入配置，具体真实额度现在 `null/BLOCKED`。
上述 6 turn/6 候选不是 tokenizer 或收费额度；静态规则+当前输入若超硬界也必须拒绝或请求用户缩短，
不得静默截断主人当前话语或安全规则。输出继续走各场景 `max_tokens`，额外 review 同样算次数与费用。

## 5. SQLite 派生图合同

SQLite 是唯一事实源，复用 `memories / episodes / traits / memory_vectors / ErasureLedger`。
`node_tag / evidence_link / relation_edge` 仅是待评审概念，不在此冻结表名或迁移。
标签、边、摘要均为可重建索引，不作为新事实、长期人格、授权或新的记忆主库。

- 节点定位既有 memory/episode；保留 subject、状态、事实键、证据 ID、内容版本/哈希、
  source-grant 绑定版本、privacy epoch 与派生构建版本。边同时记录两端与精确证据依赖。
- 标签 Unicode 规范化、大小写折叠、长度/数量限额、去重；常见“喜欢/今天/东西”等低区分度
  标签降权。按文档频率加权只能贡献检索相关性，不能据同标签断言因果或偏好变化。
- 初始种子≤6、只允许 1 跳、邻居≤2、总节点≤8、最终 prompt 参考≤6；按证据相关性与
  稳定 ID 排序，去重后截断。时间邻近是检索线索；只有逐条证据支持的关系才标为事实关系。
- graph 扩展前和输出前都重新过滤 identity/private、当前源授权语义、epoch、状态、
  证据仍存在与内容版本；节点过滤后不得借相邻节点复活同一删掉事实。
- 脚本内原型只验证 shared-evidence 的 1 跳和上界，不建图表、不接生产检索；
  同标签、无证据、FORGOTTEN、旧 epoch 的邻居不能靠边混入。真实 FTS/向量 A/B 待独立后续实验。

### 遗忘、纠正、来源权限和可逆性

显式 forget 使用既有 fsynced Ledger 先记录完整源/派生闭包，再清除 memory/vector/
观察/日记/反思/所有候选图索引/缓存；依赖节点、owner corrected successor 和其边也在闭包内。
构建读到旧快照时不得在 epoch 改变后发布索引或 prompt；后台 lease 同样 fencing。
旧备份必须先重放最新独立 Ledger，再重建派生索引；source grant 的旧 COMPLETE 歧义仍保持既有
fail-closed 规则，不能为“恢复图”绕过人工归属审查。

自然过期或仅撤销未来读取：停止新 GET，已有合法历史仍可检索；不会仅凭 grant 非 active
就偷偷清历史。明确遗忘：删相关历史与所有派生。撤销若同时声明历史不可再处理，按更严格授权
独立冻结；不要把两种操作混成一个 bool。缓存键必须体现操作语义与版本，不能沿用旧查询结果。

后续 schema 仅可增加可重建派生索引，不修改事实表语义或 Ledger 权威。迁移前使用隔离副本和
最新 Ledger 验证；实验关闭时不读这些表，可清其可再生内容而不删源数据。新 Ledger 格式若涉及
升级，仍须兼容二进制恢复环境，不能直接 downgrade 在线 DB；本轮没有任何迁移。

## 6. PAD、表达与缓存

现有明确交互偏好继续归 AffectEngine。新的 PAD 设计独立：仅可从真实已验证事件形成有界表达
参考，每维候选范围 `[-1,1]`、单事件变化≤0.1、带来源/版本/TTL（拟议≤180 分钟），缺证据归中性。
它不是用户心理诊断或 AI 真实情绪，也不固定傲娇/高冷/好感度。创作/梦境必须显式标“创作/模拟”，
不进入真实经历表或 diary observation。

PAD 只可在已有严重语境/主人表达偏好规则之后影响词句长短或候选排序的有界平分项；
不能降低核对能力、偏置事实冲突、报复翻旧账、侮辱、增加权限/调用/分享、压过 owner 最新纠正。
任何隐私/严重语境/主人暂停表达指令都使相关表达变化禁用；不为计算 PAD 每条新增模型调用。
来源删除或同一事件修订必须撤销对应表达状态，而非给未来 TTL 重新续命。

稳定 system 规则可保持字节一致的前缀；动态 owner 输入、候选记忆、目标/affect/PAD、source 引文放
明确隔离的数据段，保持合法角色与用户最新输入优先级。不能把外部引文升为 system 来换缓存，
也不能在“缓存友好”布局中丢失 source 标签、needs_review、unknown delivery 或安全规则。
provider cache hit 只在回包有有效 usage 时记实测；共享账户/跨主体缓存隔离与供应商留存条款未确认
前不做隐私缓存承诺。应用级缓存有界（拟议≤128 项、≤5 MiB、≤60 秒），key 至少含 owner身份、
查询哈希、隐私 epoch、grant版本/处理语义、memory内容版本、embedding版本、索引构建版本；
纠正/遗忘/权限变化立刻失效，版本不明即 miss。缓存 hit 仍不得绕过最终权限检查或预算预留。

## 7. 后续实验资源预算与退出标准

这些是待验证的本地实验接受预算，不是当前主机观测或已实现配置：

| 项目 | 拟议预算/证据 |
| --- | --- |
| 本地路由 + FTS + 1 跳增量 | CPU P95≤25 ms、wall P95≤50 ms；n≥200/组，冷/热与压力分别报告 |
| embedding | 保留固定 `BAAI/bge-small-zh-v1.5` revision 和当前 CPU 2 threads；不下载新模型；现有模型不可用则该臂 BLOCKED |
| embedding wall | warm P95≤200 ms/query 的实验目标；cold load 单列，不用 stub 延迟填入 |
| 派生索引与缓存 RAM | 额外 RSS≤32 MiB；缓存≤5 MiB；native/ONNX 与 Python allocation 分开，模型基线 RSS 单列 |
| 图索引大小/构建 | 派生数据≤事实数据2倍或50 MiB的较小值；增量批次≤100节点；不持跨模型 I/O 的 DB 锁 |
| prompt | 候选/邻居/turn 有界；总量服从 B1 已验证硬上界和真实授权；真实配置未齐为 BLOCKED |
| 服务商/费用 | 无真实 provider、tokenizer、费率、审批或供应商账户上限，不进入付费试验 |

后续在 100/1,000/10,000 条公开合成记忆、固定 held-out query 集上分别跑纯 FTS、FTS+BGE、
FTS+BGE+1 hop，报告 recall@6、precision、冲突双方保留、owner纠正优先、零遗忘泄漏、零假经历、
无记录“不知道”与误触发率；标签分布/内容长度/跨会话/隐私操作序列相同，独立标注不提供给 router。
实际 provider/tokenizer 未批准时仍只报告合成 proxy，不能以增加假缓存来制造成本改善。

逐项退出：出现新隐私泄漏/旧备份复活/权限升权/UNKNOWN 当已送达立即停止；任一重要回忆分组
比基线差、增加每条 LLM 路由、CPU/RAM/延迟越界、测不到成本减少，则关闭实验并保留失败样本。
本次间接回忆已经触发“不实施”条件。后续代码原型应另开 PR，经独立审查和同 SHA 验证；
本轮 B4 的提交不得成为生产启用条件。

拟议禁用开关为独立 `context_tiering / derived_graph / pad_expression`（名字待评审、默认 false），
不是当前已存在的 Settings。关闭后沿用原上下文/Memory/Affect 路径，停止新增实验构建而保留
原始证据和 Ledger；不重置 budget/Spool/Outbox、不重试 UNKNOWN、不触碰旧服务。
所有真实模型、QQ、M1、Reader、Diary、Wake、部署与恢复审批继续独立。
