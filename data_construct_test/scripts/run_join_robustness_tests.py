import json
import sys
from pathlib import Path

# Add backend directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.parseval_data_generator import generate_and_compare
from core.error_attribution import evidence_weights_from_observation

# Define DQL JOIN ON robustness cases
robustness_cases = [
    {
        "name": "Robustness Case 1: Missing JOIN Condition (笛卡尔积错误)",
        "schema": "student(ID, name, dept_name); advisor(s_ID, i_ID)",
        "standard": "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;",
        "student": "SELECT student.name FROM student JOIN advisor;"  # Missing ON clause entirely
    },
    {
        "name": "Robustness Case 2: Outer JOIN Type Confusion (外连接与内连接混淆)",
        "schema": "student(ID, name, dept_name); takes(ID, course_id, sec_id)",
        "standard": "SELECT student.name, takes.course_id FROM student LEFT JOIN takes ON student.ID = takes.ID;",
        "student": "SELECT student.name, takes.course_id FROM student INNER JOIN takes ON student.ID = takes.ID;"
    },
    {
        "name": "Robustness Case 3: Natural JOIN Over-constraint Trap (自然连接误用陷阱)",
        "schema": "student(ID, name, dept_name); advisor(ID, i_ID, dept_name)",
        "standard": "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.ID;",
        # Natural Join implicitly joins on BOTH ID and dept_name, filtering out cross-department advisors!
        "student": "SELECT student.name FROM student NATURAL JOIN advisor;"
    }
]

def run_tests():
    for case in robustness_cases:
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
            
            print(f"Equivalent? {is_correct} (Error: {error_msg})")
            print("Standard Output Row Count:", len(run.standard_rows))
            print("Student Output Row Count:", len(run.student_rows))
            
            print("\nAttributions Generated:")
            for item in attr_res.attributions:
                print(f"  - KP: {item.knowledge_point_id} ({item.l2_code}) | Error Type: {item.error_type} | Conf: {item.confidence}")
                print(f"    Detail: {item.detail}")
                
        except Exception as e:
            print("Exception occurred running case:", e)

if __name__ == "__main__":
    run_tests()
