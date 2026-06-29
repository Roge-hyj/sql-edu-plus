import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_STD = ROOT / "outputs" / "data_std_full.json"
OUT_RAW = ROOT / "outputs" / "data_student_raw_full.json"
REPORT = ROOT / "outputs" / "student_simulation_report.md"

PERSONAS = ["Newbie", "Basic_Filter_Student", "Agg_Join_Struggler", "Logic_Master"]


def normalize_sql(sql):
    return " ".join(sql.strip().rstrip(";").split()) + ";"


def first_table(schema):
    match = re.search(r"([A-Za-z_][A-Za-z0-9_\[\] ]*)\s*\(", schema)
    return match.group(1).strip() if match else None


def first_column(schema):
    match = re.search(r"\(([^)]*)\)", schema)
    if not match:
        return "*"
    return match.group(1).split(",")[0].strip().split()[0] or "*"


def simple_select(q):
    table = first_table(q["schema"])
    if not table:
        return "SELECT 1;"
    return f"SELECT {first_column(q['schema'])} FROM {table};"


def first_table_from_sql(sql):
    match = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_\[\]]*)", sql, flags=re.I)
    return match.group(1) if match else None


def remove_group_by(sql):
    return re.sub(r"\s+GROUP\s+BY\s+.+?(?=\)|\s+HAVING|\s+ORDER\s+BY|$)", "", sql, flags=re.I)


def remove_order_by(sql):
    return re.sub(r"\s+ORDER\s+BY\s+.+?$", "", sql, flags=re.I)


def flip_comparison(sql):
    for pattern, repl in [
        (r"\s<>\s", " = "),
        (r"\s>=\s", " < "),
        (r"\s<=\s", " > "),
        (r"\s>\s", " < "),
        (r"\s<\s", " > "),
        (r"\s=\s", " <> "),
    ]:
        if re.search(pattern, sql):
            return re.sub(pattern, repl, sql, count=1)
    return sql


def balanced_parentheses(sql):
    return sql.count("(") == sql.count(")")


def should_be_correct(q, persona):
    diff = float(q.get("difficulty", 5.0))
    l1 = q["l1"]
    l2 = set(q["l2"])

    if persona == "Logic_Master":
        if diff <= 8.8:
            return True
        if {"CTE_RECURSIVE", "WIN_FRAME"} & l2:
            return False
        return diff <= 9.4
    if persona == "Newbie":
        if l1 in {"KP_BASIC", "KP_ORDER"}:
            return True
        if l1 == "KP_FILTER" and diff <= 4.0 and not {"SET_IN", "LOGIC_NOT"} & l2:
            return True
        if l1 == "KP_AGG" and diff <= 5.3 and not {"HV_SIMPLE", "GB_MULTI", "AGG_DISTINCT"} & l2:
            return True
        if l1 == "KP_JOIN" and diff <= 6.0 and "JOIN_INNER" in l2 and "JOIN_ON" in l2 and not {"JOIN_LEFT", "JOIN_FULL", "JOIN_SELF", "JOIN_CROSS"} & l2:
            return True
        return False
    if persona == "Basic_Filter_Student":
        if l1 in {"KP_BASIC", "KP_FILTER", "KP_ORDER"} and diff <= 4.5:
            return True
        if l1 == "KP_AGG" and diff <= 5.9 and "HV_SIMPLE" not in l2:
            return True
        if l1 == "KP_JOIN" and diff <= 6.7 and not {"JOIN_LEFT", "JOIN_FULL", "JOIN_SELF", "JOIN_CROSS"} & l2:
            return True
        if l1 == "KP_SUBQUERY" and diff <= 8.0 and "SUB_EXISTS" not in l2:
            return True
        return False
    if persona == "Agg_Join_Struggler":
        if l1 in {"KP_BASIC", "KP_FILTER", "KP_ORDER", "KP_FUNC"} and diff <= 4.5:
            return True
        if l1 == "KP_AGG" and diff <= 5.4 and "HV_SIMPLE" not in l2:
            return True
        if l1 == "KP_JOIN" and diff <= 6.1 and not {"JOIN_LEFT", "JOIN_FULL", "JOIN_SELF", "JOIN_CROSS"} & l2:
            return True
        if l1 == "KP_SUBQUERY" and diff <= 8.8 and "SUB_EXISTS" not in l2:
            return True
        if l1 == "KP_ADVANCED" and diff <= 9.2 and not {"CTE_RECURSIVE", "WIN_FRAME"} & l2:
            return True
        return False
    return False


def make_incorrect_sql(q):
    sql = normalize_sql(q["ans_sql"]).rstrip(";")
    l1 = q["l1"]
    l2 = set(q["l2"])

    if {"JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL"} & l2:
        bad = re.sub(r"\b(LEFT|RIGHT|FULL)\s+(OUTER\s+)?JOIN\b", "INNER JOIN", sql, flags=re.I)
        return normalize_sql(bad), "把外连接误写成内连接"

    if l1 == "KP_JOIN" or any(k.startswith("JOIN_") for k in l2):
        return simple_select(q), "只查询单表，漏掉连接逻辑"

    if "HV_SIMPLE" in l2 or re.search(r"\bHAVING\b", sql, flags=re.I):
        return normalize_sql(re.sub(r"\bHAVING\b", "WHERE", sql, count=1, flags=re.I)), "把 HAVING 写成 WHERE"

    if l1 == "KP_AGG" or any(k.startswith(("AGG_", "GB_")) for k in l2):
        bad = remove_group_by(sql)
        if bad != sql and balanced_parentheses(bad):
            return normalize_sql(bad), "漏写 GROUP BY"
        table = first_table_from_sql(sql)
        if table:
            return normalize_sql(f"SELECT * FROM {table}"), "没有使用必要聚合函数"
        return simple_select(q), "没有使用必要聚合函数"

    if "SUB_EXISTS" in l2 or "NOT EXISTS" in sql.upper():
        bad = re.sub(r"\bNOT\s+EXISTS\b", "EXISTS", sql, count=1, flags=re.I)
        if bad == sql:
            bad = re.sub(r"\bEXISTS\b", "NOT EXISTS", sql, count=1, flags=re.I)
        return normalize_sql(bad), "EXISTS/NOT EXISTS 混淆"

    if "SUB_IN_ALL_ANY" in l2 or l1 == "KP_SUBQUERY":
        bad = re.sub(r"\bNOT\s+IN\b", "IN", sql, count=1, flags=re.I)
        bad = re.sub(r"\bALL\b", "ANY", bad, count=1, flags=re.I)
        if bad == sql:
            bad = simple_select(q).rstrip(";")
        return normalize_sql(bad), "子查询条件使用错误"

    if "COMP_NULL" in l2:
        bad = re.sub(r"\bIS\s+NULL\b", "= NULL", sql, count=1, flags=re.I)
        bad = re.sub(r"\bIS\s+NOT\s+NULL\b", "<> NULL", bad, count=1, flags=re.I)
        return normalize_sql(bad), "NULL 比较方式错误"

    if "DISTINCT_SET" in l2:
        return normalize_sql(re.sub(r"\bDISTINCT\b\s*", "", sql, count=1, flags=re.I)), "忘记 DISTINCT"

    if l1 == "KP_ORDER" or "SORT_DESC" in l2:
        if re.search(r"\bDESC\b", sql, flags=re.I):
            return normalize_sql(re.sub(r"\bDESC\b", "ASC", sql, count=1, flags=re.I)), "排序方向写反"
        return normalize_sql(remove_order_by(sql)), "漏掉 ORDER BY"

    if "LIKE_STR" in l2:
        bad = re.sub(r"\bLIKE\b", "NOT LIKE", sql, count=1, flags=re.I)
        return normalize_sql(bad), "LIKE 条件方向写反"

    if "SET_IN" in l2:
        bad = re.sub(r"\bNOT\s+IN\b", "IN", sql, count=1, flags=re.I)
        if bad == sql:
            bad = re.sub(r"\bIN\b", "NOT IN", sql, count=1, flags=re.I)
        return normalize_sql(bad), "IN 条件写反"

    if l1 in {"KP_FILTER", "KP_FUNC"}:
        bad = flip_comparison(sql)
        if bad == sql and re.search(r"\bLIKE\b", sql, flags=re.I):
            bad = re.sub(r"\bLIKE\s+'%?([^%']+)%?'", r"= '\1'", sql, count=1, flags=re.I)
        if bad == sql and "=" in sql:
            bad = re.sub(r"=", "<>", sql, count=1)
        if bad == sql and re.search(r"\bWHERE\b", sql, flags=re.I):
            bad = re.sub(r"\s+WHERE\s+.+?(?=\s+GROUP\s+BY|\s+ORDER\s+BY|$)", "", sql, count=1, flags=re.I)
        if bad == sql:
            bad = simple_select(q).rstrip(";")
        return normalize_sql(bad), "过滤条件写错"

    if l1 == "KP_ADVANCED":
        return simple_select(q), "无法表达高级 SQL 结构"

    return simple_select(q), "过度简化题目"


def correct_thought(persona):
    return {
        "Newbie": "基础题能按模板完成",
        "Basic_Filter_Student": "过滤和简单连接掌握较好",
        "Agg_Join_Struggler": "非聚合连接题发挥稳定",
        "Logic_Master": "整体语义正确但非满分画像",
    }[persona]


def status_counts(records):
    correct = sum(1 for record in records if record["predicted_status"] == "Correct")
    incorrect = sum(1 for record in records if record["predicted_status"] == "Incorrect")
    return correct, incorrect


def write_report(questions, output):
    lines = [
        "# 模拟学生回答数据报告",
        "",
        "## 输出文件",
        "",
        "- `data_student_raw_full.json`",
        "",
        "## 生成依据",
        "",
        "- 标准题库：`data_std_full.json`",
        "- 生成脚本：`../scripts/simulate_student_answers.py`",
        "- 学生画像提示词：`../prompts/student_answer_simulation_prompt.md`",
        "",
        "## 学生画像",
        "",
        "共 4 类：",
        "",
        "- `Newbie`",
        "- `Basic_Filter_Student`",
        "- `Agg_Join_Struggler`",
        "- `Logic_Master`",
        "",
        "## 数据量",
        "",
        f"- 标准题目数：{len(questions)}",
        f"- 学生画像数：{len(output)}",
        f"- 学生回答记录数：{sum(len(x['records']) for x in output)}",
        "",
        "## Correct / Incorrect 分布",
        "",
    ]

    for persona_output in output:
        correct, incorrect = status_counts(persona_output["records"])
        lines.append(f"- `{persona_output['persona']}`: Correct {correct}, Incorrect {incorrect}")

    bad_parentheses = sum(
        1
        for persona_output in output
        for record in persona_output["records"]
        if not balanced_parentheses(record["sql"])
    )
    empty_fields = sum(
        1
        for persona_output in output
        for record in persona_output["records"]
        for key in ["q_id", "l1", "l2", "predicted_status", "sql", "thought"]
        if record.get(key) in ("", [], None)
    )

    answer_by_id = {question["id"]: normalize_sql(question["ans_sql"]) for question in questions}
    copied_incorrect = sum(
        1
        for persona_output in output
        for record in persona_output["records"]
        if record["predicted_status"] == "Incorrect"
        and normalize_sql(record["sql"]) == answer_by_id[record["q_id"]]
    )

    lines.extend([
        "",
        "## 校验结果",
        "",
        "- JSON 可解析。",
        f"- 每个画像均有 {len(questions)} 条记录。",
        f"- 空 `q_id / l1 / l2 / predicted_status / sql / thought` 字段数量为 {empty_fields}。",
        f"- 括号不平衡 SQL 数量为 {bad_parentheses}。",
        f"- `predicted_status = Incorrect` 且 SQL 完全等于标准答案的记录数量为 {copied_incorrect}。",
        "",
        "## 注意",
        "",
        "这是“模拟学生原始作答数据”，不是最终判分后的 `data_student_full.json`。后续仍需基于 SQL 语法、执行结果、语义等价和 AST 差分做正式正确性判断，再整理为小规模 `data_student.json` 的最终聚合格式。",
        "",
    ])
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main():
    questions = json.loads(DATA_STD.read_text(encoding="utf-8"))
    output = []

    for persona in PERSONAS:
        records = []
        for q in questions:
            if should_be_correct(q, persona):
                status = "Correct"
                sql = normalize_sql(q["ans_sql"])
                thought = correct_thought(persona)
            else:
                status = "Incorrect"
                sql, thought = make_incorrect_sql(q)
            records.append({
                "q_id": q["id"],
                "l1": q["l1"],
                "l2": q["l2"],
                "predicted_status": status,
                "sql": sql,
                "thought": thought,
            })
        output.append({"persona": persona, "records": records})

    OUT_RAW.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(questions, output)
    total = sum(len(x["records"]) for x in output)
    print(f"wrote {len(output)} personas and {total} records to {OUT_RAW}")
    print(f"wrote report to {REPORT}")


if __name__ == "__main__":
    main()
