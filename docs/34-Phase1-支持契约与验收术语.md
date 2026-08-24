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

## 14. 完整术语表

本节按“第一次接触系统的维护者”来写。前文已经用过的术语也在这里集中解释，阅读代码、测试报告或审计脚本时可以直接查阅。

### 14.1 系统和输入

- **Phase 1**：系统中负责判断 SQL 结构差异、生成验证数据并比较两条 SQL 的第一阶段。它的输出是有限证据和 verdict，不是数学定理。
- **SQL**：一种向数据库描述“读取或处理数据”的语言。本文主要处理只读查询，不把修改数据库的脚本当作普通答题 SQL。
- **query（查询）**：一条可执行的 SQL 语句，例如 `SELECT id FROM course`。
- **SQL pair（SQL 对）**：同一个题目上下文中的两条 SQL，通常是一条标准答案和一条学生答案。只有比较二者才有“是否等价”的问题。
- **standard SQL（标准 SQL）**：尽量使用 SQL 标准共同语法的写法。它不是某一个数据库产品的完整实现。
- **dialect（方言）**：某个数据库产品对 SQL 语法和语义的具体变体，例如 MySQL、PostgreSQL、T-SQL 和 Oracle。
- **vendor-specific（厂商专属）**：只在某个数据库产品，或在不同产品中行为不同的语法、函数或规则。厂商也称 vendor。
- **schema（数据库结构）**：表、列、列类型、主键、唯一约束、外键等元数据；它描述“数据长什么样”，不是数据本身。
- **metadata（元数据）**：描述数据或任务的数据，例如方言、版本、来源、列类型和创建时间；它不是 SQL 查询结果本身。
- **table（表）**：按行和列保存数据的逻辑对象。
- **column（列）**：表中具有同一含义和类型的一组值，例如 `course.id`。
- **row（行）**：表中一条完整记录。
- **corpus（语料库）**：收集到的 SQL、schema、方言、来源和 lineage 等记录的集合。
- **family / family ID**：family 是按规范 SQL、schema 和来源关系归并的一组同源记录；family ID 是它的稳定唯一标识。统计按 family 去重，避免同一题目重复出现造成虚高覆盖率。
- **lineage（来源链）**：一条 SQL 从原始来源、清洗、规范化、变异到最终测试记录的可追溯关系。
- **train / public / hidden**：三种数据分区。train 用于开发，public 用于公开回归，hidden 用于最终冻结验收；hidden 结果不能反向指导同一版本的优化。
- **snapshot（快照）**：在某个时间点固定的代码、语料、配置和依赖组合。新的 snapshot 才能作为新一轮独立验收输入。
- **SQLAlchemy**：Python 中负责连接数据库、建立模型和执行持久化操作的库；业务 API 使用它访问业务数据库。
- **Alembic**：与 SQLAlchemy 配套的数据库迁移工具，用来按版本升级或回退业务数据库结构。
- **Docker container（容器）**：由 Docker 启动的隔离进程环境；判题容器销毁后其中的临时数据也应被销毁。
- **host（宿主机）**：运行业务服务或 Docker 的操作系统环境。宿主机上的业务 MySQL 与容器内判题 MySQL 是两个实例。

### 14.2 SQL 语言基础

- **statement（语句）**：一条完整 SQL。本文通常要求输入是“单条语句”，而不是多条语句拼在一起的脚本。
- **SELECT**：从一个或多个表读取数据的语句入口。
- **projection（投影）**：`SELECT` 后面决定输出哪些列或表达式的部分。
- **expression（表达式）**：可以计算出一个值的 SQL 片段，例如 `price * quantity` 或 `id + 1`。
- **literal（字面量）**：直接写在 SQL 中的常量，例如 `10`、`'Alice'`、`NULL`。
- **alias（别名）**：给表或输出列起的临时名字，例如 `SELECT id AS course_id`。
- **CAST**：显式把一个值转换为另一种数据类型，例如 `CAST(score AS INTEGER)`。
- **predicate（谓词）**：结果为真、假或未知的条件表达式，常见于 `WHERE` 和 `ON`。
- **clause（子句）**：SQL 语句中的功能区段，例如 `SELECT`、`WHERE`、`GROUP BY`、`ORDER BY`。
- **WHERE**：过滤行，只保留条件为真的行。
- **AND / OR / NOT**：分别表示“同时满足”“至少满足一个”“取反”。
- **三值逻辑（three-valued logic）**：SQL 条件不仅有真和假，还可能是 `UNKNOWN`；这通常由 `NULL` 参与比较造成。
- **NULL**：表示缺失或未知值，不等于数字 0、空字符串或普通文本。
- **UNKNOWN**：三值逻辑中的未知结果。`WHERE` 通常只保留 TRUE，FALSE 和 UNKNOWN 都会被过滤掉。
- **NULL trap**：含 NULL 的特殊陷阱。例如 `x NOT IN (1, NULL)` 通常不能简单理解成“x 不是 1”。
- **IN**：判断一个值是否属于值列表或子查询结果。
- **BETWEEN**：判断一个值是否落在上下界之间，通常包含两端。
- **LIKE**：按通配符模式匹配文本。
- **ESCAPE**：指定 LIKE 模式中用于转义通配符的字符。
- **JOIN**：把两个或多个表按条件组合起来。
- **INNER JOIN**：只保留两边能匹配的组合。
- **OUTER JOIN**：即使一边没有匹配，也保留该边的行，并在另一边补 NULL；包括 `LEFT`、`RIGHT` 和 `FULL` JOIN。
- **CROSS JOIN / comma join**：生成两表的笛卡尔积，不要求匹配条件。
- **NATURAL JOIN**：自动按同名列连接，容易因 schema 变化产生意外结果。
- **USING**：按指定的同名连接列连接，例如 `USING (course_id)`。
- **ON**：写 JOIN 匹配条件的子句。
- **self-join（自连接）**：同一张表在一条查询中以两个别名参与 JOIN。
- **dangling row / dangling-row world**：一边表中没有另一边匹配项的行；专门构造这类数据可以验证外连接是否正确。
- **aggregate function（聚合函数）**：把多行汇总成一个值的函数，例如 `COUNT`、`SUM`、`AVG`、`MIN`、`MAX`。
- **GROUP BY**：把行按一个或多个键分组。
- **HAVING**：在分组和聚合之后过滤分组。
- **FILTER**：给某个聚合函数单独附加过滤条件，例如 `COUNT(*) FILTER (WHERE active = 1)`。
- **DISTINCT**：去掉重复结果行。
- **DISTINCT ON**：PostgreSQL 等方言提供的“按指定列去重并保留一行”写法，不是所有数据库都有。
- **ORDER BY**：指定结果排序。
- **tie（并列）**：多行在排序键上相同。没有额外排序键时，这些行的相对顺序可能不稳定。
- **NULLS FIRST / NULLS LAST**：指定排序时 NULL 放在最前还是最后。
- **ordinal（序号引用）**：用 SELECT 列的位置编号排序，例如 `ORDER BY 1`。
- **LIMIT/OFFSET**：分别限制返回行数和跳过前若干行。
- **FETCH**：SQL 标准中用于限制或分页结果的写法。
- **TOP**：T-SQL 中用于限制返回行数的写法。
- **set operation（集合运算）**：把多个查询结果组合起来的运算。
- **UNION**：合并结果并去重。
- **UNION ALL**：合并结果但保留重复行。
- **INTERSECT**：只保留同时出现在两边的结果，通常去重。
- **EXCEPT**：保留左边有、右边没有的结果，通常去重。
- **`INTERSECT ALL` / `EXCEPT ALL`**：保留重复计数的集合运算变体，当前执行边界未完整支持。
- **subquery（子查询）**：嵌套在另一条 SQL 中的查询。
- **scalar subquery（标量子查询）**：预期返回一个值的子查询。
- **`IN` subquery**：返回一列值、供 `IN` 判断成员关系的子查询。
- **`EXISTS` subquery**：只判断子查询是否至少返回一行。
- **correlated subquery（相关子查询）**：子查询引用外层查询的列，因此会随外层行变化执行逻辑。
- **ANY / ALL / SOME**：把一个值与子查询返回的多个值比较的量词写法；`SOME` 通常与 `ANY` 同义。
- **CTE（Common Table Expression，公用表表达式）**：用 `WITH` 定义、只在当前语句中使用的临时命名查询。
- **recursive CTE（递归 CTE）**：CTE 通过基础查询和递归查询反复生成结果，常用于树或图遍历。
- **CASE**：SQL 的条件表达式；`WHEN/THEN` 表示条件和结果，`ELSE` 表示所有条件都不满足时的结果。
- **window function（窗口函数）**：在不把多行合并成一行的情况下，基于相关行计算排名、偏移或聚合的函数。
- **partition（窗口分区）**：窗口函数先按指定列拆成的独立数据组。
- **window order（窗口排序）**：窗口函数在每个分区内使用的排序规则。
- **frame（窗口框架）**：窗口函数在当前行计算时实际可观察的行范围，例如前两行到当前行。
- **named window（命名窗口）**：把窗口的分区、排序和 frame 定义命名后复用。
- **ROW_NUMBER / RANK / DENSE_RANK / NTILE**：常见窗口排名函数；并列行的编号规则不同。
- **LAG / LEAD**：读取当前行前后某个位置的值。
- **FIRST_VALUE / LAST_VALUE**：读取窗口框架中的第一值或最后值。

### 14.3 方言和高级边界语法

- **MySQL**：一种数据库产品及其 SQL 方言；本业务库使用 8.0.46，判题 Docker 使用 8.4.6。
- **PostgreSQL**：一种数据库产品及其 SQL 方言；本判题 Docker 使用 16.10。
- **T-SQL**：Microsoft SQL Server 使用的 SQL 方言；本判题 Docker 使用 2022 CU20。
- **Oracle**：Oracle Database 使用的 SQL 方言；当前仅把 Oracle Free 23.7 作为边界目标，不能据此宣称完整 Oracle 生产支持。
- **SQLite**：嵌入式数据库；本地 bounded oracle 使用 Python `sqlite3` 调用它。
- **generic / standard**：没有明确厂商专属语法、按教学标准 SQL 处理的方言标签。
- **`::`**：PostgreSQL 常见的类型转换写法，例如 `score::integer`。
- **ILIKE**：PostgreSQL 提供的不区分大小写的 LIKE 匹配。
- **LATERAL**：允许一个 FROM 项引用同一 FROM 列表中它左侧项目的相关表表达式。
- **QUALIFY**：部分数据库提供的窗口函数结果过滤子句，作用位置类似窗口计算之后的 WHERE。
- **ROLLUP**：按层级逐步汇总分组的扩展语法。
- **CUBE**：生成多个维度组合汇总的扩展语法。
- **GROUPING SETS**：显式列出多组分组方式的扩展语法。
- **SEARCH / CYCLE**：递归 CTE 中描述遍历顺序和循环检测的方言扩展，常见于 Oracle/PostgreSQL 生态。
- **DDL（Data Definition Language）**：创建或修改数据库结构的语句，例如 `CREATE TABLE`、`ALTER TABLE`。
- **DML（Data Manipulation Language）**：修改数据的语句，例如 `INSERT`、`UPDATE`、`DELETE`。
- **事务控制**：`BEGIN`、`COMMIT`、`ROLLBACK` 等控制一组数据库操作原子性的语句。
- **存储过程 / 触发器**：保存在数据库中、可被调用或在事件发生时自动执行的程序逻辑。

### 14.4 判题管线和证据

- **parser（解析器）**：把 SQL 文本转换成机器可以处理的语法树，并在语法不合法时报告错误。
- **strict parser（严格解析器）**：只接受完整、边界明确且在当前语法契约内的单条 SQL；遇到多语句、自然语言前缀或无法表达的结构时拒绝，而不是猜测用户意图。
- **AST（Abstract Syntax Tree，抽象语法树）**：按 SQL 语法层级表示一条 SQL 的树形结构，例如 SELECT 节点下面有 WHERE 节点。
- **IR（Intermediate Representation，中间表示）**：在 AST 与后续分析之间使用的统一结构。它去掉不影响分析的文本差异，保留需要比较的 SQL 结构。
- **SQLStructureIR**：本项目的 SQL 结构 IR 类型，保存查询块、谓词、JOIN、聚合、窗口、CTE 等可分析结构。
- **query block（查询块）**：一条 SELECT、子查询或 CTE 内部相对独立的查询作用域；每个查询块可以有自己的 FROM、WHERE 和输出列。
- **typed IR**：带有明确字段类型和结构约束的 IR。某个语法即使 AST 能表示，如果 typed IR 没有对应字段，也不能称为完整结构支持。
- **ASTDiff**：比较两棵 AST 或两个 IR，找出新增、删除或修改的结构节点。
- **structure analysis（结构分析）**：只根据 AST/IR 判断 SQL 哪个子句发生了什么变化，不等同于执行结果比较。
- **semantic equivalence（语义等价）**：在给定数据库语义和所有相关输入数据下，两条 SQL 产生相同结果的性质。本系统只做有边界的证据验证。
- **Gold Oracle**：独立于主要 witness/诊断实现的参考判定器。它在受控数据世界中分别执行两条 SQL，帮助发现主判题逻辑的误判；它也不能把有限测试变成全局证明。
- **witness**：为暴露某一差异而构造的有限数据库实例，例如包含 NULL、重复值、边界值或空结果的实例。
- **bounded**：有明确上限的执行方式，包括表行数、结果行数、递归深度、执行步数、CPU、内存和超时；超过上限就停止并报告边界结果。
- **finite world（有限数据世界）**：一次 witness 执行所使用的具体 schema 和有限行数据。
- **native runner（原生执行器）**：直接连接目标数据库产品并按其真实语义执行 SQL 的 runner，而不是把 SQL 改写到另一种数据库。
- **runner**：负责创建数据、执行查询、收集结果和清理资源的程序组件。
- **engine（执行引擎）**：实际执行 SQL 的数据库实例或数据库进程。
- **engine_version**：记录执行器实际数据库版本的元数据，用于确认题目声明和 runner 兼容。
- **schema replay**：根据 schema 文本重新创建表、列和约束，使同一 SQL 可以在测试环境重放。
- **schema scope（schema 作用域）**：本次 SQL pair 实际引用的表、列、别名、类型和约束的集合；它决定 witness 需要创建哪些对象。
- **mutation operator**：把规范 SQL 按一条明确规则改写成学生 SQL 的程序，例如把 `>` 改为 `>=`。
- **mutation**：一次具体的有意改写；它通常预期造成 `NOT_EQUIVALENT`。
- **equivalence control**：预期保持语义不变的改写，用来检验系统不会误报。
- **repair evidence**：把变异 SQL 恢复或替换后重新执行，证明某个差异确实是导致结果变化的原因的证据。
- **attribution（归因）**：把观察到的结果差异关联到具体 SQL 结构差异、规则或知识点。
- **evidence（证据）**：支持某个 verdict 或归因的可复现材料，包括 ASTDiff、witness 数据摘要、执行结果和 mutation/repair 记录。
- **validation（验证）**：按照固定输入和规则运行检查并比较结果；本文的 validation 是有边界的实验验证，不是形式化证明。
- **replay（重放）**：使用同一输入、配置和执行器重新运行，以检查结果是否稳定。
- **silent fallback（静默降级）**：原本应使用某个引擎却无提示地改用另一个引擎。这在 Phase 1 中禁止，因为不同引擎的语义可能不同。

### 14.5 生成、执行和资源限制

- **parse failure / parser gap**：SQL 无法被当前解析器解析，或解析器没有覆盖该语法；它不是非等价结论。
- **render**：把 AST/IR 重新输出为 SQL 文本的过程。
- **operator gap**：没有适用于当前 AST 结构的 mutation operator，因而无法生成测试变异。
- **equivalence generation gap**：无法为当前 family 生成可靠的等价控制。
- **generation failure**：freeze 配对生成阶段未能生成所需 mutation 或 equivalence control 的统称。
- **INPUT_GAP**：输入、schema、模板或语句边界导致任务无法完整重放。
- **ENGINE_GAP**：需要的原生引擎缺失、不可达、版本不兼容或不支持该结构。
- **row-scale**：生成数据时使用的目标行数级别，例如 4、8、16；它不是数据库版本。
- **seed**：随机数据和规则选择使用的确定性起始数字。相同 seed 和相同输入应生成相同结果。
- **VM step**：SQLite 虚拟机执行 SQL 时消耗的指令步数；设置上限是为了阻止无界查询占满资源。
- **resource limit（资源限制）**：对单次任务施加的最大时间、内存、CPU、进程数、行数或执行步数。
- **timeout（超时）**：任务超过规定时间后停止等待并返回边界结果。线程超时不一定能杀死底层数据库进程，因此生产环境仍需要 worker 隔离。
- **worker（任务进程）**：专门执行判题任务的独立进程或服务。它可以设置硬超时、CPU/内存限制，并在卡死或崩溃后被终止和重建。
- **Docker**：用于以隔离容器运行数据库引擎的工具。判题容器是临时执行环境，不是业务数据库。
- **image tag（镜像标签）**：Docker 镜像的可读版本名称，例如 `mysql:8.4.6`。
- **image digest（镜像摘要）**：Docker 镜像内容的不可变哈希；它比标签更适合证明实际运行的镜像没有变化。
- **`PARSEVAL_*_URL`**：判题器连接各方言 native runner 的配置项，例如 `PARSEVAL_MYSQL_URL`；为空或不可达时应产生 `ENGINE_GAP`。

### 14.6 Verdict、报告和验收

- **EQUIVALENT**：在契约声明的有限证据范围内未观察到差异，并有等价控制或可信规则支持；不是全局等价证明。
- **NOT_EQUIVALENT**：至少找到一个合法 witness，使两条 SQL 的结果不同；这是反例证明，不表示所有数据都会不同。
- **UNDECIDED**：证据不足，既没有有效反例，也没有足够等价证据。
- **ENGINE_GAP**：缺少能执行该方言/版本/特性的引擎；不是等价，也不是不等价。
- **declared support scope**：在 freeze 开始前固定的 family 集合；当前机械准入条件是 mutation 和 equivalence control 都成功生成，并满足本契约其他前提。
- **scope coverage**：进入 declared support scope 的 family 数除以 hidden family 总数。
- **correctness denominator（正确性分母）**：用于计算确定性标签一致率的样本集合。本契约只把 EQUIVALENT 和 NOT_EQUIVALENT 放入其中。
- **determinate verdict（确定性 verdict）**：EQUIVALENT 或 NOT_EQUIVALENT；相对地，UNDECIDED 和 ENGINE_GAP 不是确定性结论。
- **determinate label mismatch**：测试预期标签与判题器给出的确定性 verdict 不一致。例如预期 NOT_EQUIVALENT 却输出 EQUIVALENT。
- **label（标签）**：测试记录中表示预期结论的字段，例如某个 mutation row 的标签是 NOT_EQUIVALENT，某个 equivalence control 的标签是 EQUIVALENT。
- **generation_failures**：本轮 freeze 未能生成完整测试 pair 的数量。
- **repeat_run_stable**：相同 freeze 独立运行两次时，行数、分层 verdict 和 digest 都一致。
- **freeze（冻结验收）**：固定代码、语料、配置、依赖和引擎后，对 hidden 数据执行的不可反馈验收运行。
- **hidden snapshot**：用于某次 freeze 的 hidden 数据快照；修复实现后必须换新快照，不能在旧 hidden 上反复试错。
- **fingerprint**：对代码、配置、依赖、引擎和工作树状态生成的摘要，用于确认“到底测了哪个版本”。
- **digest**：通常指对 SQL、失败 family 或报告内容计算的不可逆哈希；它可以用于重复性比对，但不能恢复原文。
- **leakage audit（泄漏审计）**：检查 train、public、hidden 之间是否存在不应有的 family、lineage、schema 或原始记录重叠。
- **hard overlap**：泄漏审计中确定违反分区隔离的重复，不是普通的语法相似。
- **acceptance.pass**：freeze runner 根据所有门禁计算出的总通过布尔值。只有 generation failures 为 0、确定性标签 mismatch 为 0、重复运行稳定且其他冻结条件满足时才可为 true。
- **MVP（Minimum Viable Product，最小可运行产品）**：能运行主链路的最小版本，不代表功能完整、参数校准或生产就绪。
- **out-of-scope**：契约明确不承诺的范围。它必须在测试前声明，不能在看到失败后临时添加。

### 14.7 常见结果如何解读

- **解析成功但不能执行**：通常只能说明结构层有证据；若缺少原生引擎，结果应是 ENGINE_GAP，而不是 EQUIVALENT。
- **执行多次都相同**：只能提高有限证据的可信度；如果没有等价控制，仍可能是 UNDECIDED。
- **执行一次出现不同**：如果输入、schema 和执行结果都有效，通常足以输出 NOT_EQUIVALENT，因为一个反例就能证明“不保证等价”。
- **生成失败但没有运行结果**：属于 generation failure，不属于 UNDECIDED，也不能从分母中悄悄删除。
- **解析器读不懂**：属于 parser/input 边界或 out-of-scope，不代表学生 SQL 一定错误。

### 14.8 工程和报告用语

- **API（Application Programming Interface，应用程序接口）**：供其他程序调用的接口，例如后端提供的提交 SQL 或查询题目的 HTTP 接口。
- **backend（后端）**：在服务器上执行业务逻辑、访问数据库和调用判题器的程序部分；前端不会直接连接判题数据库。
- **URL（Uniform Resource Locator，统一资源定位符）**：描述服务地址和连接参数的字符串。`PARSEVAL_MYSQL_URL` 就是判题器连接 MySQL runner 的 URL。
- **native（原生）**：直接使用目标数据库产品本身的语法和执行语义，不先改写到 SQLite 或其他数据库。
- **backend/database dialect**：这里的 backend 指实际执行 SQL 的数据库连接；dialect 指该连接接受的语法和语义规则，二者必须匹配。
- **persistent storage（持久化存储）**：服务重启后仍保留的数据，例如业务数据库中的用户和提交记录；witness 数据通常不是持久化数据。
- **configuration（配置）**：决定程序如何运行的外部参数，例如数据库 URL、引擎版本、超时和资源上限。
- **dependency（依赖）**：程序运行所需的 Python 包、数据库驱动、操作系统组件或 Docker 镜像。
- **audit（审计）**：对输入来源、执行过程、权限或结果进行记录和检查，以便事后复核。
- **reproducible（可复现）**：另一位维护者使用相同代码、输入、配置、依赖和引擎时，可以重现同一结果。
- **deterministic（确定性）**：相同输入和 seed 不依赖偶然因素地产生同一输出。确定性不等于结论一定正确，只表示运行稳定。
- **hard timeout（硬超时）**：达到时间上限后真正终止执行任务的机制，而不是只让请求返回、留下后台任务继续运行。
- **rollback（回滚）**：把代码、数据库迁移或引擎版本恢复到之前已验证的版本。
- **feedback（反馈）**：把一次 freeze 或测试运行的结果用于改变实现、配置或支持范围。hidden freeze 禁止用 hidden 结果做同一版本的定向 feedback。
- **coverage（覆盖率）**：满足某个条件的 family 数占总 family 数的比例；例如 scope coverage 衡量进入声明范围的比例，不等于正确率。
- **denominator（分母）**：统计比例时作为总数的样本集合。报告必须明确哪些 verdict 被纳入分母。
- **ratio / rate（比例/率）**：某类样本数除以指定分母得到的数值，例如 generation failure rate。
- **failure（失败）**：某个流程没有完成预定动作，例如没有生成 pair；它不自动等于 SQL 非等价。
- **gap（缺口）**：当前流程缺少某个必要条件或能力。不同 gap 必须按 INPUT_GAP、ENGINE_GAP、parser gap 等具体类型记录。
- **change request（变更请求）**：申请改变方言、版本、SQL 特性或验收规则的正式记录，必须附带证据和回滚方式。
- **version（版本）**：代码、文档、数据库或引擎的可识别发布编号。版本变化后应重新确认兼容性和验收结果。
