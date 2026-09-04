from __future__ import annotations

import json
from types import SimpleNamespace

import core.error_diagnosis as error_diagnosis_module
from core.error_diagnosis import (
    LOGICAL_STAGE_ORDER,
    RULE_CATALOG,
    diagnose_record,
    diagnose_record_with_llm,
    render_diagnostic_feedback,
    sanitize_public_package,
)
from core.parseval_data_generator import generate_and_compare


def _run(
    *,
    status="SUPPORTED",
    conclusion="NOT_EQUIVALENT",
    judge="WRONG",
    executed=True,
    is_equivalent=False,
    diffs=None,
    effects=None,
    mutations=None,
    dependencies=None,
    database=None,
    selected_world="world_01",
    worlds=None,
    standard_sql="SELECT title FROM course WHERE credits > 3",
    student_sql="SELECT title FROM course WHERE credits >= 3",
    boundary=None,
    guard=None,
    scope_metadata=None,
):
    data = {
        "status": status,
        "equivalence_conclusion": conclusion,
        "judge_status": judge,
        "ast_diffs": list(diffs or []),
        "obligation_effectiveness": list(effects or []),
        "selected_witness_world_id": selected_world,
        "witness_suite": {"worlds": list(worlds or [])},
        "only_in_standard_sample": [],
        "only_in_student_sample": [("extra",)] if conclusion == "NOT_EQUIVALENT" else [],
        "diagnostic_dependencies": list(dependencies or []),
    }
    if scope_metadata is not None:
        data["scope_metadata"] = scope_metadata
    if guard:
        data["verdict_guard"] = guard
    return SimpleNamespace(
        executed=executed,
        is_equivalent=is_equivalent,
        error=None,
        standard_sqlite=standard_sql,
        student_sqlite=student_sql,
        standard_rows=[],
        student_rows=[],
        standard_columns=["title"],
        student_columns=["title"],
        test_database=database or {},
        data_evidence=data,
        mutation_evidence={"tests": list(mutations or [])},
        ast_diffs=[],
        judge_status=judge,
        status=status,
        equivalence_conclusion=conclusion,
        boundary_evidence=boundary or {},
    )


def _boundary_run(*, effect_world="world_01"):
    atomic_id = "diff_atomic_boundary"
    obligation_id = "obligation_atomic_boundary"
    diffs = [
        {
            "diff_id": "diff_where_summary",
            "obligation_id": "obligation_where_summary",
            "clause": "WHERE",
            "diff_type": "where_changed",
            "standard_sql": "credits > 3",
            "student_sql": "credits >= 3",
        },
        {
            "diff_id": atomic_id,
            "obligation_id": obligation_id,
            "clause": "PREDICATE",
            "diff_type": "comparison_operator_changed",
            "standard_sql": "credits > 3",
            "student_sql": "credits >= 3",
            "standard_op": "GT",
            "student_op": "GTE",
            "value": 3,
            "column": "credits",
        },
    ]
    effects = [
        {
            "diff_id": atomic_id,
            "obligation_id": obligation_id,
            "world_id": effect_world,
            "constraints_satisfied": True,
            "distinguished": True,
            "pair_distinguished": True,
            "causal_attribution_verified": True,
            "standard_result": [("expected",)],
            "student_result": [("expected",), ("extra",)],
        }
    ]
    mutations = [
        {
            "diff_ids": [atomic_id],
            "obligation_ids": [obligation_id],
            "clause": "WHERE",
            "query_scope": "root",
            "binding_quality": "exact",
            "mutation_scope": ["WHERE"],
            "dependent_changes": [],
            "fixed_by_replacement": True,
            "replacement_exec_ok": True,
            "replacement_sql": "SELECT title FROM course WHERE credits > 3",
        }
    ]
    worlds = [
        {
            "world_id": "world_01",
            "execution": {
                "constraint_application": {
                    "applied": [
                        {
                            "table": "course",
                            "row_index": 1,
                            "column": "credits",
                            "diff_id": atomic_id,
                            "obligation_id": obligation_id,
                            "before": 4,
                            "after": 3,
                        }
                    ]
                }
            },
        }
    ]
    database = {
        "course": [
            {"course_id": 1, "title": "other", "credits": 5},
            {"course_id": 2, "title": "boundary", "credits": 3},
        ]
    }
    return _run(
        diffs=diffs,
        effects=effects,
        mutations=mutations,
        worlds=worlds,
        database=database,
    )


def _collect_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower()
            yield from _collect_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _collect_keys(nested)


def test_rule_catalog_is_the_declared_twenty_rule_mvp():
    assert len(RULE_CATALOG) == 20
    assert len({item.rule_id for item in RULE_CATALOG}) == 20
    assert {item.teaching_stage for item in RULE_CATALOG} == {"S1", "S2", "S3", "S4", "S5", "S6"}


def test_rich_correct_fast_path_is_operational_not_global_proof():
    run = _run(
        conclusion="NO_COUNTEREXAMPLE_FOUND",
        judge="CORRECT",
        is_equivalent=True,
        diffs=[{"diff_id": "ignored", "clause": "WHERE", "diff_type": "where_changed"}],
    )
    package = diagnose_record(sandbox_run=run, question="public question")
    public = package.to_dict()
    assert public["verdict"] == "CORRECT"
    assert public["diagnosis_status"] == "OPERATIONALLY_ACCEPTED"
    assert public["primary"] is None
    assert public["ordered_diff_pipeline"] == []
    assert "global equivalence is not claimed" in public["boundary_notes"][0]


def test_rich_undecided_cannot_be_promoted_by_legacy_true_boolean():
    run = _run(
        status="KNOWN_GAP",
        conclusion="UNDECIDED",
        judge="UNDECIDED",
        is_equivalent=True,
        guard={"reason": "unproven AST difference"},
    )
    public = diagnose_record(sandbox_run=run).to_dict()
    assert public["verdict"] == "UNDECIDED"
    assert public["primary"] is None
    assert public["witness"] is None


def test_correct_with_boundary_evidence_remains_undecided():
    run = _run(
        conclusion="NO_COUNTEREXAMPLE_FOUND",
        judge="CORRECT",
        is_equivalent=True,
        boundary={"reason": "dialect boundary"},
    )
    assert diagnose_record(sandbox_run=run).verdict == "UNDECIDED"


def test_real_phase1_boundary_shape_bundles_atomic_diff_and_exact_witness_row():
    run = _boundary_run()
    public = diagnose_record(
        sandbox_run=run,
        question="查询学分超过 3 学分的课程名称。",
        schema={
            "tables": [
                {
                    "name": "course",
                    "columns": [
                        {"name": "course_id", "primary_key": True, "nullable": False},
                        {"name": "title"},
                        {"name": "credits", "type": "INT", "nullable": False},
                    ],
                }
            ]
        },
    ).to_dict()
    assert public["verdict"] == "INCORRECT"
    assert public["primary"]["rule_id"] == "S2_BOUNDARY"
    assert public["primary"]["evidence_refs"]["diff_ids"] == ["diff_atomic_boundary"]
    assert public["secondary_count"] == 0
    assert public["witness"]["availability"] == "CAUSAL_VERIFIED"
    row = public["witness"]["cases"][0]["rows"][0]
    assert row["row_index"] == 1
    assert row["values"]["credits"] == 3
    assert row["values"]["course_id"] == 2


def test_public_package_recursively_excludes_reference_and_mutation_sql():
    run = _boundary_run()
    public = diagnose_record(sandbox_run=run, question="boundary question").to_dict()
    forbidden = {
        "answer_sql",
        "correct_sql",
        "standard_sql",
        "standard_node",
        "standard_fragment",
        "standard_result",
        "replacement_sql",
        "mutation_sql",
        "test_database",
        "witness_world",
    }
    assert forbidden.isdisjoint(set(_collect_keys(public)))
    encoded = json.dumps(public, ensure_ascii=False).lower()
    assert run.standard_sqlite.lower() not in encoded
    assert "select title from course where credits > 3" not in encoded


def test_public_sanitizer_redacts_atomic_reference_fragments_with_bounded_input():
    seen = 0

    def secrets():
        nonlocal seen
        for index in range(1000):
            seen += 1
            yield "credits > 3" if index == 0 else f"secret fragment {index}"

    public = sanitize_public_package(
        {"witness": {"value": "credits > 3"}},
        forbidden_values=secrets(),
    )

    assert public["witness"]["value"] == "[redacted reference content]"
    assert seen == error_diagnosis_module.MAX_FORBIDDEN_VALUES


def test_internal_package_is_auditable_but_still_reference_only():
    run = _boundary_run()
    package = diagnose_record(
        sandbox_run=run,
        schema={
            "tables": [
                {
                    "name": "course",
                    "columns": [
                        {"name": "course_id", "primary_key": True},
                        {"name": "credits", "type": "INTEGER"},
                    ],
                }
            ]
        },
    )
    internal = package.to_internal_dict()
    encoded = json.dumps(internal, ensure_ascii=False).lower()

    assert internal["schema_version"] == "phase2.internal.v1"
    assert internal["causal_dag"]["primary_fdp_candidate_id"] == package.primary.candidate_id
    assert internal["candidates"][0]["blocking"] is True
    assert "confidence" in internal["candidates"][0]
    assert internal["sanitizer_report"]["raw_sql_copied"] is False
    assert run.standard_sqlite.lower() not in encoded
    assert "replacement_sql" not in encoded
    assert "test_database" not in encoded


def test_exact_paired_scope_is_used_publicly_while_side_graph_stays_internal():
    diff_id = "diff_paired_boundary"
    scope_metadata = {
        "status": "COMPLETE",
        "scopes": [
            {
                "scope_id": "standard:root",
                "scope_kind": "ROOT",
                "side": "standard",
                "conceptual_scope_id": "paired:root",
                "metadata_complete": True,
            },
            {
                "scope_id": "student:root",
                "scope_kind": "ROOT",
                "side": "student",
                "conceptual_scope_id": "paired:root",
                "metadata_complete": True,
            },
        ],
        "composition_edges": [],
        "diff_bindings": [
            {
                "diff_id": diff_id,
                "scope_id": "standard:root",
                "side": "standard",
                "conceptual_scope_id": "paired:root",
                "binding_status": "EXACT_AST_PATH",
            },
            {
                "diff_id": diff_id,
                "scope_id": "student:root",
                "side": "student",
                "conceptual_scope_id": "paired:root",
                "binding_status": "EXACT_PAIRED_AST_PATH",
            },
        ],
        "limitations": [],
    }
    run = _run(
        diffs=[
            {
                "diff_id": diff_id,
                "obligation_id": "obligation_paired_boundary",
                "query_scope": "root",
                "clause": "WHERE",
                "diff_type": "comparison_operator_changed",
                "standard_sql": "credits > 3",
                "student_sql": "credits >= 3",
                "standard_op": "GT",
                "student_op": "GTE",
            }
        ],
                    effects=[
            {
                "diff_id": diff_id,
                "causal_attribution_verified": True,
                "distinguished": True,
            }
        ],
        scope_metadata=scope_metadata,
    )

    package = diagnose_record(sandbox_run=run)
    public = package.to_dict()
    internal = package.to_internal_dict()

    assert public["ordered_diff_pipeline"][0]["scope_id"] == "paired:root"
    assert package.primary is not None
    assert package.primary.scope_id == "paired:root"
    assert "scoped_query_graph" not in public
    assert "standard:root" not in json.dumps(public, ensure_ascii=False)
    assert internal["scoped_query_graph"]["status"] == "COMPLETE"
    assert {
        item["scope_id"] for item in internal["scoped_query_graph"]["scopes"]
    } == {"standard:root", "student:root"}


def test_conflicting_side_scope_pair_is_not_arbitrarily_merged():
    diff_id = "diff_scope_conflict"
    scope_metadata = {
        "status": "PARTIAL",
        "scopes": [
            {
                "scope_id": "standard:scope:nested",
                "scope_kind": "SUBQUERY",
                "side": "standard",
                "conceptual_scope_id": "paired:nested",
                "metadata_complete": False,
            },
            {
                "scope_id": "student:root",
                "scope_kind": "ROOT",
                "side": "student",
                "conceptual_scope_id": "paired:root",
                "metadata_complete": False,
            },
        ],
        "diff_bindings": [
            {
                "diff_id": diff_id,
                "scope_id": "standard:scope:nested",
                "side": "standard",
                "conceptual_scope_id": "paired:nested",
                "binding_status": "EXACT_AST_PATH",
            },
            {
                "diff_id": diff_id,
                "scope_id": "student:root",
                "side": "student",
                "conceptual_scope_id": "paired:root",
                "binding_status": "EXACT_AST_PATH",
            },
        ],
        "limitations": ["diff conceptual scope unresolved"],
    }
    run = _run(
        diffs=[
            {
                "diff_id": diff_id,
                "query_scope": "root",
                "clause": "WHERE",
                "diff_type": "comparison_operator_changed",
                "standard_sql": "credits > 3",
                "student_sql": "credits >= 3",
                "standard_op": "GT",
                "student_op": "GTE",
            }
        ],
        effects=[
            {
                "diff_id": diff_id,
                "causal_attribution_verified": True,
                "distinguished": True,
            }
        ],
        scope_metadata=scope_metadata,
    )

    package = diagnose_record(sandbox_run=run)
    public = package.to_dict()

    assert public["ordered_diff_pipeline"][0]["scope_id"] == (
        "unscoped:diff_scope_conflict"
    )
    assert package.primary is not None
    assert package.primary.scope_id == "unscoped:diff_scope_conflict"
    assert package.to_internal_dict()["scoped_query_graph"]["status"] == "PARTIAL"
    encoded = json.dumps(public, ensure_ascii=False)
    assert "standard:scope:nested" not in encoded
    assert "student:root" not in encoded


def test_correct_fast_path_does_not_build_the_phase2_scope_graph(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("scope graph should be skipped for correct verdicts")

    monkeypatch.setattr(
        error_diagnosis_module,
        "build_scoped_query_graph",
        fail_if_called,
    )
    run = _run(
        conclusion="NO_COUNTEREXAMPLE_FOUND",
        judge="CORRECT",
        is_equivalent=True,
    )

    package = diagnose_record(sandbox_run=run)

    assert package.verdict == "CORRECT"
    assert package.scoped_query_graph == {}


def test_global_duplicate_signal_does_not_cross_contaminate_nested_scope():
    diffs = [
        {
            "diff_id": "diff_root_join",
            "query_scope": "root",
            "clause": "JOIN ON",
            "diff_type": "join_on_changed",
        },
        {
            "diff_id": "diff_nested_count",
            "query_scope": "subquery:stats",
            "clause": "SELECT",
            "diff_type": "aggregate_argument_changed",
            "standard_sql": "COUNT(DISTINCT item_id)",
            "student_sql": "COUNT(item_id)",
        },
    ]
    effects = [
        {
            "diff_id": item["diff_id"],
            "causal_attribution_verified": True,
            "distinguished": True,
        }
        for item in diffs
    ]
    run = _run(
        diffs=diffs,
        effects=effects,
        standard_sql=(
            "SELECT a.id FROM a JOIN b ON a.id = b.id "
            "WHERE EXISTS (SELECT COUNT(DISTINCT item_id) FROM items)"
        ),
        student_sql=(
            "SELECT a.id FROM a JOIN b ON a.id = b.other_id "
            "WHERE EXISTS (SELECT COUNT(item_id) FROM items)"
        ),
        scope_metadata={
            "status": "COMPLETE",
            "scopes": [
                {"scope_id": "root", "scope_kind": "ROOT"},
                {"scope_id": "subquery:stats", "scope_kind": "SUBQUERY"},
            ],
            "composition_edges": [
                {
                    "edge_type": "SUBQUERY_OF",
                    "source_scope_id": "subquery:stats",
                    "target_scope_id": "root",
                }
            ],
        },
    )
    run.data_evidence["standard_duplicate_row_count"] = 0
    run.data_evidence["student_duplicate_row_count"] = 4

    package = diagnose_record(sandbox_run=run)

    assert "S5_FANOUT_AGGREGATE" not in {
        candidate.rule_id for candidate in package.candidate_trace
    }


def test_ordered_pipeline_is_stable_under_diff_input_permutation_and_keeps_scopes():
    diffs = [
        {"diff_id": "root_order", "obligation_id": "o1", "clause": "ORDER BY", "diff_type": "order_direction_changed", "query_scope": "root"},
        {"diff_id": "cte_where", "obligation_id": "o2", "clause": "WHERE", "diff_type": "logical_operator_changed", "query_scope": "cte:recent"},
        {"diff_id": "sub_group", "obligation_id": "o3", "clause": "GROUP BY", "diff_type": "grouping_grain_too_coarse", "query_scope": "subquery:1"},
    ]
    scope_metadata = {
        "status": "COMPLETE",
        "scopes": [
            {"scope_id": "root", "scope_kind": "ROOT"},
            {"scope_id": "cte:recent", "scope_kind": "CTE"},
            {"scope_id": "subquery:1", "scope_kind": "SUBQUERY"},
        ],
        "composition_edges": [
            {
                "edge_type": "CTE_FEEDS",
                "source_scope_id": "cte:recent",
                "target_scope_id": "root",
            },
            {
                "edge_type": "SUBQUERY_OF",
                "source_scope_id": "subquery:1",
                "target_scope_id": "root",
            },
        ],
    }
    one = diagnose_record(
        sandbox_run=_run(diffs=diffs, scope_metadata=scope_metadata)
    ).to_dict()["ordered_diff_pipeline"]
    two = diagnose_record(
        sandbox_run=_run(
            diffs=list(reversed(diffs)),
            scope_metadata=scope_metadata,
        )
    ).to_dict()["ordered_diff_pipeline"]
    assert one == two
    assert [item["diff_id"] for item in one] == ["cte_where", "sub_group", "root_order"]
    assert {item["scope_id"] for item in one} == {"cte:recent", "subquery:1", "root"}
    assert len(LOGICAL_STAGE_ORDER) == 14


def test_missing_query_scope_remains_unscoped_with_explicit_limitation():
    run = _run(diffs=[{"diff_id": "d1", "clause": "WHERE", "diff_type": "logical_operator_changed"}])
    public = diagnose_record(sandbox_run=run).to_dict()
    assert public["ordered_diff_pipeline"][0]["scope_id"] == "unscoped:d1"
    assert any("query_scope missing" in note for note in public["boundary_notes"])
    assert any("exact paired conceptual scope unavailable" in note for note in public["boundary_notes"])


def test_full_scan_retains_independent_late_stage_root():
    diffs = [
        {"diff_id": "join", "obligation_id": "oj", "clause": "JOIN_TYPE", "diff_type": "join_type_changed", "query_scope": "root", "standard_side": "INNER", "student_side": "CROSS"},
        {"diff_id": "order", "obligation_id": "oo", "clause": "ORDER BY", "diff_type": "order_direction_changed", "query_scope": "root"},
    ]
    effects = [
        {"diff_id": "join", "obligation_id": "oj", "causal_attribution_verified": True, "distinguished": True},
        {"diff_id": "order", "obligation_id": "oo", "causal_attribution_verified": True, "distinguished": True},
    ]
    public = diagnose_record(sandbox_run=_run(diffs=diffs, effects=effects)).to_dict()
    assert public["primary"]["rule_id"] == "S1_CARTESIAN_PRODUCT"
    assert public["secondary_count"] == 1
    assert public["secondary"][0]["rule_id"] == "S6_ORDER_OFFSET"


def test_fdp_uses_scope_graph_topology_before_global_teaching_stage():
    diffs = [
        {
            "diff_id": "root_join_fault",
            "obligation_id": "root_join_obligation",
            "clause": "JOIN_TYPE",
            "diff_type": "join_type_changed",
            "query_scope": "root",
            "standard_side": "INNER",
            "student_side": "CROSS",
        },
        {
            "diff_id": "cte_boundary_fault",
            "obligation_id": "cte_boundary_obligation",
            "clause": "WHERE",
            "diff_type": "comparison_operator_changed",
            "query_scope": "cte:source",
            "standard_op": "GT",
            "student_op": "GTE",
        },
    ]
    effects = [
        {
            "diff_id": item["diff_id"],
            "obligation_id": item["obligation_id"],
            "causal_attribution_verified": True,
            "distinguished": True,
        }
        for item in diffs
    ]
    package = diagnose_record(
        sandbox_run=_run(
            diffs=diffs,
            effects=effects,
            scope_metadata={
                "status": "COMPLETE",
                "scopes": [
                    {"scope_id": "root", "scope_kind": "ROOT"},
                    {"scope_id": "cte:source", "scope_kind": "CTE"},
                ],
                "composition_edges": [
                    {
                        "edge_type": "CTE_FEEDS",
                        "source_scope_id": "cte:source",
                        "target_scope_id": "root",
                    }
                ],
            },
        )
    )
    public = package.to_dict()

    assert [
        item["diff_id"] for item in public["ordered_diff_pipeline"]
    ] == ["cte_boundary_fault", "root_join_fault"]
    assert public["primary"]["rule_id"] == "S2_BOUNDARY"
    assert public["secondary_count"] == 1
    assert public["secondary"][0]["rule_id"] == "S1_CARTESIAN_PRODUCT"


def test_explicit_causal_edge_suppresses_only_an_unverified_dependent_symptom():
    diffs = [
        {"diff_id": "join", "obligation_id": "oj", "clause": "JOIN_TYPE", "diff_type": "join_type_changed", "query_scope": "root", "standard_side": "INNER", "student_side": "CROSS"},
        {"diff_id": "projection", "obligation_id": "op", "clause": "SELECT", "diff_type": "projection_changed", "query_scope": "root"},
    ]
    effects = [
        {"diff_id": "join", "obligation_id": "oj", "causal_attribution_verified": True, "distinguished": True},
    ]
    package = diagnose_record(
        sandbox_run=_run(
            diffs=diffs,
            effects=effects,
            dependencies=[
                {"from_diff_id": "join", "to_diff_id": "projection", "type": "CAUSES"}
            ],
        )
    )
    assert package.primary.rule_id == "S1_CARTESIAN_PRODUCT"
    assert package.unresolved == []
    assert [item["rule_id"] for item in package.suppressed] == [
        "UNCLASSIFIED_SUPPORTED_DIFF"
    ]
    assert package.causal_edges[0]["type"] == "CAUSES"


def test_explicit_dependency_never_suppresses_an_independently_verified_root():
    diffs = [
        {"diff_id": "join", "obligation_id": "oj", "clause": "JOIN_TYPE", "diff_type": "join_type_changed", "query_scope": "root", "standard_side": "INNER", "student_side": "CROSS"},
        {"diff_id": "order", "obligation_id": "oo", "clause": "ORDER BY", "diff_type": "order_direction_changed", "query_scope": "root"},
    ]
    effects = [
        {"diff_id": diff_id, "obligation_id": obligation_id, "causal_attribution_verified": True, "distinguished": True}
        for diff_id, obligation_id in (("join", "oj"), ("order", "oo"))
    ]
    package = diagnose_record(
        sandbox_run=_run(
            diffs=diffs,
            effects=effects,
            dependencies=[
                {"from_diff_id": "join", "to_diff_id": "order", "type": "MASKS"}
            ],
        )
    )
    assert package.primary.rule_id == "S1_CARTESIAN_PRODUCT"
    assert [item.rule_id for item in package.secondary] == ["S6_ORDER_OFFSET"]
    assert package.suppressed == []


def test_co_occurrence_edge_is_auditable_but_never_causal_suppression():
    diffs = [
        {"diff_id": "where", "obligation_id": "ow", "clause": "WHERE", "diff_type": "logical_operator_changed", "query_scope": "root"},
        {"diff_id": "order", "obligation_id": "oo", "clause": "ORDER BY", "diff_type": "order_direction_changed", "query_scope": "root"},
    ]
    effects = [
        {"diff_id": "where", "obligation_id": "ow", "causal_attribution_verified": True, "distinguished": True}
    ]
    package = diagnose_record(
        sandbox_run=_run(
            diffs=diffs,
            effects=effects,
            dependencies=[
                {"from_diff_id": "where", "to_diff_id": "order", "type": "CO_OCCURS"}
            ],
        )
    )
    assert package.primary.rule_id == "S2_BOOLEAN_LOGIC"
    assert [item.rule_id for item in package.unresolved] == ["S6_ORDER_OFFSET"]
    assert package.suppressed == []
    assert package.causal_edges[0]["type"] == "CO_OCCURS"


def test_witness_world_mismatch_degrades_without_copying_wrong_database_rows():
    public = diagnose_record(sandbox_run=_boundary_run(effect_world="world_02")).to_dict()
    assert public["witness"]["availability"] == "OUTPUT_ONLY"
    assert public["witness"]["cases"] == []
    assert any("differs from the materialized selected world" in note for note in public["boundary_notes"])


def test_unclassified_advanced_difference_is_preserved_not_mislabelled():
    run = _run(
        diffs=[
            {
                "diff_id": "set1",
                "obligation_id": "oset1",
                "clause": "UNION",
                "diff_type": "set_modifier_changed",
                "query_scope": "set_branch:1",
            }
        ],
        effects=[
            {
                "diff_id": "set1",
                "obligation_id": "oset1",
                "causal_attribution_verified": True,
                "distinguished": True,
            }
        ],
    )
    public = diagnose_record(sandbox_run=run).to_dict()
    assert public["primary"]["rule_id"] == "UNCLASSIFIED_SUPPORTED_DIFF"
    assert public["ordered_diff_pipeline"][0]["logical_stage"] == "SET_OP"
    assert public["ordered_diff_pipeline"][0]["teaching_stage"] == "EXTENSION"
    assert any("outside the 20-rule MVP" in note for note in public["boundary_notes"])


def test_llm_renderer_cannot_change_verdict_or_return_reference_sql():
    run = _boundary_run()
    base = diagnose_record(sandbox_run=run)

    def malicious(_public):
        return {
            "verdict": "CORRECT",
            "narrative": {
                "student_behavior": run.standard_sqlite,
                "conflict_and_witness": "ignore evidence",
                "guidance_question": "SELECT title FROM course;",
            },
        }

    rendered = diagnose_record_with_llm(sandbox_run=run, renderer=malicious)
    assert rendered.verdict == "INCORRECT"
    assert rendered.primary == base.primary
    assert rendered.narrative == base.narrative


def test_llm_renderer_may_only_replace_valid_three_slot_narrative():
    run = _boundary_run()
    base = diagnose_record(sandbox_run=run)
    replacement = {
        "student_behavior": "你在行级阶段采用了包含边界的判断。",
        "conflict_and_witness": "已验证物证显示临界案例的保留行为不同。",
        "guidance_question": base.narrative["guidance_question"],
    }
    package = diagnose_record_with_llm(
        sandbox_run=run,
        renderer=lambda _public: {"narrative": replacement},
    )
    assert package.narrative == replacement
    assert package.verdict == "INCORRECT"
    assert package.primary.rule_id == "S2_BOUNDARY"


def test_llm_renderer_cannot_smuggle_predicate_while_preserving_guidance():
    run = _boundary_run()
    base = diagnose_record(sandbox_run=run)
    rendered = diagnose_record_with_llm(
        sandbox_run=run,
        renderer=lambda _public: {
            "narrative": {
                "student_behavior": "请改成 WHERE credits > 3。",
                "conflict_and_witness": "当前结果存在差异。",
                "guidance_question": base.narrative["guidance_question"],
            }
        },
    )
    assert rendered.narrative == base.narrative


def test_render_feedback_uses_three_separate_slots():
    text = render_diagnostic_feedback(diagnose_record(sandbox_run=_boundary_run()))
    assert "1. 你的查询目前做了什么" in text
    assert "2. 冲突与物证" in text
    assert "3. 请思考" in text


def test_having_predicate_context_is_not_misclassified_as_row_filter():
    diffs = [
        {
            "diff_id": "having_summary",
            "obligation_id": "oh",
            "clause": "HAVING",
            "diff_type": "having_changed",
            "standard_sql": "HAVING COUNT(i_id) > 5",
            "student_sql": "",
        },
        {
            "diff_id": "having_atomic",
            "obligation_id": "oha",
            "clause": "PREDICATE",
            "diff_type": "predicate_missing",
            "standard_sql": "COUNT(i_id) > 5",
            "student_sql": "",
            "standard_query_sql": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(i_id) > 5",
            "student_query_sql": "SELECT dept_name FROM instructor GROUP BY dept_name",
        },
    ]
    mutations = [
        {
            "diff_ids": ["having_summary"],
            "clause": "HAVING",
            "query_scope": "root",
            "binding_quality": "exact",
            "mutation_scope": ["HAVING"],
            "dependent_changes": [],
            "fixed_by_replacement": True,
            "replacement_exec_ok": True,
        }
    ]
    public = diagnose_record(sandbox_run=_run(diffs=diffs, mutations=mutations)).to_dict()
    assert public["primary"]["rule_id"] == "S4_HAVING_MISSING"
    assert {item["logical_stage"] for item in public["ordered_diff_pipeline"]} == {"GROUP_FILTER"}


def test_set_vs_scalar_comparison_is_not_reported_as_numeric_boundary():
    diffs = [
        {
            "diff_id": "sub_cmp",
            "obligation_id": "osub",
            "clause": "PREDICATE",
            "diff_type": "comparison_operator_changed",
            "standard_op": "IN",
            "student_op": "EQ",
            "standard_sql": "credits IN (SELECT credits FROM course)",
            "student_sql": "credits = (SELECT credits FROM course)",
            "student_value_kind": "expression",
        }
    ]
    effects = [
        {
            "diff_id": "sub_cmp",
            "obligation_id": "osub",
            "constraints_satisfied": True,
            "distinguished": True,
            "causal_attribution_verified": True,
        }
    ]
    public = diagnose_record(sandbox_run=_run(diffs=diffs, effects=effects)).to_dict()
    assert public["primary"]["rule_id"] == "S1_SUBQUERY_CARDINALITY"
    assert public["primary"]["stage"] == "S1"


def test_direct_aggregate_in_where_signal_forms_specific_bundle():
    diffs = [
        {
            "diff_id": "where_summary",
            "obligation_id": "ow",
            "clause": "WHERE",
            "diff_type": "where_changed",
            "standard_sql": "salary > (SELECT AVG(salary) FROM instructor)",
            "student_sql": "salary > AVG(salary)",
        },
        {
            "diff_id": "sub_removed",
            "obligation_id": "os",
            "clause": "SUBQUERY",
            "diff_type": "subquery_removed",
        },
    ]
    mutations = [
        {
            "diff_ids": ["where_summary"],
            "clause": "WHERE",
            "query_scope": "root",
            "binding_quality": "exact",
            "mutation_scope": ["WHERE"],
            "dependent_changes": [],
            "fixed_by_replacement": True,
            "replacement_exec_ok": True,
        }
    ]
    run = _run(
        diffs=diffs,
        mutations=mutations,
        student_sql="SELECT i_name FROM instructor WHERE salary > AVG(salary)",
    )
    public = diagnose_record(sandbox_run=run).to_dict()
    assert public["primary"]["rule_id"] == "S2_AGGREGATE_IN_WHERE"
    assert all(item["rule_id"] != "S2_ROW_FILTER_MISMATCH" for item in public["secondary"])


def test_ambiguous_or_multi_change_repair_does_not_become_atomic_root():
    run = _run(
        diffs=[
            {
                "diff_id": "boundary",
                "obligation_id": "ob",
                "clause": "WHERE",
                "diff_type": "comparison_operator_changed",
                "standard_op": "GT",
                "student_op": "GTE",
            }
        ],
        mutations=[
            {
                "diff_ids": ["boundary"],
                "fixed_by_replacement": True,
                "replacement_exec_ok": True,
                "binding_quality": "ambiguous",
                "mutation_scope": ["WHERE", "SELECT"],
                "dependent_changes": ["SELECT"],
            }
        ],
    )
    package = diagnose_record(sandbox_run=run)
    assert package.primary is None
    assert package.diagnosis_status == "PARTIAL"
    assert package.unresolved[0].rule_id == "S2_BOUNDARY"
    assert package.unresolved[0].evidence_grade == "OUTPUT_ONLY"


def test_pair_difference_without_valid_atomic_constraints_stays_unresolved():
    run = _run(
        diffs=[
            {
                "diff_id": "distinct",
                "obligation_id": "od",
                "clause": "DISTINCT",
                "diff_type": "distinct_changed",
                "standard_sql": "True",
                "student_sql": "False",
            }
        ],
        effects=[
            {
                "diff_id": "distinct",
                "obligation_id": "od",
                "distinguished": True,
                "pair_distinguished": True,
                "constraints_satisfied": False,
                "causal_attribution_verified": False,
            }
        ],
    )
    package = diagnose_record(sandbox_run=run)
    assert package.verdict == "INCORRECT"
    assert package.primary is None
    assert [item.rule_id for item in package.unresolved] == [
        "UNCLASSIFIED_SUPPORTED_DIFF"
    ]


def test_non_endpoint_comparison_rewrite_is_not_mislabeled_as_boundary():
    diff_id = "comparison_semantics"
    package = diagnose_record(
        sandbox_run=_run(
            diffs=[
                {
                    "diff_id": diff_id,
                    "obligation_id": "ocomparison",
                    "clause": "PREDICATE",
                    "diff_type": "comparison_operator_changed",
                    "standard_op": "EQ",
                    "student_op": "NEQ",
                    "standard_sql": "credits = 3",
                    "student_sql": "credits != 3",
                }
            ],
            effects=[
                {
                    "diff_id": diff_id,
                    "obligation_id": "ocomparison",
                    "constraints_satisfied": True,
                    "distinguished": True,
                    "causal_attribution_verified": True,
                }
            ],
        )
    )

    assert package.primary is not None
    assert package.primary.rule_id == "UNCLASSIFIED_SUPPORTED_DIFF"
    assert package.primary.rule_id not in {"S2_BOUNDARY", "S4_AGG_BOUNDARY"}


def test_phase1_null_specific_diff_types_map_to_null_logic_family():
    for diff_type in (
        "null_predicate_negation_changed",
        "null_sensitive_antijoin_equivalence",
    ):
        diff_id = f"diff_{diff_type}"
        package = diagnose_record(
            sandbox_run=_run(
                diffs=[
                    {
                        "diff_id": diff_id,
                        "obligation_id": f"obligation_{diff_type}",
                        "clause": "NULL",
                        "diff_type": diff_type,
                        "standard_sql": "grade IS NULL",
                        "student_sql": "grade IS NOT NULL",
                    }
                ],
                effects=[
                    {
                        "diff_id": diff_id,
                        "obligation_id": f"obligation_{diff_type}",
                        "constraints_satisfied": True,
                        "distinguished": True,
                        "causal_attribution_verified": True,
                    }
                ],
                mutations=[
                    {
                        "diff_ids": [diff_id],
                        "clause": "WHERE",
                        "query_scope": "root",
                        "binding_quality": "exact",
                        "mutation_scope": ["WHERE"],
                        "dependent_changes": [],
                        "fixed_by_replacement": True,
                        "replacement_exec_ok": True,
                    }
                ],
            )
        )

        assert package.primary is not None
        assert package.primary.rule_id == "S2_NULL_LOGIC"


def test_cross_join_signal_is_not_duplicated_as_outer_join_misuse():
    diff_id = "diff_cross_join"
    package = diagnose_record(
        sandbox_run=_run(
            diffs=[
                {
                    "diff_id": diff_id,
                    "obligation_id": "obligation_cross_join",
                    "clause": "JOIN_TYPE",
                    "diff_type": "join_type_changed",
                    "standard_side": "INNER",
                    "student_side": "CROSS",
                    "standard_sql": "JOIN department d ON i.dept_name = d.dept_name",
                    "student_sql": "CROSS JOIN department d",
                }
            ],
            effects=[
                {
                    "diff_id": diff_id,
                    "obligation_id": "obligation_cross_join",
                    "constraints_satisfied": True,
                    "distinguished": True,
                    "causal_attribution_verified": True,
                }
            ],
        )
    )

    assert package.primary is not None
    assert package.primary.rule_id == "S1_CARTESIAN_PRODUCT"
    assert all(item.rule_id != "S1_OUTER_JOIN_MISUSE" for item in package.secondary)
    assert all(item.rule_id != "S1_OUTER_JOIN_MISUSE" for item in package.unresolved)


def test_real_phase1_to_phase2_chain_covers_representative_rule_families():
    schema = """
        student(s_id INT, s_name TEXT, dept_name TEXT);
        takes(s_id INT, course_id TEXT, grade TEXT);
        course(course_id TEXT, title TEXT, dept_name TEXT, credits INT);
        instructor(i_id INT, i_name TEXT, dept_name TEXT, salary INT);
        department(dept_name TEXT, building TEXT, budget INT);
    """
    cases = (
        (
            "SELECT title FROM course WHERE credits > 3",
            "SELECT title FROM course WHERE credits >= 3",
            "S2_BOUNDARY",
        ),
        (
            "SELECT * FROM takes WHERE grade IS NULL",
            "SELECT * FROM takes WHERE grade IS NOT NULL",
            "S2_NULL_LOGIC",
        ),
        (
            "SELECT i.i_name, d.building FROM instructor i "
            "JOIN department d ON i.dept_name = d.dept_name",
            "SELECT i.i_name, d.building FROM instructor i, department d",
            "S1_CARTESIAN_PRODUCT",
        ),
        (
            "SELECT s.s_name, COUNT(t.course_id) FROM student s "
            "LEFT JOIN takes t ON s.s_id = t.s_id GROUP BY s.s_id, s.s_name",
            "SELECT s.s_name, COUNT(t.course_id) FROM student s "
            "JOIN takes t ON s.s_id = t.s_id GROUP BY s.s_id, s.s_name",
            "S1_OUTER_JOIN_MISUSE",
        ),
        (
            "SELECT i_name, salary FROM instructor "
            "ORDER BY salary DESC LIMIT 3",
            "SELECT i_name, salary FROM instructor LIMIT 3",
            "S6_TOPN_WITHOUT_ORDER",
        ),
        (
            "SELECT i_name, CASE WHEN salary > 80000 THEN 'High' "
            "ELSE 'Normal' END AS salary_level FROM instructor",
            "SELECT i_name, CASE WHEN salary > 80000 THEN 'High' "
            "END AS salary_level FROM instructor",
            "S5_CASE_INCOMPLETE",
        ),
        (
            "SELECT dept_name, AVG(salary) FROM instructor "
            "WHERE salary > 80000 GROUP BY dept_name",
            "SELECT dept_name, AVG(salary) FROM instructor "
            "GROUP BY dept_name HAVING salary > 80000",
            "S4_ROW_FILTER_IN_HAVING",
        ),
    )

    for standard_sql, student_sql, expected_rule in cases:
        run = generate_and_compare(
            schema,
            standard_sql,
            student_sql,
            max_rows_per_table=6,
        )
        package = diagnose_record(sandbox_run=run, schema=schema)

        assert run.equivalence_conclusion == "NOT_EQUIVALENT"
        assert package.primary is not None
        assert package.primary.rule_id == expected_rule


def test_real_phase1_signals_do_not_expand_specific_rules_beyond_their_contract():
    schema = """
        sample(id INT, x INT, y INT);
        instructor(i_id INT, i_name TEXT, salary INT, dept_name TEXT);
        department(dept_name TEXT, building TEXT);
    """
    cases = (
        (
            "SELECT id FROM sample WHERE x IN (1, 2)",
            "SELECT id FROM sample WHERE x = 1",
            {"S1_SUBQUERY_CARDINALITY"},
        ),
        (
            "SELECT CASE WHEN x > 3 THEN 1 ELSE 0 END FROM sample",
            "SELECT CASE WHEN x >= 3 THEN 1 ELSE 0 END FROM sample",
            {"S2_BOUNDARY", "S4_AGG_BOUNDARY"},
        ),
        (
            "SELECT id, x, y FROM sample ORDER BY x",
            "SELECT id, x, y FROM sample ORDER BY x, y",
            {"S6_ORDER_OFFSET", "S6_TOPN_WITHOUT_ORDER"},
        ),
        (
            "SELECT id, x, y FROM sample ORDER BY x, y",
            "SELECT id, x, y FROM sample ORDER BY x",
            {"S6_ORDER_OFFSET", "S6_TOPN_WITHOUT_ORDER"},
        ),
        (
            "SELECT i.i_name, d.building FROM instructor i "
            "JOIN department d ON i.dept_name = d.dept_name",
            "SELECT i_name FROM instructor",
            {"S1_MISSING_BRIDGE"},
        ),
    )

    for standard_sql, student_sql, forbidden_rules in cases:
        run = generate_and_compare(
            schema,
            standard_sql,
            student_sql,
            max_rows_per_table=6,
        )
        package = diagnose_record(sandbox_run=run, schema=schema)
        labelled = {
            item.rule_id
            for item in (
                ([package.primary] if package.primary is not None else [])
                + list(package.secondary)
                + list(package.unresolved)
            )
        }
        labelled.update(str(item.get("rule_id")) for item in package.suppressed)

        assert run.equivalence_conclusion == "NOT_EQUIVALENT"
        assert labelled.isdisjoint(forbidden_rules)


def test_missing_null_branch_uses_phase1_real_diff_shape_for_null_family():
    predicate_id = "diff_missing_null_predicate"
    package = diagnose_record(
        sandbox_run=_run(
            diffs=[
                {
                    "diff_id": "diff_where_null_summary",
                    "obligation_id": "obligation_where_null_summary",
                    "clause": "WHERE",
                    "diff_type": "where_changed",
                    "standard_sql": "grade != 'F' OR grade IS NULL",
                    "student_sql": "grade != 'F'",
                },
                {
                    "diff_id": predicate_id,
                    "obligation_id": "obligation_missing_null_predicate",
                    "clause": "PREDICATE",
                    "diff_type": "predicate_missing",
                    "standard_sql": "grade IS NULL",
                    "student_sql": "",
                    "standard_query_sql": (
                        "SELECT * FROM takes WHERE grade != 'F' OR grade IS NULL"
                    ),
                    "student_query_sql": "SELECT * FROM takes WHERE grade != 'F'",
                },
                {
                    "diff_id": "diff_null_logical_container",
                    "obligation_id": "obligation_null_logical_container",
                    "clause": "LOGICAL",
                    "diff_type": "logical_operator_changed",
                    "standard_sql": "grade != 'F' OR grade IS NULL",
                    "student_sql": "grade != 'F'",
                },
            ],
            effects=[
                {
                    "diff_id": predicate_id,
                    "obligation_id": "obligation_missing_null_predicate",
                    "constraints_satisfied": True,
                    "distinguished": True,
                    "causal_attribution_verified": True,
                }
            ],
        )
    )

    assert package.primary is not None
    assert package.primary.rule_id == "S2_NULL_LOGIC"


def test_count_null_rule_requires_nullable_fact_on_the_proven_source_table():
    diff_id = "diff_count_source"
    package = diagnose_record(
        sandbox_run=_run(
            diffs=[
                {
                    "diff_id": diff_id,
                    "obligation_id": "obligation_count_source",
                    "clause": "AGGREGATE",
                    "diff_type": "aggregate_argument_changed",
                    "standard_sql": "COUNT(bonus)",
                    "student_sql": "COUNT(*)",
                    "column": "bonus",
                    "standard_source_table": "instructor",
                }
            ],
            effects=[
                {
                    "diff_id": diff_id,
                    "obligation_id": "obligation_count_source",
                    "constraints_satisfied": True,
                    "distinguished": True,
                    "causal_attribution_verified": True,
                }
            ],
        ),
        schema={
            "tables": [
                {
                    "name": "instructor",
                    "columns": [{"name": "bonus", "nullable": False}],
                },
                {
                    "name": "research_grant",
                    "columns": [{"name": "bonus", "nullable": True}],
                },
            ]
        },
    )

    assert package.primary is not None
    assert package.primary.rule_id == "UNCLASSIFIED_SUPPORTED_DIFF"
