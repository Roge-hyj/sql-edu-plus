# SQLite Phase1 + Phase2 全链路回归集

本目录保存经过当前实现重新执行验证的精简数据，而不是把历史 `outputs/` 整包复制进来。

## 文件

- `cases/sqlite_phase12_verified.json`：79 条去重后的输入与验收约束。
- `run_full_pipeline_eval.py`：从公开入口 `run_pipeline` 执行完整链路的独立评测器。
- `baselines/sqlite_phase12_baseline.json`：双次全量运行产生的当前基线，不包含完整 witness 数据库或参考 SQL 副本。

## 数据构成

| 子集 | 数量 | 作用 |
| --- | ---: | --- |
| `teaching_core` | 34 | 18 个当前能够完成 witness、修复验证和精确 primary 定位的教学规则 |
| `phase1_operator` | 19 | 每个已保留 Phase1 SQL 操作族各一条结构回归 |
| `equivalent_control` | 10 | 10 种等价改写，禁止误报为不等价 |
| `public_reference_mutation` | 12 | 来自公开链接的参考查询与确定性生成的学生 SQL 变体 |
| `fail_closed` | 4 | 多语句、DELETE、PRAGMA 和语法错误参考查询必须安全退化 |

公开来源子集不是“真实学生答案数据”。它只使用公开参考查询增加 schema 和查询结构多样性，学生 SQL 均标记为 `deterministic_mutation`。入选查询的参考侧和学生侧都经过原生 SQLite 编译检查。

## 选择与验收原则

数据按 schema、参考 SQL、学生 SQL 三元组精确去重。诊断样本必须同时满足：

1. Phase1 在 SQLite 下执行并得到 `NOT_EQUIVALENT`；
2. 生成的诊断数据库确实区分两条查询；
3. 受限替换修复至少一次消除输出冲突；
4. Phase2 生成 primary，并达到 `REPAIR_VERIFIED` 或更强证据等级；
5. Phase2 能安全公开物理行 witness 或结果差异；
6. 三层 learner hint 均不包含参考 SQL、修复 SQL、完整数据库或内部候选链；
7. 使用新的内存 SQLite 连接独立重放 Phase1 witness，结果与链路记录一致。

等价对照允许保守返回 `UNDECIDED`，但绝不允许返回 `NOT_EQUIVALENT`。安全输入必须返回 `UNDECIDED` 且不得生成错误归因。

## 当前基线

基线环境为 Python 3.11.14、SQLite 3.51.2、SQLGlot 29.0.1。双次全量运行结果：

- 79/79 条验收通过，158 次完整 pipeline 调用；
- 65/65 条语义差异生成并执行了区分性 Phase1 witness；
- 75/75 个可执行案例通过独立 SQLite 重放；
- 474/474 个分级提示载荷通过泄漏检查；
- 0 个确定性差异，单表最多 12 行，总生成数据最多 24 行；
- 峰值常驻内存约 51 MB，无 Swap 使用。

Phase2 当前只有 7 条能够安全绑定到最小物理输入行；其余 58 条错误案例公开经过验证的 `result_delta`。这不影响 Phase1 witness 数据库已经区分查询，但说明“最小物理行提示”仍是后续要提高的能力，不能把 65 条全部表述为最小行级 witness。

## 已知未纳入规则

- `S1_MISSING_BRIDGE`：候选能够被检测，但目前不稳定地排在 primary。
- `S5_FANOUT_AGGREGATE`：定向样本尚未生成有效区分性 witness。

这两项保留为能力边界，不计入“已完整支持的 18 个规则”。

## 运行

从仓库根目录、激活开发环境后执行：

```bash
python evaluation/run_full_pipeline_eval.py \
  --repeat 2 \
  --output /tmp/sqlite_phase12_evaluation.json
```

任一案例不满足声明约束或两次运行结果摘要不一致时，评测器以非零状态退出。评测过程最多接受 500 条、2 MiB 数据文件，每表硬限制为 32 行，独立重放最多读取 1,024 行，并同时设置 SQLite 指令与时间预算。

## 使用边界

这是从历史生成数据和公开参考查询中筛选出来的回归集，适合复现实现能力和防止退化，不是独立、无偏的论文效果评测集。论文中的最终成功率仍应在冻结实现之后，使用未参与开发和筛选的 holdout 数据重新测量。
