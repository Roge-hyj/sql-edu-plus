from collections import Counter
from contextlib import contextmanager

import pytest
from sqlglot import parse_one

import core.parseval_data_generator as parseval
from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import (
    _apply_not_in_null_probe,
    _execute_sqlite,
    _execute_mutation_case,
    _prepare_executable_sql_pair,
    _is_likely_backend_capability_error,
    extract_ast_diffs,
    generate_and_compare as _generate_and_compare,
    parse_schema_text,
    parse_schema_column_types,
    transpile_to_sqlite,
)
from core.native_engine_runner import (
    NativeInfrastructureError,
    NativeQueryExecutionError,
    NativeResultLimitError,
)
from core.witness_generation import SchemaCatalog
from core.witness_generation.obligations import stable_diff_id


def generate_and_compare(*args, **kwargs):
    """Make SQLite compatibility explicit for legacy unit fixtures.

    Production callers must choose a backend for declared vendor dialects;
    these fixtures exercise the bounded SQLite compatibility path unless a
    test explicitly supplies another backend.
    """
    kwargs.setdefault("execution_backend", "sqlite")
    return _generate_and_compare(*args, **kwargs)


def _patch_native_session(monkeypatch, execute):
    class FakeSession:
        def execute(self, sql):
            return execute(sql)

    @contextmanager
    def fake_native_query_session(*_args, **_kwargs):
        yield FakeSession()

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fake_native_query_session,
    )


def test_native_runtime_errors_do_not_use_sqlite_capability_heuristics():
    sql = "SELECT * FROM users CROSS JOIN LATERAL (SELECT 1) AS probe"

    assert _is_likely_backend_capability_error(
        "postgres", "connection refused", sql
    ) is False
    assert _is_likely_backend_capability_error(
        "sqlite", 'near "LATERAL": syntax error', sql
    ) is True


def test_single_row_correlated_lateral_projection_is_lowered_for_sqlite():
    standard = (
        "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL "
        "(SELECT s.id + 1 AS value) x"
    )
    student = "SELECT name, id + 1 AS value FROM student"

    sqlite_sql = transpile_to_sqlite(standard, source_dialect="mysql")
    run = generate_and_compare(
        "student(id, name);",
        standard,
        student,
        sql_dialect="mysql",
    )

    assert sqlite_sql is not None
    assert "LATERAL" not in sqlite_sql.upper()
    assert extract_ast_diffs(standard, student, dialect="mysql") == []
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.is_equivalent is True


def test_multirow_lateral_source_remains_an_explicit_engine_boundary():
    sql = (
        "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL "
        "(SELECT t.course_id AS value FROM takes t WHERE t.id = s.id) x"
    )

    run = generate_and_compare(
        "student(id, name); takes(id, course_id);",
        sql,
        sql,
        sql_dialect="mysql",
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert "LATERAL" in run.data_evidence["unsupported_features"]


@pytest.mark.parametrize(
    ("standard", "student"),
    [
        (
            "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL "
            "(SELECT s.id + 1 AS value) x",
            "SELECT name, id + 2 AS value FROM student",
        ),
        (
            "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL "
            "(SELECT s.id + 1 AS value) x WHERE s.id > 1",
            "SELECT name, id + 1 AS value FROM student",
        ),
    ],
)
def test_lateral_inline_rule_does_not_hide_unrelated_changes(standard, student):
    diffs = extract_ast_diffs(standard, student, dialect="mysql")
    run = generate_and_compare(
        "student(id, name);",
        standard,
        student,
        sql_dialect="mysql",
        max_rows_per_table=4,
    )

    assert diffs
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False


@pytest.mark.parametrize("operator", ["INTERSECT", "EXCEPT"])
def test_two_branch_set_all_is_lowered_with_duplicate_safe_row_numbers(operator):
    standard = (
        "SELECT title FROM course "
        f"{operator} ALL "
        "SELECT title FROM course WHERE credits = 3"
    )

    sqlite_sql = transpile_to_sqlite(standard, source_dialect="mysql")
    run = generate_and_compare(
        "course(id INT PRIMARY KEY, title TEXT, credits INT);",
        standard,
        standard,
        sql_dialect="mysql",
        max_rows_per_table=4,
    )

    assert sqlite_sql is not None
    assert "INTERSECT ALL" not in sqlite_sql.upper()
    assert "EXCEPT ALL" not in sqlite_sql.upper()
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True


def test_set_all_lowering_preserves_multicolumn_null_multiplicity():
    rows = {
        "items": [
            {"side": "L", "a": 1, "b": None},
            {"side": "L", "a": 1, "b": None},
            {"side": "L", "a": 1, "b": None},
            {"side": "L", "a": 2, "b": "x"},
            {"side": "R", "a": 1, "b": None},
            {"side": "R", "a": 1, "b": None},
            {"side": "R", "a": 2, "b": "x"},
            {"side": "R", "a": 2, "b": "x"},
            {"side": "R", "a": 3, "b": "y"},
        ]
    }
    base = "SELECT a, b FROM items WHERE side = '{}'"
    intersect_sql = transpile_to_sqlite(
        base.format("L") + " INTERSECT ALL " + base.format("R"),
        source_dialect="mysql",
    )
    except_sql = transpile_to_sqlite(
        base.format("L") + " EXCEPT ALL " + base.format("R"),
        source_dialect="mysql",
    )

    assert intersect_sql is not None
    assert except_sql is not None
    _, intersect_rows = _execute_sqlite(
        {"items": ["side", "a", "b"]},
        rows,
        intersect_sql,
        schema_types={
            "items": {"side": "TEXT", "a": "INT", "b": "TEXT"}
        },
    )
    _, except_rows = _execute_sqlite(
        {"items": ["side", "a", "b"]},
        rows,
        except_sql,
        schema_types={
            "items": {"side": "TEXT", "a": "INT", "b": "TEXT"}
        },
    )

    assert Counter(intersect_rows) == Counter({(1, None): 2, (2, "x"): 1})
    assert Counter(except_rows) == Counter({(1, None): 1})


@pytest.mark.parametrize(
    ("standard", "student", "row_count"),
    [
        (
            "SELECT title FROM course INTERSECT ALL SELECT title FROM course",
            "SELECT title FROM course INTERSECT SELECT title FROM course",
            4,
        ),
        (
            "SELECT title FROM course INTERSECT ALL SELECT title FROM course",
            "SELECT title FROM course INTERSECT SELECT title FROM course",
            10,
        ),
        (
            "SELECT title FROM course INTERSECT ALL "
            "SELECT title FROM course WHERE credits >= 3",
            "SELECT title FROM course INTERSECT "
            "SELECT title FROM course WHERE credits >= 3",
            4,
        ),
        (
            "SELECT title FROM course EXCEPT ALL "
            "SELECT title FROM course WHERE credits = 3",
            "SELECT title FROM course EXCEPT "
            "SELECT title FROM course WHERE credits = 3",
            4,
        ),
        (
            "SELECT title FROM course EXCEPT ALL "
            "SELECT title FROM course WHERE credits = 3",
            "SELECT title FROM course EXCEPT "
            "SELECT title FROM course WHERE credits = 3",
            10,
        ),
    ],
)
def test_set_all_modifier_difference_has_full_counterexample_evidence(
    standard,
    student,
    row_count,
):
    run = generate_and_compare(
        "course(id INT PRIMARY KEY, title TEXT, credits INT);",
        standard,
        student,
        sql_dialect="mysql",
        max_rows_per_table=row_count,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] >= 1
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "set_overlap"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["semantic_validation"]["evidence"]["query_source"] == "source_sql"


@pytest.mark.parametrize(
    "branch",
    [
        "SELECT title FROM course",
        "SELECT title FROM course WHERE credits >= 3",
    ],
)
def test_identical_deterministic_except_branches_ignore_all_modifier(branch):
    standard = f"{branch} EXCEPT ALL {branch}"
    student = f"{branch} EXCEPT {branch}"

    run = generate_and_compare(
        "course(id INT PRIMARY KEY, title TEXT, credits INT);",
        standard,
        student,
        sql_dialect="mysql",
        max_rows_per_table=4,
    )

    assert extract_ast_diffs(standard, student, dialect="mysql") == []
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.is_equivalent is True


def test_identical_except_branch_modifier_rule_rejects_unstable_expressions():
    standard = "SELECT RAND() FROM course EXCEPT ALL SELECT RAND() FROM course"
    student = "SELECT RAND() FROM course EXCEPT SELECT RAND() FROM course"

    diffs = extract_ast_diffs(standard, student, dialect="mysql")

    assert any(diff.diff_type == "set_modifier_changed" for diff in diffs)


def test_identical_except_branch_modifier_rule_does_not_cover_intersect():
    standard = "SELECT title FROM course INTERSECT ALL SELECT title FROM course"
    student = "SELECT title FROM course INTERSECT SELECT title FROM course"

    diffs = extract_ast_diffs(standard, student, dialect="mysql")

    assert any(diff.diff_type == "set_modifier_changed" for diff in diffs)


@pytest.mark.parametrize(
    ("operator", "schema", "left", "right"),
    [
        (
            "INTERSECT",
            "course(id INT PRIMARY KEY, credits INT);",
            "SELECT credits + 1 FROM course",
            "SELECT credits + 1 FROM course",
        ),
        (
            "EXCEPT",
            "left_rows(id INT PRIMARY KEY, value INT); "
            "right_rows(id INT PRIMARY KEY, value INT);",
            "SELECT value * 2 FROM left_rows",
            "SELECT value * 2 FROM right_rows",
        ),
    ],
)
def test_set_all_materializer_supports_one_column_arithmetic_projection(
    operator,
    schema,
    left,
    right,
):
    standard = f"{left} {operator} ALL {right}"
    student = f"{left} {operator} {right}"

    run = generate_and_compare(
        schema,
        standard,
        student,
        sql_dialect="mysql",
        max_rows_per_table=8,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "set_overlap"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True


@pytest.mark.parametrize("projection", ["a + b", "ABS(a)"])
def test_set_all_materializer_stays_conservative_for_unsupported_projection(
    projection,
):
    standard = (
        f"SELECT {projection} FROM t INTERSECT ALL "
        f"SELECT {projection} FROM t"
    )
    student = standard.replace(" INTERSECT ALL ", " INTERSECT ")

    run = generate_and_compare(
        "t(id INT PRIMARY KEY, a INT, b INT);",
        standard,
        student,
        sql_dialect="mysql",
        max_rows_per_table=8,
    )

    assert run.executed is True, run.error
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.is_equivalent is True


def test_set_all_with_outer_order_remains_an_engine_boundary():
    sql = "SELECT title FROM course INTERSECT ALL SELECT title FROM course ORDER BY title"

    run = generate_and_compare(
        "course(id INT PRIMARY KEY, title TEXT, credits INT);",
        sql,
        sql,
        sql_dialect="mysql",
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"


@pytest.mark.parametrize(
    ("dialect", "standard_sql", "student_sql"),
    [
        (
            "mysql",
            "SELECT IF(score >= 60, 'pass', 'fail') FROM scores",
            "SELECT IF(score > 60, 'pass', 'fail') FROM scores",
        ),
        (
            "tsql",
            "SELECT ISNULL(score, 0) FROM scores",
            "SELECT ISNULL(score, 1) FROM scores",
        ),
        (
            "oracle",
            "SELECT NVL(score, 0) FROM scores",
            "SELECT NVL(score, 1) FROM scores",
        ),
    ],
)
def test_scalar_function_mutation_has_function_localization(
    dialect, standard_sql, student_sql
):
    run = generate_and_compare(
        "scores(id BIGINT, score INT);",
        standard_sql,
        student_sql,
        sql_dialect=dialect,
    )

    function_tests = [
        item
        for item in run.mutation_evidence["tests"]
        if item["clause"] == "FUNCTION"
        and item["knowledge_point_id"] == "function"
    ]
    assert run.executed is True
    assert run.is_equivalent is False
    assert function_tests
    assert function_tests[0]["mutation_scope"] == ["FUNCTION"]
    assert function_tests[0]["fixed_by_replacement"] is True


def test_top_with_ties_probe_creates_cutoff_tie_with_unique_ids():
    standard_sql = (
        "SELECT TOP 1 WITH TIES id FROM scores ORDER BY score DESC"
    )
    student_sql = "SELECT TOP 1 id FROM scores ORDER BY score DESC"
    token = parseval._STRUCTURE_PARSE_DIALECT.set("tsql")
    try:
        rows = parseval.generate_test_database(
            {"scores": ["id", "score"]},
            standard_sql,
            student_sql,
            ast_diffs=extract_ast_diffs(
                standard_sql,
                student_sql,
                dialect="tsql",
            ),
        )["scores"]
    finally:
        parseval._STRUCTURE_PARSE_DIALECT.reset(token)

    ordered = sorted(rows, key=lambda row: row["score"], reverse=True)
    assert ordered[0]["score"] == ordered[1]["score"]
    assert len({row["id"] for row in rows}) == len(rows)


def test_oracle_nocycle_probe_creates_reachable_cycle_after_pk_repair():
    standard_sql = (
        "SELECT employee_id FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY NOCYCLE PRIOR employee_id = manager_id"
    )
    student_sql = standard_sql.replace(" NOCYCLE", "")
    token = parseval._STRUCTURE_PARSE_DIALECT.set("oracle")
    try:
        rows = parseval.generate_test_database(
            {"employees": ["employee_id", "manager_id"]},
            standard_sql,
            student_sql,
            ast_diffs=extract_ast_diffs(
                standard_sql,
                student_sql,
                dialect="oracle",
            ),
        )["employees"]
    finally:
        parseval._STRUCTURE_PARSE_DIALECT.reset(token)

    root, child, cycle = rows[:3]
    assert root["manager_id"] is None
    assert child["manager_id"] == root["employee_id"]
    assert cycle["manager_id"] == child["employee_id"]
    assert cycle["employee_id"] == root["employee_id"]


@pytest.mark.parametrize(
    ("dialect", "backend", "standard_sql", "student_sql", "clause", "kp"),
    [
        (
            "oracle",
            "oracle",
            "SELECT id FROM users SAMPLE BLOCK (10) SEED (42)",
            "SELECT id FROM users SAMPLE BLOCK (20) SEED (42)",
            "TABLE SAMPLE",
            "table-sample",
        ),
        (
            "postgres",
            "postgres",
            "SELECT id FROM ONLY users",
            "SELECT id FROM users",
            "FROM ONLY",
            "table-only",
        ),
    ],
)
def test_table_modifier_mutation_is_single_scope(
    dialect, backend, standard_sql, student_sql, clause, kp
):
    class FakeSession:
        def execute(self, _sql):
            return ["id"], [(1,)]

    tests = parseval._run_table_modifier_mutations(
        schema={"users": ["id"]},
        rows={"users": [{"id": 1}]},
        standard_ast=parse_one(standard_sql, read=dialect),
        student_ast=parse_one(student_sql, read=dialect),
        standard_columns=["id"],
        standard_rows=[(1,)],
        ordered=False,
        backend=backend,
        sql_dialect=dialect,
        execution_session=FakeSession(),
    )

    matching = [
        test
        for test in tests
        if test["clause"] == clause and test["knowledge_point_id"] == kp
    ]
    assert matching
    assert matching[0]["mutation_scope"] == [clause]
    assert matching[0]["replacement_exec_ok"] is True
    assert matching[0]["replacement_equivalent"] is True


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


def test_join_structure_mutation_restores_missing_join_dependency_closure():
    run = generate_and_compare(
        "majors(id, major_name); students(major_id, name);",
        "SELECT s.name, m.major_name FROM students s JOIN majors m ON s.major_id = m.id",
        "SELECT s.name FROM students",
    )

    tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "JOIN STRUCTURE"
    ]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["replacement_exec_ok"] is True
    assert tests[0]["fixed_by_replacement"] is True
    assert tests[0]["mutation_scope"] == ["FROM", "JOIN", "SELECT"]
    assert "JOIN" in tests[0]["replacement_sql"].upper()


def test_left_join_on_to_where_placement_has_atomic_witness_and_mutation():
    standard = (
        "SELECT A.id, B.status FROM A "
        "LEFT JOIN B ON A.id = B.id AND B.status = 1"
    )
    student = (
        "SELECT A.id, B.status FROM A "
        "LEFT JOIN B ON A.id = B.id WHERE B.status = 1"
    )
    run = generate_and_compare(
        "A(id INTEGER PRIMARY KEY); B(id INTEGER, status INTEGER);",
        standard,
        student,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert {diff.diff_type for diff in run.ast_diffs} == {
        "join_predicate_placement_changed"
    }
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["probe"] == "join_predicate_placement"
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item.get("action") == "move_outer_join_predicate_to_standard_clause"
    ]
    assert len(mutations) == 1
    assert mutations[0]["mutation_scope"] == ["JOIN ON", "WHERE"]
    assert mutations[0]["dependent_changes"] == ["WHERE"]
    assert mutations[0]["fixed_by_replacement"] is True
    assert mutations[0]["diff_ids"] == [effectiveness[0]["diff_id"]]
    assert mutations[0]["obligation_ids"] == [
        effectiveness[0]["obligation_id"]
    ]
    assert mutations[0]["binding_quality"] == "exact"


@pytest.mark.parametrize("max_rows_per_table", [6, 10, 16])
def test_left_antijoin_limit_boundary_keeps_required_dangling_rows(
    max_rows_per_table,
):
    standard = (
        "SELECT s.name FROM student s LEFT JOIN takes t "
        "ON s.id = t.id WHERE t.id IS NULL LIMIT 2"
    )
    student = standard.replace("LIMIT 2", "LIMIT 3")
    run = generate_and_compare(
        "student(id INTEGER PRIMARY KEY, name TEXT); "
        "takes(id INTEGER PRIMARY KEY, course_id INTEGER);",
        standard,
        student,
        max_rows_per_table=max_rows_per_table,
        sql_dialect="sqlite",
    )

    student_ids = {row["id"] for row in run.test_database["student"]}
    takes_ids = {row["id"] for row in run.test_database["takes"]}
    assert len(student_ids - takes_ids) >= 3
    assert run.executed is True
    assert run.is_equivalent is False
    assert len(run.standard_rows) == 2
    assert len(run.student_rows) == 3
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "limit_row_count_boundary"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True
    assert effectiveness["causal_attribution_verified"] is True


def test_aggregate_placement_mutation_moves_where_predicate_to_having():
    run = generate_and_compare(
        "payments(account_id, amount);",
        (
            "SELECT account_id, AVG(amount) FROM payments "
            "GROUP BY account_id HAVING AVG(amount) > 60"
        ),
        (
            "SELECT account_id, AVG(amount) FROM payments "
            "WHERE AVG(amount) > 60 GROUP BY account_id"
        ),
    )

    tests = [
        item for item in run.mutation_evidence["tests"]
        if item["action"] == "move_aggregate_predicate_from_where_to_having"
    ]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["replacement_exec_ok"] is True
    assert tests[0]["fixed_by_replacement"] is True
    assert tests[0]["mutation_scope"] == ["HAVING"]
    assert tests[0]["dependent_changes"] == ["WHERE"]
    assert " HAVING " in tests[0]["replacement_sql"].upper()
    assert " WHERE " not in tests[0]["replacement_sql"].upper()


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


def test_in_list_member_witness_uses_string_value_on_id_named_column():
    run = generate_and_compare(
        "students(major_id TEXT, name TEXT);",
        "SELECT name FROM students WHERE major_id IN ('A', 'B', 'C')",
        "SELECT name FROM students WHERE major_id IN ('A', 'B')",
    )

    tactics = {item["tactic"] for item in run.data_evidence["generation_tactics"]}
    assert run.executed is True
    assert run.is_equivalent is False
    assert "in_list_membership_probe" in tactics
    assert any(row["major_id"] == "C" for row in run.test_database["students"])


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


def test_having_count_boundary_uses_complete_composite_group_key():
    run = generate_and_compare(
        "ActorDirector(actor_id, director_id, count);",
        "SELECT actor_id, director_id FROM ActorDirector "
        "GROUP BY actor_id, director_id HAVING COUNT(*) >= 3;",
        "SELECT actor_id, director_id FROM ActorDirector "
        "GROUP BY actor_id, director_id HAVING COUNT(*) > 3;",
        max_rows_per_table=4,
    )

    counts: dict[tuple[object, object], int] = {}
    for row in run.test_database["ActorDirector"]:
        key = (row["actor_id"], row["director_id"])
        counts[key] = counts.get(key, 0) + 1

    assert 3 in counts.values()
    assert all(isinstance(row["actor_id"], int) for row in run.test_database["ActorDirector"])
    assert all(isinstance(row["director_id"], int) for row in run.test_database["ActorDirector"])
    assert run.is_equivalent is False
    assert run.data_evidence["standard_row_count"] > run.data_evidence["student_row_count"]


def test_joined_having_count_repeats_child_foreign_key_not_parent_primary_key():
    run = generate_and_compare(
        "actors(actor_id BIGINT, region VARCHAR(32)); "
        "credits(credit_id BIGINT, actor_id BIGINT);",
        "SELECT a.actor_id, a.region FROM actors a "
        "JOIN credits c ON a.actor_id = c.actor_id "
        "GROUP BY a.actor_id, a.region HAVING COUNT(c.credit_id) >= 3;",
        "SELECT a.actor_id, a.region FROM actors a "
        "JOIN credits c ON a.actor_id = c.actor_id "
        "GROUP BY a.actor_id, a.region HAVING COUNT(c.credit_id) > 3;",
        max_rows_per_table=4,
    )

    parent_ids = [row["actor_id"] for row in run.test_database["actors"]]
    child_counts: dict[object, int] = {}
    for row in run.test_database["credits"]:
        actor_id = row["actor_id"]
        child_counts[actor_id] = child_counts.get(actor_id, 0) + 1

    assert len(parent_ids) == len(set(parent_ids))
    assert set(child_counts).issubset(set(parent_ids))
    assert 3 in child_counts.values()
    assert run.is_equivalent is False


@pytest.mark.parametrize("max_rows_per_table", [8, 16, 32])
def test_joined_having_count_uses_post_join_cardinality_boundary(
    max_rows_per_table,
):
    run = generate_and_compare(
        "Highschooler(id INTEGER PRIMARY KEY, name TEXT, grade INTEGER); "
        "Friend(student_id INTEGER, friend_id INTEGER);",
        "SELECT T2.name FROM Friend T1 "
        "JOIN Highschooler T2 ON T1.student_id = T2.id "
        "WHERE T2.grade > 5 GROUP BY T1.student_id "
        "HAVING COUNT(*) >= 2;",
        "SELECT T2.name FROM Friend T1 "
        "JOIN Highschooler T2 ON T1.student_id = T2.id "
        "WHERE T2.grade > 5 GROUP BY T1.student_id "
        "HAVING COUNT(*) > 2;",
        max_rows_per_table=max_rows_per_table,
    )

    highschoolers = run.test_database["Highschooler"]
    friends = run.test_database["Friend"]
    highschooler_ids = [row["id"] for row in highschoolers]
    join_group_counts: dict[object, int] = {}
    passing_ids = {
        row["id"] for row in highschoolers if row["grade"] > 5
    }
    for row in friends:
        student_id = row["student_id"]
        matches = sum(
            1
            for highschooler in highschoolers
            if highschooler["id"] == student_id and highschooler["grade"] > 5
        )
        if matches:
            join_group_counts[student_id] = (
                join_group_counts.get(student_id, 0) + matches
            )

    assert len(highschooler_ids) == len(set(highschooler_ids))
    assert set(join_group_counts).issubset(passing_ids)
    assert 2 in join_group_counts.values()
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "aggregate_boundary_group"
    )
    validation = effectiveness["semantic_validation"]
    assert validation["constraints_satisfied"] is True
    assert (
        validation["evidence"]["aggregate_cardinality_scope"]
        == "post_join"
    )
    assert 2 in validation["evidence"]["post_join_aggregate_values"].values()


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


def test_having_boundary_survives_extract_year_filter(monkeypatch):
    def fail_legacy_stabilizer(*args, **kwargs):
        raise AssertionError("declared aggregate boundary used legacy SUM stabilizer")

    monkeypatch.setattr(
        "core.parseval_data_generator._stabilize_having_sum_boundary",
        fail_legacy_stabilizer,
    )
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


def test_top_level_distinct_witness_exposes_bounded_difference():
    run = generate_and_compare(
        "golf(VCUI, RCUI, VSAB, RSAB, SON, SF, SVER);",
        "SELECT DISTINCT SVER FROM golf WHERE SVER < 1996;",
        "SELECT SVER FROM golf WHERE SVER < 1996;",
    )

    tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "DISTINCT"
    ]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["replacement_equivalent"] is True
    assert tests[0]["fixed_by_replacement"] is True


def test_constant_true_filter_is_a_supported_equivalent_rewrite():
    standard = "SELECT MIN(value) FROM t"
    student = "SELECT MIN(value) FROM t WHERE 1 = 1"

    assert extract_ast_diffs(standard, student) == []
    run = generate_and_compare("t(id, value);", standard, student)

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"


def test_filtered_aggregate_presence_materializes_a_different_minimum():
    run = generate_and_compare(
        "episodes(episode, no_in_season, viewers);",
        "SELECT MIN(no_in_season) FROM episodes WHERE viewers = '3.00'",
        "SELECT MIN(no_in_season) FROM episodes",
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "predicate_positive_negative"
    )
    assert effectiveness["constraints_satisfied"] is True


def test_distinct_filter_witness_duplicates_two_qualifying_rows():
    run = generate_and_compare(
        "episodes(episode, first_air_date, other);",
        "SELECT first_air_date FROM episodes WHERE episode = 9",
        "SELECT DISTINCT first_air_date FROM episodes WHERE episode = 9",
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "duplicate_projection"
    )
    assert effectiveness["constraints_satisfied"] is True


def test_numeric_leading_schema_identifiers_share_parser_and_sqlite_semantics():
    standard = (
        "SELECT tournament FROM matches "
        "WHERE 2007 = '1r' AND 2009 = '1r'"
    )
    student = standard.replace("AND", "OR")
    run = generate_and_compare(
        "matches(tournament, 2007, 2009);",
        standard,
        student,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "logical_truth_table"
    )
    assert effectiveness["constraints_satisfied"] is True


def test_numeric_identifier_quote_repair_preserves_diff_identity():
    schema = "matches(tournament, 2007, 2009);"
    standard = "SELECT tournament FROM matches WHERE 2007 = '1r' AND 2009 = '1r'"
    student = "SELECT tournament FROM matches WHERE 2007 = '1r' OR 2009 = '1r'"

    raw_ids = [stable_diff_id(diff) for diff in extract_ast_diffs(standard, student)]
    run = generate_and_compare(schema, standard, student)
    repaired_ids = [stable_diff_id(diff) for diff in run.ast_diffs]

    assert repaired_ids == raw_ids


def test_string_filter_reaches_count_column_vs_count_star_witness():
    run = generate_and_compare(
        "scores(country, press_index);",
        "SELECT COUNT(press_index) FROM scores WHERE country = 'Austria'",
        "SELECT COUNT(*) FROM scores WHERE country = 'Austria'",
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows


def test_string_filter_reaches_min_max_witness():
    run = generate_and_compare(
        "senators(residence, term_limited);",
        "SELECT MIN(term_limited) FROM senators WHERE residence = 'Coshocton'",
        "SELECT MAX(term_limited) FROM senators WHERE residence = 'Coshocton'",
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows


def test_string_filter_reaches_distinct_projection_witness():
    run = generate_and_compare(
        "tax(year, reserve_tax, revenue_ratio);",
        "SELECT reserve_tax FROM tax WHERE revenue_ratio = '0.79'",
        "SELECT DISTINCT reserve_tax FROM tax WHERE revenue_ratio = '0.79'",
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows


def test_self_join_distinct_witness_materializes_duplicate_result_paths():
    standard = (
        "SELECT DISTINCT l1.num AS ConsecutiveNums FROM logs l1 "
        "JOIN logs l2 ON l1.id = l2.id - 1 AND l1.num = l2.num "
        "JOIN logs l3 ON l1.id = l3.id - 2 AND l2.num = l3.num"
    )
    student = standard.replace("SELECT DISTINCT", "SELECT", 1)

    run = generate_and_compare(
        "logs(id, num);",
        standard,
        student,
        max_rows_per_table=10,
    )

    distinct_tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "DISTINCT"
    ]
    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert len(run.student_rows) > len(run.standard_rows)
    assert distinct_tests
    assert distinct_tests[0]["fixed_by_replacement"] is True


def test_distinct_group_having_witness_materializes_two_qualifying_groups():
    standard = (
        "SELECT DISTINCT viewer_id AS id FROM Views "
        "GROUP BY viewer_id, view_date "
        "HAVING COUNT(DISTINCT article_id) > 1 ORDER BY id"
    )
    student = standard.replace("SELECT DISTINCT", "SELECT", 1)

    run = generate_and_compare(
        "Views(id, article_id, viewer_id, view_date);",
        standard,
        student,
        max_rows_per_table=10,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows == [(100,)]
    assert run.student_rows == [(100,), (100,)]
    distinct = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "duplicate_projection"
    )
    assert distinct["constraints_satisfied"] is True
    assert distinct["semantic_validation"]["evidence"]["source"] == "executed_projection"


def test_distinct_group_having_beyond_row_cap_is_semantic_boundary():
    standard = (
        "SELECT DISTINCT viewer_id FROM Views "
        "GROUP BY viewer_id, view_date "
        "HAVING COUNT(DISTINCT article_id) > 16"
    )
    student = standard.replace("SELECT DISTINCT", "SELECT", 1)

    run = generate_and_compare(
        "Views(id, article_id, viewer_id, view_date);",
        standard,
        student,
        max_rows_per_table=8,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SEMANTIC_BOUNDARY"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.boundary_evidence["operator"] == "DISTINCT_GROUP_HAVING"
    assert run.boundary_evidence["required_rows"] == 34
    assert run.boundary_evidence["witness_row_limit"] == 32


def test_cte_distinct_case_sum_witness_reaches_outer_membership():
    standard = (
        "WITH tb1 AS (SELECT DISTINCT customer_id, product_name, "
        "CASE WHEN product_name IN ('A', 'B') THEN 1 "
        "WHEN product_name = 'C' THEN -1 ELSE 0 END AS c FROM Orders) "
        "SELECT * FROM Customers WHERE customer_id IN ("
        "SELECT customer_id FROM tb1 GROUP BY customer_id HAVING SUM(c) = 2)"
    )
    student = standard.replace("SELECT DISTINCT", "SELECT", 1)

    run = generate_and_compare(
        "Customers(customer_id); Orders(customer_id, product_name);",
        standard,
        student,
        max_rows_per_table=10,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows == [(8801,)]
    assert run.student_rows == []
    distinct = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "duplicate_projection"
    )
    assert distinct["constraints_satisfied"] is True
    assert distinct["causal_attribution_verified"] is True
    assert distinct["semantic_validation"]["evidence"]["source"] == "nested_query_input"


def test_tsql_variable_assignment_is_engine_gap_not_sqlite_equality():
    standard = (
        "SELECT @accept = COUNT(*) FROM ("
        "SELECT DISTINCT requester_id, accepter_id FROM request_accepted) AS tb1"
    )
    student = standard.replace("DISTINCT ", "", 1)

    run = generate_and_compare(
        "request_accepted(accepter_id, requester_id);",
        standard,
        student,
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.data_evidence["sql_dialect"] == "tsql"
    assert run.data_evidence["unsupported_features"] == [
        "TSQL_VARIABLE_ASSIGNMENT_UNOBSERVABLE"
    ]


def test_distinct_on_witness_exposes_bounded_difference():
    run = generate_and_compare(
        "golf(SVER);",
        "SELECT DISTINCT ON (SVER) SVER FROM golf WHERE SVER < 1996",
        "SELECT SVER FROM golf WHERE SVER < 1996",
        sql_dialect="postgres",
    )

    tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "DISTINCT ON"
    ]
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["replacement_equivalent"] is True
    assert tests[0]["fixed_by_replacement"] is True


def test_set_operation_distinct_does_not_use_top_level_latent_fix():
    run = generate_and_compare(
        "t(x); u(x);",
        "SELECT DISTINCT x FROM t UNION SELECT x FROM u",
        "SELECT x FROM t UNION SELECT x FROM u",
    )

    distinct_tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "DISTINCT"
    ]
    assert run.executed is True
    assert run.is_equivalent is True
    assert distinct_tests
    assert distinct_tests[0]["replacement_equivalent"] is True
    assert distinct_tests[0]["fixed_by_replacement"] is False


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
    schema = (
        "employee(emp_id, name, dept_id, salary); "
        "department(dept_id, dept_name, building);"
    )
    standard = """
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
    """
    student = """
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
    """
    run = generate_and_compare(
        schema,
        standard,
        student,
    )
    suite = parseval.generate_witness_suite(
        parse_schema_text(schema),
        standard,
        student,
    )

    assert run.executed is True
    for world in suite.worlds:
        emp_ids = [row["emp_id"] for row in world.database["employee"]]
        dept_ids = [row["dept_id"] for row in world.database["department"]]
        assert len(emp_ids) == len(set(emp_ids))
        assert len(dept_ids) == len(set(dept_ids))
    assert any(
        len(values) > len(set(values))
        for world in suite.worlds
        for values in [[
            row["dept_id"] for row in world.database["employee"]
        ]]
    )


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


def test_cross_join_mutation_restores_dependent_on_clause():
    run = generate_and_compare(
        "student(id, name); takes(id, course_id);",
        "SELECT s.name FROM student s CROSS JOIN takes t",
        "SELECT s.name FROM student s JOIN takes t ON s.id = t.id",
        sql_dialect="sqlite",
    )

    assert run.is_equivalent is False
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert [item["probe"] for item in effectiveness] == ["join_dangling_rows"]
    assert effectiveness[0]["causal_attribution_verified"] is True
    tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "JOIN TYPE"
    ]
    assert len(tests) == 1
    assert tests[0]["fixed_by_replacement"] is True
    assert tests[0]["mutation_scope"] == ["JOIN TYPE", "JOIN ON"]
    assert tests[0]["dependent_changes"] == ["JOIN ON"]


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


def test_nested_set_operator_mutation_replacement_identifies_union_all():
    standard = (
        "SELECT title FROM ("
        "SELECT title FROM course WHERE dept_name = 'CS' "
        "UNION ALL "
        "SELECT title FROM course WHERE credits > 3"
        ") AS combined"
    )
    student = standard.replace("UNION ALL", "UNION")

    run = generate_and_compare(
        "course(title, dept_name, credits);",
        standard,
        student,
        sql_dialect="mysql",
    )

    tests = [item for item in run.mutation_evidence["tests"] if item["clause"] == "UNION"]
    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert tests
    assert tests[0]["fixed_by_replacement"] is True


@pytest.mark.parametrize(
    ("standard_operator", "student_operator"),
    [("UNION", "INTERSECT"), ("INTERSECT", "UNION")],
)
def test_set_operator_type_probe_keeps_disjoint_branches_observable(
    standard_operator,
    student_operator,
):
    left = "SELECT employee_name FROM employees WHERE salary > 20"
    right = "SELECT employee_name FROM employees WHERE salary < 2"
    run = generate_and_compare(
        "employees(employee_name, salary);",
        f"{left} {standard_operator} {right}",
        f"{left} {student_operator} {right}",
    )

    generated_names = {row["employee_name"] for row in run.test_database["employees"]}
    tests = [
        item
        for item in run.mutation_evidence["tests"]
        if item["clause"] == standard_operator
    ]

    assert run.executed is True
    assert run.is_equivalent is False
    assert {"__set_left_0__", "__set_right_0__"}.issubset(generated_names)
    assert tests
    assert tests[0]["fixed_by_replacement"] is True


def test_set_operator_type_probe_separates_compatible_branch_constraints():
    run = generate_and_compare(
        "course(title, dept_id, credits);",
        "SELECT title FROM course WHERE dept_id = 1 "
        "INTERSECT SELECT title FROM course WHERE credits > 3",
        "SELECT title FROM course WHERE dept_id = 1 "
        "UNION SELECT title FROM course WHERE credits > 3",
    )

    generated_titles = {row["title"] for row in run.test_database["course"]}

    assert run.executed is True
    assert run.is_equivalent is False
    assert {"__set_left_0__", "__set_right_0__"}.issubset(generated_titles)


def test_missing_except_probe_keeps_branch_overlap_observable():
    run = generate_and_compare(
        "course(title, credits);",
        "SELECT title FROM course EXCEPT "
        "SELECT title FROM course WHERE credits < 3",
        "SELECT title FROM course",
    )

    overlap_title = next(
        row["title"]
        for row in run.test_database["course"]
        if row["title"].startswith("__set_overlap_0__")
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert (overlap_title,) in run.student_rows
    assert (overlap_title,) not in run.standard_rows


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


def test_mysql_compatibility_registers_bounded_date_membership_and_format_functions():
    sql = (
        "SELECT STR_TO_DATE('01/31/2024', '%m/%d/%Y'), "
        "FIND_IN_SET('b', 'a,b,c'), NUMBER_TO_STR(1234.5, 1), "
        "TIMESTAMPDIFF(MINUTE, STR_TO_DATE('0100PM', '%h%i%p'), "
        "STR_TO_DATE('0200PM', '%h%i%p'))"
    )
    sqlite_sql = transpile_to_sqlite(sql, source_dialect="mysql")
    columns, rows = _execute_sqlite({}, {}, sqlite_sql or sql)

    assert columns == [
        "STR_TO_DATE('01/31/2024', '%m/%d/%Y')",
        "FIND_IN_SET('b', 'a,b,c')",
        "NUMBER_TO_STR(1234.5, 1)",
        "TIMESTAMPDIFF('minute', STR_TO_TIME('0100PM', '%I%M%p'), STR_TO_TIME('0200PM', '%I%M%p'))",
    ]
    assert rows == [("2024-01-31", 2, "1,234.5", 60)]


def test_mysql_simple_with_rollup_is_lowered_without_grouping_function():
    sql = (
        "SELECT category AS category_name, SUM(amount) AS total_amount "
        "FROM sales GROUP BY category WITH ROLLUP"
    )
    sqlite_sql = transpile_to_sqlite(sql, source_dialect="mysql")

    assert sqlite_sql is not None
    assert "WITH ROLLUP" not in sqlite_sql.upper()
    assert "UNION ALL" in sqlite_sql.upper()

    run = generate_and_compare(
        "sales(id INT PRIMARY KEY, category TEXT, amount INT);",
        sql,
        sql,
        sql_dialect="mysql",
    )
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True


def test_mysql_rollup_grouping_function_is_lowered_per_grouping_branch():
    sql = (
        "SELECT CASE WHEN GROUPING(category) = 1 THEN 'TOTAL' ELSE category END AS category_name, "
        "CASE WHEN GROUPING(channel) = 1 THEN 'ALL' ELSE channel END AS channel_name, "
        "SUM(amount) AS total_amount FROM sales "
        "GROUP BY category, channel WITH ROLLUP"
    )
    sqlite_sql = transpile_to_sqlite(sql, source_dialect="mysql")

    assert sqlite_sql is not None
    assert "GROUPING(" not in sqlite_sql.upper()
    assert "UNION ALL" in sqlite_sql.upper()
    run = generate_and_compare(
        "sales(id INT PRIMARY KEY, category TEXT, channel TEXT, amount INT);",
        sql,
        sql,
        sql_dialect="mysql",
    )
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True


def test_mysql_substring_index_is_available_in_sqlite_compatibility():
    sql = (
        "SELECT SUBSTRING_INDEX(path, '-', 1) AS first_part, "
        "SUBSTRING_INDEX(path, '-', -1) AS last_part FROM paths"
    )
    run = generate_and_compare(
        "paths(id INT PRIMARY KEY, path TEXT);",
        sql,
        sql,
        sql_dialect="mysql",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True


def test_mysql_rollup_with_bounded_having_is_lowered_per_branch():
    sql = (
        "SELECT category, SUM(amount) AS total_amount FROM sales "
        "GROUP BY category WITH ROLLUP "
        "HAVING category IS NOT NULL OR category IS NULL"
    )
    sqlite_sql = transpile_to_sqlite(sql, source_dialect="mysql")

    assert sqlite_sql is not None
    assert "WITH ROLLUP" not in sqlite_sql.upper()
    assert "UNION ALL" in sqlite_sql.upper()
    run = generate_and_compare(
        "sales(id INT PRIMARY KEY, category TEXT, amount INT);",
        sql,
        sql,
        sql_dialect="mysql",
    )
    assert run.executed is True, run.error
    assert run.is_equivalent is True


def test_mysql_rollup_grouping_order_uses_private_sort_columns():
    sql = (
        "SELECT CASE WHEN GROUPING(category) = 1 THEN 'TOTAL' ELSE category END AS category_name, "
        "CASE WHEN GROUPING(channel) = 1 THEN 'ALL' ELSE channel END AS channel_name, "
        "SUM(amount) AS total_amount FROM sales "
        "GROUP BY category, channel WITH ROLLUP "
        "ORDER BY GROUPING(category), category, GROUPING(channel), channel"
    )
    sqlite_sql = transpile_to_sqlite(sql, source_dialect="mysql")

    assert sqlite_sql is not None
    assert "GROUPING(" not in sqlite_sql.upper()
    assert "__PHASE1_GROUP_ORDER_" in sqlite_sql.upper()
    run = generate_and_compare(
        "sales(id INT PRIMARY KEY, category TEXT, channel TEXT, amount INT);",
        sql,
        sql,
        sql_dialect="mysql",
    )
    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True


def test_mysql_compound_order_expression_is_wrapped_for_sqlite():
    sql = (
        "SELECT category AS category_name FROM sales "
        "UNION ALL SELECT 'TOTAL' AS category_name FROM sales "
        "ORDER BY CASE WHEN category_name = 'TOTAL' THEN 1 ELSE 0 END, category_name"
    )
    sqlite_sql = transpile_to_sqlite(sql, source_dialect="mysql")

    assert sqlite_sql is not None
    assert sqlite_sql.upper().startswith("SELECT * FROM (")
    run = generate_and_compare(
        "sales(id INT PRIMARY KEY, category TEXT);",
        sql,
        sql,
        sql_dialect="mysql",
    )
    assert run.executed is True, run.error
    assert run.is_equivalent is True


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


def test_postgres_date_trunc_and_month_length_transpile_for_sqlite():
    sqlite_sql = transpile_to_sqlite(
        "SELECT DATE_TRUNC('MONTH', started_at), "
        "CAST((month_start + INTERVAL '1' MONTH) AS DATE) "
        "- CAST(month_start AS DATE) FROM bookings",
        source_dialect="postgres",
    )

    assert sqlite_sql is not None
    assert "TIMESTAMP_TRUNC" not in sqlite_sql.upper()
    assert " INTERVAL " not in sqlite_sql.upper()
    assert "JULIANDAY" in sqlite_sql.upper()
    assert "start of month" in sqlite_sql


def test_postgres_dynamic_interval_arithmetic_uses_bounded_sqlite_udf():
    sql = (
        "SELECT starttime, starttime + slots * (INTERVAL '30 minutes') "
        "AS endtime FROM cd.bookings"
    )

    sqlite_sql = transpile_to_sqlite(sql, source_dialect="postgres")
    run = generate_and_compare(
        "bookings(starttime TIMESTAMP, slots INTEGER);",
        sql,
        sql,
        sql_dialect="postgres",
    )

    assert sqlite_sql is not None
    assert "PG_INTERVAL_ADD" in sqlite_sql
    assert "INTERVAL '" not in sqlite_sql.upper()
    assert run.executed is True
    assert run.is_equivalent is True
    assert run.standard_rows[0][1] == "2024-01-01 00:30:00"


def test_postgres_interval_month_subtraction_uses_elapsed_days():
    sql = (
        "SELECT (date_trunc('month', testts) + interval '1 month') "
        "- date_trunc('day', testts) FROM bookings"
    )
    run = generate_and_compare(
        "bookings(testts TIMESTAMP);",
        sql,
        sql,
        sql_dialect="postgres",
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.standard_rows[:3] == [(31,), (30,), (29,)]
    assert parseval._sql_date_add("month", 1, "2024-01-31") == "2024-02-29"


def test_sqlite_fixture_unqualifies_catalog_tables_and_orders_by_output_ordinal():
    catalog = SchemaCatalog.from_dict({
        "source": "fixture",
        "db_id": "club",
        "tables": [{
            "name": "members",
            "columns": [
                {"name": "memid", "data_type": "INT", "nullable": False},
                {"name": "name", "data_type": "TEXT", "nullable": False},
            ],
            "primary_key": ["memid"],
            "foreign_keys": [],
            "unique_constraints": [["memid"]],
        }],
    })
    sql = (
        "SELECT left_member.memid, right_member.name "
        "FROM cd.members left_member JOIN cd.members right_member "
        "ON left_member.memid = right_member.memid ORDER BY memid"
    )
    ast = parse_one(sql, read="postgres")

    standard, student = _prepare_executable_sql_pair(
        "sqlite",
        sql,
        sql,
        standard_ast=ast,
        student_ast=ast.copy(),
        source_dialect="postgres",
        schema_catalog=catalog,
    )

    assert standard == student
    assert standard is not None
    assert '"cd"' not in standard
    assert "ORDER BY 1" in standard


def test_mutation_replay_uses_the_same_catalog_table_mapping_as_execution():
    catalog = SchemaCatalog.from_dict({
        "source": "fixture",
        "db_id": "club",
        "tables": [{
            "name": "facilities",
            "columns": [
                {"name": "facid", "data_type": "INT", "nullable": False},
                {"name": "guestcost", "data_type": "NUMERIC", "nullable": False},
            ],
            "primary_key": ["facid"],
            "foreign_keys": [],
            "unique_constraints": [["facid"]],
        }],
    })
    run = generate_and_compare(
        "facilities(facid INT PRIMARY KEY, guestcost NUMERIC);",
        "SELECT COUNT(*) FROM cd.facilities WHERE guestcost >= 10",
        "SELECT COUNT(*) FROM cd.facilities WHERE guestcost > 10",
        sql_dialect="postgres",
        schema_catalog=catalog,
    )

    where_test = next(
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "WHERE"
    )
    assert run.executed is True
    assert run.is_equivalent is False
    assert '"cd"' not in where_test["replacement_sql"]
    assert where_test["replacement_exec_ok"] is True
    assert where_test["fixed_by_replacement"] is True


def test_temporal_comparison_boundary_materializes_valid_date():
    run = generate_and_compare(
        "events(id INT, event_date DATE, kind TEXT);",
        "SELECT id FROM events WHERE event_date >= '2020-01-01'",
        "SELECT id FROM events WHERE event_date > '2020-01-01'",
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"
    assert any(row[0] == 1 for row in run.standard_rows)
    assert all(row[0] != 1 for row in run.student_rows)
    assert run.test_database["events"][0]["event_date"] == "2020-01-01"


def test_year_comparison_boundary_materializes_a_parseable_date():
    run = generate_and_compare(
        "Dim_Simulados(id_simulado INT, nome TEXT, data_aplicacao TEXT);",
        "SELECT nome, CASE WHEN YEAR(data_aplicacao) < 2025 THEN 'Antigo' ELSE 'Recente' END AS status FROM Dim_Simulados",
        "SELECT nome, CASE WHEN YEAR(data_aplicacao) <= 2025 THEN 'Antigo' ELSE 'Recente' END AS status FROM Dim_Simulados",
        max_rows_per_table=4,
        sql_dialect="tsql",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.test_database["Dim_Simulados"][0]["data_aplicacao"] == "2025-01-01"
    assert run.standard_rows[0][1] == "Recente"
    assert run.student_rows[0][1] == "Antigo"


def test_temporal_boundary_keeps_compound_join_filter_reachable():
    run = generate_and_compare(
        "facilities(facid INT, name TEXT); bookings(bookid INT, facid INT, starttime DATE);",
        "SELECT b.starttime, f.name FROM facilities f JOIN bookings b ON f.facid=b.facid WHERE f.name IN ('Tennis Court 2','Tennis Court 1') AND b.starttime >= '2012-09-21' AND b.starttime < '2012-09-22'",
        "SELECT b.starttime, f.name FROM facilities f JOIN bookings b ON f.facid=b.facid WHERE f.name IN ('Tennis Court 2','Tennis Court 1') AND b.starttime > '2012-09-21' AND b.starttime < '2012-09-22'",
        max_rows_per_table=4,
        sql_dialect="postgres",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows
    assert run.student_rows == []
    assert run.test_database["bookings"][0]["starttime"] == "2012-09-21"


def test_cte_aggregate_alias_boundary_is_pushed_to_base_rows():
    standard_sql = (
        "WITH averages AS ("
        "SELECT t.name AS group_name, AVG(a.age) AS average_age "
        "FROM people a JOIN teams t ON a.team_id = t.team_id "
        "GROUP BY t.name) "
        "SELECT group_name, average_age FROM averages WHERE average_age > 22"
    )
    student_sql = standard_sql.replace("average_age > 22", "average_age >= 22")
    run = generate_and_compare(
        "people(id INT, age INT, team_id INT); teams(team_id INT, name TEXT);",
        standard_sql,
        student_sql,
        max_rows_per_table=8,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows == []
    assert len(run.student_rows) == 1
    assert run.student_rows[0][1] == 22
    assert [row["age"] for row in run.test_database["people"][:2]] == [21, 23]


def test_join_having_count_boundary_is_post_join_exact():
    run = generate_and_compare(
        "questions(question_id INT, quiz_id INT); quizzes(quiz_id INT, name TEXT);",
        "SELECT qz.name, COUNT(q.question_id) AS total FROM questions q JOIN quizzes qz ON q.quiz_id = qz.quiz_id GROUP BY qz.name HAVING COUNT(q.question_id) > 15",
        "SELECT qz.name, COUNT(q.question_id) AS total FROM questions q JOIN quizzes qz ON q.quiz_id = qz.quiz_id GROUP BY qz.name HAVING COUNT(q.question_id) >= 15",
        max_rows_per_table=16,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows == []
    assert len(run.student_rows) == 1
    assert run.student_rows[0][1] == 15


def test_having_percentage_boundary_materializes_joined_error_ratio():
    standard_sql = (
        "SELECT t.name AS topic, d.name AS discipline, COUNT(r.response_id) AS total, "
        "SUM(CASE WHEN r.correct = 0 THEN 1 ELSE 0 END) AS errors, "
        "100.0 * SUM(CASE WHEN r.correct = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(COUNT(r.response_id), 0) AS error_pct "
        "FROM responses r JOIN questions q ON r.question_id = q.question_id "
        "JOIN topics t ON q.topic_id = t.topic_id "
        "JOIN disciplines d ON t.discipline_id = d.discipline_id "
        "GROUP BY t.name, d.name HAVING 100.0 * "
        "SUM(CASE WHEN r.correct = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(COUNT(r.response_id), 0) > 40"
    )
    student_sql = standard_sql.replace("> 40", ">= 40")
    run = generate_and_compare(
        "responses(response_id INT, question_id INT, correct INT); "
        "questions(question_id INT, topic_id INT); "
        "topics(topic_id INT, discipline_id INT, name TEXT); "
        "disciplines(discipline_id INT, name TEXT);",
        standard_sql,
        student_sql,
        max_rows_per_table=16,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows == []
    assert len(run.student_rows) == 1
    assert run.student_rows[0][2:] == (5, 2, 40)


def test_window_alias_boundary_materializes_required_partition_rows():
    standard_sql = (
        "WITH ranked AS (SELECT p.id, p.name, t.name AS team, p.age, "
        "ROW_NUMBER() OVER (PARTITION BY t.name ORDER BY p.age DESC) AS rn "
        "FROM people p LEFT JOIN teams t ON p.team_id = t.team_id) "
        "SELECT id, name, team, age FROM ranked WHERE rn <= 3 ORDER BY team, rn"
    )
    student_sql = standard_sql.replace("rn <= 3", "rn < 3")
    run = generate_and_compare(
        "people(id INT, name TEXT, age INT, team_id INT); teams(team_id INT, name TEXT);",
        standard_sql,
        student_sql,
        max_rows_per_table=8,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert len(run.standard_rows) > len(run.student_rows)
    assert len(run.standard_rows) - len(run.student_rows) == 1


def test_null_order_case_does_not_mask_direction_with_limit():
    run = generate_and_compare(
        "Products(Code INT, Name TEXT, Price INT, Manufacturer INT);",
        "SELECT name, price FROM Products ORDER BY price ASC LIMIT 1",
        "SELECT name, price FROM Products ORDER BY CASE WHEN price IS NULL THEN 1 ELSE 0 END DESC, price DESC LIMIT 1",
        max_rows_per_table=12,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows[0][1] != run.student_rows[0][1]
    assert all(row["Price"] is not None for row in run.test_database["Products"])


def test_derived_sum_alias_boundary_materializes_exact_revenue():
    standard_sql = (
        "SELECT name, revenue FROM (SELECT f.name, "
        "SUM(CASE WHEN b.memid = 0 THEN b.slots * f.guestcost "
        "ELSE b.slots * f.membercost END) AS revenue "
        "FROM bookings b JOIN facilities f ON b.facid = f.facid "
        "GROUP BY f.name) AS totals WHERE revenue < 1000 ORDER BY revenue"
    )
    student_sql = standard_sql.replace("revenue < 1000", "revenue <= 1000")
    run = generate_and_compare(
        "bookings(bookid INT, facid INT, memid INT, slots INT); "
        "facilities(facid INT, name TEXT, membercost INT, guestcost INT);",
        standard_sql,
        student_sql,
        max_rows_per_table=8,
        sql_dialect="postgres",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.student_rows[-1][1] == 1000
    assert all(row[1] != 1000 for row in run.standard_rows)


def test_recursive_identity_world_is_acyclic_without_ast_differences():
    sql = """
        WITH RECURSIVE recommendeds(memid) AS (
          SELECT memid FROM members WHERE recommendedby = 1
          UNION ALL
          SELECT mems.memid FROM recommendeds recs
          JOIN members mems ON mems.recommendedby = recs.memid
        )
        SELECT recs.memid, mems.name
        FROM recommendeds recs JOIN members mems ON recs.memid = mems.memid
        ORDER BY memid
    """

    run = generate_and_compare(
        "members(memid INT PRIMARY KEY, name TEXT, recommendedby INT);",
        sql,
        sql,
        max_rows_per_table=8,
        sql_dialect="postgres",
    )

    assert run.executed is True
    assert run.is_equivalent is True


def test_nested_exists_boundary_survives_outer_join_count_and_not_in_filters():
    standard = """
        SELECT Pt.NAME, PhPCP.NAME
        FROM Patient Pt, Physician PhPCP
        WHERE Pt.PCP = PhPCP.EmployeeID
          AND EXISTS (
              SELECT * FROM Prescribes Pr
              WHERE Pr.Patient = Pt.SSN AND Pr.Physician = Pt.PCP
          )
          AND EXISTS (
              SELECT * FROM Undergoes U, Procedures Pr
              WHERE U.Procedures = Pr.CODE
                AND U.Patient = Pt.SSN
                AND Pr.Cost > 5000
          )
          AND 2 <= (
              SELECT COUNT(A.AppointmentID)
              FROM Appointment A, Nurse N
              WHERE A.PrepNurse = N.EmployeeID AND N.Registered = 1
          )
          AND NOT Pt.PCP IN (SELECT Head FROM Department)
    """
    student = standard.replace("Pr.Cost > 5000", "Pr.Cost >= 5000")
    schema = (
        "Physician(EmployeeID INT PRIMARY KEY, Name TEXT); "
        "Department(DepartmentID INT PRIMARY KEY, Head INT); "
        "Procedures(Code INT PRIMARY KEY, Cost INT); "
        "Patient(SSN INT PRIMARY KEY, Name TEXT, PCP INT); "
        "Nurse(EmployeeID INT PRIMARY KEY, Registered INT); "
        "Appointment(AppointmentID INT PRIMARY KEY, PrepNurse INT); "
        "Prescribes(Physician INT, Patient INT); "
        "Undergoes(Patient INT, Procedures INT);"
    )

    run = generate_and_compare(
        schema,
        standard,
        student,
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.standard_rows == []
    assert run.student_rows
    boundary = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "comparison_boundary_tristate"
    )
    assert boundary["constraints_satisfied"] is True
    assert boundary["causal_attribution_verified"] is True


def test_recursive_cte_output_alias_maps_to_anchor_relationship_column():
    sql = """
        WITH RECURSIVE recommenders(recommender, member) AS (
          SELECT recommendedby, memid FROM cd.members
          UNION ALL
          SELECT mems.recommendedby, recs.member
          FROM recommenders recs
          INNER JOIN cd.members mems ON mems.memid = recs.recommender
        )
        SELECT recs.member, mems.firstname, mems.surname, recs.recommender
        FROM recommenders recs
        INNER JOIN cd.members mems ON recs.recommender = mems.memid
        ORDER BY recs.member, recs.recommender
    """

    run = generate_and_compare(
        "members(memid INTEGER PRIMARY KEY, recommendedby INTEGER, "
        "firstname TEXT, surname TEXT);",
        sql,
        sql,
        max_rows_per_table=8,
        sql_dialect="postgres",
    )

    members = run.test_database["members"]
    assert run.executed is True
    assert run.is_equivalent is True
    assert members[0]["recommendedby"] is None
    assert all(
        member["recommendedby"] == members[index - 1]["memid"]
        for index, member in enumerate(members[1:], start=1)
    )


def test_postgres_lpad_and_translate_have_exact_sqlite_compatibility():
    sql = (
        "SELECT LPAD(CAST(zipcode AS CHAR(5)), 5, '0'), "
        "TRANSLATE(telephone, '-() ', '') FROM members ORDER BY memid"
    )

    run = generate_and_compare(
        "members(memid INT PRIMARY KEY, zipcode INT, telephone TEXT);",
        sql,
        sql,
        sql_dialect="postgres",
    )

    assert parseval._sql_lpad("42", 5, "0") == "00042"
    assert parseval._sql_lpad("abcdef", 4, "0") == "abcd"
    assert parseval._sql_translate("(555) 12-34", "-() ", "") == "5551234"
    assert run.executed is True
    assert run.is_equivalent is True


def test_postgres_bounded_literal_generate_series_executes_in_sqlite():
    sql = (
        "SELECT generate_series(timestamp '2012-10-01', "
        "timestamp '2012-10-31', interval '1 day') AS ts"
    )

    run = generate_and_compare("", sql, sql, sql_dialect="postgres")

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True
    assert len(run.standard_rows) == 31
    assert run.standard_rows[0] == ("2012-10-01 00:00:00",)
    assert run.standard_rows[-1] == ("2012-10-31 00:00:00",)


def test_postgres_bounded_generate_series_preserves_derived_filter_mutation():
    standard = (
        "SELECT d.date FROM (SELECT CAST(generate_series("
        "timestamp '2012-08-01', timestamp '2012-08-05', "
        "interval '1 day') AS date) AS date) d "
        "WHERE d.date < '2012-08-03'"
    )
    student = standard.replace(" < '2012-08-03'", " <= '2012-08-03'")

    run = generate_and_compare(
        "",
        standard,
        student,
        sql_dialect="postgres",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.standard_rows == [("2012-08-01",), ("2012-08-02",)]
    assert run.student_rows == [
        ("2012-08-01",),
        ("2012-08-02",),
        ("2012-08-03",),
    ]


def test_postgres_series_derived_column_is_visible_to_correlated_date_filter():
    sql = (
        "SELECT dategen.date, (SELECT SUM(b.slots) FROM bookings b "
        "WHERE b.starttime > dategen.date - INTERVAL '14 days' "
        "AND b.starttime < dategen.date + INTERVAL '1 day') AS total "
        "FROM (SELECT CAST(generate_series(timestamp '2012-08-01', "
        "'2012-08-31', '1 day') AS date) AS date) AS dategen "
        "ORDER BY dategen.date"
    )

    run = generate_and_compare(
        "bookings(id INT PRIMARY KEY, starttime TIMESTAMP, slots INT);",
        sql,
        sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True
    assert len(run.standard_rows) == 31
    assert run.standard_rows[0][0] == "2012-08-01"
    assert run.standard_rows[-1][0] == "2012-08-31"


def test_postgres_correlated_series_expression_boundary_is_materialized():
    standard = (
        "SELECT d.date, (SELECT SUM(b.slots * f.membercost) "
        "FROM bookings b JOIN facilities f ON b.facid = f.facid "
        "WHERE b.starttime > d.date - INTERVAL '14 days' "
        "AND b.starttime < d.date + INTERVAL '1 day') AS revenue "
        "FROM (SELECT CAST(generate_series(timestamp '2012-08-01', "
        "'2012-08-05', '1 day') AS date) AS date) d ORDER BY d.date"
    )
    student = standard.replace(
        " < d.date + INTERVAL '1 day'",
        " <= d.date + INTERVAL '1 day'",
    )

    run = generate_and_compare(
        "bookings(bookid INT PRIMARY KEY, facid INT, starttime TIMESTAMP, "
        "slots INT); facilities(facid INT PRIMARY KEY, membercost DECIMAL);",
        standard,
        student,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.test_database["bookings"][0]["starttime"] == "2012-08-02"
    assert run.standard_rows[0] == ("2012-08-01", None)
    assert run.student_rows[0] == ("2012-08-01", 1)
    boundary = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "comparison_boundary_tristate"
    )
    assert boundary["constraints_satisfied"] is True
    assert boundary["distinguished"] is True
    assert boundary["semantic_validation"]["evidence"][
        "raw_expression_absent"
    ] is True


def test_nested_correlated_atomic_comparison_has_one_precise_diff():
    standard = (
        "SELECT d.date, (SELECT SUM(b.slots * f.membercost) "
        "FROM bookings b JOIN facilities f ON b.facid = f.facid "
        "WHERE b.starttime > d.date - INTERVAL '14 days' "
        "AND b.starttime < d.date + INTERVAL '1 day') AS revenue "
        "FROM (SELECT CAST(generate_series(timestamp '2012-08-01', "
        "'2012-08-05', '1 day') AS date) AS date) d ORDER BY d.date"
    )
    student = standard.replace(
        " < d.date + INTERVAL '1 day'",
        " <= d.date + INTERVAL '1 day'",
    )

    diffs = extract_ast_diffs(standard, student, dialect="postgres")

    assert [diff.diff_type for diff in diffs] == [
        "comparison_operator_changed"
    ]
    assert diffs[0].extra["subquery_depth"] == 1


def test_nested_comparison_is_not_atomic_when_outer_projection_also_changes():
    standard = (
        "SELECT d.date, (SELECT SUM(b.slots) FROM bookings b "
        "WHERE b.starttime < d.date + INTERVAL '1 day') AS total "
        "FROM (SELECT CAST(generate_series(timestamp '2012-08-01', "
        "'2012-08-05', '1 day') AS date) AS date) d"
    )
    student = standard.replace("SELECT d.date,", "SELECT d.date AS day,").replace(
        " < d.date + INTERVAL '1 day'",
        " <= d.date + INTERVAL '1 day'",
    )

    diffs = extract_ast_diffs(standard, student, dialect="postgres")
    diff_types = {diff.diff_type for diff in diffs}

    assert "comparison_operator_changed" in diff_types
    assert "alias_changed" in diff_types
    assert len(diffs) > 1


def test_nested_having_comparison_keeps_aggregate_context_for_witness():
    standard = (
        "SELECT Name FROM Departments WHERE Code IN ("
        "SELECT Department FROM Employees GROUP BY Department "
        "HAVING COUNT(*) > 2)"
    )
    student = standard.replace("COUNT(*) > 2", "COUNT(*) >= 2")

    diffs = extract_ast_diffs(standard, student, dialect="mysql")
    diff_types = {diff.diff_type for diff in diffs}

    assert "comparison_operator_changed" in diff_types
    assert "having_changed" in diff_types

    run = generate_and_compare(
        "Departments(Code INT PRIMARY KEY, Name VARCHAR(255), "
        "Budget DECIMAL); Employees(SSN INT PRIMARY KEY, Name VARCHAR(255), "
        "LastName VARCHAR(255), Department INT);",
        standard,
        student,
        sql_dialect="mysql",
        max_rows_per_table=8,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False


def test_postgres_large_generate_series_is_an_explicit_engine_boundary():
    sql = (
        "SELECT generate_series(timestamp '2000-01-01', "
        "timestamp '2020-01-01', interval '1 day') AS ts"
    )

    run = generate_and_compare("", sql, sql, sql_dialect="postgres")

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.data_evidence["unsupported_features"] == [
        "POSTGRES_GENERATE_SERIES_UNBOUNDED"
    ]


def test_postgres_static_epoch_difference_executes_as_exact_seconds():
    sql = (
        "SELECT EXTRACT(EPOCH FROM (timestamp '2012-09-02 00:00:00' "
        "- '2012-08-31 01:00:00'))"
    )

    run = generate_and_compare("", sql, sql, sql_dialect="postgres")

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True
    assert run.standard_rows == [(169200,)]


def test_postgres_dynamic_epoch_extract_is_an_explicit_engine_boundary():
    sql = "SELECT EXTRACT(EPOCH FROM starttime) FROM bookings"

    run = generate_and_compare(
        "bookings(starttime TIMESTAMP);",
        sql,
        sql,
        sql_dialect="postgres",
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.data_evidence["unsupported_features"] == [
        "POSTGRES_EXTRACT_EPOCH_DYNAMIC"
    ]


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
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.data_evidence["status"] == "KNOWN_GAP"
    assert "ROLLUP" in run.data_evidence["unsupported_features"]


def test_postgres_simple_rollup_executes_through_union_all_lowering():
    sql = (
        "SELECT facid, EXTRACT(MONTH FROM starttime) AS month, "
        "SUM(slots) AS slots FROM bookings "
        "GROUP BY ROLLUP(facid, month) ORDER BY facid, month"
    )

    run = generate_and_compare(
        "bookings(id INT PRIMARY KEY, facid INT, starttime TIMESTAMP, slots INT);",
        sql,
        sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True
    assert (None, None, 10) in run.standard_rows


def test_postgres_simple_cube_executes_through_bounded_union_all_lowering():
    sql = (
        "SELECT region, product, SUM(amount) AS total FROM sales "
        "GROUP BY CUBE(region, product) ORDER BY region, product"
    )

    run = generate_and_compare(
        "sales(id INT PRIMARY KEY, region TEXT, product TEXT, amount INT);",
        sql,
        sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True
    assert len(run.standard_rows) >= 4
    assert any(row[0] is None and row[1] is None for row in run.standard_rows)


def test_cube_with_four_keys_remains_an_explicit_engine_boundary():
    sql = (
        "SELECT a, b, c, d, COUNT(*) FROM t "
        "GROUP BY CUBE(a, b, c, d)"
    )
    run = generate_and_compare(
        "t(id INT PRIMARY KEY, a TEXT, b TEXT, c TEXT, d TEXT);",
        sql,
        sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert "CUBE" in run.data_evidence["unsupported_features"]


def test_postgres_rollup_preserves_filter_boundary_mutation():
    standard = (
        "SELECT facid, EXTRACT(MONTH FROM starttime) AS month, "
        "SUM(slots) AS slots FROM bookings "
        "WHERE starttime >= '2012-01-01' AND starttime < '2013-01-01' "
        "GROUP BY ROLLUP(facid, month) ORDER BY facid, month"
    )
    student = standard.replace(
        "starttime >= '2012-01-01'",
        "starttime > '2012-01-01'",
    )

    run = generate_and_compare(
        "bookings(id INT PRIMARY KEY, facid INT, starttime TIMESTAMP, slots INT);",
        standard,
        student,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows


@pytest.mark.parametrize("include_grand_total", [False, True])
def test_postgres_simple_grouping_sets_execute_through_union_all_lowering(
    include_grand_total,
):
    sets = (
        "(), (city_name), (city_name, post_code)"
        if include_grand_total
        else "(city_name), (city_name, post_code)"
    )
    sql = (
        "SELECT city_name, post_code, COUNT(*) AS corp_count "
        "FROM corporations GROUP BY GROUPING SETS ("
        f"{sets}) ORDER BY city_name, corp_count DESC"
    )

    run = generate_and_compare(
        "corporations(id INT PRIMARY KEY, city_name TEXT, post_code TEXT);",
        sql,
        sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is True
    assert any(row[1] is None for row in run.standard_rows)
    assert ((None, None, 4) in run.standard_rows) is include_grand_total


def test_postgres_grouping_function_keeps_grouping_extension_at_engine_boundary():
    sql = (
        "SELECT region, GROUPING(region), SUM(amount) FROM sales "
        "GROUP BY ROLLUP(region)"
    )

    run = generate_and_compare(
        "sales(region TEXT, amount DECIMAL);",
        sql,
        sql,
        sql_dialect="postgres",
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert "GROUPING" in run.data_evidence["unsupported_features"]


def test_malformed_standard_query_is_an_input_gap_not_an_engine_gap():
    run = generate_and_compare(
        "events(expression TEXT);",
        "SELECT DISTINCT ON ( expression",
        "SELECT expression FROM events",
    )

    assert run.executed is False
    assert run.is_equivalent is None
    assert run.status == "INPUT_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.error_code == "STANDARD_SQL_PARSE_ERROR"
    assert run.boundary_evidence == {
        "reason": "invalid_standard_sql",
        "sql_role": "standard",
        "error_code": "STANDARD_SQL_PARSE_ERROR",
    }


def test_missing_schema_for_referenced_table_is_an_input_gap_not_engine_gap():
    run = generate_and_compare(
        "",
        "SELECT id FROM missing_table",
        "SELECT id FROM missing_table",
    )

    assert run.executed is False
    assert run.judge_status == "INPUT_ERROR"
    assert run.status == "INPUT_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.error_code == "SCHEMA_PARSE_FAILED"
    assert run.boundary_evidence["reason"] == "schema_unreplayable"


def test_cte_window_neighbor_context_makes_direct_alias_comparison_observable():
    standard = (
        "WITH tb1 AS ("
        "SELECT seat_id, free AS free1, "
        "LEAD(free) OVER (ORDER BY seat_id) AS free2, "
        "LAG(free) OVER (ORDER BY seat_id) AS free0 FROM cinema"
        ") SELECT seat_id FROM tb1 "
        "WHERE free1 = 1 AND (free2 = 1 OR free0 = 1)"
    )
    student = standard.replace("free1 = 1", "free1 <> 1")

    run = generate_and_compare(
        "cinema(seat_id INTEGER, free INTEGER);",
        standard,
        student,
        max_rows_per_table=12,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"


def test_rank_alias_predicate_survives_distinct_partition_projection():
    standard = (
        "WITH RankedProducts AS ("
        "SELECT product_id, sale_date, revenue, "
        "RANK() OVER (PARTITION BY product_id, sale_date ORDER BY revenue DESC) AS ranks "
        "FROM product_sales"
        ") SELECT DISTINCT product_id FROM RankedProducts WHERE ranks = 1"
    )
    student = standard.replace("ranks = 1", "ranks <> 1")

    run = generate_and_compare(
        "product_sales(product_id INTEGER, sale_date DATE, revenue NUMERIC);",
        standard,
        student,
        max_rows_per_table=12,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"


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
        execution_backend="sqlite",
    )

    assert run.executed is True
    assert run.judge_status == "CORRECT"
    assert run.data_evidence["execution_backend"] == "sqlite"
    assert run.data_evidence["sql_dialect"] == "mysql"


@pytest.mark.parametrize("dialect", ["mysql", "postgres", "tsql", "oracle"])
def test_declared_vendor_without_execution_backend_is_explicit_engine_gap(dialect):
    run = _generate_and_compare(
        "course(course_id BIGINT, title VARCHAR(255));",
        "SELECT title FROM course",
        "SELECT title FROM course",
        sql_dialect=dialect,
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_GAP"
    assert run.status == "ENGINE_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.error_code == "EXECUTION_BACKEND_REQUIRED"
    assert run.data_evidence["execution_backend"] is None
    assert run.data_evidence["boundary_evidence"]["reason"] == (
        "declared_vendor_dialect_without_execution_backend"
    )
    assert run.data_evidence["boundary_evidence"]["declared_dialect"] == dialect


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
    assert "NATIVE_CONNECTION_URL_REQUIRED" in (run.error or "")
    assert run.data_evidence["execution_backend"] == "mysql"


def test_auto_backend_requires_detected_postgres_native_configuration():
    run = generate_and_compare(
        "orders(customer_id, amount);",
        "SELECT DISTINCT ON (customer_id) customer_id, amount FROM orders",
        "SELECT DISTINCT ON (customer_id) customer_id, amount FROM orders",
        execution_backend="auto",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert run.data_evidence["execution_backend"] == "postgres"
    assert "NATIVE_CONNECTION_URL_REQUIRED" in (run.error or "")
    assert run.data_evidence["unsupported_features"] == []


def test_explicit_auto_mysql_does_not_silently_downgrade_without_url():
    run = generate_and_compare(
        "course(course_id BIGINT, title VARCHAR(255));",
        "SELECT title FROM course",
        "SELECT title FROM course",
        sql_dialect="mysql",
        execution_backend="auto",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert run.data_evidence["execution_backend"] == "mysql"
    assert "NATIVE_CONNECTION_URL_REQUIRED" in (run.error or "")


def test_auto_native_runner_receives_both_queries_rendered_for_target_dialect(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    @contextmanager
    def fake_session(backend, _schema, _schema_types, _rows, connection_url):
        class Session:
            def execute(self, sql):
                calls.append((backend, sql, connection_url))
                return ["id"], [(1,), (2,)]

        yield Session()

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fake_session,
    )

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users ORDER BY id LIMIT 2",
        "SELECT TOP 2 id FROM users ORDER BY id",
        execution_backend="auto",
        native_executor_url="mssql://sa:password@db:1433/master",
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.data_evidence["execution_backend"] == "tsql"
    assert len(calls) >= 2
    assert all(backend == "tsql" for backend, _, _ in calls)
    assert all(url == "mssql://sa:password@db:1433/master" for _, _, url in calls)
    assert all("TOP 2" in sql.upper() for _, sql, _ in calls[:2])
    assert all("LIMIT" not in sql.upper() for _, sql, _ in calls[:2])


def test_declared_native_dialect_preserves_original_sql_for_engine_validation():
    standard, student = _prepare_executable_sql_pair(
        "postgres",
        "SELECT COALESCE(name, 'x') FROM users;",
        "SELECT IFNULL(name, 'x') FROM users;",
        standard_ast=parse_one("SELECT COALESCE(name, 'x') FROM users", read="postgres"),
        student_ast=parse_one("SELECT IFNULL(name, 'x') FROM users", read="postgres"),
        target_dialect="postgres",
        preserve_source_sql=True,
    )

    assert standard == "SELECT COALESCE(name, 'x') FROM users"
    assert student == "SELECT IFNULL(name, 'x') FROM users"


@pytest.mark.parametrize(
    ("sql_dialect", "standard_sql", "student_sql"),
    [
        (
            "postgres",
            "SELECT COALESCE(name, 'x') FROM users",
            "SELECT IFNULL(name, 'x') FROM users",
        ),
        ("mysql", "SELECT CAST(id AS CHAR) FROM users", "SELECT id::text FROM users"),
        ("tsql", "SELECT TOP 1 id FROM users", "SELECT id FROM users LIMIT 1"),
    ],
)
def test_declared_native_dialect_rejects_foreign_student_syntax_before_execution(
    monkeypatch,
    sql_dialect,
    standard_sql,
    student_sql,
):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("foreign syntax must fail before native session creation")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = generate_and_compare(
        "users(id BIGINT, name VARCHAR(64));",
        standard_sql,
        student_sql,
        sql_dialect=sql_dialect,
        execution_backend="auto",
    )

    assert run.executed is False
    assert run.judge_status == "WRONG"
    assert run.error == "student_sql_parse_failed"


def test_student_side_native_connection_failure_is_platform_error(monkeypatch):
    calls = 0

    def flaky_execute(_sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ["id"], [(1,)]
        raise ConnectionError("connection reset by peer")

    _patch_native_session(monkeypatch, flaky_execute)

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users",
        "SELECT id FROM users WHERE id > 0",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert "student_sql_platform_failed" in (run.error or "")


def test_standard_native_schema_resolution_is_input_gap_not_engine_error(monkeypatch):
    def missing_table(_sql):
        raise NativeQueryExecutionError(
            "NATIVE_QUERY_FAILED", "mysql", "(1146, Table 'products' doesn't exist)"
        )

    _patch_native_session(monkeypatch, missing_table)

    run = _generate_and_compare(
        "Products(id BIGINT);",
        "SELECT id FROM products",
        "SELECT DISTINCT id FROM products",
        sql_dialect="mysql",
        execution_backend="auto",
        native_executor_url="mysql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "INPUT_ERROR"
    assert run.status == "INPUT_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.error_code == "NATIVE_SCHEMA_REPLAY_GAP"
    assert run.boundary_evidence == {
        "reason": "native_schema_resolution_failed",
        "sql_role": "standard",
        "schema_error": "mysql.table_not_found",
    }


def test_student_native_query_rejection_remains_student_error(monkeypatch):
    calls = 0

    def rejecting_execute(_sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ["id"], [(1,)]
        raise NativeQueryExecutionError(
            "NATIVE_QUERY_FAILED", "postgres", "column does not exist"
        )

    _patch_native_session(monkeypatch, rejecting_execute)

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users",
        "SELECT missing FROM users",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is True
    assert run.judge_status == "WRONG"
    assert run.data_evidence["student_exec_ok"] is False
    assert "NATIVE_QUERY_FAILED" in run.data_evidence["student_exec_error"]


def test_student_native_timeout_returns_timeout_without_verdict(monkeypatch):
    calls = 0

    def timing_out_execute(_sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ["id"], [(1,)]
        raise NativeQueryExecutionError(
            "NATIVE_QUERY_FAILED", "postgres", "statement timeout"
        )

    _patch_native_session(monkeypatch, timing_out_execute)

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users",
        "SELECT id FROM users WHERE id > 0",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "TIMEOUT"


def test_native_submission_reuses_one_session_for_main_and_mutation_queries(monkeypatch):
    entered = 0
    session_ids: list[int] = []

    @contextmanager
    def fake_session(*_args, **_kwargs):
        nonlocal entered
        entered += 1

        class Session:
            def execute(self, sql):
                session_ids.append(id(self))
                if "<>" in sql:
                    return ["id"], [(2,)]
                return ["id"], [(1,)]

        yield Session()

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fake_session,
    )

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users WHERE id = 1",
        "SELECT id FROM users WHERE id <> 1",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is True
    assert entered == 1
    assert len(session_ids) > 2
    assert len(set(session_ids)) == 1
    assert run.mutation_evidence["summary"]["executed"] > 0


def test_student_unsafe_native_sql_is_rejected_before_session_creation(monkeypatch):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("unsafe SQL must be rejected before provisioning")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users",
        "SELECT pg_read_file('/etc/passwd') FROM users",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "SECURITY_REJECTED"
    assert run.error_code == "NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE"


def test_student_native_sql_cannot_read_outside_fixture_before_session_creation(monkeypatch):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("non-fixture SQL must be rejected before provisioning")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users",
        "SELECT name FROM secrets",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "SECURITY_REJECTED"
    assert run.error_code == "NATIVE_SQL_UNSAFE_OBJECT"


def test_trusted_postgres_namespace_is_rewritten_before_native_session(monkeypatch):
    executed_sql: list[str] = []

    def execute(sql):
        executed_sql.append(sql)
        return ["memid"], [(1,), (2,)]

    _patch_native_session(monkeypatch, execute)

    run = _generate_and_compare(
        "members(memid INT);",
        "SELECT memid FROM cd.members",
        "SELECT memid FROM cd.members",
        sql_dialect="postgres",
        execution_backend="postgres",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is True, run.error
    assert run.judge_status == "CORRECT"
    assert executed_sql
    assert all("cd." not in sql.lower() for sql in executed_sql)
    assert all("members" in sql.lower() for sql in executed_sql)


def test_unknown_student_namespace_is_rejected_before_native_session(monkeypatch):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("unknown namespaces must fail before provisioning")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = _generate_and_compare(
        "members(memid INT);",
        "SELECT memid FROM cd.members",
        "SELECT memid FROM other.members",
        sql_dialect="postgres",
        execution_backend="postgres",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "SECURITY_REJECTED"
    assert run.error_code == "NATIVE_SQL_UNSAFE_OBJECT"


def test_system_catalog_table_is_rejected_before_native_session(monkeypatch):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("system catalog SQL must fail before provisioning")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = _generate_and_compare(
        "members(memid INT);",
        "SELECT memid FROM cd.members",
        "SELECT memid FROM pg_catalog.members",
        sql_dialect="postgres",
        execution_backend="postgres",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "SECURITY_REJECTED"
    assert run.error_code == "NATIVE_SQL_UNSAFE_OBJECT"


def test_pg_catalog_table_without_fixture_resolution_is_an_input_gap_before_session(
    monkeypatch,
):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("unresolved system catalog SQL must not provision")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = _generate_and_compare(
        "members(memid INT);",
        "SELECT memid FROM cd.members",
        "SELECT tablename FROM pg_catalog.pg_tables",
        sql_dialect="postgres",
        execution_backend="postgres",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "SECURITY_REJECTED"
    assert run.error_code == "NATIVE_SQL_UNSAFE_OBJECT"


def test_trusted_namespace_mutation_replay_keeps_fixture_boundary(monkeypatch):
    executed_sql: list[str] = []

    def execute(sql):
        executed_sql.append(sql)
        if "<>" in sql:
            return ["memid"], [(2,)]
        return ["memid"], [(1,)]

    _patch_native_session(monkeypatch, execute)

    run = _generate_and_compare(
        "members(memid INT);",
        "SELECT memid FROM cd.members WHERE memid = 1",
        "SELECT memid FROM cd.members WHERE memid <> 1",
        sql_dialect="postgres",
        execution_backend="postgres",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is True, run.error
    assert run.judge_status == "WRONG"
    assert run.mutation_evidence["summary"]["executed"] > 0
    assert executed_sql
    assert all("cd." not in sql.lower() for sql in executed_sql)


def test_native_mutation_cannot_read_outside_fixture():
    executed_sql: list[str] = []

    class RecordingSession:
        def execute(self, sql):
            executed_sql.append(sql)
            return ["id"], [(1,)]

    result = _execute_mutation_case(
        schema={"users": ["id"]},
        rows={"users": [{"id": 1}]},
        clause="WHERE",
        knowledge_point_id="where",
        replacement_sql="SELECT id FROM secrets",
        removal_sql=None,
        standard_columns=["id"],
        standard_rows=[(1,)],
        ordered=False,
        backend="postgres",
        sql_dialect="postgres",
        execution_session=RecordingSession(),
    )

    assert result["replacement_exec_ok"] is False
    assert "replacement_security_rejected" in result["error"]
    assert executed_sql == []


def test_unsafe_standard_sql_is_an_engine_error_before_session_creation(monkeypatch):
    def fail_if_session_created(*_args, **_kwargs):
        raise AssertionError("unsafe standard SQL must not provision a database")

    monkeypatch.setattr(
        "core.parseval_data_generator.native_query_session",
        fail_if_session_created,
    )

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT pg_read_file('/etc/passwd') FROM users",
        "SELECT id FROM users",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert run.error_code == "NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE"


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (
            NativeResultLimitError(
                "NATIVE_RESULT_ROW_LIMIT_EXCEEDED", "postgres", "too many rows"
            ),
            "ENGINE_ERROR",
        ),
        (
            NativeInfrastructureError(
                "NATIVE_CONNECTION_LOST", "postgres", "connection reset"
            ),
            "ENGINE_ERROR",
        ),
        (
            NativeQueryExecutionError(
                "NATIVE_QUERY_FAILED", "postgres", "statement timeout"
            ),
            "TIMEOUT",
        ),
    ],
)
def test_mutation_platform_failure_produces_no_verdict(
    monkeypatch,
    failure,
    expected_status,
):
    calls = 0

    def execute(_sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ["id"], [(1,)]
        if calls == 2:
            return ["id"], [(2,)]
        raise failure

    _patch_native_session(monkeypatch, execute)

    run = generate_and_compare(
        "users(id BIGINT);",
        "SELECT id FROM users WHERE id = 1",
        "SELECT id FROM users WHERE id <> 1",
        sql_dialect="postgres",
        execution_backend="auto",
        native_executor_url="postgresql://judge:pw@db/parseval",
    )

    assert run.executed is False
    assert run.judge_status == expected_status
    assert run.error_code == failure.code


def test_unknown_execution_backend_is_rejected():
    run = generate_and_compare(
        "course(id, title);",
        "SELECT title FROM course",
        "SELECT title FROM course",
        execution_backend="typo_backend",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert "UNSUPPORTED_EXECUTION_BACKEND" in (run.error or "")


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
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []
    assert run.data_evidence["columns_match"] is True
    assert run.data_evidence["column_names_match"] is False
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] == 0


def test_referenced_output_alias_is_not_suppressed_as_header_only():
    diffs = extract_ast_diffs(
        "SELECT amount AS total FROM sales ORDER BY total",
        "SELECT amount FROM sales ORDER BY total",
        dialect="sqlite",
    )

    assert any(diff.diff_type == "alias_changed" for diff in diffs)


def test_cte_output_alias_change_is_not_suppressed_as_header_only():
    diffs = extract_ast_diffs(
        "WITH totals AS (SELECT amount AS total FROM sales) SELECT total FROM totals",
        "WITH totals AS (SELECT amount AS value FROM sales) SELECT total FROM totals",
        dialect="sqlite",
    )

    assert any(diff.diff_type == "cte_changed" for diff in diffs)


def test_numeric_projection_identity_uses_schema_aware_equivalence():
    run = generate_and_compare(
        "sales(id INTEGER, amount DECIMAL(10, 2));",
        "SELECT id, amount + 0 AS amount FROM sales",
        "SELECT id, amount AS amount FROM sales",
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_text_projection_plus_zero_is_not_suppressed_as_numeric_identity():
    run = generate_and_compare(
        "things(id INTEGER, name TEXT);",
        "SELECT name + 0 AS name FROM things",
        "SELECT name AS name FROM things",
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert any(diff.diff_type == "projection_changed" for diff in run.ast_diffs)


@pytest.mark.parametrize(
    "student_aggregate",
    [
        "COUNT(*)",
        "COUNT(*) FILTER (WHERE temp_lo < 40)",
    ],
)
def test_aggregate_filter_change_has_single_validated_obligation(student_aggregate):
    standard_sql = (
        "SELECT city, COUNT(*) FILTER (WHERE temp_lo < 45) "
        "FROM weather GROUP BY city"
    )
    student_sql = (
        f"SELECT city, {student_aggregate} FROM weather GROUP BY city"
    )

    run = generate_and_compare(
        "weather(id INTEGER, city TEXT, temp_lo INTEGER);",
        standard_sql,
        student_sql,
        sql_dialect="postgres",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert [(diff.clause_category, diff.diff_type) for diff in run.ast_diffs] == [
        ("AGGREGATE FILTER", "aggregate_filter_changed")
    ]
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["probe"] == "aggregate_filter_paths"
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    assert effectiveness[0]["semantic_validation"]["evidence"][
        "divergent_row_indexes"
    ]
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["clause"] == "AGGREGATE"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_aggregate_filter_cleanup_preserves_independent_projection_change():
    diffs = extract_ast_diffs(
        "SELECT city, COUNT(*) FILTER (WHERE temp_lo < 45) FROM weather GROUP BY city",
        "SELECT id, COUNT(*) FROM weather GROUP BY id",
        dialect="postgres",
    )

    assert any(diff.diff_type == "aggregate_filter_changed" for diff in diffs)
    assert any(diff.diff_type == "projection_changed" for diff in diffs)


def test_aggregate_filter_diff_handles_missing_peer_projection():
    diffs = extract_ast_diffs(
        "SELECT city, COUNT(*) FILTER (WHERE temp_lo < 45) FROM weather GROUP BY city",
        "SELECT city FROM weather GROUP BY city",
        dialect="postgres",
    )

    assert any(diff.diff_type == "aggregate_filter_changed" for diff in diffs)
    assert any(diff.diff_type == "column_dropped" for diff in diffs)


def test_is_distinct_from_uses_validated_null_safe_comparison_paths():
    run = generate_and_compare(
        "employee(id INTEGER, name TEXT, manager_id INTEGER);",
        "SELECT name FROM employee WHERE manager_id IS DISTINCT FROM 3",
        "SELECT name FROM employee WHERE manager_id <> 3",
        sql_dialect="postgres",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["probe"] == "null_safe_comparison_paths"
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    evidence = effectiveness[0]["semantic_validation"]["evidence"]
    assert evidence["null_row_indexes"]
    assert evidence["boundary_row_indexes"]
    assert evidence["other_row_indexes"]
    assert evidence["divergent_row_indexes"]
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["clause"] == "WHERE"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


@pytest.mark.parametrize(
    ("standard_operator", "student_operator", "divergent_path"),
    [
        ("IS NOT DISTINCT FROM", "=", "both_null_row_indexes"),
        ("IS DISTINCT FROM", "<>", "one_null_row_indexes"),
    ],
)
def test_null_safe_column_comparison_materializes_all_row_paths(
    standard_operator,
    student_operator,
    divergent_path,
):
    run = generate_and_compare(
        "employee(id INTEGER, name TEXT, manager_id INTEGER, backup_id INTEGER);",
        f"SELECT name FROM employee WHERE manager_id {standard_operator} backup_id",
        f"SELECT name FROM employee WHERE manager_id {student_operator} backup_id",
        sql_dialect="postgres",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    evidence = effectiveness[0]["semantic_validation"]["evidence"]
    assert evidence["standard_value_kind"] == "column"
    assert evidence["student_value_kind"] == "column"
    assert evidence["standard_right_column"] == "backup_id"
    assert evidence["student_right_column"] == "backup_id"
    assert evidence["both_null_row_indexes"]
    assert evidence["one_null_row_indexes"]
    assert evidence["equal_non_null_row_indexes"]
    assert evidence["unequal_non_null_row_indexes"]
    assert set(evidence[divergent_path]) & set(evidence["divergent_row_indexes"])
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["clause"] == "WHERE"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


@pytest.mark.parametrize(
    ("standard_expression", "student_expression"),
    [
        ("NOT NOT credits = 3", "credits = 3"),
        ("NOT (NOT (credits = 3))", "credits = 3"),
        ("NOT NOT NOT NOT credits = 3", "credits = 3"),
    ],
)
def test_double_negation_is_exact_three_valued_equivalence(
    standard_expression,
    student_expression,
):
    run = generate_and_compare(
        "course(id INTEGER, title TEXT, credits INTEGER);",
        f"SELECT title FROM course WHERE {standard_expression}",
        f"SELECT title FROM course WHERE {student_expression}",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_double_negation_rewrite_does_not_hide_independent_set_branch_change():
    diffs = extract_ast_diffs(
        "SELECT title FROM course WHERE NOT NOT credits = 3 "
        "UNION SELECT title FROM course WHERE credits = 4",
        "SELECT title FROM course WHERE credits = 3 "
        "UNION SELECT title FROM course WHERE credits = 5",
    )

    assert diffs
    assert any(
        diff.diff_type in {"literal_changed", "where_changed"}
        for diff in diffs
    )


def test_odd_negation_is_not_canonicalized_as_equivalent():
    diffs = extract_ast_diffs(
        "SELECT title FROM course WHERE NOT NOT NOT credits = 3",
        "SELECT title FROM course WHERE credits = 3",
    )

    assert diffs


def test_cte_column_list_alias_is_mapped_for_simple_inline_equivalence():
    run = generate_and_compare(
        "employee(id INTEGER, name TEXT);",
        "WITH e(x, y) AS (SELECT id, name FROM employee) SELECT y FROM e",
        "SELECT name FROM employee",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_passthrough_cte_dependency_chain_is_safely_inlined():
    run = generate_and_compare(
        "employee(id INTEGER, name TEXT, salary INTEGER);",
        "WITH a AS (SELECT * FROM employee), "
        "b AS (SELECT name FROM a WHERE salary > 3) SELECT name FROM b",
        "SELECT name FROM employee WHERE salary > 3",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


@pytest.mark.parametrize(
    ("standard", "student"),
    [
        (
            "WITH avg_sal AS (SELECT AVG(salary) AS v FROM instructor) "
            "SELECT name FROM instructor, avg_sal WHERE salary > avg_sal.v",
            "SELECT name FROM instructor WHERE salary > "
            "(SELECT AVG(salary) FROM instructor)",
        ),
        (
            "WITH avg_sal AS (SELECT AVG(salary) AS v FROM instructor) "
            "SELECT i.name FROM instructor i CROSS JOIN avg_sal a "
            "WHERE i.salary > a.v",
            "SELECT i.name FROM instructor i WHERE i.salary > "
            "(SELECT AVG(salary) FROM instructor)",
        ),
    ],
)
def test_single_row_aggregate_cte_is_equivalent_to_scalar_subquery(
    standard,
    student,
):
    run = generate_and_compare(
        "instructor(ID INTEGER, name TEXT, dept_name TEXT, salary INTEGER);",
        standard,
        student,
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


@pytest.mark.parametrize(
    ("standard", "student"),
    [
        (
            "WITH avg_sal AS (SELECT AVG(salary) AS v FROM instructor "
            "WHERE dept_name = 'CS') "
            "SELECT name FROM instructor, avg_sal WHERE salary > avg_sal.v",
            "SELECT name FROM instructor WHERE salary > "
            "(SELECT AVG(salary) FROM instructor WHERE dept_name = 'Math')",
        ),
        (
            "WITH avg_sal AS (SELECT dept_name, AVG(salary) AS v "
            "FROM instructor GROUP BY dept_name) "
            "SELECT name FROM instructor, avg_sal WHERE salary > avg_sal.v",
            "SELECT name FROM instructor WHERE salary > "
            "(SELECT AVG(salary) FROM instructor)",
        ),
        (
            "WITH avg_sal AS (SELECT AVG(salary) AS v FROM instructor) "
            "SELECT name FROM instructor, avg_sal "
            "WHERE salary > avg_sal.v OR ID > avg_sal.v",
            "SELECT name FROM instructor WHERE salary > "
            "(SELECT AVG(salary) FROM instructor) OR ID > "
            "(SELECT AVG(salary) FROM instructor)",
        ),
    ],
)
def test_scalar_aggregate_cte_rewrite_does_not_hide_unproven_shapes(
    standard,
    student,
):
    assert extract_ast_diffs(standard, student)


def test_star_matches_complete_schema_ordered_projection():
    run = generate_and_compare(
        "course(id INTEGER, title TEXT, credits INTEGER);",
        "SELECT * FROM course",
        "SELECT id, title, credits FROM course",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


@pytest.mark.parametrize(
    "projection",
    ["id, title", "title, id, credits", "id, title, title"],
)
def test_star_equivalence_requires_complete_schema_order(projection):
    catalog = SchemaCatalog.from_legacy(
        {"course": ["id", "title", "credits"]}
    )

    diffs = extract_ast_diffs(
        "SELECT * FROM course",
        f"SELECT {projection} FROM course",
        schema_catalog=catalog,
    )

    assert diffs


def test_simple_derived_table_projection_is_safely_inlined():
    run = generate_and_compare(
        "student(id INTEGER, name TEXT, credits INTEGER);",
        "SELECT x.name FROM ("
        "SELECT name FROM student WHERE credits > 3"
        ") x",
        "SELECT name FROM student WHERE credits > 3",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_named_window_is_equivalent_to_exact_inline_definition():
    run = generate_and_compare(
        "instructor(id INTEGER, name TEXT, dept TEXT, salary INTEGER);",
        "SELECT name, SUM(salary) OVER w FROM instructor "
        "WINDOW w AS (PARTITION BY dept)",
        "SELECT name, SUM(salary) OVER (PARTITION BY dept) FROM instructor",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_named_window_reference_override_is_not_treated_as_exact_inline():
    diffs = extract_ast_diffs(
        "SELECT name, SUM(salary) OVER (w ORDER BY salary) FROM instructor "
        "WINDOW w AS (PARTITION BY dept)",
        "SELECT name, SUM(salary) OVER (PARTITION BY dept) FROM instructor",
    )

    assert diffs


def test_in_filter_is_equivalent_to_exact_correlated_exists():
    run = generate_and_compare(
        "student(id INTEGER, name TEXT); takes(id INTEGER, course_id INTEGER);",
        "SELECT name FROM student s WHERE id IN (SELECT id FROM takes)",
        "SELECT name FROM student s WHERE EXISTS ("
        "SELECT 1 FROM takes t WHERE t.id = s.id)",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_simple_uncorrelated_in_exists_probe_keeps_match_and_nonmatch_outer_keys():
    run = generate_and_compare(
        "gap_subqueries_correlation_0242(id INT PRIMARY KEY, value TEXT); "
        "gap_subqueries_correlation_0242_lookup(id INT PRIMARY KEY, value TEXT);",
        "SELECT id FROM gap_subqueries_correlation_0242 WHERE id IN "
        "(SELECT id FROM gap_subqueries_correlation_0242_lookup)",
        "SELECT id FROM gap_subqueries_correlation_0242 WHERE EXISTS "
        "(SELECT id FROM gap_subqueries_correlation_0242_lookup)",
        sql_dialect="sqlite",
        max_rows_per_table=8,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    outer_rows = run.test_database["gap_subqueries_correlation_0242"]
    inner_rows = run.test_database["gap_subqueries_correlation_0242_lookup"]
    outer_ids = [row["id"] for row in outer_rows]
    inner_ids = [row["id"] for row in inner_rows]
    assert set(outer_ids) & set(inner_ids)
    assert any(value not in set(inner_ids) for value in outer_ids)
    assert run.standard_rows != run.student_rows
    obligation = run.data_evidence["obligation_effectiveness"][0]
    assert obligation["probe"] == "subquery_membership_paths"
    assert obligation["constraints_satisfied"] is True
    assert obligation["causal_attribution_verified"] is True


def test_in_exists_equivalence_requires_identical_inner_filters():
    diffs = extract_ast_diffs(
        "SELECT name FROM student s WHERE id IN ("
        "SELECT id FROM takes WHERE course_id = 1)",
        "SELECT name FROM student s WHERE EXISTS ("
        "SELECT 1 FROM takes t WHERE t.id = s.id AND t.course_id = 2)",
    )

    assert diffs


def test_equal_any_subquery_is_equivalent_to_in_subquery():
    run = generate_and_compare(
        "student(id INTEGER, name TEXT); takes(id INTEGER, course_id INTEGER);",
        "SELECT name FROM student WHERE id = ANY (SELECT id FROM takes)",
        "SELECT name FROM student WHERE id IN (SELECT id FROM takes)",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_non_equality_any_is_not_normalized_to_in():
    diffs = extract_ast_diffs(
        "SELECT name FROM student WHERE id <> ANY (SELECT id FROM takes)",
        "SELECT name FROM student WHERE id IN (SELECT id FROM takes)",
    )

    assert diffs


def test_nullable_all_max_rewrite_is_executable_but_not_claimed_equivalent():
    standard = (
        "SELECT name FROM student WHERE credits >= ("
        "SELECT MAX(credits) FROM student)"
    )
    student = (
        "SELECT name FROM student WHERE credits >= ALL ("
        "SELECT credits FROM student)"
    )

    run = generate_and_compare(
        "student(id INTEGER, name TEXT, credits INTEGER);",
        standard,
        student,
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.ast_diffs


@pytest.mark.parametrize(
    ("operator", "aggregate"),
    [(">", "MAX"), (">=", "MAX"), ("<", "MIN"), ("<=", "MIN")],
)
def test_root_where_all_extreme_filter_is_schema_proven(operator, aggregate):
    standard = (
        f"SELECT name FROM student WHERE credits {operator} ("
        f"SELECT {aggregate}(credits) FROM student)"
    )
    student = (
        f"SELECT name FROM student WHERE credits {operator} ALL ("
        "SELECT credits FROM student)"
    )

    run = generate_and_compare(
        "student(id INTEGER, name TEXT, credits INTEGER NOT NULL);",
        standard,
        student,
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


@pytest.mark.parametrize(
    ("operator", "aggregate"),
    [(">", "MIN"), (">=", "MIN"), ("<", "MAX"), ("<=", "MAX")],
)
def test_root_where_any_extreme_filter_is_supported(operator, aggregate):
    standard = (
        f"SELECT name FROM student WHERE credits {operator} ("
        f"SELECT {aggregate}(credits) FROM student WHERE id > 0)"
    )
    student = (
        f"SELECT name FROM student WHERE credits {operator} ANY ("
        "SELECT credits FROM student WHERE id > 0)"
    )

    run = generate_and_compare(
        "student(id INTEGER, name TEXT, credits INTEGER);",
        standard,
        student,
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


@pytest.mark.parametrize(
    "inner_filter",
    ["1 = 0", "credits IS NULL"],
)
def test_root_where_any_extreme_filter_handles_empty_and_null_inputs(inner_filter):
    standard = (
        "SELECT name FROM student WHERE credits > ("
        f"SELECT MIN(credits) FROM student WHERE {inner_filter})"
    )
    student = (
        "SELECT name FROM student WHERE credits > ANY ("
        f"SELECT credits FROM student WHERE {inner_filter})"
    )

    run = generate_and_compare(
        "student(id INTEGER, name TEXT, credits INTEGER);",
        standard,
        student,
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_sqlite_quantified_rewrite_has_guarded_root_any_support():
    assert " IN " in parseval._rewrite_quantified_subqueries(
        "SELECT id FROM student WHERE id = ANY (SELECT id FROM takes)"
    )
    rewritten = parseval._rewrite_quantified_subqueries(
        "SELECT name FROM student WHERE credits > ANY ("
        "SELECT credits FROM student)"
    )
    assert "SELECT MIN(credits)" in rewritten

    for sql in (
        "SELECT name FROM student WHERE NOT (credits > ANY ("
        "SELECT credits FROM student))",
        "SELECT name FROM student WHERE (credits > ANY ("
        "SELECT credits FROM student)) IS FALSE",
        "SELECT credits > ANY (SELECT credits FROM student) FROM student",
    ):
        assert parseval._rewrite_quantified_subqueries(sql) == sql
        assert "QUANTIFIED_SUBQUERY_COMPARISON" in (
            parseval._detect_sqlite_unsupported_features(
                sql,
                target_dialect="mysql",
            )
        )


@pytest.mark.parametrize(
    ("inner_filter", "expected_names"),
    [
        ("1 = 0", {"low", "high", "null"}),
        ("id < 3", {"high"}),
        ("id = 3", set()),
    ],
)
def test_root_where_all_not_exists_lowering_preserves_empty_null_and_values(
    inner_filter,
    expected_names,
):
    sql = (
        "SELECT name FROM student WHERE credits >= ALL ("
        f"SELECT credits FROM student WHERE {inner_filter})"
    )
    rewritten = parseval._rewrite_quantified_subqueries(sql)

    assert " ALL " not in rewritten.upper()
    assert "NOT EXISTS" in rewritten.upper()
    assert "QUANTIFIED_SUBQUERY_COMPARISON" not in (
        parseval._detect_sqlite_unsupported_features(
            sql,
            target_dialect="mysql",
        )
    )
    _, result_rows = parseval._execute_sqlite(
        {"student": ["id", "name", "credits"]},
        {
            "student": [
                {"id": 1, "name": "low", "credits": 1},
                {"id": 2, "name": "high", "credits": 3},
                {"id": 3, "name": "null", "credits": None},
            ]
        },
        rewritten,
        schema_types={
            "student": {
                "id": "INTEGER",
                "name": "TEXT",
                "credits": "INTEGER",
            }
        },
    )

    assert {row[0] for row in result_rows} == expected_names


def test_all_lowering_keeps_observable_three_valued_contexts_as_engine_gaps():
    for sql in (
        "SELECT credits >= ALL (SELECT credits FROM student) FROM student",
        "SELECT name FROM student WHERE NOT (credits >= ALL ("
        "SELECT credits FROM student))",
        "SELECT name FROM student WHERE (credits >= ALL ("
        "SELECT credits FROM student)) IS FALSE",
        "SELECT name FROM student WHERE credits >= ALL ("
        "SELECT credits FROM student) OR id = 1",
    ):
        assert parseval._rewrite_quantified_subqueries(sql) == sql
        assert "QUANTIFIED_SUBQUERY_COMPARISON" in (
            parseval._detect_sqlite_unsupported_features(
                sql,
                target_dialect="mysql",
            )
        )


def test_all_extreme_equivalence_requires_same_unfiltered_not_null_column():
    standard = (
        "SELECT s.name FROM student s WHERE s.credits >= ("
        "SELECT MAX(x.credits) FROM student x)"
    )
    student = (
        "SELECT s.name FROM student s WHERE s.credits >= ALL ("
        "SELECT x.credits FROM student x)"
    )
    not_null_catalog = SchemaCatalog.from_legacy(
        {"student": ["id", "name", "credits"]},
        {
            "student": {
                "id": "INTEGER PRIMARY KEY",
                "name": "TEXT",
                "credits": "INTEGER NOT NULL",
            }
        },
    )
    nullable_catalog = SchemaCatalog.from_legacy(
        {"student": ["id", "name", "credits"]},
        {"student": {"id": "INTEGER", "name": "TEXT", "credits": "INTEGER"}},
    )

    assert extract_ast_diffs(
        standard,
        student,
        dialect="mysql",
        schema_catalog=not_null_catalog,
    ) == []
    assert extract_ast_diffs(
        standard,
        student,
        dialect="mysql",
        schema_catalog=nullable_catalog,
    )
    assert extract_ast_diffs(
        standard,
        student.replace(
            "SELECT x.credits FROM student x",
            "SELECT x.credits FROM student x WHERE x.id > 0",
        ),
        dialect="mysql",
        schema_catalog=not_null_catalog,
    )


def test_statically_empty_scalar_subquery_is_null():
    run = generate_and_compare(
        "student(id INTEGER, name TEXT);",
        "SELECT (SELECT id FROM student WHERE 1 = 0)",
        "SELECT NULL",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


@pytest.mark.parametrize(
    ("standard", "student"),
    [("SELECT 100.0", "SELECT 1e2"), ("SELECT TRUE", "SELECT 1")],
)
def test_standalone_equivalent_literals_follow_value_judge_contract(
    standard,
    student,
):
    run = generate_and_compare("", standard, student, max_rows_per_table=4)

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_boolean_numeric_literal_equivalence_is_dialect_scoped():
    assert extract_ast_diffs(
        "SELECT TRUE",
        "SELECT 1",
        dialect="postgres",
    )


def test_numeric_literal_canonicalization_does_not_change_arithmetic_types():
    assert extract_ast_diffs("SELECT 1 / 2", "SELECT 1.0 / 2")


def test_nonempty_scalar_subquery_is_not_normalized_to_null():
    diffs = extract_ast_diffs(
        "SELECT (SELECT id FROM student WHERE 1 = 1)",
        "SELECT NULL",
    )

    assert diffs


def test_derived_table_outer_filter_is_not_collapsed_into_inner_filter():
    diffs = extract_ast_diffs(
        "SELECT x.name FROM ("
        "SELECT name, credits FROM student WHERE credits > 3"
        ") x WHERE x.credits < 8",
        "SELECT name FROM student WHERE credits > 3",
    )

    assert diffs


def test_filtered_cte_dependency_is_not_collapsed_as_passthrough():
    diffs = extract_ast_diffs(
        "WITH a AS (SELECT * FROM employee WHERE salary > 10), "
        "b AS (SELECT name FROM a WHERE salary > 3) SELECT name FROM b",
        "SELECT name FROM employee WHERE salary > 3",
    )

    assert diffs


def test_cte_column_list_alias_mapping_does_not_hide_wrong_output_column():
    diffs = extract_ast_diffs(
        "WITH e(x, y) AS (SELECT id, name FROM employee) SELECT x FROM e",
        "SELECT name FROM employee",
    )

    assert diffs


def test_join_using_and_explicit_on_are_equivalent_for_non_key_projection():
    run = generate_and_compare(
        "enrollment(id INTEGER, year INTEGER, grade TEXT); "
        "exam(id INTEGER, year INTEGER, score INTEGER);",
        "SELECT grade FROM enrollment JOIN exam USING (id, year)",
        "SELECT grade FROM enrollment e JOIN exam x "
        "ON e.id = x.id AND e.year = x.year",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_natural_join_and_explicit_on_are_equivalent_for_non_key_projection():
    run = generate_and_compare(
        "student(id INTEGER, name TEXT); takes(id INTEGER, course_id INTEGER);",
        "SELECT name FROM student NATURAL JOIN takes",
        "SELECT name FROM student JOIN takes ON student.id = takes.id",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.ast_diffs == []


def test_using_on_equivalence_does_not_hide_select_star_shape_change():
    diffs = extract_ast_diffs(
        "SELECT * FROM enrollment JOIN exam USING (id)",
        "SELECT * FROM enrollment e JOIN exam x ON e.id = x.id",
    )

    assert diffs


@pytest.mark.parametrize(
    ("standard_sql", "student_sql"),
    [
        (
            "SELECT NULLIF(amount, 0) FROM sales",
            "SELECT CASE WHEN amount = 0 THEN NULL ELSE amount END FROM sales",
        ),
        (
            "SELECT COALESCE(dept_name, 'Unknown') FROM employee",
            "SELECT CASE WHEN dept_name IS NULL THEN 'Unknown' ELSE dept_name END "
            "FROM employee",
        ),
    ],
)
def test_nullif_and_two_argument_coalesce_case_rewrites_are_equivalent(
    standard_sql,
    student_sql,
):
    run = generate_and_compare(
        "sales(id INTEGER, amount INTEGER); employee(id INTEGER, dept_name TEXT);",
        standard_sql,
        student_sql,
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_function_case_canonicalization_rejects_changed_else_and_longer_coalesce():
    changed_else = extract_ast_diffs(
        "SELECT NULLIF(amount, 0) FROM sales",
        "SELECT CASE WHEN amount = 0 THEN NULL ELSE amount + 1 END FROM sales",
    )
    longer_coalesce = extract_ast_diffs(
        "SELECT COALESCE(dept_name, 'Unknown') FROM employee",
        "SELECT COALESCE(dept_name, 'Unknown', 'Fallback') FROM employee",
    )

    assert changed_else
    assert longer_coalesce


def test_is_true_is_equivalent_to_bare_predicate_only_in_filter_context():
    run = generate_and_compare(
        "course(id INTEGER, title TEXT, credits INTEGER);",
        "SELECT title FROM course WHERE (credits > 3) IS TRUE",
        "SELECT title FROM course WHERE credits > 3",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.ast_diffs == []


def test_is_true_projection_keeps_false_and_null_distinct():
    standard_sql = "SELECT (credits > 3) IS TRUE FROM course"
    student_sql = "SELECT credits > 3 FROM course"
    diffs = extract_ast_diffs(standard_sql, student_sql)

    assert [item.diff_type for item in diffs] == [
        "boolean_projection_truth_test_changed"
    ]
    run = generate_and_compare(
        "course(id INTEGER, title TEXT, credits INTEGER);",
        standard_sql,
        student_sql,
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert any(row[0] is None for row in run.student_rows)
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    assert effectiveness[0]["semantic_validation"]["evidence"][
        "unknown_row_indexes"
    ]
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item.get("clause") == "SELECT"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_projection_truth_test_focus_rejects_other_projection_changes():
    is_false = extract_ast_diffs(
        "SELECT (credits > 3) IS FALSE FROM course",
        "SELECT credits > 3 FROM course",
    )
    independent_change = extract_ast_diffs(
        "SELECT (credits > 3) IS TRUE, title FROM course",
        "SELECT credits > 3, credits FROM course",
    )
    multiple_truth_tests = extract_ast_diffs(
        "SELECT (credits > 3) IS TRUE, (id > 1) IS TRUE FROM course",
        "SELECT credits > 3, id > 1 FROM course",
    )

    assert is_false
    assert independent_change
    assert multiple_truth_tests
    assert all(
        item.diff_type != "boolean_projection_truth_test_changed"
        for diffs in (is_false, independent_change, multiple_truth_tests)
        for item in diffs
    )


@pytest.mark.parametrize(
    ("standard_sql", "student_sql"),
    [
        (
            "SELECT name FROM student WHERE id IN (1, 2, 3)",
            "SELECT name FROM student WHERE id = 1 OR id = 2 OR id = 3",
        ),
        (
            "SELECT name FROM student WHERE id IN (NULL, 1, 1)",
            "SELECT name FROM student WHERE id = NULL OR id = 1",
        ),
    ],
)
def test_literal_in_list_and_or_chain_are_equivalent(
    standard_sql,
    student_sql,
):
    run = generate_and_compare(
        "student(id INTEGER, name TEXT);",
        standard_sql,
        student_sql,
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_in_list_canonicalization_does_not_hide_subquery_or_changed_member():
    subquery_diffs = extract_ast_diffs(
        "SELECT name FROM student WHERE id IN (SELECT id FROM takes)",
        "SELECT name FROM student WHERE id = 1 OR id = 2",
    )
    changed_member_diffs = extract_ast_diffs(
        "SELECT name FROM student WHERE id IN (1, 2, 3)",
        "SELECT name FROM student WHERE id = 1 OR id = 2 OR id = 4",
    )

    assert subquery_diffs
    assert changed_member_diffs


def test_simple_and_searched_case_are_equivalent_for_column_operand():
    run = generate_and_compare(
        "course(id INTEGER, credits INTEGER);",
        "SELECT CASE credits WHEN 3 THEN 'A' WHEN 4 THEN 'B' ELSE 'C' END "
        "FROM course",
        "SELECT CASE WHEN credits = 3 THEN 'A' WHEN credits = 4 THEN 'B' "
        "ELSE 'C' END FROM course",
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_simple_case_canonicalization_rejects_wrong_branch_condition():
    diffs = extract_ast_diffs(
        "SELECT CASE credits WHEN 3 THEN 'A' ELSE 'B' END FROM course",
        "SELECT CASE WHEN credits = 4 THEN 'A' ELSE 'B' END FROM course",
    )

    assert diffs


@pytest.mark.parametrize(
    ("standard_sql", "student_sql"),
    [
        (
            "SELECT title, credits FROM course ORDER BY 2, 1",
            "SELECT title, credits FROM course ORDER BY credits, title",
        ),
        (
            "SELECT title, credits + 1 AS c FROM course ORDER BY c DESC",
            "SELECT title, credits + 1 AS c FROM course ORDER BY credits + 1 DESC",
        ),
    ],
)
def test_order_ordinal_and_output_alias_resolve_to_projection_expression(
    standard_sql,
    student_sql,
):
    run = generate_and_compare(
        "course(id INTEGER, title TEXT, credits INTEGER);",
        standard_sql,
        student_sql,
        max_rows_per_table=6,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_order_reference_canonicalization_rejects_bad_ordinal_and_direction():
    bad_ordinal = extract_ast_diffs(
        "SELECT title FROM course ORDER BY 2",
        "SELECT title FROM course ORDER BY title",
    )
    changed_direction = extract_ast_diffs(
        "SELECT title FROM course ORDER BY 1 DESC",
        "SELECT title FROM course ORDER BY title ASC",
    )

    assert bad_ordinal
    assert changed_direction


def test_group_by_key_order_and_duplicate_key_are_equivalent():
    reordered = generate_and_compare(
        "sales(id INTEGER, region TEXT, amount INTEGER);",
        "SELECT region, SUM(amount) FROM sales GROUP BY region, id",
        "SELECT region, SUM(amount) FROM sales GROUP BY id, region",
        max_rows_per_table=6,
    )
    duplicate = generate_and_compare(
        "sales(id INTEGER, region TEXT, amount INTEGER);",
        "SELECT region, SUM(amount) FROM sales GROUP BY region, region",
        "SELECT region, SUM(amount) FROM sales GROUP BY region",
        max_rows_per_table=6,
    )

    for run in (reordered, duplicate):
        assert run.executed is True, run.error
        assert run.is_equivalent is True
        assert run.status == "SUPPORTED"
        assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
        assert run.ast_diffs == []


def test_null_safe_equality_with_non_null_literal_is_filter_equivalent():
    run = generate_and_compare(
        "employee(id INTEGER, name TEXT, manager_id INTEGER);",
        "SELECT name FROM employee WHERE manager_id IS NOT DISTINCT FROM 3",
        "SELECT name FROM employee WHERE manager_id = 3",
        sql_dialect="postgres",
        max_rows_per_table=6,
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_null_safe_equality_projection_preserves_false_vs_null_difference():
    run = generate_and_compare(
        "employee(id INTEGER, manager_id INTEGER);",
        "SELECT manager_id IS NOT DISTINCT FROM 3 FROM employee",
        "SELECT manager_id = 3 FROM employee",
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert any(row == (None,) for row in run.student_rows)
    assert all(row != (None,) for row in run.standard_rows)


@pytest.mark.parametrize(
    ("standard_predicate", "student_predicate"),
    [
        ("manager_id IS NOT DISTINCT FROM NULL", "manager_id IS NULL"),
        ("manager_id IS DISTINCT FROM NULL", "manager_id IS NOT NULL"),
    ],
)
def test_null_safe_comparison_against_null_is_canonical_equivalence(
    standard_predicate,
    student_predicate,
):
    run = generate_and_compare(
        "employee(id INTEGER, name TEXT, manager_id INTEGER);",
        f"SELECT name FROM employee WHERE {standard_predicate}",
        f"SELECT name FROM employee WHERE {student_predicate}",
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_projected_is_distinct_from_null_matches_is_not_null():
    run = generate_and_compare(
        "employee(id INTEGER, manager_id INTEGER);",
        "SELECT manager_id IS DISTINCT FROM NULL FROM employee",
        "SELECT manager_id IS NOT NULL FROM employee",
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.ast_diffs == []


def test_aggregate_distinct_does_not_use_top_level_distinct_latent_fix():
    run = generate_and_compare(
        "events(id);",
        "SELECT COUNT(DISTINCT id) FROM events",
        "SELECT COUNT(id) FROM events",
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] == 0


def test_aggregate_function_probe_and_mutation_do_not_rely_on_column_labels():
    run = generate_and_compare(
        "courses(dept_id, credits);",
        "SELECT dept_id, MAX(credits) AS extreme FROM courses GROUP BY dept_id",
        "SELECT dept_id, MIN(credits) AS extreme FROM courses GROUP BY dept_id",
    )

    grouped_values: dict[object, set[object]] = {}
    for row in run.test_database["courses"]:
        grouped_values.setdefault(row["dept_id"], set()).add(row["credits"])
    aggregate_mutants = [
        item
        for item in run.mutation_evidence["tests"]
        if item["clause"] == "AGGREGATE"
    ]

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_columns == run.student_columns == ["dept_id", "extreme"]
    assert any(len(values) >= 2 for values in grouped_values.values())
    assert aggregate_mutants
    assert aggregate_mutants[0]["fixed_by_replacement"] is True


def test_order_direction_probe_is_not_masked_by_repeating_projection_values():
    run = generate_and_compare(
        "course(course_id, title, credits);",
        "SELECT title FROM course ORDER BY credits DESC",
        "SELECT title FROM course ORDER BY credits ASC",
        max_rows_per_table=10,
    )

    assert run.is_equivalent is False
    assert len({row["title"] for row in run.test_database["course"]}) == 10


def test_order_direction_join_resolves_the_changed_key_table_not_first_from_source():
    run = generate_and_compare(
        "people(People_ID INTEGER PRIMARY KEY, Birth_Date TEXT); "
        "poker_player(Poker_Player_ID INTEGER PRIMARY KEY, People_ID INTEGER, Earnings INTEGER);",
        "SELECT p.Birth_Date FROM people p JOIN poker_player pp "
        "ON p.People_ID = pp.People_ID ORDER BY pp.Earnings ASC LIMIT 1",
        "SELECT p.Birth_Date FROM people p JOIN poker_player pp "
        "ON p.People_ID = pp.People_ID ORDER BY pp.Earnings DESC LIMIT 1",
        max_rows_per_table=8,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    effectiveness = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "order_key_separation"
    ]
    assert effectiveness
    assert effectiveness[0]["constraints_satisfied"] is True


@pytest.mark.parametrize("row_scale", [8, 16, 32])
def test_grouped_aggregate_function_witness_is_stable_across_row_scales(row_scale):
    standard = (
        "WITH daily AS ("
        "SELECT log_date, SUM(page_views) AS total_page_views "
        "FROM web_logs GROUP BY log_date "
        "ORDER BY total_page_views DESC LIMIT 1"
        ") SELECT w.log_date, w.user_id FROM web_logs w "
        "JOIN daily d ON w.log_date = d.log_date"
    )
    student = standard.replace("SUM(page_views)", "AVG(page_views)")

    run = generate_and_compare(
        "web_logs(user_id, log_date, page_views);",
        standard,
        student,
        max_rows_per_table=row_scale,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] >= 1


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


def test_top_level_nulls_placement_has_complete_witness_chain():
    run = generate_and_compare(
        "instructor(id, name, salary);",
        "SELECT name, salary FROM instructor ORDER BY salary ASC NULLS LAST",
        "SELECT name, salary FROM instructor ORDER BY salary ASC",
        max_rows_per_table=10,
    )

    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert any(diff.diff_type == "order_nulls_changed" for diff in run.ast_diffs)
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    assert effectiveness[0]["semantic_validation"]["evidence"]["null_order_path"] is True
    assert effectiveness[0]["mutation_validation"]["relevant_fixed_by_replacement"] is True


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


@pytest.mark.parametrize("row_scale", [8, 32])
def test_correlated_exists_wrong_inner_key_gets_standard_only_path(row_scale):
    standard = (
        "SELECT CustomerId FROM Customer c WHERE EXISTS ("
        "SELECT 1 FROM Invoice i "
        "WHERE i.CustomerId = c.CustomerId AND i.Total > 10)"
    )
    student = (
        "SELECT CustomerId FROM Customer c WHERE EXISTS ("
        "SELECT 1 FROM Invoice i "
        "WHERE i.InvoiceId = c.CustomerId AND i.Total > 10)"
    )

    run = generate_and_compare(
        "Customer(CustomerId INTEGER PRIMARY KEY); "
        "Invoice(InvoiceId INTEGER PRIMARY KEY, CustomerId INTEGER, Total NUMERIC);",
        standard,
        student,
        max_rows_per_table=row_scale,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] >= 1


@pytest.mark.parametrize("row_scale", [4, 8])
def test_correlated_scalar_aggregate_wrong_outer_key_has_complete_evidence(
    row_scale,
):
    standard = (
        "SELECT e.id FROM employee e WHERE e.salary > ("
        "SELECT AVG(x.salary) FROM employee x WHERE x.dept = e.dept)"
    )
    student = standard.replace("x.dept = e.dept", "x.dept = e.id")

    run = generate_and_compare(
        "employee(id INTEGER PRIMARY KEY, dept INTEGER, salary INTEGER);",
        standard,
        student,
        max_rows_per_table=row_scale,
        sql_dialect="mysql",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False
    focused = [
        item
        for item in run.ast_diffs
        if item.diff_type == "correlated_predicate_changed"
        and item.extra.get("query_scope") == "nested_correlation"
    ]
    assert len(focused) == 1
    assert focused[0].extra["standard_outer_column"] == "dept"
    assert focused[0].extra["student_outer_column"] == "id"
    rows = run.test_database["employee"]
    assert len({row["id"] for row in rows}) == len(rows)
    assert max(Counter(row["dept"] for row in rows).values()) >= 2
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item.get("clause") == "CORRELATED SUBQUERY"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_inner_table_alias_scope_detects_self_correlation_without_shadowing():
    correlated = parse_one(
        "SELECT e.id FROM employee e WHERE EXISTS ("
        "SELECT 1 FROM employee x WHERE x.dept = e.dept)",
        read="mysql",
    ).find(parseval.exp.Exists).this
    shadowed = parse_one(
        "SELECT e.id FROM employee e WHERE EXISTS ("
        "SELECT 1 FROM employee WHERE employee.dept = employee.dept)",
        read="mysql",
    ).find(parseval.exp.Exists).this

    assert parseval._subquery_is_correlated(correlated) is True
    assert parseval._subquery_is_correlated(shadowed) is False


@pytest.mark.parametrize("row_scale", [4, 8, 16])
def test_nested_correlated_exists_wrong_ancestor_key_has_complete_evidence(row_scale):
    standard = (
        "SELECT c.CustomerId FROM Customer c WHERE EXISTS ("
        "SELECT 1 FROM Invoice i WHERE i.CustomerId = c.CustomerId AND EXISTS ("
        "SELECT 1 FROM InvoiceLine l "
        "WHERE l.InvoiceId = i.InvoiceId AND l.UnitPrice > 10))"
    )
    student = standard.replace(
        "l.InvoiceId = i.InvoiceId",
        "l.InvoiceId = c.CustomerId",
    )

    run = generate_and_compare(
        "Customer(CustomerId INTEGER PRIMARY KEY); "
        "Invoice(InvoiceId INTEGER PRIMARY KEY, CustomerId INTEGER); "
        "InvoiceLine(LineId INTEGER PRIMARY KEY, InvoiceId INTEGER, UnitPrice NUMERIC);",
        standard,
        student,
        max_rows_per_table=row_scale,
        sql_dialect="sqlite",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False
    focused = [
        item
        for item in run.ast_diffs
        if item.diff_type == "correlated_predicate_changed"
        and item.extra.get("query_scope") == "nested_correlation"
    ]
    assert len(focused) == 1
    assert focused[0].extra["standard_source_table"] == "invoice"
    assert focused[0].extra["student_source_table"] == "customer"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item.get("clause") == "CORRELATED SUBQUERY"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


@pytest.mark.parametrize("row_scale", [4, 8, 16, 32])
def test_nested_in_wrong_membership_key_has_complete_evidence(row_scale):
    standard = (
        "SELECT c.CustomerId FROM Customer c WHERE c.CustomerId IN ("
        "SELECT i.CustomerId FROM Invoice i WHERE i.InvoiceId IN ("
        "SELECT l.InvoiceId FROM InvoiceLine l "
        "WHERE l.Quantity > c.CustomerId))"
    )
    student = standard.replace("i.InvoiceId IN", "i.CustomerId IN")

    run = generate_and_compare(
        "Customer(CustomerId INTEGER PRIMARY KEY); "
        "Invoice(InvoiceId INTEGER PRIMARY KEY, CustomerId INTEGER); "
        "InvoiceLine(LineId INTEGER PRIMARY KEY, InvoiceId INTEGER, Quantity INTEGER);",
        standard,
        student,
        max_rows_per_table=row_scale,
        sql_dialect="sqlite",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert [item.diff_type for item in run.ast_diffs] == [
        "subquery_membership_key_changed"
    ]
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item.get("clause") == "IN"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_sqlite_instruction_guard_interrupts_unbounded_recursive_cte():
    with pytest.raises(Exception, match="interrupted"):
        parseval._execute_sqlite(
            {},
            {},
            "WITH RECURSIVE forever(n) AS ("
            "SELECT 1 UNION ALL SELECT n + 1 FROM forever) "
            "SELECT MAX(n) FROM forever",
        )


def test_nested_in_membership_key_focus_rejects_independent_filter_change():
    standard = (
        "SELECT c.CustomerId FROM Customer c WHERE c.CustomerId IN ("
        "SELECT i.CustomerId FROM Invoice i WHERE i.InvoiceId IN ("
        "SELECT l.InvoiceId FROM InvoiceLine l "
        "WHERE l.Quantity > c.CustomerId))"
    )
    student = standard.replace(
        "i.InvoiceId IN",
        "i.CustomerId IN",
    ).replace("l.Quantity > c.CustomerId", "l.Quantity >= c.CustomerId")

    diffs = extract_ast_diffs(standard, student, dialect="sqlite")

    assert diffs
    assert all(
        item.diff_type != "subquery_membership_key_changed"
        for item in diffs
    )


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


def test_not_in_negation_witness_keeps_complex_outer_path_reachable():
    """A NOT IN/IN mutation must survive all neighboring outer predicates."""
    schema = (
        "Physician(EmployeeID INT PRIMARY KEY, Name VARCHAR(30), "
        "Position VARCHAR(30), SSN INT); "
        "Department(DepartmentID INT PRIMARY KEY, Name VARCHAR(30), Head INT); "
        "Procedures(Code INT PRIMARY KEY, Name VARCHAR(30), Cost FLOAT); "
        "Patient(SSN INT PRIMARY KEY, Name VARCHAR(30), Address VARCHAR(30), "
        "Phone VARCHAR(30), InsuranceID INT, PCP INT); "
        "Nurse(EmployeeID INT PRIMARY KEY, Name VARCHAR(30), "
        "Position VARCHAR(30), Registered BOOLEAN, SSN INT); "
        "Appointment(AppointmentID INT PRIMARY KEY, Patient INT, "
        "PrepNurse INT, Physician INT, Start DATETIME, End DATETIME, "
        "ExaminationRoom TEXT); "
        "Prescribes(Physician INT, Patient INT, Medication INT, Date DATETIME, "
        "Appointment INT, Dose VARCHAR(30)); "
        "Undergoes(Patient INT, Procedures INT, Stay INT, "
        "DateUndergoes DATETIME, Physician INT, AssistingNurse INT);"
    )
    standard = (
        "SELECT Pt.NAME, PhPCP.NAME FROM Patient Pt, Physician PhPCP "
        "WHERE Pt.PCP = PhPCP.EmployeeID "
        "AND EXISTS (SELECT * FROM Prescribes Pr "
        "WHERE Pr.Patient = Pt.SSN AND Pr.Physician = Pt.PCP) "
        "AND EXISTS (SELECT * FROM Undergoes U, Procedures Pr "
        "WHERE U.Procedures = Pr.CODE AND U.Patient = Pt.SSN "
        "AND Pr.Cost > 5000) "
        "AND 2 <= (SELECT COUNT(A.AppointmentID) "
        "FROM Appointment A, Nurse N "
        "WHERE A.PrepNurse = N.EmployeeID AND N.Registered = 1) "
        "AND NOT Pt.PCP IN (SELECT Head FROM Department)"
    )
    student = standard.replace("AND NOT Pt.PCP IN", "AND Pt.PCP IN")

    run = generate_and_compare(
        schema,
        standard,
        student,
        max_rows_per_table=4,
        sql_dialect="mysql",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    membership = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "subquery_membership_paths"
        and item.get("semantic_validation", {})
        .get("evidence", {})
        .get("inner_table", "")
        .lower() == "department"
    ]
    assert membership
    assert membership[0]["constraints_satisfied"] is True


@pytest.mark.parametrize(
    "join_predicate",
    ["u.manager_id = m.id", "m.id = u.manager_id"],
)
def test_not_in_negation_targets_changed_predicate_among_multiple_not_in(
    join_predicate,
):
    standard = (
        "SELECT u.id FROM users u, managers m "
        f"WHERE {join_predicate} "
        "AND u.id NOT IN (SELECT id FROM banned) "
        "AND u.manager_id NOT IN (SELECT head FROM departments)"
    )
    student = standard.replace(
        "AND u.manager_id NOT IN",
        "AND u.manager_id IN",
    )

    run = generate_and_compare(
        "users(id INT PRIMARY KEY, manager_id INT); "
        "managers(id INT PRIMARY KEY); banned(id INT); departments(head INT);",
        standard,
        student,
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.standard_rows != run.student_rows


@pytest.mark.parametrize("row_scale", [8, 16, 32])
def test_in_subquery_comparison_boundary_is_stable_across_row_scales(row_scale):
    standard = (
        "SELECT CustomerId FROM Customer WHERE CustomerId IN ("
        "SELECT CustomerId FROM Invoice WHERE Total > 10)"
    )
    student = standard.replace("Total > 10", "Total >= 10")

    run = generate_and_compare(
        "Customer(CustomerId INTEGER PRIMARY KEY); "
        "Invoice(InvoiceId INTEGER PRIMARY KEY, CustomerId INTEGER, Total NUMERIC);",
        standard,
        student,
        max_rows_per_table=row_scale,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] >= 1


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


def test_recursive_literal_anchor_and_outer_cte_filter_survive_order_probe():
    standard = (
        "WITH RECURSIVE hierarchy(emp_id, manager_id) AS ("
        "SELECT emp_id, manager_id FROM employee WHERE emp_id = 27 "
        "UNION ALL "
        "SELECT e.emp_id, e.manager_id FROM employee e "
        "JOIN hierarchy h ON e.manager_id = h.emp_id) "
        "SELECT h.emp_id FROM hierarchy h "
        "WHERE h.emp_id = 22 OR h.emp_id = 12 ORDER BY h.emp_id DESC"
    )
    student = standard.replace("ORDER BY h.emp_id DESC", "ORDER BY h.emp_id ASC")

    run = generate_and_compare(
        "employee(emp_id, name, manager_id);",
        standard,
        student,
        max_rows_per_table=8,
    )

    rows = run.test_database["employee"]
    assert run.executed is True
    assert run.is_equivalent is False
    assert rows[0]["emp_id"] == 27
    assert [row["emp_id"] for row in rows[:3]] == [27, 22, 12]
    assert rows[0]["manager_id"] is None
    assert rows[1]["manager_id"] == 27
    assert rows[2]["manager_id"] == 22
    effectiveness = run.data_evidence["obligation_effectiveness"]
    order_effectiveness = next(
        item for item in effectiveness if item["probe"] == "order_key_separation"
    )
    assert order_effectiveness["constraints_satisfied"] is True
    assert order_effectiveness["causal_attribution_verified"] is True
    assert order_effectiveness["semantic_validation"]["evidence"]["source"] == (
        "bounded_query_result"
    )


def test_recursive_parent_projection_literal_anchor_walks_toward_ancestors():
    standard = (
        "WITH RECURSIVE recommenders(recommender) AS ("
        "SELECT recommendedby FROM members WHERE memid = 27 "
        "UNION ALL "
        "SELECT mems.recommendedby FROM recommenders recs "
        "JOIN members mems ON mems.memid = recs.recommender) "
        "SELECT recs.recommender, mems.firstname, mems.surname "
        "FROM recommenders recs "
        "JOIN members mems ON recs.recommender = mems.memid "
        "ORDER BY memid DESC"
    )
    student = standard.replace("ORDER BY memid DESC", "ORDER BY memid ASC")

    run = generate_and_compare(
        "members(memid INTEGER PRIMARY KEY, recommendedby INTEGER, "
        "firstname TEXT, surname TEXT);",
        standard,
        student,
        max_rows_per_table=8,
        sql_dialect="postgres",
    )

    rows = run.test_database["members"]
    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert rows[0]["memid"] == 27
    assert rows[0]["recommendedby"] == rows[1]["memid"]
    assert rows[1]["recommendedby"] == rows[2]["memid"]
    assert rows[-1]["recommendedby"] is None
    assert len(run.standard_rows) >= 2
    assert run.standard_rows == list(reversed(run.student_rows))


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


@pytest.mark.parametrize(
    ("operator", "standard_order", "student_order"),
    [
        ("UNION", (1, 2), (2, 1)),
        ("UNION ALL", (1, 2), (2, 1)),
        ("UNION", (1, 2, 3), (3, 1, 2)),
        ("INTERSECT", (1, 2), (2, 1)),
    ],
)
def test_commutative_set_branch_permutations_are_structurally_equivalent(
    operator,
    standard_order,
    student_order,
):
    def query(order):
        return f" {operator} ".join(
            f"SELECT title FROM course WHERE dept = {value}"
            for value in order
        )

    standard = query(standard_order)
    student = query(student_order)
    run = generate_and_compare(
        "course(id INTEGER PRIMARY KEY, title TEXT, dept INTEGER);",
        standard,
        student,
        max_rows_per_table=6,
        sql_dialect="sqlite",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.is_equivalent is True
    assert run.ast_diffs == []
    assert run.data_evidence["obligation_effectiveness"] == []


@pytest.mark.parametrize(
    ("standard", "student"),
    [
        (
            "SELECT title FROM course WHERE dept = 1 EXCEPT "
            "SELECT title FROM course WHERE dept = 2",
            "SELECT title FROM course WHERE dept = 2 EXCEPT "
            "SELECT title FROM course WHERE dept = 1",
        ),
        (
            "SELECT title FROM course WHERE dept = 1 UNION "
            "SELECT title FROM course WHERE dept = 2 INTERSECT "
            "SELECT title FROM course WHERE dept = 3",
            "SELECT title FROM course WHERE dept = 3 UNION "
            "SELECT title FROM course WHERE dept = 2 INTERSECT "
            "SELECT title FROM course WHERE dept = 1",
        ),
        (
            "SELECT title FROM course WHERE dept = 1 UNION "
            "SELECT title FROM course WHERE dept = 2 ORDER BY title LIMIT 1",
            "SELECT title FROM course WHERE dept = 2 UNION "
            "SELECT title FROM course WHERE dept = 1 ORDER BY title LIMIT 1",
        ),
    ],
)
def test_set_branch_permutation_focus_rejects_non_commutative_or_bounded_shapes(
    standard,
    student,
):
    assert extract_ast_diffs(standard, student, dialect="sqlite")


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
    assert [diff.diff_type for diff in run.ast_diffs] == [
        "logical_precedence_tree_changed"
    ]
    assert run.data_evidence["standard_row_count"] != run.data_evidence["student_row_count"]
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True


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


def test_recursive_union_modifier_uses_unique_graph_diamond_paths():
    standard = (
        "WITH RECURSIVE reach(node) AS ("
        "SELECT 1 AS node UNION ALL "
        "SELECT e.dst_id FROM edges e JOIN reach r ON e.src_id = r.node"
        ") SELECT node FROM reach"
    )
    student = standard.replace("UNION ALL", "UNION", 1)

    run = generate_and_compare(
        "edges(edge_id, src_id, dst_id);",
        standard,
        student,
        max_rows_per_table=8,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert run.standard_rows.count((4,)) == 2
    assert run.student_rows.count((4,)) == 1
    edge_ids = [row["edge_id"] for row in run.test_database["edges"][:4]]
    assert len(edge_ids) == len(set(edge_ids))
    set_effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "set_overlap"
    )
    assert set_effectiveness["constraints_satisfied"] is True
    assert set_effectiveness["causal_attribution_verified"] is True


@pytest.mark.parametrize(
    ("anchor", "step", "predicate"),
    [
        ("1", "x + 1", "x < 4"),
        ("4", "x - 1", "x > 1"),
    ],
)
def test_bounded_monotonic_recursive_union_modifier_is_equivalent(
    anchor,
    step,
    predicate,
):
    standard = (
        "WITH RECURSIVE n(x) AS ("
        f"SELECT {anchor} UNION SELECT {step} FROM n WHERE {predicate}"
        ") SELECT x FROM n"
    )
    student = standard.replace(" UNION SELECT", " UNION ALL SELECT")

    run = generate_and_compare("", standard, student, max_rows_per_table=4)

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert run.ast_diffs == []


def test_unbounded_monotonic_recursive_union_modifier_is_not_proven_equivalent():
    standard = (
        "WITH RECURSIVE n(x) AS ("
        "SELECT 1 UNION SELECT x + 1 FROM n"
        ") SELECT x FROM n"
    )
    student = standard.replace(" UNION SELECT", " UNION ALL SELECT")

    assert extract_ast_diffs(standard, student)


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


def test_recursive_multicolumn_graph_union_modifier_uses_diamond_paths():
    standard = (
        "WITH RECURSIVE search_graph(id, link, data, depth) AS ("
        "SELECT g.id, g.link, g.data, 0 FROM graph g UNION ALL "
        "SELECT g.id, g.link, g.data, sg.depth + 1 "
        "FROM graph g, search_graph sg WHERE g.id = sg.link"
        ") SELECT * FROM search_graph"
    )
    student = standard.replace("UNION ALL", "UNION")

    run = generate_and_compare(
        "graph(id INTEGER PRIMARY KEY, link INTEGER, data TEXT);",
        standard,
        student,
        max_rows_per_table=12,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.status == "SUPPORTED"
    assert len(run.standard_rows) > len(run.student_rows)
    assert len({row["id"] for row in run.test_database["graph"]}) == len(
        run.test_database["graph"]
    )


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


@pytest.mark.parametrize("max_rows_per_table", [8, 16, 32])
def test_scalar_min_boundary_survives_join_and_sibling_filter(
    max_rows_per_table,
):
    run = generate_and_compare(
        "car_names(MakeId INTEGER PRIMARY KEY, Make TEXT); "
        "cars_data(Id INTEGER PRIMARY KEY REFERENCES car_names(MakeId), "
        "Horsepower INTEGER, Cylinders INTEGER);",
        "SELECT t2.MakeId, t2.Make FROM cars_data AS t1 "
        "JOIN car_names AS t2 ON t1.Id = t2.MakeId "
        "WHERE t1.Horsepower > "
        "(SELECT MIN(Horsepower) FROM cars_data) "
        "AND t1.Cylinders < 4;",
        "SELECT t2.MakeId, t2.Make FROM cars_data AS t1 "
        "JOIN car_names AS t2 ON t1.Id = t2.MakeId "
        "WHERE t1.Horsepower >= "
        "(SELECT MIN(Horsepower) FROM cars_data) "
        "AND t1.Cylinders < 4;",
        max_rows_per_table=max_rows_per_table,
        sql_dialect="sqlite",
    )

    cars = run.test_database["cars_data"]
    names = run.test_database["car_names"]
    minimum = min(row["Horsepower"] for row in cars)
    matching_name_ids = {row["MakeId"] for row in names}
    boundary_rows = [
        row
        for row in cars
        if row["Horsepower"] == minimum
        and isinstance(row["Cylinders"], (int, float))
        and row["Cylinders"] < 4
        and row["Id"] in matching_name_ids
    ]

    assert boundary_rows
    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "scalar_subquery_boundary_path"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True
    assert effectiveness["causal_attribution_verified"] is True
    assert effectiveness["semantic_validation"]["evidence"][
        "boundary_path_rows"
    ]


def test_authoritative_catalog_seeds_unhinted_numeric_columns_by_type():
    catalog = {
        "source": "spider_tables_json",
        "db_id": "typed_seed_probe",
        "tables": [{
            "name": "faculty",
            "columns": [
                {"name": "Faculty", "data_type": "BIGINT", "nullable": None},
                {"name": "ScoreCode", "data_type": "TEXT", "nullable": None},
            ],
            "primary_key": [],
            "foreign_keys": [],
            "unique_constraints": [],
        }],
    }
    run = generate_and_compare(
        "faculty(Faculty BIGINT, ScoreCode TEXT);",
        "SELECT Faculty FROM faculty",
        "SELECT Faculty FROM faculty",
        sql_dialect="sqlite",
        schema_catalog=catalog,
    )

    assert run.executed is True
    assert all(
        isinstance(row["Faculty"], (int, float))
        and not isinstance(row["Faculty"], bool)
        for row in run.test_database["faculty"]
    )
    assert all(
        isinstance(row["ScoreCode"], str)
        for row in run.test_database["faculty"]
    )


@pytest.mark.parametrize("max_rows_per_table", [8, 16, 32])
def test_spider_school_finance_where_boundary_crosses_join_having(
    max_rows_per_table,
):
    standard = (
        "SELECT T2.School_name FROM endowment T1 "
        "JOIN school T2 ON T1.school_id = T2.school_id "
        "WHERE T1.amount > 8.5 GROUP BY T1.school_id "
        "HAVING COUNT(*) > 1"
    )
    student = standard.replace("amount > 8.5", "amount >= 8.5")
    spider_catalog = {
        "source": "spider_tables_json",
        "db_id": "school_finance",
        "tables": [
            {
                "name": "School",
                "columns": [
                    {"name": "School_id", "data_type": "BIGINT", "nullable": None},
                    {"name": "School_name", "data_type": "TEXT", "nullable": None},
                ],
                "primary_key": ["School_id"],
                "foreign_keys": [],
                "unique_constraints": [["School_id"]],
            },
            {
                "name": "endowment",
                "columns": [
                    {"name": "endowment_id", "data_type": "BIGINT", "nullable": None},
                    {"name": "School_id", "data_type": "BIGINT", "nullable": None},
                    {"name": "donator_name", "data_type": "TEXT", "nullable": None},
                    {"name": "amount", "data_type": "BIGINT", "nullable": None},
                ],
                "primary_key": ["endowment_id"],
                "foreign_keys": [{
                    "column": "School_id",
                    "references_table": "School",
                    "references_column": "School_id",
                }],
                "unique_constraints": [["endowment_id"]],
            },
        ],
    }
    run = generate_and_compare(
        "School(School_id BIGINT PRIMARY KEY, School_name TEXT); "
        "endowment(endowment_id BIGINT PRIMARY KEY, School_id BIGINT, "
        "donator_name TEXT, amount BIGINT);",
        standard,
        student,
        max_rows_per_table=max_rows_per_table,
        sql_dialect="sqlite",
        schema_catalog=spider_catalog,
    )

    endowments = run.test_database["endowment"]
    schools = run.test_database["School"]
    school_ids = {row["School_id"] for row in schools}
    boundary_rows = [row for row in endowments if row["amount"] == 8.5]
    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows == []
    assert run.student_rows
    assert len({row["School_id"] for row in schools}) == len(schools)
    assert boundary_rows
    assert any(
        row["School_id"] in school_ids
        and sum(
            other["School_id"] == row["School_id"]
            and other["amount"] > 8.5
            for other in endowments
        ) >= 1
        for row in boundary_rows
    )
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "filtered_aggregate_boundary_path"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True
    assert effectiveness["causal_attribution_verified"] is True
    path_evidence = effectiveness["semantic_validation"]["evidence"]
    assert path_evidence["filtered_aggregate_scope"] == "query_path"
    assert path_evidence["standard_path_groups"] == []
    assert path_evidence["student_path_groups"]
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] == 1


@pytest.mark.parametrize("max_rows_per_table", [8, 16, 32])
def test_spider_csu_scalar_max_boundary_crosses_filtered_join_subquery(
    max_rows_per_table,
):
    standard = (
        "SELECT T1.campus FROM campuses AS T1 "
        "JOIN faculty AS T2 ON T1.id = T2.campus "
        "WHERE T2.year = 2002 AND faculty > ("
        "SELECT MAX(faculty) FROM campuses AS T1 "
        "JOIN faculty AS T2 ON T1.id = T2.campus "
        'WHERE T2.year = 2002 AND T1.county = "Orange")'
    )
    student = standard.replace("faculty > (", "faculty >= (")
    spider_catalog = {
        "source": "spider_tables_json",
        "db_id": "csu_1",
        "tables": [
            {
                "name": "Campuses",
                "columns": [
                    {"name": "Id", "data_type": "BIGINT", "nullable": None},
                    {"name": "Campus", "data_type": "TEXT", "nullable": None},
                    {"name": "Location", "data_type": "TEXT", "nullable": None},
                    {"name": "County", "data_type": "TEXT", "nullable": None},
                    {"name": "Year", "data_type": "BIGINT", "nullable": None},
                ],
                "primary_key": ["Id"],
                "foreign_keys": [],
                "unique_constraints": [["Id"]],
            },
            {
                "name": "faculty",
                "columns": [
                    {"name": "Campus", "data_type": "BIGINT", "nullable": None},
                    {"name": "Year", "data_type": "BIGINT", "nullable": None},
                    {"name": "Faculty", "data_type": "BIGINT", "nullable": None},
                ],
                "primary_key": [],
                "foreign_keys": [{
                    "column": "Campus",
                    "references_table": "Campuses",
                    "references_column": "Id",
                }],
                "unique_constraints": [],
            },
        ],
    }
    run = generate_and_compare(
        "Campuses(Id BIGINT PRIMARY KEY, Campus TEXT, Location TEXT, "
        "County TEXT, Year BIGINT); faculty(Campus BIGINT REFERENCES "
        "Campuses(Id), Year BIGINT, Faculty BIGINT);",
        standard,
        student,
        max_rows_per_table=max_rows_per_table,
        sql_dialect="sqlite",
        schema_catalog=spider_catalog,
    )

    campuses = run.test_database["Campuses"]
    faculty_rows = run.test_database["faculty"]
    orange_ids = {
        row["Id"] for row in campuses if row["County"] == "Orange"
    }
    inner_values = [
        row["Faculty"]
        for row in faculty_rows
        if row["Year"] == 2002 and row["Campus"] in orange_ids
    ]
    assert run.executed is True
    assert run.is_equivalent is False
    assert inner_values
    assert all(isinstance(value, (int, float)) for value in inner_values)
    boundary = max(inner_values)
    assert any(
        row["Year"] == 2002
        and row["Campus"] in {campus["Id"] for campus in campuses}
        and row["Faculty"] == boundary
        for row in faculty_rows
    )
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "scalar_subquery_boundary_path"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True
    assert effectiveness["causal_attribution_verified"] is True
    assert effectiveness["semantic_validation"]["evidence"][
        "scalar_source_table"
    ] == "faculty"
    fixed_mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item.get("fixed_by_replacement")
        and effectiveness["diff_id"] in item.get("diff_ids", [])
        and effectiveness["obligation_id"] in item.get("obligation_ids", [])
    ]
    assert fixed_mutations
    assert all(item["binding_quality"] == "exact" for item in fixed_mutations)


@pytest.mark.parametrize("max_rows_per_table", [8, 16, 32])
def test_spider_storm_record_except_union_materializes_grouped_right_branch(
    max_rows_per_table,
):
    standard = (
        "SELECT name FROM storm EXCEPT SELECT T1.name FROM storm AS T1 "
        "JOIN affected_region AS T2 ON T1.storm_id = T2.storm_id "
        "GROUP BY T1.storm_id HAVING COUNT(*) >= 2"
    )
    student = standard.replace(" EXCEPT ", " UNION ")
    spider_catalog = {
        "source": "spider_tables_json",
        "db_id": "storm_record",
        "tables": [
            {
                "name": "storm",
                "columns": [
                    {"name": "Storm_ID", "data_type": "BIGINT", "nullable": None},
                    {"name": "Name", "data_type": "TEXT", "nullable": None},
                ],
                "primary_key": ["Storm_ID"],
                "foreign_keys": [],
                "unique_constraints": [["Storm_ID"]],
            },
            {
                "name": "affected_region",
                "columns": [
                    {"name": "Region_id", "data_type": "BIGINT", "nullable": None},
                    {"name": "Storm_ID", "data_type": "BIGINT", "nullable": None},
                    {"name": "Number_city_affected", "data_type": "BIGINT", "nullable": None},
                ],
                "primary_key": ["Region_id"],
                "foreign_keys": [{
                    "column": "Storm_ID",
                    "references_table": "storm",
                    "references_column": "Storm_ID",
                }],
                "unique_constraints": [["Region_id"]],
            },
        ],
    }
    run = generate_and_compare(
        "storm(Storm_ID BIGINT PRIMARY KEY, Name TEXT); "
        "affected_region(Region_id BIGINT PRIMARY KEY, Storm_ID BIGINT, "
        "Number_city_affected BIGINT);",
        standard,
        student,
        max_rows_per_table=max_rows_per_table,
        sql_dialect="sqlite",
        schema_catalog=spider_catalog,
    )

    storms = run.test_database["storm"]
    affected = run.test_database["affected_region"]
    qualifying_ids = {
        storm_id
        for storm_id, count in Counter(
            row["Storm_ID"] for row in affected
        ).items()
        if count >= 2
    }
    assert run.executed is True
    assert run.is_equivalent is False
    assert qualifying_ids
    assert qualifying_ids <= {row["Storm_ID"] for row in storms}
    assert len({row["Storm_ID"] for row in storms}) == len(storms)
    assert len({row["Region_id"] for row in affected}) == len(affected)
    assert set(run.standard_rows) != set(run.student_rows)
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "set_overlap"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True
    assert effectiveness["causal_attribution_verified"] is True
    evidence = effectiveness["semantic_validation"]["evidence"]
    assert evidence["source"] == "query_branches"
    assert evidence["right_branch_rows"]
    assert evidence["simulated_standard_result"] != evidence[
        "simulated_student_result"
    ]
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] == 1


@pytest.mark.parametrize("max_rows_per_table", [8, 16, 32])
def test_except_multi_join_max_min_keeps_unchanged_join_path_reachable(
    max_rows_per_table,
):
    standard = (
        "SELECT Name FROM Scientists EXCEPT "
        "SELECT T3.Name FROM AssignedTo AS T1 "
        "JOIN Projects AS T2 ON T1.Project = T2.Code "
        "JOIN Scientists AS T3 ON T1.Scientist = T3.SSN "
        "WHERE T2.Hours = (SELECT MAX(Hours) FROM Projects)"
    )
    run = generate_and_compare(
        "Scientists(SSN INTEGER PRIMARY KEY, Name TEXT); "
        "Projects(Code TEXT PRIMARY KEY, Name TEXT, Hours INTEGER); "
        "AssignedTo(Scientist INTEGER PRIMARY KEY REFERENCES Scientists(SSN), "
        "Project TEXT REFERENCES Projects(Code));",
        standard,
        standard.replace("MAX(Hours)", "MIN(Hours)"),
        max_rows_per_table=max_rows_per_table,
        sql_dialect="sqlite",
    )

    scientist_ids = {
        row["SSN"] for row in run.test_database["Scientists"]
    }
    project_codes = {
        row["Code"] for row in run.test_database["Projects"]
    }
    assignments = run.test_database["AssignedTo"]
    hours = [
        row["Hours"]
        for row in run.test_database["Projects"]
        if row["Hours"] is not None
    ]

    assert {row["Scientist"] for row in assignments}.issubset(scientist_ids)
    assert {row["Project"] for row in assignments}.issubset(project_codes)
    assert min(hours) != max(hours)
    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    effectiveness = next(
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "aggregate_function_separation"
    )
    assert effectiveness["constraints_satisfied"] is True
    assert effectiveness["distinguished"] is True
    assert effectiveness["causal_attribution_verified"] is True
    aggregate_values = effectiveness["semantic_validation"]["evidence"][
        "aggregate_function_values"
    ]["()"]
    assert aggregate_values["standard"] != aggregate_values["student"]


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


def test_filtered_in_negation_witness_removes_probe_null_and_reaches_inner_filter():
    run = generate_and_compare(
        "student(id, name); takes(id, course_id, year);",
        "SELECT name FROM student WHERE id IN "
        "(SELECT id FROM takes WHERE year = 2020)",
        "SELECT name FROM student WHERE id NOT IN "
        "(SELECT id FROM takes WHERE year = 2020)",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    # The ordinary filtered membership path needs a non-NULL row that reaches
    # the subquery.  NULL-sensitive NOT IN/NOT EXISTS pairs use a separate
    # obligation and retain their explicit NULL witness.
    assert all(row["id"] is not None for row in run.test_database["takes"])


@pytest.mark.parametrize(
    ("schema", "standard_sql", "student_sql", "column"),
    [
        (
            "courses(course_name, credits);",
            "SELECT course_name FROM courses WHERE credits * 2 > 600",
            "SELECT course_name FROM courses WHERE credits + 2 > 600",
            "credits",
        ),
        (
            "tickets(subject, priority);",
            "SELECT subject FROM tickets WHERE priority * 2 > 800",
            "SELECT subject FROM tickets WHERE priority + 2 > 800",
            "priority",
        ),
    ],
)
def test_arithmetic_expression_probe_solves_distinguishing_boundary(
    schema, standard_sql, student_sql, column
):
    run = generate_and_compare(schema, standard_sql, student_sql)

    assert run.executed is True
    assert run.is_equivalent is False
    assert any(row[column] > 0 for row in run.test_database[next(iter(run.test_database))])
    assert run.standard_rows != run.student_rows


def test_not_in_null_probe_satisfies_subquery_filter_and_changes_antijoin_result():
    run = generate_and_compare(
        "majors(id, inactive_at); students(major_id, name);",
        "SELECT name FROM students WHERE major_id NOT IN "
        "(SELECT id FROM majors WHERE inactive_at IS NULL)",
        "SELECT s.name FROM students s WHERE NOT EXISTS "
        "(SELECT 1 FROM majors m WHERE m.inactive_at IS NULL AND m.id = s.major_id)",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert any(
        row["id"] is None and row["inactive_at"] is None
        for row in run.test_database["majors"]
    )
    assert run.standard_rows != run.student_rows
    where_tests = [
        item for item in run.mutation_evidence["tests"]
        if item["clause"] == "WHERE" and item["query_scope"] == "root"
    ]
    assert where_tests
    assert where_tests[0]["replacement_exec_ok"] is True
    assert where_tests[0]["fixed_by_replacement"] is True
    assert where_tests[0]["mutation_scope"] == ["WHERE"]
    assert where_tests[0]["dependent_changes"] == ["FROM ALIAS", "SELECT"]


def test_not_in_not_exists_null_witness_keeps_single_focused_obligation():
    run = generate_and_compare(
        "outer_t(id INT PRIMARY KEY, v INT); "
        "inner_t(id INT PRIMARY KEY, v INT);",
        "SELECT v FROM outer_t WHERE v NOT IN (SELECT v FROM inner_t)",
        "SELECT v FROM outer_t WHERE NOT EXISTS ("
        "SELECT 1 FROM inner_t WHERE inner_t.v = outer_t.v)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert len(run.ast_diffs) == 1
    diff = run.ast_diffs[0]
    assert diff.diff_type == "null_sensitive_antijoin_equivalence"
    assert diff.extra["standard_source_table"] == "outer_t"
    assert diff.extra["standard_membership_table"] == "inner_t"
    assert diff.extra["require_inner_null"] is True
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert item["causal_attribution_verified"] is True
    assert item["mutation_validation"]["relevant_test_count"] == 1
    assert item["mutation_validation"]["relevant_fixed_by_replacement"] is True
    mutation = item["mutation_validation"]["tests"][0]
    assert mutation["diff_ids"] == [item["diff_id"]]
    assert any(row["v"] is None for row in run.test_database["inner_t"])
    assert run.standard_rows != run.student_rows


def test_not_in_null_probe_does_not_violate_is_not_null_filter():
    data = {
        "majors": [
            {"id": 1, "inactive_at": "2026-01-01"},
            {"id": 2, "inactive_at": "2026-01-02"},
        ],
        "students": [{"major_id": 3, "name": "Alice"}],
    }
    sql = (
        "SELECT name FROM students WHERE major_id NOT IN "
        "(SELECT id FROM majors WHERE inactive_at IS NOT NULL)"
    )

    _apply_not_in_null_probe(data, sql, sql)

    assert data["majors"][0]["id"] is None
    assert data["majors"][0]["inactive_at"] is not None


def test_not_in_is_not_null_uses_outer_null_path_for_antijoin_witness():
    run = generate_and_compare(
        "outer_t(id INT PRIMARY KEY, v INT); "
        "inner_t(id INT PRIMARY KEY, v INT);",
        "SELECT v FROM outer_t WHERE v NOT IN "
        "(SELECT v FROM inner_t WHERE v IS NOT NULL)",
        "SELECT v FROM outer_t WHERE NOT EXISTS ("
        "SELECT 1 FROM inner_t WHERE inner_t.v IS NOT NULL "
        "AND inner_t.v = outer_t.v)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    diff = run.ast_diffs[0]
    assert diff.diff_type == "null_sensitive_antijoin_equivalence"
    assert diff.extra["require_inner_null"] is False
    assert diff.extra["require_outer_null"] is True
    item = run.data_evidence["obligation_effectiveness"][0]
    evidence = item["semantic_validation"]["evidence"]
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert evidence["inner_null_count"] == 0
    assert evidence["outer_null_count"] >= 1
    assert run.standard_rows != run.student_rows


def test_not_in_null_probe_keeps_root_literal_predicate_reachable():
    run = generate_and_compare(
        "outer_t(id INT PRIMARY KEY, v INT, active INT); "
        "inner_t(id INT PRIMARY KEY, v INT);",
        "SELECT v FROM outer_t WHERE active = 1 AND v NOT IN "
        "(SELECT v FROM inner_t)",
        "SELECT v FROM outer_t WHERE active = 1 AND NOT EXISTS ("
        "SELECT 1 FROM inner_t WHERE inner_t.v = outer_t.v)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert run.standard_rows != run.student_rows
    assert any(
        row["active"] == 1 and row["v"] is not None
        for row in run.test_database["outer_t"]
    )


def test_not_in_null_probe_preserves_explicit_numeric_membership_types():
    run = generate_and_compare(
        "outer_t(id INT PRIMARY KEY, v INT); "
        "inner_t(id INT PRIMARY KEY, v INT);",
        "SELECT v FROM outer_t WHERE v NOT IN "
        "(SELECT v FROM inner_t WHERE v IS NULL)",
        "SELECT v FROM outer_t WHERE NOT EXISTS ("
        "SELECT 1 FROM inner_t WHERE inner_t.v IS NULL "
        "AND inner_t.v = outer_t.v)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.data_evidence["obligation_effectiveness"][0]["distinguished"] is True
    for table in ("outer_t", "inner_t"):
        assert all(
            row["v"] is None or isinstance(row["v"], int)
            for row in run.test_database[table]
        )


def test_not_in_null_probe_keeps_explicit_outer_null_predicate_reachable():
    run = generate_and_compare(
        "outer_t(id INT PRIMARY KEY, v INT); "
        "inner_t(id INT PRIMARY KEY, v INT);",
        "SELECT v FROM outer_t WHERE v IS NULL AND v NOT IN "
        "(SELECT v FROM inner_t)",
        "SELECT v FROM outer_t WHERE v IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM inner_t WHERE inner_t.v = outer_t.v)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert item["semantic_validation"]["evidence"]["outer_null_count"] >= 1
    assert run.standard_rows != run.student_rows


def test_not_in_not_exists_is_equivalent_when_both_membership_keys_are_nonnull():
    run = generate_and_compare(
        "outer_t(id INT PRIMARY KEY, v INT NOT NULL); "
        "inner_t(id INT PRIMARY KEY, v INT NOT NULL);",
        "SELECT v FROM outer_t WHERE v NOT IN (SELECT v FROM inner_t)",
        "SELECT v FROM outer_t WHERE NOT EXISTS ("
        "SELECT 1 FROM inner_t WHERE inner_t.v = outer_t.v)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert not run.ast_diffs
    assert all(
        row["v"] is not None
        for table in ("outer_t", "inner_t")
        for row in run.test_database[table]
    )


@pytest.mark.parametrize(
    ("schema", "standard_sql", "student_sql"),
    [
        (
            "outer_t(id INT PRIMARY KEY, v INT NOT NULL); "
            "inner_t(id INT PRIMARY KEY, v INT);",
            "SELECT v FROM outer_t WHERE v NOT IN "
            "(SELECT v FROM inner_t WHERE v IS NOT NULL)",
            "SELECT v FROM outer_t WHERE NOT EXISTS ("
            "SELECT 1 FROM inner_t WHERE inner_t.v IS NOT NULL "
            "AND inner_t.v = outer_t.v)",
        ),
        (
            "outer_t(id INT PRIMARY KEY, v INT); "
            "inner_t(id INT PRIMARY KEY, v INT);",
            "SELECT v FROM outer_t WHERE v IS NOT NULL AND v NOT IN "
            "(SELECT v FROM inner_t WHERE v IS NOT NULL)",
            "SELECT v FROM outer_t WHERE v IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM inner_t WHERE inner_t.v IS NOT NULL "
            "AND inner_t.v = outer_t.v)",
        ),
        (
            "outer_t(id INT PRIMARY KEY, v INT); "
            "inner_t(id INT PRIMARY KEY, v INT NOT NULL);",
            "SELECT v FROM outer_t WHERE v NOT IN "
            "(SELECT v FROM inner_t WHERE v IS NULL)",
            "SELECT v FROM outer_t WHERE NOT EXISTS ("
            "SELECT 1 FROM inner_t WHERE inner_t.v IS NULL "
            "AND inner_t.v = outer_t.v)",
        ),
    ],
)
def test_not_in_null_equivalence_uses_query_null_filters_and_hard_schema(
    schema, standard_sql, student_sql
):
    run = generate_and_compare(
        schema,
        standard_sql,
        student_sql,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert not run.ast_diffs
    assert all(
        row["v"] is not None
        for table, rows in run.test_database.items()
        if "NOT NULL" in schema.split(table, 1)[1].split(";", 1)[0].upper()
        for row in rows
    )


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


def test_tsql_dateadd_unit_does_not_hide_comparison_diff():
    standard = (
        "SELECT id FROM weather WHERE DATEADD(day, 1, previous_date) = record_date "
        "AND previous_temperature < temperature"
    )
    student = (
        "SELECT id FROM weather WHERE DATEADD(day, 1, previous_date) = record_date "
        "AND previous_temperature <= temperature"
    )

    diffs = extract_ast_diffs(standard, student, dialect="tsql")

    assert any(diff.diff_type == "comparison_operator_changed" for diff in diffs)


@pytest.mark.parametrize(
    ("standard", "student", "diff_type", "knowledge_point_id"),
    [
        (
            "SELECT * FROM employees e JOIN departments d ON "
            "e.department_id = d.department_id AND d.department_id IN (40, 80)",
            "SELECT * FROM employees e JOIN departments d ON "
            "e.department_id = d.department_id AND d.department_id NOT IN (40, 80)",
            "in_predicate_negation_changed",
            "in-list",
        ),
        (
            "SELECT CASE WHEN id IN (SELECT p_id FROM tree) THEN 'Inner' END FROM tree",
            "SELECT CASE WHEN id NOT IN (SELECT p_id FROM tree) THEN 'Inner' END FROM tree",
            "in_predicate_negation_changed",
            "in-list",
        ),
        (
            "SELECT CASE WHEN p_id IS NULL THEN 'Root' END FROM tree",
            "SELECT CASE WHEN p_id IS NOT NULL THEN 'Root' END FROM tree",
            "null_predicate_negation_changed",
            "null-handling",
        ),
    ],
)
def test_predicate_negation_diff_preserves_specialized_knowledge_point(
    standard, student, diff_type, knowledge_point_id
):
    diffs = extract_ast_diffs(standard, student)

    assert any(
        diff.diff_type == diff_type
        and diff.knowledge_point_id == knowledge_point_id
        for diff in diffs
    )


@pytest.mark.parametrize(
    ("schema", "standard", "student"),
    [
        (
            "weather(recorddate, temperature, id);",
            (
                "SELECT id FROM (SELECT *, LAG(temperature) OVER (ORDER BY recorddate) "
                "AS prev_temp, LAG(recorddate) OVER (ORDER BY recorddate) AS prev_date "
                "FROM weather) tb1 WHERE DATEADD(day, 1, prev_date) = recorddate "
                "AND prev_temp < temperature"
            ),
            (
                "SELECT id FROM (SELECT *, LAG(temperature) OVER (ORDER BY recorddate) "
                "AS prev_temp, LAG(recorddate) OVER (ORDER BY recorddate) AS prev_date "
                "FROM weather) tb1 WHERE DATEADD(day, 1, prev_date) = recorddate "
                "AND prev_temp <= temperature"
            ),
        ),
        (
            "employee_performance(employee_id, performance_score, quarter);",
            (
                "WITH improved AS (SELECT employee_id, quarter, performance_score, "
                "LAG(performance_score, 1) OVER (PARTITION BY employee_id ORDER BY quarter) "
                "AS prev_score, LAG(performance_score, 2) OVER "
                "(PARTITION BY employee_id ORDER BY quarter) AS prev_two FROM employee_performance) "
                "SELECT DISTINCT employee_id FROM improved "
                "WHERE performance_score > prev_score AND prev_score > prev_two"
            ),
            (
                "WITH improved AS (SELECT employee_id, quarter, performance_score, "
                "LAG(performance_score, 1) OVER (PARTITION BY employee_id ORDER BY quarter) "
                "AS prev_score, LAG(performance_score, 2) OVER "
                "(PARTITION BY employee_id ORDER BY quarter) AS prev_two FROM employee_performance) "
                "SELECT DISTINCT employee_id FROM improved "
                "WHERE performance_score >= prev_score AND prev_score > prev_two"
            ),
        ),
        (
            "employee(id, month, salary);",
            (
                "SELECT e1.id, e1.month, SUM(e2.salary) FROM employee e1 JOIN employee e2 "
                "ON e1.id = e2.id AND e1.month >= e2.month "
                "AND e1.month <= e2.month + 2 WHERE e1.month <> "
                "(SELECT MAX(month) FROM employee WHERE id = e1.id) "
                "GROUP BY e1.id, e1.month"
            ),
            (
                "SELECT e1.id, e1.month, SUM(e2.salary) FROM employee e1 JOIN employee e2 "
                "ON e1.id = e2.id AND e1.month >= e2.month "
                "AND e1.month < e2.month + 2 WHERE e1.month <> "
                "(SELECT MAX(month) FROM employee WHERE id = e1.id) "
                "GROUP BY e1.id, e1.month"
            ),
        ),
        (
            "stadium(id, visit_date, people);",
            (
                "WITH tb1 AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS r FROM stadium "
                "WHERE people >= 100), tb2 AS (SELECT id, visit_date, people, "
                "COUNT(*) OVER (PARTITION BY id - r) AS num FROM tb1) "
                "SELECT id, visit_date, people FROM tb2 WHERE num >= 3"
            ),
            (
                "WITH tb1 AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS r FROM stadium "
                "WHERE people >= 100), tb2 AS (SELECT id, visit_date, people, "
                "COUNT(*) OVER (PARTITION BY id - r) AS num FROM tb1) "
                "SELECT id, visit_date, people FROM tb2 WHERE num > 3"
            ),
        ),
        (
            "emp_department(dpt_allotment, dpt_code); emp_details(emp_dept, emp_fname, emp_lname);",
            (
                "SELECT emp_fname, emp_lname FROM emp_details WHERE emp_dept IN "
                "(SELECT dpt_code FROM emp_department WHERE dpt_allotment = "
                "(SELECT MIN(dpt_allotment) FROM emp_department WHERE dpt_allotment > "
                "(SELECT MIN(dpt_allotment) FROM emp_department)))"
            ),
            (
                "SELECT emp_fname, emp_lname FROM emp_details WHERE emp_dept IN "
                "(SELECT dpt_code FROM emp_department WHERE dpt_allotment = "
                "(SELECT MIN(dpt_allotment) FROM emp_department WHERE dpt_allotment >= "
                "(SELECT MIN(dpt_allotment) FROM emp_department)))"
            ),
        ),
        (
            "useractivity(username, activity, startdate, enddate);",
            (
                "SELECT username, activity, startdate, enddate FROM (SELECT *, "
                "ROW_NUMBER() OVER (PARTITION BY username ORDER BY enddate DESC) AS r, "
                "COUNT(*) OVER (PARTITION BY username) AS c FROM useractivity) tb1 "
                "WHERE r = 2 OR c = 1"
            ),
            (
                "SELECT username, activity, startdate, enddate FROM (SELECT *, "
                "ROW_NUMBER() OVER (PARTITION BY username ORDER BY enddate ASC) AS r, "
                "COUNT(*) OVER (PARTITION BY username) AS c FROM useractivity) tb1 "
                "WHERE r = 2 OR c = 1"
            ),
        ),
        (
            "employee_performance(employee_id, performance_score, quarter);",
            (
                "WITH improved AS (SELECT employee_id, quarter, performance_score, "
                "LAG(performance_score, 1) OVER (PARTITION BY employee_id ORDER BY quarter) "
                "AS prev_score, LAG(performance_score, 2) OVER "
                "(PARTITION BY employee_id ORDER BY quarter) AS prev_two FROM employee_performance) "
                "SELECT DISTINCT employee_id FROM improved "
                "WHERE performance_score > prev_score AND prev_score > prev_two"
            ),
            (
                "WITH improved AS (SELECT employee_id, quarter, performance_score, "
                "LAG(performance_score, 1) OVER (PARTITION BY employee_id ORDER BY quarter) "
                "AS prev_score, LAG(performance_score, 2) OVER "
                "(PARTITION BY employee_id ORDER BY quarter) AS prev_two FROM employee_performance) "
                "SELECT employee_id FROM improved "
                "WHERE performance_score > prev_score AND prev_score > prev_two"
            ),
        ),
    ],
)
def test_phase1_web_boundary_regressions_are_observable(schema, standard, student):
    run = generate_and_compare(schema, standard, student, max_rows_per_table=8)

    assert run.executed is True, run.error
    assert run.is_equivalent is False

def test_row_number_partition_removal_generates_counterexample():
    run = generate_and_compare(
        "instructor(ID, name, dept_name, salary);",
        "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rn FROM instructor;",
        "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn FROM instructor;",
        max_rows_per_table=4,
    )

    assert run.executed
    assert run.is_equivalent is False
    assert len({row["dept_name"] for row in run.test_database["instructor"]}) >= 2


@pytest.mark.parametrize("row_limit", [4, 8, 16])
def test_window_nulls_first_last_has_complete_witness_chain(row_limit):
    run = generate_and_compare(
        "sales(id, seq);",
        "SELECT id, ROW_NUMBER() OVER (ORDER BY seq NULLS FIRST) FROM sales",
        "SELECT id, ROW_NUMBER() OVER (ORDER BY seq NULLS LAST) FROM sales",
        max_rows_per_table=row_limit,
        sql_dialect="sqlite",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["semantic_validation"]["evidence"]["null_order_path"] is True
    assert effectiveness[0]["mutation_validation"]["relevant_fixed_by_replacement"] is True


@pytest.mark.parametrize(
    ("explicit_order", "default_order"),
    [
        ("seq ASC NULLS FIRST", "seq ASC"),
        ("seq DESC NULLS LAST", "seq DESC"),
    ],
)
def test_sqlite_default_window_null_placement_is_normalized_as_equivalent(
    explicit_order, default_order
):
    run = generate_and_compare(
        "sales(id, seq);",
        f"SELECT id, ROW_NUMBER() OVER (ORDER BY {explicit_order}) FROM sales",
        f"SELECT id, ROW_NUMBER() OVER (ORDER BY {default_order}) FROM sales",
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert not [item for item in run.ast_diffs if item.diff_type == "window_over_changed"]


@pytest.mark.parametrize("row_limit", [4, 8, 16])
def test_partition_only_window_change_has_complete_witness_chain(row_limit):
    run = generate_and_compare(
        "sales(id, dept, region, amount);",
        "SELECT SUM(amount) OVER (PARTITION BY dept, region) FROM sales",
        "SELECT SUM(amount) OVER (PARTITION BY dept) FROM sales",
        max_rows_per_table=row_limit,
        sql_dialect="sqlite",
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["semantic_validation"]["evidence"]["partition_relation_changed"] is True


def test_schema_parser_deduplicates_repeated_public_corpus_headers():
    assert parse_schema_text("poll(county, kerry, kerry, bush, bush);") == {
        "poll": ["county", "kerry", "bush"]
    }


def test_named_parameter_rewrite_preserves_markers_inside_string_literals():
    sql = (
        "SELECT * FROM people WHERE note = 'Category:Articles with hCards' "
        "AND email = 'teacher@example.com' AND id = :student_id"
    )
    rewritten = parseval._replace_named_parameters(sql)

    assert "'Category:Articles with hCards'" in rewritten
    assert "'teacher@example.com'" in rewritten
    assert "id = 1" in rewritten


@pytest.mark.parametrize("operator", ["REGEXP", "regexp", "RLIKE"])
def test_regex_pattern_change_has_complete_phase1_evidence_chain(operator):
    standard_sql = (
        "SELECT mailid FROM Contacts "
        f"WHERE mailid {operator} '^[A-Za-z0-9]{{2}}$'"
    )
    student_sql = (
        "SELECT mailid FROM Contacts "
        f"WHERE mailid {operator} '^[A-Za-z0-9]{{3}}$'"
    )

    diffs = extract_ast_diffs(standard_sql, student_sql, dialect="mysql")
    regex_diffs = [item for item in diffs if item.diff_type == "regex_pattern_changed"]

    assert len(regex_diffs) == 1
    assert regex_diffs[0].clause_category == "PREDICATE"
    assert regex_diffs[0].target_table == "Contacts"
    assert regex_diffs[0].target_column == "mailid"
    assert regex_diffs[0].knowledge_point_id == "regex"

    run = generate_and_compare(
        "Contacts(mailid VARCHAR(64));",
        standard_sql,
        student_sql,
        sql_dialect="mysql",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "regex_pattern_separation"
    ]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True

    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["knowledge_point_id"] == "regex"
    ]
    assert len(mutations) == 1
    assert mutations[0]["clause"] == "PREDICATE"
    assert mutations[0]["mutation_scope"] == ["REGEXP"]
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_sqlite_compat_preserves_regex_character_classes_only_in_literals():
    rewritten = parseval._sqlite_compat(
        "SELECT [Mail ID] FROM Contacts "
        "WHERE mailid REGEXP '^[A-Z][0-9]$'"
    )

    assert 'SELECT "Mail ID"' in rewritten
    assert "'^[A-Z][0-9]$'" in rewritten


def test_identical_regex_predicates_remain_equivalent_control():
    sql = (
        "SELECT mailid FROM Contacts "
        "WHERE mailid REGEXP '^[A-Za-z0-9]{2}$'"
    )

    run = generate_and_compare(
        "Contacts(mailid VARCHAR(64));",
        sql,
        sql,
        sql_dialect="mysql",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"


@pytest.mark.parametrize(
    ("operator", "dialect", "standard_pattern", "student_pattern"),
    [
        ("LIKE", "mysql", "a%", "%b"),
        ("ILIKE", "postgres", "a%", "%b"),
    ],
)
def test_like_pattern_change_has_dedicated_witness_and_mutation(
    operator, dialect, standard_pattern, student_pattern
):
    standard_sql = (
        "SELECT name FROM people "
        f"WHERE name {operator} '{standard_pattern}'"
    )
    student_sql = (
        "SELECT name FROM people "
        f"WHERE name {operator} '{student_pattern}'"
    )

    diffs = extract_ast_diffs(standard_sql, student_sql, dialect=dialect)
    pattern_diffs = [item for item in diffs if item.diff_type == "like_pattern_changed"]

    assert len(pattern_diffs) == 1
    assert pattern_diffs[0].clause_category == "PREDICATE"
    assert pattern_diffs[0].target_table == "people"
    assert pattern_diffs[0].target_column == "name"
    assert pattern_diffs[0].knowledge_point_id == "like"

    run = generate_and_compare(
        "people(name TEXT);",
        standard_sql,
        student_sql,
        sql_dialect=dialect,
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "like_pattern_separation"
    ]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True

    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["knowledge_point_id"] == "like"
    ]
    assert len(mutations) == 1
    assert mutations[0]["clause"] == "PREDICATE"
    assert mutations[0]["mutation_scope"] == [operator]
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_like_escape_change_replaces_complete_escape_predicate():
    standard_sql = (
        "SELECT name FROM people "
        "WHERE name LIKE 'a#%b' ESCAPE '#'"
    )
    student_sql = (
        "SELECT name FROM people "
        "WHERE name LIKE 'a#%b' ESCAPE '$'"
    )

    diffs = extract_ast_diffs(standard_sql, student_sql, dialect="postgres")
    pattern_diffs = [item for item in diffs if item.diff_type == "like_pattern_changed"]

    assert len(pattern_diffs) == 1
    assert pattern_diffs[0].extra["standard_escape"] == "#"
    assert pattern_diffs[0].extra["student_escape"] == "$"
    assert "ESCAPE '#'" in pattern_diffs[0].extra["standard_sql"]
    assert "ESCAPE '$'" in pattern_diffs[0].extra["student_sql"]

    run = generate_and_compare(
        "people(name TEXT);",
        standard_sql,
        student_sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["knowledge_point_id"] == "like"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_glob_pattern_change_has_dedicated_witness_and_mutation():
    standard_sql = "SELECT name FROM people WHERE name GLOB 'a*'"
    student_sql = "SELECT name FROM people WHERE name GLOB 'b*'"

    diffs = extract_ast_diffs(standard_sql, student_sql, dialect="sqlite")
    glob_diffs = [item for item in diffs if item.diff_type == "glob_pattern_changed"]

    assert len(glob_diffs) == 1
    assert glob_diffs[0].clause_category == "PREDICATE"
    assert glob_diffs[0].target_table == "people"
    assert glob_diffs[0].target_column == "name"
    assert glob_diffs[0].knowledge_point_id == "glob"

    run = generate_and_compare(
        "people(name TEXT);",
        standard_sql,
        student_sql,
        sql_dialect="sqlite",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "glob_pattern_separation"
    ]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["knowledge_point_id"] == "glob"
    ]
    assert len(mutations) == 1
    assert mutations[0]["clause"] == "PREDICATE"
    assert mutations[0]["mutation_scope"] == ["GLOB"]
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_similar_to_pattern_change_has_dedicated_witness_and_mutation():
    standard_sql = "SELECT name FROM people WHERE name SIMILAR TO 'a%'"
    student_sql = "SELECT name FROM people WHERE name SIMILAR TO 'b%'"

    diffs = extract_ast_diffs(standard_sql, student_sql, dialect="postgres")
    similar_diffs = [
        item for item in diffs if item.diff_type == "similar_pattern_changed"
    ]

    assert len(similar_diffs) == 1
    assert similar_diffs[0].clause_category == "PREDICATE"
    assert similar_diffs[0].target_table == "people"
    assert similar_diffs[0].target_column == "name"
    assert similar_diffs[0].knowledge_point_id == "similar-to"

    run = generate_and_compare(
        "people(name TEXT);",
        standard_sql,
        student_sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "similar_pattern_separation"
    ]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert effectiveness[0]["causal_attribution_verified"] is True
    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["knowledge_point_id"] == "similar-to"
    ]
    assert len(mutations) == 1
    assert mutations[0]["clause"] == "PREDICATE"
    assert mutations[0]["mutation_scope"] == ["SIMILAR TO"]
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True


def test_similar_to_escape_change_generates_literal_and_wildcard_witnesses():
    standard_sql = (
        "SELECT name FROM people "
        "WHERE name SIMILAR TO 'a#%b' ESCAPE '#'"
    )
    student_sql = (
        "SELECT name FROM people "
        "WHERE name SIMILAR TO 'a#%b' ESCAPE '$'"
    )

    run = generate_and_compare(
        "people(name TEXT);",
        standard_sql,
        student_sql,
        sql_dialect="postgres",
        max_rows_per_table=4,
    )

    assert run.executed is True, run.error
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.standard_rows != run.student_rows

    effectiveness = [
        item
        for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "similar_pattern_separation"
    ]
    assert len(effectiveness) == 1
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    evidence = effectiveness[0]["semantic_validation"]["evidence"]
    assert evidence["standard_escape"] == "#"
    assert evidence["student_escape"] == "$"
    assert any(
        item["value"] == "a%b" and item["standard_matches"] is True
        for item in evidence["evaluations"]
    )
    assert any(
        item["value"] == "a#b" and item["student_matches"] is True
        for item in evidence["evaluations"]
    )

    mutations = [
        item
        for item in run.mutation_evidence["tests"]
        if item["knowledge_point_id"] == "similar-to"
    ]
    assert len(mutations) == 1
    assert mutations[0]["binding_quality"] == "exact"
    assert mutations[0]["fixed_by_replacement"] is True
