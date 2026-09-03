from __future__ import annotations

import json

from sqlglot import exp

from core.ast_schema import ASTDiffNode
from core.parseval_data_generator import (
    _build_phase1_scope_metadata,
    _parse_sql,
    extract_ast_diffs,
    generate_and_compare,
)
from core.scoped_query_graph import build_scoped_query_graph


def _scope_metadata(standard_sql: str, student_sql: str | None = None) -> dict:
    student_sql = student_sql or standard_sql
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    assert standard_ast is not None
    assert student_ast is not None
    return _build_phase1_scope_metadata(
        standard_ast,
        student_ast,
        extract_ast_diffs(standard_sql, student_sql),
    )


def test_generate_and_compare_attaches_stable_scope_contract() -> None:
    run = generate_and_compare(
        "course(id INT)",
        "SELECT id FROM course WHERE id > 1",
        "SELECT id FROM course WHERE id >= 1",
        max_rows_per_table=4,
        sql_dialect="sqlite",
    )

    metadata = run.data_evidence["scope_metadata"]
    assert metadata["schema_version"] == "phase1.scope-metadata.v1"
    assert metadata["status"] == "COMPLETE"
    assert metadata["truncated"] is False
    assert [scope["scope_id"] for scope in metadata["scopes"]] == [
        "standard:root",
        "student:root",
    ]
    assert metadata["conceptual_scopes"] == [{
        "conceptual_scope_id": "paired:root",
        "scope_kind": "ROOT",
        "standard_scope_id": "standard:root",
        "student_scope_id": "student:root",
        "pairing_status": "EXACT_AST_PATH",
    }]

    diff_ids = {item["diff_id"] for item in run.data_evidence["ast_diffs"]}
    bindings = metadata["diff_bindings"]
    assert {item["diff_id"] for item in bindings} == diff_ids
    assert {item["side"] for item in bindings} == {"standard", "student"}
    assert all(item["binding_status"].startswith("EXACT") for item in bindings)
    assert {item["conceptual_scope_id"] for item in bindings} == {"paired:root"}

    rebuilt = _scope_metadata(
        "SELECT id FROM course WHERE id > 1",
        "SELECT id FROM course WHERE id >= 1",
    )
    assert json.dumps(metadata, sort_keys=True) == json.dumps(rebuilt, sort_keys=True)


def test_same_cte_name_in_nested_scopes_never_merges_or_cross_links() -> None:
    sql = """
        WITH x AS (SELECT id FROM outer_base)
        SELECT d.id
        FROM (
            WITH x AS (SELECT id FROM inner_base)
            SELECT id FROM x
        ) AS d
        JOIN x AS outer_x ON d.id = outer_x.id
    """
    metadata = _scope_metadata(sql)
    assert metadata["status"] == "COMPLETE"

    standard_scopes = {
        item["scope_id"]: item
        for item in metadata["scopes"]
        if item["side"] == "standard"
    }
    ctes = [
        item
        for item in standard_scopes.values()
        if item["scope_kind"] == "CTE" and item.get("cte_name") == "x"
    ]
    assert len(ctes) == 2
    assert len({item["scope_id"] for item in ctes}) == 2

    derived = next(
        item for item in standard_scopes.values()
        if item["scope_kind"] == "DERIVED"
    )
    inner_cte = next(item for item in ctes if item["parent_scope_id"] == derived["scope_id"])
    outer_cte = next(item for item in ctes if item["parent_scope_id"] == "standard:root")
    cte_edges = {
        (item["source_scope_id"], item["target_scope_id"])
        for item in metadata["composition_edges"]
        if item["edge_type"] == "CTE_FEEDS"
        and item["source_scope_id"].startswith("standard:")
    }
    assert (inner_cte["scope_id"], derived["scope_id"]) in cte_edges
    assert (outer_cte["scope_id"], "standard:root") in cte_edges
    assert (inner_cte["scope_id"], "standard:root") not in cte_edges
    assert (outer_cte["scope_id"], derived["scope_id"]) not in cte_edges


def test_ast_proves_derived_set_and_correlated_edges_without_cross_side_edges() -> None:
    sql = """
        SELECT o.id
        FROM outer_t AS o
        WHERE EXISTS (
            SELECT d.id
            FROM (SELECT id FROM inner_t) AS d
            WHERE d.id = o.id
            UNION ALL
            SELECT i.id FROM inner_t AS i WHERE i.id = o.id
        )
    """
    metadata = _scope_metadata(sql)
    assert metadata["status"] == "COMPLETE"
    assert {item["edge_type"] for item in metadata["composition_edges"]} == {
        "CORRELATED_TO",
        "DERIVED_FEEDS",
        "SET_MEMBER_OF",
        "SUBQUERY_OF",
    }
    assert {item["edge_type"] for item in metadata["parent_edges"]} == {"PARENT"}

    scopes = {item["scope_id"]: item for item in metadata["scopes"]}
    assert {item["scope_kind"] for item in scopes.values()} >= {
        "ROOT",
        "SUBQUERY",
        "SET_BRANCH",
        "DERIVED",
    }
    for edge in metadata["parent_edges"] + metadata["composition_edges"]:
        source_side = scopes[edge["source_scope_id"]]["side"]
        target_side = scopes[edge["target_scope_id"]]["side"]
        assert source_side == target_side


def test_ast_emits_explicit_subquery_of_edges_for_each_sql_side() -> None:
    sql = """
        SELECT o.id
        FROM outer_t AS o
        WHERE o.id IN (SELECT i.id FROM inner_t AS i)
    """

    metadata = _scope_metadata(sql)

    assert metadata["status"] == "COMPLETE"
    scopes = {item["scope_id"]: item for item in metadata["scopes"]}
    for side in ("standard", "student"):
        subquery = next(
            item
            for item in scopes.values()
            if item["side"] == side and item["scope_kind"] == "SUBQUERY"
        )
        expected = (subquery["scope_id"], f"{side}:root")
        subquery_edges = {
            (item["source_scope_id"], item["target_scope_id"])
            for item in metadata["composition_edges"]
            if item["edge_type"] == "SUBQUERY_OF"
        }
        parent_edges = {
            (item["source_scope_id"], item["target_scope_id"])
            for item in metadata["parent_edges"]
        }
        assert expected in subquery_edges
        assert expected in parent_edges


def test_ast_emits_lateral_and_derived_edges_without_marking_plain_derived() -> None:
    lateral_sql = """
        SELECT o.id
        FROM outer_t AS o
        CROSS JOIN LATERAL (SELECT o.id AS id) AS x
    """

    metadata = _scope_metadata(lateral_sql)

    assert metadata["status"] == "COMPLETE"
    scopes = {item["scope_id"]: item for item in metadata["scopes"]}
    for side in ("standard", "student"):
        derived = next(
            item
            for item in scopes.values()
            if item["side"] == side and item["scope_kind"] == "DERIVED"
        )
        assert derived["is_lateral"] is True
        expected = (derived["scope_id"], f"{side}:root")
        by_type = {
            item["edge_type"]: (
                item["source_scope_id"],
                item["target_scope_id"],
            )
            for item in metadata["composition_edges"]
            if item["source_scope_id"] == derived["scope_id"]
        }
        assert by_type["DERIVED_FEEDS"] == expected
        assert by_type["LATERAL_TO"] == expected
        assert by_type["CORRELATED_TO"] == expected

    plain = _scope_metadata(
        "SELECT d.id FROM (SELECT id FROM inner_t) AS d"
    )
    assert all(item["is_lateral"] is False for item in plain["scopes"])
    assert not [
        item
        for item in plain["composition_edges"]
        if item["edge_type"] == "LATERAL_TO"
    ]


def test_full_phase1_output_builds_complete_subquery_and_lateral_graphs() -> None:
    cases = (
        (
            "outer_t(id INT); inner_t(id INT)",
            """
                SELECT o.id FROM outer_t AS o
                WHERE o.id IN (
                    SELECT i.id FROM inner_t AS i WHERE i.id > 1
                )
            """,
            """
                SELECT o.id FROM outer_t AS o
                WHERE o.id IN (
                    SELECT i.id FROM inner_t AS i WHERE i.id >= 1
                )
            """,
            {"SUBQUERY_OF"},
        ),
        (
            "outer_t(id INT)",
            """
                SELECT o.id, x.id
                FROM outer_t AS o
                CROSS JOIN LATERAL (SELECT o.id AS id) AS x
            """,
            """
                SELECT o.id, x.id
                FROM outer_t AS o
                CROSS JOIN LATERAL (SELECT o.id AS id) AS x
            """,
            {"CORRELATED_TO", "DERIVED_FEEDS", "LATERAL_TO"},
        ),
    )

    for schema, standard_sql, student_sql, expected_edges in cases:
        run = generate_and_compare(
            schema,
            standard_sql,
            student_sql,
            max_rows_per_table=4,
            sql_dialect="sqlite",
        )
        metadata = run.data_evidence["scope_metadata"]
        graph = build_scoped_query_graph(run)

        assert metadata["status"] == "COMPLETE"
        assert {
            item["edge_type"] for item in metadata["composition_edges"]
        } >= expected_edges
        assert graph.status == "COMPLETE"
        assert graph.limitations == ()


def test_unprovable_diff_scope_and_non_lateral_outer_reference_are_partial() -> None:
    standard_sql = """
        SELECT o.id
        FROM outer_t AS o
        JOIN (SELECT i.id FROM inner_t AS i WHERE i.id = o.id) AS d
          ON d.id = o.id
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(standard_sql)
    assert standard_ast is not None
    assert student_ast is not None
    unbound = ASTDiffNode(
        clause_category="WHERE",
        diff_type="where_changed",
        standard_node="detached-standard-summary",
        student_node="detached-student-summary",
        knowledge_point_id="where",
    )

    metadata = _build_phase1_scope_metadata(
        standard_ast,
        student_ast,
        [unbound],
    )

    assert metadata["status"] == "PARTIAL"
    assert metadata["diff_bindings"] == []
    assert not [
        item
        for item in metadata["composition_edges"]
        if item["edge_type"] == "CORRELATED_TO"
    ]
    assert any("diff scope unresolved" in item for item in metadata["limitations"])
    assert any(
        "non-lateral derived outer reference not linked" in item
        for item in metadata["limitations"]
    )


def test_scope_contract_emits_only_the_frozen_edge_allowlist() -> None:
    metadata = _scope_metadata(
        "WITH x AS (SELECT id FROM a) SELECT id FROM x UNION SELECT id FROM b"
    )
    edge_types = {
        item["edge_type"]
        for item in metadata["parent_edges"] + metadata["composition_edges"]
    }
    assert edge_types <= {
        "PARENT",
        "CTE_FEEDS",
        "DERIVED_FEEDS",
        "SUBQUERY_OF",
        "CORRELATED_TO",
        "SET_MEMBER_OF",
        "LATERAL_TO",
    }


def test_diff_with_different_side_scopes_has_no_arbitrary_conceptual_scope() -> None:
    sql = """
        SELECT o.id FROM outer_t AS o
        WHERE o.id > 0
          AND EXISTS (SELECT 1 FROM inner_t AS i WHERE i.id = o.id)
    """
    standard_ast = _parse_sql(sql)
    student_ast = _parse_sql(sql)
    assert standard_ast is not None
    assert student_ast is not None
    standard_selects = list(standard_ast.find_all(exp.Select))
    student_selects = list(student_ast.find_all(exp.Select))
    assert len(standard_selects) == len(student_selects) == 2
    mismatched = ASTDiffNode(
        clause_category="WHERE",
        diff_type="where_changed",
        standard_node=standard_selects[1].args["where"],
        student_node=student_selects[0].args["where"],
        knowledge_point_id="where",
    )

    metadata = _build_phase1_scope_metadata(
        standard_ast,
        student_ast,
        [mismatched],
    )

    assert metadata["status"] == "PARTIAL"
    assert len(metadata["diff_bindings"]) == 2
    assert all(
        "conceptual_scope_id" not in item
        for item in metadata["diff_bindings"]
    )
    assert any(
        "diff conceptual scope unresolved" in item
        for item in metadata["limitations"]
    )
