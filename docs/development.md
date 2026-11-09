# 开发与验证

每轮先明确本次行为、验收场景、文件归属与已授权范围。独立模块可并行，共享配置、
数据库合同和公共接口由一名集成人负责。沿用当前有效授权完成实现、修复与验证；
需要超出范围的外部动作时，先准备具体可审材料，再请求缺少的授权。

## 一轮改动

1. 在独立 worktree 实现并运行相关测试：
   `./scripts/check.ps1 -TestFiles tests/test_api_image_lifespan.py`。脚本同时运行全仓 Ruff；不指定
   测试文件仍运行全量。跨模块改动先验证实际配置加载、API 生命周期与独立 Worker 的
   交接，外部供应商使用模拟传输。认证、预算、幂等、UNKNOWN 和遗忘恢复合同继续保留。
2. 只改文档时运行 `./scripts/check.ps1 -Docs`。它与 CI 共用隐私、引用和证据检查，
   不执行整套产品测试。隐私扫描先运行，失败时立即停止后续检查，避免校验错误输出原文。
   公开 Markdown/JSON 自动进入隐私扫描；证据 JSON 必须在
   `scripts/ci_policy.py` 登记对应检查，未知 JSON、Schema 和运行配置仍走完整 CI。
3. 审查与定向验证收敛后推送最终候选，代码 PR 按 [Windows 路径规则](ci.md#windows-path-selection)
   运行完整或明确标记的 `selected` CI；通用 Python 和未知路径保留 Windows 产品检查。已有各包验证时，
   组合验收重点覆盖它们的交接；没有新的变化、失败或环境疑点时，不再机械追加一轮
   相同全量检查。集成涉及迁移、跨进程状态或既有检查未覆盖的风险时补充相应验证。
4. 交付链接最终 `ci` 任务的 Validation receipt：检查配置、实际提交、Git tree、
   运行链接和任务结果。每次 CI 的选择/跳过原因、矩阵、实际结果保存为 `ci-validation-*`；
   仅完整跨平台验证另保存可复用的 `ci-full-*` artifact，
   保留 7 天。历史验收记录保持其历史身份；当前摘要引用回执，避免手工复制多份 SHA。
5. 合并按用户当前授权执行。同仓库 PR 最新完整 CI 与合并树完全一致时，主线按现有
   策略复用验证；缺失、过期、失败或树不同均重新完整检查。实际运行验收使用固定候选，
   QQ、付费调用、部署和恢复按已有明确范围执行，保留费用、发送和停止回执。

## 定位慢测与复用环境

保存本轮计时与 JUnit：

```powershell
./scripts/check.ps1 -TestFiles tests/test_api_image_lifespan.py -Durations 10 -JunitXml .local/test-results/targeted.xml
```

仅在新增变化或未解决问题需要时扩大测试范围，不把测试数量相加当成覆盖证明。

独立 worktree 可以传 `-Python <已有虚拟环境的解释器>`，使用当前 worktree 的源码，
避免重复安装相同锁文件依赖。解释器环境必须已经满足当前锁文件；锁文件变化时先同步。
脚本退出时恢复进程环境变量，只清理本轮拥有的临时目录，保留显式指定的 JUnit。

## 本地并行与影响选测

同步当前 dev 锁后，`check.ps1` 默认用 pytest-xdist 的两个 worker，按完整测试文件分配，
原有 `-TestFiles` 和 JUnit 参数继续可用。用 `-Workers 0` 禁用并行；`-Full` 明确运行全套，
不能同时传 `-TestFiles`。这些本地选项不更改 CI 的完整分片覆盖。

```powershell
./scripts/check.ps1 -TestFiles tests/test_m0.py,tests/test_console_proxy.py -Workers 2
./scripts/check.ps1 -Impact -TestFiles tests/test_expression.py -Workers 2
./scripts/check.ps1 -Full -Workers 0
```

`-Impact` 使用原生 pytest-testmon 的实际覆盖依赖，不按名称猜测业务依赖。首次运行或环境
失效时，在所选范围内完整执行并建立数据；已有数据时执行受变化影响及此前失败的测试。
不指定 `-TestFiles` 才是整个仓库范围；只建立部分文件的数据不能称为全仓验证。
`-Full -Impact` 可完整刷新依赖数据，`-Full` 单独使用则完全不依赖 testmon 选测。
精确 `file.py::test_name` 选择继续遵守 testmon 的原生禁止二次筛选行为。

数据保存在本 worktree 的 `.local/testmon`，使用 testmon 原生 environment 参数隔离。
Python/平台/worker 数、锁文件、共享配置、迁移、非 Python 数据、辅助脚本及已知子进程入口
变化会更换环境；新增/删除路径也使环境失效。新环境按所选范围完整运行。无法读取输入清单
或缓存损坏时，回退到该范围全部测试，保留损坏缓存用于诊断。同一缓存的两个控制进程由
FileLock 互斥，忙时退出非零；同一次运行的 workers 使用 testmon 自带的 xdist 协调。

覆盖跟踪不能证明任意外部服务、动态文件或任意子进程的完整依赖。跨 API/worker、外部资源
或未登记的动态输入变更仍应显式传相关端到端测试，或使用 `-Full`；本地影响结果不替代冻结
候选的 CI 证据。不要将缩减结果记录为完整运行。

数据库准备复用已有 session fixture，每个 pytest worker 迁移一次原生 PostgreSQL 模板，
关闭模板连接后，通过 `CREATE DATABASE ... TEMPLATE` 为每个测试复制独立数据库。
专用临时集群的配置与清理边界见[迁移说明](postgresql-migration.md#隔离验证与离线评估)。
每个 worker 使用自己的进程临时目录，子进程继承它，所有目录仍属于本轮 `.local/test-temp`。
真实端口测试继续绑定系统分配端口，锁和子进程测试照常执行；不通过删测或替换服务规避冲突。

父进程在启动 pytest workers 前准备一次现有固定 revision 的 BGE 文件快照，复用本地缓存，
不创建推理会话。确知本轮不涉及模型时可用 `-SkipModelPreparation`；测试自身仍可按原逻辑
访问资源。ONNX/fastembed 实例仍是每个使用模型的 worker 各一份，进程内缓存和真实推理保留，
没有共享推理服务。首次下载与缓存已就绪后的测试耗时需分别记录；worker 数增加可能提高
内存占用和启动成本，不能预设并行一定更快。

实现依据为 [xdist 文件分配](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)、
[跨 worker session fixture](https://pytest-xdist.readthedocs.io/en/stable/how-to.html#making-session-scoped-fixtures-execute-only-once)
和 [testmon 原生选测、环境及数据文件选项](https://www.testmon.org/)。锁定的插件版本及其依赖
均在 dev 组/`uv.lock`，不进入运行时依赖。

一次 Windows 本地对比使用相同 40 个 m0、Console 真实 HTTP、voice 子进程取消及 worker 锁用例，
缓存均已就绪：串行 pytest 79.63 秒，两 worker 69.23 秒，均通过（约减少 13%）。包含父进程
准备/启动的入口耗时分别为 84.13 和 72.86 秒。这是该有限范围的一次测量，不是全仓或 CI
提速承诺。两个独立离线进程的真实 BGE 512 维推理各初始化一个实例、进程内第二次取用命中
缓存；模型文件共享不等于跨进程共享推理实例。


## Outbox 属性测试

`tests/test_outbox_stateful.py` 使用 [Hypothesis 原生状态机](https://hypothesis.readthedocs.io/en/latest/stateful.html)
的 RuleBasedStateMachine、Bundle、rule 和 invariant，生成 owner/private 的重复输入、租约、
过期、旧令牌、发送结果和数据库重开序列。复用 Core、传输函数、FakeModel 与原生模板复制；
每个示例由 `core_factory` 获得独立数据库及隐私账本路径，已有 `core` 的 tmp_path 接口保留。
测试检查无重复计费、旧令牌不能改变投递、发送开始后不自动重发，以及原令牌迟到确认可以
结算 DELIVERY_UNCERTAIN。范围仅限本地普通私聊 Outbox，不执行外部提供方或 QQ 操作。

每次运行最多 25 个示例、每例 20 步，关闭墙钟 deadline，使用
[Hypothesis 示例数据库](https://hypothesis.readthedocs.io/en/latest/reference/api.html#hypothesis.settings.database)
`.local/hypothesis/outbox` 保存并重放收缩后的失败序列。该缓存属于测试反馈数据，
不应提交；失败时保留 Hypothesis 输出的最小调用序列。运行入口仍为
`./scripts/check.ps1 -TestFiles tests/test_outbox_stateful.py -SkipModelPreparation`。

## 分片与交接成本

CI 以四个完整文件分片覆盖每个选中平台；完整运行包含 Ubuntu 和 Windows。
分片只决定哪些文件在哪个作业运行，不删除用例。缩减运行不替代完整跨平台复用证据。
耗时权重与刷新命令见 [CI guide](ci.md#file-shards-and-timing-maintenance)。业务功能无需
修改验证策略；新增测试自动进入分片。涉及矩阵变化时，同时更新复用所需作业清单与对应
覆盖测试，不能让旧分片的成功回执替代新矩阵的完整验证。

并行任务各自保留定向检查结果；集成人只审查明确冻结的候选与已存在回执。相同候选、
环境和检查范围的成功结果直接复用。组合代码改变时先检查接缝，再对最终组合运行一次
完整 CI。没有候选变化时，交接、重开聊天和读取报告都不是重新运行测试的理由。
