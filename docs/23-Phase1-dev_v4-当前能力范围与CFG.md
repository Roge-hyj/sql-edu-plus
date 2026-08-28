# Phase 1 dev_v4 Flow-Scoped CFG：当前能力范围的形式化定义

本文描述当前实现和证据覆盖的范围，不是完整 SQL 标准，也不是对任意数据库和任意有限关系实例的语义完备证明。

## 0. Flow-scoped 形式对象

给定一次提交：

```text
x = <student_sql, correct_sql, schema_text>
```

实际处理流为：

```text
x
 -> strict parser
 -> SQLStructureIR
 -> ASTDiffNode / DiffGraph
 -> TacticRegistry / Obligation
 -> WitnessWorld / E_db
 -> SQLite sandbox / E_data
 -> mutation isolation / E_MUT
 -> Phi attribution arbiter
 -> AttributionResult
```

定义查询 CFG。机器权威源是 `contracts/phase1_cfg_grammar.json`；本文的数学记号和该文件逐项对应，不能用本文未列出的 SQLGlot AST 节点扩大符号范围：

```text
G_Q = (N_Q, Sigma_Q, P_Q, Query)
L(G_Q) = { q | Query =>* q }
```

其中：

- `N_Q` 等于机器文件的 `nonterminals`，是 `Submission`、`Query`、`SelectQuery`、`Predicate`、`Expression` 等句法非终结符；
- `Sigma_Q` 等于机器文件的 `terminals`，包括关键字、标点和 `IDENTIFIER`、`QUALIFIED_IDENTIFIER`、`LITERAL`、`SCHEMA_TEXT` 等词法终结符；
- `P_Q` 等于机器文件的 `productions`，每条产生式有稳定 `id`，并绑定到一个契约 `feature_family`；
- `SchemaCatalog`、`Scope`、`Obligation`、`WitnessWorld`、`Backend`、`ExecResult`、`Verdict` 是语义流对象，不属于 CFG 符号；
- `Submission`、`Query` 和 `SchemaText` 的入口形状由同一机器文件固定，不能因 parser 能读到额外节点而扩大。

输入语言是查询对和 schema，而不是单条 SQL：

```text
Submission = Query x Query x SchemaText
```

CFG 只定义句法。当前 flow 进一步定义三个能力域：

```text
L_struct = { q in L(G_Q) |
             Parse(q) is defined
             and IR(q) is defined
             and ASTDiff(q) is representable }

L_scope = { <q,S> in L_struct x Schema |
            Scope(q,S) is defined }

L_exec = { <q_s,q_c,S> |
           q_s,q_c in L_struct
           and Scope(q_s,S), Scope(q_c,S) are executable
           and Witness(q_s,q_c,S) is non-empty
           and compatible backend is available }
```

因此：

```text
L(G_Q)  = 句法可产生范围
L_struct = 结构 IR 和 ASTDiff 可定义范围
L_scope  = schema scope 可解析范围
L_exec   = 可造数、可执行、可比较范围
```

`L_exec` 不是 CFG 语言，而是句法、schema、方言、witness、执行器和资源约束共同定义的部分函数域。机器文件中的 `parser_predicate`、`structural_predicate` 和 `semantic_predicate` 与当前实现契约及生成的 `phase1_current_formalization.json` 保持一致；CFG 接受不自动推出 `SCHEMA/WITNESS/ENGINE/RESOURCE/VERDICT` 支持。

当前 flow 的核心部分函数为：

```text
P(q)                : Query -> AST | ParseError
I(ast)              : AST -> SQLStructureIR | IRBoundary
Delta(ir_s, ir_c)   : IR x IR -> DiffGraph
R(diff, S)          : DiffGraph x Schema -> Obligation*
W(q_s,q_c,S,O)      : Query x Query x Schema x Obligation* -> WitnessWorld* | WitnessGap
E(q,w,b)             : Query x WitnessWorld x Backend -> ExecResult | EngineGap
M(q_s,q_c,w,D)       : Query x Query x WitnessWorld x DiffGraph -> MutationEvidence
Phi(E_AST,E_data,E_MUT) : Evidence* -> AttributionResult
```

`R` 是 TacticRegistry 与 obligation planner 的抽象；`E_AST` 是 DiffGraph，`E_data` 是沙盒输出和 validator 证据，`E_MUT` 是 mutant 执行和修复证据。解析失败直接返回语法错误，不进入数据等价比较。

## 0.1 Flow 结果分类

对 `x = <q_s,q_c,S>`，当前边界按以下优先级分类：

```text
SyntaxError
  iff P(q_s) or P(q_c) 未定义

InputGap
  iff 解析成功，但 schema scope 无法解析，
  或物理表/列缺失、schema 不可 replay

EngineGap
  iff 解析和 IR 成功，但所需方言/执行后端不可用

Undecided
  iff 执行条件满足，但没有有效 witness 区分，
  且没有可信等价规则

NotEquivalent
  iff 存在有效 witness w，使 E(q_s,w) != E(q_c,w)

Equivalent
  iff 可信等价规则成立，或声明的 bounded obligation worlds 全部一致
```

最后一项是 bounded validation 结论，不是任意关系数据库上的形式化等价定理。有限 witness 可以证明观察到差异，但不能证明所有数据库上等价。

## 0.2 Flow 节点契约


| 节点                  | 输入                                        | 输出                             | 当前能力边界                                                             |
| ------------------- | ----------------------------------------- | ------------------------------ | ------------------------------------------------------------------ |
| `I_INPUT`           | `student_sql`、`correct_sql`、`schema_text` | `Submission`                   | 两条 SQL 和 schema 是同一任务上下文；缺失 schema 不会自动变成可执行任务                     |
| `T_AST_PARSE`       | 一条 SQL                                    | AST 或 `ParseError`             | 只接受严格单条查询；解析失败立即进入 `O_SYNTAX_ERR`                                  |
| `T_IR_BUILD`        | AST                                       | `SQLStructureIR` 或结构边界         | typed IR 只保留当前字段；宽松 parser 能读到但 IR 无法表达的结构不能当作完整支持                 |
| `T_DIFF_ENGINE`     | Standard/Student IR                       | `ASTDiffNode[]`、DiffGraph      | 只对可表达的结构差异生成节点；结构差异不等于数据语义差异                                       |
| `T_TACTIC_REGISTRY` | DiffGraph、schema 元数据                      | obligation、tactic、validator 选择 | 没有适用 tactic 或 obligation 绑定时，不能声称完成 witness 验证                     |
| `T_DATA_GEN`        | schema scope、obligation、tactic            | `WitnessWorld[]`、`E_db`        | 需要物理表/列可解析、约束可满足、行数和资源界限可满足                                        |
| `T_SANDBOX`         | 查询对、WitnessWorld、backend                  | `E_data`、执行状态                  | SQLite 或声明的原生 backend 不可执行时输出 `ENGINE_GAP`；schema 缺失输出 `INPUT_GAP` |
| `T_MUTATION`        | DiffGraph、查询对、执行世界                        | `E_MUT`、repair evidence        | 只在有可定位 diff、可构造 mutant 且变体可执行时形成完整 mutation 证据                     |
| `T_ATTRIBUTION`     | `E_AST`、`E_data`、`E_MUT`                  | `AttributionResult`            | 可在执行边界上输出结构归因，但不能用结构归因替代等价/非等价语义结论                                 |


因此，某个 SQL 产生式进入的最深流程位置可以不同：

```text
L(G_Q) \ L_struct       -> ParseError 或 IRBoundary
L_struct \ L_scope      -> 结构证据可有，schema scope 停止
L_scope \ L_exec        -> ASTDiff/Attribution 可有，Data 阶段为 INPUT_GAP/ENGINE_GAP
L_exec                  -> 可进入 witness、sandbox 和条件性的 mutation 链
```

这一区分是当前能力边界的定义核心：CFG 说明“能否进入解析/结构域”，而 flow contract 说明“能否继续进入造数、执行、mutation 和归因域”。

## 1. 当前总体状态

开发快照 ID：


| 项目                      | 结果                     |
| ----------------------- | ---------------------- |
| 全部题目家族                  | 57,852                 |
| train / public / hidden | 40,518 / 8,801 / 8,533 |
| 公开开发集家族                 | 49,319                 |
| 核心能力类别                  | 12 类，均至少 300 个开发集家族    |
| mutation 家族             | 47,902                 |
| mutation 覆盖率            | 97.13%                 |
| 等价控制覆盖率                 | 92.60%                 |
| Gold Oracle 评估对         | 6,613                  |
| ASTDiff-obligation 绑定   | 6,888/6,888，100%       |
| production chain 完整通过   | 81/151，53.64%          |


Gold Oracle verdict：


| verdict        | 数量    | 统计处理         |
| -------------- | ----- | ------------ |
| NOT_EQUIVALENT | 2,589 | 纳入非等价检出统计    |
| EQUIVALENT     | 2,958 | 纳入等价控制统计     |
| UNDECIDED      | 491   | 单独统计，不进正确率分母 |
| ENGINE_GAP     | 573   | 单独统计，不进正确率分母 |
| INPUT_GAP      | 2     | 单独统计，不进正确率分母 |


Oracle 使用 10 个 seed、row scale 4/8/16，单表最多 32 行。非等价检出 Wilson 95% 下界约 99.8518%；等价控制下界约 99.8703%，低于 99.9%，不能宣称正式统计验收达标。

最终 hidden freeze 尚未执行。hidden 只在一次性 split leakage audit 中读取哈希/键检查分区完整性，未作为优化、Gold Oracle 或 production-chain 输入。







## 2. 第一阶段已实现能力



### 2.1 语料、分区和能力矩阵

- 统一接入教材、课程网站、题库、GitHub、Spider、WikiSQL 等来源记录。
- 保存 URL、抓取日期、原始文本或 SQL fallback、schema、方言、类别、来源状态和哈希。
- 按显式 lineage 或 normalized SQL + schema 生成稳定题目家族 ID并去重。
- 确定性生成 train、public、hidden 分区。
- 12 个核心类别均达到至少 300 个开发集家族。
- 矩阵还记录方言、来源、replay eligibility、scenario candidate 和 observed scenario。
- null、empty_result、duplicate_candidate、boundary_candidate、mutation_ready、paired_mutation、schema_constraint 的 observed 证据尚未在所有类别达到目标。

### 2.2 解析、结构 IR 和 ASTDiff

- 支持单条查询的严格解析，区分 syntax rejection、语义不可判定和执行器边界。
- 将 SELECT、谓词、JOIN、聚合、窗口、CTE、集合操作、子查询和部分方言结构映射为结构 IR。
- 输出 clause、diff type、knowledge point、severity、confidence 和证据来源。
- Gold Oracle 审计中 ASTDiff 与 obligation 绑定率为 100%。
- 对结构可识别但 SQLite 不能执行的结构保留 ENGINE_GAP，不伪造原生执行结果。

### Gold Oracle 是一个独立的“标准答案判定器”，用来判断两条 SQL 是否等价，避免系统只依赖自己的 witness generator 或当前判题逻辑。                                                                                                                                                                                                                       

  它的基本流程是：                                                                                                                                                                                                                        

  标准 SQL + 学生 SQL + schema                                                                                          

          ↓                                                                                                             

  生成独立测试数据库                                                                                                    

          ↓                                                                                                             

  分别执行两条 SQL                                                                                                      

          ↓                                                                                                             

  比较结果、列、重复值和顺序                                                                                            

          ↓                                                                                                             

  输出独立 verdict                                                                                                                                                                                                                      

  当前支持五类结果：                                                                                                                                                                                                   

  - NOT_EQUIVALENT：找到一个数据库，使两条 SQL 结果不同。                                                               

  - EQUIVALENT：有可信的等价控制或规则支持。                                                                            

  - UNDECIDED：没有找到反例，但也没有足够证据证明等价。                                                                 

  - ENGINE_GAP：需要的方言或数据库执行器不可用。                                                                        

  - INPUT_GAP：schema、物理表或查询无法重放。



### 2.3 Witness、validator、mutation 和 attribution

- 根据比较边界、布尔逻辑、NULL 三值逻辑、JOIN 键漂移、外连接 dangling row、分组粒度、排序键、窗口 partition/order 等 obligation 生成 bounded witness。
- 支持 compact schema 和部分 schema catalog；使用主键、外键、唯一约束等信息指导造数。
- validator 比较结果行、列名、重复行、顺序和 obligation 证据。
- mutation repair 可用标准子句替换或删除检查学生错误是否修复。
- attribution 将 AST、mutation 和执行证据绑定到 knowledge point，输出 syntax、logical、semantic、lacking 等错误类型。

 ASTDiff

    -> Witness

    -> Validator

    -> Mutation

    -> Attribution

  ### 1. Witness

  Witness 是一个专门构造出来的数据库实例，用来把两条 SQL 的语义差异“激活”出来。

  例如：

  -- 标准

  SELECT * FROM users WHERE age > 18;

  -- 学生

  SELECT * FROM users WHERE age >= 18;

  Witness 会构造：

  users:

  age = 18

  age = 20

  这样标准 SQL 不返回 18，学生 SQL 返回 18，差异被观察到。

  Witness 不是普通随机数据，而是针对 obligation 定向构造的测试世界，例如：

  - 比较边界：构造等于阈值的行；

  - JOIN：构造匹配行和未匹配行；

  - NULL：构造 NULL 和非 NULL；

  - GROUP BY：构造重复值和分组粒度差异；

  - ORDER BY：构造排序相同的 tie；

  - 窗口函数：构造相同 partition、不同 order 的行。

  ### 2. Validator

  Validator 用来确认 Witness 是否真的满足预期条件，以及执行结果是否支持这个 obligation。

  它检查：

  Witness 是否满足约束

  标准 SQL 和学生 SQL 是否都成功执行

  结果行是否不同

  列名、重复值、顺序是否符合比较规则

  目标 obligation 是否被激活

  例如，某个 JOIN obligation 要求存在“一个匹配父表、一个无匹配子表”的数据。如果生成的数据没有满足这个条件，validator 会

  判定：

  obligation 未激活

  因此，validator 不是简单比较 SQL 输出，而是验证“测试数据确实测试到了目标语义”。

  ### 3. Mutation

  Mutation 是对学生 SQL 做定向变异或修复，再重新执行，用来验证差异是否确实来自某个结构。

  例如：

  -- 学生 SQL

  SELECT * FROM users WHERE age >= 18;

  -- mutation repair

  SELECT * FROM users WHERE age > 18;

  如果替换 >= 为 > 后结果与标准 SQL 一致，就说明：

  比较操作符是实际错误来源

  Mutation 主要用于：

  - 删除或替换 WHERE 条件；

  - 修复错误 JOIN 列；

  - 把 INNER JOIN 修复为 LEFT JOIN；

  - 恢复被删除的 GROUP BY 列；

  - 修复 HAVING 阈值；

  - 恢复 DISTINCT；

  - 修复 ORDER、LIMIT、窗口 partition/order；

  - 修复 CASE 分支或递归终止条件。

  ### 4. Attribution

  Attribution 是最终归因器，把多种证据合并成学生错误诊断：

  E_AST   = AST 结构差异

  E_data  = Witness 执行结果差异

  E_MUT   = Mutation 修复证据

  然后输出类似：

  knowledge_point: where-comparison

  clause: WHERE

  error_type: logical

  detail: 使用 >=，应为 >

  confidence: 0.96

  evidence:

    - ASTDiff: comparison operator changed

    - Witness: boundary row produces different result

    - Mutation: replacing >= with > restores agreement



### 2.4 Gold Oracle 和统计

- Gold Oracle 使用独立审计入口执行公开开发评估对。
- 固定输出 EQUIVALENT、NOT_EQUIVALENT、UNDECIDED、ENGINE_GAP、INPUT_GAP。
- UNDECIDED、ENGINE_GAP、INPUT_GAP 不进入正确率分母。
- 原生 MySQL、PostgreSQL、SQL Server/T-SQL、Oracle runner 未全部配置；无原生证据的样本保持 ENGINE_GAP。
- production chain 分层抽样的完整通过为 81/151；validator 激活、执行差异、targeted mutation repair 和 attribution 仍有失败。



## 3. 功能范围和边界

“结构支持”表示可以解析、构造 IR、做 ASTDiff 或归因；“可执行”表示当前 bounded SQLite 兼容路径有实际执行比较。


| 能力                   | 当前范围                                                                                                     | 边界                                    |
| -------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| SELECT/projection    | 列、限定列、星号、别名、表达式、算术、CAST、字面量                                                                              | 受 schema 和 SQLite 类型语义约束              |
| WHERE/三值逻辑           | AND、OR、NOT、比较、括号、NULL 谓词                                                                                 | NULL 需要专门 witness                     |
| IN/BETWEEN/LIKE      | 值列表、子查询、NOT IN、BETWEEN、LIKE、ESCAPE                                                                       | NOT IN 的 NULL trap 单独处理               |
| JOIN                 | comma/cross、inner、left、right、full、natural、using、on、自连接、多表                                                | 外连接依赖 dangling-row world              |
| GROUP/HAVING/聚合      | COUNT、SUM、AVG、MIN、MAX、GROUP BY、HAVING、FILTER                                                             | ROLLUP/CUBE/GROUPING SETS 是执行边界       |
| DISTINCT/ORDER/LIMIT | DISTINCT、DISTINCT ON 结构、ASC/DESC、NULLS、ordinal、alias、LIMIT/OFFSET、FETCH、TOP                              | vendor 形式可能只做转写或结构验证                  |
| 集合操作                 | UNION、UNION ALL、INTERSECT、EXCEPT、三分支链                                                                    | INTERSECT ALL/EXCEPT ALL 是 SQLite gap |
| 子查询                  | scalar、IN、EXISTS、相关/嵌套子查询、ANY/ALL/SOME                                                                   | 依赖相关列、空结果和 NULL world                 |
| CTE                  | 单/多 CTE、依赖链、递归 UNION/UNION ALL                                                                           | SEARCH/CYCLE 主要是结构/方言边界               |
| CASE                 | simple/searched CASE、WHEN/THEN、ELSE                                                                      | 可执行，支持分支 mutation                     |
| 窗口                   | ROW_NUMBER、RANK、DENSE_RANK、NTILE、LAG、LEAD、FIRST_VALUE、LAST_VALUE、聚合窗口、partition/order/frame、named window | tie、frame 和顺序需显式约束                    |
| 方言特性                 | MySQL、PostgreSQL、SQLite、T-SQL 识别及部分转写；Oracle 作为识别/边界目标                                                   | 不能宣称所有方言原生执行                          |
| 结构扩展                 | GROUPING SETS、ROLLUP、CUBE、LATERAL、QUALIFY、FILTER、DISTINCT ON、IS DISTINCT FROM、SEARCH/CYCLE               | 部分 typed IR/ASTDiff 已支持，执行状态单独标注      |


最新 150 条 CFG fragment attack 中有 148 条 supported、2 条 engine gap，支持率 98.67%。这是有限攻击集实证，不是整个 CFG 的形式化覆盖率。



4.方言           当前支持                                                                                              

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                      

   Generic SQL    主要教学 SQL 主路径                                                                                   

  ─────────────  ──────────────────────────────────────────────────────────                                             

   SQLite         当前主要 bounded 执行后端                                                                             

  ─────────────  ──────────────────────────────────────────────────────────                                             

   MySQL          可识别、解析、部分转写，如反引号、LIMIT offset,count                                                  

  ─────────────  ──────────────────────────────────────────────────────────                                             

   PostgreSQL     可识别、解析部分特性，如 ::、ILIKE、DISTINCT ON                                                       

  ─────────────  ──────────────────────────────────────────────────────────                                             

   T-SQL          可识别、解析部分特性，如 TOP、方括号标识符                                                            

  ─────────────  ──────────────────────────────────────────────────────────                                             

   Oracle         可识别部分语法或作为边界记录，但当前没有完整原生执行验证                                              

                                                                                                                        

  当前支持分为四层：                                                                                                                                                                                                                      

  方言识别                                                                                                              

    -> 方言解析                                                                                                         

    -> IR / ASTDiff                                                                                                     

    -> 原生执行验证                                                                                                     

                                                                                                                        

  前 3 层已经覆盖一部分 MySQL、PostgreSQL、SQLite、T-SQL 特性；最后一层目前主要依赖 SQLite bounded compatibility        

  execution。没有配置对应 MySQL/PostgreSQL/T-SQL/Oracle 原生 runner 时，不能把 SQLite 结果称为原生方言结果，而应标记为： ENGINE_GAP



Docker本地作为开发测试：

层次               是否依赖 Docker      说明                                                                         

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

   方言识别与解析     否                   由 SQLGlot 完成，支持部分 MySQL、PostgreSQL、SQLite、T-SQL、Oracle 语法      

 ──────────────────────────────────────────────────────────────────────────────

   SQLite 兼容执行    否                   使用本地 SQLite，当前开发 Gold Oracle 主要走这条路径                         

──────────────────────────────────────────────────────────────────────────────

   原生方言执行       Docker 不是硬依赖    通过 Python 驱动连接真实数据库，可连接 Docker 数据库，也可连接宿主机或远程数 

                                           据库                                                                         

                                                                                                                        

  原生 runner 使用的驱动大致是：                                                                                                                                                                                              

  MySQL       -> pymysql                                                                                                

  PostgreSQL  -> psycopg / psycopg2                                                                                     

  T-SQL       -> pyodbc + ODBC Driver 18                                                                                

  Oracle      -> oracledb
