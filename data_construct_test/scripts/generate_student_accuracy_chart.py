import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_STUDENT = ROOT / "outputs" / "data_student_full.json"
OUT_SVG = ROOT / "outputs" / "student_persona_accuracy_chart.svg"
OUT_CSV = ROOT / "outputs" / "student_persona_accuracy.csv"


ROLE_LABELS = {
    "Newbie": "Newbie",
    "Basic_Filter_Student": "Basic Filter",
    "Agg_Join_Struggler": "Agg/Join Struggler",
    "Logic_Master": "Logic Master",
}


BAR_COLORS = {
    "Newbie": "#D95F59",
    "Basic_Filter_Student": "#E5A53A",
    "Agg_Join_Struggler": "#3A8DDE",
    "Logic_Master": "#2C9C69",
}


def load_data():
    return json.loads(DATA_STUDENT.read_text(encoding="utf-8"))


def accuracy_rows(data):
    rows = []
    for persona_data in data:
        records = persona_data["records"]
        correct = sum(1 for record in records if record["status"] == "Correct")
        total = len(records)
        rows.append({
            "persona": persona_data["persona"],
            "label": ROLE_LABELS.get(persona_data["persona"], persona_data["persona"]),
            "correct": correct,
            "total": total,
            "accuracy": correct / total if total else 0,
        })
    return rows


def write_csv(rows):
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["persona", "label", "correct", "total", "accuracy"])
        writer.writeheader()
        writer.writerows(rows)


def svg_text(x, y, text, size=16, weight=400, fill="#1F2933", anchor="middle"):
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-family="Inter, Arial, sans-serif" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}">{text}</text>'
    )


def write_svg(rows):
    width = 1040
    height = 620
    left = 96
    right = 56
    top = 92
    bottom = 120
    plot_w = width - left - right
    plot_h = height - top - bottom
    bar_w = 104
    group_gap = plot_w / len(rows)
    axis_color = "#5D6975"
    grid_color = "#D8DEE6"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        svg_text(width / 2, 42, "Simulated Student Persona Accuracy", size=28, weight=700),
        svg_text(width / 2, 70, "Correct answers over 211 SQL DQL questions", size=15, fill="#52606D"),
    ]

    for value in [0, 25, 50, 75, 100]:
        y = top + plot_h - (value / 100) * plot_h
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="{grid_color}" stroke-width="1"/>')
        parts.append(svg_text(left - 18, y + 5, f"{value}%", size=13, fill="#52606D", anchor="end"))

    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="{axis_color}" stroke-width="1.5"/>')
    parts.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" stroke="{axis_color}" stroke-width="1.5"/>')

    for index, row in enumerate(rows):
        center_x = left + group_gap * index + group_gap / 2
        bar_h = row["accuracy"] * plot_h
        x = center_x - bar_w / 2
        y = top + plot_h - bar_h
        color = BAR_COLORS.get(row["persona"], "#52606D")
        percent = f"{row['accuracy'] * 100:.1f}%"
        count = f"{row['correct']}/{row['total']}"

        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{bar_h:.1f}" rx="6" fill="{color}"/>')
        parts.append(svg_text(center_x, y - 12, percent, size=18, weight=700, fill=color))
        parts.append(svg_text(center_x, top + plot_h + 34, row["label"], size=15, weight=700))
        parts.append(svg_text(center_x, top + plot_h + 58, count, size=14, fill="#52606D"))

    parts.append(svg_text(left + plot_w / 2, height - 32, "Persona", size=14, fill="#52606D"))
    parts.append(svg_text(24, top + plot_h / 2, "Accuracy", size=14, fill="#52606D", anchor="middle"))
    parts.append("</svg>")
    OUT_SVG.write_text("\n".join(parts), encoding="utf-8")


def main():
    rows = accuracy_rows(load_data())
    write_csv(rows)
    write_svg(rows)
    for row in rows:
        print(f"{row['persona']}: {row['accuracy'] * 100:.1f}% ({row['correct']}/{row['total']})")
    print(f"wrote chart to {OUT_SVG}")
    print(f"wrote csv to {OUT_CSV}")


if __name__ == "__main__":
    main()
