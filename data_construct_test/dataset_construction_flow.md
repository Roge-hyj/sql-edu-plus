# 数据集构建流程图蓝图

本图用于说明 SQL DQL 数据集从教材 PDF 到标准题库、学生模拟作答、知识点掌握矩阵和 20 题初始诊断集的完整构建链路。

已生成 SVG 图：

- `outputs/dataset_construction_flow_detailed.svg`

## 具体化流程图

```mermaid
flowchart LR
    A["1. 教材 PDF 输入<br/><br/>输入：source_pdfs/ 下 3 本 SQL / 数据库教材 PDF<br/>工具/方法：人工确定教材来源，保留文件名和 source 追溯<br/>输出：Database System Concepts 7e；Fundamentals of Database Systems；Learn SQL Fast"]

    B["2. 全书扫描抽取 SQL DQL 题目<br/><br/>输入：三本 PDF 全文、练习题与答案段落<br/>工具/方法：pypdf.PdfReader 文本抽取；pdf_question_extraction_prompt.md；build_data_std_full.py<br/>输出：full_scan_candidates.json；extraction_report.md"]

    C["3. 筛除非 DQL / 概念题 / DDL / DML<br/><br/>输入：全书扫描候选题、题干、答案 SQL、来源<br/>工具/方法：只保留 SELECT / WITH / 集合查询；排除概念题、DDL、DML、DCL、TCL、ER、规范化、索引等非查询题<br/>输出：SQL DQL 候选题集合；211 道可标准化题目"]

    D["4. L1 / L2 知识点标注<br/><br/>输入：DQL 题目、标准答案 SQL、schema、source、difficulty<br/>工具/方法：knowledge_taxonomy.md；infer_tags 自动初标；data_std_full_tag_audit 逐题复核<br/>输出：l1 核心知识点；l2 原子知识点数组；difficulty 1.0-10.0"]

    E["5. data_std_full.json 标准题库<br/><br/>输入：规范化题目记录，含 id、schema、q、ans_sql、source<br/>工具/方法：build_data_std_full.py；JSON schema 字段规范；来源和空字段检查<br/>输出：data_std_full.json；211 道标准 SQL DQL 题"]

    F["6. 四类学生模拟<br/><br/>输入：data_std_full.json、每题标准答案、L1/L2 标签<br/>工具/方法：外部 AI 学生作答模拟；四类画像 Newbie、Basic_Filter_Student、Agg_Join_Struggler、Logic_Master<br/>输出：data_student_raw_full.json；4 × 211 条学生 SQL 作答"]

    G["7. data_student_full.json 知识点掌握矩阵<br/><br/>输入：data_student_raw_full.json、学生 SQL、正确性、错因<br/>工具/方法：build_data_student_full.py；按 L1/L2 聚合正确率；平滑生成 kp1_matrix / kp2_matrix<br/>输出：data_student_full.json；records + 8 个 KP1 + 56 个 KP2"]

    H["8. 人工筛选 20 道关键题<br/><br/>输入：data_std_full.json、全量题库实际出现的 L1/L2<br/>工具/方法：人工筛选 + select_initial_diagnostic_20.py；覆盖知识点与难度梯度；题干局部改写用于诊断<br/>输出：initial_diagnostic_20.json；initial_diagnostic_20_report.md"]

    Q["质量控制与复核<br/><br/>source 可追溯；JSON schema 校验；SQL 答案清洗；L1/L2 覆盖统计；data_std_full_tag_audit 逐题审计；20 题覆盖验证"]

    A --> B --> C --> D --> E --> F --> G
    E --> H
    Q -.-> B
    Q -.-> D
    Q -.-> E
    Q -.-> G
    Q -.-> H
```

## 节点展开

| 阶段 | 输入 | 工具/方法 | 输出 |
| --- | --- | --- | --- |
| 1. 教材 PDF 输入 | `source_pdfs/` 下三本教材 PDF | 人工确认教材来源；保留文件名和 source 字段用于追溯 | 三本权威教材输入源 |
| 2. 全书扫描抽取 | PDF 全文、练习题、答案段落 | `pypdf.PdfReader`；`pdf_question_extraction_prompt.md`；`build_data_std_full.py` | `full_scan_candidates.json`、`extraction_report.md` |
| 3. DQL 筛除 | 全书扫描候选题 | 保留 `SELECT` / `WITH` / 集合查询；排除概念题、DDL、DML、DCL、TCL、数据库设计题 | 211 道可标准化 SQL DQL 题 |
| 4. L1/L2 标注 | 题干、标准 SQL、schema、source | `knowledge_taxonomy.md`；`infer_tags` 初标；逐题标签审计 | `l1`、`l2`、`difficulty` |
| 5. 标准题库 | 规范化题目记录 | `build_data_std_full.py`；字段规范与空字段检查 | `data_std_full.json` |
| 6. 四类学生模拟 | `data_std_full.json` | 外部 AI 按四类学生画像生成 SQL 作答 | `data_student_raw_full.json` |
| 7. 掌握矩阵聚合 | 学生原始作答与正确性 | `build_data_student_full.py`；按 L1/L2 聚合正确率；平滑补全矩阵 | `data_student_full.json` |
| 8. 20 题诊断集 | `data_std_full.json` 与全量 L1/L2 覆盖情况 | 人工筛选；`select_initial_diagnostic_20.py`；覆盖和难度梯度校验 | `initial_diagnostic_20.json` |

## 产物关系

- `data_std_full.json` 是标准题库基准，后续学生模拟和 20 题诊断集都从它派生。
- `data_student_raw_full.json` 保存四类学生的原始 SQL 作答。
- `data_student_full.json` 在原始作答基础上生成 `records`、`kp1_matrix`、`kp2_matrix`。
- `initial_diagnostic_20.json` 从标准题库中人工筛选 20 道关键题，用于前端初始能力诊断。
