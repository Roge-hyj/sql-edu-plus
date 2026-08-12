import pytest
from sqlglot import parse_one

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import (
    extract_ast_diffs,
    generate_and_compare,
    parse_schema_column_types,
    transpile_to_sqlite,
)


def test_mutation_replacement_identifies_where_clause_fix():
    run = generate_and_compare(
        "student(id, name, dept);",
        "SELECT name FROM student WHERE dept = 'CS'",
        "SELECT name FROM student WHERE dept <> 'CS'",
    )

    assert run.executed is True
    where_tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "WHERE"]
    assert where_tests
    assert where_tests[0]["fixed_by_replacement"] is True


def test_generation_is_driven_by_ast_comparison_diff():
    diffs = extract_ast_diffs(
        "SELECT title FROM course WHERE credits > 3",
        "SELECT title FROM course WHERE credits >= 3",
    )

    assert any(diff["diff_type"] == "comparison_operator_changed" for diff in diffs)

    run = generate_and_compare(
        "course(course_id, title, credits);",
        "SELECT title FROM course WHERE credits > 3",
        "SELECT title FROM course WHERE credits >= 3",
    )

    tactics = {item["tactic"] for item in run.data_evidence["generation_tactics"]}
    assert "comparison_boundary_tristate" in tactics
    assert any(diff["diff_type"] == "comparison_operator_changed" for diff in run.data_evidence["ast_diffs"])


def test_having_count_boundary_generates_exact_size_group():
    run = generate_and_compare(
        "student(ID, name, dept_name, tot_cred);",
        "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) >= 4;",
        "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > 4;",
        max_rows_per_table=8,
    )

    counts: dict[str, int] = {}
    for row in run.test_database["student"]:
        dept = row["dept_name"]
        counts[dept] = counts.get(dept, 0) + 1

    assert 4 in counts.values()
    assert run.is_equivalent is False
    assert run.data_evidence["standard_row_count"] > run.data_evidence["student_row_count"]


def test_having_count_boundary_can_expand_beyond_default_rows():
    run = generate_and_compare(
        "student(ID, name, dept_name, tot_cred);",
        "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) >= 9;",
        "SELECT dept_name FROM student GROUP BY dept_name HAVING COUNT(*) > 9;",
    )

    counts: dict[str, int] = {}
    for row in run.test_database["student"]:
        dept = row["dept_name"]
        counts[dept] = counts.get(dept, 0) + 1

    assert len(run.test_database["student"]) >= 10
    assert 9 in counts.values()
    assert run.is_equivalent is False


def test_having_boundary_survives_extract_year_filter():
    run = generate_and_compare(
        "Orders(CustomerID, OrderDate, TotalAmount);",
        """
        SELECT CustomerID FROM Orders
        WHERE EXTRACT(YEAR FROM OrderDate) = 2023
        GROUP BY CustomerID HAVING SUM(TotalAmount) > 500
        """,
        """
        SELECT CustomerID FROM Orders
        WHERE EXTRACT(YEAR FROM OrderDate) = 2023
        GROUP BY CustomerID HAVING SUM(TotalAmount) >= 500
        """,
    )

    assert run.is_equivalent is False
    assert all(row["OrderDate"].startswith("2023-") for row in run.test_database["Orders"])


def test_compound_having_probe_satisfies_unchanged_aggregate_condition():
    run = generate_and_compare(
        "Orders(CustomerID, OrderDate, TotalAmount);",
        """
        SELECT CustomerID FROM Orders GROUP BY CustomerID
        HAVING MAX(TotalAmount) > 1000 AND COUNT(DISTINCT OrderDate) >= 3
        """,
        """
        SELECT CustomerID FROM Orders GROUP BY CustomerID
        HAVING MAX(TotalAmount) > 1000 AND COUNT(DISTINCT OrderDate) > 3
        """,
    )

    assert run.is_equivalent is False


def test_cross_table_having_probe_aligns_implicit_join_keys():
    run = generate_and_compare(
        "company_mast(com_id, com_name); item_mast(pro_com, pro_price);",
        """
        SELECT AVG(pro_price), company_mast.com_name
        FROM item_mast, company_mast
        WHERE item_mast.pro_com = company_mast.com_id
        GROUP BY company_mast.com_name HAVING AVG(pro_price) >= 350
        """,
        """
        SELECT AVG(pro_price), company_mast.com_name
        FROM item_mast, company_mast
        WHERE item_mast.pro_com = company_mast.com_id
        GROUP BY company_mast.com_name HAVING AVG(pro_price) > 350
        """,
    )

    assert run.is_equivalent is False
    assert run.standard_rows


def test_same_table_having_membership_aligns_outer_key():
    run = generate_and_compare(
        "employee(id, managerid, name);",
        """
        SELECT name FROM employee WHERE id IN (
            SELECT managerid FROM employee GROUP BY managerid HAVING COUNT(*) >= 5
        )
        """,
        """
        SELECT name FROM employee WHERE id IN (
            SELECT managerid FROM employee GROUP BY managerid HAVING COUNT(*) > 5
        )
        """,
    )

    assert run.is_equivalent is False


def test_distinct_mutation_replacement_identifies_missing_distinct():
    run = generate_and_compare(
        "takes(ID, course_id, year);",
        "SELECT DISTINCT course_id FROM takes;",
        "SELECT course_id FROM takes;",
    )

    tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "DISTINCT"]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["knowledge_point_id"] == "distinct"
    assert tests[0]["fixed_by_replacement"] is True


def test_group_by_probe_keeps_multiple_parameterized_filter_rows():
    run = generate_and_compare(
        "Actions(post_id, Action_date, action, extra);",
        """
        SELECT extra, COUNT(DISTINCT post_id) FROM Actions
        WHERE Action_date = @d AND action = 'report' GROUP BY extra
        """,
        """
        SELECT extra, COUNT(DISTINCT post_id) FROM Actions
        WHERE Action_date = @d AND action = 'report' GROUP BY '__group_probe__'
        """,
    )

    assert run.is_equivalent is False
    positive_rows = [
        row for row in run.test_database["Actions"]
        if row["Action_date"] == "2024-01-01" and row["action"] == "report"
    ]
    assert len(positive_rows) >= 4


def test_dynamic_generation_preserves_pk_candidates_when_probing_distinct_and_cte():
    run = generate_and_compare(
        "employee(emp_id, name, dept_id, salary); department(dept_id, dept_name, building);",
        """
        WITH active_dept AS (
            SELECT dept_id FROM department WHERE dept_id BETWEEN 1000 AND 1006
        )
        SELECT DISTINCT d.dept_name, COUNT(DISTINCT e.emp_id) AS emp_count
        FROM employee e
        JOIN department d ON e.dept_id = d.dept_id
        WHERE e.salary BETWEEN 3 AND 6
          AND d.dept_id IN (SELECT dept_id FROM active_dept)
        GROUP BY d.dept_name
        HAVING COUNT(DISTINCT e.emp_id) >= 1
        ORDER BY emp_count DESC, d.dept_name ASC
        LIMIT 3 OFFSET 0;
        """,
        """
        WITH active_dept AS (
            SELECT dept_id FROM department WHERE dept_id > 1000
        )
        SELECT d.dept_name, COUNT(e.emp_id) AS emp_count
        FROM employee e
        JOIN department d ON e.dept_id = d.dept_id
        WHERE e.salary > 3
          AND d.dept_id IN (SELECT dept_id FROM active_dept)
        GROUP BY d.building
        HAVING COUNT(e.emp_id) > 1
        ORDER BY emp_count ASC
        LIMIT 2 OFFSET 1;
        """,
    )

    emp_ids = [row["emp_id"] for row in run.test_database["employee"]]
    dept_ids = [row["dept_id"] for row in run.test_database["department"]]
    employee_dept_ids = [row["dept_id"] for row in run.test_database["employee"]]

    assert len(emp_ids) == len(set(emp_ids))
    assert len(dept_ids) == len(set(dept_ids))
    assert len(employee_dept_ids) > len(set(employee_dept_ids))


def test_join_type_mutation_replacement_identifies_left_join():
    run = generate_and_compare(
        "student(ID, name); takes(ID, course_id);",
        "SELECT s.name FROM student AS s LEFT JOIN takes AS t ON s.ID = t.ID;",
        "SELECT s.name FROM student AS s JOIN takes AS t ON s.ID = t.ID;",
    )

    tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "JOIN TYPE"]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["knowledge_point_id"] == "join-left"
    assert tests[0]["fixed_by_replacement"] is True


def test_case_mutation_replacement_identifies_case_boundary():
    run = generate_and_compare(
        "sales(sale_id, category, amount);",
        "SELECT category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) FROM sales GROUP BY category;",
        "SELECT category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) FROM sales GROUP BY category;",
    )

    tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "CASE"]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["knowledge_point_id"] == "case"
    assert tests[0]["fixed_by_replacement"] is True


def test_window_mutation_replacement_identifies_over_clause():
    run = generate_and_compare(
        "instructor(ID, name, dept_name, salary);",
        "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn FROM instructor;",
        "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn FROM instructor;",
    )

    tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "WINDOW"]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["knowledge_point_id"] == "window-row-number"
    assert tests[0]["fixed_by_replacement"] is True


def test_set_operator_mutation_replacement_identifies_union_all():
    run = generate_and_compare(
        "course(course_id, title, dept_name, credits);",
        "SELECT title FROM course WHERE dept_name = 'CS' UNION SELECT title FROM course WHERE credits > 3;",
        "SELECT title FROM course WHERE dept_name = 'CS' UNION ALL SELECT title FROM course WHERE credits > 3;",
    )

    tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "UNION"]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["knowledge_point_id"] == "union"
    assert tests[0]["fixed_by_replacement"] is True


def test_union_all_difference_is_present_in_ast_diff_graph():
    diffs = extract_ast_diffs(
        "SELECT title FROM course UNION SELECT title FROM course",
        "SELECT title FROM course UNION ALL SELECT title FROM course",
    )

    set_diffs = [diff for diff in diffs if diff.diff_type == "set_operator_changed"]
    assert set_diffs
    assert set_diffs[0].extra["standard_modifier"] == "DISTINCT"
    assert set_diffs[0].extra["student_modifier"] == "ALL"


def test_generate_and_compare_rejects_malformed_sql_before_transpilation():
    run = generate_and_compare(
        "student(id, name);",
        "SELECT name FROM student",
        "SELECT name FROM (student",
    )

    assert run.executed is False
    assert run.error == "student_sql_parse_failed"
    assert run.test_database == {}


def test_sql_server_recursive_cte_transpiles_with_recursive_and_without_option():
    sqlite_sql = transpile_to_sqlite(
        """
        WITH descendants AS (
            SELECT employee_id FROM Employees WHERE manager_id = 1
            UNION ALL
            SELECT e.employee_id FROM Employees e
            JOIN descendants d ON e.manager_id = d.employee_id
        )
        SELECT employee_id FROM descendants OPTION (MAXRECURSION 3);
        """
    )

    assert sqlite_sql is not None
    assert sqlite_sql.upper().startswith("WITH RECURSIVE")
    assert "MAXRECURSION" not in sqlite_sql.upper()
    assert "OPTION" not in sqlite_sql.upper()


def test_sql_server_recursive_union_modifier_gets_duplicate_state_probe():
    standard = """
        WITH descendants AS (
            SELECT employee_id FROM Employees WHERE manager_id = 1
            UNION ALL
            SELECT e.employee_id FROM Employees e
            JOIN descendants d ON e.manager_id = d.employee_id
        )
        SELECT employee_id FROM descendants OPTION (MAXRECURSION 3);
    """
    student = standard.replace("UNION ALL", "UNION")
    run = generate_and_compare(
        "Employees(employee_id, manager_id);",
        standard,
        student,
    )

    assert run.executed is True
    assert run.is_equivalent is False


def test_postgres_recursive_search_and_cycle_decorations_degrade_for_sqlite():
    search_sql = transpile_to_sqlite(
        """
        WITH RECURSIVE search_tree(id, link) AS (
            SELECT id, link FROM tree
            UNION ALL
            SELECT t.id, t.link FROM tree t JOIN search_tree st ON t.id = st.link
        ) SEARCH BREADTH FIRST BY id SET ordercol
        SELECT * FROM search_tree ORDER BY ordercol;
        """
    )
    cycle_sql = transpile_to_sqlite(
        """
        WITH RECURSIVE search_graph(id, link, depth) AS (
            SELECT id, link, 1 FROM graph
            UNION ALL
            SELECT g.id, g.link, sg.depth + 1
            FROM graph g JOIN search_graph sg ON g.id = sg.link
        ) CYCLE id SET is_cycle USING path
        SELECT * FROM search_graph;
        """
    )

    assert search_sql is not None
    assert " SEARCH " not in search_sql.upper()
    assert "ORDERCOL" not in search_sql.upper()
    assert 'ORDER BY "id"' in search_sql
    assert cycle_sql is not None
    assert " CYCLE " not in cycle_sql.upper()


def test_sqlite_unsupported_dialect_feature_is_not_judged_as_wrong():
    run = generate_and_compare(
        "sales(region, product, amount);",
        "SELECT region, product, SUM(amount) FROM sales GROUP BY ROLLUP(region, product)",
        "SELECT region, product, SUM(amount) FROM sales GROUP BY region, product",
    )

    assert run.executed is False
    assert run.is_equivalent is None
    assert run.judge_status == "UNSUPPORTED"
    assert "ROLLUP" in run.data_evidence["unsupported_features"]


def test_typed_schema_metadata_is_parsed_for_native_executors():
    types = parse_schema_column_types(
        "orders(id BIGINT NOT NULL, created_at DATETIME, amount DECIMAL(10,2), note TEXT);"
    )

    assert types["orders"]["id"] == "BIGINT NOT NULL"
    assert types["orders"]["created_at"] == "DATETIME"
    assert types["orders"]["amount"] == "DECIMAL(10,2)"
    assert types["orders"]["note"] == "TEXT"


def test_grouped_count_distinct_generates_in_group_duplicate_counterexample():
    run = generate_and_compare(
        "t(a, b);",
        "SELECT a, COUNT(DISTINCT b) FROM t GROUP BY a;",
        "SELECT a, COUNT(b) FROM t GROUP BY a;",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    target_rows = [row for row in run.test_database["t"] if row["a"] == "__distinct_count_group__"]
    assert len(target_rows) >= 3
    assert len({row["b"] for row in target_rows}) < len(target_rows)


def test_boolean_absorption_equivalence_does_not_emit_ast_diff():
    standard = "SELECT * FROM t WHERE (a > 1 AND b = 1) OR b = 1;"
    student = "SELECT * FROM t WHERE b = 1;"

    assert extract_ast_diffs(standard, student) == []
    run = generate_and_compare("t(a, b);", standard, student)
    assert run.executed is True
    assert run.is_equivalent is True
    assert run.ast_diffs == []
    truth_pairs = {(row["a"] > 1, row["b"] == 1) for row in run.test_database["t"][:4]}
    assert truth_pairs == {
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    }


def test_rewrite_guard_preserves_set_operator_diff():
    standard = (
        "SELECT customer_name FROM orders WHERE total_amount > 40 "
        "UNION SELECT customer_name FROM orders WHERE total_amount < 4"
    )
    student = (
        "SELECT customer_name FROM orders WHERE total_amount > 40 "
        "INTERSECT SELECT customer_name FROM orders WHERE total_amount < 4"
    )

    diffs = extract_ast_diffs(standard, student)

    assert any(diff.diff_type == "set_operator_changed" for diff in diffs)


@pytest.mark.parametrize(
    ("standard", "student", "diff_type"),
    [
        (
            "SELECT name FROM student WHERE id > 2",
            "SELECT name FROM instructor WHERE id > 2",
            "from_source_changed",
        ),
        (
            "SELECT DISTINCT name FROM student WHERE id > 2",
            "SELECT name FROM student WHERE id > 2",
            "distinct_changed",
        ),
        (
            "SELECT name FROM student WHERE id > 2",
            "SELECT name, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM student WHERE id > 2",
            "window_over_changed",
        ),
    ],
)
def test_rewrite_guard_preserves_other_top_level_shape_diffs(standard, student, diff_type):
    diffs = extract_ast_diffs(standard, student)

    assert any(diff.diff_type == diff_type for diff in diffs)


def test_structure_ir_distinct_is_select_level_only():
    aggregate_ir = SQLStructureIR.from_ast(
        parse_one("SELECT COUNT(DISTINCT dept_id) FROM student", read="mysql")
    )
    select_ir = SQLStructureIR.from_ast(
        parse_one("SELECT DISTINCT dept_id FROM student", read="mysql")
    )

    assert aggregate_ir.distinct is False
    assert "distinct" not in aggregate_ir.feature_kps()
    assert select_ir.distinct is True
    assert "distinct" in select_ir.feature_kps()


def test_non_equivalent_boolean_logic_still_emits_ast_diff():
    diffs = extract_ast_diffs(
        "SELECT * FROM t WHERE a > 1 AND b = 1;",
        "SELECT * FROM t WHERE b = 1;",
    )

    assert any(diff.diff_type in {"where_changed", "logical_operator_changed", "predicate_missing"} for diff in diffs)


def test_generate_and_compare_marks_execution_backend_in_evidence():
    run = generate_and_compare(
        "course(course_id, title);",
        "SELECT title FROM course",
        "SELECT title FROM course",
        sql_dialect="mysql",
    )

    assert run.executed is True
    assert run.judge_status == "CORRECT"
    assert run.data_evidence["execution_backend"] == "sqlite"
    assert run.data_evidence["sql_dialect"] == "mysql"


def test_forced_mysql_backend_requires_native_executor_url():
    run = generate_and_compare(
        "course(course_id BIGINT, title VARCHAR(255));",
        "SELECT title FROM course",
        "SELECT title FROM course",
        sql_dialect="mysql",
        execution_backend="mysql",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert "mysql_native_executor_not_configured" in (run.error or "")
    assert run.data_evidence["execution_backend"] == "mysql"


def test_sql_server_bare_offset_gets_sqlite_unbounded_limit():
    sqlite_sql = transpile_to_sqlite(
        "SELECT visited_on FROM Customer ORDER BY visited_on OFFSET 6 ROWS"
    )

    assert sqlite_sql is not None
    assert "LIMIT -1 OFFSET 6" in sqlite_sql.upper()


def test_output_alias_does_not_change_query_equivalence():
    run = generate_and_compare(
        "student(id, name);",
        "SELECT name AS student_name FROM student",
        "SELECT name FROM student",
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.data_evidence["columns_match"] is True
    assert run.data_evidence["column_names_match"] is False


def test_order_direction_probe_is_not_masked_by_repeating_projection_values():
    run = generate_and_compare(
        "course(course_id, title, credits);",
        "SELECT title FROM course ORDER BY credits DESC",
        "SELECT title FROM course ORDER BY credits ASC",
        max_rows_per_table=10,
    )

    assert run.is_equivalent is False
    assert len({row["title"] for row in run.test_database["course"]}) == 10


def test_order_secondary_key_probe_forces_opposite_tie_order():
    run = generate_and_compare(
        "instructor(id, name, salary);",
        "SELECT name FROM instructor ORDER BY salary ASC, name DESC",
        "SELECT name FROM instructor ORDER BY salary ASC",
    )

    assert run.is_equivalent is False
    assert run.standard_rows[:2] == [("Bob",), ("Alice",)]
    assert run.student_rows[:2] == [("Alice",), ("Bob",)]


@pytest.mark.parametrize(
    "schema,standard,student",
    [
        (
            "instructor(id, name, dept_name, salary);",
            "SELECT AVG(salary) FROM instructor",
            "SELECT SUM(salary) / COUNT(*) FROM instructor",
        ),
        (
            "instructor(id, name, dept_name, salary);",
            "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name HAVING SUM(salary) > 50000",
            "SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name HAVING SUM(salary) >= 50000",
        ),
        (
            "instructor(id, name, dept_name, salary);",
            "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(salary) >= 2",
            "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(*) >= 2",
        ),
        (
            "instructor(id, name, dept_name, salary);",
            "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)",
            "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor WHERE dept_name = 'Comp. Sci.')",
        ),
        (
            "instructor(id, name, dept_name, salary); student(id, name, dept_name, tot_cred);",
            "SELECT name FROM instructor WHERE dept_name = 'Comp. Sci.' UNION SELECT name FROM student WHERE dept_name = 'Math'",
            "SELECT name FROM instructor WHERE dept_name = 'Math' UNION SELECT name FROM student WHERE dept_name = 'Comp. Sci.'",
        ),
        (
            "student(id, name); takes(id, course_id);",
            "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id WHERE t.id IS NULL LIMIT 2",
            "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id WHERE t.id IS NULL LIMIT 3",
        ),
        (
            "instructor(id, name, salary);",
            "SELECT name, salary FROM instructor ORDER BY salary ASC NULLS LAST",
            "SELECT name, salary FROM instructor ORDER BY salary ASC",
        ),
        (
            "works(company_name, person_name, salary); company(company_name, city);",
            (
                "WITH co AS (SELECT company_name FROM company WHERE city = 'Beijing') "
                "SELECT person_name FROM works JOIN co ON works.company_name = co.company_name WHERE salary > 10000"
            ),
            (
                "WITH co AS (SELECT company_name FROM company WHERE city = 'Beijing') "
                "SELECT person_name FROM works JOIN co ON works.company_name = co.company_name WHERE salary < 10000"
            ),
        ),
    ],
)
def test_adversarial_data_probes_expose_counterexamples(schema, standard, student):
    run = generate_and_compare(schema, standard, student, max_rows_per_table=10)

    assert run.executed is True, run.error
    assert run.is_equivalent is False


def test_schema_free_recursive_cte_executes_and_exposes_boundary():
    run = generate_and_compare(
        "",
        (
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums"
        ),
        (
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM nums WHERE n < 3) SELECT n FROM nums"
        ),
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False


# ─────────────────────────────────────────────────────────────
# 新增探针测试：IS NULL / IS NOT NULL
# ─────────────────────────────────────────────────────────────

def test_is_null_probe_generates_null_and_non_null_rows():
    run = generate_and_compare(
        "employee(emp_id, name, manager_id);",
        "SELECT name FROM employee WHERE manager_id IS NULL;",
        "SELECT name FROM employee WHERE manager_id IS NOT NULL;",
    )

    rows = run.test_database["employee"]
    null_count = sum(1 for r in rows if r["manager_id"] is None)
    non_null_count = sum(1 for r in rows if r["manager_id"] is not None)

    assert null_count >= 1, "Should have at least one NULL row"
    assert non_null_count >= 1, "Should have at least one non-NULL row"
    assert run.is_equivalent is False


def test_is_not_null_probe_generates_counter_example():
    run = generate_and_compare(
        "orders(order_id, customer_id, status);",
        "SELECT order_id FROM orders WHERE status IS NOT NULL;",
        "SELECT order_id FROM orders;",
    )

    rows = run.test_database["orders"]
    null_status = sum(1 for r in rows if r["status"] is None)

    assert null_status >= 1, "Should have NULL counter-example for IS NOT NULL"


# ─────────────────────────────────────────────────────────────
# 新增探针测试：相关子查询
# ─────────────────────────────────────────────────────────────

def test_correlated_subquery_probe_ensures_cross_table_overlap():
    run = generate_and_compare(
        "department(dept_id, dept_name); instructor(id, name, dept_id, salary);",
        """SELECT d.dept_name FROM department d
           WHERE EXISTS (SELECT 1 FROM instructor i WHERE i.dept_id = d.dept_id AND i.salary > 70000)""",
        """SELECT d.dept_name FROM department d""",
    )

    dept_ids = [r["dept_id"] for r in run.test_database["department"]]
    instr_dept_ids = [r["dept_id"] for r in run.test_database["instructor"]]

    overlap = set(dept_ids) & set(instr_dept_ids)
    assert len(overlap) >= 2, "Should have overlapping dept_id values across tables"
    assert run.is_equivalent is False


def test_correlated_in_subquery_probe():
    run = generate_and_compare(
        "student(id, name, dept_name); takes(student_id, course_id);",
        """SELECT s.name FROM student s
           WHERE s.id IN (SELECT t.student_id FROM takes t WHERE t.course_id = 'CS101')""",
        "SELECT s.name FROM student s",
    )

    student_ids = [r["id"] for r in run.test_database["student"]]
    takes_ids = [r["student_id"] for r in run.test_database["takes"]]

    overlap = set(student_ids) & set(takes_ids)
    assert len(overlap) >= 1, "Should have overlapping ID values for correlated IN subquery"


# ─────────────────────────────────────────────────────────────
# 新增探针测试：CTE（简单 + 递归）
# ─────────────────────────────────────────────────────────────

def test_simple_cte_probe_extracts_inner_constraints():
    run = generate_and_compare(
        "employee(emp_id, name, salary, dept_id);",
        """WITH high_salary AS (
            SELECT * FROM employee WHERE salary > 50000
        )
        SELECT name FROM high_salary""",
        "SELECT name FROM employee",
    )

    salaries = [r["salary"] for r in run.test_database["employee"]]
    assert any(s > 50000 for s in salaries), "Should have rows matching CTE constraint salary > 50000"
    assert any(s <= 50000 for s in salaries), "Should have counter-example rows"
    assert run.is_equivalent is False


def test_recursive_cte_probe_generates_hierarchy():
    run = generate_and_compare(
        "employee(emp_id, name, manager_id);",
        """WITH RECURSIVE hierarchy AS (
            SELECT emp_id, name, manager_id, 1 AS level FROM employee WHERE manager_id IS NULL
            UNION ALL
            SELECT e.emp_id, e.name, e.manager_id, h.level + 1
            FROM employee e JOIN hierarchy h ON e.manager_id = h.emp_id
        )
        SELECT name, level FROM hierarchy""",
        "SELECT name, 1 AS level FROM employee",
    )

    rows = run.test_database["employee"]
    null_managers = sum(1 for r in rows if r["manager_id"] is None)
    assert null_managers >= 1, "Recursive CTE should have root node(s) with NULL manager_id"
    assert run.is_equivalent is False


def test_cte_mutation_detects_changed_cte():
    run = generate_and_compare(
        "sales(sale_id, region, amount);",
        """WITH regional AS (
            SELECT region, SUM(amount) AS total FROM sales GROUP BY region
        )
        SELECT region FROM regional WHERE total > 10""",
        """WITH regional AS (
            SELECT region, SUM(amount) AS total FROM sales GROUP BY region
        )
        SELECT region FROM regional WHERE total > 5""",
    )

    assert run.is_equivalent is False


# ─────────────────────────────────────────────────────────────
# 新增探针测试：CASE WHEN 分支遍历
# ─────────────────────────────────────────────────────────────

def test_case_when_probe_covers_all_branches():
    run = generate_and_compare(
        "orders(order_id, amount, status);",
        """SELECT order_id,
           CASE
               WHEN amount > 1000 THEN 'high'
               WHEN amount > 500 THEN 'medium'
               ELSE 'low'
           END AS tier
        FROM orders""",
        """SELECT order_id,
           CASE
               WHEN amount > 1000 THEN 'premium'
               WHEN amount > 500 THEN 'standard'
               ELSE 'basic'
           END AS tier
        FROM orders""",
    )

    amounts = [r["amount"] for r in run.test_database["orders"]]
    # 探针应覆盖 CASE WHEN 分支边界值
    assert any(a >= 500 for a in amounts), "Should have rows near middle WHEN branch boundary"
    assert any(a < 500 for a in amounts), "Should have rows for ELSE branch (amount <= 500)"
    assert run.is_equivalent is False, "Different CASE output labels should be detected"


def test_case_when_mutation_detects_boundary_change():
    run = generate_and_compare(
        "student(id, grade);",
        "SELECT id, CASE WHEN grade >= 60 THEN 'pass' ELSE 'fail' END FROM student",
        "SELECT id, CASE WHEN grade >= 70 THEN 'pass' ELSE 'fail' END FROM student",
    )

    grades = [r["grade"] for r in run.test_database["student"]]
    assert any(60 <= g < 70 for g in grades), "Should have boundary values between 60 and 70"
    assert run.is_equivalent is False


# ─────────────────────────────────────────────────────────────
# 完整 Phase 1 流水线集成测试
# ─────────────────────────────────────────────────────────────

def test_full_pipeline_complex_query_with_multiple_constructs():
    """完整流水线测试：CTE + JOIN + GROUP BY + HAVING + ORDER BY + LIMIT"""
    run = generate_and_compare(
        "student(id, name, dept_name, tot_cred); takes(student_id, course_id, grade); course(course_id, title, credits);",
        """WITH student_courses AS (
            SELECT s.id, s.name, s.dept_name, COUNT(t.course_id) AS course_count
            FROM student s
            LEFT JOIN takes t ON s.id = t.student_id
            GROUP BY s.id, s.name, s.dept_name
            HAVING COUNT(t.course_id) >= 2
        )
        SELECT name, dept_name, course_count
        FROM student_courses
        ORDER BY course_count DESC
        LIMIT 5""",
        """SELECT s.name, s.dept_name, COUNT(t.course_id) AS course_count
        FROM student s
        JOIN takes t ON s.id = t.student_id
        GROUP BY s.name, s.dept_name
        ORDER BY course_count ASC
        LIMIT 3""",
    )

    assert run.executed is True
    assert run.is_equivalent is False

    # 验证 AST diff 检测到多个差异
    diffs = run.data_evidence["ast_diffs"]
    diff_types = {d["diff_type"] for d in diffs}
    assert len(diff_types) >= 2, "Should detect multiple diff types"

    # 验证变异测试识别了关键差异
    tests = run.mutation_evidence["tests"]
    assert len(tests) > 0, "Should have mutation test results"


def test_full_pipeline_subquery_with_aggregation():
    """完整流水线测试：子查询 + 聚合 + 比较"""
    run = generate_and_compare(
        "instructor(id, name, salary, dept_name);",
        "SELECT name FROM instructor WHERE salary > (SELECT AVG(salary) FROM instructor)",
        "SELECT name FROM instructor WHERE salary > 50000",
    )

    assert run.executed is True
    assert run.is_equivalent is False

    # 验证造数考虑了子查询的 AVG 边界
    salaries = [r["salary"] for r in run.test_database["instructor"]]
    avg_salary = sum(salaries) / len(salaries) if salaries else 0
    assert any(s > avg_salary for s in salaries), "Should have salaries above average"
    assert any(s <= avg_salary for s in salaries), "Should have salaries at or below average"


def test_full_pipeline_window_function_ranking():
    """完整流水线测试：窗口函数排名"""
    run = generate_and_compare(
        "sales(sale_id, salesperson, region, amount);",
        """SELECT salesperson, region, amount,
           RANK() OVER (PARTITION BY region ORDER BY amount DESC) AS rank
        FROM sales""",
        """SELECT salesperson, region, amount,
           ROW_NUMBER() OVER (PARTITION BY region ORDER BY amount DESC) AS rank
        FROM sales""",
    )

    assert run.executed is True
    assert run.is_equivalent is False

    # 验证造数包含并列值（测试 RANK vs ROW_NUMBER 差异）
    amounts = [r["amount"] for r in run.test_database["sales"]]
    regions = [r["region"] for r in run.test_database["sales"]]
    assert len(amounts) > 0


def test_full_pipeline_set_operations():
    """完整流水线测试：集合操作"""
    run = generate_and_compare(
        "course(course_id, title, dept_name);",
        """SELECT title FROM course WHERE dept_name = 'CS'
           INTERSECT
           SELECT title FROM course WHERE dept_name = 'Math'""",
        """SELECT title FROM course WHERE dept_name = 'CS'
           UNION
           SELECT title FROM course WHERE dept_name = 'Math'""",
    )

    assert run.executed is True
    assert run.is_equivalent is False


def test_full_pipeline_correlated_exists():
    """完整流水线测试：相关 EXISTS 子查询"""
    run = generate_and_compare(
        "department(dept_id, name, budget); instructor(id, name, dept_id, salary);",
        """SELECT d.name FROM department d
           WHERE EXISTS (
               SELECT 1 FROM instructor i
               WHERE i.dept_id = d.dept_id AND i.salary > 80000
           )""",
        "SELECT d.name FROM department d WHERE d.budget > 100000",
    )

    assert run.executed is True
    assert run.is_equivalent is False


def test_deep_nested_membership_probe_aligns_each_value_domain():
    run = generate_and_compare(
        "employees(employee_id, manager_id, department_id, first_name);"
        "departments(department_id, location_id);"
        "locations(location_id, country_id);",
        "SELECT first_name FROM employees e WHERE manager_id IN ("
        "SELECT employee_id FROM employees m WHERE department_id IN ("
        "SELECT department_id FROM departments d WHERE location_id IN ("
        "SELECT location_id FROM locations l WHERE country_id = 'US')))" ,
        "SELECT first_name FROM employees e WHERE manager_id IN ("
        "SELECT employee_id FROM employees m WHERE department_id IN ("
        "SELECT department_id FROM departments d WHERE location_id IN ("
        "SELECT location_id FROM locations l WHERE country_id = 'CA')))" ,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows
    assert run.student_rows
    assert any(diff.get("subquery_depth", 0) >= 2 for diff in run.data_evidence["ast_diffs"])


def test_correlated_same_table_avg_boundary_is_observable():
    run = generate_and_compare(
        "orders(id, customer_id, purch_amt);",
        "SELECT id FROM orders a WHERE purch_amt > ("
        "SELECT AVG(purch_amt) FROM orders b WHERE b.customer_id = a.customer_id)",
        "SELECT id FROM orders a WHERE purch_amt >= ("
        "SELECT AVG(purch_amt) FROM orders b WHERE b.customer_id = a.customer_id)",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    assert {2, 3}.issubset({row[0] for row in run.student_rows})


def test_correlated_sum_half_boundary_is_observable():
    run = generate_and_compare(
        "employees(first_name, last_name, salary, department_id);",
        "SELECT first_name FROM employees e1 WHERE salary > ("
        "SELECT SUM(salary) * 0.5 FROM employees e2 "
        "WHERE e1.department_id = e2.department_id)",
        "SELECT first_name FROM employees e1 WHERE salary >= ("
        "SELECT SUM(salary) * 0.5 FROM employees e2 "
        "WHERE e1.department_id = e2.department_id)",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows


def test_window_running_sum_uses_numeric_probe_and_multiple_partitions():
    run = generate_and_compare(
        "activity(player_id, event_date, games_played);",
        "SELECT player_id, event_date, SUM(games_played) OVER ("
        "PARTITION BY player_id ORDER BY event_date) FROM activity",
        "SELECT player_id, event_date, SUM(games_played) OVER ("
        "ORDER BY event_date) FROM activity",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert len({row["player_id"] for row in run.test_database["activity"]}) >= 2
    assert all(isinstance(row["games_played"], (int, float)) for row in run.test_database["activity"])


def test_logical_precedence_probe_emits_truth_table_counterexample():
    run = generate_and_compare(
        "t(id, a, b);",
        "SELECT id FROM t WHERE (a = 1 OR a = 2) AND b = 1",
        "SELECT id FROM t WHERE a = 1 OR a = 2 AND b = 1",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert any(diff.diff_type == "logical_precedence_tree_changed" for diff in run.ast_diffs)
    assert run.data_evidence["standard_row_count"] != run.data_evidence["student_row_count"]


def test_recursive_union_all_duplicate_state_probe_is_observable():
    standard = (
        "WITH descendants AS ("
        "SELECT employee_id FROM Employees WHERE manager_id = 1 UNION ALL "
        "SELECT e.employee_id FROM Employees e JOIN descendants d "
        "ON e.manager_id = d.employee_id) SELECT employee_id FROM descendants"
    )
    student = standard.replace("UNION ALL", "UNION")
    run = generate_and_compare("Employees(employee_id, manager_id);", standard, student)

    assert run.executed is True
    assert run.is_equivalent is False
    assert any(diff.diff_type == "set_modifier_changed" for diff in run.ast_diffs)
    assert any(row[0] == 1001 for row in run.standard_rows)
    assert len(run.standard_rows) > len(run.student_rows)


def test_full_pipeline_null_handling():
    """完整流水线测试：NULL 处理"""
    run = generate_and_compare(
        "employee(emp_id, name, commission, bonus);",
        "SELECT name FROM employee WHERE commission IS NOT NULL AND bonus > 1000",
        "SELECT name FROM employee WHERE commission IS NULL OR bonus > 1000",
    )

    assert run.executed is True
    assert run.is_equivalent is False

    rows = run.test_database["employee"]
    null_commissions = sum(1 for r in rows if r["commission"] is None)
    non_null_commissions = sum(1 for r in rows if r["commission"] is not None)
    assert null_commissions >= 1, "Should have NULL commission rows"
    assert non_null_commissions >= 1, "Should have non-NULL commission rows"


def test_full_pipeline_implicit_vs_explicit_join():
    """完整流水线测试：隐式 JOIN vs 显式 JOIN 等价性"""
    run = generate_and_compare(
        "student(id, name); takes(student_id, course_id);",
        "SELECT s.name, t.course_id FROM student s JOIN takes t ON s.id = t.student_id",
        "SELECT s.name, t.course_id FROM student s, takes t WHERE s.id = t.student_id",
    )

    assert run.executed is True
    assert run.is_equivalent is True, "Implicit and explicit JOIN should be equivalent"


def test_filtered_avg_subquery_probe_crosses_both_average_thresholds():
    run = generate_and_compare(
        "student(id, name, dept, credits);",
        "SELECT name FROM student WHERE credits > (SELECT AVG(credits) FROM student)",
        (
            "SELECT name FROM student WHERE credits > "
            "(SELECT AVG(credits) FROM student WHERE dept = 'CS')"
        ),
        max_rows_per_table=10,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    cs_credits = [row["credits"] for row in run.test_database["student"] if row["dept"] == "CS"]
    assert sorted(cs_credits) == [10, 20]


def test_full_pipeline_distinct_vs_all():
    """完整流水线测试：DISTINCT vs 无 DISTINCT"""
    run = generate_and_compare(
        "takes(course_id, student_id);",
        "SELECT DISTINCT course_id FROM takes",
        "SELECT course_id FROM takes",
    )

    assert run.executed is True
    # 如果有重复 course_id，应该不等价
    course_ids = [r["course_id"] for r in run.test_database["takes"]]
    if len(course_ids) != len(set(course_ids)):
        assert run.is_equivalent is False


def test_full_pipeline_group_by_with_multiple_aggregates():
    """完整流水线测试：多聚合函数 + GROUP BY"""
    run = generate_and_compare(
        "orders(order_id, customer_id, amount, status);",
        """SELECT customer_id, COUNT(*) AS order_count, SUM(amount) AS total, AVG(amount) AS avg_amount
        FROM orders
        WHERE status = 'completed'
        GROUP BY customer_id
        HAVING COUNT(*) >= 3""",
        """SELECT customer_id, COUNT(*) AS order_count, SUM(amount) AS total
        FROM orders
        GROUP BY customer_id
        HAVING COUNT(*) >= 2""",
    )

    assert run.executed is True
    assert run.is_equivalent is False


def test_full_pipeline_nested_subquery():
    """完整流水线测试：嵌套子查询"""
    run = generate_and_compare(
        "student(id, name, dept_name); takes(student_id, course_id); course(course_id, title);",
        """SELECT s.name FROM student s
        WHERE s.id IN (
            SELECT t.student_id FROM takes t
            WHERE t.course_id IN (
                SELECT c.course_id FROM course c WHERE c.title LIKE '%Database%'
            )
        )""",
        """SELECT s.name FROM student s
        WHERE s.dept_name = 'CS'""",
    )

    assert run.executed is True
    assert run.is_equivalent is False


@pytest.mark.parametrize(
    "student_sql",
    [
        "DELETE FROM student WHERE id = 1",
        "UPDATE student SET name = 'x' WHERE id = 1",
        "SELECT name FROM student; SELECT id FROM student",
    ],
)
def test_phase1_rejects_non_query_or_multiple_statements(student_sql):
    run = generate_and_compare(
        "student(id, name);",
        "SELECT name FROM student",
        student_sql,
    )

    assert run.executed is False
    assert run.error == "student_sql_parse_failed"


def test_trailing_comment_is_still_one_query():
    run = generate_and_compare(
        "student(id, name);",
        "SELECT name FROM student",
        "SELECT name FROM student; -- trailing comment",
    )

    assert run.executed is True
    assert run.is_equivalent is True


@pytest.mark.parametrize(
    ("standard_sql", "student_sql"),
    [
        ("SELECT ABS(amount) FROM sales", "SELECT amount FROM sales"),
        ("SELECT ROUND(amount, 0) FROM sales", "SELECT ROUND(amount, 2) FROM sales"),
        ("SELECT TRIM(label) FROM sales", "SELECT label FROM sales"),
        ("SELECT CAST(amount AS INTEGER) FROM sales", "SELECT amount FROM sales"),
        (
            "SELECT COALESCE(label, fallback, 'unknown') FROM sales",
            "SELECT COALESCE(label, 'unknown') FROM sales",
        ),
    ],
)
def test_projection_expression_probes_generate_counterexamples(standard_sql, student_sql):
    run = generate_and_compare(
        "sales(id, amount, label, fallback);",
        standard_sql,
        student_sql,
    )

    assert run.executed is True
    assert run.is_equivalent is False


def test_null_safe_comparison_probe_injects_null():
    run = generate_and_compare(
        "employee(id, manager_id);",
        "SELECT manager_id IS DISTINCT FROM 3 FROM employee",
        "SELECT manager_id <> 3 FROM employee",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert any(row["manager_id"] is None for row in run.test_database["employee"])


def test_not_in_probe_preserves_match_and_injects_null():
    run = generate_and_compare(
        "student(id, name); takes(id, course_id);",
        "SELECT name FROM student WHERE id IN (SELECT id FROM takes)",
        "SELECT name FROM student WHERE id NOT IN (SELECT id FROM takes)",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    inner_ids = [row["id"] for row in run.test_database["takes"]]
    outer_ids = [row["id"] for row in run.test_database["student"]]
    assert None in inner_ids
    assert set(inner_ids) & set(outer_ids)


@pytest.mark.parametrize("row_scale", [4, 8, 12, 16])
@pytest.mark.parametrize(
    ("schema", "standard_sql", "student_sql"),
    [
        (
            "instructor(id, dept, salary);",
            "SELECT COUNT(DISTINCT dept) FROM instructor",
            "SELECT COUNT(dept) FROM instructor",
        ),
        (
            "course(id, title, credits);",
            "SELECT title FROM course WHERE credits < 3 OR credits > 6",
            "SELECT title FROM course WHERE credits < 3 AND credits > 6",
        ),
        (
            "course(id, title, credits);",
            "SELECT title FROM course WHERE credits NOT IN (1, 3)",
            "SELECT title FROM course WHERE credits IN (1, 3)",
        ),
        (
            "instructor(id, name, dept, salary);",
            "SELECT name, DENSE_RANK() OVER (PARTITION BY dept ORDER BY salary DESC) FROM instructor",
            "SELECT name, RANK() OVER (PARTITION BY dept ORDER BY salary DESC) FROM instructor",
        ),
        (
            "instructor(id, name, dept, salary);",
            (
                "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn "
                "FROM instructor QUALIFY rn = 1"
            ),
            (
                "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn "
                "FROM instructor QUALIFY rn <= 2"
            ),
        ),
    ],
)
def test_counterexample_probes_are_stable_across_row_scales(
    row_scale,
    schema,
    standard_sql,
    student_sql,
):
    run = generate_and_compare(
        schema,
        standard_sql,
        student_sql,
        max_rows_per_table=row_scale,
    )

    assert run.executed is True
    assert run.is_equivalent is False
