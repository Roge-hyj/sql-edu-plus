# Phase 1 SQL 上下文无关语法（CFG）

> Phase 1 当前实测可分析、造数、沙盒判等和归因的 SQL 子集。
> 记号约定：`::=` 定义；`[ ]` 可选；`( A | B )` 二选一；`*` 零或多次；`+` 一次或多次。

截至 2026-07-23，细粒度基准共 150 条：145 条通过，5 条为执行层已知边界，实测通过率 96.67%。该数字描述有限攻击集，不是语义完备性证明。完整证据见：

- `data_construct_test/outputs/phase1_cfg_fragment_capability.json`
- `data_construct_test/outputs/phase1_cfg_supported_samples.jsonl`
- `data_construct_test/outputs/phase1_cfg_known_gaps.jsonl`

## 攻击规模与当前边界

当前能力边界由三层相互独立的实验证据描述：

| 层次 | 覆盖规模 | 结果 |
|------|----------|------|
| CFG 备选项攻击 | 32 个产生式、150 个备选项 | 145 支持，5 个已知执行边界 |
| 参数化收敛攻击 | 600 个分层基例 + 12,000 个生成例，共 12,600 例 | 12,580 支持，20 次已知边界复现，0 个意外失败 |
| 数据库剖面攻击 | 138 个可执行 SQL 对 x 8 类数据库 x 8 个种子，共 8,832 次执行 | 0 执行错误；38 个等价对无反例；100 个非等价对全部检出 |

12,600 例收敛攻击覆盖 20 个参数化语义族、4/8/12/16 四种行数尺度和 2,810 个唯一 SQL/数据库尺度组合。其中 11,816 例预期不等价、756 例预期等价、28 例预期语法拒绝；反例检出率、等价保持率、语法拒绝率和归因命中率均为 100%。26 个批次均未出现新的意外失败签名。在当前分层采样模型下，意外失败率为 0，Wilson 95% 上界为 0.030528%。

数据库剖面攻击覆盖 `targeted`、`empty`、`singleton`、`uniform`、`null_heavy`、`duplicate_heavy`、`group_skew`、`join_aligned`。100 个非等价 SQL 对由“定向 + 随机”数据库全部区分；仅使用随机剖面时检出 99 个，唯一漏检是 `HAVING AVG(salary) > 50000` 与 `>= 50000`，因为随机数据未恰好产生平均值 50000，定向造数已命中该边界。

完整大规模证据见：

- `data_construct_test/outputs/phase1_cfg_convergence_report.json`
- `data_construct_test/outputs/phase1_cfg_convergence_all.jsonl`
- `data_construct_test/outputs/phase1_cfg_convergence_supported.jsonl`
- `data_construct_test/outputs/phase1_cfg_convergence_failures.jsonl`
- `data_construct_test/outputs/phase1_cfg_convergence_detailed_evidence.jsonl`
- `data_construct_test/outputs/phase1_cfg_database_profiles_report.json`
- `data_construct_test/outputs/phase1_cfg_database_profiles_all.jsonl`

这组结果是对当前 CFG、生成器、SQLite 沙盒和采样分布的经验收敛界，不是“任意 SQL、任意数据库”的形式化语义完备证明。CFG 能定义语法可生成范围；语义完备性还需要对方言、类型系统、NULL 三值逻辑、包语义/集合语义、顺序、错误行为以及数据库实例域建立形式化模型，并对每条等价规则给出证明。有限测试只能发现反例和量化未观察失败率，不能证明不存在反例。

---

```
Query         ::= SetQuery                       [入口]
                | SelectQuery

SetQuery      ::= SelectQuery (SetOp SelectQuery)+
SetOp         ::= "UNION" ["ALL"]               [支持]
                | "INTERSECT"                    [支持]
                | "EXCEPT"                       [支持]
                | "INTERSECT ALL"                [不支持：SQLite 执行层]
                | "EXCEPT ALL"                   [不支持：SQLite 执行层]

SelectQuery   ::= [WithClause] SelectBody         [cte / select-basic]

WithClause    ::= "WITH" ["RECURSIVE"] CteList   [cte / cte-recursive]
CteList       ::= CteDef ("," CteDef)*
CteDef        ::= Ident ["(" ColumnList ")"] "AS" "(" SelectQuery ")"

SelectBody    ::= "SELECT" ["TOP" Number] ["DISTINCT"] ProjList
                  [FromClause] JoinStmt*
                  [WhereClause] [GroupByClause]
                  [HavingClause] [WindowClause]
                  [QualifyClause]
                  [OrderByClause] [LimitClause]

ProjList      ::= ProjElem ("," ProjElem)*
ProjElem      ::= Expr [Alias]                   [alias]
                | "*"                              [select-basic]
Alias         ::= "AS" Ident | Ident

CaseExpr      ::= "CASE" WhenClause+ (ELSE Expr)? "END"
WhenClause    ::= "WHEN" Expr "THEN" Expr        [case]

NullExpr      ::= Expr "IS" ["NOT"] "NULL"       [null-handling]
                | Expr "IS" ["NOT"] "DISTINCT FROM" Expr
                | "COALESCE" "(" Expr ("," Expr)* ")"
                | "NULLIF" "(" Expr "," Expr ")"

FuncCall      ::= AggFunc "(" (DISTINCT)? ArgList? ")" [agg-count]
                | ScalarFunc "(" ArgList? ")"
                | RankingFunc OverClause           [window-row-number]
                | ValueWindowFunc OverClause
AggFunc       ::= "COUNT" | "SUM" | "AVG" | "MIN" | "MAX"
ScalarFunc    ::= "ABS" | "LOWER" | "UPPER" | "ROUND" | "TRIM"
RankingFunc   ::= "ROW_NUMBER" | "RANK" | "DENSE_RANK" | "NTILE"
ValueWindowFunc ::= "LAG" | "LEAD" | "FIRST_VALUE" | "LAST_VALUE"

OverClause    ::= "OVER" "(" [PartitionBy] [OBInner] [Frame] ")"
PartitionBy   ::= "PARTITION BY" ExprList         [window-agg]
OBInner       ::= "ORDER BY" OrderItem ("," OrderItem)*
Frame         ::= ("ROWS" | "RANGE") "BETWEEN" FrameBound "AND" FrameBound
FrameBound    ::= "UNBOUNDED PRECEDING" | "CURRENT ROW"
                | Number "PRECEDING" | Number "FOLLOWING"
WindowClause  ::= "WINDOW" Ident "AS" "(" [PartitionBy] [OBInner] [Frame] ")"
QualifyClause ::= "QUALIFY" Predicate              [支持，转写后执行]

FromClause    ::= "FROM" TSource ("," TSource)*
TSource       ::= TableName [Alias]               [select-basic]
                | SubQuery Alias
                | "LATERAL" SubQuery Alias        [不支持：SQLite 执行层]
SubQuery      ::= "(" SelectQuery ")"

JoinStmt      ::= JoinSide "JOIN" TSource JoinCond
                | "CROSS JOIN" TSource
                | "NATURAL JOIN" TSource
JoinSide      ::= ["INNER" | "LEFT" ["OUTER"] | "RIGHT" ["OUTER"] | "FULL" ["OUTER"]]
JoinCond      ::= "ON" Predicate                  [join-on]
                | "USING" "(" ColumnList ")"

WhereClause   ::= "WHERE" Predicate               [where]
Predicate     ::= Predicate ("AND" | "OR") Predicate
                | "NOT" Predicate
                | "(" Predicate ")"
                | Comparison                        [where-comp]
                | BetweenExpr                     [between]
                | InExpr                          [subquery-in / in-list]
                | LikeExpr                        [like]
                | ExistsExpr                      [subquery-exists]
                | NullExpr

Comparison    ::= Expr CompOp Expr
                | Expr CompOp Quantifier "(" SelectQuery ")"
CompOp        ::= "=" | "!=" | "<>" | ">" | "<" | ">=" | "<="
Quantifier    ::= "ALL" | "ANY" | "SOME"

BetweenExpr   ::= Expr ["NOT"] "BETWEEN" Expr "AND" Expr

InExpr        ::= Expr ["NOT"] "IN" "(" ValueList ")"
                | Expr ["NOT"] "IN" "(" SelectQuery ")"

LikeExpr      ::= Expr ["NOT"] "LIKE" Pattern ["ESCAPE" String]

ExistsExpr    ::= ["NOT"] "EXISTS" "(" SelectQuery ")"

GroupByClause ::= "GROUP BY" ExprList             [支持]
                | "GROUP BY ROLLUP" "(" ExprList ")" [不支持]
                | "GROUP BY CUBE" "(" ExprList ")"   [不支持]
HavingClause  ::= "HAVING" Predicate              [having]

OrderByClause ::= "ORDER BY" OrderItem ("," OrderItem)*
OrderItem     ::= (Expr | Number) ["ASC" | "DESC"] ["NULLS" ("FIRST" | "LAST")]

LimitClause   ::= "LIMIT" Number OffsetClause?    [支持]
                | "LIMIT" Number "," Number       [支持，MySQL]
                | "FETCH FIRST" Number "ROWS ONLY" [支持]
OffsetClause  ::= "OFFSET" Number

WindowRef     ::= Expr Alias                       [window-agg / window-row-number]

Expr          ::= Term (AddOp Term)*              [arithmetic]
                | UnaryOp Expr
                | CaseExpr                         [case]
                | FuncCall
                | "CAST" "(" Expr "AS" TypeName ")"
                | SubScalarExpr                    [subquery-scalar]
                | ColumnRef
                | Literal
Term          ::= Factor (MulOp Factor)*
Factor        ::= "(" Expr ")" | ColumnRef | Literal | FuncCall
SubScalarExpr ::= "(" SelectQuery ")"
AddOp         ::= "+" | "-" | "||"
MulOp         ::= "*" | "/" | "%"
UnaryOp       ::= "+" | "-"
TypeName      ::= "INTEGER" | "REAL" | "TEXT"

ColumnRef     ::= Ident ("." Ident)*
TableName     ::= Ident ("." Ident)*
ColumnList    ::= Ident ("," Ident)*
Ident         ::= BareIdent | QuotedIdent
QuotedIdent   ::= '"' Char+ '"' | "`" Char+ "`" | "[" Char+ "]"
Literal       ::= String | Number | "NULL" | BOOL
Number        ::= Digit+ ["." Digit+] [Exponent]
Exponent      ::= ("e" | "E") ["+" | "-"] Digit+
String        ::= "'" (Char | "''")* "'"
BOOL          ::= "TRUE" | "FALSE"
Pattern       ::= String
ValueList     ::= Expr ("," Expr)*
ExprList      ::= Expr ("," Expr)*
ArgList       ::= Expr ("," Expr)*
```

---

## KP → 语法规则映射

| KP ID | 对应规则 | 说明 |
|-------|---------|------|
| `select-basic` | SelectBody, TSource | 基本 SELECT ... FROM |
| `distinct` | DISTINCT | 去重 |
| `alias` | ProjElem ..., As... | 列别名 |
| `arithmetic` | Expr, Term | 算术运算 |
| `case` | CaseExpr, WhenClause | CASE WHEN |
| `null-handling` | NullExpr | COALESCE / IS NULL |
| `where` | WhereClause | WHERE 子句 |
| `where-comp` | Comparison | 比较谓词 |
| `between` | BetweenExpr | BETWEEN |
| `in-list` | InExpr (ValueList) | IN 列表 |
| `subquery-in` | InExpr (SelectQuery) | IN 子查询 |
| `subquery-exists` | ExistsExpr | EXISTS 子查询 |
| `subquery-scalar` | SubScalarExpr | 标量子查询 |
| `join-inner` | JoinSide=INNER, JOINStmt | 内连接 |
| `join-left` | JoinSide=LEFT, JoinStmt | 左连接 |
| `join-right-full` | JoinSide=RIGHT/FULL, JoinStmt | 右/全连接 |
| `join-on` | JoinStmt ..., ON ... | JOIN ON 条件 |
| `complex-join` | JoinStmt* (>=2) | 多表连接 |
| `union` | SetQuery, SetOp | 集合操作 |
| `group-by` | GroupByClause | GROUP BY |
| `having` | HavingClause | HAVING |
| `order-by` | OrderByClause | ORDER BY |
| `limit-offset` | LimitClause | LIMIT/OFFSET |
| `agg-count` | FuncCall(Agg...) | COUNT/SUM/AVG/MIN/MAX |
| `window-row-number` | RankingFunc OverClause | ROW_NUMBER/RANK/DENSE_RANK |
| `window-agg` | AggFunc + OverClause; | 聚合窗口函数 |
| `cte` | WithClause (非递归) | CTE |
| `cte-recursive` | WithClause RECURSIVE | 递归 CTE |
