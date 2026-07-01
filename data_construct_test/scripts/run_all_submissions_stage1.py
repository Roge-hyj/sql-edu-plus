import json
import sys
from pathlib import Path

# Add backend directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.parseval_data_generator import generate_and_compare
from core.error_attribution import evidence_weights_from_observation

def run_evaluation():
    std_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "data_std_full.json"
    student_path = PROJECT_ROOT / "data_construct_test" / "outputs" / "data_student_raw_full.json"

    if not std_path.exists() or not student_path.exists():
        print("Error: data_std_full.json or data_student_raw_full.json not found!")
        return

    questions = {q["id"]: q for q in json.loads(std_path.read_text(encoding="utf-8"))}
    personas = json.loads(student_path.read_text(encoding="utf-8"))

    results = []
    
    print("Evaluating all student submissions...")
    for p_data in personas:
        persona = p_data["persona"]
        for record in p_data["records"]:
            q_id = record["q_id"]
            student_sql = record["sql"]
            q = questions.get(q_id)
            if not q:
                continue

            schema_text = q["schema"]
            standard_sql = q["ans_sql"]

            try:
                run = generate_and_compare(
                    schema_text=schema_text,
                    standard_sql=standard_sql,
                    student_sql=student_sql
                )
                
                is_correct = bool(run.is_equivalent)
                # If there's an execution error, it's not correct
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
                # print(f"Failed to evaluate Q{q_id} for {persona}: {e}")
                pass

    print(f"Evaluation complete. Evaluated {len(results)} records.")
    
    # Save the output to a temporary JSON so we can analyze it or inspect
    out_dir = PROJECT_ROOT / "data_construct_test" / "outputs"
    out_file = out_dir / "stage1_evaluation_results.json"
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Results saved to {out_file}")

if __name__ == "__main__":
    run_evaluation()
