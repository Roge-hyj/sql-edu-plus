from types import SimpleNamespace

import pytest

from core.witness_generation.regex_support import (
    glob_matches,
    like_matches,
)
from core.witness_generation.validators import validate_obligation
from core.witness_generation.obligations import ConstraintSpec, DistinguishingObligation


def _obligation(kind, relation="", column="", value=None):
    return DistinguishingObligation(
        id="obligation_test",
        diff_id="diff_test",
        diff_type="test",
        clause="test",
        knowledge_point_id="test",
        required_tables={relation} if relation else set(),
        hard_constraints=[ConstraintSpec(kind, relation, column, value)],
    )


def _join_obligation():
    obligation = _obligation(
        "matched_and_dangling_join_rows", "customers", "customer_id"
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "matched_and_dangling_join_rows",
        "customers",
        "customer_id",
        metadata=(("standard_join_pairs", (("orders", "customer_id", "customers", "customer_id"),)),),
    )
    return obligation


def test_join_validator_requires_matched_and_dangling_key_paths():
    world = SimpleNamespace(database={
        "orders": [{"customer_id": 1}, {"customer_id": 2}],
        "customers": [{"customer_id": 1}, {"customer_id": 3}],
    })

    result = validate_obligation(
        world,
        _join_obligation(),
    )

    assert result.constraints_satisfied is True
    assert result.evidence["matched_values"] == [1]


def test_join_validator_normalizes_typed_key_values_from_authoritative_schema():
    obligation = _obligation(
        "matched_and_dangling_join_rows", "singer", "Singer_ID"
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "matched_and_dangling_join_rows",
        "singer",
        "Singer_ID",
        metadata=(
            (
                "standard_join_pairs",
                (("singer_in_concert", "Singer_ID", "singer", "Singer_ID"),),
            ),
        ),
    )
    world = SimpleNamespace(database={
        "singer_in_concert": [{"Singer_ID": "1"}, {"Singer_ID": "900032"}],
        "singer": [{"Singer_ID": 1}, {"Singer_ID": 2}],
    })

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["matched_values"] == ["1"]
    assert result.evidence["dangling_left_values"] == ["900032"]


def _join_drift_obligation(standard_pairs, student_pairs):
    obligation = _obligation(
        "standard_join_equal_student_join_unequal", "left_table", "id"
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "standard_join_equal_student_join_unequal",
        metadata=(
            ("standard_join_pairs", standard_pairs),
            ("student_join_pairs", student_pairs),
        ),
    )
    return obligation


def test_join_drift_validator_compares_complete_row_pair_predicates():
    world = SimpleNamespace(database={
        "customers": [{"id": 1}],
        "orders": [{"customer_id": 1, "id": 9}],
    })
    obligation = _join_drift_obligation(
        (("customers", "id", "orders", "customer_id"),),
        (("customers", "id", "orders", "id"),),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["standard_truth"] is True
    assert result.evidence["student_truth"] is False
    assert result.evidence["divergence_direction"] == "standard_only"


def test_join_drift_validator_detects_removed_conjunct_as_student_only_path():
    world = SimpleNamespace(database={
        "enrollment": [{"id": 1, "year": 2024}],
        "exam": [{"id": 1, "year": 2025}],
    })
    obligation = _join_drift_obligation(
        (
            ("enrollment", "id", "exam", "id"),
            ("enrollment", "year", "exam", "year"),
        ),
        (("enrollment", "id", "exam", "id"),),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["standard_truth"] is False
    assert result.evidence["student_truth"] is True
    assert result.evidence["divergence_direction"] == "student_only"


def test_join_drift_validator_uses_independent_roles_for_self_join():
    world = SimpleNamespace(database={
        "employee": [
            {"id": 1, "manager_id": 2},
            {"id": 2, "manager_id": 99},
        ],
    })
    obligation = _join_drift_obligation(
        (("employee", "manager_id", "employee", "id"),),
        (("employee", "id", "employee", "id"),),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["left_table"] == "employee"
    assert result.evidence["right_table"] == "employee"
    assert result.evidence["standard_truth"] != result.evidence["student_truth"]


def test_group_validator_requires_duplicate_and_split_group_keys():
    world = SimpleNamespace(database={
        "sales": [
            {"dept": "A"}, {"dept": "A"}, {"dept": "B"},
        ]
    })

    obligation = _obligation("group_grain_split", "sales", "dept")
    obligation.hard_constraints[0] = ConstraintSpec(
        "group_grain_split", "sales", "dept",
        metadata=(("standard_group_columns", ("dept",)),),
    )
    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["overlap_values"] == ["A"]
    assert result.evidence["outer_only_values"] == ["B"]
    assert result.evidence["group_key_counts"] == {"('A',)": 2, "('B',)": 1}


def test_group_validator_requires_standard_and_student_partitions_to_differ():
    obligation = _obligation("group_grain_split", "sales", "region")
    obligation.diff_type = "grouping_grain_too_fine"
    obligation.hard_constraints[0] = ConstraintSpec(
        "group_grain_split",
        "sales",
        "region",
        metadata=(
            ("standard_group_columns", ("dept",)),
            ("student_group_columns", ("dept", "region")),
        ),
    )
    split_world = SimpleNamespace(database={
        "sales": [
            {"dept": "A", "region": "north"},
            {"dept": "A", "region": "south"},
            {"dept": "B", "region": "north"},
        ]
    })
    aligned_world = SimpleNamespace(database={
        "sales": [
            {"dept": "A", "region": "north"},
            {"dept": "A", "region": "north"},
            {"dept": "B", "region": "south"},
        ]
    })

    split = validate_obligation(split_world, obligation)
    aligned = validate_obligation(aligned_world, obligation)

    assert split.constraints_satisfied is True
    assert split.evidence["standard_groups_split_by_student"] == [["A"]]
    assert aligned.constraints_satisfied is False


def test_group_validator_treats_missing_group_by_as_global_group():
    world = SimpleNamespace(database={
        "sales": [
            {"dept": "A"},
            {"dept": "B"},
            {"dept": "B"},
        ]
    })
    added_group = _obligation("group_grain_split", "sales", "dept")
    added_group.diff_type = "grouping_grain_too_fine"
    added_group.hard_constraints[0] = ConstraintSpec(
        "group_grain_split",
        "sales",
        "dept",
        metadata=(
            ("standard_group_columns", ()),
            ("student_group_columns", ("dept",)),
        ),
    )
    removed_group = _obligation("group_grain_split", "sales", "dept")
    removed_group.diff_type = "grouping_grain_too_coarse"
    removed_group.hard_constraints[0] = ConstraintSpec(
        "group_grain_split",
        "sales",
        "dept",
        metadata=(
            ("standard_group_columns", ("dept",)),
            ("student_group_columns", ()),
        ),
    )

    added = validate_obligation(world, added_group)
    removed = validate_obligation(world, removed_group)

    assert added.constraints_satisfied is True
    assert added.evidence["standard_groups_split_by_student"] == [[]]
    assert removed.constraints_satisfied is True
    assert removed.evidence["student_groups_split_by_standard"] == [[]]


def test_aggregate_validator_requires_a_group_at_the_numeric_boundary():
    world = SimpleNamespace(database={
        "sales": [
            {"dept": "A", "amount": 10},
            {"dept": "A", "amount": 20},
            {"dept": "A", "amount": 30},
            {"dept": "B", "amount": 40},
        ]
    })

    obligation = _obligation("aggregate_boundary_group", "sales", "amount", 3)
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group", "sales", "amount", 3,
        metadata=(
            ("standard_aggregate_function", "COUNT"),
            ("standard_aggregate_argument", "*"),
            ("standard_group_columns", ("dept",)),
        ),
    )
    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["aggregate_group_counts"]["('A',)"] == 3


def test_aggregate_validator_accepts_count_column_to_count_star_null_path():
    world = SimpleNamespace(database={
        "employee": [
            {"manager_id": None},
            {"manager_id": 7},
            {"manager_id": 8},
        ]
    })
    obligation = _obligation(
        "aggregate_boundary_group", "employee", '"manager_id"'
    )
    obligation.diff_type = "aggregate_argument_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group",
        "employee",
        '"manager_id"',
        metadata=(
            ("standard_aggregate_function", "COUNT"),
            ("standard_aggregate_argument", '"manager_id"'),
            ("student_aggregate_argument", "*"),
            ("standard_group_columns", ()),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["nullable_argument_column"] == "manager_id"
    assert result.evidence["null_row_indexes"] == [0]


def test_aggregate_validator_rejects_count_column_to_count_star_without_null():
    world = SimpleNamespace(database={
        "employee": [{"manager_id": 7}, {"manager_id": 8}],
    })
    obligation = _obligation(
        "aggregate_boundary_group", "employee", '"manager_id"'
    )
    obligation.diff_type = "aggregate_argument_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group",
        "employee",
        '"manager_id"',
        metadata=(
            ("standard_aggregate_function", "COUNT"),
            ("standard_aggregate_argument", '"manager_id"'),
            ("student_aggregate_argument", "*"),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert "aggregate_nullable_argument_path_missing" in result.diagnostics


def test_joined_count_validator_rejects_base_boundary_multiplied_by_join():
    world = SimpleNamespace(
        database={
            "Highschooler": [
                {"id": 100, "name": "Alice", "grade": 6},
                {"id": 100, "name": "Bob", "grade": 6},
            ],
            "Friend": [
                {"student_id": 100, "friend_id": 1},
                {"student_id": 100, "friend_id": 2},
            ],
        },
        execution={
            "validation_context": {
                "execution_backend": "sqlite",
                "standard_sql": (
                    "SELECT T2.name FROM Friend T1 "
                    "JOIN Highschooler T2 ON T1.student_id = T2.id "
                    "WHERE T2.grade > 5 GROUP BY T1.student_id "
                    "HAVING COUNT(*) >= 2"
                ),
            }
        },
    )
    obligation = _obligation(
        "aggregate_boundary_group", "Friend", "COUNT(*)", 2
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group",
        "Friend",
        "COUNT(*)",
        2,
        metadata=(
            ("standard_aggregate_function", "COUNT"),
            ("standard_aggregate_argument", "*"),
            ("standard_group_columns", ("student_id",)),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert result.evidence["aggregate_cardinality_scope"] == "post_join"
    assert 4 in result.evidence["post_join_aggregate_values"].values()
    assert "aggregate_boundary_group_missing_after_join" in result.diagnostics


def test_scalar_subquery_validator_requires_boundary_to_survive_full_query_path():
    sql = (
        "SELECT t2.MakeId FROM cars_data t1 JOIN car_names t2 "
        "ON t1.Id = t2.MakeId WHERE t1.Horsepower > "
        "(SELECT MIN(Horsepower) FROM cars_data) "
        "AND t1.Cylinders < 4"
    )
    obligation = _obligation(
        "scalar_subquery_boundary_path", "cars_data", "Horsepower"
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "scalar_subquery_boundary_path",
        "cars_data",
        "Horsepower",
        metadata=(
            ("standard_scalar_aggregate_function", "MIN"),
            ("standard_scalar_source_table", "cars_data"),
            ("standard_scalar_source_column", "horsepower"),
        ),
    )
    reachable = SimpleNamespace(
        database={
            "car_names": [{"MakeId": 1}, {"MakeId": 2}],
            "cars_data": [
                {"Id": 1, "Horsepower": 51, "Cylinders": 3},
                {"Id": 2, "Horsepower": 50, "Cylinders": 3},
            ],
        },
        execution={"validation_context": {"standard_sql": sql}},
    )
    filtered_out = SimpleNamespace(
        database={
            "car_names": [{"MakeId": 1}, {"MakeId": 2}],
            "cars_data": [
                {"Id": 1, "Horsepower": 51, "Cylinders": 3},
                {"Id": 2, "Horsepower": 50, "Cylinders": 5},
            ],
        },
        execution={"validation_context": {"standard_sql": sql}},
    )

    reachable_result = validate_obligation(reachable, obligation)
    filtered_result = validate_obligation(filtered_out, obligation)

    assert reachable_result.constraints_satisfied is True
    assert reachable_result.evidence["scalar_boundary_scope"] == "query_path"
    assert reachable_result.evidence["boundary_path_rows"] == [[50, 50]]
    assert filtered_result.constraints_satisfied is False
    assert "scalar_subquery_boundary_path_missing" in filtered_result.diagnostics


def test_filtered_aggregate_validator_requires_boundary_to_cross_having_path():
    standard = (
        "SELECT t2.School_name FROM endowment t1 "
        "JOIN school t2 ON t1.School_id = t2.School_id "
        "WHERE t1.amount > 8.5 GROUP BY t1.School_id "
        "HAVING COUNT(*) > 1"
    )
    student = standard.replace("amount > 8.5", "amount >= 8.5")
    obligation = _obligation(
        "filtered_aggregate_boundary_path", "endowment", "amount", 8.5
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "filtered_aggregate_boundary_path",
        "endowment",
        "amount",
        8.5,
        metadata=(
            ("having_aggregate_function", "COUNT"),
            ("having_boundary", 1),
            ("having_operator", "GT"),
            ("standard_boundary_included", False),
            ("student_boundary_included", True),
        ),
    )

    def world(endowments, schools):
        return SimpleNamespace(
            database={"endowment": endowments, "School": schools},
            execution={"validation_context": {
                "execution_backend": "sqlite",
                "standard_sql": standard,
                "student_sql": student,
            }},
        )

    reachable = world(
        [
            {"endowment_id": 1, "School_id": 1, "amount": 9.5},
            {"endowment_id": 2, "School_id": 1, "amount": 8.5},
        ],
        [{"School_id": 1, "School_name": "A"}],
    )
    split_groups = world(
        [
            {"endowment_id": 1, "School_id": 1, "amount": 9.5},
            {"endowment_id": 2, "School_id": 2, "amount": 8.5},
        ],
        [
            {"School_id": 1, "School_name": "A"},
            {"School_id": 2, "School_name": "B"},
        ],
    )

    reachable_result = validate_obligation(reachable, obligation)
    split_result = validate_obligation(split_groups, obligation)

    assert reachable_result.constraints_satisfied is True
    assert reachable_result.evidence["filtered_aggregate_scope"] == "query_path"
    assert reachable_result.evidence["standard_path_groups"] == []
    assert reachable_result.evidence["student_path_groups"] == [[1, 2]]
    assert split_result.constraints_satisfied is False
    assert "filtered_aggregate_boundary_path_missing" in split_result.diagnostics


def test_set_validator_executes_grouped_right_branch_for_except_union():
    standard = (
        "SELECT name FROM storm EXCEPT SELECT t1.name FROM storm t1 "
        "JOIN affected_region t2 ON t1.storm_id = t2.storm_id "
        "GROUP BY t1.storm_id HAVING COUNT(*) >= 2"
    )
    student = standard.replace(" EXCEPT ", " UNION ")
    obligation = _obligation("set_left_right_overlap")
    obligation.hard_constraints[0] = ConstraintSpec(
        "set_left_right_overlap",
        metadata=(
            ("standard_op", "EXCEPT"),
            ("student_op", "UNION"),
            ("standard_modifier", "DISTINCT"),
            ("student_modifier", "DISTINCT"),
            ("standard_projection_columns", ("name",)),
        ),
    )
    world = SimpleNamespace(
        database={
            "storm": [
                {"storm_id": 1, "name": "A"},
                {"storm_id": 2, "name": "B"},
            ],
            "affected_region": [
                {"region_id": 1, "storm_id": 1},
                {"region_id": 2, "storm_id": 1},
            ],
        },
        execution={"validation_context": {
            "execution_backend": "sqlite",
            "standard_sql": standard,
            "student_sql": student,
        }},
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["source"] == "query_branches"
    assert result.evidence["right_branch_rows"] == [["A"]]
    assert result.evidence["simulated_standard_result"] == [["B"]]
    assert result.evidence["simulated_student_result"] == [["A"], ["B"]]


def test_aggregate_function_validator_requires_different_group_results():
    obligation = _obligation(
        "aggregate_function_separation", "projects", "hours"
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_function_separation",
        "projects",
        "hours",
        metadata=(
            ("standard_aggregate_function", "MAX"),
            ("student_aggregate_function", "MIN"),
            ("standard_aggregate_argument", "hours"),
            ("student_aggregate_argument", "hours"),
            ("standard_group_columns", ()),
        ),
    )
    separated = SimpleNamespace(database={
        "projects": [{"hours": 1}, {"hours": 44}, {"hours": 4}],
    })
    collapsed = SimpleNamespace(database={
        "projects": [{"hours": 4}, {"hours": 4}],
    })

    separated_result = validate_obligation(separated, obligation)
    collapsed_result = validate_obligation(collapsed, obligation)

    assert separated_result.constraints_satisfied is True
    assert separated_result.evidence["aggregate_function_values"]["()"] == {
        "standard": 44,
        "student": 1,
    }
    assert collapsed_result.constraints_satisfied is False
    assert "aggregate_function_results_not_separated" in collapsed_result.diagnostics


def test_aggregate_metadata_preserves_function_argument_and_group_columns():
    obligation = _obligation("aggregate_boundary_group", "sales", "amount", 3)
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group", "sales", "amount", 3,
        metadata=(
            ("standard_aggregate_function", "SUM"),
            ("standard_aggregate_argument", "amount"),
            ("standard_group_columns", ("dept", "region")),
        ),
    )
    assert dict(obligation.hard_constraints[0].metadata)["standard_aggregate_function"] == "SUM"
    assert dict(obligation.hard_constraints[0].metadata)["standard_group_columns"] == ("dept", "region")


def test_sum_validator_checks_aggregate_value_not_row_count():
    world = SimpleNamespace(database={
        "sales": [
            {"dept": "A", "amount": 10},
            {"dept": "A", "amount": 20},
            {"dept": "B", "amount": 30},
        ]
    })
    obligation = _obligation("aggregate_boundary_group", "sales", "amount", 30)
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group", "sales", "amount", 30,
        metadata=(
            ("standard_aggregate_function", "SUM"),
            ("standard_aggregate_argument", "amount"),
            ("standard_group_columns", ("dept",)),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["aggregate_values"]["('A',)"] == 30
    assert result.evidence["aggregate_function"] == "SUM"


def test_count_distinct_validator_ignores_null_and_duplicate_values():
    world = SimpleNamespace(database={
        "events": [
            {"dept": "A", "event_id": 1},
            {"dept": "A", "event_id": 1},
            {"dept": "A", "event_id": 2},
            {"dept": "A", "event_id": None},
        ]
    })
    obligation = _obligation("aggregate_boundary_group", "events", "event_id", 2)
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group", "events", "event_id", 2,
        metadata=(
            ("standard_aggregate_function", "COUNT"),
            ("standard_aggregate_argument", "event_id"),
            ("standard_aggregate_distinct", True),
            ("standard_group_columns", ("dept",)),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["aggregate_values"]["('A',)"] == 2
    assert result.evidence["aggregate_distinct"] is True


def test_avg_validator_ignores_null_values():
    world = SimpleNamespace(database={
        "scores": [
            {"dept": "A", "score": 10},
            {"dept": "A", "score": None},
            {"dept": "A", "score": 20},
        ]
    })
    obligation = _obligation("aggregate_boundary_group", "scores", "score", 15)
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group", "scores", "score", 15,
        metadata=(
            ("standard_aggregate_function", "AVG"),
            ("standard_aggregate_argument", "score"),
            ("standard_group_columns", ("dept",)),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["aggregate_values"]["('A',)"] == 15


def test_min_max_validator_does_not_crash_on_heterogeneous_legacy_values():
    world = SimpleNamespace(database={
        "tracks": [
            {"milliseconds": 9},
            {"milliseconds": "legacy_probe_value"},
        ]
    })
    obligation = _obligation(
        "aggregate_boundary_group",
        "tracks",
        "milliseconds",
        "legacy_probe_value",
    )
    obligation.hard_constraints[0] = ConstraintSpec(
        "aggregate_boundary_group",
        "tracks",
        "milliseconds",
        "legacy_probe_value",
        metadata=(
            ("standard_aggregate_function", "MAX"),
            ("standard_aggregate_argument", "milliseconds"),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["aggregate_values"]["()"] == "legacy_probe_value"
    assert result.evidence["heterogeneous_extreme_groups"] == ["()"]
    assert "aggregate_mixed_types_ordered_deterministically" in result.diagnostics


def test_window_validator_requires_declared_partitions_and_order_ties():
    world = SimpleNamespace(database={
        "scores": [
            {"dept": "A", "score": 10},
            {"dept": "A", "score": 10},
            {"dept": "B", "score": 20},
        ]
    })
    obligation = _obligation("window_partitions_and_ties", "scores", "score")
    obligation.hard_constraints[0] = ConstraintSpec(
        "window_partitions_and_ties", "scores", "score",
        metadata=(
            ("standard_window_partition", ("dept",)),
            ("standard_window_order", "score ASC"),
        ),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["partition_count"] == 2
    assert result.evidence["order_tie_count"] == 1


def test_window_value_validator_requires_distinct_order_path_inside_partition():
    obligation = _obligation("window_partitions_and_ties", "scores", "score")
    obligation.diff_type = "window_function_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "window_partitions_and_ties",
        "scores",
        "score",
        metadata=(
            ("standard_window_partition", ("dept",)),
            ("standard_window_order", "ORDER BY score"),
            ("student_window_order", "ORDER BY score"),
            ("standard_window_frame", "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"),
            ("student_window_frame", "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"),
            ("standard_window_function", "FIRST_VALUE"),
            ("student_window_function", "LAST_VALUE"),
        ),
    )
    world = SimpleNamespace(database={
        "scores": [
            {"dept": "A", "score": 10},
            {"dept": "A", "score": 10},
            {"dept": "B", "score": 20},
        ]
    })

    missing = validate_obligation(world, obligation)
    assert missing.constraints_satisfied is False
    assert missing.diagnostics == ["window_distinct_order_path_missing"]

    world.database["scores"].insert(2, {"dept": "A", "score": 30})
    satisfied = validate_obligation(world, obligation)
    assert satisfied.constraints_satisfied is True
    assert satisfied.evidence["distinct_order_partition_count"] == 1


def test_window_null_placement_validator_requires_null_and_non_null_paths():
    obligation = _obligation("window_partitions_and_ties", "scores", "score")
    obligation.diff_type = "window_over_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "window_partitions_and_ties",
        "scores",
        "score",
        metadata=(
            ("standard_window_order", "ORDER BY score"),
            ("student_window_order", "ORDER BY score NULLS LAST"),
            ("standard_window_order_columns", ("score",)),
            ("student_window_order_columns", ("score",)),
            ("standard_window_order_items", (("score", False, True),)),
            ("student_window_order_items", (("score", False, False),)),
        ),
    )
    world = SimpleNamespace(database={
        "scores": [{"score": 10}, {"score": 20}, {"score": 30}],
    })

    missing = validate_obligation(world, obligation)
    assert missing.constraints_satisfied is False
    assert missing.diagnostics == ["window_null_order_path_missing"]

    world.database["scores"][0]["score"] = None
    satisfied = validate_obligation(world, obligation)
    assert satisfied.constraints_satisfied is True
    assert satisfied.evidence["nulls_first_changed"] is True
    assert satisfied.evidence["null_order_path"] is True


def test_partition_only_window_validator_compares_row_equivalence_classes():
    obligation = _obligation("window_partitions_and_ties", "sales", "amount")
    obligation.diff_type = "window_over_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "window_partitions_and_ties",
        "sales",
        "amount",
        metadata=(
            ("standard_window_partition", ("dept", "region")),
            ("student_window_partition", ("dept",)),
            ("standard_window_order", ""),
            ("student_window_order", ""),
        ),
    )
    world = SimpleNamespace(database={
        "sales": [
            {"dept": "A", "region": "north", "amount": 10},
            {"dept": "A", "region": "south", "amount": 20},
        ],
    })

    separated = validate_obligation(world, obligation)
    assert separated.constraints_satisfied is True
    assert separated.evidence["partition_relation_changed"] is True

    world.database["sales"][1]["region"] = "north"
    missing = validate_obligation(world, obligation)
    assert missing.constraints_satisfied is False
    assert missing.diagnostics == ["window_partition_relation_missing"]


def _order_obligation(diff_type, standard_keys, student_keys):
    obligation = _obligation("order_key_separation", "employee")
    obligation.diff_type = diff_type
    obligation.hard_constraints[0] = ConstraintSpec(
        "order_key_separation",
        "employee",
        metadata=(
            ("standard_order_keys", standard_keys),
            ("student_order_keys", student_keys),
            ("standard_source_table", "employee"),
        ),
    )
    return obligation


def test_order_direction_validator_requires_two_values_at_changed_key():
    world = SimpleNamespace(database={
        "employee": [
            {"salary": 10, "name": "A"},
            {"salary": 20, "name": "B"},
        ]
    })
    obligation = _order_obligation(
        "order_direction_changed",
        (("salary", False),),
        (("salary", True),),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["discriminator_column"] == "salary"
    assert result.evidence["distinguishing_row_indexes"] == [0, 1]


def test_order_tiebreaker_validator_requires_primary_tie_and_secondary_split():
    world = SimpleNamespace(database={
        "employee": [
            {"dept": "A", "salary": 20},
            {"dept": "A", "salary": 10},
            {"dept": "B", "salary": 30},
        ]
    })
    obligation = _order_obligation(
        "order_by_tiebreaker_missing",
        (("dept", False), ("salary", False)),
        (("dept", False),),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["prefix_columns"] == ["dept"]
    assert result.evidence["distinguishing_row_indexes"] == [0, 1]


def test_order_added_key_validator_uses_student_discriminator():
    world = SimpleNamespace(database={
        "employee": [
            {"dept": "A", "salary": 20},
            {"dept": "A", "salary": 10},
            {"dept": "B", "salary": 30},
        ]
    })
    obligation = _order_obligation(
        "order_by_key_added",
        (("dept", False),),
        (("dept", False), ("salary", True)),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["discriminator_column"] == "salary"


def test_order_validator_fails_closed_for_expression_key():
    world = SimpleNamespace(database={
        "employee": [{"name": "A"}, {"name": "B"}]
    })
    obligation = _order_obligation(
        "order_direction_changed",
        (("LOWER(name)", False),),
        (("LOWER(name)", True),),
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert result.diagnostics == ["order_expression_not_supported"]


def _boolean_obligation():
    obligation = _obligation("boolean_truth_table", "t")
    obligation.diff_type = "logical_operator_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "boolean_truth_table",
        "t",
        metadata=(
            ("standard_predicate_sql", "a = 1 AND b = 1"),
            ("student_predicate_sql", "a = 1 OR b = 1"),
            ("standard_source_table", "t"),
        ),
    )
    return obligation


def test_boolean_validator_requires_full_binary_truth_table():
    world = SimpleNamespace(database={
        "t": [
            {"a": 1, "b": 1},
            {"a": 1, "b": 0},
            {"a": 0, "b": 1},
            {"a": 0, "b": 0},
        ]
    })

    result = validate_obligation(world, _boolean_obligation())

    assert result.constraints_satisfied is True
    assert result.evidence["full_binary_truth_table"] is True
    assert result.evidence["truth_assignments"] == [
        ["F", "F"], ["F", "T"], ["T", "F"], ["T", "T"]
    ]
    assert result.evidence["distinguishing_row_indexes"] == [1, 2]


def test_boolean_validator_rejects_partial_independent_truth_table():
    world = SimpleNamespace(database={
        "t": [
            {"a": 1, "b": 1},
            {"a": 1, "b": 0},
            {"a": 0, "b": 1},
        ]
    })

    result = validate_obligation(world, _boolean_obligation())

    assert result.constraints_satisfied is False
    assert result.diagnostics == ["boolean_truth_table_not_materialized"]


def test_set_validator_requires_declared_overlap_and_exclusive_paths():
    obligation = _obligation("set_left_right_overlap")
    obligation.diff_type = "set_operator_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "set_left_right_overlap",
        metadata=(
            ("standard_op", "INTERSECT"),
            ("student_op", "UNION"),
            ("standard_left_source_table", "left_branch"),
            ("standard_right_source_table", "right_branch"),
            ("standard_projection_columns", ("id",)),
        ),
    )
    world = SimpleNamespace(database={
        "left_branch": [{"id": 1}, {"id": 2}],
        "right_branch": [{"id": 2}, {"id": 3}],
    })

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["overlap_count"] == 1
    assert result.evidence["left_only_count"] == 1
    assert result.evidence["right_only_count"] == 1
    assert result.evidence["required_paths"] == ["left_only", "overlap", "right_only"]


def test_case_validator_requires_each_when_and_an_unmatched_row():
    obligation = _obligation("case_unmatched_and_branch_rows", "employee")
    obligation.diff_type = "case_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "case_unmatched_and_branch_rows",
        "employee",
        metadata=(
            ("standard_case_when_predicates", ("salary < 50", "salary >= 50")),
            ("standard_source_table", "employee"),
        ),
    )
    world = SimpleNamespace(database={
        "employee": [
            {"salary": 10},
            {"salary": 50},
            {"salary": 80},
        ]
    })

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert result.diagnostics == ["case_branch_or_unmatched_path_missing"]


def test_case_validator_accepts_when_paths_and_unmatched_path():
    obligation = _obligation("case_unmatched_and_branch_rows", "employee")
    obligation.diff_type = "case_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "case_unmatched_and_branch_rows",
        "employee",
        metadata=(
            ("standard_case_when_predicates", ("salary < 50", "salary >= 100")),
            ("standard_source_table", "employee"),
        ),
    )
    world = SimpleNamespace(database={
        "employee": [
            {"salary": 10},
            {"salary": 60},
            {"salary": 120},
        ]
    })

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True
    assert result.evidence["branch_hit_counts"] == [1, 1]
    assert result.evidence["unmatched_row_indexes"] == [1]


def test_membership_validator_requires_overlap_and_outer_only_path():
    obligation = _obligation("subquery_membership_paths", "employee", "id")
    obligation.diff_type = "correlated_predicate_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "subquery_membership_paths",
        "employee",
        "id",
        metadata=(
            ("standard_source_table", "employee"),
            ("standard_membership_table", "bonus"),
            ("standard_outer_column", "id"),
            ("standard_membership_column", "employee_id"),
        ),
    )
    world = SimpleNamespace(database={
        "employee": [{"id": 1}, {"id": 9}],
        "bonus": [{"employee_id": 1}],
    })

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is True


def test_membership_validator_enforces_declared_not_in_null_path():
    obligation = _obligation("subquery_membership_paths", "employee", "id")
    obligation.diff_type = "null_sensitive_antijoin_equivalence"
    obligation.hard_constraints[0] = ConstraintSpec(
        "subquery_membership_paths",
        "employee",
        "id",
        metadata=(
            ("standard_source_table", "employee"),
            ("standard_membership_table", "employee"),
            ("standard_outer_column", "id"),
            ("standard_membership_column", "manager_id"),
            ("require_inner_null", True),
        ),
    )
    world = SimpleNamespace(database={
        "employee": [
            {"id": 1, "manager_id": 1},
            {"id": 2, "manager_id": 3},
        ]
    })

    missing = validate_obligation(world, obligation)
    assert missing.constraints_satisfied is False
    assert missing.diagnostics == ["subquery_membership_null_path_missing"]

    world.database["employee"].append({"id": 4, "manager_id": None})
    satisfied = validate_obligation(world, obligation)
    assert satisfied.constraints_satisfied is True
    assert satisfied.evidence["inner_null_count"] == 1


def test_in_list_validator_requires_declared_symmetric_difference_value():
    obligation = _obligation("in_list_membership_paths", "students", "major_id")
    obligation.diff_type = "in_list_member_removed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "in_list_membership_paths",
        "students",
        "major_id",
        metadata=(
            ("standard_in_values", ("A", "B", "C")),
            ("student_in_values", ("A", "B")),
            ("distinguishing_values", ("C",)),
        ),
    )
    world = SimpleNamespace(database={
        "students": [{"major_id": "A"}, {"major_id": "B"}],
    })

    missing = validate_obligation(world, obligation)
    assert missing.constraints_satisfied is False
    assert missing.evidence["materialized_distinguishing_values"] == []

    world.database["students"].append({"major_id": "C"})
    satisfied = validate_obligation(world, obligation)
    assert satisfied.constraints_satisfied is True
    assert satisfied.evidence["materialized_distinguishing_values"] == ["C"]


def test_projection_shape_validator_uses_executed_result_widths():
    obligation = _obligation("projection_shape_paths")
    world = SimpleNamespace(
        database={"employee": [{"id": 1, "name": "Ada"}]},
        execution={
            "attempts": [{
                "standard_result": [(1, "Ada")],
                "student_result": [(1,)],
            }]
        },
    )

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.evidence["standard_result_widths"] == [2]
    assert result.evidence["student_result_widths"] == [1]


def test_inline_recursive_validator_uses_materialized_cte_result_paths():
    obligation = _obligation("cte_base_recursive_orphan_paths")
    world = SimpleNamespace(
        database={},
        execution={
            "attempts": [{
                "standard_result": [(1,), (2,), (3,)],
                "student_result": [(1,), (2,)],
            }]
        },
    )

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.evidence["inline_recursive"] is True
    assert result.evidence["standard_distinct_row_count"] == 3


def test_distinct_validator_rejects_source_duplicates_when_query_result_is_empty():
    obligation = _obligation("duplicate_projected_tuple", "logs", "num")
    obligation.diff_type = "distinct_changed"
    world = SimpleNamespace(
        database={"logs": [{"num": 7}, {"num": 7}]},
        execution={
            "attempts": [{
                "standard_result": [],
                "student_result": [],
            }]
        },
    )

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert result.evidence["source"] == "executed_projection"
    assert result.diagnostics == ["duplicate_projection_not_observed"]


def test_distinct_validator_requires_observable_duplicate_projection():
    obligation = _obligation("duplicate_projected_tuple", "logs", "num")
    obligation.diff_type = "distinct_changed"
    world = SimpleNamespace(
        database={"logs": [{"num": 7}, {"num": 7}]},
        execution={
            "attempts": [{
                "standard_result": [(7,)],
                "student_result": [(7,), (7,)],
            }]
        },
    )

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.evidence["standard_duplicate_count"] == 0
    assert result.evidence["student_duplicate_count"] == 1


def test_aggregate_distinct_validator_uses_duplicate_input_values():
    obligation = _obligation("duplicate_projected_tuple", "logs", "num")
    obligation.diff_type = "aggregate_distinct_changed"
    world = SimpleNamespace(
        database={"logs": [{"num": 7}, {"num": 7}]},
        execution={
            "attempts": [{
                "standard_result": [(1,)],
                "student_result": [(2,)],
            }]
        },
    )

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.evidence["source"] == "physical_table"
    assert result.evidence["duplicate_values"] == {"7": 2}


def test_nested_distinct_validator_uses_duplicate_projected_input_tuple():
    obligation = _obligation("duplicate_projected_tuple", "orders", "")
    obligation.diff_type = "distinct_changed"
    obligation.hard_constraints[0] = ConstraintSpec(
        "duplicate_projected_tuple",
        "orders",
        metadata=(
            ("query_scope", "cte:tb1"),
            ("standard_projection_columns", ("customer_id", "product_name")),
        ),
    )
    world = SimpleNamespace(
        database={
            "orders": [
                {"customer_id": 1, "product_name": "A"},
                {"customer_id": 1, "product_name": "B"},
                {"customer_id": 1, "product_name": "B"},
            ]
        },
        execution={
            "attempts": [{
                "standard_result": [(1,)],
                "student_result": [],
            }]
        },
    )

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.evidence["source"] == "nested_query_input"
    assert result.evidence["duplicate_tuples"] == {"(1, 'B')": 2}


def test_set_validator_uses_recursive_execution_duplicate_path_without_branch_tables():
    obligation = _obligation("set_left_right_overlap")
    obligation.hard_constraints[0] = ConstraintSpec(
        "set_left_right_overlap",
        metadata=(
            ("standard_modifier", "ALL"),
            ("student_modifier", "DISTINCT"),
        ),
    )
    world = SimpleNamespace(
        database={"edges": []},
        execution={
            "attempts": [{
                "standard_result": [(1,), (2,), (3,), (4,), (4,)],
                "student_result": [(1,), (2,), (3,), (4,)],
            }]
        },
    )

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.evidence["source"] == "executed_recursive_set"
    assert result.evidence["standard_duplicate_count"] == 1


def test_boundary_validator_requires_exact_boundary_value():
    obligation = DistinguishingObligation(
        id="obligation_boundary",
        diff_id="diff_boundary",
        diff_type="comparison_operator_changed",
        clause="WHERE",
        knowledge_point_id="where-comp",
        hard_constraints=[ConstraintSpec("boundary_tristate", "employee", "salary", 50000)],
    )
    world = SimpleNamespace(database={"employee": [{"salary": 49999}, {"salary": 50000}, {"salary": 50001}]})

    result = validate_obligation(world, obligation, execution_distinguished=True)

    assert result.constraints_satisfied is True
    assert result.execution_distinguished is True
    assert result.evidence["boundary"] == 50000


def test_null_validator_requires_both_null_and_non_null_paths():
    obligation = DistinguishingObligation(
        id="obligation_null",
        diff_id="diff_null",
        diff_type="null_equality_changed",
        clause="WHERE",
        knowledge_point_id="comp-null",
        hard_constraints=[ConstraintSpec("null_and_non_null_rows", "employee", "manager_id")],
    )
    world = SimpleNamespace(database={"employee": [{"manager_id": None}, {"manager_id": 1}]})

    assert validate_obligation(world, obligation).constraints_satisfied is True


def test_null_safe_column_validator_requires_all_four_same_row_paths():
    obligation = DistinguishingObligation(
        id="obligation_null_safe_columns",
        diff_id="diff_null_safe_columns",
        diff_type="comparison_operator_changed",
        clause="WHERE",
        knowledge_point_id="where",
        hard_constraints=[ConstraintSpec(
            "null_safe_comparison_paths",
            "employee",
            "manager_id",
            metadata=(
                ("standard_op", "NULLSAFEEQ"),
                ("student_op", "EQ"),
                ("standard_value_kind", "column"),
                ("student_value_kind", "column"),
                ("standard_right_column", "backup_id"),
                ("student_right_column", "backup_id"),
                ("same_right_column", True),
            ),
        )],
    )
    rows = [
        {"manager_id": None, "backup_id": None},
        {"manager_id": None, "backup_id": 1},
        {"manager_id": 2, "backup_id": 2},
        {"manager_id": 3, "backup_id": 4},
    ]

    complete = validate_obligation(
        SimpleNamespace(database={"employee": rows}),
        obligation,
    )
    missing_unequal = validate_obligation(
        SimpleNamespace(database={"employee": rows[:3]}),
        obligation,
    )

    assert complete.constraints_satisfied is True
    assert complete.evidence["divergent_row_indexes"] == [0]
    assert missing_unequal.constraints_satisfied is False


def test_regex_validator_fails_closed_for_invalid_pattern():
    obligation = DistinguishingObligation(
        id="obligation_regex",
        diff_id="diff_regex",
        diff_type="regex_pattern_changed",
        clause="PREDICATE",
        knowledge_point_id="regex",
        required_tables={"contacts"},
        hard_constraints=[ConstraintSpec(
            "regex_pattern_separation",
            "contacts",
            "mailid",
            metadata=(
                ("standard_pattern", "["),
                ("student_pattern", "^[A-Z]+$"),
            ),
        )],
    )
    world = SimpleNamespace(database={"contacts": [{"mailid": "ABC"}]})

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert result.diagnostics
    assert result.diagnostics[0].startswith("regex_evaluation_failed:invalid_regex_pattern")


def test_bounded_like_matching_respects_escaped_wildcards_and_ilike():
    assert like_matches("a%b", "axxb") is True
    assert like_matches(r"a\%b", "a%b") is True
    assert like_matches(r"a\%b", "axxb") is False
    assert like_matches(r"a\%b", "a%b", escape="") is False
    assert like_matches("a_b", "acb") is True
    assert like_matches("abc", "ABC", case_insensitive=True) is True


def test_bounded_like_matching_rejects_unbounded_input():
    with pytest.raises(ValueError, match="like_pattern_too_long"):
        like_matches("a" * 257, "a")

    with pytest.raises(ValueError, match="trailing_escape"):
        like_matches("abc\\", "abc")


def test_like_validator_fails_closed_for_invalid_pattern():
    obligation = DistinguishingObligation(
        id="obligation_like",
        diff_id="diff_like",
        diff_type="like_pattern_changed",
        clause="PREDICATE",
        knowledge_point_id="like",
        required_tables={"people"},
        hard_constraints=[ConstraintSpec(
            "like_pattern_separation",
            "people",
            "name",
            metadata=(
                ("standard_pattern", "abc\\"),
                ("student_pattern", "abc%"),
            ),
        )],
    )
    world = SimpleNamespace(database={"people": [{"name": "abc"}]})

    result = validate_obligation(world, obligation)

    assert result.constraints_satisfied is False
    assert result.diagnostics == [
        "like_evaluation_failed:invalid_like_pattern:trailing_escape"
    ]


def test_bounded_glob_matching_supports_wildcards_and_character_classes():
    assert glob_matches("a*", "abc") is True
    assert glob_matches("a?c", "abc") is True
    assert glob_matches("[ab]*", "bar") is True
    assert glob_matches("[ab]*", "car") is False


def test_bounded_glob_matching_rejects_unbounded_input():
    with pytest.raises(ValueError, match="glob_pattern_too_long"):
        glob_matches("a" * 257, "a")
