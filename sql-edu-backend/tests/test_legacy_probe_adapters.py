from types import SimpleNamespace

from core.parseval_data_generator import (
    extract_ast_diffs,
    generate_test_database,
    generate_witness_suite,
)
from core.witness_generation.adapters import (
    LegacyProbeAdapter,
    LegacyProbeRegistry,
    run_adapter,
)
from core.witness_generation.schema_scope import ColumnRef
from core.witness_generation.planner import (
    CellConstraint,
    ConstraintConflict,
    ConstraintLedger,
    WitnessWorld,
    split_world_on_conflict,
)


def _diff(kind="comparison_operator_changed", clause="WHERE", kp="where-comp"):
    return SimpleNamespace(diff_type=kind, clause_category=clause, knowledge_point_id=kp)


def test_adapter_matches_and_runs_under_stable_owner():
    owners = []

    def apply(data, schema, standard, student, diffs):
        owners.append("called")
        data["employee"][0]["salary"] = 50000

    adapter = LegacyProbeAdapter(
        name="comparison_boundary",
        phase=4,
        apply=apply,
        diff_types=frozenset({"comparison_operator_changed"}),
        write_set=frozenset({ColumnRef("employee", "salary", "root")}),
    )
    result = run_adapter(
        adapter,
        data={"employee": [{"salary": 1}]},
        schema={"employee": ["salary"]},
        standard_sql="SELECT salary FROM employee",
        student_sql="SELECT salary FROM employee",
        ast_diffs=[_diff()],
    )

    assert result.activated is True
    assert result.applied is True
    assert owners == ["called"]
    assert result.write_set_satisfied is True
    assert result.writes == [{
        "table": "employee",
        "row_index": 0,
        "column": "salary",
        "before": 1,
        "after": 50000,
        "kind": "cell_changed",
    }]


def test_adapter_rolls_back_writes_outside_declared_write_set():
    adapter = LegacyProbeAdapter(
        name="misdeclared_probe",
        phase=4,
        apply=lambda data, schema, standard, student, diffs: data["employee"][0].__setitem__("name", "changed"),
        diff_types=frozenset({"comparison_operator_changed"}),
        write_set=frozenset({ColumnRef("employee", "salary", "root")}),
    )
    data = {"employee": [{"salary": 1, "name": "before"}]}

    result = run_adapter(
        adapter,
        data=data,
        schema={"employee": ["salary", "name"]},
        standard_sql="SELECT salary FROM employee",
        student_sql="SELECT salary FROM employee",
        ast_diffs=[_diff()],
    )

    assert result.applied is False
    assert result.write_set_satisfied is False
    assert result.conflicts[0]["action"] == "split_world"
    assert data == {"employee": [{"salary": 1, "name": "before"}]}


def test_adapter_registry_is_phase_ordered_and_deduplicated():
    registry = LegacyProbeRegistry()
    noop = lambda data, schema, standard, student, diffs: None
    registry.register(LegacyProbeAdapter("late", 12, noop, clauses=frozenset({"WHERE"})))
    registry.register(LegacyProbeAdapter("early", 4, noop, clauses=frozenset({"WHERE"})))

    assert [item.name for item in registry.active([_diff()])] == ["early", "late"]
    try:
        registry.register(LegacyProbeAdapter("early", 5, noop))
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate adapter name was accepted")


def test_adapter_reports_legacy_failure_without_hiding_it():
    def broken(data, schema, standard, student, diffs):
        raise RuntimeError("probe failed")

    result = run_adapter(
        LegacyProbeAdapter("broken", 4, broken, diff_types=frozenset({"comparison_operator_changed"})),
        data={}, schema={}, standard_sql="", student_sql="", ast_diffs=[_diff()],
    )

    assert result.applied is False
    assert result.diagnostics == ["adapter_failed:RuntimeError:probe failed"]


def test_adapter_can_activate_from_sql_shape_without_ast_diff():
    adapter = LegacyProbeAdapter(
        "boolean_coverage",
        4,
        lambda data, schema, standard, student, diffs: None,
        sql_trigger=lambda standard, student: " AND " in standard.upper(),
    )

    result = run_adapter(
        adapter,
        data={},
        schema={},
        standard_sql="SELECT * FROM t WHERE a = 1 AND b = 2",
        student_sql="SELECT * FROM t WHERE b = 2",
        ast_diffs=[],
    )

    assert result.activated is True
    assert result.applied is True


def test_adapter_activation_guard_rejects_matching_diff_with_wrong_sql_shape():
    adapter = LegacyProbeAdapter(
        "correlated_only",
        7,
        lambda data, schema, standard, student, diffs: (_ for _ in ()).throw(
            AssertionError("guarded adapter must not run")
        ),
        diff_types=frozenset({"subquery_removed"}),
        activation_guard=lambda standard, student: "EXISTS" in standard.upper(),
    )

    result = run_adapter(
        adapter,
        data={},
        schema={},
        standard_sql="SELECT id FROM employee",
        student_sql="SELECT id FROM employee",
        ast_diffs=[_diff("subquery_removed", "SUBQUERY", "subquery")],
    )

    assert result.activated is False
    assert result.applied is False


def test_adapter_declared_constraint_conflict_requests_world_split():
    ledger = ConstraintLedger()
    existing = CellConstraint(
        table="employee", row_slot="boundary_row", column="salary",
        relation="equals", value=50000, owner="comparison",
    )
    ledger.add(existing)
    adapter = LegacyProbeAdapter(
        "other_boundary", 4,
        lambda data, schema, standard, student, diffs: (_ for _ in ()).throw(AssertionError("must not run")),
        cell_constraints=(CellConstraint(
            table="employee", row_slot="boundary_row", column="salary",
            relation="equals", value=60000, owner="other",
        ),),
        sql_trigger=lambda standard, student: True,
    )

    result = run_adapter(
        adapter,
        data={}, schema={}, standard_sql="", student_sql="", ast_diffs=[],
        ledger=ledger,
    )

    assert result.applied is False
    assert result.diagnostics == ["adapter_conflict"]
    assert result.conflicts[0]["action"] == "split_world"
    assert len(result.constraint_conflicts) == 1
    assert result.constraint_conflicts[0].existing == existing
    assert result.constraint_conflicts[0].incoming.value == 60000


def test_split_world_on_constraint_conflict_keeps_independent_candidates():
    existing = CellConstraint(
        table="employee", row_slot="boundary_row", column="salary",
        relation="equals", value=50000, owner="comparison",
        obligation_id="obligation_comparison",
        diff_id="diff_comparison",
    )
    incoming = CellConstraint(
        table="employee", row_slot="boundary_row", column="salary",
        relation="equals", value=60000, owner="having",
        obligation_id="obligation_having",
        diff_id="diff_having",
    )
    world = WitnessWorld(
        id="world_01",
        obligation_ids=["obligation_comparison", "obligation_having"],
        diff_ids=["diff_comparison", "diff_having"],
        constraints=[existing],
        database={"employee": [{"salary": 50000}]},
    )
    left, right = split_world_on_conflict(
        world,
        ConstraintConflict(
            target=existing.target,
            existing=existing,
            incoming=incoming,
            reason="locked_cell_incompatible",
        ),
    )

    assert left.id == "world_01"
    assert right.id == "world_01_split"
    assert [item.value for item in left.constraints] == [50000]
    assert [item.value for item in right.constraints] == [60000]
    assert left.obligation_ids == ["obligation_comparison"]
    assert right.obligation_ids == ["obligation_having"]
    assert left.diff_ids == ["diff_comparison"]
    assert right.diff_ids == ["diff_having"]
    assert left.database is not right.database
    assert left.constraints is not right.constraints
    assert world.constraints[0].value == 50000
    assert left.execution["world_splits"][0]["selected_side"] == "existing"
    assert right.execution["world_splits"][0]["selected_side"] == "incoming"


def test_generate_witness_suite_materializes_comparison_conflict_as_two_worlds():
    suite = generate_witness_suite(
        {"employee": ["id", "salary"]},
        "SELECT salary FROM employee "
        "WHERE salary > 50000 AND salary < 60000",
        "SELECT salary FROM employee "
        "WHERE salary >= 50000 AND salary <= 60000",
        max_rows_per_table=4,
        max_worlds=8,
    )

    split_worlds = [
        world
        for world in suite.worlds
        if "world_split_from_constraint_conflict" in world.diagnostics
    ]
    comparison_obligation_ids = {
        obligation.id
        for obligation in suite.obligations
        if obligation.diff_type == "comparison_operator_changed"
    }
    assert len(split_worlds) == 2
    assert len(comparison_obligation_ids) == 2
    assert all(
        len(set(world.obligation_ids) & comparison_obligation_ids) == 1
        for world in split_worlds
    )
    assert split_worlds[0].database is not split_worlds[1].database
    assert {
        constraint.value
        for world in split_worlds
        for constraint in world.constraints
    } == {50000, 60000}
    assert all(not world.execution["adapter_conflicts"] for world in split_worlds)
    assert any(
        item.startswith("adapter_constraint_conflict_split:")
        for item in suite.planner_diagnostics
    )


def test_join_key_drift_adapter_declares_endpoints_and_replaces_registry_dispatch():
    standard = (
        "SELECT c.name FROM customers c "
        "JOIN orders o ON c.id = o.customer_id"
    )
    student = (
        "SELECT c.name FROM customers c "
        "JOIN orders o ON c.id = o.id"
    )
    metadata = {}
    write_audit = []

    generate_test_database(
        {"customers": ["id", "name"], "orders": ["id", "customer_id"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    run = next(
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "join_key_drift"
    )
    declared = {
        (item["relation"], item["column"])
        for item in run["declared_write_set"]
    }
    written = {
        (item["table"], item["column"])
        for item in run["writes"]
        if item["kind"] == "cell_changed"
    }

    assert run["activated"] is True
    assert run["applied"] is True
    assert run["write_set_satisfied"] is True
    assert declared == {
        ("customers", "id"),
        ("orders", "customer_id"),
        ("orders", "id"),
    }
    assert written == declared
    assert any(event.owner == "legacy:join_key_drift" for event in write_audit)
    assert all(
        event.owner != "registry:join_on_counterexample"
        for event in write_audit
    )


def test_join_matched_dangling_adapter_runs_once_at_final_stage():
    standard = (
        "SELECT o.id FROM orders o LEFT JOIN customers c "
        "ON o.customer_id = c.id"
    )
    student = standard.replace("LEFT JOIN", "JOIN")
    metadata = {}
    write_audit = []

    database = generate_test_database(
        {"orders": ["id", "customer_id"], "customers": ["id", "name"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "join_matched_dangling"
    ]
    assert len(runs) == 1
    run = runs[0]
    assert run["stage"] == "final"
    assert run["applied"] is True
    assert run["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in run["declared_write_set"]
    } == {("orders", "customer_id"), ("customers", "id")}

    order_keys = {row["customer_id"] for row in database["orders"]}
    customer_keys = {row["id"] for row in database["customers"]}
    assert order_keys & customer_keys
    assert order_keys - customer_keys
    assert all(
        (item["table"], item["column"])
        in {("orders", "customer_id"), ("customers", "id")}
        for item in run["writes"]
    )
    assert any(
        event.owner == "legacy:join_matched_dangling"
        for event in write_audit
    )


def test_group_grain_adapter_splits_student_key_inside_standard_group():
    standard = "SELECT dept, SUM(amount) FROM sales GROUP BY dept"
    student = (
        "SELECT dept, SUM(amount) FROM sales GROUP BY dept, region"
    )
    metadata = {}
    write_audit = []

    database = generate_test_database(
        {"sales": ["id", "dept", "region", "amount"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    run = next(
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "group_grain_split"
    )
    assert run["stage"] == "main"
    assert run["applied"] is True
    assert run["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in run["declared_write_set"]
    } == {("sales", "dept"), ("sales", "region")}
    assert database["sales"][0]["dept"] == database["sales"][1]["dept"]
    assert database["sales"][0]["region"] != database["sales"][1]["region"]
    assert any(event.owner == "legacy:group_grain_split" for event in write_audit)
    assert all(
        event.owner != "registry:group_cardinality_probe"
        for event in write_audit
    )


def test_correlated_subquery_adapter_declares_outer_and_inner_keys():
    standard = (
        "SELECT e.name FROM employee e WHERE EXISTS ("
        "SELECT 1 FROM bonus b WHERE b.employee_id = e.id)"
    )
    student = "SELECT e.name FROM employee e"
    metadata = {}
    write_audit = []

    database = generate_test_database(
        {"employee": ["id", "name"], "bonus": ["employee_id", "amount"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "correlated_subquery_overlap"
    ]
    assert len(runs) == 1
    assert runs[0]["stage"] == "post_main"
    assert runs[0]["applied"] is True
    assert runs[0]["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("employee", "id"), ("bonus", "employee_id")}
    assert len(
        {row["id"] for row in database["employee"]}
        & {row["employee_id"] for row in database["bonus"]}
    ) >= 2
    assert any(
        event.owner == "legacy:correlated_subquery_overlap"
        for event in write_audit
    )


def test_correlated_subquery_adapter_ignores_uncorrelated_membership():
    standard = (
        "SELECT s.name FROM student s WHERE s.id IN ("
        "SELECT t.student_id FROM takes t WHERE t.course_id = 'CS101')"
    )
    student = "SELECT s.name FROM student s"
    metadata = {}

    generate_test_database(
        {"student": ["id", "name"], "takes": ["student_id", "course_id"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
    )

    assert all(
        item["name"] != "correlated_subquery_overlap"
        for item in metadata["legacy_probe_adapters"]
    )


def test_set_overlap_adapter_declares_branch_columns_once():
    standard = "SELECT id FROM a INTERSECT SELECT id FROM b"
    student = "SELECT id FROM a UNION SELECT id FROM b"
    metadata = {}
    write_audit = []

    generate_test_database(
        {"a": ["id"], "b": ["id"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "set_overlap"
    ]
    assert len(runs) == 1
    assert runs[0]["stage"] == "main"
    assert runs[0]["applied"] is True
    assert runs[0]["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("a", "id"), ("b", "id")}
    assert any(event.owner == "legacy:set_overlap" for event in write_audit)
    assert all(
        event.owner != "registry:set_operator_overlap_probe"
        for event in write_audit
    )


def test_cte_base_adapter_declares_inner_columns_and_replaces_registry_tactic():
    standard = (
        "WITH high_salary AS ("
        "SELECT name FROM employee WHERE salary > 50000"
        ") SELECT name FROM high_salary"
    )
    student = "SELECT name FROM employee"
    metadata = {}
    write_audit = []

    generate_test_database(
        {"employee": ["id", "name", "salary"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "cte_base_constraints"
    ]
    assert len(runs) == 1
    assert runs[0]["stage"] == "main"
    assert runs[0]["applied"] is True
    assert runs[0]["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("employee", "name"), ("employee", "salary")}
    assert any(event.owner == "legacy:cte_base_constraints" for event in write_audit)
    assert all(
        event.owner != "registry:cte_constraint_probe"
        for event in write_audit
    )


def test_case_obligation_materializer_replaces_registry_tactic():
    standard = (
        "SELECT CASE WHEN grade = 'A' THEN 'A' "
        "WHEN grade = 'B' THEN 'B' ELSE 'other' END FROM takes"
    )
    student = (
        "SELECT CASE WHEN grade = 'A' THEN 'A' "
        "ELSE 'other' END FROM takes"
    )
    write_audit = []

    database = generate_test_database(
        {"takes": ["id", "grade"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        write_audit=write_audit,
    )

    assert [row["grade"] for row in database["takes"][:3]] == [
        "A",
        "B",
        "not_A",
    ]
    assert any(
        event.owner == "materializer:case_branch_coverage"
        for event in write_audit
    )
    assert all(
        event.owner != "registry:case_branch_probe"
        for event in write_audit
    )


def test_distinct_adapter_runs_once_at_final_stage():
    standard = "SELECT DISTINCT course_id FROM takes"
    student = "SELECT course_id FROM takes"
    metadata = {}
    write_audit = []

    generate_test_database(
        {"takes": ["id", "course_id", "year"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "distinct_projection"
    ]
    assert len(runs) == 1
    assert runs[0]["stage"] == "final"
    assert runs[0]["applied"] is True
    assert runs[0]["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("takes", "course_id")}
    assert any(event.owner == "legacy:distinct_projection" for event in write_audit)
    assert all(
        event.owner != "registry:duplicate_projection_probe"
        for event in write_audit
    )


def test_window_adapters_preserve_phase_order_and_replace_registry_tactics():
    standard = (
        "SELECT dept, ROW_NUMBER() OVER ("
        "PARTITION BY dept ORDER BY score) AS rn FROM scores"
    )
    student = (
        "SELECT dept, ROW_NUMBER() OVER (ORDER BY score) AS rn FROM scores"
    )
    metadata = {}
    write_audit = []

    generate_test_database(
        {"scores": ["id", "dept", "score"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] in {"window_partition_layout", "bounded_order_ties"}
    ]
    assert [item["name"] for item in runs] == [
        "window_partition_layout",
        "bounded_order_ties",
    ]
    assert all(item["stage"] == "main" for item in runs)
    assert all(item["applied"] is True for item in runs)
    assert all(item["write_set_satisfied"] is True for item in runs)
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("scores", "dept")}
    owners = {event.owner for event in write_audit}
    assert "legacy:window_partition_layout" in owners
    assert "legacy:bounded_order_ties" in owners
    assert "registry:window_partition_order_probe" not in owners
    assert "registry:ordered_compare_probe" not in owners


def test_window_alias_adapter_declares_physical_layout_after_repairs():
    standard = (
        "SELECT name, ROW_NUMBER() OVER ("
        "PARTITION BY dept ORDER BY salary DESC) AS rn "
        "FROM instructor QUALIFY rn = 1"
    )
    student = (
        "SELECT name, ROW_NUMBER() OVER ("
        "PARTITION BY dept ORDER BY salary DESC) AS rn "
        "FROM instructor QUALIFY rn <= 2"
    )
    metadata = {}
    write_audit = []

    generate_test_database(
        {"instructor": ["id", "name", "dept", "salary"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "window_alias_predicate_layout"
    ]
    assert len(runs) == 1
    assert runs[0]["stage"] == "post_repair"
    assert runs[0]["applied"] is True
    assert runs[0]["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("instructor", "dept"), ("instructor", "salary")}
    assert any(
        event.owner == "legacy:window_alias_predicate_layout"
        for event in write_audit
    )
    assert all(
        event.owner != "legacy_probe"
        for event in write_audit
        if event.column in {"dept", "salary"}
        and str(event.value).startswith("__group_")
    )


def test_order_key_adapter_declares_sort_and_projection_columns_once():
    standard = "SELECT name FROM employee ORDER BY salary ASC"
    student = "SELECT name FROM employee ORDER BY salary DESC"
    metadata = {}
    write_audit = []

    generate_test_database(
        {"employee": ["id", "name", "salary"]},
        standard,
        student,
        ast_diffs=extract_ast_diffs(standard, student),
        generation_metadata=metadata,
        write_audit=write_audit,
    )

    runs = [
        item
        for item in metadata["legacy_probe_adapters"]
        if item["name"] == "order_key_separation"
    ]
    assert len(runs) == 1
    assert runs[0]["applied"] is True
    assert runs[0]["write_set_satisfied"] is True
    assert {
        (item["relation"], item["column"])
        for item in runs[0]["declared_write_set"]
    } == {("employee", "name"), ("employee", "salary")}
    assert any(event.owner == "legacy:order_key_separation" for event in write_audit)
    assert all(
        event.owner != "registry:ordered_compare_probe"
        for event in write_audit
    )
