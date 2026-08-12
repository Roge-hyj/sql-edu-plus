import json
import subprocess
import sys
import os
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "data_construct_test" / "scripts"))
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from run_e2e_robustness_fuzzer import run_case

OPERATORS = [
    "WHERE", "NULL", "DISTINCT", "JOIN_ON", "JOIN_TYPE",
    "GROUP_BY", "HAVING_AGG", "HAVING_COUNT", "ORDER_BY", "LIMIT",
    "SUBQUERY", "CORRELATED_SUBQUERY", "SET_OPERATOR", "CASE_WHEN",
    "WINDOW", "CTE", "RECURSIVE_CTE"
]

def generate_batch(batch_idx: int, size: int = 15) -> list[dict]:
    operators_subset = [random.choice(OPERATORS) for _ in range(size)]
    prompt = (
        f"Generate exactly {size} SQL robustness test cases for a SQL education checker. "
        "Return ONLY a valid JSON object, NO markdown formatting, NO backticks. "
        "The JSON must have the following structure: "
        '{"cases": [{"id": "ai_' + str(batch_idx) + '_001", '
        '"operator": "one of ' + "|".join(OPERATORS) + '", '
        '"schema": "compact schema e.g. student(id, name); course(id, title);", '
        '"standard": "SQLite compatible SELECT standard solution;", '
        '"student": "SQLite compatible executable SELECT student wrong attempt;", '
        '"expected_kps": ["kp-id"], '
        '"note": "reason for failure"}]}. '
        "Requirements: Standard and student queries must be semantically non-equivalent. "
        "Use simple SQLite schemas and queries. Do not use complex vendor functions. "
        "Expected KPs must come from: where, null, select-basic, distinct, join-on, join-left, "
        "join-inner, group-by, having, agg-count, order-by, limit, subquery, "
        "subquery-correlated, union, intersect, except, case, window-row-number, cte, cte-recursive."
    )
    
    try:
        # Call gemini CLI using the flash model
        cmd = [
            "gemini",
            "--skip-trust",
            "-m", "gemini-3-flash-preview",
            "-p", prompt
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        content = res.stdout.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            if lines[0].startswith("```json") or lines[0].startswith("```"):
                content = "\n".join(lines[1:-1])
        data = json.loads(content)
        return data.get("cases", [])
    except Exception as e:
        print(f"Batch {batch_idx} generation failed: {e}")
        return []

def main():
    print("Starting automated high-volume robustness testing generation...")
    all_cases = []
    
    # We run 6 batches of 15 cases (90 cases total) to verify system stability under high stress
    for idx in range(1, 7):
        print(f"Generating batch {idx}/6...")
        cases = generate_batch(idx, size=15)
        print(f"Batch {idx} successfully generated {len(cases)} cases.")
        all_cases.extend(cases)
        
    print(f"Total AI cases generated: {len(all_cases)}")
    
    results = []
    failures = []
    for case in all_cases:
        try:
            case.setdefault("expect_equiv", False)
            case.setdefault("expected_kps", [])
            res = run_case(case)
            results.append(res)
            if res["status"] != "PASS":
                failures.append(res)
        except Exception as e:
            print(f"Error executing case {case.get('id')}: {e}")
            
    passed = sum(1 for r in results if r["status"] == "PASS")
    total = len(results)
    pass_rate = (passed / total * 100) if total > 0 else 0
    
    print(f"\nExecution Summary:")
    print(f"Total tested: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print(f"Pass rate: {pass_rate:.1%}")
    
    report_path = PROJECT_ROOT / "data_construct_test/outputs/ai_robustness_run_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "total": total,
                "passed": passed,
                "failed": len(failures),
                "pass_rate": pass_rate
            },
            "failures": failures,
            "all_results": results
        }, f, indent=2, ensure_ascii=False)
        
    print(f"Report saved to {report_path}")

if __name__ == "__main__":
    main()
