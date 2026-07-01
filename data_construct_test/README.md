# SQL DQL 数据集构建与评测验证方案 (data_construct_test)

本目录是 SQL 智能教学系统 **阶段一（Observe：证据采集与感知）** 的核心数据集构建、模拟作答生成以及评测完备性校验的独立测试工程。目前，全量 PDF 抽题、四类学生作答模拟以及 20 道初始诊断题目的筛选工作已全部完成。

为了保持工作区的极致清爽，所有大体量的中间缓存与图表均已清理归档，目录内仅保留了最终的核心数据集资产、数学规约与评测脚本。

---

## 目录结构索引

本测试工程划分为以下四个核心子目录，各自配有详细说明文档：

1. **[`outputs/`](./outputs/README.md) — 数据集输出目录**
   - 包含最终生成的全量标准题库（`data_std_full.json`）、多画像模拟学生作答集（`data_student_full.json`）以及核心 20 道初始诊断题（`initial_diagnostic_20.json`）。
2. **[`scripts/`](./scripts/README.md) — 构建与评测验证脚本**
   - 包含从零构建数据集、LLM 作答模拟、诊断题自动筛选，以及 **16类经典 SQL 算子评测** 和 **23项 ParSEval 动态造数策略完备性检验** 的核心运行脚本。
3. **[`templates/`](./templates/README.md) — 数据规约与设计规约**
   - 包含用于核验数据集格式规范的 JSON Schemas，以及系统感知层的 Mermaid 控制流设计流图。
4. **[`prompts/`](./prompts/README.md) — 提示词资产**
   - 包含供外部大模型进行学生作答模拟与 PDF 题目抽取的 Prompt 工程设计文档。

---

## 核心实现说明

1. **ParSEval 动态造数完备性 (23种造数策略)**
   - 通过静态解析 DQL 语句，自动在内存沙盒数据库中生成符合数值三态边界（$c$, $c+1$, $c-1$）、外连接悬浮元组、GROUP BY 分组多样性、HAVING 聚合三态值等多维度的高质量评测数据集。
2. **变分隔离测试机制 (Mutation Testing)**
   - 系统支持单变量变分。通过将学生 SQL 中的错漏子句（如 `WHERE`、`JOIN ON`、`HAVING` 等）替换为标答子句，在沙盒中执行比对。若 Mutant-SQL 执行结果变为正确，则在实验上证明该算子错误是导致不匹配的充分必要原因，实现细粒度错因的精准定位。
3. **安全沙盒熔断守护**
   - 在沙盒内执行 DQL 时，限制最大执行 VM 指令上限为 10 万周期，彻底规避了递归 CTE 题目和学生错误死循环查询挂死系统的高危风险。

---

## 快速开始

在项目根目录下，您可以直接运行完备性测试集：

```bash
# 运行 16 类经典 SQL 算子感知判题测试
python data_construct_test/scripts/run_all_operator_tests.py

# 运行 23 种动态造数策略的数学边界完备性检验
python data_construct_test/scripts/run_generation_completeness_tests.py
```
