# SQLite Phase1 → Phase2 contract

## 输入

- `schema_text`：可信且可重放的紧凑 schema。
- `reference_sql`：教师提供的单条 SQLite 查询。
- `student_sql`：待诊断的单条 SQLite 查询。
- 可选的结构化 `schema_catalog`、题意文本和语言。

任何参考 SQL、schema 或执行器故障都必须投影为 `UNDECIDED`，不得归咎于学生。

输入边界使用 SQLite grammar 严格解析。已知非 SQLite 构造必须在 AST 差异提取和执行之前以 `KNOWN_GAP/UNSUPPORTED` 结束；系统不做方言猜测、方言路由或跨方言降级转换。

## Phase1 输出

Phase1 保留完整的内部证据：AST 差异、obligation、bounded witness suite、原生 SQLite 执行结果、修复干预结果和作用域元数据。确定性的错误结论必须同时满足：

```text
status ∈ {SUPPORTED, SUPPORTED_WITH_LIMITS}
equivalence_conclusion = NOT_EQUIVALENT
judge_status = WRONG
```

`NO_COUNTEREXAMPLE_FOUND` 仅表示当前有界测试未发现反例。

## Phase2 输出

Phase2 不执行 SQL。它依据 Phase1 证据进行分级、候选归并、查询路径排序和 primary error 选择。只有具备足够原子/因果证据的候选才可成为主要诊断。

学生可见接口 `PipelineResult.learner_hint(level)` 每次只披露一个层级：

1. `LOCATION`：描述当前行为及错误所在阶段。
2. `WITNESS`：给出经验证的最小冲突物证。
3. `REFLECTION`：给出不含修复 SQL 的引导问题。

所有层级只围绕同一个 primary error。次级候选、参考 SQL、mutation SQL、完整 witness world 与内部 causal trace 均不得进入学生响应。

## 执行边界

- 固定 `engine=sqlite`，不接受 dialect/backend/connection URL 参数。
- 单查询、内存数据库、行数/world/尝试次数/VM 指令/时间均有硬上界。
- 所有 SQLGlot `read`、`write` 和渲染 dialect 参数均固定为 `sqlite`。
- 兼容公开名称 `transpile_to_sqlite` 只做 SQLite→SQLite 的确定性规范化，不接收源方言。
- 执行连接仅注册有资源上界的 SQLite `REGEXP` 回调，不提供方言兼容 UDF。
- 不加载本地模型，不启动数据库容器，不依赖外部网络。

## 代码边界

- `parseval_data_generator.py` 是小型兼容 facade，不承载实现逻辑。
- Phase1 八层模块形成单向依赖图，任何一层不得反向导入上层，且单文件不得超过 5,000 行。
- `ast_schema.py` 只定义共享 `ASTDiffNode`；Phase2 只消费 Phase1 的结构化证据。
- QUALIFY、LATERAL 等非 SQLite 阶段或边类型不得进入 Phase2 scope graph。
