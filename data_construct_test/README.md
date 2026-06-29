# SQL DQL 数据集构建方案

本目录用于构建新的大规模 SQL DQL 题库和后续学生作答数据。

## 目录结构

- `source_pdfs/`: 三本教材 PDF 原始来源。
- `reference/`: SQL 知识点初步划分图片等参考材料。
- `prompts/`: 给外部 AI、Figma/FigJam 使用的提示词。
- `templates/`: 数据结构模板和字段规范。
- `outputs/`: 后续抽取题目、学生作答、判分整理后的输出目录。

## 构建目标

从 `source_pdfs/` 中三本 PDF 提取所有 SQL DQL 相关题目，按教材知识点出现顺序组织成一个大的标准题库 JSON。题目需要统一包含：

- `id`: 全局递增题号。
- `difficulty`: 1.0 到 10.0 的难度值，由题目复杂度、SQL 结构和知识点数量综合评估。
- `l1`: 一级知识点。
- `l2`: 二级原子知识点数组。
- `schema`: 题目涉及的表结构。
- `q`: 题目文本。
- `ans_sql`: 标准答案 SQL。
- `source`: 题目来源，至少包含教材名、章节或页码。

## 总体流程

1. 读取三本 PDF，并定位 SQL DQL 章节、例题、练习题、课后题。
2. 只保留 DQL 查询题，排除 DDL、DML、DCL、TCL、纯概念题和数据库设计题。
3. 按教材知识点出现顺序整理题目，而不是按抽取时间排序。
4. 使用 `reference/SQL知识点初步划分.png` 对题目打 L1/L2 标签。
5. 如果图片中的划分覆盖不全，按 `knowledge_taxonomy.md` 的补充规则扩展 L2 标签。
6. 生成标准题库 JSON，结构参考 `templates/question_dataset.schema.json`。
7. 将标准题库交给外部 AI 模拟不同学生回答。
8. 将学生回答再输入判分流程，判断正确性、错因和知识点掌握度。
9. 按 `data_small_test/data_student.json` 的聚合格式整理成新的学生数据。

## 输出命名建议

- `outputs/data_std_full.json`: 全量标准题库。
- `outputs/data_student_raw_full.json`: 外部 AI 模拟学生后的原始作答。
- `outputs/data_student_full.json`: 判分后整理成与小规模数据一致的新学生数据。
- `outputs/perception_audit_full.json`: 判分和知识点诊断审计日志。

## 当前阶段

当前任务是完成数据集构建流程图设计材料，即 `prompts/figma_dataset_construction_prompt.md`。真正的 PDF 全量抽题和学生作答模拟属于后续阶段。
