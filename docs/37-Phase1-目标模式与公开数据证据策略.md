# Phase 1 目标模式与公开数据证据策略

版本：v1（2026-08-24）

本文定义产品希望最终达到的目标，不把目标状态冒充为当前已完成状态。当前真实状态仍以 `contracts/phase1_current_implementation.json`、`docs/35-Phase1-真实代码能力盘点.md` 和 v16 冻结报告为准；目标机器契约位于 `contracts/phase1_product_target.json`。

## 1. 两份契约并存

| 文件 | 作用 |
| --- | --- |
| `phase1_current_implementation.json` | 记录当前代码和当前证据真正支持到哪里。 |
| `phase1_product_target.json` | 记录产品最终希望验证到哪里，以及达到该状态所需门禁。 |

目标契约不会覆盖当前契约，也不会把 `TARGET` 直接改成 `VERIFIED`。每项能力必须沿着：

```text
TARGET → IMPLEMENTED → VERIFIED
```

推进。`ENGINE_GAP` 和 `UNDECIDED` 必须保留真实原因，不能通过改分母或删除样本消失。

## 2. 每项能力的实现顺序

对一个 SQL 能力逐项完成：

```text
Parser/CFG
→ IR/ASTDiff
→ schema qualification/replay
→ witness obligation/planner/validator
→ compatible/native executor
→ query and task resource limits
→ rich verdict and API mapping
→ public regression
→ independent evaluation slices
→ new hidden freeze
```

例如，窗口函数解析成功不等于窗口函数已支持。必须同时证明作用域和 frame 能被 IR 表达、tie/NULL 边界能生成 witness、声明版本的原生引擎可执行、超时和结果预算有效，并且 verdict 状态不会把 `ENGINE_GAP` 当成学生错误。

## 3. 验收提升规则

### `IMPLEMENTED`

至少需要：代码入口、自动化测试和可复现公开证据。它只能说明“这条代码路径在受限输入下能运行”。

### `VERIFIED`

还必须满足：

- generation failures 为 0；
- determinate label mismatches 为 0；
- 两次运行结果稳定；
- `acceptance.pass=true`；
- source、时间、方言、schema、feature 和 property-based slices 均有报告；
- 运行时版本、资源边界和引擎版本已指纹化；
- 优化过程没有读取 hidden 分区。

有限数据未发现反例，仍不能写成任意 SQL 的全局等价证明。

## 4. 公开网络数据如何进入证据链

可以继续从网络获取真实 SQL，但“抓到了数据”不等于“数据可以用于验收”。现有入口：

- `data_construct_test/scripts/collect_web_sql_corpus.py`：按 manifest 下载、缓存和标准化公开语料；
- `data_construct_test/scripts/build_phase1_corpus_universe.py`：按 question family 分层并生成 train/public/hidden；
- `data_construct_test/scripts/audit_phase1_split_leakage.py`：审计 family、schema、SQL 和 mutation lineage 泄漏；
- `data_construct_test/scripts/run_phase1_freeze_verification.py`：只在冻结阶段消费 hidden。

公开语料的 mutation/parse 构建也必须有独立的资源门禁：在线 SQL smoke builder 在 POSIX/WSL 中对候选变换和再解析设置硬超时，逐案 evaluator 使用可杀 child；解析异常或超时只可作为构建层跳过或 `RESOURCE_LIMIT/UNDECIDED` 记录，不能阻塞整个快照，也不能被计入产品的 `ENGINE_GAP` 或学生错误。公开 v2 smoke 证据保存在 `data_construct_test/outputs/online_random250_structure_generation_report_v2.json`。

新增来源至少要记录：来源 ID、URL 或归档 ID、抓取时间、许可/条款说明、原始内容 SHA-256、抽取方法、方言来源、schema 来源和偏差风险。没有可信 schema 的记录可以作为结构参考，但不能支撑语义等价验收。

采集必须遵守 robots.txt、网站条款、许可证、速率限制和署名要求；优先使用官方 API、仓库 raw 文件或公开归档。不得采集凭据、私密数据或未经授权的用户 SQL。原始响应要不可变缓存，变更 parser 或来源后重新生成快照，而不是覆盖旧证据。

## 5. 防止过拟合的切分

不能按单行随机切分，因为同一题目的 SQL 改写、同一 schema 的变体和同一 mutation lineage 会泄漏到不同分区。切分单位必须至少包含：

```text
question_family + schema_digest + mutation_lineage
```

除 train/public/hidden 外，每次验收还要报告：

- source holdout：整来源留出；
- temporal holdout：较新的抓取时间留出；
- dialect holdout：方言或版本留出；
- schema holdout：未见过的 schema 留出；
- feature holdout：特性组合留出；
- property-based synthetic：脱离网络语料的组合和边界生成。

优化只能使用 train、public、批准的外部快照、公开手工 fixture 和 property-based synthetic。hidden 只能在代码和 public 证据冻结后生成并评估，不能用于定向修复同一版本。

## 6. 形式化的生成时机

实现过程中先维护最小可执行约束：输入边界、方言/版本、资源预算、禁止操作和 verdict 状态。能力完成并通过冻结后，再从同一机器契约生成：

```text
CFG + parser constraints + IR constraints
+ schema constraints + witness constraints
+ engine constraints + resource constraints
```

生成结果必须能回答：输入为何进入 PolicyScope、为何未进入 RunnableScope、卡在 parser/schema/witness/engine/resource 哪一层，以及为何最终是 `EQUIVALENT`、`NOT_EQUIVALENT`、`UNDECIDED` 或 `ENGINE_GAP`。
