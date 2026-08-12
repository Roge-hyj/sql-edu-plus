import json
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))
sys.path.insert(0, str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from run_online_random250_structure_generation_tests import evaluate_case

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
DOCS_DIR = PROJECT_ROOT / "docs"

CATEGORIES = [
    "SELECT", "DISTINCT", "WHERE", "Comparison", "NULL", "IN / BETWEEN / LIKE",
    "Logic", "JOIN", "JOIN ON", "GROUP BY", "HAVING", "Aggregate", "ORDER BY",
    "LIMIT / OFFSET", "Subquery", "Correlated Subquery", "CTE", "Recursive CTE",
    "Set Operation", "CASE", "Window", "Dialect Boundary"
]

def normalize_structure(s):
    if s in ["IN", "BETWEEN", "LIKE"]: return "IN / BETWEEN / LIKE"
    return s

def analyze_results(results):
    by_structure = defaultdict(lambda: {"total": 0, "struct_pass": 0, "gen_pass": 0, "mut_pass": 0, "e2e_pass": 0})
    total = struct_pass = gen_pass = mut_pass = e2e_pass = 0
    
    for r in results:
        struct = normalize_structure(r["structure"])
        by_structure[struct]["total"] += 1
        total += 1
        
        has_struct = len(r.get("diff_types", [])) > 0
        has_gen = r.get("executed", False) and r.get("observable_mismatch", False)
        
        mut_summary = r.get("mutation_summary", {})
        has_mut = mut_summary.get("fixed_by_replacement", 0) > 0 or mut_summary.get("remove_kept_correct", 0) > 0
        
        if has_struct:
            by_structure[struct]["struct_pass"] += 1
            struct_pass += 1
        if has_gen:
            by_structure[struct]["gen_pass"] += 1
            gen_pass += 1
        if has_mut:
            by_structure[struct]["mut_pass"] += 1
            mut_pass += 1
            
        if has_struct and has_gen and has_mut:
            by_structure[struct]["e2e_pass"] += 1
            e2e_pass += 1
            
    return {
        "total": total, "struct_pass": struct_pass, "gen_pass": gen_pass, "mut_pass": mut_pass, "e2e_pass": e2e_pass,
        "by_structure": dict(by_structure)
    }

def generate_markdown(title, description, analysis, out_path):
    lines = [
        f"# {title}",
        "",
        description,
        "",
        "## 1. 总体端到端 (E2E) 结果",
        "端到端成功定义为：**结构解析成功 (Struct) -> 造数生成反例差异 (Gen) -> 变异验证定位错因 (Mutation)**。",
        "",
        f"- **总测试数**: {analysis['total']}",
        f"- **结构提取成功**: {analysis['struct_pass']} ({(analysis['struct_pass']/analysis['total'])*100:.1f}%)",
        f"- **沙盒造数成功 (反例穿透)**: {analysis['gen_pass']} ({(analysis['gen_pass']/analysis['total'])*100:.1f}%)",
        f"- **变异引擎定位成功**: {analysis['mut_pass']} ({(analysis['mut_pass']/analysis['total'])*100:.1f}%)",
        f"- **端到端完全闭环**: {analysis['e2e_pass']} ({(analysis['e2e_pass']/analysis['total'])*100:.1f}%)",
        "",
        "## 2. 各 SQL 结构端到端支持矩阵",
        "",
        "| SQL 结构 | 测试数 | 结构通过 | 造数通过 | 变异通过 | 端到端闭环 | 当前端到端结论 |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :--- |"
    ]
    
    for struct in CATEGORIES:
        stats = analysis["by_structure"].get(struct, {"total":0, "struct_pass":0, "gen_pass":0, "mut_pass":0, "e2e_pass":0})
        if stats["total"] == 0:
            continue
        
        e2e_rate = stats['e2e_pass'] / stats['total']
        if e2e_rate > 0.8:
            conclusion = "**主能力基石**：全链路极度稳定。"
        elif e2e_rate > 0.5:
            conclusion = "**中等瓶颈**：存在部分未穿透或等价漏判。"
        else:
            conclusion = "**严重盲区**：链路极易断裂，需 CFG 重点干预。"
            
        lines.append(f"| **{struct}** | {stats['total']} | {stats['struct_pass']} | {stats['gen_pass']} | {stats['mut_pass']} | **{stats['e2e_pass']}** | {conclusion} |")

    lines.extend([
        "",
        "## 3. 端到端流程断点分析",
        "- **结构解析阶段 (Parse/ASTDiff)**：处理大部分常见错因，但涉及隐式 JOIN 展平、多重子查询关联等价时退化。",
        "- **反例造数阶段 (Sandbox Generation)**：对包含方言的题库断崖式下跌，且无法 100% 在 10 行随机数据内撞出深层聚合差异。",
        "- **错因变异阶段 (Mutation Verify)**：严重依赖造数阶段的存活率，若沙盒执行失败，变异引擎必定无法定位错因。"
    ])
    
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def main():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. 有方言的 250 题 (Online Random)
    online_file = OUTPUT_DIR / "online_random250_structure_generation_cases.jsonl"
    online_results = []
    if online_file.exists():
        with online_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip(): online_results.append(json.loads(line))
    
    # 2. 无方言的 250 题 (Web Common) - Need to re-evaluate to get mutation summaries
    web_file = OUTPUT_DIR / "web_common250_structure_cases.jsonl"
    web_results = []
    if web_file.exists():
        with web_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    case = json.loads(line)
                    case["structure"] = normalize_structure(case["structure"])
                    web_results.append(evaluate_case(case, max_rows=10))
                    
    # Generate Analysis
    online_analysis = analyze_results(online_results)
    web_analysis = analyze_results(web_results)
    
    generate_markdown(
        "Phase 1 端到端能力矩阵 (带方言真实互联网组)",
        "本报告测试了 250 道从互联网真实抓取的带有各路数据库方言的测试题。用于揭示系统在**面对未经清洗的真实输入时**，端到端完整链路（结构、造数、变异）的抗压能力和断裂点。",
        online_analysis,
        DOCS_DIR / "12-Phase1-E2E-带方言-支持矩阵.md"
    )
    
    generate_markdown(
        "Phase 1 端到端能力矩阵 (无方言纯净教学组)",
        "本报告测试了 250 道无方言干扰的规范化教学模版题。用于揭示排除了底层沙盒报错噪音后，系统在**最理想情况下的纯逻辑战力**（结构提取、反例穿透、变异定位）。",
        web_analysis,
        DOCS_DIR / "13-Phase1-E2E-无方言-支持矩阵.md"
    )
    
    print("Markdown reports generated successfully in docs/")

if __name__ == "__main__":
    main()
