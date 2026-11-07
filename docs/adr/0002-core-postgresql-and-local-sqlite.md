---
status: accepted
---

# Core PostgreSQL 与节点本地 SQLite

主人已确认数据库分工：Core 的业务主库迁到 PostgreSQL，Bridge 和语音节点保留各自的本地
SQLite 队列。多个后台进程持续写入同一主库已经造成锁等待；按职责使用两种数据库，保留节点
断网时的本地执行与投递记录。数据库分工及以下原生锁与通知协调范围均已由主人确认。

## 数据归属与接口

PostgreSQL 保存 Core 的聊天、记忆及检索数据、任务、来源授权、费用投影、Outbox 和
删除回放进度。Bridge 与语音节点仅保存原有本地队列、执行记录和独立确认凭证，通过现有
HTTP 接口交接；各自的认证、幂等键、未知结果和顺序确认语义继续有效。

既有独立费用日志和删除日志继续作为必须核对的凭证，保留单调费用下界和备份恢复后的删除
回放。数据库迁移不把这些凭证降为缓存，也不重放既有模型请求或投递。Core 采用单一
PostgreSQL 实现，旧 SQLite 主库由只读迁移入口导入并封存，不形成永久双写主库。

采用 PostgreSQL 16 及以上版本、Psycopg 3 的原生连接与事务，沿用 Alembic 管理结构迁移。HTTP 复用
已有 `ApiStore` 的有限并发、独立连接和取消后等待完成约定。数据库连接信息与本地运行目录
分别配置，独立日志、媒体暂存和节点状态文件继续使用本地目录。

## 协调实现范围

当前代码依赖 SQLite 的 `BEGIN IMMEDIATE` 全库写锁、文件变化唤醒，以及 FTS5、`rowid`
和文件身份驱动的校验缓存。直接改连接地址不能保留这些行为。

- 任务领取采用 PostgreSQL 原生行锁和 `FOR UPDATE SKIP LOCKED`。同一任务、授权修订、
  费用预留与投递的互斥关系逐项验证；不在已经产生独立日志或外部效果后自动重试整笔事务。
- 独立费用日志的追加和对应数据库投影使用 PostgreSQL 事务级 advisory lock 协调；
  普通心跳与其他无关写入不共用全库串行锁。锁范围、获取顺序和故障路径必须由并发及故障测试验证。
- 后台唤醒使用 PostgreSQL `LISTEN/NOTIFY` 作为提示。任务的权威状态仍在原任务表，
  定时复查保留；通知丢失不能丢任务，通知正文不承载聊天或凭据。
- 文件身份相关校验改用 PostgreSQL 事务快照与实际凭证核对。缓存失效不能削弱对费用、
  授权、投递及删除投影篡改的拒绝；这一部分的具体策略须由并发和故障测试支持。
- 中文检索迁到 PostgreSQL 原生索引方案前，保留原召回探针与结果对照。索引是派生数据，
  可重建，不能替代来源和删除凭证。

备选是保留所有 Core 事务共用一个全局锁，或引入同步双写和跨库事务。前者会保留主要写入
排队，后者给本地离线节点增加不必要的事务依赖；因此提议按业务互斥范围使用原生锁，继续
通过已有 HTTP 与确认协议连接节点。

## 验证与交付

先完成 PostgreSQL 真实服务上的结构、查询、并发领取、费用上界、未知结果、删除和恢复
测试，再对原数据副本执行导入及逐表/凭证核对。上线沿用原生 STOP、停机备份、更新与
交接、恢复流程；真实 QQ 换图、分段、人物归属和耗时分别验收。保留失败请求和原费用。
独立切片按依赖交付，同时活跃 PR 不超过三个。

参考：

- [SQLite WAL 并发](https://www.sqlite.org/wal.html)
- [SQLite 本地及边缘用途](https://www.sqlite.org/whentouse.html)
- [PostgreSQL 事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html)
- [PostgreSQL 原生锁](https://www.postgresql.org/docs/16/explicit-locking.html)
- [PostgreSQL LISTEN/NOTIFY](https://www.postgresql.org/docs/16/sql-notify.html)
- [Psycopg 事务](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
- [DSH 已核验存储分工](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/storage/README.md)：
  该版本提供 JSON 与 SQLite 文档存储，没有可直接复用的 PostgreSQL 业务事务实现。

- [PostgreSQL pg_trgm](https://www.postgresql.org/docs/16/pgtrgm.html)：检验中文三字片段召回与排序，不能直接视为 FTS5 等价替换。
- [GitHub 官方 runner 软件清单](https://github.com/actions/runner-images/tree/main/images)：CI 使用预装 PostgreSQL 二进制，在工作区创建独立测试实例；缺失服务时必须失败。
