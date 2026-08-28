from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sqlite3
import sys
import tarfile
import time

import pytest
import sqlglot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "data_construct_test"
    / "scripts"
    / "prepare_phase1_web_gate_corpus.py"
)
COLLECTOR_PATH = (
    PROJECT_ROOT
    / "data_construct_test"
    / "scripts"
    / "collect_web_sql_corpus.py"
)
SPIDER_SCHEMA_CATALOG_PATH = (
    PROJECT_ROOT
    / "data_construct_test"
    / "scripts"
    / "spider_schema_catalog.py"
)
ONLINE_RANDOM250_PATH = (
    PROJECT_ROOT
    / "data_construct_test"
    / "scripts"
    / "run_online_random250_structure_generation_tests.py"
)


def _load_selector_module():
    spec = importlib.util.spec_from_file_location("prepare_phase1_web_gate_corpus", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_collector_module():
    spec = importlib.util.spec_from_file_location("collect_web_sql_corpus", COLLECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_spider_schema_catalog_module():
    spec = importlib.util.spec_from_file_location("spider_schema_catalog", SPIDER_SCHEMA_CATALOG_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_online_random250_module():
    spec = importlib.util.spec_from_file_location(
        "run_online_random250_structure_generation_tests", ONLINE_RANDOM250_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_online_mutation_builder_bounds_pathological_parser_work():
    online = _load_online_random250_module()
    if not hasattr(online.signal, "SIGALRM"):
        pytest.skip("POSIX parser timeout is not available on this platform")

    with pytest.raises(TimeoutError, match="construction budget"):
        online._run_with_parser_timeout(lambda: time.sleep(0.1), timeout_seconds=0.01)

    assert online._run_with_parser_timeout(lambda: "ok", timeout_seconds=0.1) == "ok"


def test_online_case_evaluator_has_hard_timeout_and_fail_closed_projection():
    online = _load_online_random250_module()
    case = {
        "id": "bounded-evaluator-fixture",
        "dataset": "fixture",
        "structure": "SELECT",
        "source": "fixture",
        "source_id": "fixture",
        "source_url": "https://example.invalid/fixture",
        "member": "fixture.sql",
        "standard": "SELECT 1",
        "student": "SELECT 1",
        "expected_equivalent": True,
    }
    result = online._evaluate_case_bounded(
        case,
        max_rows=4,
        worker_memory_mb=512,
        timeout_seconds=0.0001,
    )
    assert result["data_generation_status"] == "RESOURCE_LIMIT"
    assert result["equivalence_conclusion"] == "UNDECIDED"
    assert result["strict_pass"] is False


def test_spider_tables_json_catalog_preserves_physical_schema_and_keys():
    spider = _load_spider_schema_catalog_module()
    catalog = spider.normalize_spider_schema({
        "db_id": "concert_singer",
        "table_names_original": ["stadium", "concert"],
        "column_names_original": [
            [-1, "*"],
            [0, "stadium_id"],
            [0, "name"],
            [1, "concert_id"],
            [1, "stadium_id"],
            [1, "year"],
        ],
        "column_types": ["text", "number", "text", "number", "number", "time"],
        "primary_keys": [1, 3],
        "foreign_keys": [[4, 1]],
    })

    stadium, concert = catalog["tables"]
    assert stadium["primary_key"] == ["stadium_id"]
    assert concert["primary_key"] == ["concert_id"]
    assert concert["foreign_keys"] == [{
        "column": "stadium_id",
        "references_table": "stadium",
        "references_column": "stadium_id",
    }]
    assert "stadium_id BIGINT PRIMARY KEY" in spider.compact_schema(catalog)
    assert "year DATETIME" in spider.compact_schema(catalog)


def test_spider_compact_schema_does_not_render_invalid_composite_primary_keys():
    spider = _load_spider_schema_catalog_module()
    catalog = spider.normalize_spider_schema({
        "db_id": "composite_key_example",
        "table_names_original": ["enrollment"],
        "column_names_original": [
            [-1, "*"],
            [0, "student_id"],
            [0, "course_id"],
            [0, "grade"],
        ],
        "column_types": ["text", "number", "number", "text"],
        "primary_keys": [1, 2],
        "foreign_keys": [],
    })

    compact = spider.compact_schema(catalog)
    assert catalog["tables"][0]["primary_key"] == ["student_id", "course_id"]
    assert "PRIMARY KEY" not in compact
    assert "student_id BIGINT" in compact
    assert "course_id BIGINT" in compact


def test_collector_splits_semicolon_free_ctes_and_rejects_dml():
    collector = _load_collector_module()
    text = """
    -- first read-only example
WITH ranked AS (
  SELECT employee_id, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rn
  FROM employee
)
SELECT employee_id FROM ranked WHERE rn = 1

-- second read-only example
WITH totals AS (
  SELECT department_id, SUM(salary) AS total FROM employee GROUP BY department_id
)
SELECT department_id FROM totals

-- mutation example must never enter an equivalence corpus
WITH top_sales AS (SELECT employee_id FROM sales)
UPDATE employee SET salary = salary + 1
FROM top_sales WHERE employee.employee_id = top_sales.employee_id
"""

    extracted = list(collector._extract_sql_text(text))

    assert len(extracted) == 2
    assert all(item["sql"].startswith("WITH") for item in extracted)
    assert all("UPDATE" not in item["sql"].upper() for item in extracted)


def test_collector_rejects_select_into_and_delete_cte():
    collector = _load_collector_module()

    assert collector._is_read_only_query("SELECT * INTO backup FROM employee") is False
    assert collector._is_read_only_query(
        "WITH duplicate_rows AS (SELECT id FROM employee) DELETE FROM duplicate_rows"
    ) is False


def test_collector_never_starts_a_query_inside_tutorial_comments():
    collector = _load_collector_module()
    extracted = list(collector._extract_sql_text(
        """
        -- Select all departments whose budget is above average.
        SELECT * FROM departments
        WHERE budget > (SELECT AVG(budget) FROM departments);
        /* With a subquery */
        SELECT title FROM movies WHERE code NOT IN (
          SELECT movie FROM theaters WHERE movie IS NOT NULL
        );
        """
    ))

    assert len(extracted) == 2
    assert extracted[0]["sql"].startswith("SELECT * FROM departments")
    assert extracted[1]["sql"].startswith("SELECT title FROM movies")


def test_ddl_catalog_preserves_types_nullability_primary_and_foreign_keys():
    collector = _load_collector_module()
    catalog = collector._parse_ddl_catalog(
        """
        CREATE TABLE departments (
          id INTEGER PRIMARY KEY,
          name VARCHAR(80) NOT NULL UNIQUE
        );
        CREATE TABLE employees (
          id INTEGER NOT NULL,
          department_id INTEGER,
          salary DECIMAL(10, 2),
          CONSTRAINT employees_pk PRIMARY KEY (id),
          CONSTRAINT employee_department_fk FOREIGN KEY (department_id)
            REFERENCES departments(id)
        );
        """,
        dialect="postgresql",
        source_id="fixture",
        database_id="fixture-db",
    )

    departments, employees = catalog["tables"]
    assert departments["primary_key"] == ["id"]
    assert departments["columns"][1]["nullable"] is False
    assert departments["unique_constraints"] == [["name"], ["id"]]
    assert employees["primary_key"] == ["id"]
    assert employees["foreign_keys"] == [{
        "columns": ["department_id"],
        "references_table": "departments",
        "references_columns": ["id"],
    }]
    assert employees["columns"][2]["data_type"].startswith("DECIMAL")


def test_archive_collector_pairs_each_answer_directory_with_its_ddl(tmp_path):
    collector = _load_collector_module()
    archive_path = tmp_path / "teaching.tar.gz"
    documents = {
        "repo/SQL_exercise_01/1_build_schema.sql": b"""
            CREATE TABLE Manufacturers (Code INTEGER PRIMARY KEY, Name TEXT NOT NULL);
            CREATE TABLE Products (
              Code INTEGER PRIMARY KEY,
              Name TEXT NOT NULL,
              Manufacturer INTEGER REFERENCES Manufacturers(Code)
            );
        """,
        "repo/SQL_exercise_01/1_questions_and_solutions.sql": b"""
            -- 1.1 Select product names.
            SELECT Name FROM Products;
            -- 1.2 Select products and manufacturers.
            SELECT p.Name, m.Name FROM Products p
            JOIN Manufacturers m ON p.Manufacturer = m.Code;
            INSERT INTO Products VALUES (2, 'unsafe', 1);
        """,
    }
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, payload in documents.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    source = {
        "id": "paired-fixture",
        "name": "paired fixture",
        "kind": "real_sql_tutorial_repository",
        "local_path": str(archive_path),
        "dialect": "postgresql",
        "extraction": {
            "mode": "archive_ddl_queries",
            "query_members": ["*/SQL_exercise_*/*questions_and_solution*.sql"],
            "schema_members": ["*/SQL_exercise_*/*build_schema.sql"],
            "schema_pairing": "directory",
            "query_format": "numbered_comments",
        },
    }
    records = collector.collect_source(source, tmp_path, 1, True, 10)

    assert len(records) == 2
    assert all(record["replay_eligible"] is True for record in records)
    assert all(record["schema_trust"] == "authoritative_source_catalog" for record in records)
    assert all("INSERT" not in record["sql"].upper() for record in records)
    products = records[0]["schema_catalog"]["tables"][1]
    assert products["primary_key"] == ["Code"]
    assert products["foreign_keys"][0]["references_table"] == "Manufacturers"


def test_sqlite_catalog_reads_schema_without_loading_table_rows():
    collector = _load_collector_module()
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            "CREATE TABLE parent(id INTEGER PRIMARY KEY, code TEXT UNIQUE);"
            "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "FOREIGN KEY(parent_id) REFERENCES parent(id));"
        )
        raw = connection.serialize()
    finally:
        connection.close()

    catalog = collector._sqlite_catalog(raw, source_id="sqlite-fixture", database_id="fixture")

    tables = {table["name"]: table for table in catalog["tables"]}
    parent, child = tables["parent"], tables["child"]
    assert parent["primary_key"] == ["id"]
    assert ["code"] in parent["unique_constraints"]
    assert child["foreign_keys"] == [{
        "columns": ["parent_id"],
        "references_table": "parent",
        "references_columns": ["id"],
    }]


def test_query_inferred_schema_is_reference_only():
    collector = _load_collector_module()
    record = collector._record(
        {"id": "inferred", "kind": "fixture", "dialect": "generic"},
        "SELECT score FROM attempts",
        "",
        "fixture.sql",
        "generic_recursive",
    )

    assert record["schema_trust"] == "query_text_inferred"
    assert record["replay_eligible"] is False


def test_archive_admission_detects_mixed_dialects_and_catalog_mismatches():
    collector = _load_collector_module()
    catalog = {
        "tables": [{"name": "Products", "columns": [{"name": "name"}]}],
    }

    dialect = collector._detected_query_dialect(
        "SELECT TOP 1 name FROM Products",
        "mysql",
        mixed=True,
    )
    ast = collector._strict_query_ast("SELECT TOP 1 name FROM Products", dialect)

    assert dialect == "tsql"
    assert ast is not None
    assert collector._catalog_query_compatibility(ast, catalog) == (True, [])
    system_ast = collector._strict_query_ast(
        "SELECT * FROM SYS.ALL_INDEXES",
        "oracle",
    )
    assert system_ast is not None
    assert collector._catalog_query_compatibility(system_ast, catalog) == (
        False,
        ["all_indexes"],
    )
    assert collector._strict_query_ast("SELECT name FROM Products )", "mysql") is None
    assert collector._is_read_only_query(
        "WITH active AS (SELECT id FROM employee) SELECT id FROM active"
    ) is True
    assert collector._is_read_only_query(
        "WITH first AS (SELECT id FROM employee) SELECT id FROM first "
        "WITH second AS (SELECT id FROM department) SELECT id FROM second"
    ) is False


def test_spider_preflight_forwards_authoritative_schema_catalog(monkeypatch):
    selector = _load_selector_module()
    catalog = {
        "source": "spider_tables_json",
        "db_id": "department_management",
        "tables": [{"name": "head", "columns": [{"name": "age"}]}],
    }
    captured = {}

    def fake_case(*args, **kwargs):
        captured.update(kwargs)
        return {"schema_catalog": kwargs.get("schema_catalog")}

    monkeypatch.setattr(
        selector,
        "_benchmark_helpers",
        lambda: (None, None, None, lambda _item: "sqlite"),
    )
    monkeypatch.setattr(selector, "_case", fake_case)
    monkeypatch.setattr(
        selector,
        "run_case",
        lambda case: {"expectation_met": case["schema_catalog"] is catalog},
    )

    assert selector._identity_passes({
        "id": "spider-case",
        "sql": "SELECT age FROM head",
        "schema": "head(age);",
        "schema_catalog": catalog,
    }) is True
    assert captured["schema_catalog"] is catalog


def test_spider_collector_accepts_authoritative_schema_retained_in_local_snapshot(tmp_path):
    collector = _load_collector_module()
    source_file = tmp_path / "spider_snapshot.jsonl"
    source_file.write_text(
        '{"query":"SELECT age FROM head WHERE age > 56",'
        '"schema":"head(age);","schema_catalog":{"db_id":"department_management"},'
        '"db_id":"department_management"}\n',
        encoding="utf-8",
    )
    records = collector._collect_generic(
        source_file,
        {
            "id": "spider_hf_train",
            "name": "Spider local snapshot",
            "kind": "text_to_sql_benchmark",
            "dialect": "generic",
            "extraction": {"mode": "json_recursive"},
        },
        max_items=10,
        spider_catalog=None,
    )
    assert len(records) == 1
    assert records[0]["schema"] == "head(age);"
    assert records[0]["schema_trust"] == "authoritative_source_catalog"
    assert records[0]["source_id"] == "spider_hf_train"


def test_spider_collector_rejects_query_inferred_schema_without_catalog(tmp_path):
    collector = _load_collector_module()
    source_file = tmp_path / "spider_inferred.jsonl"
    source_file.write_text(
        '{"query":"SELECT age FROM head WHERE age > 56",'
        '"schema":"head(age);","db_id":"department_management"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="authoritative schema catalog"):
        collector._collect_generic(
            source_file,
            {"id": "spider_hf_train", "dialect": "generic", "extraction": {}},
            max_items=10,
            spider_catalog=None,
        )


def _record(
    index: int,
    *,
    source: str = "test-source",
    labels: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": f"record-{index}",
        "source_id": source,
        "schema": "items(id INTEGER, score INTEGER);",
        "sql": f"SELECT score FROM items WHERE score >= {index}",
        "cfg_labels": labels or ["select-basic", "where", "where-comp"],
    }


def test_selector_honors_explicit_minimum_record_count(monkeypatch):
    selector = _load_selector_module()
    monkeypatch.setattr(selector, "_identity_passes", lambda item: True)

    selected, report = selector.select_records(
        [_record(index) for index in range(6)],
        max_sql_length=800,
        max_records=6,
        minimum_records=4,
        minimum_mutations=1,
        seed=7,
        preflight_identities=True,
    )

    assert len(selected) == 4
    assert report["selected_records"] == 4
    assert report["minimum_records"] == 4


def test_selector_rejects_corpus_below_minimum_record_count(monkeypatch):
    selector = _load_selector_module()
    monkeypatch.setattr(selector, "_identity_passes", lambda item: True)

    try:
        selector.select_records(
            [_record(index) for index in range(3)],
            max_sql_length=800,
            max_records=5,
            minimum_records=4,
            minimum_mutations=1,
            seed=7,
            preflight_identities=True,
        )
    except RuntimeError as exc:
        assert "record count 3" in str(exc)
    else:
        raise AssertionError("selector should reject an undersized corpus")


def test_selector_excludes_explicit_reference_only_schema(monkeypatch):
    selector = _load_selector_module()
    monkeypatch.setattr(selector, "_identity_passes", lambda item: True)
    records = [
        _record(index)
        for index in range(4)
    ] + [{**_record(99), "replay_eligible": False}]

    selected, report = selector.select_records(
        records,
        max_sql_length=800,
        max_records=4,
        minimum_records=4,
        minimum_mutations=1,
        seed=7,
    )

    assert len(selected) == 4
    assert report["excluded_counts"]["reference_only_schema"] == 1


def test_generic_multi_table_schema_deduplicates_unqualified_columns():
    selector = _load_selector_module()
    item = {
        **_record(1),
        "extraction_method": "generic_recursive",
        "sql": (
            "SELECT e.name, d.name, e.salary FROM employee e "
            "JOIN department d ON e.department_id = d.id"
        ),
        "schema": "employee(name, salary, department_id, id); department(name, salary, id, department_id);",
    }

    normalized = selector._normalize_generic_schema(item)
    schema = selector.parse_schema_text(normalized["schema"])

    assert schema["employee"].count("salary") == 1
    assert schema["department"].count("salary") == 0


def test_selector_prioritizes_source_and_label_coverage(monkeypatch):
    selector = _load_selector_module()
    monkeypatch.setattr(selector, "_identity_passes", lambda item: True)
    records = [
        *[_record(index, source="common") for index in range(6)],
        _record(20, source="rare", labels=["select-basic", "cte"]),
        _record(21, source="rare", labels=["select-basic", "window-agg"]),
    ]

    selected, report = selector.select_records(
        records,
        max_sql_length=800,
        max_records=5,
        minimum_records=5,
        minimum_mutations=1,
        seed=3,
        preflight_identities=True,
        coverage_per_source=2,
        coverage_per_label=1,
    )

    assert len(selected) == 5
    assert report["source_counts"]["common"] >= 2
    assert report["source_counts"]["rare"] == 2
    assert report["label_counts"]["cte"] >= 1
    assert report["label_counts"]["window-agg"] >= 1
    assert report["source_coverage_shortfalls"] == {}
    assert report["label_coverage_shortfalls"] == {}


def test_generic_web_sql_uses_auto_dialect_resolution():
    selector = _load_selector_module()

    assert selector._web_sql_dialect({"dialect": "generic"}) is None
    assert selector._web_sql_dialect({}) is None
    assert selector._web_sql_dialect({"dialect": "ansi"}) == "standard"
    assert selector._web_sql_dialect({"dialect": "postgresql"}) == "postgres"


def test_web_mutations_ignore_literals_and_keep_nested_queries_valid():
    selector = _load_selector_module()

    literal_mutations = selector._web_mutations(
        "SELECT name FROM teams WHERE note = 'ASC UNION LIMIT 1'",
        "teams(name, note);",
    )
    literal_names = {name for name, _, _ in literal_mutations}
    assert "order_asc_to_desc" not in literal_names
    assert "union_to_union_all" not in literal_names
    assert "limit_plus_one" not in literal_names

    sql = (
        "SELECT employee_id, salary - (SELECT AVG(salary) FROM employees) AS delta "
        "FROM employees"
    )
    projection = next(
        mutated
        for name, mutated, _ in selector._web_mutations(
            sql,
            "employees(employee_id, salary, department_id);",
        )
        if name == "projection_to_star"
    )
    assert sqlglot.parse_one(projection) is not None

    nested_except = (
        "SELECT product_id FROM product WHERE product_id IN ("
        "SELECT product_id FROM sales EXCEPT SELECT product_id FROM returns)"
    )
    removed = next(
        mutated
        for name, mutated, _ in selector._web_mutations(
            nested_except,
            "product(product_id); sales(product_id); returns(product_id);",
        )
        if name == "except_removed"
    )
    assert sqlglot.parse_one(removed) is not None


def test_generic_top_query_uses_tsql_for_structure_preflight():
    selector = _load_selector_module()
    sql = "SELECT TOP 1 person_name FROM queue ORDER BY turn DESC"
    case = selector._case(
        "top-preflight",
        "WEB_CORPUS_PREFLIGHT",
        "equivalent",
        "queue(person_name, turn);",
        sql,
        sql,
        [],
        max_rows_per_table=4,
        sql_dialect=None,
    )

    result = selector.run_case(case)

    assert result["strict_standard_parse_ok"] is True
    assert result["standard_ir_build_ok"] is True


def test_identity_attribution_uses_the_declared_postgres_dialect():
    selector = _load_selector_module()
    sql = "SELECT TIMESTAMP '2012-08-31 01:00:00'"
    case = selector._case(
        "postgres-identity-attribution",
        "WEB_CORPUS_PREFLIGHT",
        "equivalent",
        "",
        sql,
        sql,
        [],
        max_rows_per_table=4,
        sql_dialect="postgres",
    )

    result = selector.run_case(case)

    assert result["data_stage_met"] is True
    assert result["attribution_stage_met"] is True
    assert result["top_attributions"] == []


def test_capability_reporting_does_not_count_undecided_equivalence_as_supported():
    selector = _load_selector_module()
    case = selector._case(
        "finite-cardinality-undecided",
        "WEB_CORPUS_PREFLIGHT",
        "equivalent",
        "course(id);",
        "SELECT id FROM course LIMIT 100",
        "SELECT id FROM course",
        [],
        max_rows_per_table=4,
    )

    result = selector.run_case(case)

    assert result["verdict_status"] == "SEMANTIC_BOUNDARY"
    assert result["equivalence_conclusion"] == "UNDECIDED"
    assert result["data_stage_met"] is False
    assert result["expectation_met"] is False
    assert result["capability_bucket"] == "semantic_boundary"


def test_web_mutations_skip_semantically_redundant_changes():
    selector = _load_selector_module()

    projection_names = {
        name
        for name, _, _ in selector._web_mutations(
            "SELECT first_name, last_name FROM employees",
            "employees(first_name, last_name);",
        )
    }
    assert "projection_to_star" not in projection_names

    reordered_full_projection_names = {
        name
        for name, _, _ in selector._web_mutations(
            "SELECT first_name, last_name, salary, department_id FROM employees",
            "employees(department_id, first_name, last_name, salary);",
        )
    }
    assert "projection_to_star" not in reordered_full_projection_names

    grouped_projection_names = {
        name
        for name, _, _ in selector._web_mutations(
            "SELECT product_id, SUM(quantity) FROM sales GROUP BY product_id",
            "sales(product_id, quantity);",
        )
    }
    assert "projection_to_star" not in grouped_projection_names

    in_distinct_names = {
        name
        for name, _, _ in selector._web_mutations(
            "SELECT name FROM departments WHERE id IN "
            "(SELECT DISTINCT department_id FROM employees)",
            "departments(id, name); employees(department_id);",
        )
    }
    assert "distinct_removed" not in in_distinct_names

    literal_union_names = {
        name
        for name, _, _ in selector._web_mutations(
            "SELECT 'desktop' AS platform UNION SELECT 'mobile'",
            "",
        )
    }
    assert "union_to_union_all" not in literal_union_names

    nested_limit_names = {
        name
        for name, _, _ in selector._web_mutations(
            "SELECT name FROM candidate WHERE id = "
            "(SELECT TOP 1 candidate_id FROM vote ORDER BY score DESC)",
            "candidate(id, name); vote(candidate_id, score);",
        )
    }
    assert "limit_plus_one" not in nested_limit_names


def test_web_mutations_skip_finite_single_parent_recursive_union_modifiers():
    selector = _load_selector_module()
    schema = (
        "members(memid INT PRIMARY KEY, firstname TEXT, surname TEXT, "
        "recommendedby INT);"
    )
    ancestor_paths = (
        "WITH RECURSIVE recommenders(recommender, member) AS ("
        "SELECT recommendedby, memid FROM members "
        "UNION ALL "
        "SELECT mems.recommendedby, recs.member FROM recommenders recs "
        "JOIN members mems ON mems.memid = recs.recommender) "
        "SELECT member, recommender FROM recommenders"
    )
    descendant_paths = (
        "WITH RECURSIVE recommendeds(memid) AS ("
        "SELECT memid FROM members WHERE recommendedby = 1 "
        "UNION ALL "
        "SELECT mems.memid FROM recommendeds recs "
        "JOIN members mems ON mems.recommendedby = recs.memid) "
        "SELECT memid FROM recommendeds"
    )

    for sql in (ancestor_paths, descendant_paths):
        names = {name for name, _, _ in selector._web_mutations(sql, schema)}
        assert "union_all_to_union" not in names


def test_web_mutations_keep_observable_recursive_union_attacks():
    selector = _load_selector_module()
    graph_sql = (
        "WITH RECURSIVE reachable(node) AS ("
        "SELECT 1 "
        "UNION ALL "
        "SELECT edges.target FROM reachable r "
        "JOIN edges ON edges.source = r.node) "
        "SELECT node FROM reachable"
    )
    overlapping_anchor_sql = (
        "WITH RECURSIVE descendants(memid) AS ("
        "SELECT memid FROM members "
        "UNION ALL "
        "SELECT child.memid FROM descendants parent "
        "JOIN members child ON child.recommendedby = parent.memid) "
        "SELECT memid FROM descendants"
    )

    graph_names = {
        name for name, _, _ in selector._web_mutations(
            graph_sql,
            "edges(edgeid INT PRIMARY KEY, source INT, target INT);",
        )
    }
    overlapping_names = {
        name for name, _, _ in selector._web_mutations(
            overlapping_anchor_sql,
            "members(memid INT PRIMARY KEY, recommendedby INT);",
        )
    }

    assert "union_all_to_union" in graph_names
    assert "union_all_to_union" in overlapping_names


def test_digit_leading_schema_identifier_is_rendered_with_dialect_quotes():
    selector = _load_selector_module()

    sql = selector._quote_unsafe_schema_identifiers(
        "SELECT 4th_place FROM results WHERE 3rd_place = '3rd_place'",
        "results(year, 3rd_place, 4th_place);",
    )
    parsed = selector._parsed_web_query(
        sql
    )

    assert parsed is not None
    root, dialect = parsed
    assert "`4th_place`" in root.sql(dialect=dialect)
    assert "`3rd_place`" in root.sql(dialect=dialect)
    assert "'3rd_place'" in root.sql(dialect=dialect)


def test_reserved_schema_identifier_is_quoted_before_web_parse():
    selector = _load_selector_module()
    sql = selector._quote_unsafe_schema_identifiers(
        "SELECT drawn FROM standings WHERE for = 41",
        "standings(drawn, for);",
    )

    assert "`for`" in sql
    assert selector._parsed_web_query(sql) is not None


def test_comparison_mutations_use_clause_specific_labels():
    selector = _load_selector_module()

    join_mutation = next(
        item
        for item in selector._web_mutations(
            "SELECT a.id FROM a JOIN b ON a.id >= b.id",
            "a(id); b(id);",
        )
        if item[0] == "gte_to_gt"
    )
    having_mutation = next(
        item
        for item in selector._web_mutations(
            "SELECT department_id FROM employees GROUP BY department_id "
            "HAVING COUNT(*) >= 3",
            "employees(department_id);",
        )
        if item[0] == "gte_to_gt"
    )

    assert join_mutation[2] == ["join-on"]
    assert having_mutation[2] == ["having"]


def test_predicate_mutations_use_join_and_case_context_labels():
    selector = _load_selector_module()

    join_in = next(
        item
        for item in selector._web_mutations(
            "SELECT e.id FROM employees e JOIN departments d "
            "ON e.department_id = d.id AND d.id IN (40, 80)",
            "employees(id, department_id); departments(id);",
        )
        if item[0] == "in_to_not_in"
    )
    case_null = next(
        item
        for item in selector._web_mutations(
            "SELECT CASE WHEN parent_id IS NULL THEN 'root' ELSE 'leaf' END FROM tree",
            "tree(parent_id);",
        )
        if item[0] == "is_null_to_not_null"
    )

    assert join_in[2] == ["join-on", "in-list"]
    assert case_null[2] == ["case", "null-handling"]


def test_generic_schema_normalization_removes_derived_alias_columns():
    selector = _load_selector_module()
    item = {
        "id": "window-query",
        "source_id": "local",
        "source_kind": "local_external_seed",
        "extraction_method": "generic_recursive",
        "dialect": "generic",
        "sql": (
            "WITH ranked AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS r "
            "FROM stadium WHERE people >= 100) "
            "SELECT id, visit_date FROM ranked WHERE r >= 3"
        ),
        "schema": "stadium(row_number, id, r, people, visit_date, ranked);",
        "cfg_labels": ["cte", "window-row-number"],
    }

    normalized = selector._normalize_generic_schema(item)

    assert normalized["schema"] == "stadium(id, people, visit_date)"
    assert normalized["source_schema"] == item["schema"]


def test_generic_schema_normalization_deduplicates_case_and_function_tokens():
    selector = _load_selector_module()
    item = {
        "id": "mixed-case-query",
        "source_id": "local",
        "source_kind": "local_external_seed",
        "extraction_method": "generic_recursive",
        "dialect": "generic",
        "sql": (
            "SELECT CustomerID, SUM(TotalAmount) FROM Orders "
            "WHERE OrderDate >= '2024-01-01' GROUP BY CustomerID"
        ),
        "schema": "orders(customerid, totalamount, orderdate);",
        "cfg_labels": ["group-by", "where-comp"],
    }

    normalized = selector._normalize_generic_schema(item)

    assert normalized["schema"] == "Orders(CustomerID, OrderDate, TotalAmount)"

    dateadd_item = {
        **item,
        "id": "dateadd-query",
        "sql": (
            "SELECT id FROM (SELECT *, LAG(temperature) OVER (ORDER BY recorddate) "
            "AS prev_temp, LAG(recorddate) OVER (ORDER BY recorddate) prev_date "
            "FROM weather) tb1 WHERE DATEADD(day, 1, prev_date) = recorddate "
            "AND prev_temp < temperature"
        ),
        "schema": (
            "weather(id, lag, temperature, recorddate, prev_temp, prev_date, "
            "tb1, dateadd, day);"
        ),
    }

    normalized_dateadd = selector._normalize_generic_schema(dateadd_item)

    assert normalized_dateadd["schema"] == "weather(recorddate, temperature, id)"

    alias_collision = {
        **item,
        "id": "qualified-alias-collision",
        "sql": (
            "SELECT e1.id, e1.month, SUM(e2.salary) AS salary FROM Employee e1 "
            "JOIN Employee e2 ON e1.id = e2.id AND e1.month <= e2.month + 2 "
            "GROUP BY e1.id, e1.month"
        ),
        "schema": "employee(e1, id, month, sum, e2, salary);",
    }

    normalized_collision = selector._normalize_generic_schema(alias_collision)

    assert normalized_collision["schema"] == "Employee(id, month, salary)"

    cte_tables = {
        **item,
        "id": "cte-token-schema",
        "sql": (
            "WITH tb1 AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS r "
            "FROM stadium WHERE people >= 100), tb2 AS (SELECT id, visit_date, "
            "people, COUNT(*) OVER (PARTITION BY id - r) AS num FROM tb1) "
            "SELECT id, visit_date, people FROM tb2 WHERE num >= 3"
        ),
        "schema": (
            "stadium(row_number, id, r, people, visit_date, count, num); "
            "tb1(row_number, id, r, people, visit_date, count, num); "
            "tb2(row_number, id, r, people, visit_date, count, num);"
        ),
    }

    normalized_cte = selector._normalize_generic_schema(cte_tables)

    assert normalized_cte["schema"] == "stadium(id, people, visit_date)"
