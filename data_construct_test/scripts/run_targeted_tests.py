import json
import sys
from pathlib import Path

# Add backend directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.parseval_data_generator import generate_and_compare
from core.error_attribution import evidence_weights_from_observation

def run_targeted_case(name, schema, standard_sql, student_sql):
    print(f"\n======================================")
    print(f"Running Targeted Case: {name}")
    print(f"Standard SQL: {standard_sql}")
    print(f"Student SQL : {student_sql}")
    try:
        run = generate_and_compare(
            schema_text=schema,
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
        
        print("Sandbox DB generated:")
        print(json.dumps(run.test_database, indent=2))
        print("Standard rows:", run.standard_rows)
        print("Student rows:", run.student_rows)
        print("Mutation evidence:")
        print(json.dumps(run.mutation_evidence, indent=2))
        print("Attributions:")
        print(json.dumps([item.to_dict() for item in attr_res.attributions], indent=2, ensure_ascii=False))
        
    except Exception as e:
        print("ERROR running case:", e)

if __name__ == "__main__":
    # Case 1: DISTINCT (Individual Operator)
    run_targeted_case(
        name="Case 1: Individual - DISTINCT (Missing DISTINCT)",
        schema="sales(sale_id, customer_id, amount)",
        standard_sql="SELECT DISTINCT customer_id FROM sales;",
        student_sql="SELECT customer_id FROM sales;"
    )

    # Case 2: HAVING (Individual Operator)
    run_targeted_case(
        name="Case 2: Individual - HAVING (Predicate Mismatch)",
        schema="orders(order_id, customer_id, amount)",
        standard_sql="SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(order_id) > 3;",
        student_sql="SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(order_id) = 3;"
    )

    # Case 3: Mixed (JOIN + GROUP BY + HAVING + ORDER BY)
    run_targeted_case(
        name="Case 3: Mixed Operators (HAVING mismatch + ORDER BY direction mismatch)",
        schema="employee(emp_id, name, dept_id, salary); department(dept_id, dept_name)",
        standard_sql="SELECT department.dept_name, SUM(employee.salary) AS total_payroll FROM employee JOIN department ON employee.dept_id = department.dept_id GROUP BY department.dept_name HAVING AVG(employee.salary) > 50000 ORDER BY total_payroll DESC;",
        student_sql="SELECT department.dept_name, SUM(employee.salary) AS total_payroll FROM employee JOIN department ON employee.dept_id = department.dept_id GROUP BY department.dept_name HAVING AVG(employee.salary) <= 50000 ORDER BY total_payroll ASC;"
    )
