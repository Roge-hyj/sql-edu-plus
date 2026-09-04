# sql-edu-plus

这是论文收敛后的最小研究实现：**单一 SQLite 执行语义的 Phase1 + Phase2**。

它回答的核心问题是：给定可信数据库模式、参考 SQL 和学生 SQL，如何生成紧凑且可执行的区分性数据库实例，验证并定位语义偏差，再把证据转换成不泄露参考答案的提示。

## 完整链路

```text
Schema + reference SQL + student SQL
                  │
                  ▼
AST diff → obligation → bounded witness worlds
                  │
                  ▼
SQLite execution → behavioral conflict → repair verification
                  │
                  ▼
bounded Phase1 verdict → Phase2 evidence grading/ranking
                  │
                  ▼
primary error → minimal witness → one progressive safe hint
```

Phase1 没有反例时只给出 `NO_COUNTEREXAMPLE_FOUND`，不声称证明了全局 SQL 等价。Phase2 只消费 Phase1 已验证的证据，不重新判定等价性。

## 保留范围

- Phase1：`ast_schema.py`、`parseval_data_generator.py`、`phase1_verdict.py` 与 `witness_generation/`。
- Phase2：`error_diagnosis.py`、`phase2_schema_catalog.py`、`scoped_query_graph.py`。
- 最小编排入口：`pipeline.py`。
- SQLite-only 回归、Phase2 规则、查询作用域、schema catalog、witness validator 测试。

本仓库不包含原生 MySQL/PostgreSQL/T-SQL/Oracle 执行器、多方言路由、学生历史/BKT、Phase3–5、Web 前端、LLM 服务、实验大输出或数据库驱动。

## 安装与验证

需要 Python 3.11 或 3.12：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cd sql-edu-backend
pytest
```

最小调用：

```python
from core.pipeline import run_pipeline

result = run_pipeline(
    schema_text="students(id INTEGER PRIMARY KEY, score INTEGER)",
    reference_sql="SELECT id FROM students WHERE score >= 60",
    student_sql="SELECT id FROM students WHERE score > 60",
    question="找出所有及格学生",
)

# level=1 定位，level=2 最小物证，level=3 反思问题；每次只返回一级。
learner_payload = result.learner_hint(level=1)
```

`result.phase1` 和 `result.phase2.to_internal_dict()` 只用于服务器端审计。面向学生时仅序列化 `learner_hint()`。

## 资源与安全边界

- 最多 8 个 witness worlds、每个 world 最多 8 次尝试、每表最多 32 行。
- SQLite VM 指令预算为 1,000,000，单次执行时间预算为 0.5 秒。
- 只接受单条查询，执行数据库位于内存中；非 SQLite 后端调用会直接失败。
- learner payload 不包含参考 SQL、修复 SQL、完整 witness 数据库或次级候选链。

详细接口约束见 [`contracts/phase12-contract.md`](contracts/phase12-contract.md)。

