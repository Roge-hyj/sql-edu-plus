import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_STD = ROOT / "outputs" / "data_std_full.json"
OUT_JSON = ROOT / "outputs" / "initial_diagnostic_20.json"
OUT_REPORT = ROOT / "outputs" / "initial_diagnostic_20_report.md"


KPS_HIERARCHY = {
    "KP_BASIC": ["PROJ_COL", "PROJ_EXPR", "ALIAS_COL", "ALIAS_TAB", "DISTINCT_SET", "LIMIT_OFF"],
    "KP_FILTER": ["COMP_VAL", "COMP_NULL", "LOGIC_AND_OR", "LOGIC_NOT", "RANGE_BET", "SET_IN", "LIKE_STR"],
    "KP_ORDER": ["SORT_ASC", "SORT_DESC", "SORT_MULTI", "SORT_NULLS"],
    "KP_AGG": ["AGG_BASIC", "AGG_DISTINCT", "GB_SIMPLE", "GB_MULTI", "HV_SIMPLE", "HV_COMPLEX"],
    "KP_JOIN": ["JOIN_INNER", "JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL", "JOIN_SELF", "JOIN_CROSS", "JOIN_ON", "JOIN_USING", "JOIN_NATURAL"],
    "KP_SUBQUERY": ["SUB_SCALAR", "SUB_ROW", "SUB_TABLE", "SUB_IN_ALL_ANY", "SUB_EXISTS", "SUB_CORR"],
    "KP_FUNC": ["STR_CASE", "STR_SUB", "NUM_ROUND", "DATE_EXT", "DATE_DIFF", "CASE_SIMPLE", "CASE_SEARCH", "TYPE_CAST"],
    "KP_ADVANCED": ["WIN_OVER", "WIN_RANK", "WIN_LEAD_LAG", "WIN_FRAME", "CTE_SIMPLE", "CTE_RECURSIVE", "SET_UNION", "SET_INTERSECT", "SET_EXCEPT", "NULL_COAL"],
}


# Chosen to cover every L2 tag that appears in data_std_full.json while keeping a diagnostic difficulty ramp.
DIAGNOSTIC_IDS = [
    146, 176, 1, 178, 173,
    64, 5, 72, 2, 8,
    95, 202, 4, 6, 92,
    31, 65, 67, 73, 59,
]


QUESTION_OVERRIDES = {
    146: "Display each product's name as Product, unit price, units in stock, and stock value.",
    178: "List the names and prices of the ten cheapest products.",
    173: "List the names, phone numbers, titles, and countries of non-US contacts whose titles begin with Sales or Marketing.",
    95: "For each branch whose name begins with B, find accounts with the maximum balance using a self left join instead of a nested subquery.",
    202: "Return the Cartesian product of Shippers and Customers using CROSS JOIN.",
}


def load_questions():
    questions = json.loads(DATA_STD.read_text(encoding="utf-8"))
    return {question["id"]: question for question in questions}, questions


def build_diagnostic_set(question_by_id):
    output = []
    for order, question_id in enumerate(DIAGNOSTIC_IDS, start=1):
        question = dict(question_by_id[question_id])
        if question_id in QUESTION_OVERRIDES:
            question["original_q"] = question["q"]
            question["q"] = QUESTION_OVERRIDES[question_id]
        question["diagnostic_order"] = order
        question["diagnostic_role"] = diagnostic_role(question)
        output.append(question)
    return output


def diagnostic_role(question):
    l1 = question["l1"]
    if l1 == "KP_BASIC":
        return "基础投影与表达式探测"
    if l1 == "KP_FILTER":
        return "过滤、逻辑条件与 NULL 探测"
    if l1 == "KP_ORDER":
        return "排序与限制返回探测"
    if l1 == "KP_AGG":
        return "聚合、分组与 HAVING 探测"
    if l1 == "KP_JOIN":
        return "连接类型与连接条件探测"
    if l1 == "KP_SUBQUERY":
        return "子查询与嵌套逻辑探测"
    if l1 == "KP_ADVANCED":
        return "高级 SQL 结构探测"
    return "综合 SQL 能力探测"


def coverage(items):
    l1 = {item["l1"] for item in items}
    l2 = {tag for item in items for tag in item["l2"]}
    return l1, l2


def write_outputs(diagnostic_items, all_questions):
    OUT_JSON.write_text(json.dumps(diagnostic_items, ensure_ascii=False, indent=2), encoding="utf-8")

    all_l1, all_l2 = coverage(all_questions)
    selected_l1, selected_l2 = coverage(diagnostic_items)
    taxonomy_l2 = {tag for tags in KPS_HIERARCHY.values() for tag in tags}
    l1_counts = Counter(item["l1"] for item in diagnostic_items)
    difficulties = [float(item["difficulty"]) for item in diagnostic_items]

    lines = [
        "# 初始能力诊断 20 题报告",
        "",
        "## 输出文件",
        "",
        "- `initial_diagnostic_20.json`",
        "",
        "## 选题目标",
        "",
        "- 用 20 道题作为前后端项目初始判断学生能力的诊断集。",
        "- 覆盖全量题库中实际出现的 L1/L2 知识点。",
        "- 保留从基础查询到高级 SQL 的难度梯度，便于初始化隐状态向量 `L_t`。",
        "",
        "## 覆盖结果",
        "",
        f"- 题目数：{len(diagnostic_items)}",
        f"- 覆盖 L1：{len(selected_l1)}/{len(all_l1)}",
        f"- 覆盖全量题库已出现 L2：{len(selected_l2)}/{len(all_l2)}",
        f"- 知识体系中未在全量题库出现的 L2：{len(taxonomy_l2 - all_l2)}",
        f"- 平均难度：{sum(difficulties) / len(difficulties):.2f}",
        f"- 难度范围：{min(difficulties):.1f} - {max(difficulties):.1f}",
        "",
        "## L1 分布",
        "",
    ]

    for l1 in KPS_HIERARCHY:
        if l1 in all_l1 or l1_counts[l1]:
            lines.append(f"- `{l1}`: {l1_counts[l1]} 题")

    lines.extend([
        "",
        "## 诊断题顺序",
        "",
    ])
    for item in diagnostic_items:
        l2_text = ", ".join(item["l2"])
        lines.append(
            f"{item['diagnostic_order']}. Q{item['id']} | {item['l1']} | diff {item['difficulty']} | {l2_text}"
        )

    missing_from_selected = sorted(all_l2 - selected_l2)
    lines.extend([
        "",
        "## 校验",
        "",
        f"- 未覆盖的已出现 L2 数量：{len(missing_from_selected)}",
    ])
    if missing_from_selected:
        lines.append(f"- 未覆盖 L2：{', '.join(missing_from_selected)}")
    else:
        lines.append("- 已覆盖全量题库中出现过的全部 L2 标签。")

    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    question_by_id, all_questions = load_questions()
    missing_ids = [question_id for question_id in DIAGNOSTIC_IDS if question_id not in question_by_id]
    if missing_ids:
        raise SystemExit(f"missing question ids: {missing_ids}")

    diagnostic_items = build_diagnostic_set(question_by_id)
    write_outputs(diagnostic_items, all_questions)
    selected_l1, selected_l2 = coverage(diagnostic_items)
    _, all_l2 = coverage(all_questions)
    print(f"wrote {len(diagnostic_items)} questions to {OUT_JSON}")
    print(f"covered {len(selected_l1)} L1 and {len(selected_l2)}/{len(all_l2)} appeared L2 tags")
    print(f"wrote report to {OUT_REPORT}")


if __name__ == "__main__":
    main()
