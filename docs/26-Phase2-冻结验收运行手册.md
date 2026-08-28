# Phase 2 冻结验收运行手册

Phase 2 的冻结结论由受限验收脚本生成，不依赖网络、大模型、外部数据库或大语料。它只检查 `phase2.rules.mvp20.v1` 声明的 20 条规则、证据契约和公开安全边界，不将结论扩大为对任意 SQL 的全局完备性证明。

最终状态是 `PHASE2_MVP_ACCEPTED / PHASE1_GLOBAL_ACCEPTANCE_OPEN`。这两个状态必须同时保留：Phase 1 v16 仍有 22 个确定性标签 mismatch 与 7 个 parser/input gap，冻结文件明确记录 `acceptance.pass=false`。Phase 2 的 PASS 不能改写或替代这个 Phase 1 结论。

## 运行方式

在项目根目录执行：

```bash
/home/roge/miniconda3/envs/my_new_env/bin/python \
  sql-edu-backend/scripts/run_phase2_acceptance.py \
  --output data_construct_test/outputs/phase2_acceptance_report.json
```

脚本默认对每个 pytest 子进程设置 4096 MiB 地址空间上限、240 秒单命令超时、8 MiB 输出文件上限，并将常见数值库线程数固定为 1。在当前 24 GB WSL 2 基座上，这个配额不会触发大模型级别的瞬时内存申请。

权威报告写入 `data_construct_test/outputs/phase2_acceptance_report.json`。脚本使用临时文件 + `os.replace` 原子替换，并将报告权限设为 `0600`。不需要、也不应在代码中配置 `HTTP_PROXY`。

## 验收边界

默认白名单包含：

- Phase 1→2 的有界 scope contract，以及双侧 scope 到 conceptual scope 的精确配对；
- SchemaCatalog 规范化与权威关系事实；
- scoped query graph、side-aware scope、精确 conceptual binding 及显式 parent/CTE/derived/correlation/set 作用域边；
- 20 条规则的正例、邻近反例、证据不足三态矩阵；
- 诊断包、显式因果依赖、FDP/secondary/suppressed/unresolved 分流、Minimal Witness/QSS/三段式教学链和去答案泄露合约；
- 公开题目 DTO、Schema Preview 清洗、rich verdict 路由安全与资源门控的纯测试。

具体测试目标由 `run_phase2_acceptance.py` 内的静态 `TEST_GROUPS` 白名单决定。脚本还会直接比对规则目录和矩阵，要求两者均恰好包含 20 个不重复 rule ID。

规则矩阵对关键证据门禁 fail closed：Missing Bridge 需声明 FK 桥路径；WHERE→HAVING 需同 scope 同 predicate 的 `where_changed + having_changed`；fan-out 需 `aggregate_distinct_changed` 且有声明 1:N 或直接行/聚合 delta；OFFSET 通过 Phase 1 `limit_changed` 适配；NULL 规则接受 `null_predicate_negation_changed` 或已证明的 missing `IS NULL` branch。不满足这些前置条件时保留 unclassified/unresolved，不为了提高覆盖率而强行命名。

数据库仓储、真实引擎集成以及依赖真实工作线程唤醒的路由执行属于独立的系统验收层，不会被这个纯 Phase 2 门禁隐式跳过或伪装成通过。本门禁保留对超时后容量槽延迟释放的协程级纯测试，但不启动真实 worker thread。

20 条目录之外的 WINDOW/QUALIFY 语义、复杂 set operation、递归 CTE、LATERAL 与高级相关子查询教学规则不在本门禁的“已覆盖”声明中。它们必须被保留为 `EXTENSION/UNCLASSIFIED_SUPPORTED_DIFF` 或证据边界，不得就近套用 MVP 标签。

完整 standard/student 双侧 ScopedQueryGraph 是内部审计证据，不是学生响应契约。公开 ordered pipeline 只能使用已证明精确配对的 side-neutral conceptual scope；无法配对时必须保留 unscoped/`PARTIAL`。

Public Schema Preview 的冻结测试已覆盖 SQL 形态、分隔符编码与自由文本单元格阻断。但若上游 LLM 可任意创造标识符、且这些标识符未与权威 DDL 做交集校验，“非 SQL 形态标识符”仍存在作为隐蔽编码通道的理论风险。因此本报告的 PASS 不宣称形式化零泄漏证明；生产级收紧需要将生成标识符限定为权威 DDL 交集。

## 离线与资源安全策略

- 子进程使用 `shell=False`，单进程串行执行；
- 环境只传递少量白名单变量，不传递凭据；
- 开启 `TRANSFORMERS_OFFLINE` 和 `HF_HUB_OFFLINE`，静态白名单不引用模型加载代码；
- pytest guard 阻断 Internet TCP `connect/connect_ex/create_connection` 与 UDP `sendto`，Unix-domain socket 仅作为本地 IPC 保留；
- 单进程地址空间上限可在 512–8192 MiB 范围内调整，超出范围 fail closed；
- 捕获的 pytest 原始输出不进入 JSON，避免断言信息夹带参考 SQL、变异 SQL 或 hidden 数据。

## 报告判定

JSON 报告的 `result` 只有 `PASS` 和 `FAIL`。仅当以下条件同时成立时返回零退出码：

1. 所有白名单测试文件存在；
2. 规则目录恰好为 20 条，版本和规则 ID 与矩阵完全一致；
3. 每组收集到的测试均实际执行并全部通过，无 skip、xfail 或收集错误；
4. 所有子进程在资源和超时上限内结束。

报告保留命令参数、退出码和测试计数，但故意不保留 pytest 原始输出，以防断言信息夹带标准 SQL、变异 SQL 或隐藏数据。

## 独立系统回归

冻结 runner 的 PASS 不代替数据库路由回归。在合并后应在同样受限的地址空间下分别执行 runner 自身测试、完整 `test_check_sql_flow.py`、广义 API/安全邻接回归和 Phase 1 scope/rich-verdict 相关回归。如果 WSL 的 `aiosqlite` 跨线程 selector 唤醒不可靠，测试 fixture 的本地 heartbeat 只是保持 event loop 轮询，不会改变业务语义。

这些回归的计数应记入 `docs/25-Phase2-Diagnosis-修订实施计划.md` 第 14.8 节，不能混入纯 Phase 2 JSON 报告的 `totals`。

## 本轮冻结记录

| 字段 | 最终值 |
|---|---|
| 报告 `result` | `PASS` |
| `totals.groups_passed / totals.groups` | `7 / 7` |
| `totals.passed / totals.collected` | `169 / 169`（分组 `6+24+18+61+37+18+5`；20/20 规则精确匹配） |
| runner 自身测试 | `15 / 15` |
| 完整 DB 判题路由回归 | `10 / 10` |
| 广义 API/安全邻接回归 | `51 / 51` |
| Phase 1 相关回归 | `640 / 640` |

以上数据来自 2026-08-24 最终受限复跑。权威报告位于 `data_construct_test/outputs/phase2_acceptance_report.json`；它的 PASS 只签发 `PHASE2_MVP_ACCEPTED`，不改写 Phase 1 v16 的 `acceptance.pass=false`。
