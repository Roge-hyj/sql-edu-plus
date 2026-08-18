from __future__ import annotations

from collections import Counter
import pickle

import pytest
from sqlglot import parse_one

from core.witness_generation.obligations import compile_obligations, stable_diff_id
from core.witness_generation.schema_scope import (
    SchemaCatalog,
    analyze_schema_qualification,
    extract_physical_table_names,
)
from core.witness_generation.planner import (
    CellConstraint,
    ConstraintLedger,
    WitnessPlanner,
    apply_cell_constraints,
    summarize_write_audit,
    track_database_rows,
    write_owner,
    WitnessSuite,
    WitnessWorld,
)
from core.ast_schema import ASTDiffNode
from core.parseval_data_generator import (
    SandboxRun,
    _attach_witness_evidence,
    extract_ast_diffs,
    generate_test_database,
    _link_mutation_diff_ids,
)
from core.parseval_data_generator import generate_and_compare


def test_schema_qualification_separates_cte_and_derived_relations_from_physical_tables():
    sql = (
        "WITH active AS (SELECT id, dept_id FROM employee WHERE enabled = 1) "
        "SELECT a.id FROM active a "
        "JOIN (SELECT id FROM department) d ON a.dept_id = d.id"
    )
    schema = {
        "employee": ["id", "dept_id", "enabled"],
        "department": ["id"],
    }

    qualification = analyze_schema_qualification(sql, schema)

    assert qualification.executable is True
    assert qualification.physical_tables == {"employee", "department"}
    assert "active" not in qualification.physical_tables
    assert "d" not in qualification.physical_tables
    assert qualification.missing_tables == set()
    assert qualification.missing_columns == set()


def test_schema_catalog_preserves_types_nullability_and_explicit_constraints():
    catalog = SchemaCatalog.from_legacy(
        {
            "department": ["id", "name"],
            "employee": ["id", "department_id", "note"],
        },
        {
            "department": {"id": "BIGINT PRIMARY KEY", "name": "TEXT NOT NULL"},
            "employee": {
                "id": "BIGINT PRIMARY KEY",
                "department_id": "BIGINT REFERENCES department(id)",
                "note": "TEXT",
            },
        },
    )

    employee = catalog.table("employee")
    assert employee is not None
    assert employee.primary_key == ("id",)
    assert employee.columns["note"].nullable is True
    assert employee.columns["department_id"].data_type == "BIGINT"
    assert employee.foreign_keys[0].references_table == "department"
    assert employee.foreign_keys[0].references_columns == ("id",)


def test_schema_catalog_restores_normalized_spider_keys_without_guessing():
    catalog = SchemaCatalog.from_dict({
        "source": "spider_tables_json",
        "db_id": "department_management",
        "tables": [
            {
                "name": "head",
                "columns": [
                    {"name": "head_ID", "data_type": "BIGINT", "nullable": None, "is_primary_key": True},
                    {"name": "name", "data_type": "TEXT", "nullable": None, "is_primary_key": False},
                ],
                "primary_key": ["head_ID"],
                "foreign_keys": [],
                "unique_constraints": [["head_ID"]],
            },
            {
                "name": "management",
                "columns": [
                    {"name": "department_ID", "data_type": "BIGINT", "nullable": None},
                    {"name": "head_ID", "data_type": "BIGINT", "nullable": None},
                ],
                "primary_key": ["department_ID", "head_ID"],
                "foreign_keys": [
                    {
                        "column": "head_ID",
                        "references_table": "head",
                        "references_column": "head_ID",
                    }
                ],
                "unique_constraints": [["department_ID", "head_ID"]],
            },
        ],
    })

    head = catalog.table("HEAD")
    management = catalog.table("management")
    assert catalog.source == "spider_tables_json"
    assert catalog.database_id == "department_management"
    assert head is not None and head.primary_key == ("head_ID",)
    assert head.columns["head_id"].nullable is False
    assert management is not None
    assert management.primary_key == ("department_ID", "head_ID")
    assert management.foreign_keys[0].references_table == "head"
    type_hints = catalog.as_legacy_types()
    assert "PRIMARY KEY" in type_hints["head"]["head_ID"]
    assert "PRIMARY KEY" not in type_hints["management"]["department_ID"]
    assert "REFERENCES head(head_ID)" in type_hints["management"]["head_ID"]


def test_generate_and_compare_uses_authoritative_spider_catalog_columns():
    spider_catalog = {
        "source": "spider_tables_json",
        "db_id": "department_management",
        "tables": [
            {
                "name": "head",
                "columns": [
                    {"name": "head_ID", "data_type": "BIGINT", "nullable": None, "is_primary_key": True},
                    {"name": "name", "data_type": "TEXT", "nullable": None},
                    {"name": "born_state", "data_type": "TEXT", "nullable": None},
                    {"name": "age", "data_type": "BIGINT", "nullable": None},
                ],
                "primary_key": ["head_ID"],
                "foreign_keys": [],
                "unique_constraints": [["head_ID"]],
            }
        ],
    }

    run = generate_and_compare(
        # This intentionally mirrors the old query-scraped Spider fixture,
        # which omitted an output column used by the SQL pair.
        "head(age);",
        "SELECT name FROM head WHERE age > 56",
        "SELECT name FROM head WHERE age >= 56",
        schema_catalog=spider_catalog,
        sql_dialect="sqlite",
        max_rows_per_table=4,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert set(run.test_database["head"][0]) == {"head_ID", "name", "born_state", "age"}
    assert run.data_evidence["schema_catalog"] == {
        "source": "spider_tables_json",
        "database_id": "department_management",
        "physical_table_count": 1,
        "primary_key_count": 1,
        "foreign_key_count": 0,
        "authoritative": True,
    }


def test_cte_aggregate_output_boundary_obligation_resolves_to_physical_column():
    standard = (
        "WITH totals AS (SELECT dept, SUM(amount) AS total FROM sales GROUP BY dept) "
        "SELECT dept FROM totals WHERE total > 100"
    )
    student = standard.replace("total > 100", "total >= 100")
    schema = {"sales": ["id", "dept", "amount"]}
    diffs = extract_ast_diffs(standard, student, dialect="sqlite")
    qualifications = (
        analyze_schema_qualification(standard, schema, dialect="sqlite"),
        analyze_schema_qualification(student, schema, dialect="sqlite"),
    )

    obligations = compile_obligations(
        diffs,
        schema=schema,
        qualifications=qualifications,
    )
    obligation = next(
        item for item in obligations
        if item.diff_type == "comparison_operator_changed"
    )
    constraint = obligation.hard_constraints[0]
    metadata = dict(constraint.metadata)

    assert obligation.required_tables == {"sales"}
    assert {(item.relation, item.column) for item in obligation.required_columns} == {
        ("sales", "amount")
    }
    assert constraint.kind == "aggregate_boundary_group"
    assert constraint.relation == "sales"
    assert constraint.column == "amount"
    assert metadata["derived_relation"] == "totals"
    assert metadata["derived_column"] == "total"
    assert metadata["standard_aggregate_function"] == "SUM"
    assert metadata["standard_aggregate_argument"] == "amount"
    assert metadata["standard_group_columns"] == ("dept",)


def test_cte_aggregate_output_boundary_materializes_and_distinguishes():
    standard = (
        "WITH totals AS (SELECT dept, SUM(amount) AS total FROM sales GROUP BY dept) "
        "SELECT dept FROM totals WHERE total > 100"
    )
    student = standard.replace("total > 100", "total >= 100")

    run = generate_and_compare(
        "sales(id, dept, amount);",
        standard,
        student,
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    evidence = next(
        item for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "aggregate_boundary_group"
    )
    assert evidence["constraints_satisfied"] is True
    assert evidence["distinguished"] is True
    groups = {}
    for row in run.test_database["sales"]:
        groups.setdefault(row["dept"], []).append(row["amount"])
    assert any(sum(values) == 100 for values in groups.values())


def test_cte_count_star_output_boundary_materializes_exact_group_size():
    standard = (
        "WITH totals AS (SELECT dept, COUNT(*) AS cnt FROM sales GROUP BY dept) "
        "SELECT dept FROM totals WHERE cnt > 3"
    )
    student = standard.replace("cnt > 3", "cnt >= 3")

    run = generate_and_compare(
        "sales(id, dept, amount);",
        standard,
        student,
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    evidence = next(
        item for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "aggregate_boundary_group"
    )
    assert evidence["constraints_satisfied"] is True
    assert evidence["distinguished"] is True
    counts = Counter(row["dept"] for row in run.test_database["sales"])
    assert 3 in counts.values()


def test_cte_global_count_star_boundary_uses_whole_table_group():
    standard = (
        "WITH totals AS (SELECT COUNT(*) AS cnt FROM sales) "
        "SELECT cnt FROM totals WHERE cnt > 3"
    )
    student = standard.replace("cnt > 3", "cnt >= 3")

    run = generate_and_compare(
        "sales(id, amount);",
        standard,
        student,
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert len(run.test_database["sales"]) == 3
    evidence = next(
        item for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "aggregate_boundary_group"
    )
    assert evidence["constraints_satisfied"] is True
    assert evidence["distinguished"] is True
    assert evidence["semantic_validation"]["evidence"]["aggregate_values"] == {
        "()": 3
    }


def test_nested_cte_aggregate_boundary_traces_to_physical_source():
    standard = (
        "WITH base AS (SELECT id, dept, amount FROM sales), "
        "totals AS (SELECT dept, SUM(amount) AS total FROM base GROUP BY dept) "
        "SELECT dept FROM totals WHERE total > 100"
    )
    student = standard.replace("total > 100", "total >= 100")

    run = generate_and_compare(
        "sales(id, dept, amount);",
        standard,
        student,
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    evidence = next(
        item for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "aggregate_boundary_group"
    )
    assert evidence["constraints_satisfied"] is True
    assert evidence["distinguished"] is True
    assert evidence["semantic_validation"]["evidence"]["aggregate_argument"] == "amount"


def test_schema_qualification_reports_missing_physical_objects_before_generation():
    qualification = analyze_schema_qualification(
        "SELECT o.id, o.total FROM orders o JOIN customer c ON o.customer_id = c.id",
        {"orders": ["id", "customer_id"]},
    )

    assert qualification.executable is False
    assert qualification.missing_tables == {"customer"}
    assert any(ref.relation == "orders" and ref.column == "total" for ref in qualification.missing_columns)
    assert qualification.boundary_reason == "missing_physical_tables"


def test_schema_qualification_rejects_unknown_qualified_aliases():
    qualification = analyze_schema_qualification(
        "SELECT missing_alias.id FROM orders o",
        {"orders": ["id"]},
    )

    assert qualification.executable is False
    assert any(
        ref.relation == "missing_alias" and ref.column == "id"
        for ref in qualification.missing_columns
    )


def test_schema_qualification_rejects_ambiguous_unqualified_columns():
    qualification = analyze_schema_qualification(
        "SELECT id FROM orders o JOIN customers c ON o.id = c.id",
        {"orders": ["id"], "customers": ["id"]},
    )

    assert qualification.executable is False
    assert any(ref.relation == "" and ref.column == "id" for ref in qualification.missing_columns)


def test_sqlite_schema_qualification_accepts_unresolved_double_quoted_literal():
    qualification = analyze_schema_qualification(
        'SELECT campus FROM campuses WHERE county = "Orange"',
        {"campuses": ["campus", "county"]},
        dialect="sqlite",
    )

    assert qualification.executable is True
    assert qualification.missing_columns == set()
    assert all(
        reference.column != "orange"
        for scope in qualification.scopes
        for reference in scope.referenced_columns
    )


def test_standard_identifier_dialect_still_rejects_unknown_quoted_column():
    qualification = analyze_schema_qualification(
        'SELECT campus FROM campuses WHERE county = "Orange"',
        {"campuses": ["campus", "county"]},
        dialect="postgres",
    )

    assert qualification.executable is False
    assert any(
        reference.column == "orange"
        for reference in qualification.missing_columns
    )


def test_schema_qualification_rejects_multiple_statements_and_dml():
    multiple = analyze_schema_qualification("SELECT id FROM t; SELECT id FROM u", {"t": ["id"], "u": ["id"]})
    update = analyze_schema_qualification("UPDATE t SET id = 2", {"t": ["id"]})

    assert multiple.executable is False
    assert multiple.boundary_reason == "multiple_sql_statements"
    assert update.executable is False
    assert update.boundary_reason == "non_query_statement"


def test_legacy_physical_table_extraction_now_ignores_cte_names():
    sql = "WITH filtered AS (SELECT id FROM employee) SELECT id FROM filtered"

    assert extract_physical_table_names(sql) == {"employee"}


def test_obligation_ids_are_stable_and_compile_semantic_constraints():
    diff = ASTDiffNode(
        clause_category="WHERE",
        diff_type="comparison_operator_changed",
        target_table="employee",
        target_column="salary",
        knowledge_point_id="where-comp",
        extra={"value": 50000, "query_scope": "root"},
    )

    first = stable_diff_id(diff)
    second = stable_diff_id(diff)
    inserted_before = stable_diff_id(diff, 99)
    obligation = compile_obligations([diff])[0]

    assert first == second == inserted_before
    assert obligation.diff_id == first
    assert obligation.required_tables == {"employee"}
    assert obligation.minimum_rows == {"employee": 3}
    assert obligation.hard_constraints[0].kind == "boundary_tristate"
    assert obligation.required_columns.pop().column == "salary"


def test_null_safe_column_comparison_compiles_both_columns_and_four_paths():
    diffs = extract_ast_diffs(
        "SELECT name FROM employee "
        "WHERE manager_id IS NOT DISTINCT FROM backup_id",
        "SELECT name FROM employee WHERE manager_id = backup_id",
        dialect="postgres",
    )
    comparison = next(
        item for item in diffs
        if item.diff_type == "comparison_operator_changed"
    )

    obligation = compile_obligations(
        [comparison],
        schema={"employee": ["name", "manager_id", "backup_id"]},
    )[0]
    declaration = WitnessPlanner().plan([obligation]).worlds[0]

    assert comparison.extra["standard_value_kind"] == "column"
    assert comparison.extra["student_value_kind"] == "column"
    assert comparison.extra["standard_right_column"] == "backup_id"
    assert comparison.extra["student_right_column"] == "backup_id"
    assert {item.column for item in obligation.required_columns} == {
        "manager_id",
        "backup_id",
    }
    assert obligation.minimum_rows == {"employee": 4}
    assert len(declaration.constraints) == 8
    assert {item.row_slot for item in declaration.constraints} == {
        "row_slot_0",
        "row_slot_1",
        "row_slot_2",
        "row_slot_3",
    }


def test_join_and_window_diffs_compile_to_different_world_requirements():
    obligations = compile_obligations(
        [
            ASTDiffNode("JOIN_TYPE", "join_type_changed", target_table="department"),
            ASTDiffNode("WINDOW", "window_function_changed", target_table="employee", target_column="salary"),
        ]
    )

    assert obligations[0].hard_constraints[0].kind == "matched_and_dangling_join_rows"
    assert obligations[0].estimated_cost == 2
    assert obligations[1].hard_constraints[0].kind == "window_partitions_and_ties"
    assert obligations[1].estimated_cost == 3


def test_window_null_placement_obligation_locks_three_order_key_paths():
    standard_sql = (
        "SELECT id, ROW_NUMBER() OVER (ORDER BY seq NULLS FIRST) FROM sales"
    )
    student_sql = (
        "SELECT id, ROW_NUMBER() OVER (ORDER BY seq NULLS LAST) FROM sales"
    )
    obligations = compile_obligations(
        extract_ast_diffs(standard_sql, student_sql),
        schema={"sales": ["id", "seq"]},
    )

    assert len(obligations) == 1
    metadata = dict(obligations[0].hard_constraints[0].metadata)
    assert metadata["standard_window_order_items"] == (("seq", False, True),)
    assert metadata["student_window_order_items"] == (("seq", False, False),)
    world = WitnessPlanner().plan(obligations).worlds[0]
    assert [(item.row_slot, item.column, item.relation, item.value) for item in world.constraints] == [
        ("row_slot_0", "seq", "is_null", None),
        ("row_slot_1", "seq", "equals", 10),
        ("row_slot_2", "seq", "equals", 20),
    ]


def test_distinct_on_diff_compiles_key_and_competing_payload_obligation():
    distinct = parse_one(
        "SELECT DISTINCT ON (dept) dept, name FROM student ORDER BY dept, name",
        read="postgres",
    ).args["distinct"]
    ordinary = parse_one(
        "SELECT DISTINCT dept, name FROM student ORDER BY dept, name",
        read="postgres",
    ).args["distinct"]

    obligation = compile_obligations([
        ASTDiffNode("DISTINCT ON", "distinct_on_changed", standard_node=distinct, student_node=ordinary)
    ], schema={"student": ["dept", "name"]})[0]

    constraint = obligation.hard_constraints[0]
    assert constraint.kind == "distinct_on_competing_payload"
    assert constraint.relation == "student"
    assert constraint.column == "name"
    assert dict(constraint.metadata)["key_columns"] == ("dept",)

    extracted = compile_obligations(
        extract_ast_diffs(
            "SELECT DISTINCT ON (dept) dept, name FROM student ORDER BY dept, name",
            "SELECT DISTINCT dept, name FROM student ORDER BY dept, name",
        ),
        schema={"student": ["dept", "name"]},
    )
    extracted_constraint = next(
        item.hard_constraints[0]
        for item in extracted
        if item.diff_type == "distinct_on_changed"
    )
    assert extracted_constraint.relation == "student"
    assert extracted_constraint.column == "name"


def test_finalization_respects_isolated_world_probe_scope():
    standard = (
        "SELECT o.customerid FROM orders o LEFT JOIN customers c "
        "ON o.customerid = c.customerid GROUP BY o.customerid "
        "HAVING SUM(o.totalamount) > 500"
    )
    student = standard.replace("LEFT JOIN", "JOIN")
    join_diffs = [
        diff
        for diff in extract_ast_diffs(standard, student)
        if diff.diff_type == "join_type_changed"
    ]
    metadata = {}

    database = generate_test_database(
        {
            "orders": ["customerid", "orderdate", "totalamount"],
            "customers": ["customerid"],
        },
        standard,
        student,
        ast_diffs=join_diffs,
        generation_metadata=metadata,
    )

    assert metadata["world_probe_scope"]["join"] is True
    assert metadata["world_probe_scope"]["aggregate"] is False
    amounts = [row["totalamount"] for row in database["orders"]]
    assert amounts != [500] + [0] * (len(amounts) - 1)


def test_locked_cell_constraints_reject_conflicting_writes_without_replacement():
    ledger = ConstraintLedger()
    first = CellConstraint(
        table="employee",
        row_slot="boundary_row",
        column="salary",
        relation="equals",
        value=50000,
        owner="comparison_boundary",
        obligation_id="obligation_a",
    )
    conflicting = CellConstraint(
        table="employee",
        row_slot="boundary_row",
        column="salary",
        relation="equals",
        value=60000,
        owner="another_probe",
        obligation_id="obligation_b",
    )

    assert ledger.add(first) is True
    assert ledger.add(conflicting) is False
    assert ledger.constraints == [first]
    assert ledger.conflicts[0].reason == "locked_cell_incompatible"


def test_having_summary_whitespace_maps_to_atomic_boundary_obligation():
    diffs = [
        ASTDiffNode(
            "HAVING",
            "having_changed",
            target_table="sales",
            knowledge_point_id="having",
            extra={
                "standard_sql": "HAVING\n  SUM(amount)   >= 100",
                "student_sql": "HAVING SUM(amount) > 100",
                "standard_group_columns": ("dept",),
                "standard_source_table": "sales",
            },
        ),
        ASTDiffNode(
            "PREDICATE",
            "comparison_operator_changed",
            target_table="sales",
            target_column="amount",
            knowledge_point_id="having",
            extra={
                "standard_sql": "SUM(amount) >= 100",
                "student_sql": "SUM(amount) > 100",
                "standard_op": ">=",
                "student_op": ">",
                "value": 100,
            },
        ),
    ]

    obligations = compile_obligations(
        diffs,
        schema={"sales": ["dept", "amount"]},
    )

    assert len(obligations) == 1
    assert obligations[0].diff_type == "comparison_operator_changed"
    assert obligations[0].hard_constraints[0].kind == "aggregate_boundary_group"
    assert obligations[0].hard_constraints[0].value == 100


def test_scalar_aggregate_comparison_has_dedicated_obligation_kind():
    standard = (
        "SELECT t2.MakeId FROM cars_data t1 JOIN car_names t2 "
        "ON t1.Id = t2.MakeId WHERE t1.Horsepower > "
        "(SELECT MIN(Horsepower) FROM cars_data) AND t1.Cylinders < 4"
    )
    student = standard.replace("Horsepower >", "Horsepower >=")

    obligations = compile_obligations(
        extract_ast_diffs(standard, student),
        schema={
            "cars_data": ["Id", "Horsepower", "Cylinders"],
            "car_names": ["MakeId"],
        },
    )

    assert len(obligations) == 1
    constraint = obligations[0].hard_constraints[0]
    assert constraint.kind == "scalar_subquery_boundary_path"
    metadata = dict(constraint.metadata)
    assert metadata["standard_scalar_aggregate_function"] == "MIN"
    assert metadata["standard_scalar_source_table"] == "cars_data"
    assert metadata["standard_scalar_source_column"] == "horsepower"


def test_scalar_aggregate_unqualified_column_uses_unique_inner_schema_owner():
    standard = (
        "SELECT T1.campus FROM campuses AS T1 "
        "JOIN faculty AS T2 ON T1.id = T2.campus "
        "WHERE T2.year = 2002 AND faculty > ("
        "SELECT MAX(faculty) FROM campuses AS T1 "
        "JOIN faculty AS T2 ON T1.id = T2.campus "
        'WHERE T2.year = 2002 AND T1.county = "Orange")'
    )
    student = standard.replace("faculty > (", "faculty >= (")

    obligations = compile_obligations(
        extract_ast_diffs(standard, student),
        schema={
            "campuses": ["Id", "Campus", "Location", "County", "Year"],
            "faculty": ["Campus", "Year", "Faculty"],
        },
    )

    assert len(obligations) == 1
    constraint = obligations[0].hard_constraints[0]
    assert constraint.kind == "scalar_subquery_boundary_path"
    metadata = dict(constraint.metadata)
    assert metadata["standard_scalar_source_table"] == "faculty"
    assert metadata["standard_scalar_source_column"] == "faculty"


def test_scalar_aggregate_unqualified_column_refuses_ambiguous_schema_owner():
    standard = (
        "SELECT faculty FROM campuses JOIN faculty "
        "ON campuses.id = faculty.campus "
        "WHERE faculty > (SELECT MAX(faculty) FROM campuses JOIN faculty "
        "ON campuses.id = faculty.campus)"
    )
    student = standard.replace("faculty > (", "faculty >= (")

    obligations = compile_obligations(
        extract_ast_diffs(standard, student),
        schema={
            "campuses": ["id", "campus", "faculty"],
            "faculty": ["campus", "faculty"],
        },
    )

    assert len(obligations) == 1
    metadata = dict(obligations[0].hard_constraints[0].metadata)
    assert metadata["standard_scalar_source_table"] == ""


def test_where_boundary_before_group_having_has_query_path_obligation():
    standard = (
        "SELECT t2.School_name FROM endowment t1 "
        "JOIN school t2 ON t1.School_id = t2.School_id "
        "WHERE t1.amount > 8.5 GROUP BY t1.School_id "
        "HAVING COUNT(*) > 1"
    )
    student = standard.replace("amount > 8.5", "amount >= 8.5")

    obligations = compile_obligations(
        extract_ast_diffs(standard, student),
        schema={
            "school": ["School_id", "School_name"],
            "endowment": ["endowment_id", "School_id", "amount"],
        },
    )

    assert len(obligations) == 1
    constraint = obligations[0].hard_constraints[0]
    assert constraint.kind == "filtered_aggregate_boundary_path"
    assert obligations[0].minimum_rows["endowment"] == 2
    metadata = dict(constraint.metadata)
    assert metadata["standard_source_table"] == "endowment"
    assert metadata["standard_boundary_included"] is False
    assert metadata["student_boundary_included"] is True
    assert metadata["common_qualifying_rows"] == 1
    assert metadata["having_aggregate_function"] == "COUNT"
    assert metadata["having_operator"] == "GT"
    assert metadata["having_boundary"] == 1


def test_aggregate_function_change_requires_separated_results_not_boundary():
    standard = "SELECT Name FROM Projects WHERE Hours = (SELECT MAX(Hours) FROM Projects)"
    student = standard.replace("MAX(Hours)", "MIN(Hours)")

    obligations = compile_obligations(
        extract_ast_diffs(standard, student),
        schema={"Projects": ["Code", "Name", "Hours"]},
    )

    assert len(obligations) == 1
    constraint = obligations[0].hard_constraints[0]
    assert constraint.kind == "aggregate_function_separation"
    assert constraint.value is None
    metadata = dict(constraint.metadata)
    assert metadata["standard_aggregate_function"] == "MAX"
    assert metadata["student_aggregate_function"] == "MIN"


def test_legacy_probe_write_audit_detects_cell_overwrite():
    audit = []
    database = {"employee": [{"salary": 10}]}
    track_database_rows(database, audit)
    with write_owner("probe:a"):
        database["employee"][0]["salary"] = 20
    with write_owner("probe:b"):
        database["employee"][0]["salary"] = 30

    report = summarize_write_audit(audit)

    assert report["write_count"] == 2
    assert report["unique_cells_written"] == 1
    assert report["overwritten_count"] == 1
    assert report["overwritten_cells"][0]["column"] == "salary"
    assert report["overwritten_by_other_owner_count"] == 1


def test_tracked_row_survives_worker_pickle_round_trip():
    audit = []
    database = {"employee": [{"salary": 10}]}
    track_database_rows(database, audit, owner="comparison_boundary")

    restored = pickle.loads(pickle.dumps(database))
    restored_row = restored["employee"][0]
    restored_row["salary"] = 20

    report = summarize_write_audit(restored_row._audit)
    assert report["write_count"] == 1
    assert report["owners"] == ["comparison_boundary"]


def test_planner_splits_high_risk_join_and_window_obligations_into_worlds():
    obligations = compile_obligations(
        [
            ASTDiffNode("JOIN_TYPE", "join_type_changed", target_table="department"),
            ASTDiffNode(
                "WINDOW",
                "window_function_changed",
                target_table="employee",
                target_column="salary",
            ),
        ]
    )

    suite = WitnessPlanner(max_worlds=4).plan(obligations)

    assert len(suite.worlds) == 2
    assert {world.obligation_ids[0] for world in suite.worlds} == {
        item.id for item in obligations
    }
    assert suite.uncovered_obligations == []


def test_planner_materializes_boundary_constraint_in_its_owned_row_slot():
    obligation = compile_obligations(
        [
            ASTDiffNode(
                "WHERE",
                "comparison_operator_changed",
                target_table="employee",
                target_column="salary",
                extra={"value": 50000},
            )
        ]
    )[0]
    suite = WitnessPlanner().plan([obligation])
    world = suite.worlds[0]
    database = {
        "employee": [
            {"id": 1, "salary": 49000},
            {"id": 2, "salary": 51000},
            {"id": 3, "salary": 52000},
        ]
    }

    report = apply_cell_constraints(database, world.constraints)

    assert database["employee"][1]["salary"] == 50000
    assert report["constraints_satisfied"] is True
    assert report["overwritten"] is False


def test_generate_and_compare_stops_before_generation_when_standard_schema_is_invalid():
    run = generate_and_compare(
        "employee(id, name);",
        "SELECT id FROM missing_table",
        "SELECT id FROM employee",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert run.status == "ENGINE_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.error_code == "STANDARD_SCHEMA_QUALIFICATION_FAILED"
    assert "missing_tables=missing_table" in str(run.error)
    assert run.test_database == {}


def test_generate_and_compare_classifies_student_missing_table_as_wrong_answer():
    run = generate_and_compare(
        "employee(id, name);",
        "SELECT id FROM employee",
        "SELECT id FROM misspelled_employee",
    )

    assert run.executed is False
    assert run.judge_status == "WRONG"
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    assert run.error_code == "STUDENT_SCHEMA_REFERENCE_FAILED"
    assert "missing_tables=misspelled_employee" in str(run.error)


def test_generate_and_compare_does_not_require_cte_alias_in_physical_schema():
    run = generate_and_compare(
        "employee(id, enabled);",
        "WITH active AS (SELECT id FROM employee WHERE enabled = 1) SELECT id FROM active",
        "WITH active AS (SELECT id FROM employee WHERE enabled = 1) SELECT id FROM active",
    )

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"


def test_equal_bounded_world_with_uncovered_ast_diff_is_not_proof_of_equivalence():
    diff = ASTDiffNode(
        clause_category="WHERE",
        diff_type="predicate_expression_operator_changed",
        target_table="employee",
        target_column="salary",
    )
    run = SandboxRun(
        executed=True,
        is_equivalent=True,
        error=None,
        standard_sqlite="SELECT salary FROM employee",
        student_sqlite="SELECT salary FROM employee",
        standard_rows=[(1,)],
        student_rows=[(1,)],
        standard_columns=["salary"],
        student_columns=["salary"],
        test_database={"employee": [{"salary": 1}]},
        data_evidence={},
        mutation_evidence={"tests": []},
        ast_diffs=[diff],
    )
    suite = WitnessSuite(worlds=[WitnessWorld(id="world_01")], obligations=[])
    _attach_witness_evidence(run, suite, "world_01", [diff])

    assert run.status == "KNOWN_GAP"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.judge_status == "UNDECIDED"
    assert run.data_evidence["judge_status"] == "UNDECIDED"
    assert run.data_evidence["verdict_guard"]["distinguished_obligation"] is False


def test_large_limit_boundary_is_unresolved_in_all_public_verdict_fields():
    run = generate_and_compare(
        "items(id INTEGER);",
        "SELECT id FROM items LIMIT 100",
        "SELECT id FROM items LIMIT 101",
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is True  # bounded-world compatibility observation
    assert run.status == "SEMANTIC_BOUNDARY"
    assert run.equivalence_conclusion == "UNDECIDED"
    assert run.judge_status == "UNDECIDED"
    assert run.data_evidence["judge_status"] == "UNDECIDED"
    assert run.boundary_evidence["required_rows"] == 102
    assert run.boundary_evidence["witness_row_limit"] == 32


def test_mutation_binding_prefers_knowledge_point_and_marks_ambiguous_matches():
    diffs = [
        ASTDiffNode("WHERE", "comparison_operator_changed", knowledge_point_id="where-comp"),
        ASTDiffNode("WHERE", "logical_operator_changed", knowledge_point_id="where-logic"),
    ]
    evidence = {
        "tests": [
            {"clause": "WHERE", "knowledge_point_id": "where-comp"},
            {"clause": "WHERE", "knowledge_point_id": "unknown"},
        ]
    }

    _link_mutation_diff_ids(evidence, diffs)

    assert len(evidence["tests"][0]["diff_ids"]) == 1
    assert evidence["tests"][0]["binding_quality"] == "exact"
    assert len(evidence["tests"][1]["diff_ids"]) == 2
    assert evidence["tests"][1]["binding_quality"] == "ambiguous"


def test_window_function_obligation_carries_source_and_tie_evidence():
    run = generate_and_compare(
        "scores(dept, score);",
        "SELECT dept, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY score) AS rn FROM scores",
        "SELECT dept, RANK() OVER (PARTITION BY dept ORDER BY score) AS rn FROM scores",
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    item = effectiveness[0]
    assert item["probe"] == "window_partition_ties"
    assert item["constraints_satisfied"] is True
    assert item["semantic_validation"]["evidence"]["partition_count"] >= 2
    assert item["semantic_validation"]["evidence"]["order_tie_count"] >= 1


@pytest.mark.parametrize(
    ("standard_sql", "student_sql"),
    [
        (
            "SELECT name, FIRST_VALUE(salary) OVER "
            "(PARTITION BY dept ORDER BY salary "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM instructor",
            "SELECT name, LAST_VALUE(salary) OVER "
            "(PARTITION BY dept ORDER BY salary "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM instructor",
        ),
        (
            "SELECT name, SUM(salary) OVER "
            "(PARTITION BY dept ORDER BY salary) AS value FROM instructor",
            "SELECT name, SUM(salary) OVER "
            "(PARTITION BY dept) AS value FROM instructor",
        ),
    ],
)
def test_window_value_and_default_frame_obligations_get_three_row_partition(
    standard_sql, student_sql
):
    run = generate_and_compare(
        "instructor(id, name, dept, salary);",
        standard_sql,
        student_sql,
        max_rows_per_table=4,
    )

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.data_evidence["obligation_effectiveness"][0]["distinguished"] is True


def test_order_direction_obligation_has_one_complete_diff_evidence_chain():
    run = generate_and_compare(
        "employee(id, name, salary);",
        "SELECT name FROM employee ORDER BY salary ASC",
        "SELECT name FROM employee ORDER BY salary DESC",
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    item = effectiveness[0]
    assert item["probe"] == "order_key_separation"
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert item["semantic_validation"]["evidence"]["discriminator_column"] == "salary"
    order_mutations = [
        mutation
        for mutation in run.mutation_evidence["tests"]
        if mutation.get("clause") == "ORDER BY"
    ]
    assert len(order_mutations) == 1
    assert order_mutations[0]["binding_quality"] == "exact"
    assert order_mutations[0]["diff_ids"] == [item["diff_id"]]


def test_order_tiebreaker_obligation_validates_tied_prefix_and_split_key():
    run = generate_and_compare(
        "employee(id, name, salary);",
        "SELECT name FROM employee ORDER BY salary ASC, name DESC",
        "SELECT name FROM employee ORDER BY salary ASC",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    evidence = effectiveness[0]["semantic_validation"]["evidence"]
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True
    assert evidence["prefix_columns"] == ["salary"]
    assert evidence["discriminator_column"] == "name"
    assert len(evidence["distinguishing_row_indexes"]) == 2


def test_join_on_obligation_keeps_declared_standard_and_student_paths_bound():
    run = generate_and_compare(
        "customers(id, name); orders(id, customer_id);",
        "SELECT c.name FROM customers c JOIN orders o ON c.id = o.customer_id",
        "SELECT c.name FROM customers c JOIN orders o ON c.id = o.id",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert [diff.diff_type for diff in run.ast_diffs] == [
        "join_on_changed",
        "join_key_column_changed",
    ]
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    item = effectiveness[0]
    assert item["probe"] == "join_key_drift"
    assert item["constraints_satisfied"] is True
    assert item["causal_attribution_verified"] is True
    assert item["semantic_validation"]["evidence"]["standard_matched_values"]
    assert item["semantic_validation"]["evidence"]["student_matched_values"] == []


@pytest.mark.parametrize("row_count", [4, 8, 12, 16])
def test_self_join_key_drift_has_stable_complete_evidence_chain(row_count):
    run = generate_and_compare(
        "employee(id, name, manager_id);",
        "SELECT e.name FROM employee e "
        "JOIN employee m ON e.manager_id = m.id",
        "SELECT e.name FROM employee e "
        "JOIN employee m ON e.id = m.id",
        max_rows_per_table=row_count,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert item["causal_attribution_verified"] is True
    assert item["semantic_validation"]["evidence"]["left_table"] == "employee"
    assert item["semantic_validation"]["evidence"]["right_table"] == "employee"
    assert any(test["distinguished"] for test in item["atomic_validation"])


@pytest.mark.parametrize("row_count", [4, 8, 12, 16])
def test_removed_join_conjunct_has_stable_complete_evidence_chain(row_count):
    run = generate_and_compare(
        "enrollment(id, year, grade); exam(id, year, score);",
        "SELECT grade FROM enrollment e JOIN exam x "
        "ON e.id = x.id AND e.year = x.year",
        "SELECT grade FROM enrollment e JOIN exam x ON e.id = x.id",
        max_rows_per_table=row_count,
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.equivalence_conclusion == "NOT_EQUIVALENT"
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert item["causal_attribution_verified"] is True
    assert item["semantic_validation"]["evidence"]["divergence_direction"] == "student_only"
    assert any(test["distinguished"] for test in item["atomic_validation"])


def test_set_operator_obligation_binds_branch_paths_and_atomic_mutation():
    run = generate_and_compare(
        "a(id); b(id);",
        "SELECT id FROM a INTERSECT SELECT id FROM b",
        "SELECT id FROM a UNION SELECT id FROM b",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    item = effectiveness[0]
    assert item["probe"] == "set_overlap"
    assert item["constraints_satisfied"] is True
    assert item["causal_attribution_verified"] is True
    assert item["semantic_validation"]["evidence"]["required_paths"] == [
        "left_only", "overlap", "right_only"
    ]
    assert item["atomic_validation"][0]["distinguished"] is True


def test_correlated_subquery_obligation_keeps_two_outer_membership_paths():
    run = generate_and_compare(
        "employee(id, name); bonus(employee_id, amount);",
        "SELECT name FROM employee WHERE EXISTS ("
        "SELECT 1 FROM bonus b WHERE b.employee_id = employee.id)",
        "SELECT name FROM employee WHERE EXISTS ("
        "SELECT 1 FROM bonus b WHERE b.employee_id <> employee.id)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    item = effectiveness[0]
    assert item["probe"] == "subquery_membership_paths"
    assert item["constraints_satisfied"] is True
    assert item["causal_attribution_verified"] is True
    assert item["semantic_validation"]["evidence"]["outer_only_values"]


def test_multi_world_execution_keeps_atomic_obligation_evidence_separate():
    run = generate_and_compare(
        "employee(id, salary);",
        "SELECT DISTINCT salary FROM employee WHERE salary > 50000",
        "SELECT salary FROM employee WHERE salary >= 50000",
        sql_dialect="sqlite",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    suite = run.data_evidence["witness_suite"]
    assert suite["world_count"] >= 3  # two isolated worlds plus a compatibility world
    assert all(len(world["obligation_ids"]) in {1, 2} for world in suite["worlds"])
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert {item["probe"] for item in effectiveness} >= {
        "duplicate_projection",
        "comparison_boundary_tristate",
    }
    assert all(item["diff_id"] for item in effectiveness)
    assert any(item["causal_attribution_verified"] for item in effectiveness)


def test_unqualified_missing_column_is_rejected_before_database_generation():
    run = generate_and_compare(
        "employee(id, name);",
        "SELECT missing FROM employee",
        "SELECT id FROM employee",
        sql_dialect="sqlite",
    )

    assert run.executed is False
    assert run.judge_status == "ENGINE_ERROR"
    assert run.error_code == "STANDARD_SCHEMA_QUALIFICATION_FAILED"
    assert "missing_physical_columns" in str(run.error)


def test_star_projection_is_a_single_shape_obligation_with_atomic_attribution():
    run = generate_and_compare(
        "employee(id, name);",
        "SELECT * FROM employee",
        "SELECT id FROM employee",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    effectiveness = run.data_evidence["obligation_effectiveness"]
    assert len(effectiveness) == 1
    item = effectiveness[0]
    assert item["probe"] == "projection_shape_check"
    assert item["constraints_satisfied"] is True
    assert item["causal_attribution_verified"] is True


def test_inline_recursive_cte_obligation_is_validated_from_execution_result():
    run = generate_and_compare(
        "",
        "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) SELECT n FROM nums",
        "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 3) SELECT n FROM nums",
        sql_dialect="sqlite",
    )

    recursive = next(
        item for item in run.data_evidence["obligation_effectiveness"]
        if item["probe"] == "cte_recursive_paths"
    )
    assert recursive["constraints_satisfied"] is True
    assert recursive["causal_attribution_verified"] is True


def test_core_teaching_diffs_close_semantic_and_atomic_evidence_chain():
    cases = [
        (
            "projection_value_check",
            "t(id, name);",
            "SELECT id FROM t",
            "SELECT name FROM t",
        ),
        (
            "null_tristate",
            "t(id, value);",
            "SELECT id FROM t WHERE value IS NULL",
            "SELECT id FROM t WHERE value IS NOT NULL",
        ),
        (
            "aggregate_boundary_group",
            "sales(id, dept, amount);",
            "SELECT dept FROM sales GROUP BY dept HAVING COUNT(*) >= 3",
            "SELECT dept FROM sales GROUP BY dept HAVING COUNT(*) > 3",
        ),
        (
            "limit_row_count_boundary",
            "t(id);",
            "SELECT id FROM t LIMIT 2",
            "SELECT id FROM t LIMIT 3",
        ),
        (
            "subquery_membership_paths",
            "a(id); b(a_id);",
            "SELECT id FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.a_id = a.id)",
            "SELECT id FROM a",
        ),
    ]

    for probe, schema, standard, student in cases:
        run = generate_and_compare(schema, standard, student, sql_dialect="sqlite")
        item = next(
            evidence for evidence in run.data_evidence["obligation_effectiveness"]
            if evidence["probe"] == probe
        )
        assert run.status == "SUPPORTED", probe
        assert item["constraints_satisfied"] is True, probe
        assert item["causal_attribution_verified"] is True, probe


def test_added_group_by_has_rewritable_atomic_variant_without_feedback_loop():
    run = generate_and_compare(
        "sales(id INTEGER, dept TEXT, amount INTEGER);",
        "SELECT SUM(amount) FROM sales",
        "SELECT dept, SUM(amount) FROM sales GROUP BY dept",
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    item = next(
        evidence
        for evidence in run.data_evidence["obligation_effectiveness"]
        if evidence["probe"] == "group_grain_split"
    )
    assert run.status == "SUPPORTED"
    assert item["constraints_satisfied"] is True
    assert item["distinguished"] is True
    assert item["causal_attribution_verified"] is True
    assert item["attempt_count"] == 1
    assert item["atomic_validation"][0]["supported"] is True
    assert "GROUP BY" in item["atomic_validation"][0]["variant_sql"].upper()


def test_phase1_predicate_presence_uses_query_context_for_compound_where():
    cases = [
        (
            "SELECT title FROM course WHERE credits > 3 AND id > 2",
            "SELECT title FROM course WHERE credits > 3",
        ),
        (
            "SELECT title FROM course WHERE credits > 3",
            "SELECT title FROM course WHERE credits > 3 AND id > 2",
        ),
    ]

    for standard, student in cases:
        run = generate_and_compare(
            "course(id, title, credits);",
            standard,
            student,
            sql_dialect="sqlite",
        )
        assert run.status == "SUPPORTED"
        assert run.is_equivalent is False
        assert len(run.data_evidence["obligation_effectiveness"]) == 1
        item = run.data_evidence["obligation_effectiveness"][0]
        assert item["probe"] == "predicate_positive_negative"
        assert item["constraints_satisfied"] is True
        assert item["distinguished"] is True
        assert item["causal_attribution_verified"] is True
        assert item["semantic_validation"]["evidence"]["divergent_row_indexes"]


def test_phase1_logical_precedence_jointly_solves_same_column_leaves():
    run = generate_and_compare(
        "course(id, title, credits);",
        "SELECT title FROM course WHERE (credits = 1 OR credits = 3) AND id > 2",
        "SELECT title FROM course WHERE credits = 1 OR credits = 3 AND id > 2",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["probe"] == "logical_truth_table"
    assert item["constraints_satisfied"] is True
    assert item["causal_attribution_verified"] is True
    assert item["semantic_validation"]["evidence"]["distinguishing_row_indexes"]


def test_phase1_removed_case_branch_materializes_branch_and_unmatched_paths():
    run = generate_and_compare(
        "takes(id, grade);",
        "SELECT CASE WHEN grade = 'A' THEN 'A' WHEN grade = 'B' THEN 'B' "
        "ELSE 'other' END FROM takes",
        "SELECT CASE WHEN grade = 'A' THEN 'A' ELSE 'other' END FROM takes",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    assert len(run.data_evidence["obligation_effectiveness"]) == 1
    item = run.data_evidence["obligation_effectiveness"][0]
    assert item["probe"] == "case_branch_coverage"
    assert item["constraints_satisfied"] is True
    assert item["causal_attribution_verified"] is True
    evidence = item["semantic_validation"]["evidence"]
    assert evidence["branch_hit_counts"] == [1, 1]
    assert evidence["unmatched_row_indexes"]


def test_phase1_not_in_null_trap_has_null_sensitive_causal_witness():
    run = generate_and_compare(
        "employee(id, name, manager_id);",
        "SELECT name FROM employee e WHERE NOT EXISTS ("
        "SELECT 1 FROM employee m WHERE m.manager_id = e.id)",
        "SELECT name FROM employee WHERE id NOT IN ("
        "SELECT manager_id FROM employee)",
        sql_dialect="sqlite",
    )

    assert run.status == "SUPPORTED"
    assert run.is_equivalent is False
    null_evidence = next(
        item for item in run.data_evidence["obligation_effectiveness"]
        if item["semantic_validation"]["evidence"].get("requires_inner_null")
    )
    assert null_evidence["constraints_satisfied"] is True
    assert null_evidence["causal_attribution_verified"] is True
    assert null_evidence["semantic_validation"]["evidence"]["inner_null_count"] >= 1
    assert run.mutation_evidence["summary"]["fixed_by_replacement"] >= 1


def test_phase1_core_boundary_suite_includes_distinct_offset_and_case():
    cases = [
        (
            "duplicate_projection",
            "instructor(id, dept, salary);",
            "SELECT COUNT(DISTINCT dept) FROM instructor",
            "SELECT COUNT(dept) FROM instructor",
        ),
        (
            "limit_row_count_boundary",
            "course(id, title, credits);",
            "SELECT title FROM course ORDER BY id LIMIT 3 OFFSET 2",
            "SELECT title FROM course ORDER BY id LIMIT 3 OFFSET 3",
        ),
        (
            "case_branch_coverage",
            "course(id, title, credits);",
            "SELECT CASE WHEN credits >= 3 THEN title ELSE 'other' END FROM course",
            "SELECT CASE WHEN credits >= 3 THEN title END FROM course",
        ),
    ]

    for probe, schema, standard, student in cases:
        run = generate_and_compare(schema, standard, student, sql_dialect="sqlite")
        item = next(
            evidence for evidence in run.data_evidence["obligation_effectiveness"]
            if evidence["probe"] == probe
        )
        assert run.status == "SUPPORTED", probe
        assert item["constraints_satisfied"] is True, probe
        assert item["distinguished"] is True, probe
        assert item["causal_attribution_verified"] is True, probe
