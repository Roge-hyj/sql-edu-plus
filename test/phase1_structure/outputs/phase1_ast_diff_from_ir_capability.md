# Phase 1 AST Diff Capability

Generated at: `2026-07-23T14:10:59.381003+00:00`

This benchmark follows the IR benchmark: each category first tested for single-query IR recognition is then tested with standard/student SQL pairs to see whether structural differences are identified.

## Summary

- Total cases: `76`
- Buckets: `{'supported': 68, 'known_gap': 4, 'known_boundary': 4}`
- Non-boundary support rate: `94.44%`
- Unexpected failures: `0`

## IR To AST Diff Continuity

| IR category | IR supported | IR gaps | AST supported | AST gaps | status |
| --- | ---: | ---: | ---: | ---: | --- |
| Aggregate | 2 | 1 | 2 | 1 | `diff_supported` |
| CASE | 3 | 0 | 3 | 0 | `diff_supported` |
| CTE | 3 | 0 | 3 | 0 | `diff_supported` |
| Comparison | 3 | 0 | 3 | 0 | `diff_supported` |
| Correlated Subquery | 2 | 0 | 2 | 0 | `diff_supported` |
| DISTINCT | 2 | 1 | 2 | 1 | `diff_supported` |
| Dialect Boundary | 0 | 0 | 0 | 0 | `diff_boundary` |
| GROUP BY | 3 | 1 | 3 | 1 | `diff_supported` |
| HAVING | 3 | 0 | 3 | 0 | `diff_supported` |
| IN/BETWEEN/LIKE | 5 | 0 | 5 | 0 | `diff_supported` |
| JOIN | 5 | 0 | 5 | 0 | `diff_supported` |
| JOIN ON | 4 | 0 | 4 | 0 | `diff_supported` |
| LIMIT/OFFSET | 4 | 0 | 4 | 0 | `diff_supported` |
| Logic | 2 | 0 | 2 | 0 | `diff_supported` |
| NULL | 2 | 0 | 2 | 0 | `diff_supported` |
| ORDER BY | 3 | 0 | 3 | 0 | `diff_supported` |
| Recursive CTE | 1 | 1 | 1 | 1 | `diff_supported` |
| SELECT | 6 | 0 | 6 | 0 | `diff_supported` |
| Set Operation | 4 | 0 | 4 | 0 | `diff_supported` |
| Subquery | 5 | 0 | 5 | 0 | `diff_supported` |
| WHERE | 2 | 0 | 2 | 0 | `diff_supported` |
| Window | 4 | 0 | 4 | 0 | `diff_supported` |
## Category Matrix

| category | supported | known_gap | known_boundary | unexpected_failure |
| --- | ---: | ---: | ---: | ---: |
| Aggregate | 2 | 1 | 0 | 0 |
| CASE | 3 | 0 | 0 | 0 |
| CTE | 3 | 0 | 0 | 0 |
| Comparison | 3 | 0 | 0 | 0 |
| Correlated Subquery | 2 | 0 | 0 | 0 |
| DISTINCT | 2 | 1 | 0 | 0 |
| Dialect Boundary | 0 | 0 | 4 | 0 |
| GROUP BY | 3 | 1 | 0 | 0 |
| HAVING | 3 | 0 | 0 | 0 |
| IN/BETWEEN/LIKE | 5 | 0 | 0 | 0 |
| JOIN | 5 | 0 | 0 | 0 |
| JOIN ON | 4 | 0 | 0 | 0 |
| LIMIT/OFFSET | 4 | 0 | 0 | 0 |
| Logic | 2 | 0 | 0 | 0 |
| NULL | 2 | 0 | 0 | 0 |
| ORDER BY | 3 | 0 | 0 | 0 |
| Recursive CTE | 1 | 1 | 0 | 0 |
| SELECT | 6 | 0 | 0 | 0 |
| Set Operation | 4 | 0 | 0 | 0 |
| Subquery | 5 | 0 | 0 | 0 |
| WHERE | 2 | 0 | 0 | 0 |
| Window | 4 | 0 | 0 | 0 |

## Cases

| result | category | id | expected diff types | actual diff types | note |
| --- | --- | --- | --- | --- | --- |
| `supported` | SELECT | `from_ir__select_projection_alias_star_expression` | `['column_dropped']` | `['column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case select_projection_alias_star_expression. |
| `supported` | DISTINCT | `from_ir__distinct_top_level_and_count_distinct` | `['distinct_changed']` | `['distinct_changed']` | Category-level AST diff pair generated from IR case distinct_top_level_and_count_distinct. |
| `supported` | WHERE | `from_ir__where_basic_and_compound_predicates` | `['where_changed', 'predicate_missing']` | `['predicate_missing', 'where_changed']` | Category-level AST diff pair generated from IR case where_basic_and_compound_predicates. |
| `supported` | Comparison | `from_ir__comparison_all_common_operators` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'where_changed']` | Category-level AST diff pair generated from IR case comparison_all_common_operators. |
| `supported` | NULL | `from_ir__null_predicates` | `['comparison_operator_changed', 'null_equality_changed']` | `['comparison_operator_changed', 'null_equality_changed', 'where_changed']` | Category-level AST diff pair generated from IR case null_predicates. |
| `supported` | IN/BETWEEN/LIKE | `from_ir__in_between_like_predicates` | `['literal_changed']` | `['literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case in_between_like_predicates. |
| `supported` | Logic | `from_ir__logic_and_or_not_parentheses` | `['logical_operator_changed']` | `['logical_operator_changed', 'where_changed']` | Category-level AST diff pair generated from IR case logic_and_or_not_parentheses. |
| `supported` | JOIN | `from_ir__join_common_types` | `['join_type_changed']` | `['join_type_changed']` | Category-level AST diff pair generated from IR case join_common_types. |
| `supported` | JOIN | `from_ir__join_self_join_aliases` | `['join_type_changed']` | `['join_type_changed']` | Category-level AST diff pair generated from IR case join_self_join_aliases. |
| `supported` | JOIN ON | `from_ir__join_on_single_multi_nonequi_conditions` | `['join_on_changed']` | `['join_on_changed']` | Category-level AST diff pair generated from IR case join_on_single_multi_nonequi_conditions. |
| `supported` | GROUP BY | `from_ir__group_by_single_multi_expression` | `['group_by_changed']` | `['column_added', 'column_dropped', 'group_by_changed', 'projection_changed']` | Category-level AST diff pair generated from IR case group_by_single_multi_expression. |
| `supported` | HAVING | `from_ir__having_aggregate_predicate` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'having_changed']` | Category-level AST diff pair generated from IR case having_aggregate_predicate. |
| `supported` | HAVING | `from_ir__having_without_group_by` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'having_changed']` | Category-level AST diff pair generated from IR case having_without_group_by. |
| `supported` | Aggregate | `from_ir__aggregate_common_functions` | `['aggregate_function_changed']` | `['aggregate_function_changed', 'column_added', 'column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case aggregate_common_functions. |
| `supported` | ORDER BY | `from_ir__order_by_direction_multi_expression_alias_ordinal` | `['order_by_changed']` | `['order_by_changed']` | Category-level AST diff pair generated from IR case order_by_direction_multi_expression_alias_ordinal. |
| `supported` | LIMIT/OFFSET | `from_ir__limit_offset_sqlite` | `['limit_changed']` | `['limit_changed']` | Category-level AST diff pair generated from IR case limit_offset_sqlite. |
| `supported` | LIMIT/OFFSET | `from_ir__limit_mysql_offset_count` | `['limit_changed']` | `['limit_changed']` | Category-level AST diff pair generated from IR case limit_mysql_offset_count. |
| `supported` | LIMIT/OFFSET | `from_ir__limit_tsql_top` | `['limit_changed']` | `['limit_changed']` | Category-level AST diff pair generated from IR case limit_tsql_top. |
| `supported` | Subquery | `from_ir__subquery_in_scalar_exists` | `['subquery_removed']` | `['predicate_missing', 'subquery_removed', 'where_changed']` | Category-level AST diff pair generated from IR case subquery_in_scalar_exists. |
| `supported` | Correlated Subquery | `from_ir__correlated_subquery_outer_column_reference` | `['correlated_predicate_changed']` | `['correlated_predicate_changed', 'predicate_added', 'predicate_missing', 'where_changed']` | Category-level AST diff pair generated from IR case correlated_subquery_outer_column_reference. |
| `supported` | CTE | `from_ir__cte_multiple_and_dependent` | `['cte_changed']` | `['cte_changed', 'literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case cte_multiple_and_dependent. |
| `supported` | Recursive CTE | `from_ir__cte_recursive` | `['recursive_cte_changed']` | `['literal_changed', 'recursive_cte_changed', 'where_changed']` | Category-level AST diff pair generated from IR case cte_recursive. |
| `supported` | Set Operation | `from_ir__set_operation_union_intersect_except` | `['set_operator_changed']` | `['set_operator_changed']` | Category-level AST diff pair generated from IR case set_operation_union_intersect_except. |
| `supported` | CASE | `from_ir__case_simple_and_searched` | `['projection_changed']` | `['column_added', 'column_dropped', 'literal_changed', 'projection_changed']` | Category-level AST diff pair generated from IR case case_simple_and_searched. |
| `supported` | Window | `from_ir__window_rank_aggregate_frame` | `['window_over_changed']` | `['column_added', 'column_dropped', 'projection_changed', 'window_over_changed']` | Category-level AST diff pair generated from IR case window_rank_aggregate_frame. |
| `supported` | SELECT | `from_ir__select_table_star` | `['column_dropped']` | `['column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case select_table_star. |
| `supported` | SELECT | `from_ir__select_function_and_cast_projection` | `['column_dropped']` | `['column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case select_function_and_cast_projection. |
| `supported` | SELECT | `from_ir__select_scalar_subquery_projection` | `['column_dropped']` | `['column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case select_scalar_subquery_projection. |
| `supported` | DISTINCT | `from_ir__distinct_multi_projection` | `['distinct_changed']` | `['distinct_changed']` | Category-level AST diff pair generated from IR case distinct_multi_projection. |
| `supported` | Comparison | `from_ir__comparison_null_safe_operators` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'where_changed']` | Category-level AST diff pair generated from IR case comparison_null_safe_operators. |
| `supported` | NULL | `from_ir__null_coalesce_nullif_functions` | `['comparison_operator_changed', 'null_equality_changed']` | `['comparison_operator_changed', 'null_equality_changed', 'where_changed']` | Category-level AST diff pair generated from IR case null_coalesce_nullif_functions. |
| `supported` | IN/BETWEEN/LIKE | `from_ir__predicate_not_in_list` | `['literal_changed']` | `['literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case predicate_not_in_list. |
| `supported` | IN/BETWEEN/LIKE | `from_ir__predicate_in_subquery` | `['literal_changed']` | `['literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case predicate_in_subquery. |
| `supported` | IN/BETWEEN/LIKE | `from_ir__predicate_not_between` | `['literal_changed']` | `['literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case predicate_not_between. |
| `supported` | IN/BETWEEN/LIKE | `from_ir__predicate_like_escape` | `['literal_changed']` | `['literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case predicate_like_escape. |
| `supported` | Logic | `from_ir__logic_demorgan_shape` | `['logical_operator_changed']` | `['logical_operator_changed', 'where_changed']` | Category-level AST diff pair generated from IR case logic_demorgan_shape. |
| `supported` | JOIN | `from_ir__from_implicit_comma_join` | `['join_type_changed']` | `['join_type_changed']` | Category-level AST diff pair generated from IR case from_implicit_comma_join. |
| `supported` | JOIN | `from_ir__join_natural` | `['join_type_changed']` | `['join_type_changed']` | Category-level AST diff pair generated from IR case join_natural. |
| `supported` | JOIN ON | `from_ir__join_using_single_column` | `['join_on_changed']` | `['join_on_changed']` | Category-level AST diff pair generated from IR case join_using_single_column. |
| `supported` | JOIN ON | `from_ir__join_using_multiple_columns` | `['join_on_changed']` | `['join_on_changed']` | Category-level AST diff pair generated from IR case join_using_multiple_columns. |
| `supported` | JOIN | `from_ir__join_three_table_chain` | `['join_type_changed']` | `['join_type_changed']` | Category-level AST diff pair generated from IR case join_three_table_chain. |
| `supported` | GROUP BY | `from_ir__group_by_alias_and_ordinal` | `['group_by_changed']` | `['column_added', 'column_dropped', 'group_by_changed', 'projection_changed']` | Category-level AST diff pair generated from IR case group_by_alias_and_ordinal. |
| `supported` | HAVING | `from_ir__having_sum_avg_min_max_predicates` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'having_changed']` | Category-level AST diff pair generated from IR case having_sum_avg_min_max_predicates. |
| `supported` | Aggregate | `from_ir__aggregate_distinct_sum_avg` | `['aggregate_function_changed']` | `['aggregate_function_changed', 'column_added', 'column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case aggregate_distinct_sum_avg. |
| `supported` | ORDER BY | `from_ir__order_by_nulls_first_last` | `['order_by_changed']` | `['order_by_changed']` | Category-level AST diff pair generated from IR case order_by_nulls_first_last. |
| `supported` | LIMIT/OFFSET | `from_ir__limit_fetch_first` | `['limit_changed']` | `['limit_changed']` | Category-level AST diff pair generated from IR case limit_fetch_first. |
| `supported` | Subquery | `from_ir__subquery_derived_table` | `['subquery_removed']` | `['predicate_missing', 'subquery_removed', 'where_changed']` | Category-level AST diff pair generated from IR case subquery_derived_table. |
| `supported` | Subquery | `from_ir__subquery_not_exists` | `['subquery_removed']` | `['predicate_missing', 'subquery_removed', 'where_changed']` | Category-level AST diff pair generated from IR case subquery_not_exists. |
| `supported` | Subquery | `from_ir__subquery_any_all` | `['subquery_removed']` | `['predicate_missing', 'subquery_removed', 'where_changed']` | Category-level AST diff pair generated from IR case subquery_any_all. |
| `supported` | CTE | `from_ir__cte_column_list` | `['cte_changed']` | `['cte_changed', 'literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case cte_column_list. |
| `supported` | CTE | `from_ir__cte_three_dependency_chain` | `['cte_changed']` | `['cte_changed', 'literal_changed', 'where_changed']` | Category-level AST diff pair generated from IR case cte_three_dependency_chain. |
| `supported` | CASE | `from_ir__case_nested` | `['projection_changed']` | `['column_added', 'column_dropped', 'literal_changed', 'projection_changed']` | Category-level AST diff pair generated from IR case case_nested. |
| `supported` | Window | `from_ir__window_lag_lead_ntile_values` | `['window_over_changed']` | `['column_added', 'column_dropped', 'projection_changed', 'window_over_changed']` | Category-level AST diff pair generated from IR case window_lag_lead_ntile_values. |
| `supported` | Window | `from_ir__window_named_reference` | `['window_over_changed']` | `['column_added', 'column_dropped', 'projection_changed', 'window_over_changed']` | Category-level AST diff pair generated from IR case window_named_reference. |
| `supported` | SELECT | `from_ir__select_quoted_identifier` | `['column_dropped']` | `['column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case select_quoted_identifier. |
| `supported` | SELECT | `from_ir__select_unary_parenthesized_modulo` | `['column_dropped']` | `['column_dropped', 'projection_changed']` | Category-level AST diff pair generated from IR case select_unary_parenthesized_modulo. |
| `supported` | WHERE | `from_ir__where_boolean_literal` | `['where_changed', 'predicate_missing']` | `['predicate_missing', 'where_changed']` | Category-level AST diff pair generated from IR case where_boolean_literal. |
| `supported` | Comparison | `from_ir__comparison_column_to_column` | `['comparison_operator_changed']` | `['comparison_operator_changed', 'where_changed']` | Category-level AST diff pair generated from IR case comparison_column_to_column. |
| `supported` | JOIN ON | `from_ir__join_non_equi_between_tables` | `['join_on_changed']` | `['join_on_changed']` | Category-level AST diff pair generated from IR case join_non_equi_between_tables. |
| `supported` | GROUP BY | `from_ir__group_by_function_expression` | `['group_by_changed']` | `['column_added', 'column_dropped', 'group_by_changed', 'projection_changed']` | Category-level AST diff pair generated from IR case group_by_function_expression. |
| `supported` | ORDER BY | `from_ir__order_by_collate` | `['order_by_changed']` | `['order_by_changed']` | Category-level AST diff pair generated from IR case order_by_collate. |
| `supported` | Subquery | `from_ir__subquery_nested_in` | `['subquery_removed']` | `['predicate_missing', 'subquery_removed', 'where_changed']` | Category-level AST diff pair generated from IR case subquery_nested_in. |
| `supported` | Correlated Subquery | `from_ir__correlated_scalar_subquery` | `['correlated_predicate_changed']` | `['correlated_predicate_changed', 'predicate_added', 'predicate_missing', 'where_changed']` | Category-level AST diff pair generated from IR case correlated_scalar_subquery. |
| `supported` | Set Operation | `from_ir__set_operation_parenthesized_branches` | `['set_operator_changed']` | `['set_operator_changed']` | Category-level AST diff pair generated from IR case set_operation_parenthesized_branches. |
| `supported` | CASE | `from_ir__case_simple_without_else` | `['projection_changed']` | `['column_added', 'column_dropped', 'literal_changed', 'projection_changed']` | Category-level AST diff pair generated from IR case case_simple_without_else. |
| `supported` | Window | `from_ir__window_range_frame` | `['window_over_changed']` | `['column_added', 'column_dropped', 'projection_changed', 'window_over_changed']` | Category-level AST diff pair generated from IR case window_range_frame. |
| `known_gap` | DISTINCT | `from_ir__gap_distinct_on` | `[]` | `[]` | Inherited from IR known_gap: PostgreSQL DISTINCT ON is outside current typed DISTINCT IR. |
| `known_gap` | GROUP BY | `from_ir__gap_grouping_sets` | `[]` | `[]` | Inherited from IR known_gap: GROUPING SETS is not first-class typed in current GROUP BY IR. |
| `known_gap` | Aggregate | `from_ir__gap_aggregate_filter` | `[]` | `[]` | Inherited from IR known_gap: Aggregate FILTER predicate is not first-class typed in current aggregate IR. |
| `known_gap` | Recursive CTE | `from_ir__gap_recursive_search_cycle` | `[]` | `[]` | Inherited from IR known_gap: Recursive SEARCH/CYCLE clauses are outside current typed recursive CTE IR. |
| `known_boundary` | Dialect Boundary | `from_ir__boundary_lateral` | `[]` | `[]` | Inherited from IR known_boundary: Known execution/transpilation boundary. |
| `known_boundary` | Dialect Boundary | `from_ir__boundary_qualify` | `[]` | `[]` | Inherited from IR known_boundary: QUALIFY is treated as a dialect boundary for Phase 1. |
| `known_boundary` | Dialect Boundary | `from_ir__boundary_rollup` | `[]` | `[]` | Inherited from IR known_boundary: Known execution/transpilation boundary. |
| `known_boundary` | Dialect Boundary | `from_ir__boundary_cube` | `[]` | `[]` | Inherited from IR known_boundary: Known execution/transpilation boundary. |
| `supported` | Set Operation | `from_ir__set_operation_intersect_all` | `['set_operator_changed']` | `['set_operator_changed']` | Category-level AST diff pair generated from IR case set_operation_intersect_all. |
| `supported` | Set Operation | `from_ir__set_operation_except_all` | `['set_operator_changed']` | `['set_operator_changed']` | Category-level AST diff pair generated from IR case set_operation_except_all. |
