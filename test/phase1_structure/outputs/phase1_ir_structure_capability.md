# Phase 1 IR Structure Capability

Generated at: `2026-07-23T13:36:59.946723+00:00`

This benchmark evaluates IR structure recognition only. It does not test data generation, sandbox equivalence, or mutation isolation.

## Summary

- Total cases: `76`
- Parse success: `76`
- IR build success: `76`
- Buckets: `{'first_class': 68, 'known_gap': 4, 'known_boundary': 4}`
- Non-boundary support rate: `94.44%`

Bucket meanings:

- `first_class`: captured by dedicated IR fields.
- `weak_textual`: visible only as SQL text inside an IR field.
- `known_boundary`: outside current Phase 1 IR/execution boundary.
- `known_gap`: in or near teaching scope, but not first-class typed by the current IR.
- `unexpected_failure`: expected supported structure was not captured.

## Category Matrix

| category | first_class | weak_textual | known_gap | known_boundary | unexpected_failure |
| --- | ---: | ---: | ---: | ---: | ---: |
| Aggregate | 2 | 0 | 1 | 0 | 0 |
| CASE | 3 | 0 | 0 | 0 | 0 |
| CTE | 3 | 0 | 0 | 0 | 0 |
| Comparison | 3 | 0 | 0 | 0 | 0 |
| Correlated Subquery | 2 | 0 | 0 | 0 | 0 |
| DISTINCT | 2 | 0 | 1 | 0 | 0 |
| Dialect Boundary | 0 | 0 | 0 | 4 | 0 |
| GROUP BY | 3 | 0 | 1 | 0 | 0 |
| HAVING | 3 | 0 | 0 | 0 | 0 |
| IN/BETWEEN/LIKE | 5 | 0 | 0 | 0 | 0 |
| JOIN | 5 | 0 | 0 | 0 | 0 |
| JOIN ON | 4 | 0 | 0 | 0 | 0 |
| LIMIT/OFFSET | 4 | 0 | 0 | 0 | 0 |
| Logic | 2 | 0 | 0 | 0 | 0 |
| NULL | 2 | 0 | 0 | 0 | 0 |
| ORDER BY | 3 | 0 | 0 | 0 | 0 |
| Recursive CTE | 1 | 0 | 1 | 0 | 0 |
| SELECT | 6 | 0 | 0 | 0 | 0 |
| Set Operation | 4 | 0 | 0 | 0 | 0 |
| Subquery | 5 | 0 | 0 | 0 | 0 |
| WHERE | 2 | 0 | 0 | 0 | 0 |
| Window | 4 | 0 | 0 | 0 | 0 |

## Cases

| result | category | id | dialect | checks | note |
| --- | --- | --- | --- | --- | --- |
| `first_class` | SELECT | `select_projection_alias_star_expression` | `sqlite` | `2/2` |  |
| `first_class` | DISTINCT | `distinct_top_level_and_count_distinct` | `sqlite` | `3/3` |  |
| `first_class` | WHERE | `where_basic_and_compound_predicates` | `sqlite` | `3/3` |  |
| `first_class` | Comparison | `comparison_all_common_operators` | `sqlite` | `2/2` |  |
| `first_class` | NULL | `null_predicates` | `sqlite` | `2/2` |  |
| `first_class` | IN/BETWEEN/LIKE | `in_between_like_predicates` | `sqlite` | `2/2` |  |
| `first_class` | Logic | `logic_and_or_not_parentheses` | `sqlite` | `1/1` |  |
| `first_class` | JOIN | `join_common_types` | `sqlite` | `2/2` |  |
| `first_class` | JOIN | `join_self_join_aliases` | `sqlite` | `2/2` |  |
| `first_class` | JOIN ON | `join_on_single_multi_nonequi_conditions` | `sqlite` | `2/2` |  |
| `first_class` | GROUP BY | `group_by_single_multi_expression` | `sqlite` | `2/2` |  |
| `first_class` | HAVING | `having_aggregate_predicate` | `sqlite` | `4/4` |  |
| `first_class` | HAVING | `having_without_group_by` | `sqlite` | `4/4` |  |
| `first_class` | Aggregate | `aggregate_common_functions` | `sqlite` | `2/2` |  |
| `first_class` | ORDER BY | `order_by_direction_multi_expression_alias_ordinal` | `sqlite` | `3/3` |  |
| `first_class` | LIMIT/OFFSET | `limit_offset_sqlite` | `sqlite` | `2/2` |  |
| `first_class` | LIMIT/OFFSET | `limit_mysql_offset_count` | `mysql` | `2/2` |  |
| `first_class` | LIMIT/OFFSET | `limit_tsql_top` | `tsql` | `2/2` |  |
| `first_class` | Subquery | `subquery_in_scalar_exists` | `sqlite` | `2/2` |  |
| `first_class` | Correlated Subquery | `correlated_subquery_outer_column_reference` | `sqlite` | `1/1` |  |
| `first_class` | CTE | `cte_multiple_and_dependent` | `sqlite` | `2/2` |  |
| `first_class` | Recursive CTE | `cte_recursive` | `sqlite` | `2/2` |  |
| `first_class` | Set Operation | `set_operation_union_intersect_except` | `sqlite` | `1/1` |  |
| `first_class` | CASE | `case_simple_and_searched` | `sqlite` | `2/2` |  |
| `first_class` | Window | `window_rank_aggregate_frame` | `sqlite` | `2/2` |  |
| `first_class` | SELECT | `select_table_star` | `sqlite` | `2/2` |  |
| `first_class` | SELECT | `select_function_and_cast_projection` | `sqlite` | `2/2` |  |
| `first_class` | SELECT | `select_scalar_subquery_projection` | `sqlite` | `2/2` |  |
| `first_class` | DISTINCT | `distinct_multi_projection` | `sqlite` | `2/2` |  |
| `first_class` | Comparison | `comparison_null_safe_operators` | `sqlite` | `2/2` |  |
| `first_class` | NULL | `null_coalesce_nullif_functions` | `sqlite` | `2/2` |  |
| `first_class` | IN/BETWEEN/LIKE | `predicate_not_in_list` | `sqlite` | `2/2` |  |
| `first_class` | IN/BETWEEN/LIKE | `predicate_in_subquery` | `sqlite` | `2/2` |  |
| `first_class` | IN/BETWEEN/LIKE | `predicate_not_between` | `sqlite` | `2/2` |  |
| `first_class` | IN/BETWEEN/LIKE | `predicate_like_escape` | `sqlite` | `2/2` |  |
| `first_class` | Logic | `logic_demorgan_shape` | `sqlite` | `2/2` |  |
| `first_class` | JOIN | `from_implicit_comma_join` | `sqlite` | `2/2` |  |
| `first_class` | JOIN | `join_natural` | `sqlite` | `2/2` |  |
| `first_class` | JOIN ON | `join_using_single_column` | `sqlite` | `2/2` |  |
| `first_class` | JOIN ON | `join_using_multiple_columns` | `sqlite` | `2/2` |  |
| `first_class` | JOIN | `join_three_table_chain` | `sqlite` | `2/2` |  |
| `first_class` | GROUP BY | `group_by_alias_and_ordinal` | `sqlite` | `2/2` |  |
| `first_class` | HAVING | `having_sum_avg_min_max_predicates` | `sqlite` | `3/3` |  |
| `first_class` | Aggregate | `aggregate_distinct_sum_avg` | `sqlite` | `2/2` |  |
| `first_class` | ORDER BY | `order_by_nulls_first_last` | `sqlite` | `2/2` |  |
| `first_class` | LIMIT/OFFSET | `limit_fetch_first` | `sqlite` | `2/2` |  |
| `first_class` | Subquery | `subquery_derived_table` | `sqlite` | `2/2` |  |
| `first_class` | Subquery | `subquery_not_exists` | `sqlite` | `2/2` |  |
| `first_class` | Subquery | `subquery_any_all` | `sqlite` | `4/4` |  |
| `first_class` | CTE | `cte_column_list` | `sqlite` | `2/2` |  |
| `first_class` | CTE | `cte_three_dependency_chain` | `sqlite` | `2/2` |  |
| `first_class` | CASE | `case_nested` | `sqlite` | `2/2` |  |
| `first_class` | Window | `window_lag_lead_ntile_values` | `sqlite` | `2/2` |  |
| `first_class` | Window | `window_named_reference` | `sqlite` | `3/3` |  |
| `first_class` | SELECT | `select_quoted_identifier` | `sqlite` | `2/2` |  |
| `first_class` | SELECT | `select_unary_parenthesized_modulo` | `sqlite` | `2/2` |  |
| `first_class` | WHERE | `where_boolean_literal` | `sqlite` | `3/3` |  |
| `first_class` | Comparison | `comparison_column_to_column` | `sqlite` | `3/3` |  |
| `first_class` | JOIN ON | `join_non_equi_between_tables` | `sqlite` | `3/3` |  |
| `first_class` | GROUP BY | `group_by_function_expression` | `sqlite` | `3/3` |  |
| `first_class` | ORDER BY | `order_by_collate` | `sqlite` | `3/3` |  |
| `first_class` | Subquery | `subquery_nested_in` | `sqlite` | `2/2` |  |
| `first_class` | Correlated Subquery | `correlated_scalar_subquery` | `sqlite` | `2/2` |  |
| `first_class` | Set Operation | `set_operation_parenthesized_branches` | `sqlite` | `1/1` |  |
| `first_class` | CASE | `case_simple_without_else` | `sqlite` | `2/2` |  |
| `first_class` | Window | `window_range_frame` | `sqlite` | `2/2` |  |
| `known_gap` | DISTINCT | `gap_distinct_on` | `postgres` | `0/0` | PostgreSQL DISTINCT ON is outside current typed DISTINCT IR. |
| `known_gap` | GROUP BY | `gap_grouping_sets` | `sqlite` | `0/0` | GROUPING SETS is not first-class typed in current GROUP BY IR. |
| `known_gap` | Aggregate | `gap_aggregate_filter` | `sqlite` | `0/0` | Aggregate FILTER predicate is not first-class typed in current aggregate IR. |
| `known_gap` | Recursive CTE | `gap_recursive_search_cycle` | `sqlite` | `0/0` | Recursive SEARCH/CYCLE clauses are outside current typed recursive CTE IR. |
| `known_boundary` | Dialect Boundary | `boundary_lateral` | `sqlite` | `0/0` | Known execution/transpilation boundary. |
| `known_boundary` | Dialect Boundary | `boundary_qualify` | `sqlite` | `0/0` | QUALIFY is treated as a dialect boundary for Phase 1. |
| `known_boundary` | Dialect Boundary | `boundary_rollup` | `sqlite` | `0/0` | Known execution/transpilation boundary. |
| `known_boundary` | Dialect Boundary | `boundary_cube` | `sqlite` | `0/0` | Known execution/transpilation boundary. |
| `first_class` | Set Operation | `set_operation_intersect_all` | `sqlite` | `2/2` | IR supports the ALL flag; execution support is evaluated in the sandbox stage. |
| `first_class` | Set Operation | `set_operation_except_all` | `sqlite` | `2/2` | IR supports the ALL flag; execution support is evaluated in the sandbox stage. |
