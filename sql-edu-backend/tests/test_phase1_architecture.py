from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.parseval_data_generator as phase1


CORE = Path(__file__).resolve().parents[1] / "core"
PHASE1_LAYERS = (
    "phase1_foundation",
    "phase1_sql_semantics",
    "phase1_constraints",
    "phase1_query_paths",
    "phase1_witness_strategies",
    "phase1_witness_materialization",
    "phase1_evidence",
    "phase1_engine",
)


def test_phase1_facade_is_small_and_keeps_the_frozen_public_api():
    facade = CORE / "parseval_data_generator.py"

    assert facade.read_text(encoding="utf-8").count("\n") + 1 <= 100
    assert phase1.__all__ == [
        "SandboxRun",
        "extract_ast_diffs",
        "generate_and_compare",
        "generate_test_database",
        "generate_witness_suite",
        "parse_schema_column_types",
        "parse_schema_text",
        "transpile_to_sqlite",
    ]


def test_shared_ast_contract_contains_no_unused_structure_ir():
    path = CORE / "ast_schema.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]

    assert len(path.read_text(encoding="utf-8").splitlines()) <= 100
    assert classes == ["ASTDiffNode"]


def test_phase1_layers_form_a_one_way_dependency_graph():
    positions = {name: index for index, name in enumerate(PHASE1_LAYERS)}

    for module_name in PHASE1_LAYERS:
        path = CORE / f"{module_name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 5_000
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                dependency = node.module.removeprefix("core.")
                if dependency in positions:
                    assert positions[dependency] < positions[module_name], (
                        f"{module_name} must not import later layer {dependency}"
                    )
                assert all(alias.name != "*" for alias in node.names)


def test_sqlite_executor_registers_only_the_regexp_callback():
    for filename in ("phase1_evidence.py", "witness_generation/validators.py"):
        path = CORE / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        registered = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "create_function":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                registered.append(node.args[0].value)

        assert registered == ["REGEXP"], filename


def test_all_sqlglot_dialect_arguments_are_fixed_to_sqlite():
    paths = [CORE / f"{module}.py" for module in PHASE1_LAYERS]
    paths.extend((CORE / "witness_generation").glob("*.py"))
    paths.append(CORE / "ast_schema.py")

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in {"read", "write", "dialect"}:
                    continue
                assert isinstance(keyword.value, ast.Constant), path.name
                assert keyword.value.value == "sqlite", path.name


def test_core_has_no_external_database_driver_imports():
    forbidden_roots = {
        "pymysql",
        "MySQLdb",
        "psycopg",
        "psycopg2",
        "oracledb",
        "cx_Oracle",
        "pyodbc",
        "sqlalchemy",
        "duckdb",
    }
    imported_roots = set()
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

    assert forbidden_roots.isdisjoint(imported_roots)


@pytest.mark.parametrize(
    ("student_sql", "feature"),
    [
        ("SELECT TOP 1 id FROM t", "TOP_LIMIT"),
        ("SELECT DATEADD(day, 1, id) FROM t", "NON_SQLITE_DATE_FUNCTION"),
        ("SELECT YEAR(id) FROM t", "NON_SQLITE_DATE_FUNCTION"),
        ("SELECT EXTRACT(YEAR FROM id) FROM t", "EXTRACT"),
        (
            "SELECT id FROM t WHERE REGEXP_LIKE(CAST(id AS TEXT), '^1')",
            "NON_SQLITE_SCALAR_FUNCTION",
        ),
        (
            "SELECT id FROM t WHERE id = ANY (SELECT id FROM t)",
            "QUANTIFIED_SUBQUERY_COMPARISON",
        ),
        ("SELECT id FROM t WHERE CAST(id AS TEXT) ILIKE '1%'", "ILIKE"),
        ("SELECT id FROM t WHERE CAST(id AS TEXT) SIMILAR TO '1%'", "SIMILAR_TO"),
        ("SELECT DISTINCT ON (id) id FROM t", "DISTINCT_ON"),
        (
            "SELECT id FROM t QUALIFY ROW_NUMBER() OVER () = 1",
            "QUALIFY",
        ),
        ("SELECT id FROM t TABLESAMPLE SYSTEM (10)", "TABLE_SAMPLE"),
        ("SELECT id FROM t FETCH FIRST 1 ROWS WITH TIES", "LIMIT_WITH_TIES"),
        (
            "SELECT * FROM t JOIN LATERAL (SELECT 1) AS x ON TRUE",
            "LATERAL",
        ),
        ("SELECT id FROM public.t", "ATTACHED_DATABASE_NAMESPACE"),
    ],
)
def test_non_sqlite_features_fail_closed_before_execution(student_sql, feature):
    run = phase1.generate_and_compare(
        "t(id INTEGER)",
        "SELECT id FROM t",
        student_sql,
    )

    assert run.executed is False
    assert run.status == "KNOWN_GAP"
    assert run.judge_status == "UNSUPPORTED"
    assert feature in run.data_evidence["unsupported_features"]


def test_boundary_scanner_ignores_literals_and_sqlite_regexp_still_executes():
    literal_sql = (
        "SELECT 'DATEADD(day, 1, x)', 'name@example.com', "
        "'value:token', 'PIVOT'"
    )
    literal_run = phase1.generate_and_compare("", literal_sql, literal_sql)
    regexp_run = phase1.generate_and_compare(
        "t(id INTEGER, name TEXT)",
        "SELECT id FROM t WHERE name REGEXP '^A'",
        "SELECT id FROM t WHERE name REGEXP '^A'",
    )

    assert literal_run.executed is True
    assert literal_run.status == "SUPPORTED"
    assert regexp_run.executed is True
    assert regexp_run.status == "SUPPORTED"


def test_missing_join_predicate_identity_is_hash_seed_independent():
    diffs = phase1.extract_ast_diffs(
        "SELECT student.name, course.title "
        "FROM student "
        "JOIN takes ON student.id = takes.id "
        "JOIN course ON takes.course_id = course.course_id",
        "SELECT student.name, course.title FROM student, takes, course",
    )
    join_diff = next(diff for diff in diffs if diff.diff_type == "join_on_changed")

    assert join_diff.extra["standard_sql"] == "student.id = takes.id"


def test_removed_execution_routes_do_not_reappear():
    implementation = "\n".join(
        (CORE / f"{module}.py").read_text(encoding="utf-8")
        for module in PHASE1_LAYERS
    )
    forbidden = {
        "native_query_session",
        "execute_native_query",
        "native_executor_url",
        "_dialect_candidates",
        "dialect_resolution",
        "_select_execution_backend",
        "_execute_with_backend",
        "similar_pattern_separation",
        "distinct_on_competing_payload",
        "exp.Lateral",
        "LATERAL_TO",
        "is_lateral",
        'dialect="mysql"',
        'dialect="postgres"',
        'dialect="oracle"',
        'dialect="tsql"',
    }

    assert not {token for token in forbidden if token in implementation}
