# Plan 1: Phase 1 SQL Structure Capability Evaluation

## Goal

Evaluate Phase 1 structure capability before testing data generation or mutation.

This plan measures whether the current system can recognize SQL structures inside the supported SQL fragment. It does not claim formal SQL semantic equivalence.

## Scope

The first evaluation target is:

- `SQLStructureIR.from_ast()`: can the system identify structures contained in a single SQL query?
- Later stages:
  - `extract_ast_diffs()`: can the system identify structural differences between standard SQL and student SQL?
  - data generation: can the system synthesize counterexample databases?
  - mutation tests: can the system isolate faulty clauses?

## Stage 1: IR Structure Recognition

For each SQL case, parse the SQL, build `SQLStructureIR`, and check whether expected structures are present.

Pass criteria:

- SQL parses as one query.
- IR build succeeds.
- Expected IR fields or predicate evidence are present.

Important distinction:

- Some structures are first-class IR fields, for example `projection`, `distinct`, `joins`, `group_by`, `having_predicates`, `order_by`, `limit_offset`, `subqueries`, `ctes`, `set_operations`, `case_branches`, and `window_functions`.
- Some structures are currently represented inside predicate SQL text rather than typed IR nodes, for example comparison operators, `IN`, `BETWEEN`, `LIKE`, `IS NULL`, `AND`, `OR`, and `NOT`. These should be recorded separately as textual predicate evidence.

## CFG Productions To Cover

| Category | Structures to Test |
| --- | --- |
| SELECT | projection columns, expressions, `*`, aliases, computed columns |
| DISTINCT | top-level `DISTINCT`, aggregate-level `COUNT(DISTINCT col)` |
| WHERE | basic predicates, compound predicates |
| Comparison | `=`, `<>`, `<`, `<=`, `>`, `>=`, `!=` |
| NULL | `IS NULL`, `IS NOT NULL`, `= NULL` |
| IN/BETWEEN/LIKE | `IN (...)`, `BETWEEN`, `LIKE`, `NOT LIKE` |
| Logic | `AND`, `OR`, `NOT`, parenthesized precedence |
| JOIN | INNER, LEFT, RIGHT, FULL, CROSS, self join |
| JOIN ON | single condition, multiple conditions, non-equi condition |
| GROUP BY | single column, multiple columns, expression grouping |
| HAVING | aggregate predicate, HAVING without GROUP BY |
| Aggregate | `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `COUNT DISTINCT` |
| ORDER BY | ASC/DESC, multiple keys, expression, alias, ordinal |
| LIMIT/OFFSET | `LIMIT`, `OFFSET`, MySQL `LIMIT offset,count`, T-SQL `TOP` |
| Subquery | `IN (SELECT)`, scalar subquery, `EXISTS` |
| Correlated Subquery | outer-column reference |
| CTE | normal CTE, multiple CTEs, dependent CTEs |
| Recursive CTE | `WITH RECURSIVE` |
| Set Operation | `UNION`, `UNION ALL`, `INTERSECT`, `EXCEPT` |
| CASE | simple CASE, searched CASE |
| Window | `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `SUM OVER`, frame |
| Dialect Boundary | `LATERAL`, `ROLLUP`, `CUBE`, `INTERSECT ALL`, `EXCEPT ALL` |

## Metrics

- `parse_success_rate`
- `ir_build_success_rate`
- `case_support_rate`
- `category_support_rate`
- `first_class_ir_support_rate`
- `textual_predicate_support_rate`
- `known_boundary_count`
- `unexpected_failure_count`

## Result Buckets

- `supported`: expected structures are captured by IR or accepted predicate evidence.
- `weak_textual`: structure is detected only inside predicate/projection SQL text, not as typed IR.
- `known_boundary`: the SQL feature is intentionally outside the current executable/IR support boundary.
- `unexpected_failure`: expected supported structure was not parsed or not captured.

## Output Artifacts

The IR capability script should generate:

- `data_construct_test/outputs/phase1_ir_structure_capability.json`
- `data_construct_test/outputs/phase1_ir_structure_capability.md`

These artifacts should record both passed and failed examples so current capability can be quantified.

## Current Typed IR Implementation Status

The first five typed IR priorities have been added to `SQLStructureIR`:

- `predicate_ir`: comparison, NULL, IN, BETWEEN, LIKE, and logical predicate nodes.
- `logic_trees`: nested boolean predicate trees by context.
- `aggregate_functions`: function name, arguments, DISTINCT flag, and context.
- `expression_ir`: typed summaries for projection, GROUP BY, and ORDER BY expressions.
- `set_operation_details`: set operator, ALL/DISTINCT flag, and branch SQL.
- `window_function_details`: window function, partition keys, order keys, and frame.

The original text-oriented fields remain for backward compatibility.
