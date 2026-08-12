import json
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))
sys.path.insert(0, str(PROJECT_ROOT / "data_construct_test" / "scripts"))

from run_online_random250_structure_generation_tests import evaluate_case

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"

def main():
    cases_file = OUTPUT_DIR / "web_common250_structure_cases.jsonl"
    cases = []
    with cases_file.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line))
                
    for c in cases:
        if c["structure"] in ["IN", "BETWEEN", "LIKE"]:
            c["structure"] = "IN / BETWEEN / LIKE"
                
    results = []
    for case in cases:
        res = evaluate_case(case, max_rows=10)
        results.append(res)
        
    by_structure = {}
    for structure in set(c["structure"] for c in cases):
        items = [r for r in results if r["structure"] == structure]
        by_structure[structure] = {
            "total": len(items),
            "strict_pass": sum(1 for item in items if item["strict_pass"]),
            "strict_fail": sum(1 for item in items if not item["strict_pass"]),
            "executed": sum(1 for item in items if item.get("executed")),
            "observable_counterexample": sum(1 for item in items if item.get("observable_mismatch")),
            "tactic_activated": sum(1 for item in items if item.get("generation_tactics")),
        }
        
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "strict_pass": sum(1 for result in results if result["strict_pass"]),
        "strict_fail": sum(1 for result in results if not result["strict_pass"]),
        "executed": sum(1 for result in results if result.get("executed")),
        "observable_counterexamples": sum(1 for result in results if result.get("observable_mismatch")),
        "tactic_activated": sum(1 for result in results if result.get("generation_tactics")),
        "by_structure": by_structure,
        "by_status": dict(Counter(result.get("data_generation_status") for result in results)),
        "errors": dict(Counter(error for result in results for error in result.get("errors", []))),
    }
    
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
