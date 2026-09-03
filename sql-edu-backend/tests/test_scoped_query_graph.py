from __future__ import annotations

from types import SimpleNamespace

from core.scoped_query_graph import (
    COMPOSITION_EDGE_TYPES,
    LOGICAL_STAGE_ORDER,
    MAX_DIFFS,
    MAX_EDGES,
    MAX_SCOPES,
    build_scoped_query_graph,
)


def _stage_map(scope: dict) -> dict[str, list[str]]:
    return {item["stage"]: item["diff_ids"] for item in scope["stages"]}


def _scope(public: dict, scope_id: str) -> dict:
    return next(item for item in public["scopes"] if item["scope_id"] == scope_id)


def test_root_scope_has_all_fourteen_stages_and_ordered_diff_ids():
    graph = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "diff_where_z",
                "query_scope": "root",
                "clause": "WHERE",
                "diff_type": "logical_operator_changed",
            },
            {
                "diff_id": "diff_select",
                "query_scope": "root",
                "clause": "SELECT",
                "diff_type": "projection_changed",
            },
            {
                "diff_id": "diff_where_a",
                "query_scope": "root",
                "clause": "WHERE",
                "diff_type": "literal_changed",
            },
        ]
    ).to_dict()

    assert graph["status"] == "COMPLETE"
    assert graph["logical_stage_order"] == list(LOGICAL_STAGE_ORDER)
    assert len(graph["scopes"]) == 1
    root = graph["scopes"][0]
    assert root["scope_id"] == "root"
    assert root["scope_kind"] == "ROOT"
    assert len(root["stages"]) == 14
    stages = _stage_map(root)
    assert stages["ROW_FILTER"] == ["diff_where_a", "diff_where_z"]
    assert stages["PROJECTION"] == ["diff_select"]
    assert graph["composition_edges"] == []


def test_cte_edge_is_created_only_from_explicit_consumer_metadata():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "diff_cte_where",
                "query_scope": "cte:recent",
                "scope_kind": "CTE",
                "cte_consumer_scope_id": "root",
                "clause": "WHERE",
                "diff_type": "comparison_operator_changed",
            }
        ]
    ).to_dict()

    assert public["status"] == "COMPLETE"
    assert {item["scope_id"] for item in public["scopes"]} == {"root", "cte:recent"}
    assert _scope(public, "cte:recent")["scope_kind"] == "CTE"
    assert public["composition_edges"] == [
        {
            "edge_type": "CTE_FEEDS",
            "source_scope_id": "cte:recent",
            "target_scope_id": "root",
            "evidence_refs": ["diff_cte_where"],
        }
    ]


def test_correlated_subquery_keeps_lexical_and_correlation_edges_separate():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "diff_corr",
                "query_scope": "subquery:1",
                "scope_kind": "SUBQUERY",
                "subquery_parent_scope_id": "root",
                "correlated_to_scope_id": "root",
                "is_correlated": True,
                "clause": "CORRELATED SUBQUERY",
                "diff_type": "correlated_predicate_changed",
            }
        ]
    ).to_dict()

    assert public["status"] == "COMPLETE"
    assert _scope(public, "subquery:1")["scope_kind"] == "SUBQUERY"
    assert {
        (edge["edge_type"], edge["source_scope_id"], edge["target_scope_id"])
        for edge in public["composition_edges"]
    } == {
        ("SUBQUERY_OF", "subquery:1", "root"),
        ("CORRELATED_TO", "subquery:1", "root"),
    }


def test_lateral_derived_scope_preserves_kind_and_requires_lateral_edge():
    scope_metadata = {
        "status": "COMPLETE",
        "scopes": [
            {"scope_id": "root", "scope_kind": "ROOT"},
            {
                "scope_id": "derived:lateral",
                "scope_kind": "DERIVED",
                "is_lateral": True,
            },
        ],
        "composition_edges": [
            {
                "edge_type": "DERIVED_FEEDS",
                "source_scope_id": "derived:lateral",
                "target_scope_id": "root",
            },
            {
                "edge_type": "LATERAL_TO",
                "source_scope_id": "derived:lateral",
                "target_scope_id": "root",
            },
        ],
    }

    complete = build_scoped_query_graph(scope_metadata=scope_metadata).to_dict()

    assert complete["status"] == "COMPLETE"
    assert _scope(complete, "derived:lateral")["scope_kind"] == "DERIVED"
    assert [item["edge_type"] for item in complete["composition_edges"]] == [
        "DERIVED_FEEDS",
        "LATERAL_TO",
    ]

    missing_lateral = build_scoped_query_graph(
        scope_metadata={
            **scope_metadata,
            "composition_edges": scope_metadata["composition_edges"][:1],
        }
    ).to_dict()
    assert missing_lateral["status"] == "PARTIAL"
    assert any(
        "explicit lateral target missing" in item
        for item in missing_lateral["limitations"]
    )


def test_set_branches_are_members_only_when_set_parent_is_explicit():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "branch_b",
                "query_scope": "set_branch:1",
                "scope_kind": "SET_BRANCH",
                "set_parent_scope_id": "root",
                "clause": "UNION",
                "diff_type": "set_modifier_changed",
            },
            {
                "diff_id": "branch_a",
                "query_scope": "set_branch:0",
                "scope_kind": "SET_BRANCH",
                "set_parent_scope_id": "root",
                "clause": "SELECT",
                "diff_type": "projection_changed",
            },
        ]
    ).to_dict()

    assert public["status"] == "COMPLETE"
    assert [
        (edge["edge_type"], edge["source_scope_id"], edge["target_scope_id"])
        for edge in public["composition_edges"]
    ] == [
        ("SET_MEMBER_OF", "set_branch:0", "root"),
        ("SET_MEMBER_OF", "set_branch:1", "root"),
    ]
    assert _stage_map(_scope(public, "set_branch:1"))["SET_OP"] == ["branch_b"]


def test_explicit_composition_dag_orders_producers_before_root_stably():
    scopes = [
        {"scope_id": "root", "scope_kind": "ROOT"},
        {"scope_id": "cte:source", "scope_kind": "CTE"},
        {"scope_id": "derived:source", "scope_kind": "DERIVED"},
        {"scope_id": "subquery:source", "scope_kind": "SUBQUERY"},
        {"scope_id": "set_branch:0", "scope_kind": "SET_BRANCH"},
    ]
    edges = [
        {
            "edge_type": "CTE_FEEDS",
            "source_scope_id": "cte:source",
            "target_scope_id": "root",
        },
        {
            "edge_type": "DERIVED_FEEDS",
            "source_scope_id": "derived:source",
            "target_scope_id": "root",
        },
        {
            "edge_type": "SUBQUERY_OF",
            "source_scope_id": "subquery:source",
            "target_scope_id": "root",
        },
        {
            "edge_type": "SET_MEMBER_OF",
            "source_scope_id": "set_branch:0",
            "target_scope_id": "root",
        },
    ]

    forward = build_scoped_query_graph(
        scope_metadata={"scopes": scopes, "composition_edges": edges}
    ).to_dict()
    reverse = build_scoped_query_graph(
        scope_metadata={
            "scopes": list(reversed(scopes)),
            "composition_edges": list(reversed(edges)),
        }
    ).to_dict()

    assert forward == reverse
    scope_order = [item["scope_id"] for item in forward["scopes"]]
    assert scope_order[-1] == "root"
    assert set(scope_order[:-1]) == {
        "cte:source",
        "derived:source",
        "subquery:source",
        "set_branch:0",
    }


def test_explicit_scope_declarations_support_derived_and_global_edges():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "derived_limit",
                "query_scope": "derived:orders",
                "clause": "LIMIT",
                "diff_type": "limit_changed",
            }
        ],
        scope_metadata={
            "scopes": [
                {"scope_id": "root", "scope_kind": "ROOT"},
                {"scope_id": "derived:orders", "scope_kind": "DERIVED"},
            ],
            "composition_edges": [
                {
                    "edge_type": "DERIVED_FEEDS",
                    "source_scope_id": "derived:orders",
                    "target_scope_id": "root",
                    "evidence_refs": ["scope_catalog"],
                }
            ],
        },
    ).to_dict()

    assert public["status"] == "COMPLETE"
    assert _scope(public, "derived:orders")["scope_kind"] == "DERIVED"
    assert _stage_map(_scope(public, "derived:orders"))["PAGINATION"] == [
        "derived_limit"
    ]
    assert public["composition_edges"][0]["edge_type"] == "DERIVED_FEEDS"


def test_mutation_diff_binding_is_accepted_as_explicit_scope_evidence():
    source = SimpleNamespace(
        data_evidence={
            "ast_diffs": [
                {
                    "diff_id": "diff_bound",
                    "clause": "ORDER BY",
                    "diff_type": "order_direction_changed",
                }
            ]
        },
        mutation_evidence={
            "tests": [
                {
                    "test_id": "mutation_order",
                    "diff_ids": ["diff_bound"],
                    "query_scope": "root",
                    "clause": "ORDER BY",
                    "action": "restore_order",
                }
            ]
        },
    )

    graph = build_scoped_query_graph(source)
    public = graph.to_dict()

    assert public["status"] == "COMPLETE"
    assert _stage_map(_scope(public, "root"))["ROOT_ORDER"] == ["diff_bound"]
    assert not any("query_scope missing" in item for item in public["limitations"])


def test_graph_serialization_is_stable_under_all_input_permutations():
    diffs = [
        {
            "diff_id": "root_order",
            "query_scope": "root",
            "clause": "ORDER BY",
            "diff_type": "order_direction_changed",
        },
        {
            "diff_id": "cte_where",
            "query_scope": "cte:recent",
            "scope_kind": "CTE",
            "cte_consumer_scope_id": "root",
            "clause": "WHERE",
            "diff_type": "logical_operator_changed",
        },
        {
            "diff_id": "sub_group",
            "query_scope": "subquery:1",
            "scope_kind": "SUBQUERY",
            "subquery_parent_scope_id": "root",
            "clause": "GROUP BY",
            "diff_type": "grouping_grain_too_coarse",
        },
    ]
    scopes = {
        "scopes": [
            {"scope_id": "root", "scope_kind": "ROOT"},
            {"scope_id": "cte:recent", "scope_kind": "CTE"},
            {"scope_id": "subquery:1", "scope_kind": "SUBQUERY"},
        ]
    }

    forward = build_scoped_query_graph(
        ast_diffs=diffs,
        scope_metadata=scopes,
    ).to_dict()
    reverse = build_scoped_query_graph(
        ast_diffs=list(reversed(diffs)),
        scope_metadata={"scopes": list(reversed(scopes["scopes"]))},
    ).to_dict()

    assert forward == reverse


def test_missing_scope_or_composition_metadata_is_partial_and_never_guesses_edges():
    missing_scope = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "diff_no_scope",
                "clause": "WHERE",
                "diff_type": "literal_changed",
            }
        ]
    ).to_dict()
    assert missing_scope["status"] == "PARTIAL"
    assert missing_scope["composition_edges"] == []
    assert missing_scope["scopes"][0]["scope_id"].startswith("unscoped:")
    assert any("query_scope missing" in item for item in missing_scope["limitations"])

    cte_without_consumer = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "diff_cte",
                "query_scope": "cte:orphan",
                "scope_kind": "CTE",
                "clause": "SELECT",
                "diff_type": "projection_changed",
            }
        ]
    ).to_dict()
    assert cte_without_consumer["status"] == "PARTIAL"
    assert cte_without_consumer["composition_edges"] == []
    assert any(
        "explicit composition metadata missing for scope cte:orphan" in item
        for item in cte_without_consumer["limitations"]
    )


def test_nested_phase1_label_does_not_guess_subquery_or_derived_kind():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "nested_diff",
                "query_scope": "nested:1",
                "clause": "WHERE",
                "diff_type": "literal_changed",
            }
        ]
    ).to_dict()

    assert public["status"] == "PARTIAL"
    assert public["composition_edges"] == []
    assert public["scopes"][0]["scope_kind"] == "UNKNOWN"
    assert any("scope_kind missing or ambiguous" in item for item in public["limitations"])


def test_hard_caps_bound_diffs_scopes_edges_and_mark_partial():
    diffs = [
        {
            "diff_id": f"diff_{index:03d}",
            "query_scope": "root",
            "clause": "WHERE",
            "diff_type": "literal_changed",
        }
        for index in range(MAX_DIFFS + 17)
    ]
    scope_declarations = [{"scope_id": "root", "scope_kind": "ROOT"}]
    scope_declarations.extend(
        {
            "scope_id": f"cte:{index:03d}",
            "scope_kind": "CTE",
            "cte_consumer_scope_id": "root",
            "parent_scope_id": "root",
        }
        for index in range(MAX_SCOPES + 9)
    )

    public = build_scoped_query_graph(
        ast_diffs=diffs,
        scope_metadata={"scopes": scope_declarations},
        max_diffs=7,
        max_scopes=4,
        max_edges=2,
    ).to_dict()

    assert public["status"] == "PARTIAL"
    assert public["truncated"] is True
    assert public["counts"]["retained_diffs"] == 7
    assert public["counts"]["scopes"] <= 4
    assert public["counts"]["composition_edges"] <= 2
    assert public["counts"]["edges"] <= 2
    assert sum(
        len(stage["diff_ids"])
        for scope in public["scopes"]
        for stage in scope["stages"]
    ) <= 7
    assert any("limit reached" in item for item in public["limitations"])


def test_phase1_scope_graph_uses_exact_side_aware_bindings_without_merging():
    source = SimpleNamespace(
        data_evidence={
            "ast_diffs": [
                {
                    "diff_id": "paired_where",
                    "query_scope": "root",
                    "clause": "WHERE",
                    "diff_type": "literal_changed",
                }
            ],
            "scope_graph": {
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
                "diff_bindings": [
                    {
                        "diff_id": "paired_where",
                        "side": "standard",
                        "scope_id": "standard:root",
                        "conceptual_scope_id": "paired:root",
                        "binding_status": "EXACT_AST_PATH",
                    },
                    {
                        "diff_id": "paired_where",
                        "side": "student",
                        "scope_id": "student:root",
                        "conceptual_scope_id": "paired:root",
                        "binding_status": "EXACT_PAIRED_AST_PATH",
                    },
                ],
            },
        },
        mutation_evidence={
            "tests": [
                {
                    "test_id": "legacy_scope_label",
                    "diff_ids": ["paired_where"],
                    "query_scope": "root",
                }
            ]
        },
    )

    graph = build_scoped_query_graph(source)
    public = graph.to_dict()

    assert public["status"] == "COMPLETE"
    assert {item["scope_id"] for item in public["scopes"]} == {
        "standard:root",
        "student:root",
    }
    assert _scope(public, "standard:root")["side"] == "standard"
    assert _scope(public, "student:root")["side"] == "student"
    assert _scope(public, "standard:root")["conceptual_scope_id"] == "paired:root"
    assert public["conceptual_bindings"] == [
        {
            "diff_id": "paired_where",
            "conceptual_scope_id": "paired:root",
            "scope_ids": ["standard:root", "student:root"],
            "binding_status": "EXACT_PAIRED",
        }
    ]
    assert graph.conceptual_scope_for_diff("paired_where") == "paired:root"
    assert _stage_map(_scope(public, "standard:root"))["ROW_FILTER"] == [
        "paired_where"
    ]
    assert _stage_map(_scope(public, "student:root"))["ROW_FILTER"] == [
        "paired_where"
    ]


def test_parent_edges_remain_separate_from_composition_edges():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "sub_filter",
                "clause": "WHERE",
                "diff_type": "literal_changed",
            }
        ],
        scope_metadata={
            "status": "COMPLETE",
            "scopes": [
                {
                    "scope_id": "student:root",
                    "scope_kind": "ROOT",
                    "side": "student",
                    "conceptual_scope_id": "paired:root",
                },
                {
                    "scope_id": "student:scope:0123456789abcdef",
                    "scope_kind": "SUBQUERY",
                    "side": "student",
                    "conceptual_scope_id": "paired:scope:subquery",
                },
                {
                    "scope_id": "standard:root",
                    "scope_kind": "ROOT",
                    "side": "standard",
                    "conceptual_scope_id": "paired:root",
                },
                {
                    "scope_id": "standard:scope:fedcba9876543210",
                    "scope_kind": "SUBQUERY",
                    "side": "standard",
                    "conceptual_scope_id": "paired:scope:subquery",
                },
            ],
            "diff_bindings": [
                {
                    "diff_id": "sub_filter",
                    "scope_id": "student:scope:0123456789abcdef",
                    "side": "student",
                    "conceptual_scope_id": "paired:scope:subquery",
                    "binding_status": "EXACT_AST_PATH",
                },
                {
                    "diff_id": "sub_filter",
                    "scope_id": "standard:scope:fedcba9876543210",
                    "side": "standard",
                    "conceptual_scope_id": "paired:scope:subquery",
                    "binding_status": "EXACT_PAIRED_AST_PATH",
                }
            ],
            "composition_edges": [
                {
                    "edge_type": "SUBQUERY_OF",
                    "source_scope_id": "student:scope:0123456789abcdef",
                    "target_scope_id": "student:root",
                },
                {
                    "edge_type": "SUBQUERY_OF",
                    "source_scope_id": "standard:scope:fedcba9876543210",
                    "target_scope_id": "standard:root",
                }
            ],
            "parent_edges": [
                {
                    "edge_type": "PARENT",
                    "source_scope_id": "student:scope:0123456789abcdef",
                    "target_scope_id": "student:root",
                },
                {
                    "edge_type": "PARENT",
                    "source_scope_id": "standard:scope:fedcba9876543210",
                    "target_scope_id": "standard:root",
                }
            ],
        },
    ).to_dict()

    assert public["status"] == "COMPLETE"
    assert [edge["edge_type"] for edge in public["composition_edges"]] == [
        "SUBQUERY_OF",
        "SUBQUERY_OF",
    ]
    assert public["parent_edges"] == [
        {
            "edge_type": "PARENT",
            "source_scope_id": "standard:scope:fedcba9876543210",
            "target_scope_id": "standard:root",
            "evidence_refs": [],
        },
        {
            "edge_type": "PARENT",
            "source_scope_id": "student:scope:0123456789abcdef",
            "target_scope_id": "student:root",
            "evidence_refs": [],
        }
    ]


def test_fallback_binding_is_not_promoted_to_exact_scope_evidence():
    public = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "fallback_diff",
                "query_scope": "root",
                "clause": "SELECT",
                "diff_type": "projection_changed",
            }
        ],
        scope_metadata={
            "scopes": [
                {
                    "scope_id": "student:root",
                    "scope_kind": "ROOT",
                    "side": "student",
                }
            ],
            "diff_bindings": [
                {
                    "diff_id": "fallback_diff",
                    "scope_id": "student:root",
                    "side": "student",
                    "binding_status": "FALLBACK_LABEL",
                }
            ],
        },
    ).to_dict()

    assert public["status"] == "PARTIAL"
    assert _stage_map(_scope(public, "student:root"))["PROJECTION"] == []
    unscoped = next(
        item for item in public["scopes"] if item["scope_id"].startswith("unscoped:")
    )
    assert _stage_map(unscoped)["PROJECTION"] == ["fallback_diff"]
    assert any("fallback scope binding ignored" in item for item in public["limitations"])


def test_declared_scope_contract_does_not_fall_back_when_exact_binding_is_missing():
    graph = build_scoped_query_graph(
        ast_diffs=[
            {
                "diff_id": "contract_gap",
                "query_scope": "root",
                "clause": "WHERE",
                "diff_type": "literal_changed",
            }
        ],
        scope_metadata={
            "scopes": [
                {
                    "scope_id": "student:root",
                    "scope_kind": "ROOT",
                    "side": "student",
                }
            ],
            "diff_bindings": [],
        },
    )
    public = graph.to_dict()

    assert public["status"] == "PARTIAL"
    assert "root" not in {item["scope_id"] for item in public["scopes"]}
    assert graph.conceptual_scope_for_diff("contract_gap") is None
    assert public["conceptual_bindings"][0]["binding_status"] == "PARTIAL"
    assert any(
        "exact scope binding missing for diff contract_gap" in item
        for item in public["limitations"]
    )


def test_recursive_cte_is_the_only_supported_composition_self_edge():
    cte_id = "student:scope:fedcba9876543210"
    public = build_scoped_query_graph(
        scope_metadata={
            "status": "COMPLETE",
            "scopes": [
                {
                    "scope_id": cte_id,
                    "scope_kind": "CTE",
                    "side": "student",
                }
            ],
            "composition_edges": [
                {
                    "edge_type": "CTE_FEEDS",
                    "source_scope_id": cte_id,
                    "target_scope_id": cte_id,
                    "evidence_refs": ["recursive_cte"],
                }
            ],
        }
    ).to_dict()

    assert public["status"] == "COMPLETE"
    assert public["composition_edges"] == [
        {
            "edge_type": "CTE_FEEDS",
            "source_scope_id": cte_id,
            "target_scope_id": cte_id,
            "evidence_refs": ["recursive_cte"],
        }
    ]


def test_upstream_partial_scope_contract_and_limitations_are_propagated():
    public = build_scoped_query_graph(
        scope_metadata={
            "status": "PARTIAL",
            "scopes": [
                {
                    "scope_id": "standard:root",
                    "scope_kind": "ROOT",
                    "side": "standard",
                    "metadata_complete": False,
                }
            ],
            "limitations": ["student AST unavailable"],
        }
    ).to_dict()

    assert public["status"] == "PARTIAL"
    assert _scope(public, "standard:root")["metadata_complete"] is False
    assert "upstream scope metadata status is PARTIAL" in public["limitations"]
    assert any("student AST unavailable" in item for item in public["limitations"])


def test_catalog_and_hard_limits_are_fixed_public_contracts():
    assert len(LOGICAL_STAGE_ORDER) == 14
    assert {
        "CTE_FEEDS",
        "DERIVED_FEEDS",
        "SUBQUERY_OF",
        "CORRELATED_TO",
        "SET_MEMBER_OF",
        "LATERAL_TO",
    }.issubset(COMPOSITION_EDGE_TYPES)
    assert MAX_DIFFS == 256
    assert MAX_SCOPES == 64
    assert MAX_EDGES == 128
