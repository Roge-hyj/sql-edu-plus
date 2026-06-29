# Figma/FigJam 数据集构建流程图提示词

请绘制一张“SQL DQL 评测数据集构建流程图”。用途是放在论文、答辩或项目汇报中，说明数据从三本 SQL 教材 PDF 到标准题库、学生模拟作答、知识点掌握矩阵和 20 题初始诊断集的完整链路。

## 画布风格

- 画布比例：16:9 横向。
- 风格：学术、清晰、工程流程图，适合论文答辩/项目汇报。
- 背景：白色或浅灰。
- 配色：主流程使用蓝色、青色、橙色、紫色、绿色、红色、灰色等区分阶段；质量控制使用红色虚线边框。
- 字体：中文无衬线字体，标题加粗。
- 图形：每个节点使用圆角矩形卡片，每张卡片内部固定分为三块：输入、工具/方法、输出。
- 关系：主流程使用实线箭头；质量控制使用虚线箭头。

## 总标题

SQL DQL 评测数据集构建流程

副标题：

从三本教材 PDF 到标准题库、学生作答、知识点掌握矩阵与 20 题初始诊断集

## 主流程节点

从左到右、上下两行绘制 8 个主节点。

### 1. 教材 PDF 输入

输入：

- `source_pdfs/`
- 三本 SQL / 数据库教材 PDF

工具/方法：

- 人工确定教材来源
- 保留文件名与 `source` 字段追溯

输出：

- Database System Concepts 7th Edition
- Fundamentals of Database Systems
- Learn SQL Fast / SQL with Practice Exercises

### 2. 全书扫描抽取 SQL DQL 题目

输入：

- 三本 PDF 全文
- PDF 中的例题、练习题、课后题、答案段落

工具/方法：

- `pypdf.PdfReader` 文本抽取
- `pdf_question_extraction_prompt.md`
- `build_data_std_full.py`
- 按教材知识点出现顺序整理

输出：

- `full_scan_candidates.json`
- `extraction_report.md`

### 3. 筛除非 DQL / 概念题 / DDL / DML

输入：

- 全书扫描候选题
- 题干、答案 SQL、教材来源

工具/方法：

- 保留 `SELECT` / `WITH` / 集合查询
- 筛除概念题
- 筛除 DDL、DML、DCL、TCL
- 筛除 ER、规范化、索引、事务、恢复、并发控制等非查询题

输出：

- SQL DQL 候选题集合
- 211 道可标准化题目

### 4. L1 / L2 知识点标注

输入：

- DQL 题目
- 标准答案 SQL
- `schema`
- `source`
- `difficulty`

工具/方法：

- `knowledge_taxonomy.md`
- `infer_tags` 自动初标
- L1：BASIC、FILTER、ORDER、AGG、JOIN、SUBQUERY、FUNC、ADVANCED
- L2：投影、过滤、排序、聚合、连接、子查询、函数、高级查询等原子知识点
- `data_std_full_tag_audit` 逐题复核

输出：

- `l1` 核心知识点
- `l2` 原子知识点数组
- `difficulty` 1.0-10.0

### 5. data_std_full.json 标准题库

输入：

- 规范化题目记录
- 字段：`id`、`difficulty`、`l1`、`l2`、`schema`、`q`、`ans_sql`、`source`

工具/方法：

- `build_data_std_full.py`
- JSON schema 字段规范
- 空字段检查
- 来源可追溯检查
- 答案 SQL 清洗

输出：

- `data_std_full.json`
- 211 道标准 SQL DQL 题

### 6. 四类学生模拟

输入：

- `data_std_full.json`
- 每题标准答案 SQL
- 每题 L1/L2 知识点标签

工具/方法：

- 外部 AI 学生作答模拟
- 四类学生画像：
- `Newbie`
- `Basic_Filter_Student`
- `Agg_Join_Struggler`
- `Logic_Master`

输出：

- `data_student_raw_full.json`
- 4 × 211 条学生 SQL 作答记录

### 7. data_student_full.json 知识点掌握矩阵

输入：

- `data_student_raw_full.json`
- 学生 SQL
- 正确性状态
- 错因与知识点标签

工具/方法：

- `build_data_student_full.py`
- 按 L1/L2 聚合正确率
- 使用平滑公式生成掌握度
- 生成 `records`
- 生成 `kp1_matrix`
- 生成 `kp2_matrix`

输出：

- `data_student_full.json`
- 每类学生 211 条记录
- 8 个 KP1 掌握度
- 56 个 KP2 掌握度

### 8. 人工筛选 20 道关键题

输入：

- `data_std_full.json`
- 全量题库中实际出现的 L1/L2
- 难度分布

工具/方法：

- 人工筛选关键题
- `select_initial_diagnostic_20.py`
- 覆盖知识点与难度梯度
- 局部改写题干以适合诊断

输出：

- `initial_diagnostic_20.json`
- `initial_diagnostic_20_report.md`

## 质量控制模块

在主流程下方添加一条横向“质量控制与复核”泳道，用红色虚线边框表示，并用虚线箭头连接到第 2、4、5、7、8 阶段。

质量控制内容：

- `source` 可追溯：教材名、章节、页码或练习编号
- JSON schema 校验
- 空字段检查
- SQL 答案清洗
- L1/L2 覆盖统计
- `data_std_full_tag_audit` 逐题审计
- 20 题诊断集覆盖验证

## 输出物区域

在图右侧或底部添加一个“最终产物”小分组：

- `data_std_full.json`：全量标准题库
- `data_student_raw_full.json`：四类学生原始作答
- `data_student_full.json`：知识点掌握矩阵与学生记录
- `initial_diagnostic_20.json`：20 道初始诊断关键题

## 需要突出表达的重点

- 数据来源是三本 SQL / 数据库教材 PDF。
- 全书扫描后只保留 SQL DQL 查询题。
- 非 DQL、概念题、DDL、DML 等被明确筛除。
- 每道题都有标准 SQL 答案、schema、source、difficulty、L1/L2 标签。
- `data_std_full.json` 是后续学生模拟和 20 题诊断集的共同基准。
- 学生模拟包含四类画像，产出原始作答。
- `data_student_full.json` 不是原始作答，而是聚合后的知识点掌握矩阵。
- `initial_diagnostic_20.json` 是人工筛选的关键题集合，用于初始能力诊断。
