# 数据建模规约与流图说明 (Templates)

本目录包含整个 SQL 教学与评测数据集在构建、作答模拟以及结构化传输中的数据格式规约（JSON Schema）、数据样例以及构建流程说明文件。

## 文件清单及说明

### 1. 数据结构定义与规约 (3个)
* **`question_dataset.schema.json`**
  - **作用**：全量标准题库的数据结构校验规约 (JSON Schema)。定义了标准题库每一道题（包括 `id`, `difficulty`, `l1`, `l2` 知识点, `schema`, `correct_sql` 等）的字段类型与必要项。
* **`question_dataset.example.json`**
  - **作用**：标准题库的简单 JSON 样例，便于开发者快速直观了解数据嵌套与分布格式。
* **`student_answer_raw.schema.json`**
  - **作用**：外部 AI 模拟学生作答时的原始 JSON 响应规约。约束了大模型输出包含 `student_sql`、作答思路及微观偏差的 JSON 属性结构。

### 2. 架构设计说明 (1个)
* **`dataset_construction_flow.md`**
  - **作用**：数据集构建与第一阶段（Observe/感知）系统的总体流程说明书，内含完整的 Mermaid 控制流图（闭环 OODA 控制模型中的 Observe 分支机制说明）。

---

## 校验提示
在后续需要扩展题库或重新生成学生模拟作答集时，可以利用 Python 的 `jsonschema` 库基于本目录下的 `.schema.json` 文件对生成的 JSON 结果进行自动化格式核验，确保数据完全符合系统的接口规范。
