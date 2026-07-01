import json
import sys
from pathlib import Path

# Add backend directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.parseval_data_generator import generate_and_compare
from core.error_attribution import evidence_weights_from_observation

def run_batch_evaluation(limit=10, offset=0):
    std_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "data_std_full.json"
    student_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "data_student_raw_full.json"

    if not std_path.exists() or not student_path.exists():
        print("Error: data_std_full.json or data_student_raw_full.json not found!")
        return

    # Load questions and slice them to limit questions
    all_questions = json.loads(std_path.read_text(encoding="utf-8"))
    sliced_questions = all_questions[offset : offset + limit]
    sliced_q_ids = {q["id"] for q in sliced_questions}
    questions_map = {q["id"]: q for q in sliced_questions}

    print(f"Loaded {len(all_questions)} questions total. Evaluating batch of {len(sliced_questions)} questions (offset={offset}, limit={limit}).")
    print(f"Target Question IDs: {sorted(list(sliced_q_ids))}")

    # Load student answers
    personas = json.loads(student_path.read_text(encoding="utf-8"))

    results = []
    evaluated_count = 0

    for p_data in personas:
        persona = p_data["persona"]
        for record in p_data["records"]:
            q_id = record["q_id"]
            if q_id not in sliced_q_ids:
                continue

            student_sql = record["sql"]
            q = questions_map.get(q_id)
            if not q:
                continue

            schema_text = q["schema"]
            standard_sql = q["ans_sql"]

            evaluated_count += 1
            print(f"[{evaluated_count}] Evaluating Q{q_id} for persona: {persona} ...")

            try:
                run = generate_and_compare(
                    schema_text=schema_text,
                    standard_sql=standard_sql,
                    student_sql=student_sql
                )
                
                is_correct = bool(run.is_equivalent)
                if run.error:
                    is_correct = False
                
                error_msg = run.error or run.data_evidence.get("student_exec_error")
                
                attr_res = evidence_weights_from_observation(
                    student_sql=student_sql,
                    answer_sql=standard_sql,
                    is_correct=is_correct,
                    error_message=error_msg,
                    judge_detail=run.data_evidence,
                    mutation_detail=run.mutation_evidence
                )
                
                results.append({
                    "q_id": q_id,
                    "persona": persona,
                    "question_text": q["q"],
                    "student_sql": student_sql,
                    "standard_sql": standard_sql,
                    "is_correct": is_correct,
                    "error_msg": error_msg,
                    "sandbox_run": {
                        "test_database": run.test_database,
                        "standard_rows": run.standard_rows,
                        "student_rows": run.student_rows,
                        "mutation_evidence": run.mutation_evidence
                    },
                    "observation": attr_res.observation,
                    "attributions": [item.to_dict() for item in attr_res.attributions]
                })
            except Exception as e:
                print(f"  Failed Q{q_id} for {persona}: {e}")
                results.append({
                    "q_id": q_id,
                    "persona": persona,
                    "student_sql": student_sql,
                    "standard_sql": standard_sql,
                    "is_correct": False,
                    "error_msg": f"Exception occurred: {e}",
                    "observation": {},
                    "attributions": []
                })

    out_file = PROJECT_ROOT / "data_construct_test" / "outputs" / f"batch_results_{offset}_{limit}.json"
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Batch evaluation complete. Evaluated {evaluated_count} records.")
    print(f"Results saved to {out_file}")

if __name__ == "__main__":
    # Default to first 10 questions (offset 0, limit 10)
    run_batch_evaluation(limit=10, offset=0)
