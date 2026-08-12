# SQL 错误聚类与 Query Path Bundling 设计

## 问题背景

当前系统已经能够识别学生 SQL 中的多个局部错误，例如 `JOIN` 错误、`WHERE` 条件错误、`GROUP BY` 错误、`SELECT` 输出错误以及执行结果不一致等。

但如果系统直接把这些错误平铺输出给学生，会造成两个问题：

1. 学生同时接收大量零散错误点，认知负荷过高；
2. 多个局部错误可能来自同一个根因，如果分别反馈，会掩盖真正的理解偏差。

因此，系统需要将离散的多位置错误按成因聚类，并将聚类结果映射到学生状态和教学动作。

核心思路是：

> 按 SQL 逻辑执行顺序构建“查询路径”，再把路径上的错误按成因打包。

这里的“执行顺序”指教学意义上的 SQL 逻辑执行顺序，而不是数据库优化器的真实物理执行顺序。

```text
FROM / JOIN
  -> WHERE
  -> GROUP BY
  -> HAVING
  -> SELECT
  -> DISTINCT
  -> ORDER BY
  -> LIMIT
```

这样学生能够理解查询结果是如何一步步形成的，系统反馈也更接近教师讲题方式。

## 一、为什么按查询路径打包错误

很多 SQL 错误存在上下游关系。

例如：

```text
FROM / JOIN 错
  -> 可用字段范围错
  -> WHERE 可能引用错字段
  -> GROUP BY 粒度错
  -> SELECT 输出错
  -> 最终结果错
```

如果系统直接反馈：

```text
JOIN 错、WHERE 错、SELECT 错、结果不一致
```

学生会同时面对多个错误点，难以判断应该先改哪里。

更合理的反馈是：

> 你的查询在第一步“确定数据来源”时就偏离了题目。后面的字段和结果问题，大多是这个偏离带来的。

这样就将多个局部错误压缩为一个可理解的教学解释。

## 二、Query Path Bundle

系统可以将标准答案和学生答案都转成一条查询路径。

标准路径示例：

```text
FROM students
JOIN enrollments
JOIN courses
WHERE courses.name = '数据库'
GROUP BY students.id
SELECT students.name, COUNT(*)
```

学生路径示例：

```text
FROM courses
JOIN enrollments
JOIN students
GROUP BY courses.id
SELECT courses.name, COUNT(*)
```

此时系统不应只判断：

```text
GROUP BY mismatch
SELECT mismatch
```

而应进一步形成路径偏离解释：

```text
Path deviation:
学生从 courses 作为统计对象开始组织查询，
而题目要求围绕 students 组织查询。
```

这可以自然形成理解级归因。

## 三、路径阶段与错误打包规则

### 1. FROM / JOIN 阶段

该阶段关注查询的数据来源和表关系。

检查内容包括：

- 主查询对象是否正确；
- 是否缺少必要表；
- 表关系路径是否正确；
- `JOIN` 条件是否正确；
- `JOIN` 类型是否正确。

对应教学动作：

- 先讲“这题要从哪些表取数据”；
- 给出表关系路径提示；
- 必要时展示关系图。

如果该阶段出现根因错误，后续 `SELECT`、`WHERE`、`GROUP BY` 中的许多错误都应视为下游错误，不应优先单独反馈。

### 2. WHERE 阶段

该阶段关注行级筛选条件。

检查内容包括：

- 是否漏掉题目限定词；
- 条件字段是否正确；
- 比较方向是否正确；
- `AND` / `OR` 逻辑是否正确；
- 时间、状态、范围条件是否正确。

对应教学动作：

- 回到题目关键词；
- 指出题目中的筛选条件；
- 引导学生判断“哪些记录应该被保留”。

### 3. GROUP BY 阶段

该阶段关注结果粒度。

检查内容包括：

- 结果中一行代表什么；
- 应该按哪个实体分组；
- 聚合前后的粒度是否正确。

对应教学动作：

- 解释“一行代表什么”；
- 让学生先说最终结果的行粒度；
- 再提示 `GROUP BY` 应该围绕哪个字段。

### 4. HAVING 阶段

该阶段关注聚合后的组级筛选。

检查内容包括：

- 是否把聚合后条件误写成 `WHERE`；
- 是否理解“对分组结果筛选”；
- 聚合条件是否对应题意。

对应教学动作：

- 区分行级筛选和组级筛选；
- 用小数据展示 `WHERE` 与 `HAVING` 的区别。

### 5. SELECT 阶段

该阶段关注最终输出。

检查内容包括：

- 输出字段是否符合题目要求；
- 是否输出了错误实体；
- 聚合表达式是否正确；
- 别名和展示字段是否正确。

对应教学动作：

- 提示最终需要展示哪些列；
- 如果前面的结果粒度已经错误，则不单独强调 `SELECT`，而是将其作为下游证据。

### 6. DISTINCT / ORDER BY / LIMIT 阶段

该阶段关注结果整理。

检查内容包括：

- 是否有重复行；
- 排序字段是否正确；
- Top-N 逻辑是否正确；
- `LIMIT` 是否对应题意。

对应教学动作：

- 用反例数据解释重复来源；
- 提示“最后一步只是整理结果”。

## 四、核心算法：定位最早偏离点

系统可以按以下流程工作：

```text
1. 将标准 SQL 转成 standard_query_path
2. 将学生 SQL 转成 student_query_path
3. 每个阶段生成 evidence
4. 按 SQL 逻辑执行顺序遍历
5. 找到第一个高置信度偏离阶段
6. 将其下游相关错误打包为一个 bundle
7. 输出该 bundle 对应的教学动作
```

伪代码如下：

```python
stages = [
    "FROM_JOIN",
    "WHERE",
    "GROUP_BY",
    "HAVING",
    "SELECT",
    "DISTINCT",
    "ORDER_BY",
    "LIMIT",
]

for stage in stages:
    evidence = compare_stage(
        student_path[stage],
        standard_path[stage],
    )

    if evidence.has_root_cause_error():
        bundle = collect_downstream_errors(stage, evidence)
        action = select_action(bundle, student_state)
        return bundle, action
```

该算法遵循三个原则：

1. 最早偏离点优先；
2. 上游错误压制下游错误；
3. 下游错误作为证据，不作为多个独立反馈。

## 五、路径打包与成因聚类的关系

路径打包和成因聚类应结合使用。

可以这样理解：

```text
执行路径 = 教学讲解顺序
成因簇 = 错误压缩单位
```

也就是说，执行顺序负责组织错误，成因聚类负责压缩错误。

例如，在 `FROM / JOIN` 阶段：

```text
- 缺少 enrollments 表
- students 和 courses 直接 JOIN
- 结果出现重复或缺失
```

这些错误可以聚成：

```text
表关系路径理解错误
```

再例如，在 `GROUP BY + SELECT` 阶段：

```text
- GROUP BY courses.name
- SELECT courses.name
- 标准答案按 students.id 聚合
```

这些错误可以聚成：

```text
结果粒度理解错误
```

## 六、面向学生的查询路径纠偏

系统输出不应是：

```text
你 JOIN 错了，GROUP BY 也错了，SELECT 也错了。
```

而应是：

```text
你的查询路径在“确定统计对象”这一步发生了偏离。

题目要求的是“每个学生”的选课数量，所以结果中一行应该代表一个学生。
但你的 SQL 按课程分组，并输出课程名称，因此它更像是在回答“每门课程有多少记录”。

先不要急着改 SELECT，先确认：最终结果中一行应该代表学生，还是课程？
```

这种反馈更接近教师讲题过程，也能降低学生的认知负荷。

## 七、与学生状态绑定

同一个路径偏离，在不同学生状态下应触发不同教学动作。

```text
第一次出现：
  给题意层提示

第二次出现：
  给 Schema / 路径提示

第三次出现：
  指出具体 SQL 阶段，例如 GROUP BY

连续失败：
  给半结构化模板
```

示例一：

```text
Cause: 结果粒度理解错误
Stage: GROUP_BY + SELECT
Attempt: 第一次

Action:
  题意追问：“这题最终一行代表什么？”
```

示例二：

```text
Cause: 结果粒度理解错误
Stage: GROUP_BY + SELECT
Attempt: 第三次

Action:
  具体提示：“你的 GROUP BY 现在围绕课程字段，题目要求围绕学生字段。”
```

## 八、系统模块设计

该方案可以抽象为：

```text
Query Path Bundling
```

定义：

> 系统按照 SQL 逻辑执行顺序构建标准查询路径与学生查询路径，并在每个阶段收集结构差异、Schema 差异和执行差异。系统优先定位查询路径中的最早高置信度偏离点，将其下游相关错误作为同一成因簇的证据，从而避免向学生输出大量零散错误。最终，系统根据偏离阶段、成因类型和学生历史状态选择相应教学动作。

在现有系统中，可以将该模块放在 `Phi Arbiter` 之后、`BKT` 和 `ActionSelector` 之前：

```text
AST diff
  -> 沙盒执行
  -> 变分消融
  -> Phi Arbiter
  -> QueryPathBundler
  -> BKT / Cognitive Load
  -> ActionSelector
  -> LLM 生成自然语言反馈
```

`QueryPathBundler` 的输出示例：

```json
{
  "root_stage": "GROUP_BY",
  "cause": "wrong_result_grain",
  "evidence": [
    "GROUP BY courses.name",
    "SELECT courses.name",
    "标准答案按 students.id 聚合"
  ],
  "suppressed_downstream_errors": [
    "SELECT 输出实体错误",
    "结果行数不一致"
  ],
  "student_mapping": {
    "knowledge_components": ["GROUP BY", "aggregation"],
    "misconception": "wrong_result_grain"
  },
  "action": {
    "type": "conceptual_hint",
    "focus": "结果中一行代表什么"
  }
}
```

## 九、总结

该方案可以概括为：

> 按 SQL 逻辑执行顺序组织错误路径，按成因聚类压缩错误点，按学生状态选择教学动作。

它解决了三个关键问题：

1. 将多位置错误压缩为少量成因簇；
2. 将算法识别结果映射到学生理解偏差；
3. 将错误归因进一步映射到具体教学动作。

最终系统反馈的对象不再是零散 SQL 错误，而是学生查询路径中的核心偏离点。
