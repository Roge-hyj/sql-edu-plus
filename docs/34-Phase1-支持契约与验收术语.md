# Phase 1 支持契约与验收术语

版本：v1（2026-08-24）  
状态：优化前的正式范围声明，后续冻结验收必须遵守本契约。

## 1. 这份文档解决什么问题

Phase 1 的任务不是声称“任意 SQL 都能证明等价”，而是对一个有明确输入、方言、数据库版本、结构特性和执行器条件的 SQL family，给出可复现的有限证据。

这份契约先固定范围，再运行测试。测试失败时只能记录为 mismatch、generation gap、UNDECIDED 或 ENGINE_GAP，并按本契约的变更流程处理；不能为了让报告通过而临时把失败样本改成 out-of-scope。

本契约约束的是：

- 哪些 SQL 可以进入 Phase 1 的声明支持范围；
- 对一对 SQL 和一个 schema，判题器的结果各代表什么；
- 哪些结果进入正确性分母，哪些结果只作为风险指标；
- 业务数据库与判题数据库分别承担什么职责；
- hidden freeze 之后哪些动作被禁止。

本契约不是 SQL 标准的替代品，也不是对所有关系数据库实例的形式化等价证明。

## 2. 先区分两类数据库

系统有两套完全不同的数据库，不能把其中一套的版本写成另一套的版本。

| 用途 | 固定环境 | 作用 |
|---|---|---|
| 业务数据库 | 宿主机或业务环境 MySQL **8.0.46** | 保存用户、题目、提交、学习状态、审计和其他应用数据；SQLAlchemy/Alembic 的唯一业务数据库方言是 MySQL。 |
| 判题执行器 | Docker 中按方言启动的隔离引擎：MySQL `8.4.6`、PostgreSQL `16.10`、SQL Server `2022-CU20-ubuntu-22.04`、Oracle Free `23.7`（Oracle 23ai 系列） | 为 Gold Oracle、witness 和原生方言验证提供临时数据世界；不保存业务数据。 |
| 通用 bounded 执行器 | 运行 Phase 1 脚本的 Python `sqlite3`/SQLite 运行时 | 对 generic/standard/SQLite 兼容样本做有界的本地执行比较；具体 SQLite 版本写入每次冻结报告。 |

业务库固定为 8.0.46 不妨碍判题器支持多个方言。判题器连接哪个 Docker 引擎由题目声明的 dialect 和 `PARSEVAL_*_URL` 决定；没有可达的对应原生引擎时必须返回 `ENGINE_GAP`，不得悄悄改用 SQLite 并称为原生验证。

## 3. 四层能力模型

“支持某个方言”不是一个二元开关。一次 SQL pair 会依次经过四层，越往后要求越严格：

### 3.1 方言识别

能判断输入是 generic/standard、SQLite、MySQL、PostgreSQL、T-SQL 或 Oracle 风格。例如反引号、PostgreSQL 的 `::`、T-SQL 的 `TOP` 和方括号标识符，都可能只是识别线索。

识别成功只说明“看起来属于哪个方言”，不代表已经能安全执行。

### 3.2 方言解析

解析器能把 SQL 读成 AST，并保留方言相关信息。解析失败属于输入或 parser 边界，不能被当作 SQL 不等价。

### 3.3 结构 IR 与 ASTDiff

AST 被转换为项目的 `SQLStructureIR`，再比较标准答案和学生答案的结构差异（ASTDiff）。这一层可以说明“WHERE 运算符变了”“JOIN 条件缺失”，但结构差异本身不是数据语义差异的证明。

### 3.4 witness/原生执行验证

系统根据 schema 和差异生成有限的 witness 数据库，在同一数据世界分别执行两条 SQL，再比较列、行、重复值和顺序。需要 vendor 原生语义的样本，还必须有对应且可达的 Docker/native runner。

只有这一层的前提全部满足，样本才有资格进入声明支持范围。有限世界中观察到相同结果也不能推出任意数据库上的全局等价。

## 4. 当前方言契约

### 4.1 Generic / Standard SQL

这是教学 SQL 的主路径。允许使用本契约第 5 节列出的结构；能在 bounded SQLite 语义下重放的样本可以执行验证。若 SQL 明确依赖 vendor 语义，则必须按实际方言重新路由，不能因为写法“看起来通用”就强行使用 SQLite。

### 4.2 SQLite

允许 SQLite 可解析且可执行的教学查询。执行后端是进程内 SQLite；冻结报告记录实际 SQLite 版本、资源上限和 seed/row-scale。SQLite 兼容不代表 MySQL、PostgreSQL、SQL Server 或 Oracle 的语义等价。

### 4.3 MySQL

支持 MySQL 方言的识别、解析、部分结构转换和原生执行路由。判题 Docker 的目标镜像固定为 `mysql:8.4.6`（契约版本为 MySQL 8.4）；题目若声明 `engine_version`，必须与该 runner 兼容。业务库 MySQL 8.0.46 只用于应用持久化，不作为判题器的隐含替代。

### 4.4 PostgreSQL

支持 PostgreSQL 方言的识别、解析、部分结构转换和原生执行路由。判题 Docker 目标为 `postgres:16.10`。例如 `::`、`ILIKE`、`DISTINCT ON` 等结构要分别记录为解析、结构和执行能力；某层缺失时不能向上冒充完整支持。

### 4.5 T-SQL / SQL Server

支持 T-SQL 的识别、解析、部分结构转换和原生执行路由。判题 Docker 目标为 SQL Server `2022-CU20-ubuntu-22.04`。`TOP`、方括号标识符等语法只有在 parser、IR 和 runner 都能处理时才进入执行范围。

### 4.6 Oracle

Oracle 主要作为识别、解析和边界目标；判题 Docker 目标为 `gvenzl/oracle-free:23.7-slim-faststart`（Oracle 23ai 系列）。在没有稳定的 Oracle 原生 runner、schema replay 和结果比较证据前，Oracle 样本不得宣称完整原生支持；应返回 `ENGINE_GAP` 或其他适用的非确定结果。

## 5. SQL 结构范围

以下是当前允许进入结构分析的教学 SQL 结构。每一项仍须通过 schema、IR、witness 和执行器门禁，表中“允许”不等于“所有方言都能原生执行。

| 结构 | 允许内容 | 已知边界 |
|---|---|---|
| SELECT/projection | 列、限定列、`*`、别名、表达式、算术、字面量、CAST | 类型转换和列类型必须能在目标执行器重放 |
| WHERE/三值逻辑 | `AND`、`OR`、`NOT`、比较、括号、`IS NULL`、`IS NOT NULL` | NULL、空结果和 UNKNOWN 必须有专门 witness |
| IN/BETWEEN/LIKE | 值列表、子查询、`NOT IN`、`BETWEEN`、`LIKE`、`ESCAPE` | `NOT IN` 的 NULL trap 需要额外证据 |
| JOIN | comma/cross、inner、left、right、full、natural、using、on、自连接、多表 | 外连接需要 dangling-row world；各方言实现可能不同 |
| GROUP/HAVING/聚合 | `COUNT`、`SUM`、`AVG`、`MIN`、`MAX`、`GROUP BY`、`HAVING`、部分 `FILTER` | `ROLLUP`、`CUBE`、`GROUPING SETS` 通常停在执行边界 |
| DISTINCT/ORDER/LIMIT | `DISTINCT`、部分 `DISTINCT ON`、升降序、NULLS、ordinal、alias、`LIMIT/OFFSET`、`FETCH`、`TOP` | vendor 写法需要匹配原生 runner；排序必须明确 tie 语义 |
| 集合运算 | `UNION`、`UNION ALL`、`INTERSECT`、`EXCEPT`、三分支链 | `INTERSECT ALL`、`EXCEPT ALL` 是已知 SQLite/执行缺口 |
| 子查询 | scalar、`IN`、`EXISTS`、相关/嵌套子查询、`ANY/ALL/SOME` | 相关列、空结果和 NULL world 可能导致 UNDECIDED |
| CTE | 单/多 CTE、依赖链、递归 `UNION/UNION ALL` | `SEARCH/CYCLE` 主要是方言/结构边界 |
| CASE | simple/searched CASE、`WHEN/THEN`、`ELSE` | 分支覆盖需要构造可区分 witness |
| 窗口 | `ROW_NUMBER`、`RANK`、`DENSE_RANK`、`NTILE`、`LAG`、`LEAD`、`FIRST_VALUE`、`LAST_VALUE`、聚合窗口、partition/order/frame、named window | tie、frame 和排序稳定性必须显式约束 |

## 6. 明确 out-of-scope 或需要单独扩展的特性

下列特性不能仅凭“解析器能读”就称为 Phase 1 完整支持：

- DDL、DML、事务控制、存储过程、触发器、用户自定义函数、动态 SQL；
- 需要外部文件、网络、时间、随机数、会话变量或权限状态的查询；
- `ROLLUP`、`CUBE`、`GROUPING SETS`、`INTERSECT ALL`、`EXCEPT ALL`；
- `LATERAL`、`QUALIFY`、Oracle `SEARCH/CYCLE` 及其他只在部分引擎存在的结构；
- 任何尚未完成 typed IR、witness 生成、结果比较和原生 runner 验证的 vendor-specific 语法；
- 多语句脚本、模板占位符、夹带自然语言说明且无法确定 SQL 边界的输入；
- 资源可能失控的笛卡尔积、无界递归、超出行数/结果行数/VM step 限制的查询。

这些输入可以被记录为 parser/input gap、ENGINE_GAP 或明确 out-of-scope，但不能进入正确性分母。

## 7. 四种主要 verdict，用通俗话说

### `NOT_EQUIVALENT`

找到了一个合法的 witness 数据库，使标准 SQL 和学生 SQL 的结果不同（列、行、重复值或顺序至少一项不同）。这是“至少存在一个反例”，可以支持非等价结论；它不是说所有数据都会不同。

### `EQUIVALENT`

在当前声明的有限 seeds、row scales、schema、规则和执行器范围内，等价控制或可信规则成立，且没有观察到差异。它是 bounded validation 结论，不是对所有数据库、所有行数和所有方言语义的全局 SQL 等价定理。

### `UNDECIDED`

当前证据不足：没有找到反例，但也没有足够的等价控制或可信规则证明等价。系统必须保持保守，不能把“这次没测出问题”当成正确。

### `ENGINE_GAP`

需要某个方言的原生执行器，但对应 runner 未配置、不可达、版本不兼容或不支持该结构。它表示“验证条件缺失”，不是“SQL 等价”，也不是“SQL 不等价”。禁止静默回退到另一种数据库。

## 8. 其他常见术语

### `INPUT_GAP` 与 generation failure

`INPUT_GAP` 表示输入本身无法形成可重放任务，例如 schema 不完整、物理表/列无法建立、多语句或模板边界不明确。`generation failure` 是在 freeze 配对生成阶段没有生成出完整 mutation 或 equivalence control 的记录，常见原因是 parse、render、operator 或 equivalence 生成失败。两者都应被计数和追踪，不能偷偷从分母删除。

### SQL family

一个 family 是按稳定 family ID 归并的一组同源 SQL 记录，通常共享规范 SQL、schema、方言和 lineage。freeze 以 family 为覆盖单位，避免同一题目的重复文本把覆盖率夸大。

### mutation row

从规范 SQL 生成一个有明确规则变化的学生 SQL，例如把 `>` 改成 `>=`。它预期是 `NOT_EQUIVALENT`，用于检验判题器能否发现特定差异。

### equivalence control

对规范 SQL 做语义上应保持不变的改写，例如安全的冗余真谓词。它预期是 `EQUIVALENT`，用于检验判题器不会把合法等价写法误判为错误。

### witness

为某个具体差异构造的有限数据库实例，目的是让差异显现，例如专门制造边界值、NULL、重复值、空结果或 dangling row。

## 9. declared support scope 的准入条件

一个 family 只有同时满足以下条件，才算进入声明支持范围：

1. family、SQL、schema、dialect 和 lineage 元数据完整且可追溯；
2. 标准 SQL 与生成的学生 SQL 都能被严格解析；
3. 两条 SQL 都能转换为当前版本的结构 IR，且 ASTDiff 可表示；
4. schema scope 可解析，涉及的表、列、类型和约束可以 replay；
5. 至少生成一个预期 `NOT_EQUIVALENT` 的 mutation row；
6. 至少生成一个预期 `EQUIVALENT` 的 equivalence control；
7. 所需的 bounded 或原生执行器可达、版本兼容且资源限制明确；
8. witness、执行比较和必要的 mutation/repair 证据能完整落盘；
9. 结论不是由 parser 猜测、静默方言降级或单次偶然运行得到的。

当前 freeze runner 将“mutation 和 equivalence control 都成功生成的 hidden family”作为声明范围的机械定义；这一定义必须在同一冻结开始前固定。

## 10. 统计分母与验收门禁

- `EQUIVALENT` 和 `NOT_EQUIVALENT` 才是确定性 verdict，可进入相应标签的一致性统计分母。
- `UNDECIDED`、`ENGINE_GAP`、`INPUT_GAP` 不进入等价/非等价正确率分母，但必须单独报告数量、比例和分层分布。
- `generation_failures` 不能被当成 `UNDECIDED`，也不能通过减少声明范围来掩盖。
- `determinate_label_mismatches` 是预期标签与确定性 verdict 不一致的数量，必须为 0。

Phase 1 freeze 只有在以下条件全部满足时，才可将 `acceptance.pass` 记为 `true`：

```text
generation_failures == 0
determinate_label_mismatches == 0
repeat_run_stable == true
```

此外还必须保存代码、语料、配置、依赖、引擎版本和报告摘要的 fingerprint，并完成公开泄漏审计。通过这些门禁仍只表示“在声明边界和冻结证据下通过”，不表示全局 SQL 等价证明。

## 11. Freeze 后的不可变规则

- hidden SQL、学生 SQL 和失败 family 明文只在冻结运行时使用；报告只保存摘要、计数和不可逆 digest；
- 不得读取 hidden 失败明文后针对性修改 parser、mutation、witness 或 verdict 逻辑；
- 若需要修复问题，必须先形成公开、可审查的证据或测试，修改实现后创建新的 hidden snapshot，再重新冻结；
- 新 freeze 必须至少独立运行两次，行数、分层 verdict 和 digest 稳定；
- 支持范围、方言版本或 out-of-scope 清单发生变化时，必须更新本契约、提高版本号并重新生成报告，不能在旧报告上“解释性通过”。

## 12. 当前 v16 基线（仅作未通过的起点）

报告：`data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v16.json`

| 指标 | 当前值 |
|---|---:|
| hidden families | 5,846 |
| declared supported families | 5,839 |
| scope coverage | 99.8803% |
| generated pair rows | 11,678 |
| generation failures | 7 |
| determinate label mismatches | 22 |
| `UNDECIDED` | 680 |
| `ENGINE_GAP` | 431 |
| repeat run stable | true |
| `acceptance.pass` | **false** |

这组数据说明当前运行具有重复稳定性，但仍有 7 个生成缺口和 22 个确定性标签 mismatch。因此它是后续修复的基线，不是 Phase 1 已完成的证明。

## 13. 变更契约的审批规则

任何人提出扩大或缩小范围时，变更请求至少要包含：

- 新增或删除的方言、精确版本和 SQL 特性；
- parser、IR、witness、执行器和资源限制的证据；
- 对公开 train/public 集的回归结果；
- 新 hidden snapshot 的双次 freeze 结果；
- 对 `UNDECIDED`、`ENGINE_GAP`、generation failure 和 mismatch 的影响；
- 文档版本、代码版本、引擎镜像 digest 和回滚方式。

在变更获批准并生成新 freeze 之前，旧契约和旧报告继续有效。测试失败不是自动扩大 out-of-scope 的理由。

