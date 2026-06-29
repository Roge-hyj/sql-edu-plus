import csv
import html
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "knowledge_taxonomy.md"
DATA_STD = ROOT / "outputs" / "data_std_full.json"

OUT_L1_CSV = ROOT / "outputs" / "knowledge_coverage_l1.csv"
OUT_L2_CSV = ROOT / "outputs" / "knowledge_coverage_l2.csv"
OUT_REPORT = ROOT / "outputs" / "knowledge_coverage_report.md"
OUT_L1_SVG = ROOT / "outputs" / "knowledge_coverage_l1_chart.svg"
OUT_L2_SVG = ROOT / "outputs" / "knowledge_coverage_l2_chart.svg"
OUT_COMBINED_SVG = ROOT / "outputs" / "knowledge_coverage_chart.svg"


COLORS = {
    "KP_BASIC": "#2F6BDE",
    "KP_FILTER": "#D95F59",
    "KP_ORDER": "#7A52C7",
    "KP_AGG": "#2C9C69",
    "KP_JOIN": "#D98C2B",
    "KP_SUBQUERY": "#227C8A",
    "KP_FUNC": "#9B5C2E",
    "KP_ADVANCED": "#6C7A89",
}


def parse_taxonomy():
    lines = TAXONOMY.read_text(encoding="utf-8").splitlines()
    section = None
    current_l1 = None
    l1_items = []
    l2_items = []
    l1_seen = set()

    l1_bullet = re.compile(r"^- `([^`]+)`: (.+)$")
    l2_parent = re.compile(r"^`(KP_[A-Z_]+)`$")

    for line in lines:
        if line.startswith("## "):
            section = line.removeprefix("## ").strip()
            current_l1 = None
            continue

        if section == "L1 一级知识点":
            match = l1_bullet.match(line)
            if match:
                code, desc = match.groups()
                desc = desc.rstrip("。")
                l1_items.append({"code": code, "desc": desc})
                l1_seen.add(code)
            continue

        if section == "L2 原子知识点":
            parent_match = l2_parent.match(line.strip())
            if parent_match:
                current_l1 = parent_match.group(1)
                continue
            match = l1_bullet.match(line)
            if match and current_l1:
                code, desc = match.groups()
                desc = desc.rstrip("。")
                l2_items.append({"l1": current_l1, "code": code, "desc": desc})

    l2_codes = {item["code"] for item in l2_items}
    return l1_items, l2_items, l1_seen, l2_codes


def load_questions():
    return json.loads(DATA_STD.read_text(encoding="utf-8"))


def summarize(questions, l1_items, l2_items, l1_defined, l2_defined):
    total = len(questions)
    l1_counts = Counter(q.get("l1") for q in questions)
    l2_counts = Counter(tag for q in questions for tag in q.get("l2", []))

    unknown_l1 = sorted(code for code in l1_counts if code not in l1_defined)
    unknown_l2 = sorted(code for code in l2_counts if code not in l2_defined)

    l2_by_l1 = {}
    for item in l2_items:
        l2_by_l1.setdefault(item["l1"], []).append(item["code"])

    l1_rows = []
    for item in l1_items:
        codes = l2_by_l1.get(item["code"], [])
        covered_l2 = sum(1 for code in codes if l2_counts.get(code, 0) > 0)
        defined_l2 = len(codes)
        count = l1_counts.get(item["code"], 0)
        l1_rows.append({
            "l1": item["code"],
            "description": item["desc"],
            "question_count": count,
            "question_pct": count / total if total else 0,
            "defined_l2": defined_l2,
            "covered_l2": covered_l2,
            "l2_coverage_pct": covered_l2 / defined_l2 if defined_l2 else 0,
            "l2_occurrence_count": sum(l2_counts.get(code, 0) for code in codes),
        })

    l2_rows = []
    for item in l2_items:
        count = l2_counts.get(item["code"], 0)
        l2_rows.append({
            "l1": item["l1"],
            "l2": item["code"],
            "description": item["desc"],
            "occurrence_count": count,
            "question_pct": count / total if total else 0,
            "covered": "yes" if count else "no",
        })

    return {
        "total": total,
        "l1_rows": l1_rows,
        "l2_rows": l2_rows,
        "unknown_l1": unknown_l1,
        "unknown_l2": unknown_l2,
        "l1_counts": l1_counts,
        "l2_counts": l2_counts,
    }


def write_csv(path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def pct(value):
    return f"{value * 100:.1f}%"


def svg_text(x, y, text, size=14, weight=400, fill="#1F2933", anchor="start"):
    text = html.escape(str(text))
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-family="Inter, Arial, sans-serif" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}">{text}</text>'
    )


def write_l1_svg(rows, total, out_path):
    width = 1120
    height = 660
    left = 88
    right = 48
    top = 98
    bottom = 126
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_count = max(row["question_count"] for row in rows) or 1
    step = plot_w / len(rows)
    bar_w = min(78, step * 0.58)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        svg_text(width / 2, 42, "SQL DQL Knowledge Coverage by L1", size=28, weight=700, anchor="middle"),
        svg_text(width / 2, 70, f"Core l1 tag distribution across {total} questions", size=15, fill="#52606D", anchor="middle"),
    ]

    grid_max = ((max_count + 9) // 10) * 10
    for value in range(0, grid_max + 1, max(10, grid_max // 5)):
        y = top + plot_h - (value / grid_max) * plot_h
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#D8DEE6" stroke-width="1"/>')
        parts.append(svg_text(left - 16, y + 5, value, size=13, fill="#52606D", anchor="end"))

    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#5D6975" stroke-width="1.5"/>')
    parts.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" stroke="#5D6975" stroke-width="1.5"/>')

    for index, row in enumerate(rows):
        cx = left + step * index + step / 2
        bar_h = (row["question_count"] / grid_max) * plot_h
        x = cx - bar_w / 2
        y = top + plot_h - bar_h
        color = COLORS.get(row["l1"], "#52606D")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="5" fill="{color}"/>')
        parts.append(svg_text(cx, y - 12, row["question_count"], size=17, weight=700, fill=color, anchor="middle"))
        parts.append(svg_text(cx, top + plot_h + 34, row["l1"], size=13, weight=700, anchor="middle"))
        parts.append(svg_text(cx, top + plot_h + 56, pct(row["question_pct"]), size=13, fill="#52606D", anchor="middle"))
        parts.append(svg_text(cx, top + plot_h + 78, f"L2 {row['covered_l2']}/{row['defined_l2']}", size=12, fill="#52606D", anchor="middle"))

    parts.append(svg_text(left + plot_w / 2, height - 24, "L1 knowledge point", size=14, fill="#52606D", anchor="middle"))
    parts.append(svg_text(24, top + plot_h / 2, "Questions", size=14, fill="#52606D", anchor="middle"))
    parts.append("</svg>")
    out_path.write_text("\n".join(parts), encoding="utf-8")


def write_l2_svg(rows, total, out_path):
    row_h = 28
    top = 100
    bottom = 42
    left = 190
    right = 120
    width = 1280
    height = top + len(rows) * row_h + bottom
    plot_w = width - left - right
    max_count = max(row["occurrence_count"] for row in rows) or 1
    grid_max = ((max_count + 19) // 20) * 20

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        svg_text(width / 2, 42, "SQL DQL Atomic Knowledge Point Coverage", size=28, weight=700, anchor="middle"),
        svg_text(width / 2, 70, f"L2 tag occurrences across {total} questions; one question can cover multiple L2 tags", size=15, fill="#52606D", anchor="middle"),
    ]

    plot_top = top - 8
    plot_bottom = top + len(rows) * row_h
    for value in range(0, grid_max + 1, max(20, grid_max // 5)):
        x = left + (value / grid_max) * plot_w
        parts.append(f'<line x1="{x:.1f}" y1="{plot_top}" x2="{x:.1f}" y2="{plot_bottom}" stroke="#E5EAF0" stroke-width="1"/>')
        parts.append(svg_text(x, plot_top - 12, value, size=12, fill="#52606D", anchor="middle"))

    last_l1 = None
    for index, row in enumerate(rows):
        y = top + index * row_h
        color = COLORS.get(row["l1"], "#52606D")
        if row["l1"] != last_l1:
            parts.append(f'<line x1="48" y1="{y - 17}" x2="{width - 48}" y2="{y - 17}" stroke="#CBD4DE" stroke-width="1"/>')
            parts.append(svg_text(48, y + 5, row["l1"], size=12, weight=700, fill=color))
            last_l1 = row["l1"]

        count = row["occurrence_count"]
        bar_w = (count / grid_max) * plot_w
        parts.append(svg_text(left - 16, y + 5, row["l2"], size=12, fill="#1F2933", anchor="end"))
        if count:
            parts.append(f'<rect x="{left}" y="{y - 12}" width="{bar_w:.1f}" height="16" rx="4" fill="{color}"/>')
            parts.append(svg_text(left + bar_w + 8, y + 2, f"{count} ({pct(row['question_pct'])})", size=12, fill="#52606D"))
        else:
            parts.append(f'<circle cx="{left + 4}" cy="{y - 4}" r="3" fill="#BBC5D1"/>')
            parts.append(svg_text(left + 14, y + 2, "0", size=12, fill="#8795A1"))

    parts.append(svg_text(left + plot_w / 2, height - 16, "Occurrences", size=13, fill="#52606D", anchor="middle"))
    parts.append("</svg>")
    out_path.write_text("\n".join(parts), encoding="utf-8")


def write_combined_svg(l1_rows, l2_rows, total):
    top_l2 = sorted(l2_rows, key=lambda row: row["occurrence_count"], reverse=True)[:15]
    width = 1280
    height = 820
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        svg_text(width / 2, 42, "SQL DQL Question Knowledge Coverage", size=30, weight=700, anchor="middle"),
        svg_text(width / 2, 72, f"{total} questions from data_std_full.json, grouped by knowledge_taxonomy.md", size=15, fill="#52606D", anchor="middle"),
        svg_text(88, 124, "L1 core knowledge points", size=20, weight=700),
        svg_text(680, 124, "Top 15 L2 atomic tags", size=20, weight=700),
    ]

    l1_x = 88
    l1_y = 154
    l1_w = 500
    row_h = 48
    max_l1 = max(row["question_count"] for row in l1_rows) or 1
    for index, row in enumerate(l1_rows):
        y = l1_y + index * row_h
        color = COLORS.get(row["l1"], "#52606D")
        bar_w = (row["question_count"] / max_l1) * 330
        parts.append(svg_text(l1_x, y + 18, row["l1"], size=14, weight=700, fill=color))
        parts.append(f'<rect x="{l1_x + 112}" y="{y + 3}" width="{bar_w:.1f}" height="20" rx="5" fill="{color}"/>')
        parts.append(svg_text(l1_x + 112 + bar_w + 10, y + 19, f"{row['question_count']} / {pct(row['question_pct'])}", size=13, fill="#52606D"))
        parts.append(svg_text(l1_x + 112, y + 38, f"L2 coverage {row['covered_l2']}/{row['defined_l2']} ({pct(row['l2_coverage_pct'])})", size=12, fill="#52606D"))

    l2_x = 680
    l2_y = 154
    max_l2 = max(row["occurrence_count"] for row in top_l2) or 1
    for index, row in enumerate(top_l2):
        y = l2_y + index * 38
        color = COLORS.get(row["l1"], "#52606D")
        bar_w = (row["occurrence_count"] / max_l2) * 340
        parts.append(svg_text(l2_x, y + 16, row["l2"], size=13, weight=700, fill=color))
        parts.append(f'<rect x="{l2_x + 126}" y="{y + 2}" width="{bar_w:.1f}" height="18" rx="5" fill="{color}"/>')
        parts.append(svg_text(l2_x + 126 + bar_w + 10, y + 17, f"{row['occurrence_count']} / {pct(row['question_pct'])}", size=12, fill="#52606D"))

    covered_l2 = sum(1 for row in l2_rows if row["occurrence_count"] > 0)
    parts.append(svg_text(88, height - 62, f"Defined L2 tags covered: {covered_l2}/{len(l2_rows)} ({pct(covered_l2 / len(l2_rows))})", size=15, weight=700))
    parts.append(svg_text(88, height - 34, "See knowledge_coverage_l2_chart.svg for the full L2 distribution.", size=13, fill="#52606D"))
    parts.append("</svg>")
    OUT_COMBINED_SVG.write_text("\n".join(parts), encoding="utf-8")


def write_report(summary):
    l1_rows = summary["l1_rows"]
    l2_rows = summary["l2_rows"]
    covered_l2 = sum(1 for row in l2_rows if row["occurrence_count"] > 0)
    uncovered_l2 = [row for row in l2_rows if row["occurrence_count"] == 0]
    top_l2 = sorted(l2_rows, key=lambda row: row["occurrence_count"], reverse=True)[:20]

    lines = [
        "# data_std_full.json 知识点覆盖统计",
        "",
        f"- 题目总数：{summary['total']}",
        f"- 已覆盖 L2 原子知识点：{covered_l2}/{len(l2_rows)}（{pct(covered_l2 / len(l2_rows))}）",
        f"- 未覆盖 L2 原子知识点：{len(uncovered_l2)}",
        "- 统计口径：`l1` 按每题核心一级知识点计数；`l2` 按多标签出现次数计数，一题可覆盖多个 L2。",
        "",
        "## 图表",
        "",
        "- `knowledge_coverage_chart.svg`：总览图。",
        "- `knowledge_coverage_l1_chart.svg`：L1 核心考点分布。",
        "- `knowledge_coverage_l2_chart.svg`：完整 L2 原子知识点出现次数。",
        "",
        "## L1 核心知识点分布",
        "",
        "| L1 | 说明 | 题数 | 占比 | L2 覆盖 | L2 覆盖率 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in l1_rows:
        lines.append(
            f"| `{row['l1']}` | {row['description']} | {row['question_count']} | "
            f"{pct(row['question_pct'])} | {row['covered_l2']}/{row['defined_l2']} | {pct(row['l2_coverage_pct'])} |"
        )

    lines.extend([
        "",
        "## Top 20 L2 原子知识点",
        "",
        "| L1 | L2 | 说明 | 出现次数 | 题目占比 |",
        "| --- | --- | --- | ---: | ---: |",
    ])
    for row in top_l2:
        lines.append(
            f"| `{row['l1']}` | `{row['l2']}` | {row['description']} | "
            f"{row['occurrence_count']} | {pct(row['question_pct'])} |"
        )

    lines.extend([
        "",
        "## 未覆盖 L2 原子知识点",
        "",
        "| L1 | L2 | 说明 |",
        "| --- | --- | --- |",
    ])
    for row in uncovered_l2:
        lines.append(f"| `{row['l1']}` | `{row['l2']}` | {row['description']} |")

    if summary["unknown_l1"] or summary["unknown_l2"]:
        lines.extend([
            "",
            "## Taxonomy 外标签",
            "",
            f"- 未定义 L1：{', '.join(summary['unknown_l1']) or '无'}",
            f"- 未定义 L2：{', '.join(summary['unknown_l2']) or '无'}",
        ])

    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    l1_items, l2_items, l1_defined, l2_defined = parse_taxonomy()
    questions = load_questions()
    summary = summarize(questions, l1_items, l2_items, l1_defined, l2_defined)

    write_csv(
        OUT_L1_CSV,
        summary["l1_rows"],
        ["l1", "description", "question_count", "question_pct", "defined_l2", "covered_l2", "l2_coverage_pct", "l2_occurrence_count"],
    )
    write_csv(
        OUT_L2_CSV,
        summary["l2_rows"],
        ["l1", "l2", "description", "occurrence_count", "question_pct", "covered"],
    )
    write_l1_svg(summary["l1_rows"], summary["total"], OUT_L1_SVG)
    write_l2_svg(summary["l2_rows"], summary["total"], OUT_L2_SVG)
    write_combined_svg(summary["l1_rows"], summary["l2_rows"], summary["total"])
    write_report(summary)

    covered_l2 = sum(1 for row in summary["l2_rows"] if row["occurrence_count"] > 0)
    print(f"questions: {summary['total']}")
    print(f"covered l2: {covered_l2}/{len(summary['l2_rows'])} ({pct(covered_l2 / len(summary['l2_rows']))})")
    print(f"wrote {OUT_COMBINED_SVG}")
    print(f"wrote {OUT_L1_SVG}")
    print(f"wrote {OUT_L2_SVG}")
    print(f"wrote {OUT_L1_CSV}")
    print(f"wrote {OUT_L2_CSV}")
    print(f"wrote {OUT_REPORT}")


if __name__ == "__main__":
    main()
