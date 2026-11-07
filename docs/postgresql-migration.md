# Core PostgreSQL 结构与离线导入

数据库归属与协调范围见 [ADR 0002](adr/0002-core-postgresql-and-local-sqlite.md)。
本阶段提供原生结构迁移与离线导入入口，Core 的运行存储调用尚未切换。

## 原生结构

PostgreSQL 16 及以上版本使用独立 Alembic 修订 `pg0001_core`，对应原 SQLite
`0068_image_review_target` 的 103 张业务表。SQLite 的旧迁移历史仅用于读取原备份，
不参与 PostgreSQL 运行时查询，也不形成另一套 Core 主库。

裸结构保持业务表为空，供离线导入使用。原 SQLite 修订还初始化三条运行数据：语音
incarnation、授权恢复代次和 OFF 模式；导入保留这些原值。新的 PostgreSQL 安装需在
Core 运行入口迁移时补齐相同的首次初始化，再开放服务。

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

运行入口正在改用原生 PostgreSQL。备份沿用 Fernet 加密，明文载荷改为原生
`pg_dump --format=custom`，独立费用、删除日志仍需另外保留。`pg_restore` 只向
没有用户表的目标数据库写入，并以单笔事务恢复表、数据、序列及约束；随后执行原有
删除回放、授权撤销、费用恢复核对及未知投递保护。目标在验证完成前须与 API、worker
保持断开。恢复入口读取显式目标配置文件，不沿用源进程的数据库环境覆盖值。

客户端连接采用 libpq 原生 service/password 文件，临时文件位于目标工作目录且权限
为私有；凭据不进入命令参数。发布镜像仍需纳入匹配的 PostgreSQL 客户端工具并通过
恢复用例；本节不代表生产已经切换。

- [PostgreSQL pg_dump 一致快照及自定义归档](https://www.postgresql.org/docs/16/app-pgdump.html)
- [PostgreSQL pg_restore 单笔事务恢复](https://www.postgresql.org/docs/16/app-pgrestore.html)
- [libpq 原生连接服务文件](https://www.postgresql.org/docs/16/libpq-pgservice.html)
- [libpq 密码文件及权限](https://www.postgresql.org/docs/16/libpq-pgpass.html)

默认 `public` schema 的恢复使用 `pg_restore --list/--use-list`，仅跳过目标已有的
`CREATE SCHEMA public`，保留归档的全部表、数据、索引和约束。十八项记忆生命周期
与恢复后删除重放已经在隔离 PostgreSQL 上验证；完整 Core 业务与生产切换仍待验收。

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
