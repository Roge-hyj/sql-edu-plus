# Phase 1 AST Diff Capability

Generated at: `2026-08-18T16:33:50.462168+00:00`

This benchmark follows the IR benchmark: each category first tested for single-query IR recognition is then tested with standard/student SQL pairs to see whether structural differences are identified.

## Summary

- Total cases: `53`
- Buckets: `{'supported': 53}`
- Structure support rate: `100.00%`
- Non-boundary support rate: `100.00%`
- SQLite execution boundaries: `6` (`['rollup_changed', 'grouping_sets_changed', 'cube_changed', 'intersect_all_modifier_changed', 'except_all_modifier_changed', 'lateral_changed']`)
- Unexpected failures: `0`

## IR To AST Diff Continuity

| IR category | IR supported | IR gaps | IR boundaries | AST supported | AST gaps | AST boundaries | status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Aggregate | 3 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| CASE | 3 | 0 | 0 | 1 | 0 | 0 | `diff_supported` |
| CTE | 3 | 0 | 0 | 1 | 0 | 0 | `diff_supported` |
| Comparison | 3 | 0 | 0 | 3 | 0 | 0 | `diff_supported` |
| Correlated Subquery | 2 | 0 | 0 | 1 | 0 | 0 | `diff_supported` |
| DISTINCT | 3 | 0 | 0 | 3 | 0 | 0 | `diff_supported` |
| Dialect Boundary | 3 | 0 | 3 | 1 | 0 | 1 | `diff_supported` |
| GROUP BY | 4 | 0 | 1 | 5 | 0 | 3 | `diff_supported` |
| HAVING | 3 | 0 | 0 | 1 | 0 | 0 | `diff_supported` |
| IN/BETWEEN/LIKE | 5 | 0 | 0 | 3 | 0 | 0 | `diff_supported` |
| JOIN | 5 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| JOIN ON | 4 | 0 | 0 | 4 | 0 | 0 | `diff_supported` |
| LIMIT/OFFSET | 4 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| Logic | 2 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| NULL | 2 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| ORDER BY | 3 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| Recursive CTE | 3 | 0 | 0 | 3 | 0 | 0 | `diff_supported` |
| SELECT | 6 | 0 | 0 | 4 | 0 | 0 | `diff_supported` |
| Set Operation | 4 | 0 | 2 | 4 | 0 | 2 | `diff_supported` |
| Subquery | 5 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| WHERE | 2 | 0 | 0 | 2 | 0 | 0 | `diff_supported` |
| Window | 5 | 0 | 0 | 3 | 0 | 0 | `diff_supported` |

## Category Matrix

| category | supported | known_gap | known_boundary | unexpected_failure |
| --- | ---: | ---: | ---: | ---: |
| Aggregate | 2 | 0 | 0 | 0 |
| CASE | 1 | 0 | 0 | 0 |
| CTE | 1 | 0 | 0 | 0 |
| Comparison | 3 | 0 | 0 | 0 |
| Correlated Subquery | 1 | 0 | 0 | 0 |
| DISTINCT | 3 | 0 | 0 | 0 |
| Dialect Boundary | 1 | 0 | 0 | 0 |
| GROUP BY | 5 | 0 | 0 | 0 |
| HAVING | 1 | 0 | 0 | 0 |
| IN/BETWEEN/LIKE | 3 | 0 | 0 | 0 |
| JOIN | 2 | 0 | 0 | 0 |
| JOIN ON | 4 | 0 | 0 | 0 |
| LIMIT/OFFSET | 2 | 0 | 0 | 0 |
| Logic | 2 | 0 | 0 | 0 |
| NULL | 2 | 0 | 0 | 0 |
| ORDER BY | 2 | 0 | 0 | 0 |
| Recursive CTE | 3 | 0 | 0 | 0 |
| SELECT | 4 | 0 | 0 | 0 |
| Set Operation | 4 | 0 | 0 | 0 |
| Subquery | 2 | 0 | 0 | 0 |
| WHERE | 2 | 0 | 0 | 0 |
| Window | 3 | 0 | 0 | 0 |

## Cases

| result | category | id | expected diff types | actual diff types | note |
| --- | --- | --- | --- | --- | --- |
| `supported` | SELECT | `select_column_dropped` | `['column_dropped']` | `['column_dropped', 'projection_changed']` |  |
| `supported` | SELECT | `select_column_added` | `['column_added']` | `['column_added', 'projection_changed']` |  |
| `supported` | SELECT | `select_expression_changed` | `['projection_changed']` | `['column_added', 'column_dropped', 'projection_changed']` |  |
| `supported` | SELECT | `select_order_changed` | `['projection_changed']` | `['projection_changed']` |  |
| `supported` | DISTINCT | `distinct_missing` | `['distinct_changed']` | `['distinct_changed']` |  |
| `supported` | DISTINCT | `count_distinct_missing` | `['aggregate_distinct_changed']` | `['aggregate_distinct_changed', 'column_added', 'column_dropped', 'projection_changed']` |  |
| `supported` | WHERE | `where_missing` | `['where_changed', 'predicate_missing']` | `['predicate_missing', 'where_changed']` |  |
| `supported` | WHERE | `where_extra` | `['where_changed', 'predicate_added']` | `['predicate_added', 'where_changed']` |  |
| `supported` | Comparison | `comparison_gt_to_gte` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'where_changed']` |  |
| `supported` | Comparison | `comparison_literal_changed` | `['literal_changed']` | `['literal_changed', 'where_changed']` |  |
| `supported` | Comparison | `comparison_column_changed` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'where_changed']` |  |
| `supported` | NULL | `null_is_null_to_equals_null` | `['comparison_operator_changed', 'null_equality_changed']` | `['comparison_operator_changed', 'null_equality_changed', 'where_changed']` |  |
| `supported` | NULL | `null_is_null_to_is_not_null` | `['where_changed']` | `['null_predicate_negation_changed', 'where_changed']` |  |
| `supported` | IN/BETWEEN/LIKE | `in_list_member_removed` | `['in_list_member_removed']` | `['in_list_member_removed', 'where_changed']` |  |
| `supported` | IN/BETWEEN/LIKE | `between_boundary_changed` | `['literal_changed']` | `['literal_changed', 'where_changed']` |  |
| `supported` | IN/BETWEEN/LIKE | `like_pattern_changed` | `['like_pattern_changed']` | `['like_pattern_changed', 'where_changed']` |  |
| `supported` | Logic | `logic_and_to_or` | `['logical_operator_changed']` | `['logical_operator_changed', 'where_changed']` |  |
| `supported` | Logic | `logic_not_removed` | `['where_changed']` | `['where_changed']` |  |
| `supported` | JOIN | `join_missing` | `['join_missing']` | `['from_source_changed', 'join_missing', 'join_on_changed']` |  |
| `supported` | JOIN | `join_type_left_to_inner` | `['join_type_changed']` | `['from_source_changed', 'join_type_changed']` |  |
| `supported` | JOIN ON | `join_on_key_changed` | `['join_on_changed']` | `['join_key_column_changed', 'join_on_changed']` |  |
| `supported` | JOIN ON | `join_on_predicate_removed` | `['join_on_changed']` | `['join_key_column_changed', 'join_on_changed']` |  |
| `supported` | JOIN ON | `join_using_key_changed` | `['join_on_changed']` | `['join_on_changed']` |  |
| `supported` | JOIN ON | `join_using_key_removed` | `['join_on_changed']` | `['join_on_changed']` |  |
| `supported` | GROUP BY | `group_by_column_changed` | `['projection_changed', 'group_by_changed']` | `['column_added', 'column_dropped', 'group_by_changed', 'group_by_expression_changed', 'projection_changed']` |  |
| `supported` | GROUP BY | `group_by_column_missing` | `['column_dropped', 'group_by_changed']` | `['column_dropped', 'group_by_changed', 'grouping_grain_too_coarse', 'projection_changed']` |  |
| `supported` | HAVING | `having_operator_changed` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'having_changed']` |  |
| `supported` | Aggregate | `aggregate_sum_to_avg` | `['aggregate_function_changed']` | `['aggregate_function_changed']` |  |
| `supported` | ORDER BY | `order_direction_changed` | `['order_by_changed']` | `['order_by_changed', 'order_direction_changed']` |  |
| `supported` | ORDER BY | `order_secondary_key_missing` | `['order_by_changed']` | `['order_by_changed', 'order_by_tiebreaker_missing']` |  |
| `supported` | LIMIT/OFFSET | `limit_changed` | `['limit_changed']` | `['limit_changed']` |  |
| `supported` | LIMIT/OFFSET | `offset_changed` | `['limit_changed']` | `['limit_changed']` |  |
| `supported` | Subquery | `subquery_removed` | `['subquery_removed']` | `['from_source_changed', 'predicate_missing', 'subquery_removed', 'where_changed']` |  |
| `supported` | Subquery | `subquery_predicate_changed` | `['literal_changed']` | `['literal_changed', 'where_changed']` |  |
| `supported` | Correlated Subquery | `correlated_subquery_column_changed` | `['correlated_predicate_changed']` | `['comparison_left_column_changed', 'correlated_predicate_changed', 'function_argument_changed', 'where_changed']` |  |
| `supported` | CTE | `cte_body_changed` | `['cte_changed']` | `['cte_changed', 'literal_changed', 'where_changed']` |  |
| `supported` | Recursive CTE | `recursive_cte_boundary_changed` | `['recursive_cte_changed']` | `['literal_changed', 'recursive_cte_changed']` |  |
| `supported` | Set Operation | `set_union_to_union_all` | `['set_operator_changed']` | `['set_all_modifier_changed', 'set_modifier_changed', 'set_operator_changed']` |  |
| `supported` | Set Operation | `set_intersect_to_union` | `['set_operator_changed']` | `['set_operator_changed']` |  |
| `supported` | CASE | `case_branch_changed` | `['projection_changed']` | `['case_changed', 'column_added', 'column_dropped', 'function_argument_changed', 'literal_changed', 'projection_changed']` |  |
| `supported` | Window | `window_partition_changed` | `['window_over_changed']` | `['column_added', 'column_dropped', 'projection_changed', 'window_over_changed']` |  |
| `supported` | Window | `window_function_changed` | `['window_function_changed']` | `['column_added', 'column_dropped', 'projection_changed', 'window_function_changed']` |  |
| `supported` | DISTINCT | `distinct_on_changed` | `['distinct_on_changed']` | `['distinct_on_changed']` |  |
| `supported` | GROUP BY | `rollup_changed` | `['rollup_changed']` | `['group_by_changed', 'group_by_expression_changed', 'rollup_changed']` |  |
| `supported` | GROUP BY | `grouping_sets_changed` | `['grouping_sets_changed']` | `['group_by_changed', 'group_by_expression_changed', 'grouping_sets_changed']` |  |
| `supported` | GROUP BY | `cube_changed` | `['cube_changed']` | `['cube_changed', 'group_by_changed', 'group_by_expression_changed']` |  |
| `supported` | Aggregate | `aggregate_filter_changed` | `['aggregate_filter_changed']` | `['aggregate_filter_changed']` |  |
| `supported` | Recursive CTE | `recursive_search_changed` | `['recursive_search_changed']` | `['recursive_search_changed']` |  |
| `supported` | Recursive CTE | `recursive_cycle_changed` | `['recursive_cycle_changed']` | `['recursive_cycle_changed']` |  |
| `supported` | Window | `qualify_changed` | `['qualify_changed']` | `['comparison_operator_changed', 'qualify_changed']` |  |
| `supported` | Set Operation | `intersect_all_modifier_changed` | `['set_modifier_changed']` | `['set_all_modifier_changed', 'set_modifier_changed', 'set_operator_changed']` |  |
| `supported` | Set Operation | `except_all_modifier_changed` | `['set_modifier_changed']` | `['set_all_modifier_changed', 'set_modifier_changed', 'set_operator_changed']` |  |
| `supported` | Dialect Boundary | `lateral_changed` | `['lateral_changed']` | `['column_dropped', 'correlated_predicate_changed', 'from_source_changed', 'join_missing', 'lateral_changed', 'projection_changed', 'subquery_removed']` | Structure diff is typed; execution remains dialect-scoped. |
