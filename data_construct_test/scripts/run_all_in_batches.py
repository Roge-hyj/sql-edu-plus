import json
import sys
import gc
from pathlib import Path

# Add backend directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.parseval_data_generator import generate_and_compare
from core.error_attribution import evidence_weights_from_observation

def run_all_in_batches(batch_size=10):
    std_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "data_std_full.json"
    student_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "data_student_raw_full.json"

    if not std_path.exists() or not student_path.exists():
        print("Error: data_std_full.json or data_student_raw_full.json not found!")
        return

    # Load questions and student answers
    questions = json.loads(std_path.read_text(encoding="utf-8"))
    personas = json.loads(student_path.read_text(encoding="utf-8"))
    
    total_questions = len(questions)
    print(f"Total questions: {total_questions}. Standard student personas: {len(personas)}")
    print(f"Running evaluation in batches of {batch_size} questions...")

    results = []
    
    # Process in batches
    for offset in range(0, total_questions, batch_size):
        batch_qs = questions[offset : offset + batch_size]
        batch_q_ids = {q["id"] for q in batch_qs}
        batch_questions_map = {q["id"]: q for q in batch_qs}

        print(f"\n--- Processing Batch: Questions {offset + 1} to {min(offset + batch_size, total_questions)} (IDs: {sorted(list(batch_q_ids))}) ---")

        batch_count = 0
        for p_data in personas:
            persona = p_data["persona"]
            for record in p_data["records"]:
                q_id = record["q_id"]
                if q_id not in batch_q_ids:
                    continue

                student_sql = record["sql"]
                q = batch_questions_map.get(q_id)
                if not q:
                    continue

                schema_text = q["schema"]
                standard_sql = q["ans_sql"]

                batch_count += 1
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
                        "observation": attr_res.observation,
                        "attributions": [item.to_dict() for item in attr_res.attributions]
                    })
                except Exception as e:
                    # Append failure record to match list structure
                    results.append({
                        "q_id": q_id,
                        "persona": persona,
                        "question_text": q["q"],
                        "student_sql": student_sql,
                        "standard_sql": standard_sql,
                        "is_correct": False,
                        "error_msg": f"Batch exception: {e}",
                        "observation": {},
                        "attributions": []
                    })
        
        print(f"Batch completed: processed {batch_count} student answers.")
        # Force garbage collection to free SQLite and AST objects
        gc.collect()

    out_file = PROJECT_ROOT / "data_construct_test" / "outputs" / "stage1_evaluation_results.json"
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    
    print(f"\nAll {total_questions} questions evaluated successfully.")
    print(f"Total evaluated records: {len(results)}")
    print(f"Final results merged and saved to: {out_file}")

if __name__ == "__main__":
    run_all_in_batches(batch_size=10)
