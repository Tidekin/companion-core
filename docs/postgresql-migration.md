# Core PostgreSQL 结构与离线导入

数据库归属与协调范围见 [ADR 0002](adr/0002-core-postgresql-and-local-sqlite.md)。
Core 源码的运行存储调用已使用 PostgreSQL，同时提供原生结构迁移与离线导入入口。
生产切换仍须完成本文的对账、恢复、交接和真实入口验收。

## 原生结构

PostgreSQL 16 及以上版本从 Alembic 基线 `pg0001_core` 升级到
`pg0002_relationship_standing`，按重要性降序、更新时间降序和记忆 ID 升序索引关系候选。业务结构对应原 SQLite
`0068_image_review_target` 的 103 张业务表。SQLite 的旧迁移历史仅用于读取原备份，
不参与 PostgreSQL 运行时查询，也不形成另一套 Core 主库。

裸结构保持业务表为空，供离线导入使用。原 SQLite 修订还初始化三条运行数据：语音
incarnation、授权恢复代次和 OFF 模式；导入保留这些原值。新的 PostgreSQL 安装需在
Core 运行入口完成相同的首次初始化，再开放服务。

- 所有整数采用 `bigint`，费用、授权修订和游标保留 64 位范围；二进制使用 `bytea`。
- JSON 凭证继续保存原始 `text`，原有 JSON 有效性及对象约束改用原生 `IS JSON`。
  不经 `jsonb` 重排键或空白，既有内容摘要仍可核对。
- 每张业务表增加显式 `record_sequence`，导入原 `rowid` 以保留顺序。`audit_id`
  使用原生 identity，并保留 SQLite 已删除高位记录留下的自增下界。
- 外键默认立即检查；离线导入可在单笔事务中临时延迟，解决相互引用和导入顺序。
- 目标步骤约束锁定父目标，防止并发插入突破三个步骤。父行的无值变化写入也让持有
  旧 repeatable-read 快照的写入者失败，避免按旧计数增加第四项；不自动重试整笔事务。
- 记忆变化、遗忘或置信度失效时清除派生向量。词面候选索引使用 PostgreSQL 自带
  `pg_trgm` 扩展的部分 GIN 索引，随状态变化自动维护；查询与召回质量仍须在调用迁移时验收。

结构迁移只串行化 schema/catalog 变更，使用与运行费用锁分离的 PostgreSQL 原生
事务锁命名空间。连接信息通过 `SecretStr` 交给连接器，不写进 Alembic URL 配置。
安装包包含相同的原生 SQL 与 Alembic 文件。

## 离线导入约束

`companion.legacy_import.import_sqlite_backup` 是停机迁移入口。发布协调者须先完成
原生 STOP，保留完整备份、独立费用和删除日志，再导入一个已建好结构的空目标库。
入口本身不会改变授权、启动 worker 或发送消息。

导入以只读 SQLite 快照校验源修订、完整性与外键。在 PostgreSQL 单笔事务中锁定空目标表，
通过原生 COPY 保留全部业务值、主键和顺序；不导入 SQLite 的 FTS 派生表或内部结构。
外键恢复立即检查后，逐行比较源与目标，核对空表、原始 JSON、字节、费用和状态。
全部一致才提交，返回仅含表名和数量的结果；已有目标数据、结构不符或对账失败均拒绝。
任何失败都不自动重试，不重发模型请求或投递，不读写独立费用、删除日志。

这一步只验证数据导入。运行调用迁移、原费用与删除凭证回放、备份恢复、真实入口与独立
worker 的合成验证及 QQ 验收，仍是生产切换的必要条件。

参考：

- [Psycopg COPY 与事务回滚](https://www.psycopg.org/psycopg3/docs/basic/copy.html)
- [PostgreSQL 外键检查时机](https://www.postgresql.org/docs/16/sql-set-constraints.html)
- [PostgreSQL JSON 文本语义](https://www.postgresql.org/docs/16/datatype-json.html)
- [PostgreSQL pg_trgm](https://www.postgresql.org/docs/16/pgtrgm.html)


## 运行迁移中的备份恢复（尚未完成生产验收）

运行入口使用原生 PostgreSQL。备份沿用 Fernet 加密，明文载荷改为原生
`pg_dump --format=custom`。独立费用和删除日志须另外保留。恢复入口从显式目标配置
读取连接信息。在整个恢复期间，源和目标应保持离线，停止 API、worker 和独立日志写入。

离线恢复账户需要目标库的连接及建对象权限、安装可信扩展 `pg_trgm` 所需的数据库
`CREATE` 权限，以及同集群的 `CREATEDB` 权限。日常 Core 账户无需 `CREATEDB`。
目标库必须没有用户表。恢复进程在目标库持有会话级咨询锁，同一目标上的并发恢复会
返回 `RESTORE_TARGET_BUSY`。它从 `template0` 创建随机命名的暂存数据库，仅授予
该库所有者连接权限。先在暂存库恢复表及约束、执行迁移、重放当前删除日志、撤销
旧授权、保留未知费用与投递状态并校验结构，然后才用一次 `pg_restore --single-transaction`
发布校验后的数据。重放或校验失败时，目标库保留原状，可修正原因后重试。

封存完成后，恢复流程从目标工作目录清除已删除的原图和附件。暂存库在发布前移除，
临时明文归档位于目标工作目录的私有临时目录中。若恢复进程被强制终止，操作员应核实
进程已经退出，确认残留的 `companion_restore_<32位十六进制标识>` 暂存库的所有者和
运行时间，再清理该次暂存库和临时目录。

客户端连接采用 libpq 原生 service/password 文件，临时文件位于目标工作目录且权限
为私有；凭据不进入命令参数。发布镜像仍需纳入匹配的 PostgreSQL 客户端工具并通过
恢复用例；本节不代表生产已经切换。

- [PostgreSQL pg_dump 一致快照及自定义归档](https://www.postgresql.org/docs/16/app-pgdump.html)
- [PostgreSQL CREATE DATABASE 与 template0](https://www.postgresql.org/docs/16/sql-createdatabase.html)
- [PostgreSQL 会话级咨询锁](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS)
- [PostgreSQL pg_restore 单笔事务恢复](https://www.postgresql.org/docs/16/app-pgrestore.html)
- [libpq 原生连接服务文件](https://www.postgresql.org/docs/16/libpq-pgservice.html)
- [libpq 密码文件及权限](https://www.postgresql.org/docs/16/libpq-pgpass.html)

默认 `public` schema 的恢复使用 `pg_restore --list/--use-list`，仅跳过目标已有的
`CREATE SCHEMA public`，保留归档的全部表、数据、索引和约束。十八项记忆生命周期
与恢复后删除重放已经在隔离 PostgreSQL 上验证；完整 Core 业务与生产切换仍待验收。

## 隔离验证与离线评估

测试和离线 R1/P8-B 准备脚本读取 `COMPANION_POSTGRES_TEST_CONFIG` 指定的私有 JSON 文件，
字段为 `host`、`port`、`dbname`、`user`、`password`。它必须指向专用临时集群，账户能创建
和删除其自有测试数据库；不得使用生产连接或生产数据目录。备份用例还需要 PATH 中可用的
匹配版本 `pg_dump`、`pg_restore`。测试源、配置、独立日志和临时材料均留在本工作区。

[原生夹具](../tests/postgres_fixture.py) 为每个 pytest worker 迁移一次模板，并为各用例
复制独立数据库，退出时删除。R1/P8-B 脚本复用同一原生隔离入口，不调用模型供应商；
准备得到的数据库不作为输出交付，冻结的历史结果不覆盖。CI 通过既有
`scripts/ci_postgres.py` 在临时 runner 的回环地址启动服务，并在全部依赖该服务的检查后停止。

后台唤醒改用独立原生 LISTEN 连接，事务提交时发布空 NOTIFY，回滚不发布。
通知只提示重新扫描；定时边界也会实际返回扫描，不依赖通知完整性。等待在线程中
有限执行，取消时先等待该调用退出，再释放监听连接。

- [Psycopg 原生通知与有限等待](https://www.psycopg.org/psycopg3/docs/advanced/async.html#asynchronous-notifications)

运行诊断和私聊 trace 查询使用独立、有限连接及 PostgreSQL 只读可重复快照，
每条语句和锁等待均有超时。查询不会初始化身份、升级结构或重放删除。
费用与删除文件的核对沿用事务级共享账本锁。

诊断中的 `integrity=CATALOG_VALID` 表示目录内约束均已验证、索引均有效；
完整的堆表与索引物理检查须在独立维护或恢复演练中使用 PostgreSQL 原生工具，
验收时保留其检查范围和结果。

- [PostgreSQL amcheck 检查范围与锁行为](https://www.postgresql.org/docs/16/amcheck.html)
- [PostgreSQL pg_amcheck](https://www.postgresql.org/docs/16/app-pgamcheck.html)

运行中的容器只加载不可变的依赖文件；更新测试环境不得截断或覆盖其他进程
正在映射的原生库。长时间评测独立冻结依赖目录、文件哈希、Python、驱动与
等待实现。业务容器和 CI 沿用 Psycopg 的平台默认等待实现。

- [Psycopg 3.3.8 等待实现与环境选择](https://github.com/psycopg/psycopg/blob/3.3.8/psycopg/psycopg/waiting.py)

表情目录的离线导入直接使用 PostgreSQL 原生事务；`dry-run` 和 `reconcile`
在强制回滚事务内验证完整来源、删除重放与容量，返回物化结果后回滚。
`import` 将删除重放和导入一并提交。预检不会初始化身份或创建替代删除日志。
试运行会回滚表数据；PostgreSQL 序列允许留下空档，顺序值不作为连续计数或费用凭证。

情景接续和目标投影在比较来源快照时，仅比较原有业务字段；物理排序字段
`record_sequence` 不参与证据一致性判断。目标候选按原生聚合后的注意状态排序，
实际来源、授权、过期和未知执行状态仍须逐项核验。

记忆抽取沿用原有共享租约与检查点，以唯一键 upsert 锁住首次领取和已有租约，
在锁内重新检查所有者和到期状态。空任务不留下新租约；过期恢复仅处理该检查点
拥有的费用请求。模型请求在事务外执行。夜间 N2 创建情景使用原有独占证据写入
事务，保留较早情景与晚到消息的分组。

就绪接口和运行诊断共用版本与结构能力检查，并在有限只读快照内执行。
打包的旧版本必须迁移；未知的新版本只有具备全部所需能力时才允许兼容回退。
打包的 SQLite 版本在导入后仍属于已知旧版本。

对话的每次同步来源校验使用一个有限只读快照，并在下一次异步边界重新读取。
同一批次直接传递刚核验的当前授权视图；当前输入签名仍验证原修订的捕获密钥，
历史修订继续读取原始授权证据。用途、租约、来源和独立日志检查继续执行，
批次结果不跨模型或工具调用保存。

任务配置绑定与其原始快照通过同一条 SELECT 读取；模型请求校验同时读取任务绑定、
请求绑定及双方快照。每次调用重新读取并核验绑定 HMAC、原快照签名与启动身份，
保持缺失绑定的拒绝顺序和旧版配置恢复校验，关闭热更新也继续验证已有绑定。

首次私聊收件在既有所有者身份行上取得原生行锁，再检查幂等键和会话，
使并发的首次请求也能重放同一事件和任务。控制台登录在同一既有身份行上串行检查
失败次数与会话上限，包括尚未存在的计数行。两者均在原有限事务内提交或回滚，
健康检查继续使用独立读取。控制台清空心情使用原独占证据事务更新隐私版本。
`state_directory` 与数据库、独立账本路径一起由部署持有，不通过浏览器配置修改。

能力提示的各功能行在同一原生只读快照内计算，直接使用该快照刚核验的当前授权；
后续模型与工具边界继续各自重新校验。费用凭证的身份、摘要、数据库快照及事务写入计数
在同一条 SELECT 中读取，保持 READ COMMITTED 下的变化检测和只读快照内的复用边界。
启用原生私聊的主 Worker 启动时加载会话、输出类型模块，并通过 Pydantic 的
`TypeAdapter.rebuild()` 完成 SDK Conversation 及其消息序列化类型延迟执行的本地结构构建，
CLI 在创建工作循环前完成这些同步初始化，随后才开始领取任务。
相同的启动阶段预加载 SDK 自带的本地定价数据与已启用的只读梗词库，沿用各自原有
校验和内存复用。Core 的原费用批准、预留与独立账本继续决定实际调用和结算。
累计预算预检沿用 Store 的只读检查与权威写入重试；正常检查使用只读快照，
到期、回滚或暂停仍在原写入路径持久化。回复任务状态、事件双方版本与已消费来源的
撤回标记在同一条 SELECT 内核验，保持原来的取消、替代与来源撤回判定顺序。
一次完整账本校验中的模型控制、对账、暂停见证与累计预算指针通过原生前缀范围查询
一并读取。各验证器仍逐项比对原值与日志，待处理责任继续核对原费用行；读取结果仅
用于当次持有原账本锁的校验，不跨事务保留。
同一轮账本核验的控制元数据、Gateway 客户、Gateway 用量标记、授权修订与费用准入投影
由一条原生 SELECT 一并读取，再交给原验证器按原顺序核对。JSON 凭证与加密字段仍是
原始文本；授权修订按原版本顺序验证，费用、状态、测量凭证和 UNKNOWN 检查保持不变。
实际事务写入计数直接调用 PostgreSQL 统计视图使用的原生计数函数，保留同样的用户
模式与关系类型范围；不为仅需行变更数的检查重复连接所有索引并分组。触发器与 CTE
写入仍计入其中，并与系统视图逐项对照验证。
删除后的历史投递核对只接受有效且有限的删除时间。原始文本时间先经 PostgreSQL
`pg_input_is_valid` 检查，再在 `CASE` 内转换并检查 `isfinite`；损坏或无穷时间不能
作为覆盖投递的删除凭证，也不会使类型转换中断核对事务。

- [Pydantic TypeAdapter 延迟构建与显式 rebuild](https://docs.pydantic.dev/latest/concepts/type_adapter/#rebuilding-a-typeadapters-schema)
- [Pydantic AI 首次调用本地加载开销及原生预加载修复](https://github.com/pydantic/pydantic-ai/issues/7405)
- [PostgreSQL 事务统计视图的原生定义](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/backend/catalog/system_views.sql#L656-L693)
- [PostgreSQL 输入有效性检查](https://www.postgresql.org/docs/16/functions-info.html#FUNCTIONS-INFO-VALIDITY)
- [PostgreSQL 有限时间检查](https://www.postgresql.org/docs/16/functions-datetime.html)

## 有限检索读取的往返

有限检索仍由原生只读事务和每次读取的保存点保护。连接的只读标记与原超时值在一条
查询中读取；临时超时、带行数上限的 SELECT、超时恢复仍在同一保存点中按顺序执行。
取消或 SQL 错误回滚该保存点，调用方快照和超时保留。

回复校验在一条 SELECT 中读取事件撤回标记、消息类型和双方回复版本；来源读取也在
一条 SELECT 中取回原事件、签名回执和隐私版本。原有限来源图在读取正文时一并读取
有上限的邻接 ID，正文读取前仍先申请原字节预算。重复读取合并在各次原校验边界内，
不跨模型调用，也不保存权限或费用验证结果。

聊天校验进一步在原费用与删除日志锁下，用一条 SELECT 读取当前授权、原任务租约、
签名输入来源、配置绑定、回复修订和有序图片绑定。SQL 子查询复用各模块的原查询，
取回的原行仍交给原校验器检查签名、哈希链、生命周期、来源、配置 HMAC、租约和修订。
临时行对象只存在于一次同步 `check_conversation` 调用中；每次边界重新查询，同一写事务
内的后续修改也必须重新检查。该对象不保存授权结论，不跨 `await` 或多次边界复用。
历史来源及图片原始字节仍走原来源、凭证和有效期校验，未知费用不取代来源或结果证据。

复用既有 `to_jsonb` 行投影与原生事务，不新增存储或依赖；DSH 已核验的文档存储实现
仍不提供可替代 Core 业务校验的 PostgreSQL 事务接口，见 ADR 0002。
[PostgreSQL 的语句快照](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-READ-COMMITTED)
保证一次 SELECT 的已提交读取一致；[原生 JSON 行转换](https://www.postgresql.org/docs/16/functions-json.html)
保留复合行的字段名、空值和数值类型，转换后的行不构成执行许可。

旧金额契约退役复用离线 v2 续期请求及其原执行器退出证明、STOP、UNKNOWN 清单和日志。
请求显式增加 `monetary_retirement`，值为发生指纹变化的 meme/rem 原始配置散列输入。
原配置必须匹配原授权中的能力指纹；仅删除 meme 的 `meme_caption_daily_micro_usd`
或 rem 的 `daily_micro_usd`、`monthly_micro_usd` 后，必须恰好匹配当前配置的新指纹。
旧费用快照仍须先匹配原契约摘要，随后仅忽略已退役的八个金额字段：
`daily_micro_usd`、`local_monthly_micro_usd`、`monthly_micro_usd`、
`per_attempt_micro_usd`、`per_request_micro_usd`、`reflection_monthly_micro_usd`、
`scenario_daily_micro_usd`、`window_micro_usd`。调用、并发、token、价格、有效期、用途、
来源和隐私范围保持原值；旧窗口调用仍计入后继窗口。请求与原配置证明共同绑定到
原续期凭证的请求指纹，原费用、UNKNOWN、配置版本、签名和哈希链均不改写。
该显式操作不放宽普通续期，不允许重放旧输入或缺失原响应的模型请求。

滚动更新脚本使用 `--state-directory` 指向宿主机的 Core 状态目录，按 Worker 原生名称
核对锁文件。授权修订、UNKNOWN 数量及在途任务和投递在仍运行的 API 容器内通过原生
只读快照读取，复用该容器的部署连接配置。操作员命令参数不携带数据库凭据；首次
PostgreSQL 切换仍使用本文的 STOP、导入、恢复和交接流程。

`release_plan.py` 的 Core 预检显式使用 `--probe-config <隔离副本.toml>` 和
`--probe-network <内部网络>`。先按原生备份/恢复流程准备独立 PostgreSQL 副本及最新
账本、遗忘日志副本；配置中的数据库只指向该副本，文件路径对应预检复制后的挂载位置。
内部网络仅连接副本数据库与预检容器，不连接生产服务。脚本拒绝默认网络、外部网络
及包含生产 API 的网络，不继承生产容器的 `COMPANION_*` 环境覆盖值。目标镜像可迁移
隔离副本；原始 UNKNOWN 费用及其精确 request_id 的关闭见证也在副本内核对。

离线风格预览及语料导出通过原生 PostgreSQL 只读重复读快照读取；默认从
`COMPANION_CONFIG` 取得连接配置，显式参数也接受 PostgreSQL DSN。
