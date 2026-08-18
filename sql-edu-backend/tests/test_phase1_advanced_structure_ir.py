import json

import pytest
from sqlglot import parse_one

from core.ast_schema import SQLStructureIR


def _ir(sql: str, dialect: str | None = None) -> SQLStructureIR:
    return SQLStructureIR.from_ast(parse_one(sql, read=dialect))


def test_only_outer_select_clauses_populate_top_level_predicate_and_order_fields():
    ir = _ir(
        "SELECT COUNT(*) FILTER (WHERE score > 0) AS positive_count, "
        "(SELECT value FROM hidden WHERE hidden.flag = 1 ORDER BY value) AS inner_value, "
        "ROW_NUMBER() OVER (ORDER BY score DESC) AS rn "
        "FROM exam WHERE active = 1 HAVING COUNT(*) > 0 ORDER BY positive_count"
    )

    assert ir.where_predicates == ["WHERE active = 1"]
    assert ir.having_predicates == ["HAVING COUNT(*) > 0"]
    assert [item["column"] for item in ir.order_by] == ["positive_count"]

    predicate_contexts = {item["context"] for item in ir.predicate_ir}
    assert {"WHERE", "HAVING", "AGGREGATE FILTER"} <= predicate_contexts
    assert all("hidden.flag" not in item["sql"] for item in ir.predicate_ir)


def test_inner_filter_where_and_window_order_do_not_create_top_level_features():
    ir = _ir(
        "SELECT COUNT(*) FILTER (WHERE score > 0), "
        "(SELECT value FROM hidden WHERE hidden.flag = 1 ORDER BY value), "
        "ROW_NUMBER() OVER (ORDER BY score DESC) FROM exam"
    )

    assert ir.where_predicates == []
    assert ir.having_predicates == []
    assert ir.order_by == []
    assert "where" not in ir.feature_kps()
    assert "order-by" not in ir.feature_kps()
    assert ir.window_function_details[0]["order_by"][0]["column"] == "score"


def test_set_operation_uses_statement_order_not_branch_order_or_limit():
    ir = _ir(
        "(SELECT archived_id FROM archived ORDER BY archived_id DESC LIMIT 1) "
        "UNION SELECT active_id FROM active ORDER BY 1"
    )

    assert ir.projection == ["archived_id"]
    assert [item["column"] for item in ir.order_by] == ["1"]
    assert ir.limit_offset == {}


def test_distinct_on_and_qualify_are_first_class_and_json_safe():
    distinct_ir = _ir(
        "SELECT DISTINCT ON (dept, year) dept, name FROM student "
        "ORDER BY dept, year, name",
        "postgres",
    )
    qualify_ir = _ir(
        "SELECT name, ROW_NUMBER() OVER (ORDER BY score DESC) AS rn "
        "FROM exam QUALIFY rn = 1"
    )

    assert distinct_ir.distinct is True
    assert distinct_ir.distinct_on == ["dept", "year"]
    assert {"distinct", "distinct-on"} <= set(distinct_ir.feature_kps())
    assert qualify_ir.qualify_predicates == ["QUALIFY rn = 1"]
    assert "QUALIFY" in {item["context"] for item in qualify_ir.predicate_ir}
    assert "qualify" in qualify_ir.feature_kps()
    json.dumps(distinct_ir.to_dict())
    json.dumps(qualify_ir.to_dict())


def test_aggregate_inside_qualify_retains_qualify_context():
    ir = _ir(
        "SELECT dept FROM exam GROUP BY dept QUALIFY COUNT(*) > 1"
    )

    assert ir.aggregate_functions[0]["context"] == "QUALIFY"


def test_aggregate_filter_is_attached_to_aggregate_and_typed_as_predicate():
    ir = _ir(
        "SELECT COUNT(*) FILTER (WHERE score > 0), "
        "SUM(points) FILTER (WHERE passed = TRUE) FROM exam"
    )

    assert [item["filter_predicate"] for item in ir.aggregate_functions] == [
        "score > 0",
        "passed = TRUE",
    ]
    filter_atoms = [
        item for item in ir.predicate_ir
        if item["context"] == "AGGREGATE FILTER" and item["kind"] != "logic"
    ]
    assert {item["operator"] for item in filter_atoms} == {">", "="}
    assert "aggregate-filter" in ir.feature_kps()


def test_filter_attaches_through_within_group_wrapper():
    ir = _ir(
        "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) "
        "FILTER (WHERE passed = TRUE) FROM exam"
    )

    assert ir.where_predicates == []
    assert ir.order_by == []
    assert ir.aggregate_functions[0]["filter_predicate"] == "passed = TRUE"
    assert "AGGREGATE FILTER" in {
        item["context"] for item in ir.predicate_ir
    }


def test_grouping_extensions_preserve_nested_and_empty_grouping_sets():
    ir = _ir(
        "SELECT a, b, c, SUM(amount) FROM sales GROUP BY a, "
        "GROUPING SETS ((a, b), (), ((a, b), c)), "
        "ROLLUP(a, b), CUBE((a, b), c)"
    )

    grouping = ir.grouping_sets[0]
    assert grouping["kind"] == "grouping_sets"
    assert grouping["items"][1] == {
        "kind": "grouping_set",
        "items": [],
        "empty": True,
        "sql": "()",
    }
    nested = grouping["items"][2]
    assert nested["kind"] == "grouping_set"
    assert nested["items"][0]["kind"] == "grouping_set"
    assert ir.rollup[0]["kind"] == "rollup"
    assert ir.cube[0]["kind"] == "cube"
    assert {"group-by", "grouping-sets", "rollup", "cube"} <= set(
        ir.feature_kps()
    )


@pytest.mark.parametrize(
    ("decoration_sql", "expected", "feature"),
    [
        (
            "SEARCH DEPTH FIRST BY n SET ord",
            {"kind": "DEPTH", "by": "n", "set": "ord", "using": None},
            "recursive-search",
        ),
        (
            "CYCLE n SET is_cycle USING path",
            {
                "kind": "CYCLE",
                "by": "n",
                "set": "is_cycle",
                "using": "path",
            },
            "recursive-cycle",
        ),
    ],
)
def test_recursive_search_cycle_decoration_is_structured(
    decoration_sql, expected, feature
):
    ir = _ir(
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL "
        "SELECT n + 1 FROM t WHERE n < 3) "
        f"{decoration_sql} SELECT n FROM t",
        "postgres",
    )

    decoration = ir.recursive_decorations[0]
    assert {key: decoration[key] for key in expected} == expected
    assert feature in ir.feature_kps()


def test_lateral_source_has_typed_summary_without_changing_execution_policy():
    ir = _ir(
        "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL "
        "(SELECT s.id + 1 AS value) x",
        "postgres",
    )

    assert ir.lateral_sources == [
        {
            "alias": "x",
            "outer": False,
            "source_sql": "(SELECT s.id + 1 AS value)",
            "sql": "LATERAL (SELECT s.id + 1 AS value) AS x",
        }
    ]
    assert "lateral" in ir.feature_kps()
