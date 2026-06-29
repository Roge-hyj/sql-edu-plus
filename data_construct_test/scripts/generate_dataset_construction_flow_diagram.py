from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
OUT_SVG = ROOT / "outputs" / "dataset_construction_flow_detailed.svg"

FONT = "Inter, Noto Sans CJK SC, Microsoft YaHei, Arial, sans-serif"
INK = "#1F2937"
MUTED = "#667085"
LINE = "#CBD5E1"
PAPER = "#F8FAFC"
WHITE = "#FFFFFF"


STAGES = [
    {
        "n": "01",
        "title": "教材语料输入",
        "input": ["source_pdfs/ 下 3 本教材 PDF", "练习题与参考答案章节", "保留书名、版本、source 字段"],
        "tool": ["人工确认权威来源", "建立文件名与来源追溯规则", "限定数据域为 SQL / 数据库教材"],
        "output": ["Database System Concepts 7e", "Fundamentals of Database Systems", "Learn SQL Fast"],
        "color": "#2563EB",
    },
    {
        "n": "02",
        "title": "全文扫描与题目抽取",
        "input": ["三本 PDF 全文文本", "练习题、答案段落、上下文说明", "可能包含非 SQL 查询材料"],
        "tool": ["pypdf.PdfReader 文本抽取", "pdf_question_extraction_prompt.md", "build_data_std_full.py 初步汇总"],
        "output": ["full_scan_candidates.json", "extraction_report.md", "原始候选题及来源记录"],
        "color": "#0E7490",
    },
    {
        "n": "03",
        "title": "DQL 题目筛选",
        "input": ["全书扫描候选题", "题干、参考 SQL、schema、source", "概念题 / DDL / DML 混杂项"],
        "tool": ["保留 SELECT / WITH / 集合查询", "排除 DDL、DML、DCL、TCL", "清洗答案 SQL 与题目字段"],
        "output": ["SQL DQL 候选题集合", "211 道可标准化题目", "规范化 q / ans_sql / schema"],
        "color": "#B45309",
    },
    {
        "n": "04",
        "title": "L1 / L2 知识点标注",
        "input": ["DQL 题目与标准答案 SQL", "schema、source、difficulty", "知识点标签候选"],
        "tool": ["knowledge_taxonomy.md", "infer_tags 自动初标", "data_std_full_tag_audit 逐题复核"],
        "output": ["8 个 L1 核心知识点", "56 个 L2 原子知识点", "difficulty 1.0-10.0"],
        "color": "#7C3AED",
    },
    {
        "n": "05",
        "title": "标准题库构建",
        "input": ["规范化题目记录", "id、schema、q、ans_sql、source", "L1 / L2 / difficulty"],
        "tool": ["build_data_std_full.py", "question_dataset.schema.json", "来源、空字段、字段类型检查"],
        "output": ["data_std_full.json", "211 道标准 SQL DQL 题", "可复现实验基准题库"],
        "color": "#15803D",
    },
    {
        "n": "06",
        "title": "四类学生作答模拟",
        "input": ["data_std_full.json", "标准答案 SQL 与知识点标签", "学生画像与错误模式设定"],
        "tool": ["外部 AI 学生模拟", "Newbie / Basic_Filter_Student", "Agg_Join_Struggler / Logic_Master"],
        "output": ["data_student_raw_full.json", "4 x 211 条学生 SQL 作答", "正确性、错因、原始回答记录"],
        "color": "#DC2626",
    },
    {
        "n": "07",
        "title": "知识点掌握矩阵",
        "input": ["data_student_raw_full.json", "学生 SQL、正确性、错因", "题目 L1 / L2 标签"],
        "tool": ["build_data_student_full.py", "按 L1 / L2 聚合正确率", "平滑生成 kp1_matrix / kp2_matrix"],
        "output": ["data_student_full.json", "records + kp1_matrix", "kp2_matrix 覆盖 56 个原子点"],
        "color": "#475569",
    },
    {
        "n": "08",
        "title": "20 题初始诊断集",
        "input": ["data_std_full.json", "全量 L1 / L2 覆盖统计", "难度梯度与代表性要求"],
        "tool": ["人工筛选关键题", "select_initial_diagnostic_20.py", "覆盖校验与题干局部改写"],
        "output": ["initial_diagnostic_20.json", "initial_diagnostic_20_report.md", "面向前端诊断的 20 题集合"],
        "color": "#9333EA",
    },
]


def attr(value):
    return escape(str(value), {'"': "&quot;"})


def text(x, y, value, size=18, weight=400, fill=INK, anchor="start", opacity=1):
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{FONT}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" opacity="{opacity}">'
        f"{escape(str(value))}</text>"
    )


def rect(x, y, w, h, fill=WHITE, stroke=LINE, sw=1.2, rx=8, opacity=1, dash=None):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"{dash_attr}/>'
    )


def line(x1, y1, x2, y2, color="#64748B", sw=2, dash=None, arrow=True):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    marker = ' marker-end="url(#arrow)"' if arrow else ""
    return f'<path d="M {x1} {y1} L {x2} {y2}" fill="none" stroke="{color}" stroke-width="{sw}"{dash_attr}{marker}/>'


def poly(points, color="#64748B", sw=2, dash=None, arrow=True):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    marker = ' marker-end="url(#arrow)"' if arrow else ""
    path = "M " + " L ".join(f"{x} {y}" for x, y in points)
    return f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{sw}"{dash_attr}{marker}/>'


def section(parts, x, y, label, items, color, width):
    parts.append(rect(x, y, 72, 22, fill=color, stroke=color, sw=0, rx=11, opacity=0.11))
    parts.append(text(x + 36, y + 16, label, size=12, weight=800, fill=color, anchor="middle"))
    cursor = y + 40
    for item in items:
        parts.append(f'<circle cx="{x + 5}" cy="{cursor - 5}" r="2.6" fill="{color}" opacity="0.75"/>')
        parts.append(text(x + 15, cursor, item, size=13.2, fill=INK))
        cursor += 20
    parts.append(line(x, cursor + 2, x + width, cursor + 2, color="#E2E8F0", sw=1, arrow=False))
    return cursor + 18


def stage_card(stage, x, y, w, h):
    color = stage["color"]
    parts = [
        f'<g id="{attr("stage-" + stage["n"])}">',
        rect(x, y, w, h, fill=WHITE, stroke="#D0D7E2", sw=1.4, rx=8),
        rect(x, y, w, 58, fill=color, stroke=color, sw=0, rx=8, opacity=0.10),
        f'<rect x="{x}" y="{y}" width="7" height="{h}" rx="3.5" fill="{color}"/>',
        f'<circle cx="{x + 38}" cy="{y + 29}" r="18" fill="{color}"/>',
        text(x + 38, y + 35, stage["n"], size=14, weight=800, fill=WHITE, anchor="middle"),
        text(x + 66, y + 35, stage["title"], size=18, weight=800, fill=color),
    ]
    cursor = y + 82
    content_x = x + 28
    content_w = w - 56
    cursor = section(parts, content_x, cursor, "输入", stage["input"], color, content_w)
    cursor = section(parts, content_x, cursor, "工具", stage["tool"], color, content_w)
    section(parts, content_x, cursor, "输出", stage["output"], color, content_w)
    parts.append("</g>")
    return parts


def label_badge(x, y, value):
    parts = [
        rect(x - 82, y - 18, 164, 28, fill="#F1F5F9", stroke="#CBD5E1", sw=1, rx=14),
        text(x, y, value, size=12.5, weight=700, fill=MUTED, anchor="middle"),
    ]
    return parts


def main():
    width = 2200
    height = 1320
    card_w = 490
    card_h = 386
    xs = [70, 600, 1130, 1660]
    top_y = 185
    bottom_y = 705

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748B"/>',
        "</marker>",
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="{PAPER}"/>',
        '<path d="M 70 126 L 2130 126" stroke="#E2E8F0" stroke-width="1"/>',
        text(width / 2, 64, "SQL DQL 评测数据集构建流程", size=34, weight=850, anchor="middle"),
        text(
            width / 2,
            101,
            "Dataset construction pipeline with explicit Input - Tool - Output specification and reproducibility checks",
            size=17,
            fill=MUTED,
            anchor="middle",
        ),
    ]

    for i, stage in enumerate(STAGES[:4]):
        parts.extend(stage_card(stage, xs[i], top_y, card_w, card_h))
    for i, stage in enumerate(STAGES[4:]):
        parts.extend(stage_card(stage, xs[i], bottom_y, card_w, card_h))

    center_y_top = top_y + card_h / 2
    center_y_bottom = bottom_y + card_h / 2

    for i in range(3):
        parts.append(line(xs[i] + card_w + 14, center_y_top, xs[i + 1] - 14, center_y_top))
    parts.extend(label_badge((xs[0] + xs[1] + card_w) / 2, center_y_top - 20, "extract"))
    parts.extend(label_badge((xs[1] + xs[2] + card_w) / 2, center_y_top - 20, "filter"))
    parts.extend(label_badge((xs[2] + xs[3] + card_w) / 2, center_y_top - 20, "annotate"))

    parts.append(
        poly(
            [
                (xs[3] + card_w / 2, top_y + card_h + 12),
                (xs[3] + card_w / 2, 632),
                (xs[0] + card_w / 2, 632),
                (xs[0] + card_w / 2, bottom_y - 14),
            ]
        )
    )
    parts.extend(label_badge(1115, 621, "canonical write"))

    parts.append(line(xs[0] + card_w + 14, center_y_bottom, xs[1] - 14, center_y_bottom))
    parts.append(line(xs[1] + card_w + 14, center_y_bottom, xs[2] - 14, center_y_bottom))
    parts.extend(label_badge((xs[0] + xs[1] + card_w) / 2, center_y_bottom - 20, "simulate"))
    parts.extend(label_badge((xs[1] + xs[2] + card_w) / 2, center_y_bottom - 20, "aggregate"))

    parts.append(
        poly(
            [
                (xs[0] + card_w / 2, bottom_y + card_h + 12),
                (xs[0] + card_w / 2, 1168),
                (xs[3] + card_w / 2, 1168),
                (xs[3] + card_w / 2, bottom_y + card_h + 12),
            ],
            dash="8 7",
        )
    )
    parts.extend(label_badge(1115, 1157, "diagnostic branch from standard set"))

    qc_x = 70
    qc_y = 1195
    qc_w = 2060
    qc_h = 94
    parts.extend(
        [
            rect(qc_x, qc_y, qc_w, qc_h, fill=WHITE, stroke="#94A3B8", sw=1.4, rx=8, dash="8 7"),
            f'<rect x="{qc_x}" y="{qc_y}" width="7" height="{qc_h}" rx="3.5" fill="#0F172A"/>',
            text(qc_x + 28, qc_y + 34, "质量控制与科研复核层", size=20, weight=850, fill="#0F172A"),
            text(
                qc_x + 28,
                qc_y + 67,
                "source 可追溯  |  JSON schema 校验  |  SQL 答案清洗  |  L1/L2 覆盖统计  |  tag audit 逐题审计  |  难度梯度与 20 题覆盖验证",
                size=16,
                fill=MUTED,
            ),
        ]
    )

    for target in [
        (xs[1] + card_w / 2, top_y + card_h),
        (xs[2] + card_w / 2, top_y + card_h),
        (xs[0] + card_w / 2, bottom_y + card_h),
        (xs[2] + card_w / 2, bottom_y + card_h),
        (xs[3] + card_w / 2, bottom_y + card_h),
    ]:
        parts.append(line(qc_x + qc_w / 2, qc_y, target[0], target[1] + 6, color="#94A3B8", sw=1.5, dash="6 7"))

    parts.append("</svg>")
    OUT_SVG.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT_SVG}")


if __name__ == "__main__":
    main()
