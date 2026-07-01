import json
import sys
from pathlib import Path

# Add backend directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.parseval_data_generator import generate_and_compare
from core.error_attribution import evidence_weights_from_observation

# Define HAVING cases for all 5 aggregate functions
agg_cases = [
    {
        "name": "Agg Case 1: SUM Aggregation (HAVING SUM Mismatch)",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) > 80000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) < 80000;"
    },
    {
        "name": "Agg Case 2: AVG Aggregation (HAVING AVG Mismatch)",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) > 50000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) < 50000;"
    },
    {
        "name": "Agg Case 3: MIN Aggregation (HAVING MIN Mismatch)",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) > 30000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) < 30000;"
    },
    {
        "name": "Agg Case 4: MAX Aggregation (HAVING MAX Mismatch)",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) > 90000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) < 90000;"
    },
    {
        "name": "Agg Case 5: COUNT Aggregation (HAVING COUNT Mismatch)",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) >= 2;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) > 2;"
    }
]

def run_tests():
    for case in agg_cases:
        print(f"\n======================================")
        print(case["name"])
        print(f"Standard SQL: {case['standard']}")
        print(f"Student SQL : {case['student']}")
        
        try:
            run = generate_and_compare(
                schema_text=case["schema"],
                standard_sql=case["standard"],
                student_sql=case["student"]
            )
            
            is_correct = bool(run.is_equivalent)
            if run.error:
                is_correct = False
            error_msg = run.error or run.data_evidence.get("student_exec_error")
            
            attr_res = evidence_weights_from_observation(
                student_sql=case["student"],
                answer_sql=case["standard"],
                is_correct=is_correct,
                error_message=error_msg,
                judge_detail=run.data_evidence,
                mutation_detail=run.mutation_evidence
            )
            
            print(f"Equivalent? {is_correct}")
            print("Generated Database rows in instructor table:")
            for idx, r in enumerate(run.test_database["instructor"]):
                print(f"  Row {idx}: dept_name={r['dept_name']}, salary={r['salary']}")
                
            print("Standard Output:", run.standard_rows)
            print("Student Output :", run.student_rows)
            
            print("Attributions:")
            for item in attr_res.attributions:
                print(f"  - KP: {item.knowledge_point_id} ({item.l2_code}) | Error: {item.error_type} | Conf: {item.confidence}")
                
        except Exception as e:
            print("Exception occurred running case:", e)

if __name__ == "__main__":
    run_tests()
