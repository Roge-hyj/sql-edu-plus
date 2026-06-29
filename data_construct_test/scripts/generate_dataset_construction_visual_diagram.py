from pathlib import Path
from textwrap import wrap
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
OUT_SVG = ROOT / "outputs" / "dataset_construction_visual_pipeline.svg"

FONT = "Inter, Noto Sans CJK SC, Microsoft YaHei, Arial, sans-serif"
INK = "#172033"
MUTED = "#667085"
PAPER = "#F6F8FB"
WHITE = "#FFFFFF"
LINE = "#B8C3D3"


STEPS = [
    {
        "n": "01",
        "title": "教材 PDF 输入",
        "input": "三本 SQL / 数据库教材 PDF",
        "tool": ["人工", "来源确认", "source 追溯规则"],
        "output": "权威教材语料库",
        "icon_in": "pdf_stack",
        "icon_tool": "human_check",
        "icon_out": "book_set",
        "color": "#2563EB",
    },
    {
        "n": "02",
        "title": "全文扫描与候选题抽取",
        "input": "PDF 全文、例题、练习题、答案段落",
        "tool": ["代码", "pypdf.PdfReader", "LLM 辅助识别", "build_data_std_full.py"],
        "output": "full_scan_candidates.json + extraction_report.md",
        "icon_in": "pdf_text",
        "icon_tool": "scanner_llm",
        "icon_out": "json_doc",
        "color": "#0891B2",
    },
    {
        "n": "03",
        "title": "非 DQL 项筛除",
        "input": "全书候选题：概念题 / DDL / DML / 设计题混杂",
        "tool": ["规则 + 人工复核", "保留 SELECT / WITH / 集合查询", "排除 DDL、DML、DCL、TCL"],
        "output": "211 道可标准化 SQL DQL 题",
        "icon_in": "mixed_sql",
        "icon_tool": "filter_funnel",
        "icon_out": "sql_select",
        "color": "#D97706",
    },
    {
        "n": "04",
        "title": "L1 / L2 知识点标注",
        "input": "题干、schema、标准 SQL、source、difficulty",
        "tool": ["人工 taxonomy", "infer_tags 自动初标", "tag audit 逐题复核"],
        "output": "L1 核心知识点 + L2 原子知识点 + 难度",
        "icon_in": "sql_doc",
        "icon_tool": "tag_tree",
        "icon_out": "tagged_doc",
        "color": "#7C3AED",
    },
    {
        "n": "05",
        "title": "标准题库构建",
        "input": "规范化题目记录：id / q / ans_sql / schema / source",
        "tool": ["代码", "JSON schema 校验", "空字段检查", "SQL 清洗"],
        "output": "data_std_full.json：211 道标准题",
        "icon_in": "tagged_doc",
        "icon_tool": "code_schema",
        "icon_out": "database_json",
        "color": "#16A34A",
    },
    {
        "n": "06",
        "title": "四类学生作答模拟",
        "input": "data_std_full.json + 标准答案 + L1/L2 标签",
        "tool": ["LLM / AI", "Newbie", "Basic_Filter", "Agg_Join_Struggler", "Logic_Master"],
        "output": "data_student_raw_full.json：4 x 211 条 SQL 作答",
        "icon_in": "database_json",
        "icon_tool": "ai_students",
        "icon_out": "answer_stack",
        "color": "#DC2626",
    },
    {
        "n": "07",
        "title": "知识点掌握矩阵聚合",
        "input": "学生 SQL、Correct / Incorrect、错因、L1/L2 标签",
        "tool": ["代码统计", "(correct + 1) / (total + 2)", "KP1 / KP2 聚合"],
        "output": "data_student_full.json：records + kp1_matrix + kp2_matrix",
        "icon_in": "answer_stack",
        "icon_tool": "matrix_calc",
        "icon_out": "heatmap_matrix",
        "color": "#475569",
    },
    {
        "n": "08",
        "title": "20 题初始诊断集",
        "input": "data_std_full.json + L1/L2 覆盖 + 难度分布",
        "tool": ["人工筛选", "select_initial_diagnostic_20.py", "覆盖校验", "题干局部改写"],
        "output": "initial_diagnostic_20.json：覆盖已出现 L2 的 33/33",
        "icon_in": "database_json",
        "icon_tool": "diagnostic_select",
        "icon_out": "diagnostic_form",
        "color": "#9333EA",
    },
]


def esc(value):
    return escape(str(value))


def text(x, y, value, size=20, weight=500, fill=INK, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{FONT}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}">{esc(value)}</text>'
    )


def multiline(x, y, value, width_chars=24, line_h=24, size=18, weight=500, fill=INK, anchor="start"):
    lines = []
    for raw in str(value).split("\n"):
        lines.extend(wrap(raw, width=width_chars) or [""])
    out = []
    for i, line in enumerate(lines):
        out.append(text(x, y + i * line_h, line, size=size, weight=weight, fill=fill, anchor=anchor))
    return out


def rect(x, y, w, h, fill=WHITE, stroke=LINE, sw=1.5, rx=16, dash=None, opacity=1):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"{dash_attr}/>'
    )


def line(x1, y1, x2, y2, color="#64748B", sw=3, dash=None, arrow=True):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    marker = ' marker-end="url(#arrow)"' if arrow else ""
    return f'<path d="M {x1} {y1} L {x2} {y2}" fill="none" stroke="{color}" stroke-width="{sw}"{dash_attr}{marker}/>'


def path(d, color="#64748B", sw=3, dash=None, arrow=True):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    marker = ' marker-end="url(#arrow)"' if arrow else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}"{dash_attr}{marker}/>'


def circle(cx, cy, r, fill, stroke="none", sw=0):
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'


def label_box(x, y, value, color, w=250):
    parts = [rect(x - w / 2, y - 22, w, 36, fill="#FFFFFF", stroke=color, sw=1.4, rx=18)]
    parts.extend(multiline(x, y, value, width_chars=22, line_h=17, size=13, weight=700, fill=INK, anchor="middle"))
    return parts


def doc_icon(x, y, w, h, color, title, lines=None, folded=True):
    lines = lines or []
    parts = [
        rect(x, y, w, h, fill="#FFFFFF", stroke=color, sw=2.2, rx=10),
    ]
    if folded:
        parts.append(f'<path d="M {x+w-46} {y} L {x+w} {y+46} L {x+w-46} {y+46} Z" fill="#EAF2FF" stroke="{color}" stroke-width="1.5"/>')
    parts.append(text(x + 18, y + 38, title, size=20, weight=850, fill=color))
    yy = y + 72
    for ln in lines:
        parts.append(rect(x + 18, yy, w - 50, 9, fill="#D8E2EF", stroke="#D8E2EF", sw=0, rx=5))
        yy += 24
    return parts


def cylinder(x, y, w, h, color, label):
    parts = [
        f'<ellipse cx="{x+w/2}" cy="{y+28}" rx="{w/2}" ry="28" fill="#FFFFFF" stroke="{color}" stroke-width="2.2"/>',
        f'<path d="M {x} {y+28} L {x} {y+h-28} C {x} {y+h+10} {x+w} {y+h+10} {x+w} {y+h-28} L {x+w} {y+28}" fill="#FFFFFF" stroke="{color}" stroke-width="2.2"/>',
        f'<ellipse cx="{x+w/2}" cy="{y+h-28}" rx="{w/2}" ry="28" fill="#F2FFF5" stroke="{color}" stroke-width="2.2"/>',
        text(x + w / 2, y + h / 2 + 8, label, size=18, weight=850, fill=color, anchor="middle"),
    ]
    return parts


def icon(kind, x, y, color):
    parts = []
    if kind == "pdf_stack":
        parts.extend(doc_icon(x + 18, y + 16, 92, 124, color, "PDF", ["", "", ""]))
        parts.extend(doc_icon(x + 62, y + 42, 92, 124, color, "PDF", ["", "", ""]))
        parts.extend(doc_icon(x + 106, y + 68, 92, 124, color, "PDF", ["", "", ""]))
    elif kind == "book_set":
        for i, fill in enumerate(["#DBEAFE", "#E0F2FE", "#EDE9FE"]):
            parts.append(rect(x + 34 + i * 52, y + 54, 42, 132, fill=fill, stroke=color, sw=2, rx=8))
            parts.append(text(x + 55 + i * 52, y + 132, f"B{i+1}", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "pdf_text":
        parts.extend(doc_icon(x + 52, y + 30, 150, 160, color, "PDF", ["", "", "", ""]))
        parts.append(f'<path d="M {x+22} {y+110} L {x+50} {y+110}" stroke="{color}" stroke-width="8" stroke-linecap="round"/>')
    elif kind == "scanner_llm":
        parts.append(rect(x + 20, y + 116, 210, 44, fill="#ECFEFF", stroke=color, sw=2.2, rx=12))
        parts.append(f'<path d="M {x+48} {y+116} L {x+202} {y+54}" stroke="{color}" stroke-width="3" opacity="0.45"/>')
        parts.append(rect(x + 72, y + 38, 110, 76, fill="#FFFFFF", stroke=color, sw=2.2, rx=18))
        parts.append(circle(x + 106, y + 76, 8, color))
        parts.append(circle(x + 148, y + 76, 8, color))
        parts.append(f'<path d="M {x+112} {y+96} Q {x+127} {y+106} {x+144} {y+96}" fill="none" stroke="{color}" stroke-width="3"/>')
        parts.append(text(x + 127, y + 185, "LLM + Code", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "json_doc":
        parts.extend(doc_icon(x + 45, y + 28, 160, 170, color, "JSON", ["", "", "", ""]))
        parts.append(text(x + 125, y + 142, "{ }", size=38, weight=850, fill=color, anchor="middle"))
    elif kind == "mixed_sql":
        labels = [("SELECT", "#DCFCE7"), ("DDL", "#FFE4E6"), ("DML", "#FFE4E6"), ("概念", "#FEF3C7"), ("ER", "#FEF3C7")]
        for i, (lb, fill) in enumerate(labels):
            parts.append(rect(x + 34 + (i % 2) * 96, y + 42 + (i // 2) * 54, 84, 34, fill=fill, stroke=color, sw=1.8, rx=17))
            parts.append(text(x + 76 + (i % 2) * 96, y + 65 + (i // 2) * 54, lb, size=15, weight=850, fill=INK, anchor="middle"))
    elif kind == "filter_funnel":
        parts.append(f'<path d="M {x+34} {y+42} H {x+216} L {x+145} {y+122} V {y+178} L {x+105} {y+198} V {y+122} Z" fill="#FFF7ED" stroke="{color}" stroke-width="3"/>')
        parts.append(text(x + 125, y + 92, "DQL", size=26, weight=900, fill=color, anchor="middle"))
        parts.append(text(x + 125, y + 222, "filter", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "sql_select":
        parts.extend(doc_icon(x + 38, y + 38, 174, 154, color, "SQL", ["", "", ""]))
        parts.append(rect(x + 68, y + 116, 116, 32, fill="#DCFCE7", stroke=color, sw=1.6, rx=16))
        parts.append(text(x + 126, y + 138, "SELECT", size=16, weight=900, fill=color, anchor="middle"))
    elif kind == "sql_doc":
        parts.extend(doc_icon(x + 44, y + 32, 162, 166, color, "SQL", ["", "", "", ""]))
        parts.append(text(x + 125, y + 150, "schema", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "tag_tree":
        parts.append(circle(x + 125, y + 52, 28, "#F5F3FF", color, 3))
        parts.append(text(x + 125, y + 60, "L1", size=20, weight=900, fill=color, anchor="middle"))
        for i, dx in enumerate([-66, 0, 66]):
            parts.append(line(x + 125, y + 82, x + 125 + dx, y + 124, color=color, sw=2, arrow=False))
            parts.append(circle(x + 125 + dx, y + 152, 30, "#FFFFFF", color, 2.5))
            parts.append(text(x + 125 + dx, y + 160, f"L2-{i+1}", size=14, weight=850, fill=color, anchor="middle"))
    elif kind == "tagged_doc":
        parts.extend(doc_icon(x + 42, y + 28, 166, 170, color, "DQL", ["", "", ""]))
        for i, lb in enumerate(["KP", "L2", "diff"]):
            parts.append(rect(x + 62 + i * 48, y + 144, 40, 26, fill="#F5F3FF", stroke=color, sw=1.5, rx=13))
            parts.append(text(x + 82 + i * 48, y + 162, lb, size=12, weight=900, fill=color, anchor="middle"))
    elif kind == "code_schema":
        parts.append(rect(x + 32, y + 48, 190, 132, fill="#F8FAFC", stroke=color, sw=2.2, rx=12))
        parts.append(text(x + 52, y + 88, "build.py", size=20, weight=850, fill=color))
        parts.append(text(x + 52, y + 122, "{ schema }", size=18, weight=850, fill=INK))
        parts.append(text(x + 52, y + 154, "validate", size=18, weight=850, fill=INK))
    elif kind == "database_json":
        parts.extend(cylinder(x + 40, y + 46, 170, 150, color, "JSON"))
        parts.append(text(x + 125, y + 222, "data_std", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "ai_students":
        parts.append(rect(x + 36, y + 42, 178, 74, fill="#FFFFFF", stroke=color, sw=2.2, rx=20))
        parts.append(text(x + 125, y + 88, "AI / LLM", size=20, weight=900, fill=color, anchor="middle"))
        for i, dx in enumerate([42, 82, 122, 162]):
            parts.append(circle(x + dx, y + 160, 22, "#FFFFFF", color, 2.2))
            parts.append(rect(x + dx - 24, y + 186, 48, 30, fill="#FEE2E2", stroke=color, sw=1.8, rx=14))
    elif kind == "answer_stack":
        for i in range(4):
            parts.extend(doc_icon(x + 36 + i * 22, y + 42 + i * 18, 128, 112, color, "SQL", ["", ""]))
        parts.append(text(x + 128, y + 224, "844 answers", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "matrix_calc":
        parts.append(rect(x + 38, y + 42, 174, 120, fill="#FFFFFF", stroke=color, sw=2.2, rx=14))
        parts.append(text(x + 125, y + 82, "calc", size=22, weight=900, fill=color, anchor="middle"))
        parts.append(text(x + 125, y + 122, "(c+1)/(t+2)", size=18, weight=850, fill=INK, anchor="middle"))
        parts.append(text(x + 125, y + 190, "KP1 / KP2", size=18, weight=850, fill=color, anchor="middle"))
    elif kind == "heatmap_matrix":
        cell = 24
        fills = ["#DCFCE7", "#BBF7D0", "#86EFAC", "#FDE68A", "#FCA5A5"]
        for r in range(5):
            for c in range(6):
                parts.append(rect(x + 48 + c * (cell + 6), y + 48 + r * (cell + 6), cell, cell, fill=fills[(r + c) % len(fills)], stroke="#FFFFFF", sw=1, rx=5))
        parts.append(text(x + 125, y + 222, "mastery matrix", size=17, weight=850, fill=color, anchor="middle"))
    elif kind == "diagnostic_select":
        parts.append(circle(x + 126, y + 112, 80, "#F5F3FF", color, 3))
        parts.append(f'<path d="M {x+126} {y+50} L {x+143} {y+100} L {x+196} {y+100} L {x+153} {y+132} L {x+170} {y+184} L {x+126} {y+152} L {x+82} {y+184} L {x+99} {y+132} L {x+56} {y+100} L {x+109} {y+100} Z" fill="#FFFFFF" stroke="{color}" stroke-width="2.5"/>')
        parts.append(text(x + 126, y + 124, "20", size=32, weight=900, fill=color, anchor="middle"))
    elif kind == "diagnostic_form":
        parts.extend(doc_icon(x + 42, y + 28, 166, 178, color, "TEST", ["", "", "", ""]))
        for i in range(3):
            parts.append(circle(x + 74, y + 106 + i * 28, 7, "#FFFFFF", color, 2))
            parts.append(f'<path d="M {x+70} {y+106+i*28} L {x+74} {y+111+i*28} L {x+83} {y+100+i*28}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    else:
        parts.append(rect(x + 34, y + 34, 180, 180, fill="#FFFFFF", stroke=color, sw=2.2, rx=20))
    return parts


def artifact_panel(step, x, y):
    color = step["color"]
    parts = [
        f'<g id="step-{step["n"]}">',
        rect(x, y, 1180, 330, fill=WHITE, stroke="#D7DEE9", sw=1.5, rx=24),
        f'<rect x="{x}" y="{y}" width="10" height="330" rx="5" fill="{color}"/>',
        circle(x + 48, y + 42, 28, color),
        text(x + 48, y + 51, step["n"], size=18, weight=900, fill=WHITE, anchor="middle"),
        text(x + 90, y + 50, step["title"], size=25, weight=900, fill=INK),
    ]

    parts.append(rect(x + 36, y + 86, 250, 220, fill="#F8FAFC", stroke="#E2E8F0", sw=1.2, rx=18))
    parts.extend(icon(step["icon_in"], x + 36, y + 86, color))
    parts.append(rect(x + 466, y + 86, 250, 220, fill="#F8FAFC", stroke="#E2E8F0", sw=1.2, rx=18))
    parts.extend(icon(step["icon_tool"], x + 466, y + 86, color))
    parts.append(rect(x + 896, y + 86, 250, 220, fill="#F8FAFC", stroke="#E2E8F0", sw=1.2, rx=18))
    parts.extend(icon(step["icon_out"], x + 896, y + 86, color))

    parts.append(line(x + 286, y + 198, x + 466, y + 198, color=color, sw=3))
    parts.append(line(x + 716, y + 198, x + 896, y + 198, color=color, sw=3))
    parts.extend(label_box(x + 376, y + 160, "输入：" + step["input"], color, w=330))
    parts.extend(label_box(x + 806, y + 160, "输出：" + step["output"], color, w=350))

    tool_y = y + 118
    parts.append(text(x + 592, y + 108, "处理工具", size=14, weight=900, fill=color, anchor="middle"))
    for item in step["tool"][:4]:
        parts.append(text(x + 592, tool_y + 30, item, size=14, weight=750, fill=INK, anchor="middle"))
        tool_y += 23
    parts.append("</g>")
    return parts


def main():
    width, height = 2760, 2300
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748B"/>',
        "</marker>",
        '<filter id="shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="8" stdDeviation="10" flood-color="#1F2937" flood-opacity="0.10"/></filter>',
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="{PAPER}"/>',
        text(width / 2, 70, "SQL DQL 评测数据集构建流程", size=42, weight=900, fill=INK, anchor="middle"),
        text(width / 2, 112, "具象化展示每一步的数据形态、处理工具和输出产物", size=22, weight=650, fill=MUTED, anchor="middle"),
        rect(180, 144, 2400, 58, fill="#111827", stroke="#111827", sw=0, rx=29),
        text(width / 2, 182, "3 本教材 PDF  ->  211 道标准 DQL 题  ->  844 条学生作答  ->  KP1/KP2 掌握矩阵  ->  20 题诊断集", size=21, weight=850, fill=WHITE, anchor="middle"),
    ]

    left_x, right_x = 130, 1450
    top_y = 250
    row_gap = 386
    coords = []
    for i, step in enumerate(STEPS):
        col = 0 if i % 2 == 0 else 1
        row = i // 2
        x = left_x if col == 0 else right_x
        y = top_y + row * row_gap
        coords.append((x, y))
        parts.append(f'<g filter="url(#shadow)">')
        parts.extend(artifact_panel(step, x, y))
        parts.append("</g>")

    for i in range(len(STEPS) - 1):
        x, y = coords[i]
        nx, ny = coords[i + 1]
        if i % 2 == 0:
            parts.append(path(f"M {x+1180} {y+123} C {x+1260} {y+123}, {nx-80} {ny+123}, {nx} {ny+123}", color="#64748B", sw=3))
        else:
            parts.append(path(f"M {x} {y+330} C {x-80} {y+382}, {nx+1260} {ny-52}, {nx+1180} {ny}", color="#64748B", sw=3))
        lx = (x + nx + 1180) / 2
        ly = (y + ny) / 2 + 112
        parts.extend(label_box(lx, ly, "进入下一步的数据基准", "#64748B", w=240))

    qc_y = 1818
    parts.append(rect(130, qc_y, 1180, 230, fill=WHITE, stroke="#EF4444", sw=2, rx=24, dash="10 8"))
    parts.append(text(170, qc_y + 46, "质量控制与科研复核", size=28, weight=900, fill="#B91C1C"))
    qc_items = [
        "source 可追溯：教材名、章节、页码或练习编号",
        "JSON schema 校验：字段类型、空字段、题号覆盖",
        "SQL 清洗：移除解释文本，排除 CREATE VIEW 等非 DQL",
        "标签审计：L1/L2 覆盖统计，逐题 tag audit",
        "诊断集验证：20 题覆盖已出现 L2 的 33/33",
    ]
    for i, item in enumerate(qc_items):
        parts.append(circle(178, qc_y + 88 + i * 28, 8, "#FEE2E2", "#EF4444", 2))
        parts.append(f'<path d="M {174} {qc_y+88+i*28} L {178} {qc_y+93+i*28} L {187} {qc_y+82+i*28}" fill="none" stroke="#EF4444" stroke-width="2.2"/>')
        parts.append(text(204, qc_y + 94 + i * 28, item, size=18, weight=650, fill=INK))

    final_y = 1818
    parts.append(rect(1450, final_y, 1180, 230, fill=WHITE, stroke="#16A34A", sw=2, rx=24))
    parts.append(text(1490, final_y + 46, "最终产物", size=28, weight=900, fill="#15803D"))
    finals = [
        ("data_std_full.json", "全量标准题库；后续模拟与诊断共同基准"),
        ("data_student_raw_full.json", "四类学生原始 SQL 作答记录"),
        ("data_student_full.json", "records + kp1_matrix + kp2_matrix"),
        ("initial_diagnostic_20.json", "20 道初始能力诊断关键题"),
    ]
    for i, (name, desc) in enumerate(finals):
        yy = final_y + 84 + i * 34
        parts.append(rect(1492, yy - 19, 280, 26, fill="#DCFCE7", stroke="#86EFAC", sw=1.2, rx=13))
        parts.append(text(1632, yy, name, size=15, weight=900, fill="#166534", anchor="middle"))
        parts.append(text(1794, yy, desc, size=17, weight=650, fill=INK))

    parts.append(text(130, 2190, "图注：L2=56 表示知识体系中定义的原子知识点总数；当前全量题库实际出现 L2 为 33 个，20 题诊断集覆盖已出现 L2 的 33/33。", size=19, weight=600, fill=MUTED))
    parts.append("</svg>")
    OUT_SVG.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT_SVG}")


if __name__ == "__main__":
    main()
