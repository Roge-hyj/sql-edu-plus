from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "data_construct_test/scripts/phase1_gold_oracle.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase1_gold_oracle", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gold_oracle_finds_comparison_boundary_without_witness_generator():
    module = _load_module()
    result = module.run_gold_oracle(
        "users(id INT PRIMARY KEY, salary INT)",
        "SELECT id FROM users WHERE salary > 3",
        "SELECT id FROM users WHERE salary >= 3",
        seeds=(0,),
        row_scales=(8,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    assert result["equivalence_conclusion"] == module.NOT_EQUIVALENT
    # Each seed/scale is replayed as four worlds (varied vs duplicated rows,
    # sliding vs aligned literal layout), so the id carries a flavour suffix.
    assert result["distinguishing_world_id"].startswith("gold_0_8_")
    assert result["trials"][0]["same_result"] is False
    assert len(result["trials"][0]["database"]["users"]) == 8


def test_gold_oracle_does_not_promote_finite_match_to_equivalent():
    module = _load_module()
    result = module.run_gold_oracle(
        "users(id INT PRIMARY KEY)",
        "SELECT id FROM users",
        "SELECT id FROM users",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.UNDECIDED
    assert result["equivalence_conclusion"] == module.UNDECIDED


def test_gold_oracle_trusted_equivalent_label_is_explicit():
    module = _load_module()
    result = module.run_gold_oracle(
        "users(id INT PRIMARY KEY)",
        "SELECT id FROM users",
        "SELECT id FROM users",
        expected="equivalent",
        seeds=(0, 1),
        row_scales=(4,),
    )

    assert result["verdict"] == module.EQUIVALENT
    assert result["status"] == "SUPPORTED"
    # Two seeds x one scale x four world flavours.
    assert len(result["trials"]) == 8
    assert {trial["row_flavour"] for trial in result["trials"]} == {"varied", "duplicated"}
    assert {trial["literal_layout"] for trial in result["trials"]} == {"sliding", "aligned"}


def test_gold_oracle_separates_input_and_engine_gaps():
    module = _load_module()
    missing_schema = module.run_gold_oracle(
        None,
        "SELECT id FROM users",
        "SELECT id FROM users",
        seeds=(0,),
        row_scales=(4,),
    )
    mysql = module.run_gold_oracle(
        "users(id INT)",
        "SELECT id FROM users",
        "SELECT id FROM users",
        dialect="mysql",
        seeds=(0,),
        row_scales=(4,),
    )

    assert missing_schema["status"] == module.INPUT_GAP
    assert missing_schema["equivalence_conclusion"] == module.UNDECIDED
    expected_mysql_status = (
        module.UNDECIDED
        if module._native_dialect_url("mysql")
        else module.ENGINE_GAP
    )
    assert mysql["status"] == expected_mysql_status
    assert mysql["equivalence_conclusion"] == module.UNDECIDED


def test_gold_oracle_keeps_mysql_schema_name_resolution_out_of_engine_gap(monkeypatch):
    module = _load_module()

    class MissingTableError(Exception):
        __module__ = "pymysql.err"

        def __init__(self):
            super().__init__(1146, "Table does not exist")

    error = MissingTableError()
    assert module._native_schema_resolution_kind(error, "mysql") == "mysql.table_not_found"

    class FakeRunner:
        def execute(self, *_args, **_kwargs):
            raise error

        def close(self):
            return None

    @contextmanager
    def fake_native_runner(*_args, **_kwargs):
        yield FakeRunner()

    monkeypatch.setattr(module, "_native_dialect_url", lambda _dialect: "mysql://test")
    monkeypatch.setattr(module, "_native_runner", fake_native_runner)

    result = module.run_gold_oracle(
        "Products(id INT)",
        "SELECT id FROM products",
        "SELECT DISTINCT id FROM products",
        dialect="mysql",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.INPUT_GAP
    assert result["status"] == module.INPUT_GAP
    assert result["reason"] == (
        "standard query cannot resolve replayed schema object: mysql.table_not_found"
    )


def test_gold_oracle_treats_student_only_mysql_missing_table_as_non_equivalent(monkeypatch):
    module = _load_module()

    class MissingTableError(Exception):
        __module__ = "pymysql.err"

        def __init__(self):
            super().__init__(1146, "Table does not exist")

    error = MissingTableError()

    class FakeRunner:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return ["id"], [(1,)]
            raise error

        def close(self):
            return None

    @contextmanager
    def fake_native_runner(*_args, **_kwargs):
        yield FakeRunner()

    monkeypatch.setattr(module, "_native_dialect_url", lambda _dialect: "mysql://test")
    monkeypatch.setattr(module, "_native_runner", fake_native_runner)

    result = module.run_gold_oracle(
        "Products(id INT)",
        "SELECT id FROM Products",
        "SELECT id FROM products",
        dialect="mysql",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    assert result["status"] == "SUPPORTED"
    assert result["distinguishing_world_id"].endswith("student_schema_resolution")


def test_gold_oracle_classifies_duplicate_schema_columns_as_input_gap():
    module = _load_module()
    result = module.run_gold_oracle(
        "shows(country, country, channel)",
        "SELECT channel FROM shows WHERE country = 'NZ'",
        "SELECT channel FROM shows WHERE country IN ('NZ')",
        expected="equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.INPUT_GAP
    assert result["status"] == module.INPUT_GAP
    assert "duplicate column name" in result["reason"].lower()


def test_gold_oracle_rejects_side_effecting_statements_as_engine_gap():
    module = _load_module()
    result = module.run_gold_oracle(
        "users(id INT)",
        "SELECT id FROM users",
        "DELETE FROM users",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["status"] == module.ENGINE_GAP
    assert result["equivalence_conclusion"] == module.UNDECIDED


def test_gold_oracle_preserves_authoritative_catalog_constraints():
    module = _load_module()
    result = module.run_gold_oracle(
        None,
        "SELECT p.id FROM parent p JOIN child c ON p.id = c.parent_id",
        "SELECT p.id FROM parent p JOIN child c ON p.id = c.id",
        schema_catalog={
            "db_id": "fixture",
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "data_type": "NUMBER", "is_primary_key": True}],
                    "primary_key": ["id"],
                },
                {
                    "name": "child",
                    "columns": [
                        {"name": "id", "data_type": "NUMBER", "is_primary_key": True},
                        {"name": "parent_id", "data_type": "NUMBER"},
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {"column": "parent_id", "references_table": "parent", "references_column": "id"}
                    ],
                },
            ],
        },
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    assert result["trials"][0]["database"]["parent"]
    assert result["trials"][0]["database"]["child"]


def test_gold_oracle_covers_simple_searched_case_and_derived_labels():
    module = _load_module()
    result = module.run_gold_oracle(
        "takes(id, grade)",
        "SELECT CASE grade WHEN 'A' THEN 'pass' ELSE 'other' END FROM takes",
        "SELECT CASE WHEN grade = 'A' THEN 'pass' ELSE 'other' END FROM takes",
        expected="equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.EQUIVALENT
    assert result["trials"][0]["standard_rows"] == result["trials"][0]["student_rows"]


def test_gold_oracle_covers_rank_ties():
    module = _load_module()
    result = module.run_gold_oracle(
        "instructor(id, name, dept, salary)",
        "SELECT name, RANK() OVER (PARTITION BY dept ORDER BY salary DESC) AS value FROM instructor",
        "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS value FROM instructor",
        expected="not_equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    assert result["trials"][0]["same_result"] is False


def test_gold_oracle_accepts_constant_query_without_schema():
    module = _load_module()
    result = module.run_gold_oracle(
        None,
        "SELECT 100.0",
        "SELECT 1e2",
        expected="equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.EQUIVALENT


def test_gold_oracle_materializes_like_and_order_boundaries():
    module = _load_module()
    like_result = module.run_gold_oracle(
        "course(id, title, credits)",
        "SELECT title FROM course WHERE title LIKE 'Data%'",
        "SELECT title FROM course WHERE title LIKE '%Data'",
        seeds=(0,),
        row_scales=(4,),
    )
    order_result = module.run_gold_oracle(
        "course(id, title, credits)",
        "SELECT title, credits FROM course ORDER BY credits NULLS FIRST",
        "SELECT title, credits FROM course ORDER BY credits NULLS LAST",
        seeds=(0,),
        row_scales=(4,),
    )

    assert like_result["verdict"] == module.NOT_EQUIVALENT
    assert order_result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_materializes_subquery_membership_witness():
    module = _load_module()
    result = module.run_gold_oracle(
        "parent(id INT PRIMARY KEY); lookup(id INT PRIMARY KEY)",
        "SELECT id FROM parent WHERE id IN (SELECT id FROM lookup)",
        "SELECT id FROM parent WHERE EXISTS (SELECT id FROM lookup)",
        expected="not_equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    trial = result["trials"][0]
    assert trial["database"]["parent"][0]["id"] != trial["database"]["lookup"][0]["id"]


def test_gold_oracle_materializes_filtered_count_null_path():
    module = _load_module()
    result = module.run_gold_oracle(
        "shows(country, channel)",
        "SELECT COUNT(channel) FROM shows WHERE country = 'New Zealand'",
        "SELECT COUNT(*) FROM shows WHERE country = 'New Zealand'",
        expected="not_equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    trial = result["trials"][0]
    rows = trial["database"]["shows"]
    matching = [row for row in rows if row["country"] == "New Zealand"]
    assert len(matching) >= 2
    assert any(row["channel"] is None for row in matching)
    assert any(row["channel"] is not None for row in matching)
    assert trial["standard_rows"] != trial["student_rows"]


def test_gold_oracle_materializes_count_null_path_for_unicode_identifiers():
    module = _load_module()
    result = module.run_gold_oracle(
        "tracks(track, rōmaji_title)",
        "SELECT COUNT(track) FROM tracks WHERE rōmaji_title = 'Mō Sukoshi Tōku'",
        "SELECT COUNT(*) FROM tracks WHERE rōmaji_title = 'Mō Sukoshi Tōku'",
        expected="not_equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT
    trial = result["trials"][0]
    matching = [
        row for row in trial["database"]["tracks"] if row["rōmaji_title"] == "Mō Sukoshi Tōku"
    ]
    assert len(matching) >= 2
    assert any(row["track"] is None for row in matching)
    assert trial["standard_rows"] != trial["student_rows"]


def test_gold_oracle_does_not_invent_count_null_for_not_null_column():
    module = _load_module()
    result = module.run_gold_oracle(
        "shows(country TEXT, channel TEXT NOT NULL)",
        "SELECT COUNT(channel) FROM shows WHERE country = 'New Zealand'",
        "SELECT COUNT(*) FROM shows WHERE country = 'New Zealand'",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.UNDECIDED
    assert all(
        all(row["channel"] is not None for row in trial["database"]["shows"])
        for trial in result["trials"]
    )


def test_gold_oracle_materializes_having_boundary():
    module = _load_module()
    result = module.run_gold_oracle(
        "instructor(id, dept, salary)",
        "SELECT dept FROM instructor GROUP BY dept HAVING SUM(salary) > 50000",
        "SELECT dept FROM instructor GROUP BY dept HAVING SUM(salary) >= 50000",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_materializes_unary_function_sign_boundary():
    module = _load_module()
    result = module.run_gold_oracle(
        "sales(id, amount)",
        "SELECT ABS(amount) FROM sales",
        "SELECT amount FROM sales",
        expected="not_equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_accepts_provenance_comments_and_unicode_columns():
    module = _load_module()
    tables = module.parse_schema(
        "-- spider_db_id: college_2\ninstructor(name, dept_name, η, 2006);"
    )
    assert [table.name for table in tables] == ["instructor"]
    assert [column.name for column in tables[0].columns] == ["name", "dept_name", "η", "2006"]


def test_postgres_fixture_schema_rewrite_is_bounded_to_declared_tables():
    module = _load_module()
    sql = (
        "SELECT b.bookid, 'cd.bookings' AS literal "
        "FROM cd.bookings AS b JOIN \"cd\".\"members\" AS m ON b.memid = m.memid "
        "WHERE b.note = $$cd.bookings$$ -- cd.bookings\n"
        "AND b.facilities IS NULL"
    )

    rewritten = module._rewrite_postgres_fixture_schemas(
        sql,
        {"bookings", "members", "facilities"},
        "oracle_world_123",
    )

    assert 'FROM "oracle_world_123".bookings AS b' in rewritten
    assert 'JOIN "oracle_world_123"."members" AS m' in rewritten
    assert "'cd.bookings' AS literal" in rewritten
    assert "$$cd.bookings$$" in rewritten
    assert "-- cd.bookings" in rewritten
    # ``b`` is a query alias, not a source schema.  A column whose name happens
    # to match another fixture table must not be rewritten.
    assert "b.facilities IS NULL" in rewritten


def test_postgres_fixture_schema_rewrite_requires_from_or_join_evidence():
    module = _load_module()
    sql = "SELECT cd.bookings FROM metrics AS cd"

    assert module._rewrite_postgres_fixture_schemas(
        sql,
        {"bookings", "metrics"},
        "oracle_world_123",
    ) == sql


def test_native_text_width_fitting_preserves_unique_generated_values():
    module = _load_module()
    column = module.ColumnDef(
        name="corporate_number",
        declared_type="VARCHAR(13)",
        primary_key=True,
        unique=True,
    )

    first = module._fit_native_value(column, "corporate_number_0")
    second = module._fit_native_value(column, "corporate_number_1")

    assert len(first) <= 13
    assert len(second) <= 13
    assert first != second


def test_native_text_width_fitting_keeps_nonunique_prefix_semantics():
    module = _load_module()
    column = module.ColumnDef(name="label", declared_type="VARCHAR(5)")

    assert module._fit_native_value(column, "alphabet") == "alpha"


def test_postgres_fixture_ddl_is_connection_local_temp_table():
    module = _load_module()

    assert module._postgres_temp_table_ddl(
        'CREATE TABLE "bookings" ("bookid" INT PRIMARY KEY)'
    ) == 'CREATE TEMP TABLE "bookings" ("bookid" INT PRIMARY KEY)'


def test_gold_oracle_seeds_query_literals_so_string_predicates_match():
    module = _load_module()
    # Random text never equals 'clay', so without literal seeding both queries
    # return nothing and an AND/OR mutation would look equivalent.
    result = module.run_gold_oracle(
        "matches(surface, championship, score)",
        "SELECT score FROM matches WHERE surface = 'clay' AND championship = 'linz'",
        "SELECT score FROM matches WHERE surface = 'clay' OR championship = 'linz'",
        seeds=(0,),
        row_scales=(4,),
    )
    assert result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_seeds_literals_for_numeric_leading_unicode_columns():
    module = _load_module()
    result = module.run_gold_oracle(
        "matches(tournament, 2007, 2009)",
        "SELECT tournament FROM matches WHERE 2007 = '1r' AND 2009 = '1r'",
        "SELECT tournament FROM matches WHERE 2007 = '1r' OR 2009 = '1r'",
        expected="not_equivalent",
        seeds=(0,),
        row_scales=(4,),
    )

    assert result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_finds_aggregate_witness_behind_a_string_filter():
    module = _load_module()
    result = module.run_gold_oracle(
        "stats(player, minutes)",
        "SELECT MIN(minutes) FROM stats WHERE player = 'Sue Bird'",
        "SELECT MAX(minutes) FROM stats WHERE player = 'Sue Bird'",
        seeds=(0,),
        row_scales=(4,),
    )
    assert result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_builds_a_duplicate_world_for_distinct_mutations():
    module = _load_module()
    result = module.run_gold_oracle(
        "plays(company, play)",
        "SELECT play FROM plays WHERE company = 'radu'",
        "SELECT DISTINCT play FROM plays WHERE company = 'radu'",
        seeds=(0,),
        row_scales=(4,),
    )
    assert result["verdict"] == module.NOT_EQUIVALENT
    assert result["distinguishing_world_id"].endswith("duplicated_aligned") or result[
        "distinguishing_world_id"
    ].endswith("duplicated_sliding")


def test_gold_oracle_creates_an_unmatched_row_for_outer_join_mutations():
    module = _load_module()
    result = module.run_gold_oracle(
        "club(clubid, clubname); member_of_club(clubid, stuid)",
        "SELECT COUNT(*) FROM club AS t1 JOIN member_of_club AS t2 ON t1.clubid = t2.clubid",
        "SELECT COUNT(*) FROM club AS t1 LEFT JOIN member_of_club AS t2 ON t1.clubid = t2.clubid",
        seeds=(0,),
        row_scales=(4,),
    )
    assert result["verdict"] == module.NOT_EQUIVALENT


def test_gold_oracle_keeps_a_true_equivalence_equivalent_across_every_world():
    module = _load_module()
    result = module.run_gold_oracle(
        "t(a, b)",
        "SELECT a FROM t WHERE a = 1",
        "SELECT a FROM t WHERE a IN (1)",
        expected="equivalent",
        seeds=(0, 1),
        row_scales=(4, 8),
    )
    assert result["verdict"] == module.EQUIVALENT
    assert all(trial["same_result"] for trial in result["trials"])
