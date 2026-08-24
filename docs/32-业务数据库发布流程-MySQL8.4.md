# 业务数据库发布流程（MySQL 8.4）

## 范围

业务持久化数据库固定为 **MySQL 8.4**，使用 `mysql+aiomysql` 连接。它保存用户、题目、提交、学习状态、聊天和教学审计数据。

判题层仍可连接 MySQL、PostgreSQL、SQL Server 和 Oracle 等多种方言执行器。判题执行器的 schema、临时数据库和账号不属于业务数据库 Alembic 迁移。

SQLite 只允许用于本地测试，不得承载预发布或生产数据。

## 发布前配置

```env
DB_URL=mysql+aiomysql://user:password@host:3306/sql_edu?charset=utf8mb4
BUSINESS_DB_DIALECT=mysql
BUSINESS_DB_VERSION=8.4
BUSINESS_DB_CHARSET=utf8mb4
DB_ECHO=false
```

目标 MySQL 实例必须确认：

```sql
SELECT VERSION();
SHOW VARIABLES LIKE 'character_set_server';
SHOW VARIABLES LIKE 'collation_server';
```

## 迁移门禁

在临时 MySQL 8.4 数据库执行，不连接生产库：

```bash
alembic heads
alembic current
alembic upgrade head
alembic current
```

检查表、列、索引、外键以及 `alembic_version` 后，使用另一份临时数据库验证：

```bash
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

生产环境不使用 `downgrade base` 做回滚。生产回滚使用经过验证的备份恢复，或发布向前修复迁移。

## 备份恢复门禁

```bash
mysqldump --single-transaction --routines --events sql_edu > sql_edu.sql
mysql sql_edu_restore < sql_edu.sql
alembic current
```

恢复后必须核对用户、题目、提交记录数量以及迁移版本。备份文件不得提交到 Git。

## 发布记录

每次发布记录以下信息：

- MySQL 实际版本和字符集；
- 应用 Git SHA；
- `alembic heads` 和升级前后的 `alembic current`；
- 备份文件标识及恢复验证结果；
- 迁移失败时采用的恢复或前向修复方案。
