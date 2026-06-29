import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_STD = ROOT / "outputs" / "data_std_full.json"
DATA_RAW = ROOT / "outputs" / "data_student_raw_full.json"
OUT_FULL = ROOT / "outputs" / "data_student_full.json"
REPORT = ROOT / "outputs" / "data_student_full_report.md"


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


ALL_L2 = [kp for l2_list in KPS_HIERARCHY.values() for kp in l2_list]
L2_TO_L1 = {kp: l1 for l1, l2_list in KPS_HIERARCHY.items() for kp in l2_list}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def is_correct(record):
    return record.get("predicted_status") == "Correct" or record.get("status") == "Correct"


def build_matrices(records):
    l2_stats = {kp: {"c": 0, "t": 0} for kp in ALL_L2}
    unknown_l2 = defaultdict(int)

    for record in records:
        correct = is_correct(record)
        for kp in record["l2"]:
            if kp not in l2_stats:
                unknown_l2[kp] += 1
                continue
            l2_stats[kp]["t"] += 1
            if correct:
                l2_stats[kp]["c"] += 1

    kp2_matrix = {}
    inferred_l2 = []
    for l1, l2_list in KPS_HIERARCHY.items():
        group_correct = sum(l2_stats[kp]["c"] for kp in l2_list)
        group_total = sum(l2_stats[kp]["t"] for kp in l2_list)
        group_perf = (group_correct + 1) / (group_total + 2)

        for kp in l2_list:
            total = l2_stats[kp]["t"]
            if total > 0:
                kp2_matrix[kp] = round((l2_stats[kp]["c"] + 1) / (total + 2), 3)
            else:
                kp2_matrix[kp] = round(group_perf, 3)
                inferred_l2.append(kp)

    kp1_matrix = {
        l1: round(sum(kp2_matrix[kp] for kp in l2_list) / len(l2_list), 3)
        for l1, l2_list in KPS_HIERARCHY.items()
    }

    return kp1_matrix, kp2_matrix, inferred_l2, dict(unknown_l2)


def normalize_records(records):
    output = []
    for record in records:
        output.append({
            "q_id": record["q_id"],
            "l1": record["l1"],
            "l2": record["l2"],
            "status": "Correct" if is_correct(record) else "Incorrect",
            "sql": record["sql"],
            "thought": record["thought"],
        })
    return output


def validate(std_questions, full_data):
    expected_ids = {question["id"] for question in std_questions}
    problems = []

    for persona_data in full_data:
        records = persona_data["records"]
        ids = [record["q_id"] for record in records]
        missing = expected_ids - set(ids)
        extra = set(ids) - expected_ids
        duplicate_count = len(ids) - len(set(ids))

        if missing:
            problems.append(f"{persona_data['persona']} missing {len(missing)} ids")
        if extra:
            problems.append(f"{persona_data['persona']} has {len(extra)} extra ids")
        if duplicate_count:
            problems.append(f"{persona_data['persona']} has {duplicate_count} duplicate ids")
        if len(persona_data["kp1_matrix"]) != len(KPS_HIERARCHY):
            problems.append(f"{persona_data['persona']} kp1_matrix size mismatch")
        if len(persona_data["kp2_matrix"]) != len(ALL_L2):
            problems.append(f"{persona_data['persona']} kp2_matrix size mismatch")

    return problems


def write_report(std_questions, full_data, inferred_by_persona, unknown_by_persona, problems):
    lines = [
        "# 全量学生聚合数据报告",
        "",
        "## 输出文件",
        "",
        "- `data_student_full.json`",
        "",
        "## 聚合规则",
        "",
        "- 记录结构对齐 `data_small_test/data_student.json`。",
        "- L2 掌握度使用后验概率平滑：`(correct + 1) / (total + 2)`。",
        "- 某个 L2 没有作答数据时，使用同一 L1 知识点组的平滑表现推断。",
        "- KP1 掌握度为该 L1 下所有 L2 掌握度的平均值。",
        "",
        "## 数据量",
        "",
        f"- 标准题目数：{len(std_questions)}",
        f"- 学生画像数：{len(full_data)}",
        f"- 学生回答记录数：{sum(len(x['records']) for x in full_data)}",
        "",
        "## Correct / Incorrect 分布",
        "",
    ]

    for persona_data in full_data:
        correct = sum(1 for record in persona_data["records"] if record["status"] == "Correct")
        incorrect = sum(1 for record in persona_data["records"] if record["status"] == "Incorrect")
        lines.append(f"- `{persona_data['persona']}`: Correct {correct}, Incorrect {incorrect}")

    lines.extend(["", "## L2 缺失推断", ""])
    for persona_data in full_data:
        persona = persona_data["persona"]
        inferred = inferred_by_persona[persona]
        lines.append(f"- `{persona}`: {len(inferred)} 个 L2 使用同 L1 组数据推断")

    unknown_total = sum(sum(items.values()) for items in unknown_by_persona.values())
    lines.extend([
        "",
        "## 校验结果",
        "",
        f"- 未知 L2 标签命中次数：{unknown_total}",
        f"- 结构问题数量：{len(problems)}",
    ])
    if problems:
        lines.extend(f"- {problem}" for problem in problems)
    else:
        lines.append("- JSON 结构、题号覆盖和矩阵维度校验通过。")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    std_questions = load_json(DATA_STD)
    raw_data = load_json(DATA_RAW)
    full_data = []
    inferred_by_persona = {}
    unknown_by_persona = {}

    for persona_data in raw_data:
        records = normalize_records(persona_data["records"])
        kp1_matrix, kp2_matrix, inferred_l2, unknown_l2 = build_matrices(records)
        persona = persona_data["persona"]
        inferred_by_persona[persona] = inferred_l2
        unknown_by_persona[persona] = unknown_l2
        full_data.append({
            "persona": persona,
            "kp1_matrix": kp1_matrix,
            "kp2_matrix": kp2_matrix,
            "records": records,
        })

    problems = validate(std_questions, full_data)
    OUT_FULL.write_text(json.dumps(full_data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(std_questions, full_data, inferred_by_persona, unknown_by_persona, problems)
    print(f"wrote {len(full_data)} personas to {OUT_FULL}")
    print(f"wrote report to {REPORT}")
    if problems:
        raise SystemExit("\n".join(problems))


if __name__ == "__main__":
    main()
