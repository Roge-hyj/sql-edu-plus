"""Compile atomic AST differences into stable distinguishing obligations."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable

from sqlglot import exp
from sqlglot import parse_one

from core.ast_schema import ASTDiffNode

from .schema_scope import ColumnRef, SchemaQualification


@dataclass(frozen=True)
class ConstraintSpec:
    kind: str
    relation: str = ""
    column: str = ""
    value: Any = None
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass
class DistinguishingObligation:
    id: str
    diff_id: str
    diff_type: str
    clause: str
    knowledge_point_id: str | None
    required_tables: set[str] = field(default_factory=set)
    required_columns: set[ColumnRef] = field(default_factory=set)
    minimum_rows: dict[str, int] = field(default_factory=dict)
    hard_constraints: list[ConstraintSpec] = field(default_factory=list)
    soft_constraints: list[ConstraintSpec] = field(default_factory=list)
    conflicts_with: set[str] = field(default_factory=set)
    success_predicate: str = "standard_result_differs_from_student_result"
    estimated_cost: int = 1


_NUMERIC_IDENTIFIER_NAME_RE = re.compile(r"^[0-9][A-Za-z0-9_$]*$")


def _quoted_numeric_identifier_names(*values: Any) -> set[str]:
    """Return quoted numeric-leading column/table names present in AST nodes.

    The generic teaching corpus can spell a schema column such as ``2007``
    without identifier quotes.  The execution path repairs that spelling to
    ``"2007"`` so every parser/backend sees the same column.  Stable evidence
    IDs must treat that representation-only repair as the same source
    identity, while leaving unrelated double-quoted text untouched.
    """
    names: set[str] = set()
    for value in values:
        if isinstance(value, str):
            names.update(re.findall(r'"([0-9][A-Za-z0-9_$]*)"', value))
            continue
        if not hasattr(value, "find_all"):
            continue
        for identifier in value.find_all(exp.Identifier):
            name = str(identifier.this or "")
            if identifier.quoted and _NUMERIC_IDENTIFIER_NAME_RE.fullmatch(name):
                names.add(name)
    return names


def _canonicalize_quoted_numeric_identifiers(value: str, names: set[str]) -> str:
    if not names:
        return value

    def replace(match: re.Match[str]) -> str:
        return match.group(1) if match.group(1) in names else match.group(0)

    return re.sub(r'"([0-9][A-Za-z0-9_$]*)"', replace, value)


def _json_safe(value: Any, *, quoted_numeric_identifiers: set[str] | None = None) -> Any:
    quoted_numeric_identifiers = quoted_numeric_identifiers or set()
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, quoted_numeric_identifiers=quoted_numeric_identifiers)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe(item, quoted_numeric_identifiers=quoted_numeric_identifiers)
            for item in value
        ]
    if hasattr(value, "sql"):
        try:
            return _canonicalize_quoted_numeric_identifiers(
                value.sql(normalize=True), quoted_numeric_identifiers
            )
        except Exception:  # noqa: BLE001 - stable fallback only.
            return _canonicalize_quoted_numeric_identifiers(str(value), quoted_numeric_identifiers)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return (
            _canonicalize_quoted_numeric_identifiers(value, quoted_numeric_identifiers)
            if isinstance(value, str)
            else value
        )
    return str(value)


def _like_context(sql: str) -> dict[str, Any] | None:
    """Extract a bounded LIKE predicate, including NOT LIKE polarity."""
    try:
        root = parse_one(str(sql or ""), read="sqlite")
    except Exception:
        return None
    like = root.find(exp.Like)
    if not isinstance(like, exp.Like) or not isinstance(like.this, exp.Column):
        return None
    pattern = like.expression
    if not isinstance(pattern, exp.Literal) or not pattern.is_string:
        return None
    table = next(root.find_all(exp.Table), None)
    return {
        "relation": str(table.name or "").lower() if table is not None else "",
        "column": str(like.this.name or "").lower(),
        "pattern": str(pattern.this),
        "negated": isinstance(like.parent, exp.Not),
    }


def _query_source_table(sql: str) -> str:
    try:
        root = parse_one(str(sql or ""), read="sqlite")
    except Exception:
        return ""
    table = next(root.find_all(exp.Table), None)
    return str(table.name or "").lower() if table is not None else ""


def stable_diff_id(diff: ASTDiffNode, index: int = 0) -> str:
    """Return the identity shared by AST, obligation, witness and mutation.

    ``index`` is retained in the signature for source compatibility with the
    old callers, but it is deliberately not part of the identity.  The old
    implementation hashed the position in the diff list, which meant that
    inserting a summary diff changed every later ID and broke the evidence
    chain.  Query-block metadata and the serialized nodes already provide the
    semantic location; duplicate, byte-for-byte differences are intentionally
    treated as the same obligation by the Phase 1 compiler.
    """
    quoted_numeric_identifiers = _quoted_numeric_identifier_names(
        diff.standard_node,
        diff.student_node,
    )
    payload = {
        "clause": diff.clause_category,
        "diff_type": diff.diff_type,
        "table": diff.target_table,
        "column": diff.target_column,
        "knowledge_point_id": diff.knowledge_point_id,
        "standard": _json_safe(
            diff.standard_node,
            quoted_numeric_identifiers=quoted_numeric_identifiers,
        ),
        "student": _json_safe(
            diff.student_node,
            quoted_numeric_identifiers=quoted_numeric_identifiers,
        ),
        "extra": _json_safe(
            diff.extra,
            quoted_numeric_identifiers=quoted_numeric_identifiers,
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"diff_{digest}"


def _inferred_target_column(diff: ASTDiffNode) -> str:
    if diff.target_column:
        return str(diff.target_column).lower()
    if diff.diff_type == "aggregate_distinct_changed":
        for node in (diff.standard_node, diff.student_node):
            if isinstance(node, exp.Expression):
                column = node.find(exp.Column)
                if isinstance(column, exp.Column) and column.name:
                    return str(column.name).lower()
        return ""
    if diff.diff_type != "distinct_changed":
        return ""
    for node in (diff.standard_node, diff.student_node):
        if not hasattr(node, "parent"):
            continue
        parent = node.parent
        while parent is not None and not isinstance(parent, exp.Select):
            parent = parent.parent
        if not isinstance(parent, exp.Select):
            continue
        columns = {
            str(column.name).lower()
            for expression in parent.expressions
            for column in expression.find_all(exp.Column)
            if column.name
        }
        if len(columns) == 1:
            return next(iter(columns))
    return ""


def _distinct_on_context(diff: ASTDiffNode) -> dict[str, Any]:
    """Recover the physical key/payload pair behind a DISTINCT ON diff.

    ``DISTINCT ON`` is represented by the modifier node itself, so the AST
    diff has no ordinary target column.  The witness, however, needs two
    rows with the same ON key and different selected payload.  Deriving that
    pair here keeps the obligation, planner and validator on the same
    evidence path instead of treating it as a generic projection change.
    """
    node = next(
        (
            item
            for item in (diff.standard_node, diff.student_node)
            if isinstance(item, exp.Distinct) and item.args.get("on") is not None
            or isinstance(item, exp.Tuple)
        ),
        None,
    )
    if not isinstance(node, (exp.Distinct, exp.Tuple)):
        return {}
    on = node.args.get("on") if isinstance(node, exp.Distinct) else node
    select = node.parent
    while select is not None and not isinstance(select, exp.Select):
        select = select.parent
    if not isinstance(select, exp.Select):
        return {}
    key_nodes = on.expressions if isinstance(on, exp.Tuple) else (on,)
    key_columns = [
        str(item.name).lower()
        for expression in key_nodes
        for item in ([expression] if isinstance(expression, exp.Column) else expression.find_all(exp.Column))
        if isinstance(item, exp.Column) and item.name
    ]
    if not key_columns:
        return {}
    payload_column = next(
        (
            str(column.name).lower()
            for expression in select.expressions or ()
            for column in (
                [expression]
                if isinstance(expression, exp.Column)
                else expression.find_all(exp.Column)
            )
            if isinstance(column, exp.Column)
            and column.name
            and str(column.name).lower() not in key_columns
        ),
        "",
    )
    from_clause = select.args.get("from_") or select.args.get("from")
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    relation = str(source.name).lower() if isinstance(source, exp.Table) else ""
    return {
        "source_table": relation,
        "key_columns": tuple(key_columns),
        "payload_column": payload_column,
    }


def _column_refs(
    diff: ASTDiffNode,
    relation: str = "",
    column: str = "",
) -> set[ColumnRef]:
    column = column or _inferred_target_column(diff)
    if not column:
        return set()
    scope = str(diff.extra.get("query_scope") or "root")
    references = {
        ColumnRef(
            relation=relation or str(diff.target_table or "").lower(),
            column=column,
            query_scope=scope,
        )
    }
    if diff.diff_type == "comparison_operator_changed":
        for side in ("standard", "student"):
            if str(diff.extra.get(f"{side}_value_kind") or "").lower() != "column":
                continue
            right_column = str(
                diff.extra.get(f"{side}_right_column") or ""
            ).lower()
            if not right_column:
                continue
            references.add(ColumnRef(
                relation=(
                    str(diff.extra.get(f"{side}_right_table") or "").lower()
                    or relation
                    or str(diff.target_table or "").lower()
                ),
                column=right_column,
                query_scope=scope,
            ))
    if diff.diff_type in {"window_over_changed", "window_function_changed"}:
        # A window diff often has no ordinary target column.  Its physical
        # witness columns are the partition and ORDER BY expressions from the
        # standard window, with the student side added when it introduces a
        # different key.  Keeping these refs in the obligation lets the
        # planner lock the exact cells instead of relying on column-name
        # heuristics in a later legacy probe.
        for side in ("standard", "student"):
            over = diff.extra.get(f"{side}_over") or {}
            if not isinstance(over, dict):
                continue
            for expression in tuple(over.get("partition_by") or ()) + tuple(
                over.get("order_columns") or ()
            ):
                text = str(expression or "").strip()
                if not text:
                    continue
                column = text.split(".")[-1].strip('`" ')
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", column):
                    references.add(ColumnRef(
                        relation=relation or str(diff.target_table or "").lower(),
                        column=column.lower(),
                        query_scope=scope,
                    ))
    return references


def _simple_in_exists_metadata(diff: ASTDiffNode) -> dict[str, str] | None:
    """Describe the narrow uncorrelated IN/EXISTS rewrite we can witness.

    This mirrors the data-generator guard intentionally: only a root single
    table SELECT with one positive, single-table, no-filter subquery on each
    side is promoted from a generic predicate obligation.  Broader shapes keep
    the ordinary predicate validator and remain fail-closed.
    """
    standard_sql = str(diff.extra.get("standard_query_sql") or "")
    student_sql = str(diff.extra.get("student_query_sql") or "")
    if not standard_sql or not student_sql:
        return None
    try:
        standard_ast = parse_one(standard_sql, read="sqlite")
        student_ast = parse_one(student_sql, read="sqlite")
    except Exception:
        return None
    if not isinstance(standard_ast, exp.Select) or not isinstance(student_ast, exp.Select):
        return None

    def root(select: exp.Select) -> tuple[exp.Table, exp.Expression] | None:
        from_clause = select.args.get("from_") or select.args.get("from")
        if (
            not isinstance(from_clause, exp.From)
            or not isinstance(from_clause.this, exp.Table)
            or from_clause.expressions
            or len(select.expressions or ()) != 1
        ):
            return None
        projection = select.expressions[0]
        projection = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(projection, exp.Column):
            return None
        if any(
            select.args.get(key) is not None
            for key in (
                "joins", "group", "having", "order", "limit", "offset",
                "qualify", "distinct", "with", "with_",
            )
        ):
            return None
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            return None
        predicate = where.this
        while isinstance(predicate, exp.Paren):
            predicate = predicate.this
        if not isinstance(predicate, (exp.In, exp.Exists)):
            return None
        if isinstance(predicate, (exp.In, exp.Exists)) and isinstance(predicate.parent, exp.Not):
            return None
        return from_clause.this, predicate

    standard_root = root(standard_ast)
    student_root = root(student_ast)
    if standard_root is None or student_root is None:
        return None
    standard_table, standard_predicate = standard_root
    student_table, student_predicate = student_root
    if isinstance(standard_predicate, exp.In) == isinstance(student_predicate, exp.In):
        return None
    in_node = standard_predicate if isinstance(standard_predicate, exp.In) else student_predicate
    exists_node = standard_predicate if isinstance(standard_predicate, exp.Exists) else student_predicate
    if not isinstance(in_node, exp.In) or not isinstance(exists_node, exp.Exists):
        return None
    outer_table = str(standard_table.name or "").lower()
    if not outer_table or outer_table != str(student_table.name or "").lower():
        return None

    in_query = in_node.args.get("query")
    in_inner = in_query.this if isinstance(in_query, exp.Subquery) else None
    exists_inner = exists_node.this if isinstance(exists_node.this, exp.Select) else None
    if not isinstance(in_inner, exp.Select) or not isinstance(exists_inner, exp.Select):
        return None

    def inner_table(select: exp.Select) -> exp.Table | None:
        from_clause = select.args.get("from_") or select.args.get("from")
        if (
            not isinstance(from_clause, exp.From)
            or not isinstance(from_clause.this, exp.Table)
            or from_clause.expressions
            or len(select.expressions or ()) != 1
        ):
            return None
        if any(
            select.args.get(key) is not None
            for key in (
                "where", "joins", "group", "having", "order", "limit", "offset",
                "qualify", "distinct", "with", "with_",
            )
        ):
            return None
        return from_clause.this

    in_table = inner_table(in_inner)
    exists_table = inner_table(exists_inner)
    if in_table is None or exists_table is None:
        return None
    inner_name = str(in_table.name or "").lower()
    if not inner_name or inner_name != str(exists_table.name or "").lower() or inner_name == outer_table:
        return None
    projected = in_inner.expressions[0]
    projected = projected.this if isinstance(projected, exp.Alias) else projected
    exists_projected = exists_inner.expressions[0]
    exists_projected = exists_projected.this if isinstance(exists_projected, exp.Alias) else exists_projected
    if not isinstance(projected, exp.Column):
        return None
    if not isinstance(exists_projected, (exp.Column, exp.Literal, exp.Boolean, exp.Null)):
        return None

    def local_ref(column: exp.Column, select: exp.Select) -> tuple[str, str] | None:
        table_bindings = {
            str(table.alias or table.name or "").lower(): str(table.name or "").lower()
            for table in select.find_all(exp.Table)
            if table.find_ancestor(exp.Select) is select
        }
        qualifier = str(column.table or "").lower()
        if qualifier:
            physical = table_bindings.get(qualifier)
            return (physical, str(column.name or "").lower()) if physical else None
        if len(set(table_bindings.values())) != 1:
            return None
        return next(iter(table_bindings.values())), str(column.name or "").lower()

    outer_ref = local_ref(in_node.this, standard_ast)
    inner_ref = local_ref(projected, in_inner)
    if outer_ref is None or inner_ref is None or outer_ref[0] != outer_table or inner_ref[0] != inner_name:
        return None
    if isinstance(exists_projected, exp.Column):
        exists_ref = local_ref(exists_projected, exists_inner)
        if exists_ref is None or exists_ref[0] != inner_name:
            return None
    return {
        "standard_source_table": outer_table,
        "standard_membership_table": inner_name,
        "standard_outer_column": outer_ref[1],
        "standard_membership_column": inner_ref[1],
        "student_source_table": outer_table,
        "student_membership_table": inner_name,
        "student_outer_column": outer_ref[1],
        "student_membership_column": inner_ref[1],
    }


def _constraint_templates(
    diff: ASTDiffNode,
    relation: str = "",
    column: str = "",
    correlated_metadata: dict[str, Any] | None = None,
) -> tuple[list[ConstraintSpec], int, int]:
    relation = relation or str(diff.target_table or "").lower()
    column = column or _inferred_target_column(diff)
    value = diff.get("value", diff.get("standard_value"))
    diff_type = diff.diff_type
    aggregate_metadata = _aggregate_metadata(diff)
    if correlated_metadata:
        aggregate_metadata.update(correlated_metadata)
    if diff_type == "where_changed":
        standard_like = _like_context(str(diff.extra.get("standard_sql") or ""))
        student_like = _like_context(str(diff.extra.get("student_sql") or ""))
        if standard_like and student_like:
            return [ConstraintSpec(
                "like_pattern_separation",
                standard_like["relation"] or relation,
                standard_like["column"],
                metadata=(
                    ("standard_pattern", standard_like["pattern"]),
                    ("student_pattern", student_like["pattern"]),
                    ("standard_negated", standard_like["negated"]),
                    ("student_negated", student_like["negated"]),
                ),
            )], 3, 2
        standard_fragment = str(diff.extra.get("standard_sql") or "")
        student_fragment = str(diff.extra.get("student_sql") or "")
        if re.search(
            r"\b(?:NOT\s+)?(?:EXISTS|IN)\b",
            standard_fragment,
            re.IGNORECASE,
        ) or re.search(
            r"\b(?:NOT\s+)?(?:EXISTS|IN)\b",
            student_fragment,
            re.IGNORECASE,
        ) or re.search(
            r"\bSELECT\b",
            standard_fragment,
            re.IGNORECASE,
        ) or re.search(
            r"\bSELECT\b",
            student_fragment,
            re.IGNORECASE,
        ):
            return [ConstraintSpec(
                "subquery_predicate_paths",
                relation,
                column,
                metadata=(
                    ("standard_sql", standard_fragment),
                    ("student_sql", student_fragment),
                ),
            )], 2, 2
    if (
        diff_type in {"comparison_operator_changed", "literal_changed"}
        and aggregate_metadata.get("scalar_subquery_boundary")
    ):
        return [ConstraintSpec(
            "scalar_subquery_boundary_path",
            relation,
            column,
            metadata=tuple(sorted(aggregate_metadata.items())),
        )], 3, 2
    if (
        diff_type in {"comparison_operator_changed", "literal_changed"}
        and aggregate_metadata.get("filtered_aggregate_boundary")
    ):
        minimum_rows = int(
            aggregate_metadata.get("required_path_rows") or 3
        )
        return [ConstraintSpec(
            "filtered_aggregate_boundary_path",
            relation,
            column,
            value,
            metadata=tuple(sorted(aggregate_metadata.items())),
        )], minimum_rows, 3
    if (
        diff_type == "comparison_operator_changed"
        and {
            str(diff.extra.get("standard_op") or "").upper(),
            str(diff.extra.get("student_op") or "").upper(),
        }
        & {"NULLSAFEEQ", "NULLSAFENEQ"}
    ):
        standard_value_kind = str(
            diff.extra.get("standard_value_kind") or "literal"
        ).lower()
        student_value_kind = str(
            diff.extra.get("student_value_kind") or "literal"
        ).lower()
        standard_right_column = str(
            diff.extra.get("standard_right_column") or ""
        ).lower()
        student_right_column = str(
            diff.extra.get("student_right_column") or ""
        ).lower()
        same_right_column = bool(
            standard_value_kind == "column"
            and student_value_kind == "column"
            and standard_right_column
            and standard_right_column == student_right_column
        )
        return [ConstraintSpec(
            "null_safe_comparison_paths",
            relation,
            column,
            value if standard_value_kind != "column" else None,
            metadata=(
                ("standard_op", diff.extra.get("standard_op")),
                ("student_op", diff.extra.get("student_op")),
                ("standard_value", diff.extra.get("value")),
                ("student_value", diff.extra.get("student_value")),
                ("standard_value_kind", standard_value_kind),
                ("student_value_kind", student_value_kind),
                ("standard_right_column", standard_right_column),
                ("student_right_column", student_right_column),
                ("same_right_column", same_right_column),
            ),
        )], 4 if same_right_column and standard_right_column != column else 3, 2
    if diff_type in {"comparison_operator_changed", "literal_changed"} and aggregate_metadata.get("standard_aggregate_function"):
        minimum_rows = 4
        if (
            str(aggregate_metadata.get("standard_aggregate_function") or "").upper() == "COUNT"
            and isinstance(value, (int, float))
            and int(value) == value
            and value > 0
        ):
            minimum_rows = int(value)
        return [ConstraintSpec(
            "aggregate_boundary_group",
            relation,
            column,
            value,
            metadata=tuple(sorted(aggregate_metadata.items())),
        )], minimum_rows, 2
    if diff_type in {"comparison_operator_changed", "literal_changed"}:
        return [ConstraintSpec(
            "boundary_tristate",
            relation,
            column,
            value,
            metadata=(
                ("standard_value_kind", diff.extra.get("standard_value_kind")),
                ("student_value_kind", diff.extra.get("student_value_kind")),
            ),
        )], 3, 1
    if diff_type == "regex_pattern_changed":
        return [ConstraintSpec(
            "regex_pattern_separation",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_pattern",
                    "student_pattern",
                    "standard_query_sql",
                    "student_query_sql",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 2
    if diff_type == "like_pattern_changed":
        return [ConstraintSpec(
            "like_pattern_separation",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_pattern",
                    "student_pattern",
                    "standard_escape",
                    "student_escape",
                    "case_insensitive",
                    "standard_query_sql",
                    "student_query_sql",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 2
    if diff_type == "glob_pattern_changed":
        return [ConstraintSpec(
            "glob_pattern_separation",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_pattern",
                    "student_pattern",
                    "standard_query_sql",
                    "student_query_sql",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 2
    if diff_type == "similar_pattern_changed":
        return [ConstraintSpec(
            "similar_pattern_separation",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_pattern",
                    "student_pattern",
                    "standard_escape",
                    "student_escape",
                    "standard_query_sql",
                    "student_query_sql",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 2
    if diff_type == "aggregate_function_changed":
        return [ConstraintSpec(
            "aggregate_function_separation",
            relation,
            column,
            metadata=tuple(sorted(aggregate_metadata.items())),
        )], 3, 2
    if diff_type == "aggregate_filter_changed":
        return [ConstraintSpec(
            "aggregate_filter_paths",
            relation or str(diff.extra.get("standard_source_table") or ""),
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_filter_predicate",
                    "student_filter_predicate",
                    "standard_source_table",
                    "standard_group_columns",
                    "standard_query_sql",
                    "student_query_sql",
                )
                if diff.extra.get(key) not in (None, "")
            ),
        )], 4, 2
    if diff_type in {"predicate_missing", "predicate_added"}:
        if diff.extra.get("subquery_depth"):
            return [ConstraintSpec(
                "subquery_predicate_paths",
                relation,
                column,
                metadata=tuple(
                    (key, diff.extra.get(key) or "")
                    for key in (
                        "standard_query_sql",
                        "student_query_sql",
                        "standard_sql",
                        "student_sql",
                    )
                ),
            )], 2, 2
        simple_membership = _simple_in_exists_metadata(diff)
        if simple_membership is not None:
            return [ConstraintSpec(
                "subquery_membership_paths",
                simple_membership["standard_source_table"],
                simple_membership["standard_outer_column"],
                metadata=tuple(
                    (key, value)
                    for key, value in (
                        *simple_membership.items(),
                        ("require_inner_null", False),
                        ("require_outer_null", False),
                    )
                    if value not in (None, "")
                ),
            )], 2, 3
        # A non-trivial IN/EXISTS predicate (for example one whose inner
        # query has its own WHERE or aggregate) cannot be reduced to a pair
        # of physical membership columns.  Validate the bounded full query
        # pair instead of sending the subquery expression to the row-local
        # truth evaluator.
        standard_fragment = str(diff.extra.get("standard_sql") or "")
        student_fragment = str(diff.extra.get("student_sql") or "")
        if re.search(
            r"\b(?:NOT\s+)?(?:EXISTS|IN)\b",
            standard_fragment,
            re.IGNORECASE,
        ) or re.search(
            r"\b(?:NOT\s+)?(?:EXISTS|IN)\b",
            student_fragment,
            re.IGNORECASE,
        ) or re.search(
            r"\bSELECT\b",
            standard_fragment,
            re.IGNORECASE,
        ) or re.search(
            r"\bSELECT\b",
            student_fragment,
            re.IGNORECASE,
        ):
            return [ConstraintSpec(
                "subquery_predicate_paths",
                relation,
                column,
                metadata=tuple(
                    (key, diff.extra.get(key) or "")
                    for key in (
                        "standard_query_sql",
                        "student_query_sql",
                        "standard_sql",
                        "student_sql",
                    )
                ),
            )], 2, 2
        return [ConstraintSpec(
            "predicate_positive_negative_paths",
            relation,
            column,
            metadata=(
                ("standard_sql", diff.extra.get("standard_sql")),
                ("student_sql", diff.extra.get("student_sql")),
                ("standard_query_sql", diff.extra.get("standard_query_sql")),
                ("student_query_sql", diff.extra.get("student_query_sql")),
            ),
        )], 3, 1
    if diff_type in {"logical_operator_changed", "logical_precedence_tree_changed"}:
        return [ConstraintSpec(
            "boolean_truth_table",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_predicate_sql",
                    "student_predicate_sql",
                    "standard_source_table",
                )
                if diff.extra.get(key)
            ),
        )], 4, 2
    if diff_type == "join_predicate_placement_changed":
        metadata = _join_metadata(diff)
        return [ConstraintSpec(
            "outer_join_predicate_placement_path",
            relation,
            column,
            metadata=metadata,
        )], 3, 2
    if diff_type in {"join_missing", "join_type_changed"}:
        metadata = _join_metadata(diff)
        return [ConstraintSpec("matched_and_dangling_join_rows", relation, column, metadata=metadata)], 3, 2
    if diff_type == "join_on_changed":
        metadata = _join_metadata(diff)
        return [ConstraintSpec("standard_join_equal_student_join_unequal", relation, column, metadata=metadata)], 3, 2
    if diff_type in {
        "group_by_changed",
        "group_by_expression_changed",
        "grouping_grain_too_fine",
        "grouping_grain_too_coarse",
    }:
        return [ConstraintSpec(
            "group_grain_split",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key, ()))
                for key in (
                    "standard_keys", "student_keys", "standard_group_columns",
                    "student_group_columns", "standard_source_table",
                    "student_source_table",
                )
                if key in diff.extra and diff.extra.get(key) is not None
            ),
        )], 4, 2
    if diff_type in {"having_changed", "aggregate_argument_changed"}:
        return [ConstraintSpec(
            "aggregate_boundary_group",
            relation,
            column,
            aggregate_metadata.get("boundary_value", value),
            metadata=tuple(sorted(aggregate_metadata.items())),
        )], 4, 2
    if diff_type in {"set_operator_changed", "set_modifier_changed"}:
        return [ConstraintSpec(
            "set_left_right_overlap",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_op", "student_op",
                    "standard_modifier", "student_modifier",
                    "standard_left_source_table", "standard_right_source_table",
                    "standard_projection_columns",
                    "student_left_source_table", "student_right_source_table",
                    "student_projection_columns",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 2
    if diff_type in {"window_over_changed", "window_function_changed"}:
        window_metadata = {}
        standard_over = diff.extra.get("standard_over") or {}
        student_over = diff.extra.get("student_over") or {}
        if isinstance(standard_over, dict):
            window_metadata["standard_window_partition"] = tuple(standard_over.get("partition_by") or ())
            window_metadata["standard_window_order"] = standard_over.get("order") or ""
            window_metadata["standard_window_frame"] = standard_over.get("frame") or ""
            window_metadata["standard_window_order_items"] = tuple(
                standard_over.get("order_items") or ()
            )
            window_metadata["standard_window_order_columns"] = tuple(
                standard_over.get("order_columns") or ()
            )
        if isinstance(student_over, dict):
            window_metadata["student_window_partition"] = tuple(student_over.get("partition_by") or ())
            window_metadata["student_window_order"] = student_over.get("order") or ""
            window_metadata["student_window_frame"] = student_over.get("frame") or ""
            window_metadata["student_window_order_items"] = tuple(
                student_over.get("order_items") or ()
            )
            window_metadata["student_window_order_columns"] = tuple(
                student_over.get("order_columns") or ()
            )
        window_metadata["standard_window_function"] = (
            diff.extra.get("standard_function") or ""
        )
        window_metadata["student_window_function"] = (
            diff.extra.get("student_function") or ""
        )
        window_metadata["standard_window_source_table"] = (
            diff.extra.get("standard_window_source_table") or ""
        )
        window_metadata["student_window_source_table"] = (
            diff.extra.get("student_window_source_table") or ""
        )
        return [ConstraintSpec(
            "window_partitions_and_ties",
            relation,
            column,
            metadata=tuple(sorted(window_metadata.items())),
        )], 4, 3
    if diff_type == "order_by_changed":
        source_table = str(
            diff.extra.get("standard_source_table")
            or _query_source_table(str(diff.extra.get("standard_query_sql") or ""))
            or _query_source_table(str(diff.extra.get("standard_sql") or ""))
            or relation
        ).lower()
        return [ConstraintSpec(
            "order_key_separation",
            source_table,
            column,
            metadata=(
                ("standard_query_sql", diff.extra.get("standard_query_sql") or ""),
                ("student_query_sql", diff.extra.get("student_query_sql") or ""),
                ("standard_sql", diff.extra.get("standard_sql") or ""),
                ("student_sql", diff.extra.get("student_sql") or ""),
                ("standard_source_table", source_table),
            ),
        )], 3, 1
    if diff_type in {
        "order_direction_changed",
        "order_by_tiebreaker_missing",
        "order_by_key_added",
        "order_nulls_changed",
    }:
        order_metadata = {
            "standard_order_keys": tuple(diff.extra.get("standard_order_keys") or ()),
            "student_order_keys": tuple(diff.extra.get("student_order_keys") or ()),
            "standard_nulls_first": tuple(diff.extra.get("standard_nulls_first") or ()),
            "student_nulls_first": tuple(diff.extra.get("student_nulls_first") or ()),
            "standard_source_table": diff.extra.get("standard_source_table") or "",
        }
        return [ConstraintSpec(
            "order_key_separation",
            relation,
            column,
            metadata=tuple(sorted(order_metadata.items())),
        )], 3, 1
    if diff_type == "boolean_projection_truth_test_changed":
        return [ConstraintSpec(
            "projection_boolean_tristate_paths",
            relation or str(diff.extra.get("standard_source_table") or ""),
            column or str(diff.extra.get("predicate_column") or ""),
            diff.extra.get("predicate_value"),
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "predicate_sql",
                    "predicate_operator",
                    "predicate_value",
                    "position",
                    "standard_is_true",
                    "student_is_true",
                    "standard_source_table",
                    "standard_query_sql",
                    "student_query_sql",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 1
    if diff_type == "projection_changed":
        return [ConstraintSpec(
            "projection_value_paths",
            relation,
            column,
            metadata=(
                ("standard_sql", diff.extra.get("standard_sql")),
                ("student_sql", diff.extra.get("student_sql")),
            ),
        )], 2, 1
    if diff_type == "limit_changed":
        return [ConstraintSpec(
            "limit_row_count_paths",
            relation,
            column,
            metadata=(
                ("standard_sql", diff.extra.get("standard_sql")),
                ("student_sql", diff.extra.get("student_sql")),
            ),
        )], 3, 1
    if diff_type in {
        "case_changed",
        "case_else_missing",
        "case_else_added",
        "case_when_missing",
        "case_when_added",
    }:
        return [ConstraintSpec(
            "case_unmatched_and_branch_rows",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_case_when_predicates",
                    "student_case_when_predicates",
                    "standard_source_table",
                    "required_case_branch_indexes",
                )
                if diff.extra.get(key) is not None
            ),
        )], 3, 2
    if diff_type in {
        "correlated_predicate_changed",
        "subquery_membership_key_changed",
        "subquery_added",
        "subquery_removed",
        "null_sensitive_antijoin_equivalence",
        "in_exists_equivalence",
    }:
        membership_metadata = {
            **diff.extra,
            **aggregate_metadata,
            "require_inner_null": bool(
                diff.extra.get(
                    "require_inner_null",
                    aggregate_metadata.get("require_inner_null"),
                )
            ),
            "require_outer_null": bool(
                diff.extra.get(
                    "require_outer_null",
                    aggregate_metadata.get("require_outer_null"),
                )
            ),
        }
        return [ConstraintSpec(
            "subquery_membership_paths",
            relation,
            column,
            metadata=tuple(
                (key, membership_metadata.get(key))
                for key in (
                    "standard_source_table",
                    "standard_membership_table",
                    "standard_outer_column",
                    "standard_membership_column",
                    "student_source_table",
                    "student_membership_table",
                    "student_outer_column",
                    "student_membership_column",
                    "require_inner_null",
                    "require_outer_null",
                )
                if membership_metadata.get(key) not in (None, "")
            ),
        )], 3, 3
    if diff_type == "in_predicate_negation_changed":
        if diff.extra.get("standard_membership_table"):
            return [ConstraintSpec(
                "subquery_membership_paths",
                relation,
                column,
                metadata=tuple(
                    (key, diff.extra.get(key))
                    for key in (
                        "standard_source_table",
                        "standard_membership_table",
                        "standard_outer_column",
                        "standard_membership_column",
                    )
                    if diff.extra.get(key)
                ),
            )], 3, 3
        return [ConstraintSpec(
            "in_list_membership_paths",
            relation,
            column,
            metadata=(("standard_in_values", diff.extra.get("standard_in_values") or ()),),
        )], 3, 1
    if diff_type in {"in_list_member_removed", "in_list_member_added"}:
        standard_values = tuple(diff.extra.get("values") or ())
        student_values = tuple(diff.extra.get("student_values") or ())
        return [ConstraintSpec(
            "in_list_membership_paths",
            relation,
            column,
            metadata=(
                ("standard_in_values", standard_values),
                ("student_in_values", student_values),
                ("distinguishing_values", tuple(
                    sorted(set(standard_values) ^ set(student_values), key=str)
                )),
            ),
        )], 3, 1
    if diff_type in {"cte_changed", "recursive_cte_changed", "recursive_step_expression_changed"}:
        kind = (
            "cte_base_recursive_orphan_paths"
            if diff_type in {"recursive_cte_changed", "recursive_step_expression_changed"}
            else "cte_base_paths"
        )
        return [ConstraintSpec(
            kind,
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_sql",
                    "student_sql",
                    "standard_recursive",
                    "student_recursive",
                )
                if diff.extra.get(key) is not None
            ),
        )], 4, 4
    if diff_type == "star_mismatch":
        return [ConstraintSpec(
            "projection_shape_paths",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in (
                    "standard_has_star",
                    "student_has_star",
                    "standard_sql",
                    "student_sql",
                )
                if diff.extra.get(key) is not None
            ),
        )], 2, 1
    if diff_type == "null_equality_changed":
        return [ConstraintSpec("null_and_non_null_rows", relation, column)], 2, 1
    if diff_type == "null_predicate_negation_changed":
        # ``IS NULL`` versus ``IS NOT NULL`` is distinguishable with either
        # side of the NULL partition present.  Requiring both sides made a
        # valid NOT-NULL schema (or a deliberately all-NULL fixture) fail
        # semantic validation after execution had already shown a difference.
        # Keep the stricter two-path obligation for null equality/coercion, but
        # use a predicate-specific validator for the negation mutation.
        return [ConstraintSpec("null_predicate_paths", relation, column)], 1, 1
    if diff_type == "distinct_on_changed":
        context = _distinct_on_context(diff)
        relation = str(context.get("source_table") or relation)
        column = str(context.get("payload_column") or column)
        return [ConstraintSpec(
            "distinct_on_competing_payload",
            relation,
            column,
            metadata=tuple(
                (key, value)
                for key, value in context.items()
                if value not in (None, "", ())
            ),
        )], 2, 2
    if diff_type == "distinct_changed":
        return [ConstraintSpec(
            "duplicate_projected_tuple",
            relation,
            column,
            metadata=tuple(
                (key, diff.extra.get(key))
                for key in ("query_scope", "standard_projection_columns")
                if diff.extra.get(key) not in (None, "", ())
            ),
        )], 2, 1
    if diff_type == "aggregate_distinct_changed":
        return [ConstraintSpec("duplicate_projected_tuple", relation, column)], 2, 1
    return [ConstraintSpec("observable_projection_discriminator", relation, column)], 2, 1


def _join_metadata(diff: ASTDiffNode) -> tuple[tuple[str, Any], ...]:
    """Extract declared join endpoints; never make the validator guess them."""
    payload: dict[str, Any] = {
        key: value
        for key, value in (diff.extra or {}).items()
        if key in {
            "standard_join_pairs",
            "student_join_pairs",
            "movement",
            "moved_predicate_sql",
            "standard_side",
            "right_table",
            "standard_query_sql",
            "student_query_sql",
            "query_scope",
        }
    }
    for label in ("standard_sql", "student_sql"):
        sql = str(diff.extra.get(label) or "").strip()
        if not sql:
            continue
        try:
            ast = parse_one(sql, read="sqlite")
        except Exception:
            continue
        pairs: list[tuple[str, str, str, str]] = []
        join_nodes = [ast] if isinstance(ast, exp.Join) else []
        join_nodes.extend(ast.find_all(exp.Join))
        for join in join_nodes:
            on = join.args.get("on")
            if on is None:
                continue
            for equality in on.find_all(exp.EQ):
                left, right = equality.left, equality.right
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    continue
                pairs.append((
                    str(left.table or "").lower(), str(left.name or "").lower(),
                    str(right.table or "").lower(), str(right.name or "").lower(),
                ))
        if not pairs and isinstance(ast, exp.EQ):
            left, right = ast.left, ast.right
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                pairs.append((
                    str(left.table or "").lower(), str(left.name or "").lower(),
                    str(right.table or "").lower(), str(right.name or "").lower(),
                ))
        if pairs:
            payload.setdefault(f"{label}_join_pairs", pairs)
    return tuple(sorted(payload.items()))


def _aggregate_metadata(diff: ASTDiffNode) -> dict[str, Any]:
    """Recover grouping context for HAVING diffs that target no single column."""
    metadata: dict[str, Any] = {}
    for key in (
        "standard_aggregate_function", "student_aggregate_function",
        "standard_aggregate_argument", "student_aggregate_argument",
        "standard_group_columns", "student_group_columns",
    ):
        if diff.extra.get(key) is not None:
            metadata[key] = diff.extra[key]
    for key in ("standard_source_table", "student_source_table"):
        if diff.extra.get(key) is not None:
            metadata[key] = diff.extra[key]
    for key in ("standard_aggregate_distinct", "student_aggregate_distinct"):
        if diff.extra.get(key) is not None:
            metadata[key] = diff.extra[key]
    sql = str(diff.extra.get("standard_sql") or "")
    if sql:
        try:
            ast = parse_one(sql, read="sqlite")
            select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
            from_table = select.find(exp.Table) if isinstance(select, exp.Select) else ast.find(exp.Table)
            if from_table is not None:
                metadata.setdefault("source_table", str(from_table.name).lower())
            group = select.args.get("group") if isinstance(select, exp.Select) else None
            if group is not None:
                metadata.setdefault("standard_group_columns", tuple(
                    item.sql(dialect="sqlite") for item in group.expressions or ()
                ))
            aggregate = ast.find(*_AGGREGATE_TYPES)
            if aggregate is not None:
                metadata.setdefault("standard_aggregate_function", type(aggregate).__name__.upper())
                metadata.setdefault("standard_aggregate_argument", aggregate.this.sql(dialect="sqlite") if aggregate.this is not None else "*")
        except Exception:
            pass
    return metadata


def _source_table_from_context(diff: ASTDiffNode, schema: dict[str, list[str]] | None) -> str:
    context = " ".join(str(diff.extra.get(key) or "") for key in ("standard_sql", "student_sql"))
    names = {item.lower() for item in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", context)}
    candidates = [str(table).lower() for table in (schema or {}) if str(table).lower() in names]
    return candidates[0] if len(set(candidates)) == 1 else ""


def _normalize_sql_fragment(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").upper())


def _aggregate_comparison_metadata(diff: ASTDiffNode) -> dict[str, Any]:
    """Read aggregate function/argument/distinct from one comparison diff."""
    if _scalar_aggregate_comparison_metadata(diff):
        return {}
    sql = str(diff.extra.get("standard_sql") or "")
    if not sql:
        return {}
    try:
        node = parse_one(sql, read="sqlite")
    except Exception:
        return {}
    aggregate = node.find(*_AGGREGATE_TYPES)
    if aggregate is None:
        return {}
    return {
        "standard_aggregate_function": type(aggregate).__name__.upper(),
        "standard_aggregate_argument": (
            aggregate.this.sql(dialect="sqlite") if aggregate.this is not None else "*"
        ),
        "standard_aggregate_distinct": bool(
            aggregate.args.get("distinct") or isinstance(aggregate.this, exp.Distinct)
        ),
    }


def _select_column_schema_owner(
    select: exp.Select,
    column: exp.Column,
    schema: dict[str, list[str]] | None,
) -> str:
    """Resolve a SELECT-local column without guessing the first FROM table.

    Qualified references are resolved through aliases.  An unqualified
    reference in a multi-table query is accepted only when the supplied
    physical schema gives it exactly one owner in that query block.
    """
    tables = _direct_tables(select)
    aliases: dict[str, str] = {}
    for table in tables:
        physical = str(table.name or "").strip().lower()
        if not physical:
            continue
        aliases[physical] = physical
        alias = str(table.alias or "").strip().lower()
        if alias:
            aliases[alias] = physical

    qualifier = str(column.table or "").strip().lower()
    if qualifier:
        return aliases.get(qualifier, "")

    physical_tables = list(dict.fromkeys(aliases.values()))
    if len(physical_tables) == 1:
        return physical_tables[0]
    if not schema:
        return ""

    schema_columns = {
        str(table).strip().lower(): {
            str(item).strip().lower() for item in columns
        }
        for table, columns in schema.items()
    }
    column_name = str(column.name or "").strip().lower()
    candidates = [
        table
        for table in physical_tables
        if column_name in schema_columns.get(table, set())
    ]
    return candidates[0] if len(candidates) == 1 else ""


def _scalar_aggregate_comparison_metadata(
    diff: ASTDiffNode,
    schema: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Describe ``outer_column OP (SELECT AGG(...))`` as its own obligation."""
    sql = str(diff.extra.get("standard_sql") or "")
    if not sql:
        return {}
    try:
        node = parse_one(sql, read="sqlite")
    except Exception:
        return {}
    comparison = (
        node
        if isinstance(node, _COMPARISON_TYPES)
        else node.find(*_COMPARISON_TYPES)
    )
    if not isinstance(comparison, _COMPARISON_TYPES):
        return {}
    if isinstance(comparison.left, exp.Column):
        outer_column = comparison.left
        subquery = comparison.right if isinstance(comparison.right, exp.Subquery) else None
    elif isinstance(comparison.right, exp.Column):
        outer_column = comparison.right
        subquery = comparison.left if isinstance(comparison.left, exp.Subquery) else None
    else:
        return {}
    if not isinstance(subquery, exp.Subquery):
        return {}
    inner = subquery.this if isinstance(subquery.this, exp.Select) else None
    if not isinstance(inner, exp.Select):
        return {}
    aggregate = inner.find(*_AGGREGATE_TYPES)
    if aggregate is None:
        return {}
    argument = aggregate.this
    argument_column = aggregate.find(exp.Column)
    source_table = (
        _select_column_schema_owner(inner, argument_column, schema)
        if isinstance(argument_column, exp.Column)
        else ""
    )
    return {
        "scalar_subquery_boundary": True,
        "standard_outer_column": outer_column.sql(dialect="sqlite"),
        "standard_scalar_aggregate_function": type(aggregate).__name__.upper(),
        "standard_scalar_aggregate_argument": (
            argument.sql(dialect="sqlite") if argument is not None else "*"
        ),
        "standard_scalar_source_table": source_table,
        "standard_scalar_source_column": (
            str(argument_column.name).lower()
            if isinstance(argument_column, exp.Column)
            else ""
        ),
        "standard_scalar_comparison_sql": comparison.sql(dialect="sqlite"),
    }


def _nearest_select(node: exp.Expression | None) -> exp.Select | None:
    current = node.parent if isinstance(node, exp.Expression) else None
    while current is not None:
        if isinstance(current, exp.Select):
            return current
        current = current.parent
    return None


def _root_expression(node: exp.Expression | None) -> exp.Expression | None:
    current = node
    while isinstance(current, exp.Expression) and current.parent is not None:
        current = current.parent
    return current


def _direct_tables(select: exp.Select) -> list[exp.Table]:
    return [
        table
        for table in select.find_all(exp.Table)
        if _nearest_select(table) is select
    ]


def _literal_number(node: exp.Expression | None) -> int | float | None:
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    raw = str(node.this or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def _comparison_includes_equal_boundary(
    node: exp.Expression | None,
) -> bool | None:
    if not isinstance(node, _COMPARISON_TYPES):
        return None
    operands = (node.left, node.right)
    if not (
        any(isinstance(item, exp.Column) for item in operands)
        and any(isinstance(item, exp.Literal) for item in operands)
    ):
        return None
    if isinstance(node, (exp.EQ, exp.GTE, exp.LTE)):
        return True
    if isinstance(node, (exp.NEQ, exp.GT, exp.LT)):
        return False
    return None


def _normalized_count_comparison(
    node: exp.Expression | None,
) -> tuple[str, int | float, exp.Count] | None:
    if not isinstance(node, _COMPARISON_TYPES):
        return None
    if isinstance(node.left, exp.Count) and isinstance(node.right, exp.Literal):
        aggregate = node.left
        boundary = _literal_number(node.right)
        operator = type(node).__name__.upper()
    elif isinstance(node.right, exp.Count) and isinstance(node.left, exp.Literal):
        aggregate = node.right
        boundary = _literal_number(node.left)
        operator = {
            "GT": "LT",
            "GTE": "LTE",
            "LT": "GT",
            "LTE": "GTE",
        }.get(type(node).__name__.upper(), type(node).__name__.upper())
    else:
        return None
    if boundary is None:
        return None
    return operator, boundary, aggregate


def _count_comparison_truth(
    operator: str,
    count: int,
    boundary: int | float,
) -> bool:
    return {
        "EQ": count == boundary,
        "NEQ": count != boundary,
        "GT": count > boundary,
        "GTE": count >= boundary,
        "LT": count < boundary,
        "LTE": count <= boundary,
    }.get(operator, False)


def _filtered_aggregate_boundary_metadata(
    diff: ASTDiffNode,
) -> dict[str, Any]:
    """Describe a WHERE boundary that changes a later HAVING COUNT result.

    This first bounded form accepts one literal comparison in ``WHERE`` and
    one non-DISTINCT ``COUNT`` comparison in the same grouped query block.
    It is deliberately rejected unless adding the single distinguishing row
    moves exactly one side across the HAVING gate.
    """

    standard_comparison = diff.standard_node
    student_comparison = diff.student_node
    if not isinstance(standard_comparison, _COMPARISON_TYPES) or not isinstance(
        student_comparison, _COMPARISON_TYPES
    ):
        return {}
    select = _nearest_select(standard_comparison)
    student_select = _nearest_select(student_comparison)
    if not isinstance(select, exp.Select) or not isinstance(student_select, exp.Select):
        return {}
    where = standard_comparison.find_ancestor(exp.Where)
    if not isinstance(where, exp.Where) or _nearest_select(where) is not select:
        return {}
    group = select.args.get("group")
    having = select.args.get("having")
    if not isinstance(group, exp.Group) or not isinstance(having, exp.Having):
        return {}
    if having.find(exp.And) is not None or having.find(exp.Or) is not None:
        return {}
    having_spec = _normalized_count_comparison(having.this)
    if having_spec is None:
        return {}
    having_operator, having_boundary, count = having_spec
    if bool(count.args.get("distinct") or isinstance(count.this, exp.Distinct)):
        return {}

    standard_included = _comparison_includes_equal_boundary(standard_comparison)
    student_included = _comparison_includes_equal_boundary(student_comparison)
    if standard_included is None or student_included is None:
        return {}
    if standard_included == student_included:
        return {}
    if diff.extra.get("value") != diff.extra.get("student_value"):
        return {}

    common_rows = next(
        (
            count_value
            for count_value in range(32)
            if _count_comparison_truth(
                having_operator,
                count_value + int(standard_included),
                having_boundary,
            )
            != _count_comparison_truth(
                having_operator,
                count_value + int(student_included),
                having_boundary,
            )
        ),
        None,
    )
    if common_rows is None:
        return {}

    predicate_column = next(
        (
            item
            for item in (standard_comparison.left, standard_comparison.right)
            if isinstance(item, exp.Column)
        ),
        None,
    )
    if not isinstance(predicate_column, exp.Column):
        return {}
    direct_tables = _direct_tables(select)
    qualifier = str(predicate_column.table or "").lower()
    source_candidates = [
        table
        for table in direct_tables
        if not qualifier
        or qualifier
        in {
            str(table.name or "").lower(),
            str(table.alias_or_name or table.name or "").lower(),
        }
    ]
    if len(source_candidates) != 1:
        return {}
    source_table = str(source_candidates[0].name or "").lower()
    count_argument = count.this.sql(dialect="sqlite") if count.this is not None else "*"
    standard_root = _root_expression(standard_comparison)
    student_root = _root_expression(student_comparison)
    return {
        "filtered_aggregate_boundary": True,
        "standard_source_table": source_table,
        "standard_predicate_column": str(predicate_column.name or "").lower(),
        "standard_boundary_included": standard_included,
        "student_boundary_included": student_included,
        "common_qualifying_rows": common_rows,
        "required_path_rows": common_rows + 1,
        "standard_group_columns": tuple(
            item.sql(dialect="sqlite") for item in group.expressions or ()
        ),
        "having_aggregate_function": "COUNT",
        "having_aggregate_argument": count_argument,
        "having_operator": having_operator,
        "having_boundary": having_boundary,
        "standard_query_sql": (
            standard_root.sql(dialect="sqlite")
            if isinstance(standard_root, exp.Expression)
            else ""
        ),
        "student_query_sql": (
            student_root.sql(dialect="sqlite")
            if isinstance(student_root, exp.Expression)
            else ""
        ),
    }


def _projection_expression(
    select: exp.Select,
    output_column: str,
    explicit_columns: tuple[str, ...] = (),
) -> exp.Expression | None:
    target = output_column.lower()
    for index, projection in enumerate(select.expressions or ()):
        explicit = explicit_columns[index].lower() if index < len(explicit_columns) else ""
        output_name = str(projection.alias_or_name or "").lower()
        if target not in {explicit, output_name}:
            continue
        return projection.this if isinstance(projection, exp.Alias) else projection
    return None


def _physical_column_source(
    select: exp.Select,
    column: exp.Column | None,
    ctes: dict[str, tuple[exp.Select, tuple[str, ...]]],
    *,
    depth: int = 0,
) -> tuple[str, str] | None:
    """Resolve one unambiguous CTE column chain to a physical table."""

    if depth >= 8:
        return None
    sources = _direct_tables(select)
    if column is not None and column.table:
        qualifier = str(column.table).lower()
        sources = [
            table for table in sources
            if qualifier in {
                str(table.name or "").lower(),
                str(table.alias_or_name or table.name or "").lower(),
            }
        ]
    if len(sources) != 1:
        return None
    source = sources[0]
    source_name = str(source.name or "").lower()
    if source_name not in ctes:
        return source_name, str(column.name or "").lower() if column is not None else ""

    nested, explicit = ctes[source_name]
    if column is None:
        return _physical_column_source(nested, None, ctes, depth=depth + 1)
    expression = _projection_expression(nested, str(column.name or ""), explicit)
    if expression is None:
        return None
    nested_columns = list(expression.find_all(exp.Column))
    if isinstance(expression, exp.Column):
        nested_columns.insert(0, expression)
    nested_columns = list({id(item): item for item in nested_columns}.values())
    if len(nested_columns) != 1:
        return None
    return _physical_column_source(
        nested,
        nested_columns[0],
        ctes,
        depth=depth + 1,
    )


def _derived_projection_provenance(diff: ASTDiffNode) -> dict[str, Any]:
    """Trace a compared CTE/derived output back to its physical expression.

    Example: ``totals.total`` in an outer WHERE resolves to
    ``SUM(sales.amount)`` and the CTE's ``GROUP BY dept``.  The first bounded
    implementation accepts at most eight unambiguous CTE hops; ambiguous
    joins, recursive cycles or complex multi-column producers remain
    uncovered instead of being guessed.
    """

    comparison = diff.standard_node
    if not isinstance(comparison, exp.Expression) or not diff.target_column:
        return {}
    outer_select = _nearest_select(comparison)
    root = _root_expression(comparison)
    if not isinstance(outer_select, exp.Select) or not isinstance(root, exp.Expression):
        return {}

    target = str(diff.target_column).lower()
    compared_column = next(
        (
            column
            for column in comparison.find_all(exp.Column)
            if str(column.name or "").lower() == target
        ),
        None,
    )
    qualifier = str(compared_column.table or "").lower() if compared_column is not None else ""

    ctes: dict[str, tuple[exp.Select, tuple[str, ...]]] = {}
    for cte in root.find_all(exp.CTE):
        name = str(cte.alias_or_name or "").lower()
        query = cte.this if isinstance(cte.this, exp.Select) else cte.this.find(exp.Select)
        alias = cte.args.get("alias")
        explicit = tuple(
            str(column.name or column).lower()
            for column in (alias.args.get("columns") or ())
        ) if isinstance(alias, exp.TableAlias) else ()
        if name and isinstance(query, exp.Select):
            ctes[name] = (query, explicit)

    candidates: list[tuple[str, exp.Select, tuple[str, ...]]] = []
    for table in _direct_tables(outer_select):
        name = str(table.name or "").lower()
        alias = str(table.alias_or_name or table.name or "").lower()
        if qualifier and qualifier not in {name, alias}:
            continue
        if name in ctes:
            query, explicit = ctes[name]
            if _projection_expression(query, target, explicit) is not None:
                candidates.append((alias or name, query, explicit))
    for subquery in outer_select.find_all(exp.Subquery):
        if _nearest_select(subquery) is not outer_select:
            continue
        alias = str(subquery.alias_or_name or "").lower()
        if qualifier and qualifier != alias:
            continue
        query = subquery.this if isinstance(subquery.this, exp.Select) else None
        if isinstance(query, exp.Select) and _projection_expression(query, target) is not None:
            candidates.append((alias, query, ()))
    if len(candidates) != 1:
        return {}

    derived_relation, producer, explicit = candidates[0]
    expression = _projection_expression(producer, target, explicit)
    if expression is None:
        return {}
    aggregate = expression if isinstance(expression, _AGGREGATE_TYPES) else expression.find(*_AGGREGATE_TYPES)
    group = producer.args.get("group")
    group_expressions = tuple(group.expressions or ()) if isinstance(group, exp.Group) else ()
    columns = list(expression.find_all(exp.Column))
    if isinstance(expression, exp.Column):
        columns.insert(0, expression)
    columns = list({id(column): column for column in columns}.values())
    columnless_count = isinstance(aggregate, exp.Count) and not columns
    if len(columns) == 1:
        source = _physical_column_source(producer, columns[0], ctes)
    elif isinstance(aggregate, exp.Count) and not columns and group_expressions:
        group_column = next(
            (
                item if isinstance(item, exp.Column) else item.find(exp.Column)
                for item in group_expressions
                if isinstance(item, exp.Column) or item.find(exp.Column) is not None
            ),
            None,
        )
        source = _physical_column_source(producer, group_column, ctes)
    elif columnless_count:
        source = _physical_column_source(producer, None, ctes)
    else:
        return {}
    if source is None:
        return {}
    source_table, source_column = source
    if not source_column and not columnless_count:
        return {}

    physical_group_columns: list[str] = []
    for item in group_expressions:
        if not isinstance(item, exp.Column):
            return {}
        resolved_group = _physical_column_source(producer, item, ctes)
        if resolved_group is None or resolved_group[0] != source_table:
            return {}
        physical_group_columns.append(resolved_group[1])

    metadata: dict[str, Any] = {
        "derived_relation": derived_relation,
        "derived_column": target,
        "source_table": source_table,
        "standard_source_table": source_table,
        "source_column": source_column,
        "projection_expression": expression.sql(dialect="sqlite"),
    }
    if aggregate is not None:
        argument = aggregate.this
        distinct = bool(
            aggregate.args.get("distinct") or isinstance(argument, exp.Distinct)
        )
        physical_argument = "*" if columnless_count else source_column
        if distinct and physical_argument != "*":
            physical_argument = f"DISTINCT {physical_argument}"
        metadata.update({
            "standard_aggregate_function": type(aggregate).__name__.upper(),
            "standard_aggregate_argument": physical_argument,
            "standard_aggregate_distinct": distinct,
            "standard_group_columns": tuple(physical_group_columns),
        })
    return metadata


def _having_group_metadata(
    diff: ASTDiffNode,
    all_diffs: list[ASTDiffNode],
    *,
    include_aggregate: bool = False,
) -> dict[str, Any]:
    candidate_sql = _normalize_sql_fragment(diff.extra.get("standard_sql"))
    for item in all_diffs:
        if item.diff_type not in {"having_changed", "aggregate_condition_in_where"}:
            continue
        having_sql = _normalize_sql_fragment(item.extra.get("standard_sql"))
        if candidate_sql and candidate_sql in having_sql:
            keys = ["standard_group_columns", "standard_source_table"]
            if include_aggregate:
                keys.extend(
                    (
                        "standard_aggregate_function",
                        "standard_aggregate_argument",
                        "standard_aggregate_distinct",
                    )
                )
            return {
                key: item.extra[key]
                for key in keys
                if item.extra.get(key) is not None
            }
    return {}


def _having_boundary_context(
    diff: ASTDiffNode,
    all_diffs: list[ASTDiffNode],
) -> dict[str, Any]:
    """Associate a HAVING clause diff with its atomic comparison diff."""
    if diff.diff_type not in {"having_changed", "aggregate_condition_in_where"}:
        return {}
    standard_sql = _normalize_sql_fragment(diff.extra.get("standard_sql"))
    student_sql = _normalize_sql_fragment(diff.extra.get("student_sql"))
    candidates: list[ASTDiffNode] = []
    for candidate in all_diffs:
        if candidate.diff_type != "comparison_operator_changed":
            continue
        candidate_sql = _normalize_sql_fragment(candidate.extra.get("standard_sql"))
        if candidate_sql and (candidate_sql in standard_sql or standard_sql.endswith(candidate_sql)):
            candidates.append(candidate)
    if len(candidates) != 1:
        return {}
    candidate = candidates[0]
    metadata = {
        "boundary_value": candidate.extra.get("value", candidate.get("standard_value")),
        "standard_operator": candidate.extra.get("standard_op"),
        "student_operator": candidate.extra.get("student_op"),
        "boundary_diff_id": stable_diff_id(candidate, all_diffs.index(candidate)),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _having_summary_is_covered(diff: ASTDiffNode, all_diffs: list[ASTDiffNode]) -> bool:
    if diff.diff_type != "having_changed":
        return False
    having_sql = _normalize_sql_fragment(diff.extra.get("standard_sql"))
    if not having_sql:
        return False
    aggregate_comparisons = [
        item for item in all_diffs
        # The nested predicate extractor represents a HAVING threshold edit
        # either as an operator change (``>`` -> ``>=``) or as a literal
        # change (``100`` -> ``101``).  Both are the atomic obligation; the
        # clause-level ``having_changed`` node is only a summary and must not
        # force a second witness/validator obligation.
        if item.diff_type in {"comparison_operator_changed", "literal_changed"}
        and _normalize_sql_fragment(item.extra.get("standard_sql")) in having_sql
        and _aggregate_comparison_metadata(item)
    ]
    # A COUNT(*) -> COUNT(column) rewrite inside HAVING is represented by an
    # aggregate diff rather than a comparison diff. Treat the clause node as
    # the summary when that atomic aggregate is present.
    aggregate_argument_changes = [
        item
        for item in all_diffs
        if item.diff_type in {
            "aggregate_argument_changed",
            "aggregate_function_changed",
            "aggregate_distinct_changed",
        }
        and any(
            _normalize_sql_fragment(item.extra.get(key)) in having_sql
            for key in ("standard_sql", "student_sql")
            if item.extra.get(key)
        )
    ]
    # Some dialects render a grouped alias in HAVING (``high_score >= 90``)
    # instead of repeating the aggregate expression.  In that form the
    # concrete comparison is still the owner even though the aggregate-aware
    # metadata parser cannot recover ``MAX(score)`` from the fragment.
    alias_comparisons = [
        item
        for item in all_diffs
        if item.diff_type in {"comparison_operator_changed", "literal_changed"}
        and str(item.extra.get("predicate_clause") or "").upper() == "HAVING"
        and _normalize_sql_fragment(item.extra.get("standard_sql")) in having_sql
    ]
    expected = re.findall(
        r"(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\)\s*(?:>=|<=|<>|!=|=|>|<)",
        str(diff.extra.get("standard_sql") or ""),
        flags=re.IGNORECASE,
    )
    return bool(alias_comparisons) or (
        bool(expected)
        and (
            len(aggregate_comparisons) >= len(expected)
            or bool(aggregate_argument_changes)
        )
    )


_COMPARISON_TYPES = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _infer_aggregate_relation(diff: ASTDiffNode, schema: dict[str, list[str]] | None) -> str:
    if not schema:
        return ""
    fragments = " ".join(
        str(diff.extra.get(key) or "")
        for key in ("standard_sql", "student_sql", "standard_group_columns")
    )
    try:
        ast = parse_one(f"SELECT * FROM ({fragments}) AS _aggregate_fragment", read="sqlite")
        names = {str(item.name).lower() for item in ast.find_all(exp.Column) if item.name}
    except Exception:
        names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fragments.lower()))
    candidates = [
        str(table).lower()
        for table, columns in schema.items()
        if names & {str(column).lower() for column in columns}
    ]
    return candidates[0] if len(set(candidates)) == 1 else ""


_AGGREGATE_TYPES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)


def is_redundant_summary_diff(
    diff: ASTDiffNode,
    all_diffs: list[ASTDiffNode],
) -> bool:
    specific_types = {item.diff_type for item in all_diffs if item is not diff}

    def fragment_is_contained(summary: ASTDiffNode, candidate: ASTDiffNode) -> bool:
        summary_fragments = {
            side: _normalize_sql_fragment(summary.extra.get(f"{side}_sql"))
            for side in ("standard", "student")
        }
        for side in ("standard", "student"):
            candidate_fragment = _normalize_sql_fragment(
                candidate.extra.get(f"{side}_sql")
            )
            if (
                candidate_fragment
                and len(candidate_fragment) >= 8
                and candidate_fragment in summary_fragments[side]
            ):
                return True
        return False

    # ``IS NULL``/``= NULL`` is emitted once as a generic comparison change and
    # once as the NULL-specific atomic change.  The latter owns the semantic
    # obligation; retaining both creates duplicate witness/validator work.
    if diff.diff_type == "comparison_operator_changed" and any(
        item.diff_type == "null_equality_changed"
        and fragment_is_contained(diff, item)
        for item in all_diffs
        if item is not diff
    ):
        return True

    # The nested-query AST walker can report the same physical window
    # expression once for every enclosing derived SELECT.  Keep the deepest
    # occurrence as the owner of the witness; shallower copies are summaries
    # of the same replacement and would make one mutation bind ambiguously to
    # several otherwise identical window obligations.
    if diff.diff_type in {"window_over_changed", "window_function_changed"}:
        current_depth = diff.extra.get("subquery_depth")
        try:
            current_depth = int(current_depth)
        except (TypeError, ValueError):
            current_depth = -1
        current_standard = _normalize_sql_fragment(diff.extra.get("standard_sql"))
        current_student = _normalize_sql_fragment(diff.extra.get("student_sql"))
        def _depth(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return -1

        if current_standard and current_student and any(
                item is not diff
                and item.diff_type == diff.diff_type
                and _normalize_sql_fragment(item.extra.get("standard_sql"))
                == current_standard
                and _normalize_sql_fragment(item.extra.get("student_sql"))
                == current_student
                and _depth(item.extra.get("subquery_depth")) > current_depth
                for item in all_diffs
            ):
                return True

    # Nested aggregate extraction can report the same function replacement at
    # each enclosing SELECT depth.  Keep the deepest occurrence as the owner
    # so one aggregate mutation does not bind ambiguously to multiple worlds.
    if diff.diff_type in {
        "aggregate_function_changed",
        "aggregate_argument_changed",
        "aggregate_distinct_changed",
    }:
        current_depth = diff.extra.get("subquery_depth")
        try:
            current_depth = int(current_depth)
        except (TypeError, ValueError):
            current_depth = -1
        current_standard = _normalize_sql_fragment(diff.extra.get("standard_sql"))
        current_student = _normalize_sql_fragment(diff.extra.get("student_sql"))

        def _depth(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return -1

        if current_standard and current_student and any(
            item is not diff
            and item.diff_type == diff.diff_type
            and _normalize_sql_fragment(item.extra.get("standard_sql")) == current_standard
            and _normalize_sql_fragment(item.extra.get("student_sql")) == current_student
            and _depth(item.extra.get("subquery_depth")) > current_depth
            for item in all_diffs
        ):
            return True

    # Recursive CTE extraction intentionally emits a whole-CTE summary.  When
    # a concrete predicate, literal, or recursive-step diff is also present,
    # that atomic diff owns attribution.  Keep the summary for genuinely
    # opaque recursive changes, and only suppress it when the concrete SQL
    # fragment is actually present on the corresponding side.
    if diff.diff_type == "recursive_cte_changed":
        recursive_atomic_types = {
            "comparison_operator_changed",
            "literal_changed",
            "logical_operator_changed",
            "logical_precedence_tree_changed",
            "predicate_added",
            "predicate_missing",
            "null_equality_changed",
            "null_predicate_negation_changed",
            "recursive_step_expression_changed",
            "set_operator_changed",
            "set_modifier_changed",
            "set_all_modifier_changed",
            "projection_changed",
            "aggregate_function_changed",
            "aggregate_argument_changed",
            "aggregate_distinct_changed",
        }
        if any(
            item.diff_type in recursive_atomic_types
            and fragment_is_contained(diff, item)
            for item in all_diffs
            if item is not diff
        ):
            return True

    if diff.diff_type in {"predicate_missing", "predicate_added"}:
        # AST extraction also emits predicate add/remove summaries for a
        # changed aggregate expression in HAVING. The HAVING/aggregate diff
        # owns that semantic obligation; retaining these summaries creates
        # unsupported duplicate predicate obligations and breaks attribution.
        predicate_sql = _normalize_sql_fragment(
            diff.extra.get("standard_sql") or diff.extra.get("student_sql")
        )
        if predicate_sql:
            for item in all_diffs:
                if item.diff_type != "having_changed":
                    continue
                having_sql = " ".join(
                    _normalize_sql_fragment(item.extra.get(key))
                    for key in ("standard_sql", "student_sql")
                    if item.extra.get(key)
                )
                if predicate_sql in having_sql and _having_summary_is_covered(item, all_diffs):
                    return True
        node = diff.standard_node or diff.student_node
        inside_case = False
        while isinstance(node, exp.Expression):
            if isinstance(node, (exp.If, exp.Case)):
                inside_case = True
                break
            node = node.parent
        if inside_case and specific_types & {"case_when_missing", "case_when_added"}:
            return True
    if diff.diff_type == "where_changed":
        return bool(
            specific_types
            & {
                "comparison_operator_changed",
                "literal_changed",
                "logical_operator_changed",
                "logical_precedence_tree_changed",
                "predicate_missing",
                "predicate_added",
                "predicate_expression_operator_changed",
                "null_equality_changed",
                "null_predicate_negation_changed",
                "regex_pattern_changed",
                "like_pattern_changed",
                "glob_pattern_changed",
                "similar_pattern_changed",
                "in_predicate_negation_changed",
                "in_list_member_removed",
                "in_list_member_added",
                "correlated_predicate_changed",
                "aggregate_function_changed",
                "aggregate_argument_changed",
                "order_by_changed",
                "order_direction_changed",
                "order_by_tiebreaker_missing",
                "order_by_key_added",
                "order_nulls_changed",
            }
        )
    if diff.diff_type in {"logical_operator_changed", "logical_precedence_tree_changed"}:
        depth = diff.extra.get("subquery_depth", 0)
        return any(
            item.diff_type in {"predicate_missing", "predicate_added"}
            and item.extra.get("subquery_depth", 0) == depth
            for item in all_diffs
        )
    if diff.diff_type == "having_changed":
        return _having_summary_is_covered(diff, all_diffs)
    if diff.diff_type == "group_by_changed":
        return bool(
            specific_types
            & {
                "group_by_expression_changed",
                "grouping_grain_too_fine",
                "grouping_grain_too_coarse",
            }
        )
    if diff.diff_type == "projection_changed":
        return bool(specific_types & {
            "function_argument_changed",
            "aggregate_function_changed",
            "aggregate_argument_changed",
            "aggregate_distinct_changed",
            "window_function_changed",
            "window_over_changed",
            "case_changed",
            "star_mismatch",
        })
    if diff.diff_type in {"column_added", "column_dropped", "function_argument_changed"}:
        if diff.diff_type in {"column_added", "column_dropped"}:
            if any(
                item.diff_type in {"projection_changed", "star_mismatch"}
                for item in all_diffs
            ):
                return True
            sql = " ".join(
                str(diff.extra.get(key) or "")
                for key in ("standard_sql", "student_sql")
            ).upper()
            return bool(re.search(r"OVER\s*\(", sql)) and any(
                item.diff_type in {"window_function_changed", "window_over_changed"}
                for item in all_diffs
            )
        if diff.diff_type == "function_argument_changed" and str(
            diff.extra.get("function") or ""
        ).upper() in {"EXISTS", "IN"} and any(
            item.diff_type == "correlated_predicate_changed" for item in all_diffs
        ):
            return True
        if any(
            item.diff_type in {
                "case_changed",
                "case_else_missing",
                "case_else_added",
                "case_when_missing",
                "case_when_added",
            }
            for item in all_diffs
        ):
            return True
    if diff.diff_type in {"subquery_added", "subquery_removed"}:
        if any(
            item.diff_type in {
                "case_changed",
                "case_when_missing",
                "case_when_added",
            }
            and fragment_is_contained(item, diff)
            for item in all_diffs
            if item is not diff
        ):
            return True
        if any(
            item.diff_type in {"predicate_missing", "predicate_added"}
            and fragment_is_contained(item, diff)
            for item in all_diffs
            if item is not diff
        ):
            return True
        return any(
            item.diff_type == "correlated_predicate_changed"
            and not item.extra.get("subquery_depth")
            for item in all_diffs
        )
    if diff.diff_type == "predicate_added" and any(
        item.diff_type == "correlated_predicate_changed" for item in all_diffs
    ):
        return True
    if diff.diff_type == "comparison_left_column_changed" and any(
        item.diff_type == "correlated_predicate_changed" for item in all_diffs
    ):
        node = diff.standard_node or diff.student_node
        while isinstance(node, exp.Expression):
            if isinstance(node, (exp.Subquery, exp.Exists)):
                return True
            node = node.parent
    if diff.diff_type == "join_on_changed":
        # A CROSS/INNER rewrite creates an apparent ON diff as a dependent
        # consequence. The join-type obligation owns that topology change.
        return any(item.diff_type == "join_type_changed" for item in all_diffs)
    if diff.diff_type == "correlated_predicate_changed" and diff.extra.get("subquery_depth"):
        return any(
            item is not diff
            and item.diff_type == "correlated_predicate_changed"
            and not item.extra.get("subquery_depth")
            for item in all_diffs
        )
    if diff.diff_type == "correlated_predicate_changed":
        # Nested SELECT extraction can emit a whole derived-query summary for
        # a window partition change.  If the concrete window fragment is
        # present in that summary, the window obligation owns the evidence;
        # compiling the summary as a membership obligation has no physical
        # relation/key metadata and can never validate it.
        if any(
            item.diff_type in {"window_over_changed", "window_function_changed"}
            and fragment_is_contained(diff, item)
            for item in all_diffs
            if item is not diff
        ):
            return True
        # IN/EXISTS mutations can also emit a correlated whole-predicate
        # summary alongside a focused predicate_missing/added node.  A
        # scalar/derived predicate has no independent physical membership
        # key, so the focused bounded query-pair obligation owns it.  A
        # cross-scope column comparison is different: it is the actual
        # correlated membership path (for example ``b.employee_id <>
        # employee.id``), and the correlated summary is its only
        # relation-aware obligation.  Keep that summary so the witness
        # planner can materialize both outer-membership paths.
        focused_predicates = [
            item
            for item in all_diffs
            if item is not diff
            and item.diff_type in {"predicate_missing", "predicate_added"}
            and fragment_is_contained(diff, item)
        ]
        direct_correlated_predicate = any(
            str(item.extra.get("value_kind") or "").lower() == "column"
            and item.extra.get("left_table")
            and item.extra.get("right_table")
            for item in focused_predicates
        )
        if focused_predicates and not direct_correlated_predicate:
            return True
    if diff.diff_type == "in_predicate_negation_changed" and any(
        item.diff_type == "correlated_predicate_changed"
        and fragment_is_contained(item, diff)
        for item in all_diffs
        if item is not diff
    ):
        # The standalone IN-list detector cannot express the enclosing
        # correlated EXISTS path.  Let the correlated membership obligation
        # own this nested predicate and its exact execution replacement.
        return True
    if diff.diff_type == "case_changed":
        if any(
            item.diff_type in {
                "case_else_missing",
                "case_else_added",
                "case_when_missing",
                "case_when_added",
            }
            for item in all_diffs
        ):
            return True
        return any(
            item.diff_type in {
                "null_equality_changed",
                "null_predicate_negation_changed",
                "comparison_operator_changed",
                "literal_changed",
            }
            and fragment_is_contained(diff, item)
            for item in all_diffs
            if item is not diff
        )
    if diff.diff_type == "from_source_changed":
        return bool(
            specific_types
            & {
                "join_missing",
                "join_type_changed",
                "join_on_changed",
                "correlated_predicate_changed",
                "subquery_added",
                "subquery_removed",
                "window_over_changed",
                "window_function_changed",
            }
        )
    if diff.diff_type == "join_key_column_changed":
        return "join_on_changed" in specific_types
    if diff.diff_type in {"set_modifier_changed", "set_all_modifier_changed"}:
        return any(
            item.diff_type == "set_operator_changed"
            and item.extra.get("standard_modifier") is not None
            and item.extra.get("student_modifier") is not None
            for item in all_diffs
        )
    if diff.diff_type == "set_operator_changed" and not diff.extra.get("standard_op"):
        return any(
            item is not diff
            and item.diff_type == "set_operator_changed"
            and item.extra.get("standard_op") is not None
            for item in all_diffs
        )
    if diff.diff_type == "order_by_changed":
        return bool(
            specific_types
            & {
                "order_direction_changed",
                "order_by_tiebreaker_missing",
                "order_by_key_added",
                "order_nulls_changed",
            }
        )
    if diff.diff_type == "top_n_ordering_missing":
        return bool(
            specific_types
            & {
                "order_direction_changed",
                "order_by_tiebreaker_missing",
                "order_by_key_added",
                "order_nulls_changed",
            }
        )
    if diff.diff_type == "cte_changed":
        return any(
            str(item.extra.get("query_scope") or "").startswith("cte:")
            and item.diff_type in {
                "projection_changed",
                "distinct_changed",
                "comparison_operator_changed",
                "literal_changed",
                "logical_operator_changed",
                "logical_precedence_tree_changed",
                "where_changed",
                "group_by_changed",
                "having_changed",
                "order_by_changed",
                "limit_changed",
                "aggregate_function_changed",
                "aggregate_argument_changed",
            }
            for item in all_diffs
        )
    return False


def _resolved_relation(
    diff: ASTDiffNode,
    target_column: str,
    schema: dict[str, list[str]] | None,
    qualifications: Iterable[SchemaQualification],
) -> str:
    raw_table = str(diff.target_table or "").strip().lower()
    aliases: dict[str, str] = {}
    for qualification in qualifications:
        for scope in qualification.scopes:
            aliases.update(
                {
                    alias.lower(): canonical.lower()
                    for alias, canonical in scope.physical_tables.items()
                }
            )
    if raw_table:
        return aliases.get(raw_table, raw_table)
    column = target_column
    if not column or not schema:
        return ""
    fragments = " ".join(
        str(diff.extra.get(key) or "")
        for key in ("standard_sql", "student_sql")
    )
    for alias, canonical in aliases.items():
        if not re.search(
            rf"(?<![A-Za-z0-9_$]){re.escape(alias)}\s*\.\s*",
            fragments,
            re.IGNORECASE,
        ):
            continue
        if any(
            str(item).strip().lower() == column
            for item in schema.get(canonical, ())
        ):
            return canonical
    candidates = [
        table.lower()
        for table, columns in schema.items()
        if any(str(item).strip().lower() == column for item in columns)
    ]
    return candidates[0] if len(candidates) == 1 else ""


def compile_obligations(
    ast_diffs: Iterable[ASTDiffNode],
    *,
    schema: dict[str, list[str]] | None = None,
    qualifications: Iterable[SchemaQualification] = (),
) -> list[DistinguishingObligation]:
    resolved_diffs = list(ast_diffs)
    qualification_list = list(qualifications)
    obligations: list[DistinguishingObligation] = []
    for index, diff in enumerate(resolved_diffs):
        if is_redundant_summary_diff(diff, resolved_diffs):
            continue
        diff_id = stable_diff_id(diff, index)
        target_column = _inferred_target_column(diff)
        table = _resolved_relation(diff, target_column, schema, qualification_list)
        if diff.diff_type == "distinct_on_changed":
            distinct_on = _distinct_on_context(diff)
            target_column = target_column or str(distinct_on.get("payload_column") or "")
            table = table or str(distinct_on.get("source_table") or "")
        if not table and diff.diff_type in {"window_over_changed", "window_function_changed"}:
            table = str(
                diff.extra.get("standard_window_source_table")
                or diff.extra.get("student_window_source_table")
                or ""
            ).lower()
        if not table and diff.diff_type in {
            "order_direction_changed",
            "order_by_tiebreaker_missing",
            "order_by_key_added",
            "order_nulls_changed",
            "order_by_changed",
        }:
            table = str(diff.extra.get("standard_source_table") or "").lower()
        if not table and schema and diff.diff_type in {"where_changed", "order_by_changed"}:
            schema_tables = [str(name).lower() for name in schema]
            if len(schema_tables) == 1:
                table = schema_tables[0]
        if not table and diff.diff_type in {
            "logical_operator_changed",
            "logical_precedence_tree_changed",
        }:
            table = str(diff.extra.get("standard_source_table") or "").lower()
        if not table and diff.diff_type in {
            "case_changed",
            "case_else_missing",
            "case_else_added",
            "case_when_missing",
            "case_when_added",
        }:
            table = str(diff.extra.get("standard_source_table") or "").lower()
        if not table and diff.diff_type in {
            "in_predicate_negation_changed",
            "null_sensitive_antijoin_equivalence",
            "in_exists_equivalence",
            "correlated_predicate_changed",
        }:
            table = str(diff.extra.get("standard_source_table") or "").lower()
        if not table and diff.diff_type in {"having_changed", "aggregate_function_changed", "aggregate_argument_changed"}:
            table = str(
                _aggregate_metadata(diff).get("source_table")
                or _infer_aggregate_relation(diff, schema)
                or _aggregate_metadata(diff).get("standard_source_table")
                or _source_table_from_context(diff, schema)
                or ""
            )
        correlation = _having_boundary_context(diff, resolved_diffs)
        if diff.diff_type == "literal_changed":
            # Literal edits inside HAVING carry only the local comparison
            # fragment. Recover the grouping/source/aggregate context from the
            # clause-level summary before selecting the physical obligation
            # relation; otherwise a valid threshold witness is reported as a
            # missing table.
            correlation.update(
                _having_group_metadata(
                    diff,
                    resolved_diffs,
                    include_aggregate=True,
                )
            )
        if diff.diff_type == "null_sensitive_antijoin_equivalence":
            related = next(
                (
                    item for item in resolved_diffs
                    if item.diff_type == "correlated_predicate_changed"
                    and not item.extra.get("subquery_depth")
                ),
                None,
            )
            if related is not None:
                for key in (
                    "standard_source_table",
                    "standard_membership_table",
                    "standard_outer_column",
                    "standard_membership_column",
                ):
                    if related.extra.get(key) is not None:
                        correlation[key] = related.extra[key]
            correlation["require_inner_null"] = bool(
                diff.extra.get("require_inner_null", True)
            )
            correlation["require_outer_null"] = bool(
                diff.extra.get("require_outer_null", False)
            )
        if diff.diff_type == "comparison_operator_changed":
            filtered_aggregate_context = _filtered_aggregate_boundary_metadata(
                diff
            )
            scalar_context = _scalar_aggregate_comparison_metadata(diff, schema)
            if filtered_aggregate_context:
                correlation.update(filtered_aggregate_context)
            elif scalar_context:
                correlation.update(scalar_context)
            else:
                correlation.update(_aggregate_comparison_metadata(diff))
            correlation.update(_having_group_metadata(diff, resolved_diffs))
            provenance = _derived_projection_provenance(diff)
            if provenance:
                correlation.update(provenance)
                table = str(provenance.get("source_table") or table).lower()
                if "source_column" in provenance:
                    target_column = str(provenance.get("source_column") or "").lower()
        if not table:
            table = str(
                correlation.get("standard_source_table")
                or correlation.get("source_table")
                or ""
            ).lower()
        constraints, minimum_rows, cost = _constraint_templates(
            diff,
            table,
            target_column,
            correlation,
        )
        required_tables = {table} if table else set()
        for constraint in constraints:
            metadata = dict(constraint.metadata)
            if constraint.kind == "subquery_membership_paths":
                required_tables.update(
                    str(metadata.get(key) or "").lower()
                    for key in (
                        "standard_source_table",
                        "standard_membership_table",
                        "student_source_table",
                        "student_membership_table",
                    )
                    if metadata.get(key)
                )
            for key in ("standard_join_pairs", "student_join_pairs"):
                for pair in metadata.get(key, ()):
                    if len(pair) == 2 and all(isinstance(item, (tuple, list)) for item in pair):
                        left_table, _ = pair[0]
                        right_table, _ = pair[1]
                    elif len(pair) == 4:
                        left_table, _, right_table, _ = pair
                    else:
                        continue
                    required_tables.update(item for item in (left_table, right_table) if item)
        obligations.append(
            DistinguishingObligation(
                id=f"obligation_{diff_id.removeprefix('diff_')}",
                diff_id=diff_id,
                diff_type=diff.diff_type,
                clause=diff.clause_category,
                knowledge_point_id=diff.knowledge_point_id,
                required_tables=required_tables,
                required_columns=_column_refs(diff, table, target_column),
                minimum_rows={table: minimum_rows} if table else {"*": minimum_rows},
                hard_constraints=constraints,
                estimated_cost=cost,
            )
        )
    return obligations
