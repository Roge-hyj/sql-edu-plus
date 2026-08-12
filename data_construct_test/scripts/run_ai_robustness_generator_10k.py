import json
import subprocess
import sys
import os
import random
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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

STATUS_FILE = PROJECT_ROOT / "data_construct_test/outputs/fuzzer_10k_status.json"

def generate_batch(batch_idx: int, size: int = 25) -> list[dict]:
    operators_subset = [random.choice(OPERATORS) for _ in range(size)]
    prompt = (
        f"Generate exactly {size} distinct SQL robustness test cases for a SQL education checker. "
        "Return ONLY a valid JSON object, NO markdown formatting, NO backticks. "
        "The JSON must have the following structure: "
        '{"cases": [{"id": "ai_10k_' + str(batch_idx) + '_001", '
        '"operator": "one of ' + "|".join(OPERATORS) + '", '
        '"schema": "compact schema e.g. student(id, name); course(id, title);", '
        '"standard": "SQLite compatible SELECT standard solution;", '
        '"student": "SQLite compatible executable SELECT student wrong attempt;", '
        '"expected_kps": ["kp-id"], '
        '"note": "reason for failure"}]}. '
        "Requirements: Standard and student queries must be semantically non-equivalent. "
        "Expected KPs must come from: where, null, select-basic, distinct, join-on, join-left, "
        "join-inner, group-by, having, agg-count, order-by, limit, subquery, "
        "subquery-correlated, union, intersect, except, case, window-row-number, cte, cte-recursive."
    )
    
    # Simple exponential backoff for API rate limits
    for attempt in range(4):
        try:
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
            cases = data.get("cases", [])
            for c in cases:
                c.setdefault("expect_equiv", False)
                c.setdefault("expected_kps", [])
            return cases
        except Exception as e:
            time.sleep(2 ** attempt + random.random())
            
    print(f"Batch {batch_idx} generation failed permanently.")
    return []

def main():
    total_target = 10000
    batch_size = 25
    num_batches = total_target // batch_size
    
    print(f"Initializing 10,000-case parallel fuzzer (400 batches)...")
    
    results = []
    failures = []
    processed_count = 0
    start_time = time.time()
    
    # Using a thread pool of 12 workers to query Gemini CLI in parallel
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(generate_batch, idx, batch_size): idx for idx in range(1, num_batches + 1)}
        
        for future in as_completed(futures):
            batch_idx = futures[future]
            cases = future.result()
            
            # Execute cases immediately upon generation
            batch_results = []
            for case in cases:
                try:
                    res = run_case(case)
                    batch_results.append(res)
                    if res["status"] != "PASS":
                        failures.append(res)
                except Exception as e:
                    pass
            
            results.extend(batch_results)
            processed_count += len(cases)
            
            # Update status file continuously
            elapsed = time.time() - start_time
            pass_rate = (sum(1 for r in results if r["status"] == "PASS") / max(1, len(results))) * 100
            
            status_data = {
                "status": "RUNNING",
                "processed": processed_count,
                "target": total_target,
                "passed": sum(1 for r in results if r["status"] == "PASS"),
                "failed": len(failures),
                "pass_rate": pass_rate,
                "elapsed_seconds": elapsed,
                "estimated_remaining_seconds": (elapsed / max(1, processed_count)) * (total_target - processed_count)
            }
            STATUS_FILE.write_text(json.dumps(status_data, indent=2), encoding="utf-8")
            
            if processed_count % 100 == 0 or processed_count == total_target:
                print(f"Progress: {processed_count}/{total_target} (Failed: {len(failures)}, Pass rate: {pass_rate:.1%})")
                
    elapsed = time.time() - start_time
    pass_rate = (sum(1 for r in results if r["status"] == "PASS") / max(1, len(results))) * 100
    
    final_status = {
        "status": "COMPLETED",
        "processed": processed_count,
        "target": total_target,
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": len(failures),
        "pass_rate": pass_rate,
        "elapsed_seconds": elapsed
    }
    STATUS_FILE.write_text(json.dumps(final_status, indent=2), encoding="utf-8")
    
    # Save complete run log
    report_path = PROJECT_ROOT / "data_construct_test/outputs/ai_robustness_run_report_10k.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": final_status,
            "failures": failures,
            "all_results": results
        }, f, indent=2, ensure_ascii=False)
        
    print(f"High-volume fuzzer finished successfully. Report: {report_path}")

if __name__ == "__main__":
    main()
