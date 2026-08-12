"""
健壮性压测脚本 v2 — 覆盖 18 类算子的刁钻边界案例
每个用例标注：预期检测方向、AST 差异类型、核心攻击意图
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sql-edu-backend"))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import generate_and_compare

# ── 输出目录 ─────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 测试用例 ─────────────────────────────────────────────────────────────────
# 格式：
#   name          显示名
#   standard_sql  参考答案
#   student_sql   学生作答
#   schema        {表名: [列名列表]}
#   expect_equiv  True=期望等价(正样本), False=期望不等价(负样本)
#   expect_kp     期望的 key_point 集合（非空=需要至少命中一个）
#   attack_note   测试意图说明

CASES = [

    # ═══════════════════════════════════════════════════════════════════════
    # 1. WHERE 边界三态
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[1a] WHERE > vs >=（单列数值）",
        "standard_sql": "SELECT name FROM student WHERE tot_cred > 100",
        "student_sql":  "SELECT name FROM student WHERE tot_cred >= 100",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE"],
        "attack_note": "boundary c=100 应生成 99,100,101 三态",
    },
    {
        "name": "[1b] WHERE > 0 vs >= 0（零边界）",
        "standard_sql": "SELECT name FROM employee WHERE salary > 0",
        "student_sql":  "SELECT name FROM employee WHERE salary >= 0",
        "schema": {"employee": ["emp_id", "name", "dept_id", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE"],
        "attack_note": "零边界特殊：salary=0 在 >=0 通过但在 >0 不通过",
    },
    {
        "name": "[1c] WHERE BETWEEN vs 独立 AND（正样本）",
        "standard_sql": "SELECT name FROM student WHERE tot_cred BETWEEN 90 AND 120",
        "student_sql":  "SELECT name FROM student WHERE tot_cred >= 90 AND tot_cred <= 120",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "BETWEEN 等价展开，不应误报",
    },
    {
        "name": "[1d] WHERE LIKE 前缀 vs 精确匹配",
        "standard_sql": "SELECT name FROM instructor WHERE name LIKE 'A%'",
        "student_sql":  "SELECT name FROM instructor WHERE name = 'Alice'",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE"],
        "attack_note": "LIKE 通配 vs 精确：Bob 符合 LIKE 不符合 =",
    },
    {
        "name": "[1e] WHERE IN vs OR 展开（正样本）",
        "standard_sql": "SELECT name FROM student WHERE dept_name IN ('Comp. Sci.', 'Math')",
        "student_sql":  "SELECT name FROM student WHERE dept_name = 'Comp. Sci.' OR dept_name = 'Math'",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "IN 等价于 OR 展开，不应误报",
    },
    {
        "name": "[1f] WHERE 字符串字面量大小写",
        "standard_sql": "SELECT building FROM department WHERE dept_name = 'Comp. Sci.'",
        "student_sql":  "SELECT building FROM department WHERE dept_name = 'comp. sci.'",
        "schema": {"department": ["dept_name", "building", "budget"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE"],
        "attack_note": "SQLite 字符串大小写敏感，'Comp. Sci.' != 'comp. sci.'",
    },
    {
        "name": "[1g] WHERE 复合 AND 谓词遗漏一半",
        "standard_sql": "SELECT name FROM employee WHERE dept_id = 1 AND salary > 5000",
        "student_sql":  "SELECT name FROM employee WHERE dept_id = 1",
        "schema": {"employee": ["emp_id", "name", "dept_id", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE"],
        "attack_note": "predicate_missing: salary 谓词被删除",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 2. NULL 空值过滤
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[2a] IS NULL vs IS NOT NULL",
        "standard_sql": "SELECT name FROM instructor WHERE dept_name IS NULL",
        "student_sql":  "SELECT name FROM instructor WHERE dept_name IS NOT NULL",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["NULL", "WHERE"],
        "attack_note": "NULL 探针：行有 NULL 时两者结果互补",
    },
    {
        "name": "[2b] = NULL vs IS NULL（SQL 常见误用）",
        "standard_sql": "SELECT name FROM instructor WHERE dept_name IS NULL",
        "student_sql":  "SELECT name FROM instructor WHERE dept_name = NULL",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["NULL", "WHERE"],
        "attack_note": "= NULL 在 SQL 中永远 false，IS NULL 才正确",
    },
    {
        "name": "[2c] NULL 在聚合中被忽略",
        "standard_sql": "SELECT AVG(salary) FROM instructor",
        "student_sql":  "SELECT SUM(salary) / COUNT(*) FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["NULL", "AGGREGATE"],
        "attack_note": "COUNT(*) 含 NULL 行，AVG 忽略 NULL，两者不等价",
    },
    {
        "name": "[2d] COALESCE 等价替换（正样本）",
        "standard_sql": "SELECT COALESCE(dept_name, 'Unknown') FROM instructor",
        "student_sql":  "SELECT CASE WHEN dept_name IS NULL THEN 'Unknown' ELSE dept_name END FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "COALESCE 等价于 CASE WHEN IS NULL，不应误报",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 3. SELECT 投影结构
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[3a] 多出一列",
        "standard_sql": "SELECT name, salary FROM instructor",
        "student_sql":  "SELECT name, salary, dept_name FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["SELECT", "PROJECTION"],
        "attack_note": "projection_changed: 多了 dept_name 列",
    },
    {
        "name": "[3b] 列顺序不同",
        "standard_sql": "SELECT name, dept_name FROM instructor",
        "student_sql":  "SELECT dept_name, name FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["SELECT", "PROJECTION"],
        "attack_note": "列顺序不同会导致行元组 (name,dept) vs (dept,name) 不匹配",
    },
    {
        "name": "[3c] 别名 vs 无别名（正样本）",
        "standard_sql": "SELECT name AS student_name FROM student",
        "student_sql":  "SELECT name FROM student",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "别名只影响列名标头，数据值相同，应判等价",
    },
    {
        "name": "[3d] SELECT * vs 具名列（正样本）",
        "standard_sql": "SELECT ID, name, dept_name, tot_cred FROM student",
        "student_sql":  "SELECT * FROM student",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "SELECT * 展开与具名列等价",
    },
    {
        "name": "[3e] 计算列缺失",
        "standard_sql": "SELECT name, salary * 1.1 AS new_salary FROM instructor",
        "student_sql":  "SELECT name, salary FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["SELECT", "PROJECTION"],
        "attack_note": "salary*1.1 与 salary 数值不同",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 4. DISTINCT 重复探针
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[4a] 缺少 DISTINCT（单列）",
        "standard_sql": "SELECT DISTINCT dept_name FROM instructor",
        "student_sql":  "SELECT dept_name FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["DISTINCT"],
        "attack_note": "distinct_changed: 生成重复 dept_name 行暴露缺失",
    },
    {
        "name": "[4b] 多余 DISTINCT（正样本）",
        "standard_sql": "SELECT dept_name FROM instructor GROUP BY dept_name",
        "student_sql":  "SELECT DISTINCT dept_name FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "GROUP BY 与 DISTINCT 在无聚合时等价",
    },
    {
        "name": "[4c] DISTINCT 多列 vs 单列",
        "standard_sql": "SELECT DISTINCT dept_name, building FROM department",
        "student_sql":  "SELECT DISTINCT dept_name FROM department",
        "schema": {"department": ["dept_name", "building", "budget"]},
        "expect_equiv": False,
        "expect_kp": ["DISTINCT", "PROJECTION"],
        "attack_note": "投影列数不同，DISTINCT 组合粒度不同",
    },
    {
        "name": "[4d] COUNT DISTINCT vs COUNT ALL",
        "standard_sql": "SELECT COUNT(DISTINCT dept_name) FROM instructor",
        "student_sql":  "SELECT COUNT(dept_name) FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["DISTINCT", "AGGREGATE"],
        "attack_note": "重复 dept_name 时 COUNT DISTINCT < COUNT ALL",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 5. JOIN 拓扑对齐
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[5a] 笛卡尔积 vs INNER JOIN",
        "standard_sql": "SELECT s.name, c.title FROM student s JOIN takes t ON s.ID=t.ID JOIN course c ON t.course_id=c.course_id",
        "student_sql":  "SELECT s.name, c.title FROM student s, takes t, course c",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
            "course":  ["course_id", "title", "dept_name", "credits"],
        },
        "expect_equiv": False,
        "expect_kp": ["JOIN"],
        "attack_note": "笛卡尔积 vs 等值 JOIN：行数爆炸",
    },
    {
        "name": "[5b] JOIN 两表正确（正样本）",
        "standard_sql": "SELECT s.name, i.name FROM student s JOIN advisor a ON s.ID=a.s_ID JOIN instructor i ON a.i_ID=i.ID",
        "student_sql":  "SELECT s.name, i.name FROM student s INNER JOIN advisor a ON s.ID=a.s_ID INNER JOIN instructor i ON a.i_ID=i.ID",
        "schema": {
            "student":    ["ID", "name", "dept_name", "tot_cred"],
            "advisor":    ["s_ID", "i_ID"],
            "instructor": ["ID", "name", "dept_name", "salary"],
        },
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "JOIN 等价于 INNER JOIN，不应误报",
    },
    {
        "name": "[5c] JOIN ON 键错位（用错外键）",
        "standard_sql": "SELECT s.name FROM student s JOIN advisor a ON s.ID = a.s_ID",
        "student_sql":  "SELECT s.name FROM student s JOIN advisor a ON s.ID = a.i_ID",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "advisor": ["s_ID", "i_ID"],
        },
        "expect_equiv": False,
        "expect_kp": ["JOIN"],
        "attack_note": "join_on_changed: s_ID vs i_ID 键漂移",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 6. JOIN 跨键漂移
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[6a] 自连接课程先修表键漂移",
        "standard_sql": "SELECT prereq_id FROM prereq WHERE course_id = 'CS-301'",
        "student_sql":  "SELECT course_id FROM prereq WHERE prereq_id = 'CS-301'",
        "schema": {"prereq": ["course_id", "prereq_id"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE", "JOIN"],
        "attack_note": "course_id 与 prereq_id 角色对调",
    },
    {
        "name": "[6b] 三表链式 JOIN 中间键错位",
        "standard_sql": "SELECT s.name, c.title FROM student s JOIN takes t ON s.ID=t.ID JOIN course c ON t.course_id=c.course_id",
        "student_sql":  "SELECT s.name, c.title FROM student s JOIN takes t ON s.ID=t.ID JOIN course c ON s.dept_name=c.dept_name",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
            "course":  ["course_id", "title", "dept_name", "credits"],
        },
        "expect_equiv": False,
        "expect_kp": ["JOIN"],
        "attack_note": "最后一个 JOIN ON 条件用了错误列",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 7. LEFT JOIN 悬浮元组
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[7a] INNER JOIN vs LEFT JOIN",
        "standard_sql": "SELECT s.name, t.grade FROM student s LEFT JOIN takes t ON s.ID=t.ID",
        "student_sql":  "SELECT s.name, t.grade FROM student s INNER JOIN takes t ON s.ID=t.ID",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
        },
        "expect_equiv": False,
        "expect_kp": ["JOIN"],
        "attack_note": "join_type_changed: 无选课学生行在 LEFT JOIN 中保留，INNER 中丢失",
    },
    {
        "name": "[7b] LEFT JOIN 过滤右表 NULL（陷阱）",
        "standard_sql": "SELECT s.name FROM student s LEFT JOIN takes t ON s.ID=t.ID WHERE t.ID IS NULL",
        "student_sql":  "SELECT s.name FROM student s WHERE s.ID NOT IN (SELECT ID FROM takes)",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
        },
        "expect_equiv": False,
        "expect_kp": ["JOIN", "WHERE", "SUBQUERY"],
        "attack_note": "NOT IN 在子查询含 NULL 时与 LEFT JOIN 反连接不等价",
    },
    {
        "name": "[7c] 双表 LEFT JOIN 顺序不同",
        "standard_sql": "SELECT d.dept_name, i.name FROM department d LEFT JOIN instructor i ON d.dept_name=i.dept_name",
        "student_sql":  "SELECT d.dept_name, i.name FROM instructor i RIGHT JOIN department d ON d.dept_name=i.dept_name",
        "schema": {
            "department": ["dept_name", "building", "budget"],
            "instructor": ["ID", "name", "dept_name", "salary"],
        },
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "LEFT JOIN A→B 等价于 RIGHT JOIN B→A，不应误报",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 8. GROUP BY 分组粒度
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[8a] GROUP BY 列数错少",
        "standard_sql": "SELECT dept_name, semester, AVG(salary) FROM instructor GROUP BY dept_name, semester",
        "student_sql":  "SELECT dept_name, semester, AVG(salary) FROM instructor GROUP BY dept_name",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary", "semester"]},
        "expect_equiv": False,
        "expect_kp": ["GROUP BY"],
        "attack_note": "group_by_changed: 缺少 semester 分组列，粒度变粗",
    },
    {
        "name": "[8b] GROUP BY 用别名列（SQLite 容错，正样本）",
        "standard_sql": "SELECT dept_name, COUNT(*) AS cnt FROM instructor GROUP BY dept_name",
        "student_sql":  "SELECT dept_name, COUNT(*) FROM instructor GROUP BY dept_name",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "别名 cnt 不影响聚合结果，不应误报",
    },
    {
        "name": "[8c] GROUP BY 多列顺序不同（正样本）",
        "standard_sql": "SELECT dept_name, building, COUNT(*) FROM department GROUP BY dept_name, building",
        "student_sql":  "SELECT dept_name, building, COUNT(*) FROM department GROUP BY building, dept_name",
        "schema": {"department": ["dept_name", "building", "budget"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "GROUP BY 列顺序不影响结果，不应误报",
    },
    {
        "name": "[8d] GROUP BY 错列（用 building 代替 dept_name）",
        "standard_sql": "SELECT dept_name, AVG(budget) FROM department GROUP BY dept_name",
        "student_sql":  "SELECT dept_name, AVG(budget) FROM department GROUP BY building",
        "schema": {"department": ["dept_name", "building", "budget"]},
        "expect_equiv": False,
        "expect_kp": ["GROUP BY"],
        "attack_note": "group_by_changed: 分组键换成无关列 building",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 9. HAVING 聚合边界（SUM/AVG/MIN/MAX）
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[9a] HAVING SUM > vs >=",
        "standard_sql": "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name HAVING SUM(salary) > 50000",
        "student_sql":  "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name HAVING SUM(salary) >= 50000",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING"],
        "attack_note": "having_changed: boundary=50000 三态区分 > vs >=",
    },
    {
        "name": "[9b] HAVING AVG vs WHERE 过滤前平均",
        "standard_sql": "SELECT dept_name, AVG(salary) FROM instructor GROUP BY dept_name HAVING AVG(salary) > 60000",
        "student_sql":  "SELECT dept_name, AVG(salary) FROM instructor WHERE salary > 60000 GROUP BY dept_name",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING", "WHERE"],
        "attack_note": "WHERE 先过滤再 AVG vs HAVING 先 AVG 再过滤，结果不同",
    },
    {
        "name": "[9c] HAVING MAX vs MIN",
        "standard_sql": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) > 80000",
        "student_sql":  "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) > 80000",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING"],
        "attack_note": "MAX vs MIN: 混合工资时组内最大>=边界但最小<边界",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 10. HAVING COUNT 组大小边界
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[10a] HAVING COUNT > vs >=",
        "standard_sql": "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > 3",
        "student_sql":  "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) >= 3",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING"],
        "attack_note": "having_changed COUNT: boundary=3 时恰好3人的组区分 > vs >=",
    },
    {
        "name": "[10b] HAVING COUNT 零边界",
        "standard_sql": "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > 0",
        "student_sql":  "SELECT dept_name FROM student GROUP BY dept_name",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "COUNT(*) > 0 对所有组成立，等价于无 HAVING，不应误报",
    },
    {
        "name": "[10c] HAVING COUNT(col) vs COUNT(*) 含 NULL",
        "standard_sql": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(salary) >= 2",
        "student_sql":  "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(*) >= 2",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING", "NULL"],
        "attack_note": "salary 含 NULL 时 COUNT(salary) < COUNT(*)",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 11. ORDER BY 有序精确比对
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[11a] ORDER BY 主键方向相反",
        "standard_sql": "SELECT name, salary FROM instructor ORDER BY salary DESC",
        "student_sql":  "SELECT name, salary FROM instructor ORDER BY salary ASC",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["ORDER BY"],
        "attack_note": "order_by_changed: DESC vs ASC",
    },
    {
        "name": "[11b] ORDER BY 次键遗漏（并列打破器）",
        "standard_sql": "SELECT name, salary FROM instructor ORDER BY salary DESC, name ASC",
        "student_sql":  "SELECT name, salary FROM instructor ORDER BY salary DESC",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["ORDER BY"],
        "attack_note": "生成相同 salary 的多行，次键 name 决定顺序",
    },
    {
        "name": "[11c] ORDER BY 完全正确（正样本）",
        "standard_sql": "SELECT name FROM student ORDER BY tot_cred DESC",
        "student_sql":  "SELECT name FROM student ORDER BY tot_cred DESC",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "完全一致，不应误报",
    },
    {
        "name": "[11d] ORDER BY 不同列（salary vs name）",
        "standard_sql": "SELECT name, salary FROM instructor ORDER BY salary",
        "student_sql":  "SELECT name, salary FROM instructor ORDER BY name",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["ORDER BY"],
        "attack_note": "order_by_changed: 按 salary 排 vs 按 name 排，顺序不同",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 12. LIMIT/OFFSET 行数边界
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[12a] LIMIT 值不同",
        "standard_sql": "SELECT name FROM student ORDER BY tot_cred DESC LIMIT 3",
        "student_sql":  "SELECT name FROM student ORDER BY tot_cred DESC LIMIT 5",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": False,
        "expect_kp": ["LIMIT"],
        "attack_note": "limit_changed: 生成 6 行，第4、5行区分 LIMIT 3 vs 5",
    },
    {
        "name": "[12b] LIMIT + OFFSET 偏移错误",
        "standard_sql": "SELECT name FROM student ORDER BY tot_cred DESC LIMIT 3 OFFSET 2",
        "student_sql":  "SELECT name FROM student ORDER BY tot_cred DESC LIMIT 3 OFFSET 0",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": False,
        "expect_kp": ["LIMIT"],
        "attack_note": "OFFSET 不同，滑动窗口返回不同行",
    },
    {
        "name": "[12c] 无 LIMIT vs 有 LIMIT（行数足够时）",
        "standard_sql": "SELECT name FROM student ORDER BY tot_cred DESC LIMIT 100",
        "student_sql":  "SELECT name FROM student ORDER BY tot_cred DESC",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": False,
        "expect_kp": ["LIMIT"],
        "attack_note": "一般数据库可能超过 100 行，LIMIT 100 与无限制不等价",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 13. 子查询内外层值域重合
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[13a] IN 子查询 vs 手写 IN 列表",
        "standard_sql": "SELECT name FROM student WHERE dept_name IN (SELECT dept_name FROM department WHERE building = 'Watson')",
        "student_sql":  "SELECT name FROM student WHERE dept_name IN ('Comp. Sci.', 'Math')",
        "schema": {
            "student":    ["ID", "name", "dept_name", "tot_cred"],
            "department": ["dept_name", "building", "budget"],
        },
        "expect_equiv": False,
        "expect_kp": ["WHERE", "SUBQUERY"],
        "attack_note": "子查询动态 vs 硬编码：Watson 楼系不一定是 CS/Math",
    },
    {
        "name": "[13b] NOT IN vs NOT EXISTS（正样本）",
        "standard_sql": "SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes)",
        "student_sql":  "SELECT name FROM student WHERE NOT EXISTS (SELECT 1 FROM takes WHERE takes.ID = student.ID)",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
        },
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "NOT IN 等价于 NOT EXISTS（无 NULL 时），不应误报",
    },
    {
        "name": "[13c] 子查询 > ANY vs > MIN（正样本）",
        "standard_sql": "SELECT name FROM instructor WHERE salary > (SELECT MIN(salary) FROM instructor WHERE dept_name='Math')",
        "student_sql":  "SELECT name FROM instructor WHERE salary > ANY (SELECT salary FROM instructor WHERE dept_name='Math')",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "> ANY 等价于 > MIN，不应误报",
    },
    {
        "name": "[13d] 标量子查询边界值错位",
        "standard_sql": "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)",
        "student_sql":  "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor WHERE dept_name='Comp. Sci.')",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE", "SUBQUERY"],
        "attack_note": "全局 AVG vs 单系 AVG 阈值不同",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 14. 相关子查询关联列交叉
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[14a] 相关子查询关联列写错",
        "standard_sql": "SELECT name FROM instructor i WHERE salary > (SELECT AVG(salary) FROM instructor WHERE dept_name = i.dept_name)",
        "student_sql":  "SELECT name FROM instructor i WHERE salary > (SELECT AVG(salary) FROM instructor WHERE dept_name = 'Comp. Sci.')",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["SUBQUERY", "WHERE"],
        "attack_note": "相关子查询动态按系计算 vs 固定系名",
    },
    {
        "name": "[14b] EXISTS 相关子查询（正样本）",
        "standard_sql": "SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID=s.ID AND t.grade='A')",
        "student_sql":  "SELECT DISTINCT s.name FROM student s JOIN takes t ON s.ID=t.ID WHERE t.grade='A'",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
        },
        "expect_equiv": False,
        "expect_kp": ["SUBQUERY", "DISTINCT", "JOIN"],
        "attack_note": "学生姓名可重复时 JOIN+DISTINCT name 会错误折叠 EXISTS 的结果",
    },
    {
        "name": "[14c] 相关子查询 ALL 条件更严",
        "standard_sql": "SELECT name FROM instructor i WHERE salary >= ALL (SELECT salary FROM instructor WHERE dept_name = i.dept_name)",
        "student_sql":  "SELECT name FROM instructor i WHERE salary >= (SELECT MAX(salary) FROM instructor WHERE dept_name = i.dept_name)",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": ">= ALL 等价于 >= MAX，不应误报",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 15. UNION/INTERSECT/EXCEPT 集合操作探针
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[15a] UNION vs UNION ALL",
        "standard_sql": "SELECT name FROM instructor UNION SELECT name FROM student",
        "student_sql":  "SELECT name FROM instructor UNION ALL SELECT name FROM student",
        "schema": {
            "instructor": ["ID", "name", "dept_name", "salary"],
            "student":    ["ID", "name", "dept_name", "tot_cred"],
        },
        "expect_equiv": False,
        "expect_kp": ["UNION"],
        "attack_note": "set_operator_changed: 同名人出现在两表时 UNION 去重 UNION ALL 不去",
    },
    {
        "name": "[15b] INTERSECT vs INNER JOIN 同列（正样本）",
        "standard_sql": "SELECT dept_name FROM instructor INTERSECT SELECT dept_name FROM student",
        "student_sql":  "SELECT DISTINCT i.dept_name FROM instructor i JOIN student s ON i.dept_name=s.dept_name",
        "schema": {
            "instructor": ["ID", "name", "dept_name", "salary"],
            "student":    ["ID", "name", "dept_name", "tot_cred"],
        },
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "INTERSECT 等价于 JOIN+DISTINCT 同列，不应误报",
    },
    {
        "name": "[15c] EXCEPT vs NOT IN（正样本）",
        "standard_sql": "SELECT dept_name FROM instructor EXCEPT SELECT dept_name FROM student",
        "student_sql":  "SELECT DISTINCT dept_name FROM instructor WHERE dept_name NOT IN (SELECT dept_name FROM student)",
        "schema": {
            "instructor": ["ID", "name", "dept_name", "salary"],
            "student":    ["ID", "name", "dept_name", "tot_cred"],
        },
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "EXCEPT 等价于 NOT IN，不应误报",
    },
    {
        "name": "[15d] UNION 两部分条件颠倒",
        "standard_sql": "SELECT name FROM instructor WHERE dept_name='Comp. Sci.' UNION SELECT name FROM student WHERE dept_name='Math'",
        "student_sql":  "SELECT name FROM instructor WHERE dept_name='Math' UNION SELECT name FROM student WHERE dept_name='Comp. Sci.'",
        "schema": {
            "instructor": ["ID", "name", "dept_name", "salary"],
            "student":    ["ID", "name", "dept_name", "tot_cred"],
        },
        "expect_equiv": False,
        "expect_kp": ["UNION", "WHERE"],
        "attack_note": "两个分支条件互换，从不同表取不同系",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 16. CASE WHEN 分支边界
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[16a] CASE WHEN 缺少分支",
        "standard_sql": "SELECT name, CASE WHEN grade='A' THEN 'Excellent' WHEN grade='B' THEN 'Good' ELSE 'Other' END FROM takes",
        "student_sql":  "SELECT name, CASE WHEN grade='A' THEN 'Excellent' ELSE 'Other' END FROM takes",
        "schema": {
            "takes": ["ID", "course_id", "sec_id", "semester", "year", "grade", "name"],
        },
        "expect_equiv": False,
        "expect_kp": ["CASE"],
        "attack_note": "缺少 grade='B' THEN 'Good' 分支，B 被归入 Other",
    },
    {
        "name": "[16b] CASE WHEN 条件相同结果不同",
        "standard_sql": "SELECT name, CASE WHEN salary > 70000 THEN 'High' ELSE 'Low' END FROM instructor",
        "student_sql":  "SELECT name, CASE WHEN salary > 70000 THEN 'Low' ELSE 'High' END FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["CASE"],
        "attack_note": "结果标签对调 High/Low",
    },
    {
        "name": "[16c] CASE WHEN 边界值相差1",
        "standard_sql": "SELECT name, CASE WHEN tot_cred >= 90 THEN 'Senior' ELSE 'Junior' END FROM student",
        "student_sql":  "SELECT name, CASE WHEN tot_cred > 90 THEN 'Senior' ELSE 'Junior' END FROM student",
        "schema": {"student": ["ID", "name", "dept_name", "tot_cred"]},
        "expect_equiv": False,
        "expect_kp": ["CASE", "WHERE"],
        "attack_note": "CASE WHEN 内部比较符 >= vs >，tot_cred=90 时结果不同",
    },
    {
        "name": "[16d] CASE WHEN 等价写法（正样本）",
        "standard_sql": "SELECT name, CASE WHEN salary > 70000 THEN 'High' WHEN salary > 50000 THEN 'Mid' ELSE 'Low' END FROM instructor",
        "student_sql":  "SELECT name, CASE WHEN salary <= 50000 THEN 'Low' WHEN salary <= 70000 THEN 'Mid' ELSE 'High' END FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "倒序分支等价，不应误报",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 17. WINDOW 分区与排序数据
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[17a] RANK vs ROW_NUMBER（并列情况）",
        "standard_sql": "SELECT name, RANK() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rnk FROM instructor",
        "student_sql":  "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rnk FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WINDOW"],
        "attack_note": "同 dept 同 salary 并列时：RANK 跳号，ROW_NUMBER 不跳",
    },
    {
        "name": "[17b] PARTITION BY 列错误",
        "standard_sql": "SELECT name, AVG(salary) OVER (PARTITION BY dept_name) FROM instructor",
        "student_sql":  "SELECT name, AVG(salary) OVER (PARTITION BY name) FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WINDOW"],
        "attack_note": "window_over_changed: 分区键换成 name（每人一组）",
    },
    {
        "name": "[17c] 无窗口分区 vs 全局窗口（正样本）",
        "standard_sql": "SELECT name, AVG(salary) OVER () FROM instructor",
        "student_sql":  "SELECT name, (SELECT AVG(salary) FROM instructor) FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "OVER() 无分区等价于标量子查询全局平均，不应误报",
    },
    {
        "name": "[17d] ORDER BY 窗口缺失导致帧不同",
        "standard_sql": "SELECT name, SUM(salary) OVER (PARTITION BY dept_name ORDER BY salary) FROM instructor",
        "student_sql":  "SELECT name, SUM(salary) OVER (PARTITION BY dept_name) FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WINDOW"],
        "attack_note": "有 ORDER BY 时帧为 RANGE UNBOUNDED PRECEDING，累加 vs 全组合",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 18. CTE / 递归 CTE 基表与终止边界探针
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[18a] CTE 条件反向",
        "standard_sql": """
            WITH high_salary AS (SELECT name FROM instructor WHERE salary > 80000)
            SELECT name FROM high_salary
        """,
        "student_sql": """
            WITH high_salary AS (SELECT name FROM instructor WHERE salary <= 80000)
            SELECT name FROM high_salary
        """,
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["CTE", "WHERE"],
        "attack_note": "cte_changed: CTE 内部过滤条件反向",
    },
    {
        "name": "[18b] CTE 链式引用中间层缺失",
        "standard_sql": """
            WITH dept_avg AS (SELECT dept_name, AVG(salary) AS avg_sal FROM instructor GROUP BY dept_name),
                 high_dept AS (SELECT dept_name FROM dept_avg WHERE avg_sal > 60000)
            SELECT i.name FROM instructor i JOIN high_dept h ON i.dept_name = h.dept_name
        """,
        "student_sql": """
            WITH dept_avg AS (SELECT dept_name, AVG(salary) AS avg_sal FROM instructor GROUP BY dept_name)
            SELECT i.name FROM instructor i JOIN dept_avg d ON i.dept_name = d.dept_name WHERE d.avg_sal > 60000
        """,
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "两层 CTE vs 单层 CTE+WHERE 等价，不应误报",
    },
    {
        "name": "[18c] CTE 重用 vs 子查询重复计算（正样本）",
        "standard_sql": """
            WITH avg_sal AS (SELECT AVG(salary) AS avg FROM instructor)
            SELECT name FROM instructor, avg_sal WHERE salary > avg_sal.avg
        """,
        "student_sql": """
            SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)
        """,
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "CTE 等价于内联子查询，不应误报",
    },
    {
        "name": "[18d] 递归 CTE 终止条件差异（层数不同）",
        "standard_sql": """
            WITH RECURSIVE nums(n) AS (
                SELECT 1
                UNION ALL
                SELECT n + 1 FROM nums WHERE n < 5
            )
            SELECT n FROM nums
        """,
        "student_sql": """
            WITH RECURSIVE nums(n) AS (
                SELECT 1
                UNION ALL
                SELECT n + 1 FROM nums WHERE n < 3
            )
            SELECT n FROM nums
        """,
        "schema": {},
        "expect_equiv": False,
        "expect_kp": ["CTE", "RECURSIVE"],
        "attack_note": "recursive_cte_changed: 递归终止条件 n<5 vs n<3，行数不同",
    },

    # ═══════════════════════════════════════════════════════════════════════
    # 综合组合场景（多算子叠加）
    # ═══════════════════════════════════════════════════════════════════════
    {
        "name": "[19a] GROUP BY + HAVING + ORDER BY 全错",
        "standard_sql": "SELECT dept_name, COUNT(*) FROM instructor GROUP BY dept_name HAVING COUNT(*) > 2 ORDER BY COUNT(*) DESC",
        "student_sql":  "SELECT dept_name, COUNT(*) FROM instructor GROUP BY dept_name HAVING COUNT(*) >= 2 ORDER BY COUNT(*) ASC",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING", "ORDER BY"],
        "attack_note": "HAVING > vs >=，ORDER BY DESC vs ASC，双错",
    },
    {
        "name": "[19b] LEFT JOIN + WHERE NULL 过滤 + LIMIT",
        "standard_sql": "SELECT s.name FROM student s LEFT JOIN takes t ON s.ID=t.ID WHERE t.ID IS NULL LIMIT 2",
        "student_sql":  "SELECT s.name FROM student s LEFT JOIN takes t ON s.ID=t.ID WHERE t.ID IS NULL LIMIT 3",
        "schema": {
            "student": ["ID", "name", "dept_name", "tot_cred"],
            "takes":   ["ID", "course_id", "sec_id", "semester", "year", "grade"],
        },
        "expect_equiv": False,
        "expect_kp": ["LIMIT"],
        "attack_note": "多算子组合：LIMIT 不同决定结果",
    },
    {
        "name": "[19c] DISTINCT + ORDER BY + 子查询",
        "standard_sql": "SELECT DISTINCT dept_name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor) ORDER BY dept_name",
        "student_sql":  "SELECT dept_name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor) ORDER BY dept_name",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["DISTINCT"],
        "attack_note": "缺少 DISTINCT：同 dept 多人薪资 > 平均时出现重复系名",
    },
    {
        "name": "[19d] CTE + JOIN + HAVING 组合（正样本）",
        "standard_sql": """
            WITH dept_count AS (SELECT dept_name, COUNT(*) AS cnt FROM student GROUP BY dept_name)
            SELECT d.dept_name FROM department d JOIN dept_count dc ON d.dept_name=dc.dept_name WHERE dc.cnt > 1
        """,
        "student_sql": """
            SELECT d.dept_name
            FROM department d
            JOIN (SELECT dept_name, COUNT(*) AS cnt FROM student GROUP BY dept_name) dc ON d.dept_name=dc.dept_name
            WHERE dc.cnt > 1
        """,
        "schema": {
            "department": ["dept_name", "building", "budget"],
            "student":    ["ID", "name", "dept_name", "tot_cred"],
        },
        "expect_equiv": True,
        "expect_kp": [],
        "attack_note": "CTE 等价于内联派生表，不应误报",
    },
    {
        "name": "[19e] 窗口 + GROUP BY 混用误区",
        "standard_sql": "SELECT dept_name, AVG(salary) FROM instructor GROUP BY dept_name",
        "student_sql":  "SELECT dept_name, AVG(salary) OVER (PARTITION BY dept_name) FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WINDOW", "GROUP BY"],
        "attack_note": "窗口函数保留所有行，GROUP BY 折叠行；行数不同",
    },
]


# ── 数据生成盲区专项回归 ─────────────────────────────────────────────────────

Validation = Callable[[dict[str, Any]], tuple[bool, str]]


def _schema_text(schema: dict[str, list[str]]) -> str:
    return "; ".join(f"{table}({', '.join(columns)})" for table, columns in schema.items())


def _rows(ctx: dict[str, Any], table: str) -> list[dict[str, Any]]:
    return ctx["run"].test_database.get(table, [])


def _column_values(ctx: dict[str, Any], table: str, column: str) -> list[Any]:
    return [row.get(column) for row in _rows(ctx, table)]


def _kp_ids(ctx: dict[str, Any]) -> list[str]:
    return [item.knowledge_point_id for item in ctx["attr"].attributions]


def _kp_matches(actual: list[str], expected: set[str]) -> bool:
    if not expected:
        return True
    def norm(value: str) -> str:
        return value.lower().replace(" ", "-").replace("_", "-")

    expected_norm = {norm(item) for item in expected}
    actual_norm = {norm(item) for item in actual}
    aliases = {
        "join": {"join-on", "join-inner", "join-left", "join-right", "join-full"},
        "window": {"window-row-number"},
        "recursive": {"cte-recursive"},
        "null": {"comp-null"},
        "projection": {"select-basic"},
        "aggregate": {"agg-count", "having"},
        "subquery": {"subquery-scalar", "subquery-in", "subquery-exists", "subquery-correlated"},
    }
    for expected_item in expected_norm:
        candidates = aliases.get(expected_item, {expected_item})
        if actual_norm & candidates:
            return True
    return bool(actual_norm & expected_norm)


def _expect_values(table: str, column: str, required: set[Any]) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = _column_values(ctx, table, column)
        present = set(values)
        ok = required.issubset(present)
        return ok, f"{table}.{column} values={values}, required={sorted(required, key=str)}"

    return validate


def _expect_duplicate(table: str, column: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = _column_values(ctx, table, column)
        counts = Counter(values)
        duplicated = [value for value, count in counts.items() if count > 1]
        return bool(duplicated), f"{table}.{column} duplicate_values={duplicated}, values={values}"

    return validate


def _expect_null(table: str, column: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = _column_values(ctx, table, column)
        return any(value is None for value in values), f"{table}.{column} values={values}"

    return validate


def _expect_self_role_drift(table: str, left_col: str, right_col: str, literal: Any) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        rows = _rows(ctx, table)
        left_rows = [row for row in rows if row.get(left_col) == literal]
        right_rows = [row for row in rows if row.get(right_col) == literal]
        left_projection = {row.get(right_col) for row in left_rows}
        right_projection = {row.get(left_col) for row in right_rows}
        role_drift = any(row.get(left_col) != row.get(right_col) for row in rows)
        ok = bool(left_rows) and bool(right_rows) and role_drift and left_projection != right_projection
        detail = (
            f"{table} rows={rows}, {left_col}={literal}->{sorted(left_projection, key=str)}, "
            f"{right_col}={literal}->{sorted(right_projection, key=str)}"
        )
        return ok, detail

    return validate


def _expect_having_where_split(boundary: int | float) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        groups: dict[Any, list[float]] = defaultdict(list)
        for row in _rows(ctx, "instructor"):
            salary = row.get("salary")
            if salary is not None:
                groups[row.get("dept_name")].append(float(salary))
        mixed_group = {
            key: values
            for key, values in groups.items()
            if values and any(value <= boundary for value in values) and (sum(values) / len(values)) > boundary
        }
        return bool(mixed_group), f"dept salary groups={dict(groups)}, mixed_group={mixed_group}"

    return validate


def _expect_secondary_sort_tie(table: str, primary_col: str, secondary_col: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        buckets: dict[Any, set[Any]] = defaultdict(set)
        for row in _rows(ctx, table):
            buckets[row.get(primary_col)].add(row.get(secondary_col))
        ties = {key: sorted(values, key=str) for key, values in buckets.items() if len(values) > 1}
        return bool(ties), f"{table}.{primary_col}/{secondary_col} tie_buckets={ties}"

    return validate


def _expect_window_rank_tie() -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        buckets: dict[tuple[Any, Any], int] = Counter(
            (row.get("dept_name"), row.get("salary")) for row in _rows(ctx, "instructor")
        )
        ties = {key: count for key, count in buckets.items() if count > 1}
        return bool(ties), f"(dept_name, salary) ties={ties}, rows={_rows(ctx, 'instructor')}"

    return validate


def _expect_cte_city_contrast() -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        company_rows = _rows(ctx, "company")
        works_rows = _rows(ctx, "works")
        cities = {row.get("city") for row in company_rows}
        companies = {row.get("company_name") for row in company_rows}
        worked_companies = {row.get("company_name") for row in works_rows}
        ok = {"Beijing", "Shanghai"}.issubset(cities) and bool(companies & worked_companies)
        return ok, f"company={company_rows}, works={works_rows}"

    return validate


BLIND_SPOT_CASES: list[dict[str, Any]] = [
    {
        "name": "[BS12] 自连接 prereq 角色键值错位",
        "standard_sql": "SELECT prereq_id FROM prereq WHERE course_id = 'CS-301'",
        "student_sql": "SELECT course_id FROM prereq WHERE prereq_id = 'CS-301'",
        "schema": {"prereq": ["course_id", "prereq_id"]},
        "expect_equiv": False,
        "expect_kp": ["WHERE", "JOIN"],
        "attack_note": "造数必须让 course_id/prereq_id 两种角色同时出现 CS-301 且投影结果不同",
        "blind_spot": "self_join_prereq_role_drift",
        "data_checks": [_expect_self_role_drift("prereq", "course_id", "prereq_id", "CS-301")],
    },
    {
        "name": "[BS17] HAVING 与 WHERE 执行顺序差异",
        "standard_sql": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) > 60000",
        "student_sql": "SELECT dept_name FROM instructor WHERE salary > 60000 GROUP BY dept_name",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["HAVING", "WHERE"],
        "attack_note": "同一组内必须混入 <=60000 与 >60000 的 salary，避免 WHERE/HAVING 在全正数据上等效",
        "blind_spot": "having_where_order",
        "data_checks": [_expect_having_where_split(60000)],
    },
    {
        "name": "[BS18] ORDER BY 次键遗漏",
        "standard_sql": "SELECT title, credits FROM course ORDER BY credits DESC, title ASC",
        "student_sql": "SELECT title, credits FROM course ORDER BY credits DESC",
        "schema": {"course": ["course_id", "title", "dept_name", "credits"]},
        "expect_equiv": False,
        "expect_kp": ["ORDER BY"],
        "attack_note": "主排序键 credits 必须出现并列，title 次键才会触发",
        "blind_spot": "order_by_secondary_key",
        "data_checks": [_expect_secondary_sort_tie("course", "credits", "title")],
    },
    {
        "name": "[BS20] ORDER BY NULLS LAST",
        "standard_sql": "SELECT name, salary FROM instructor ORDER BY salary ASC NULLS LAST",
        "student_sql": "SELECT name, salary FROM instructor ORDER BY salary ASC",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["ORDER BY", "NULL"],
        "attack_note": "salary 必须包含 NULL，SQLite 默认 ASC NULLS FIRST，才能暴露 NULLS LAST 缺失",
        "blind_spot": "order_by_nulls_last",
        "data_checks": [_expect_null("instructor", "salary")],
    },
    {
        "name": "[BS32] RANK vs ROW_NUMBER 并列排名",
        "standard_sql": "SELECT name, RANK() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rnk FROM instructor",
        "student_sql": "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rnk FROM instructor",
        "schema": {"instructor": ["ID", "name", "dept_name", "salary"]},
        "expect_equiv": False,
        "expect_kp": ["WINDOW"],
        "attack_note": "同一分区内 salary 必须并列，否则 RANK 与 ROW_NUMBER 输出相同",
        "blind_spot": "rank_row_number_tie",
        "data_checks": [_expect_window_rank_tie()],
    },
    {
        "name": "[BS34] CTE 反向城市条件",
        "standard_sql": """
            WITH bj AS (SELECT company_name FROM company WHERE city = 'Beijing')
            SELECT person_name FROM works WHERE company_name IN (SELECT company_name FROM bj)
        """,
        "student_sql": """
            WITH bj AS (SELECT company_name FROM company WHERE city <> 'Beijing')
            SELECT person_name FROM works WHERE company_name IN (SELECT company_name FROM bj)
        """,
        "schema": {
            "works": ["company_name", "person_name", "salary"],
            "company": ["company_name", "city"],
        },
        "expect_equiv": False,
        "expect_kp": ["CTE", "WHERE"],
        "attack_note": "company.city 必须同时含 Beijing 和非 Beijing，并且 works 连接到两类公司",
        "blind_spot": "cte_reverse_city_condition",
        "data_checks": [_expect_cte_city_contrast()],
    },
]


# ── 运行逻辑 ──────────────────────────────────────────────────────────────────

def run_case(case: dict[str, Any]) -> dict[str, Any]:
    name = case["name"]
    std_sql = case["standard_sql"].strip()
    stu_sql = case["student_sql"].strip()
    schema = case["schema"]
    expect_equiv = case["expect_equiv"]
    expect_kp = set(case.get("expect_kp", []))
    attack_note = case.get("attack_note", "")

    try:
        run = generate_and_compare(_schema_text(schema), std_sql, stu_sql, max_rows_per_table=10)
    except Exception as exc:
        return {
            "name": name,
            "status": "ERROR",
            "detail": f"沙盒异常: {exc}",
            "error": str(exc),
            "attack_note": attack_note,
            "blind_spot": case.get("blind_spot"),
        }

    is_equiv = run.is_equivalent
    is_correct = bool(is_equiv)
    if run.error:
        is_correct = False

    try:
        attr = evidence_weights_from_observation(
            student_sql=stu_sql,
            answer_sql=std_sql,
            is_correct=is_correct,
            error_message=run.error or run.data_evidence.get("student_exec_error"),
            judge_detail=run.data_evidence,
            mutation_detail=run.mutation_evidence,
            ast_diffs=[diff.to_dict() for diff in run.ast_diffs],
        )
        kp_found = [item.knowledge_point_id for item in attr.attributions]
    except Exception as exc:
        attr = None
        kp_found = [f"ATTRIBUTION_ERROR: {exc}"]

    ctx = {"case": case, "run": run, "attr": attr}
    data_checks = []
    for validate in case.get("data_checks", []):
        try:
            ok, detail = validate(ctx)
        except Exception as exc:
            ok, detail = False, f"data_check_error: {exc}"
        data_checks.append({"ok": ok, "detail": detail})

    data_ok = all(item["ok"] for item in data_checks)
    kp_hit = _kp_matches(kp_found, expect_kp)
    exec_error = run.error or run.data_evidence.get("student_exec_error")

    if expect_equiv:
        if is_equiv is True:
            status = "PASS"
            detail = "正确判为等价"
        else:
            status = "FAIL"
            detail = f"误判正样本为不等价 | kp={kp_found}"
    else:
        if is_equiv is True:
            status = "FAIL"
            detail = "未检测到不等价（数据生成盲区）"
        elif is_equiv is False:
            if expect_kp and not kp_hit:
                status = "PARTIAL"
                detail = f"检测到不等价，但归因未命中（期望 {sorted(expect_kp)}，实际 {kp_found}）"
            else:
                status = "PASS"
                detail = f"检测到不等价，归因={kp_found}"
            if data_checks and not data_ok:
                status = "FAIL"
                detail = f"{detail}；但专项造数断言失败"
        else:
            status = "ERROR"
            detail = f"沙盒未给出等价性结论: {exec_error}"

    return {
        "name": name,
        "status": status,
        "detail": detail,
        "expect_equiv": expect_equiv,
        "is_equiv": is_equiv,
        "kp_found": kp_found,
        "kp_hit": kp_hit,
        "attack_note": attack_note,
        "blind_spot": case.get("blind_spot"),
        "exec_error": exec_error,
        "data_checks": data_checks,
        "data_evidence": run.data_evidence,
        "mutation_summary": run.mutation_evidence.get("summary"),
        "ast_diffs": run.data_evidence.get("ast_diffs", []),
        "generation_tactics": run.data_evidence.get("generation_tactics", []),
        "standard_row_count": run.data_evidence.get("standard_row_count"),
        "student_row_count": run.data_evidence.get("student_row_count"),
        "standard_rows_sample": run.standard_rows[:8],
        "student_rows_sample": run.student_rows[:8],
        "generated_rows": run.test_database,
    }


def _render_markdown(results: list[dict[str, Any]], counts: dict[str, int]) -> str:
    total = len(results)
    pass_rate = counts.get("PASS", 0) / total * 100 if total else 0
    detect_rate = (counts.get("PASS", 0) + counts.get("PARTIAL", 0)) / total * 100 if total else 0
    lines = [
        "# 健壮性压测报告 v3",
        "",
        f"- 总用例：{total}",
        f"- PASS：{counts.get('PASS', 0)}",
        f"- PARTIAL：{counts.get('PARTIAL', 0)}",
        f"- FAIL：{counts.get('FAIL', 0)}",
        f"- ERROR：{counts.get('ERROR', 0)}",
        f"- 完全通过率：{pass_rate:.1f}%",
        f"- 检测率（PASS+PARTIAL）：{detect_rate:.1f}%",
        "",
        "| 状态 | 用例 | 等价判定 | KP | 攻击意图 |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for result in results:
        lines.append(
            f"| `{result['status']}` | {result['name']} | `{result['is_equiv']}` | "
            f"`{', '.join(result['kp_found'])}` | {result['attack_note']} |"
        )

    failures = [item for item in results if item["status"] in {"FAIL", "PARTIAL", "ERROR"}]
    if failures:
        lines.extend(["", "## 失败与部分通过详情", ""])
        for result in failures:
            lines.extend(
                [
                    f"### {result['name']}",
                    f"- 状态：`{result['status']}`",
                    f"- 结果：{result['detail']}",
                    f"- 盲区标签：`{result.get('blind_spot')}`",
                    f"- 等价判定：`{result['is_equiv']}`",
                    f"- 归因 KP：`{result['kp_found']}`",
                    f"- AST diff：`{result['ast_diffs']}`",
                    f"- 造数策略：`{result['generation_tactics']}`",
                    f"- 行数：standard=`{result['standard_row_count']}`, student=`{result['student_row_count']}`",
                ]
            )
            for idx, check in enumerate(result.get("data_checks") or [], 1):
                lines.append(f"- 数据断言 {idx}：`{'PASS' if check['ok'] else 'FAIL'}` - {check['detail']}")
            lines.extend(
                [
                    "- 生成数据：",
                    "```json",
                    json.dumps(result["generated_rows"], ensure_ascii=False, indent=2),
                    "```",
                ]
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    all_cases = CASES + BLIND_SPOT_CASES
    print("=" * 70)
    print(f"  健壮性压测 v3 — 共 {len(all_cases)} 个用例（含 {len(BLIND_SPOT_CASES)} 个数据盲区专项）")
    print("=" * 70)

    results = []
    counts = {"PASS": 0, "FAIL": 0, "PARTIAL": 0, "ERROR": 0}

    for i, case in enumerate(all_cases, 1):
        result = run_case(case)
        results.append(result)
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
        detail = result.get("detail", result.get("error", ""))
        print(f"[{status}] [{i:02d}] {result['name']}")
        print(f"       {detail}")

    print()
    print("=" * 70)
    total = len(all_cases)
    print(f"  结果汇总：总计 {total} | PASS {counts['PASS']} | "
          f"PARTIAL {counts['PARTIAL']} | FAIL {counts['FAIL']} | ERROR {counts['ERROR']}")
    pass_rate = (counts['PASS'] / total) * 100
    detect_rate = ((counts['PASS'] + counts['PARTIAL']) / total) * 100
    print(f"  完全通过率：{pass_rate:.1f}%   检测率（PASS+PARTIAL）：{detect_rate:.1f}%")
    print("=" * 70)

    # 详细失败报告
    failures = [r for r in results if r["status"] in ("FAIL", "PARTIAL", "ERROR")]
    if failures:
        print()
        print("── 失败 / 部分通过 详情 ─────────────────────────────────────────────")
        for r in failures:
            print()
            print(f"  [{r['status']}] {r['name']}")
            print(f"  攻击意图: {r['attack_note']}")
            print(f"  结果:     {r.get('detail', r.get('error', ''))}")
            if r.get("kp_found"):
                print(f"  归因KP:   {r['kp_found']}")

    report_path = OUTPUT_DIR / "robustness_stress_report_v2.json"
    report_v3_path = OUTPUT_DIR / "robustness_stress_report_v3.json"
    md_path = OUTPUT_DIR / "robustness_stress_report_v3.md"
    payload = {
            "total": total,
            "counts": counts,
            "pass_rate": pass_rate,
            "detect_rate": detect_rate,
            "blind_spot_cases": len(BLIND_SPOT_CASES),
            "results": results,
        }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report_v3_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(results, counts), encoding="utf-8")
    print(f"\n  JSON报告已保存：{report_v3_path}")
    print(f"  兼容报告已保存：{report_path}")
    print(f"  Markdown报告已保存：{md_path}")


if __name__ == "__main__":
    main()
