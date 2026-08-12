"""
SQL Error Evidence Gathering and Knowledge Point Attribution (Observe & Diagnose Phases).

Collects and compiles diagnostic signals from various sensory inputs:
- Abstract Syntax Tree mismatches (E_AST)
- Execution output discrepancies (E_data)
- Mutation test results (E_MUT)

Determines the most probable knowledge point root-cause for a student's error.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import re
import sqlglot
from sqlglot import ErrorLevel, exp

from core.ast_schema import SQLStructureIR


@dataclass
class ASTError:
    """
    Local structural error shape consumed by BKT and ActionSelector.

    It intentionally mirrors core.ast_analyzer.ASTError without importing that
    module, because ast_analyzer initializes AI settings for LLM filtering.
    Phase 1/2 offline evidence packaging must not require backend .env values.

    Attributes:
        error_type (str): Category classification of the error (e.g. "missing_clause").
        clause (str): Affected SQL clause (e.g. "GROUP BY").
        knowledge_point_id (str): BKT taxonomy identifier.
        severity (float): Error severity rating scaled [0.0, 1.0].
        detail (str): Plain-text feedback description.
    """

    error_type: str
    clause: str
    knowledge_point_id: str
    severity: float
    detail: str


@dataclass
class EvidenceItem:
    """
    Individually explainable piece of diagnostic evidence.

    Attributes:
        source (str): Sensor source of the signal (e.g., E_AST, E_data, E_MUT).
        signal (str): Specific code representing the diagnostic observation.
        detail (str): Human-readable descriptive explanation of the finding.
        weight (float): Relative diagnostic weight / confidence.
    """

    source: str
    signal: str
    detail: str
    weight: float


@dataclass
class KPAttribution:
    """
    Pedagogical attribution matching errors to specific SQL knowledge points.

    Attributes:
        knowledge_point_id (str): Reference taxonomy identifier.
        l1_code (str): High-level category code.
        l2_code (str): Fine-grained category code.
        clause (str): SQL clause token.
        error_type (str): Category classification.
        severity (float): Error severity.
        confidence (float): Confidence score for BKT update calculations.
        detail (str): Summary detail message.
        evidence (list[EvidenceItem]): List of supporting evidence items.
    """

    knowledge_point_id: str
    l1_code: str
    l2_code: str
    clause: str
    error_type: str
    severity: float
    confidence: float
    detail: str
    evidence: list[EvidenceItem] = field(default_factory=list)

    def to_ast_error(self) -> ASTError:
        """
        Adapts the attribution record into a legacy ASTError consumer format.

        Returns:
            ASTError: Corresponding legacy shape.
        """
        return ASTError(
            error_type=self.error_type,
            clause=self.clause,
            knowledge_point_id=self.knowledge_point_id,
            severity=self.severity,
            detail=self.detail,
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Serializes the attribution record into a dictionary.

        Returns:
            dict[str, Any]: Dictionary representation.
        """
        data = asdict(self)
        data["evidence"] = [asdict(item) for item in self.evidence]
        return data


@dataclass
class AttributionResult:
    """
    Aggregated outcome containing the observation data and final attributions.

    Attributes:
        observation (dict[str, Any]): Raw telemetry gathered by sensors.
        attributions (list[KPAttribution]): Deduced errors ranked by severity.
        llm_arbitration_input (dict[str, Any]): Bundled context ready for LLM arbitration.
    """

    observation: dict[str, Any]
    attributions: list[KPAttribution]
    llm_arbitration_input: dict[str, Any]

    def to_ast_errors(self) -> list[ASTError]:
        """
        Converts all contained attributions into a list of legacy ASTErrors.

        Returns:
            list[ASTError]: List of legacy shapes.
        """
        return [item.to_ast_error() for item in self.attributions]

    def to_dict(self) -> dict[str, Any]:
        """
        Serializes the result payload.

        Returns:
            dict[str, Any]: Dictionary payload.
        """
        return {
            "observation": self.observation,
            "attributions": [item.to_dict() for item in self.attributions],
            "llm_arbitration_input": self.llm_arbitration_input,
        }


# Metadata dictionary mapping knowledge points to L1/L2 syllabus classification categories
KP_META: dict[str, dict[str, str]] = {
    "select-basic": {"l1": "KP_BASIC", "l2": "PROJ_COL", "clause": "SELECT"},
    "where": {"l1": "KP_FILTER", "l2": "COMP_VAL", "clause": "WHERE"},
    "comp-null": {"l1": "KP_FILTER", "l2": "COMP_NULL", "clause": "WHERE"},
    "order-by": {"l1": "KP_ORDER", "l2": "SORT_ASC", "clause": "ORDER BY"},
    "limit": {"l1": "KP_BASIC", "l2": "LIMIT_OFF", "clause": "LIMIT"},
    "distinct": {"l1": "KP_BASIC", "l2": "DISTINCT_SET", "clause": "DISTINCT"},
    "alias": {"l1": "KP_BASIC", "l2": "ALIAS_COL", "clause": "AS"},
    "agg-count": {"l1": "KP_AGG", "l2": "AGG_BASIC", "clause": "AGGREGATION"},
    "group-by": {"l1": "KP_AGG", "l2": "GB_SIMPLE", "clause": "GROUP BY"},
    "having": {"l1": "KP_AGG", "l2": "HV_SIMPLE", "clause": "HAVING"},
    "join-inner": {"l1": "KP_JOIN", "l2": "JOIN_INNER", "clause": "JOIN"},
    "join-on": {"l1": "KP_JOIN", "l2": "JOIN_ON", "clause": "JOIN ON"},
    "join-left": {"l1": "KP_JOIN", "l2": "JOIN_LEFT", "clause": "LEFT JOIN"},
    "join-right": {"l1": "KP_JOIN", "l2": "JOIN_RIGHT", "clause": "RIGHT JOIN"},
    "join-full": {"l1": "KP_JOIN", "l2": "JOIN_FULL", "clause": "FULL JOIN"},
    "subquery-scalar": {"l1": "KP_SUBQUERY", "l2": "SUB_TABLE", "clause": "SUBQUERY"},
    "subquery-correlated": {"l1": "KP_SUBQUERY", "l2": "SUB_CORRELATED", "clause": "CORRELATED SUBQUERY"},
    "subquery-in": {"l1": "KP_SUBQUERY", "l2": "SUB_IN_ALL_ANY", "clause": "IN SUBQUERY"},
    "subquery-exists": {"l1": "KP_SUBQUERY", "l2": "SUB_EXISTS", "clause": "EXISTS"},
    "cte": {"l1": "KP_ADVANCED", "l2": "CTE_SIMPLE", "clause": "WITH"},
    "cte-recursive": {"l1": "KP_ADVANCED", "l2": "CTE_RECURSIVE", "clause": "WITH RECURSIVE"},
    "union": {"l1": "KP_ADVANCED", "l2": "SET_UNION", "clause": "UNION"},
    "intersect": {"l1": "KP_ADVANCED", "l2": "SET_INTERSECT", "clause": "INTERSECT"},
    "except": {"l1": "KP_ADVANCED", "l2": "SET_EXCEPT", "clause": "EXCEPT"},
    "window-row-number": {"l1": "KP_ADVANCED", "l2": "WIN_OVER", "clause": "WINDOW"},
    "case": {"l1": "KP_FUNC", "l2": "CASE_SEARCH", "clause": "CASE"},
}

COMPARISON_NODE_TYPES = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.In, exp.Between, exp.Is)
AGG_NODE_TYPES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
COMPLEXITY_KPS = {
    "has_subquery": "subquery-scalar",
    "has_cte": "cte",
    "has_window": "window-row-number",
    "has_union": "union",
    "has_intersect": "intersect",
    "has_except": "except",
    "has_case": "case",
}


def _parse(sql: str) -> exp.Expression | None:
    """Parses SQL string into an AST, checking multiple dialects for fallback tolerance.

    Includes a roundtrip validation heuristic: sqlglot is very lenient and may
    silently re-interpret keywords (e.g. ``SELECT * FORM orders`` becomes
    ``SELECT * AS FORM``, dropping ``orders``).  We verify that meaningful
    identifiers from the original SQL survive the parse-and-serialise round trip.
    """
    import re as _re
    _KW = {
        'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'AS', 'ON', 'IN', 'IS',
        'NULL', 'LIKE', 'BETWEEN', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER',
        'CROSS', 'GROUP', 'BY', 'ORDER', 'HAVING', 'LIMIT', 'OFFSET', 'UNION',
        'ALL', 'DISTINCT', 'EXISTS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
        'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE', 'CREATE', 'TABLE',
        'DROP', 'ALTER', 'INDEX', 'WITH', 'RECURSIVE', 'ASC', 'DESC', 'TRUE',
        'FALSE', 'CAST', 'INTERSECT', 'EXCEPT', 'IF', 'THEN', 'TOP',
        'NULLS', 'FIRST', 'LAST', 'QUALIFY', 'WINDOW', 'ROWS', 'RANGE',
    }
    dialects = ("mysql", "sqlite", "tsql") if "`" in sql else ("sqlite", "mysql", "tsql")
    for dialect in dialects:
        try:
            statements = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
            parsed_statements = [
                statement for statement in statements
                if statement is not None and not isinstance(statement, exp.Semicolon)
            ]
            if len(parsed_statements) == 1 and isinstance(parsed_statements[0], exp.Query):
                parsed = parsed_statements[0]
                # Roundtrip check: ensure meaningful identifiers survive
                token_sql = _re.sub(r"--[^\r\n]*|/\*.*?\*/", " ", sql, flags=_re.DOTALL)
                raw_tokens = set(_re.findall(r'\b[A-Za-z_]\w*\b', token_sql))
                meaningful = {t for t in raw_tokens if t.upper() not in _KW}
                if meaningful:
                    roundtrip = parsed.sql(dialect=dialect)
                    rt_tokens = set(_re.findall(r'\b[A-Za-z_]\w*\b', roundtrip))
                    lost = meaningful - rt_tokens
                    if lost:
                        continue
                return parsed
        except Exception:
            continue
    return None


def _nodes(ast: exp.Expression | None, *types: type[exp.Expression]) -> list[exp.Expression]:
    """Finds all child nodes in the AST matching target node classes."""
    if ast is None:
        return []
    return list(ast.find_all(*types))


def _first(ast: exp.Expression | None, node_type: type[exp.Expression]) -> exp.Expression | None:
    """Finds the first child node in the AST matching a target class."""
    if ast is None:
        return None
    return ast.find(node_type)


def _node_sql(node: exp.Expression | None) -> str:
    """Converts a single AST node back into formatted MySQL query text."""
    if node is None:
        return ""
    try:
        return node.sql(dialect="mysql", normalize=True)
    except Exception:
        return str(node)


def _join_sides(ast: exp.Expression | None) -> list[str]:
    """Resolves join properties (e.g. LEFT, RIGHT, FULL) for all JOIN clauses in query."""
    sides: list[str] = []
    for join in _nodes(ast, exp.Join):
        side = str(join.args.get("side") or join.args.get("kind") or "INNER").upper()
        sides.append(side if side else "INNER")
    return sides


def _has_join_on(ast: exp.Expression | None) -> bool:
    """Verifies if join nodes include ON join condition arguments."""
    return any(bool(join.args.get("on")) for join in _nodes(ast, exp.Join))


def _join_on_sqls(ast: exp.Expression | None) -> list[str]:
    return [_node_sql(join.args.get("on")) for join in _nodes(ast, exp.Join) if join.args.get("on")]


def _agg_names(ast: exp.Expression | None) -> set[str]:
    """Retrieves list of aggregate functions used in query (e.g. COUNT, SUM)."""
    return {type(node).__name__.upper() for node in _nodes(ast, *AGG_NODE_TYPES)}


def _has_null_equality(ast: exp.Expression | None) -> bool:
    for node in _nodes(ast, exp.EQ, exp.NEQ):
        if isinstance(node.left, exp.Null) or isinstance(node.right, exp.Null):
            return True
    return False


def _set_operator_kp(ast: exp.Expression | None) -> str | None:
    if ast is None:
        return None
    if isinstance(ast, exp.Intersect) or ast.find(exp.Intersect):
        return "intersect"
    if isinstance(ast, exp.Except) or ast.find(exp.Except):
        return "except"
    if isinstance(ast, exp.Union) or ast.find(exp.Union):
        return "union"
    return None


def _window_sqls(ast: exp.Expression | None) -> list[str]:
    return [_node_sql(node) for node in _nodes(ast, exp.Window)]


def _has_exists_subquery(ast: exp.Expression | None) -> bool:
    return bool(_nodes(ast, exp.Exists))


def _is_subquery_correlated(subquery: exp.Expression) -> bool:
    def norm(name):
        return str(name).lower().strip('"`[]')
    inner_tables = set()
    for t in subquery.find_all(exp.Table):
        inner_tables.add(norm(t.name))
        if t.alias:
            inner_tables.add(norm(t.alias))
    for col in subquery.find_all(exp.Column):
        if col.table:
            table_ref = norm(col.table)
            if table_ref not in inner_tables:
                return True
    return False


def _has_correlated_subquery(ast: exp.Expression | None) -> bool:
    if ast is None:
        return False
    for sub in ast.find_all(exp.Subquery):
        if _is_subquery_correlated(sub):
            return True
    for exists in ast.find_all(exp.Exists):
        if _is_subquery_correlated(exists):
            return True
    return False


def _projection_sql(ast: exp.Expression | None) -> str:
    select = _first(ast, exp.Select)
    if not isinstance(select, exp.Select):
        return ""
    return ", ".join(_node_sql(item) for item in select.expressions or [])


def _select_node(ast: exp.Expression | None) -> exp.Select | None:
    select = _first(ast, exp.Select)
    return select if isinstance(select, exp.Select) else None


def _outer_select_has_distinct(ast: exp.Expression | None) -> bool:
    select = _select_node(ast)
    return bool(select and select.args.get("distinct"))


def _has_select_distinct(ast: exp.Expression | None) -> bool:
    """Return only SELECT DISTINCT, excluding aggregate DISTINCT arguments."""
    return _outer_select_has_distinct(ast)


def _outer_distinct_likely_redundant(ast: exp.Expression | None) -> bool:
    """
    Detects the common dead DISTINCT pattern:
    SELECT DISTINCT <group keys>, aggregates FROM ... GROUP BY <group keys>.

    This is intentionally conservative. It only marks the top-level DISTINCT as
    redundant when every GROUP BY expression is projected by the same SELECT.
    """
    select = _select_node(ast)
    group = _first(ast, exp.Group)
    if not select or not group or not select.args.get("distinct"):
        return False

    group_sqls = {_node_sql(expr) for expr in group.expressions or [] if _node_sql(expr)}
    if not group_sqls:
        return False

    projection_sqls = set()
    for item in select.expressions or []:
        expression = item.this if isinstance(item, exp.Alias) else item
        sql = _node_sql(expression)
        if sql:
            projection_sqls.add(sql)

    return group_sqls.issubset(projection_sqls)


def _non_aggregate_projection_sqls(ast: exp.Expression | None) -> list[str]:
    select = _select_node(ast)
    if not select:
        return []

    out: list[str] = []
    for item in select.expressions or []:
        expression = item.this if isinstance(item, exp.Alias) else item
        if isinstance(expression, exp.Star) or _node_has(expression, *AGG_NODE_TYPES):
            continue
        sql = _node_sql(expression)
        if sql:
            out.append(sql)
    return out


def _group_by_sqls(ast: exp.Expression | None) -> list[str]:
    group = _first(ast, exp.Group)
    if not group:
        return []
    return [_node_sql(expr) for expr in group.expressions or [] if _node_sql(expr)]


def _has_only_full_group_by_risk(ast: exp.Expression | None) -> bool:
    group_items = set(_group_by_sqls(ast))
    if not group_items:
        return False
    return any(item not in group_items for item in _non_aggregate_projection_sqls(ast))


def _select_projection_count(ast: exp.Expression | None) -> int:
    """Calculates number of columns/expressions projected in select list."""
    select = _select_node(ast)
    if not select:
        return 0
    return len(select.expressions or [])


def _has_recursive_cte(ast: exp.Expression | None) -> bool:
    if ast is None:
        return False
    with_node = ast.args.get("with") or ast.args.get("with_") or ast.find(exp.With)
    if with_node is not None and bool(with_node.args.get("recursive")):
        return True
    try:
        return "WITH RECURSIVE" in ast.sql(dialect="sqlite").upper()
    except Exception:
        return False


def _features(ast: exp.Expression | None) -> dict[str, Any]:
    """Extracts structural features (clause presence, join count, aggregations) from AST.

    Also builds and attaches a :class:`SQLStructureIR` instance under the ``_ir``
    key for downstream IR-based analysis (structural KP detection, clause comparison).
    The ``_ir`` key is internal and must be excluded from JSON-serialised outputs.
    """
    ir = SQLStructureIR.from_ast(ast) if ast is not None else SQLStructureIR()
    return {
        "parse_ok": ast is not None,
        "has_select": _first(ast, exp.Select) is not None,
        "has_where": _first(ast, exp.Where) is not None,
        "has_order": _first(ast, exp.Order) is not None,
        "has_limit": _first(ast, exp.Limit) is not None or _first(ast, exp.Offset) is not None,
        # ``exp.Distinct`` is also used by COUNT(DISTINCT ...) and other
        # aggregates.  The ``distinct`` KP here is the SELECT-level set
        # operation, so inspect the top SELECT flag only.
        "has_distinct": _has_select_distinct(ast),
        "has_outer_distinct": _outer_select_has_distinct(ast),
        "outer_distinct_likely_redundant": _outer_distinct_likely_redundant(ast),
        "has_group": _first(ast, exp.Group) is not None,
        "has_having": _first(ast, exp.Having) is not None,
        "has_agg": bool(_agg_names(ast)),
        "has_subquery": bool(_nodes(ast, exp.Subquery)),
        "has_cte": bool(_nodes(ast, exp.CTE)),
        "has_union": bool(_nodes(ast, exp.Union)),
        "has_intersect": bool(_nodes(ast, exp.Intersect)),
        "has_except": bool(_nodes(ast, exp.Except)),
        "has_recursive_cte": _has_recursive_cte(ast),
        "has_window": bool(_nodes(ast, exp.Window)),
        "has_case": bool(_nodes(ast, exp.Case)),
        "join_count": len(_nodes(ast, exp.Join)),
        "join_sides": _join_sides(ast),
        "has_join_on": _has_join_on(ast),
        "join_on_sqls": _join_on_sqls(ast),
        "agg_functions": sorted(_agg_names(ast)),
        "projection_count": _select_projection_count(ast),
        "non_aggregate_projection_sqls": _non_aggregate_projection_sqls(ast),
        "group_by_sqls": _group_by_sqls(ast),
        "only_full_group_by_risk": _has_only_full_group_by_risk(ast),
        "_ir": ir,
    }


def _clause_node(ast: exp.Expression | None, node_type: type[exp.Expression]) -> exp.Expression | None:
    """Wraps AST single node search calls."""
    return _first(ast, node_type)


def _node_has(node: exp.Expression | None, *types: type[exp.Expression]) -> bool:
    """Checks if a given sub-tree contains target AST nodes."""
    return bool(node and (isinstance(node, types) or any(True for _ in node.find_all(*types))))


def _node_items_sql(node: exp.Expression | None, *types: type[exp.Expression]) -> list[str]:
    """Serializes all matching child nodes inside sub-tree back to list of SQL strings."""
    if node is None:
        return []
    items: list[exp.Expression] = []
    if isinstance(node, types):
        items.append(node)
    items.extend(list(node.find_all(*types)))
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        sql = _node_sql(item)
        if sql and sql not in seen:
            seen.add(sql)
            out.append(sql)
    return out


def _comparison_locations(ast: exp.Expression | None) -> dict[str, list[str]]:
    """Maps comparison predicates (e.g. =, LIKE) to their containing SQL clauses."""
    if ast is None:
        return {}
    locations: dict[str, list[str]] = {}
    where = _clause_node(ast, exp.Where)
    having = _clause_node(ast, exp.Having)
    for name, node in [("WHERE", where), ("HAVING", having)]:
        items = _node_items_sql(node, *COMPARISON_NODE_TYPES)
        if items:
            locations[name] = items
    select_items: list[str] = []
    select = _first(ast, exp.Select)
    if isinstance(select, exp.Select):
        for expression in select.expressions or []:
            select_items.extend(_node_items_sql(expression, *COMPARISON_NODE_TYPES))
    if select_items:
        locations["SELECT"] = list(dict.fromkeys(select_items))
    join_items: list[str] = []
    for join in _nodes(ast, exp.Join):
        join_items.extend(_node_items_sql(join.args.get("on"), *COMPARISON_NODE_TYPES))
    if join_items:
        locations["JOIN ON"] = join_items
    return locations


def _aggregate_locations(ast: exp.Expression | None) -> dict[str, list[str]]:
    """Maps aggregate function occurrences to their containing SQL clauses."""
    if ast is None:
        return {}

    def aggregate_items(node: exp.Expression | None) -> list[str]:
        if node is None:
            return []
        items: list[str] = []
        for aggregate in node.find_all(*AGG_NODE_TYPES):
            parent = aggregate.parent
            nested_query = False
            while parent is not None and parent is not node:
                if isinstance(parent, (exp.Subquery, exp.Select)):
                    nested_query = True
                    break
                parent = parent.parent
            if not nested_query:
                sql = _node_sql(aggregate)
                if sql and sql not in items:
                    items.append(sql)
        return items

    locations: dict[str, list[str]] = {}
    for name, node in [
        ("WHERE", _clause_node(ast, exp.Where)),
        ("HAVING", _clause_node(ast, exp.Having)),
        ("ORDER BY", _clause_node(ast, exp.Order)),
    ]:
        items = aggregate_items(node)
        if items:
            locations[name] = items
    select_items: list[str] = []
    select = _first(ast, exp.Select)
    if isinstance(select, exp.Select):
        for expression in select.expressions or []:
            select_items.extend(_node_items_sql(expression, *AGG_NODE_TYPES))
    if select_items:
        locations["SELECT"] = list(dict.fromkeys(select_items))
    return locations


def _structural_kps(features: dict[str, Any]) -> list[str]:
    """Infers taxonomy knowledge point IDs covered based on structural AST features.

    Uses the attached :class:`SQLStructureIR` (``features["_ir"]``) for richer
    KP detection — the IR tracks per-join types, correlated subqueries, and
    EXISTS / IN subquery patterns that pure boolean features cannot capture.
    Falls back to boolean-feature-based detection when the IR is unavailable.
    """
    ir: SQLStructureIR | None = features.get("_ir")

    if ir is not None:
        kps = ir.feature_kps()
        # Augment with KPs not covered by the IR's feature_kps() method
        if features.get("has_agg") and "agg-count" not in kps:
            kps.append("agg-count")
        if ir.joins and not any(j.get("condition") for j in ir.joins):
            if "join-on" not in kps:
                kps.append("join-on")
        if features.get("only_full_group_by_risk") and "group-by" in kps:
            pass  # group-by already present; risk is a severity modifier, not a new KP
        return list(dict.fromkeys(kps))

    # Fallback: boolean feature-based detection (used when IR is unavailable)
    kps = ["select-basic"] if features.get("has_select") else []
    for kp_id, key in [
        ("where", "has_where"),
        ("order-by", "has_order"),
        ("limit", "has_limit"),
        ("distinct", "has_distinct"),
        ("agg-count", "has_agg"),
        ("group-by", "has_group"),
        ("having", "has_having"),
        ("subquery-scalar", "has_subquery"),
        ("cte", "has_cte"),
        ("union", "has_union"),
        ("intersect", "has_intersect"),
        ("except", "has_except"),
        ("cte-recursive", "has_recursive_cte"),
        ("window-row-number", "has_window"),
        ("case", "has_case"),
    ]:
        if features.get(key):
            kps.append(kp_id)
    if features.get("join_count", 0) > 0:
        kps.append("join-inner")
    if features.get("has_join_on"):
        kps.append("join-on")
    for side, kp_id in [("LEFT", "join-left"), ("RIGHT", "join-right"), ("FULL", "join-full")]:
        if side in features.get("join_sides", []):
            kps.append(kp_id)
    return list(dict.fromkeys(kps))


def _clause_sql_map(ast: exp.Expression | None) -> dict[str, str]:
    """Reconstructs text representation segments for each clause in the parsed query."""
    if ast is None:
        return {}
    clauses = {
        "WHERE": _first(ast, exp.Where),
        "GROUP BY": _first(ast, exp.Group),
        "HAVING": _first(ast, exp.Having),
        "ORDER BY": _first(ast, exp.Order),
        "LIMIT": _first(ast, exp.Limit) or _first(ast, exp.Offset),
    }
    out = {name: _node_sql(node) for name, node in clauses.items() if node is not None}
    select = _first(ast, exp.Select)
    if isinstance(select, exp.Select):
        projection_sql = ", ".join(_node_sql(item) for item in select.expressions or [])
        if projection_sql:
            out["SELECT"] = projection_sql
    join_on = [_node_sql(join.args.get("on")) for join in _nodes(ast, exp.Join) if join.args.get("on")]
    if join_on:
        out["JOIN ON"] = " | ".join(join_on)
    return out


def _kp_profile(
    *,
    role: str,
    ast: exp.Expression | None,
    features: dict[str, Any],
    question_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Constructs a profile summary detailing targeted or observed query knowledge points."""
    question_context = question_context or {}
    aggregate_locations = _aggregate_locations(ast)
    illegal_aggregate_locations = [name for name in aggregate_locations if name == "WHERE"]
    redundant_kps = ["distinct"] if features.get("outer_distinct_likely_redundant") else []
    return {
        "role": role,
        "question_l1": question_context.get("l1") if role == "intended" else None,
        "question_l2": question_context.get("l2") if role == "intended" else None,
        "structural_kps": _structural_kps(features),
        "features": {k: v for k, v in features.items() if k != "_ir"},
        "clause_sql": _clause_sql_map(ast),
        "comparison_locations": _comparison_locations(ast),
        "aggregate_locations": aggregate_locations,
        "illegal_aggregate_locations": illegal_aggregate_locations,
        "redundant_kps": redundant_kps,
        "joins_have_on": features.get("join_count", 0) == 0 or features.get("has_join_on"),
        "complexity_kps": [kp for key, kp in COMPLEXITY_KPS.items() if features.get(key)],
    }


def _ablation_candidates(
    intended: dict[str, Any],
    observed: dict[str, Any],
    judge: dict[str, Any],
    mutation_detail: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Builds a compact ablation plan for structure, probe and mutation evidence."""
    candidates: list[dict[str, Any]] = []
    intended_kps = list(dict.fromkeys(intended.get("structural_kps") or []))
    observed_kps = set(observed.get("structural_kps") or [])
    clause_map = intended.get("clause_sql") or {}

    structure_axes = {
        "where": "predicate_counterexample",
        "join-inner": "join_topology_counterexample",
        "join-on": "join_on_counterexample",
        "join-left": "outer_join_dangling_tuple",
        "join-right": "outer_join_dangling_tuple",
        "join-full": "outer_join_dangling_tuple",
        "group-by": "group_cardinality_probe",
        "having": "group_cardinality_probe",
        "order-by": "ordered_compare_probe",
        "limit": "limit_row_count_probe",
        "distinct": "duplicate_projection_probe",
        "subquery-scalar": "subquery_equivalence_probe",
        "subquery-correlated": "correlated_subquery_probe",
        "cte": "cte_base_constraint_probe",
        "cte-recursive": "recursive_cte_boundary_probe",
        "union": "set_operator_overlap_probe",
        "intersect": "set_operator_overlap_probe",
        "except": "set_operator_overlap_probe",
        "window-row-number": "window_partition_order_probe",
        "case": "case_branch_probe",
    }

    for kp_id in intended_kps:
        candidates.append({
            "layer": "AST",
            "knowledge_point_id": kp_id,
            "clause": KP_META.get(kp_id, {}).get("clause") or clause_map.get(kp_id),
            "probe_tactic": structure_axes.get(kp_id, "structure_diff_probe"),
            "operation": "drop_or_substitute",
            "expected_signal": "If the error remains after structure-level substitution, this KP is likely not the primary cause." if kp_id in observed_kps else "If restoring this structure fixes the output, this KP is a primary cause.",
        })

    data_axes = []
    if judge.get("row_count_match") is False or judge.get("standard_row_count") != judge.get("student_row_count"):
        data_axes.append(("row_count", "limit_row_count_probe"))
    if judge.get("columns_match") is False:
        data_axes.append(("column_shape", "projection_shape_check"))
    if judge.get("ordered_compare"):
        data_axes.append(("row_order", "ordered_compare_probe"))
    if judge.get("standard_duplicate_row_count") != judge.get("student_duplicate_row_count"):
        data_axes.append(("duplicate_rows", "duplicate_projection_probe"))
    if judge.get("suspected_cartesian_product"):
        data_axes.append(("cartesian_product", "join_on_counterexample"))

    for signal, tactic in data_axes:
        candidates.append({
            "layer": "DATA",
            "signal": signal,
            "probe_tactic": tactic,
            "operation": "remove_probe_dimension",
            "expected_signal": "If removing this probe axis does not change the mismatch, the error is likely elsewhere.",
        })

    mutation_tests = (mutation_detail or {}).get("tests") or []
    for test in mutation_tests:
        candidates.append({
            "layer": "MUTATION",
            "knowledge_point_id": test.get("knowledge_point_id"),
            "clause": test.get("clause"),
            "probe_tactic": test.get("action") or "mutation_test",
            "operation": "replace_then_remove",
            "expected_signal": "If replacement fixes but removal does not, the clause is a direct fault locus.",
            "fixed_by_replacement": bool(test.get("fixed_by_replacement")),
            "removed_student_clause_equivalent": bool(test.get("removed_student_clause_equivalent")),
        })

    return candidates


def _add_misalignment_evidence(
    builder: _AttributionBuilder,
    intended: dict[str, Any],
    observed: dict[str, Any],
    judge: dict[str, Any],
    is_correct: bool,
) -> list[dict[str, Any]]:
    """
    Performs bidirectional comparison between target KP requirements and student's query.

    Logs diagnostic evidence if requirements are not met or if mismatches exist.
    """
    misalignments: list[dict[str, Any]] = []
    target_kps = set(intended.get("structural_kps") or [])
    observed_kps = set(observed.get("structural_kps") or [])

    def add(
        *,
        category: str,
        kp_id: str,
        detail: str,
        source: str,
        signal: str,
        weight: float,
        error_type: str | None = None,
        clause: str | None = None,
    ) -> None:
        meta = KP_META.get(kp_id, {})
        misalignments.append({
            "category": category,
            "knowledge_point_id": kp_id,
            "l1_code": meta.get("l1"),
            "l2_code": meta.get("l2"),
            "clause": clause or meta.get("clause"),
            "detail": detail,
            "source": source,
            "signal": signal,
            "weight": round(weight, 3),
        })
        builder.add(kp_id, error_type or category.lower(), source, signal, detail, weight)

    # 1. Check for expected structural knowledge points that are missing in student query
    for kp_id in sorted(target_kps - observed_kps):
        if kp_id == "select-basic":
            continue
        if is_correct:
            add(
                category="Complication",
                kp_id=kp_id,
                detail=f"动态数据判定结果等价；目标中的 {kp_id} 被学生用其他结构实现。",
                source="E_AST",
                signal=f"equivalent_alternative:{kp_id}",
                weight=0.32,
                error_type="complication",
            )
            continue
        if kp_id in set(intended.get("redundant_kps") or []):
            add(
                category="Complication",
                kp_id=kp_id,
                detail=f"目标 SQL 包含 {kp_id}，但该结构在当前 GROUP BY 语义下可能冗余，不应作为主要错因。",
                source="E_AST",
                signal=f"redundant_target:{kp_id}",
                weight=0.24,
                error_type="complication",
            )
            continue
        add(
            category="Lacking",
            kp_id=kp_id,
            detail=f"目标知识点包含 {kp_id}，但学生 SQL 没有对应结构。",
            source="E_AST",
            signal=f"target_missing:{kp_id}",
            weight=0.88,
            error_type="lacking",
        )

    # 2. Check for clause confusion (e.g. placing WHERE filtering logic inside HAVING)
    intended_cmp = intended.get("comparison_locations") or {}
    observed_cmp = observed.get("comparison_locations") or {}
    for src, dst, kp_id in [("HAVING", "WHERE", "having"), ("WHERE", "HAVING", "where")]:
        if intended_cmp.get(src) and observed_cmp.get(dst):
            add(
                category="Confusion",
                kp_id=kp_id,
                detail=f"比较条件目标位置在 {src}，学生写在 {dst}，属于子句职责错位。",
                source="E_AST",
                signal=f"clause_confusion:{src}_vs_{dst}",
                weight=0.82,
                error_type="confusion",
                clause=f"{src}/{dst}",
            )

    # 3. Check for aggregate functions placed inside WHERE clauses (invalid SQL syntax)
    if intended.get("features", {}).get("has_agg") and observed.get("illegal_aggregate_locations"):
        add(
            category="Confusion",
            kp_id="having" if intended.get("features", {}).get("has_having") else "agg-count",
            detail="学生把聚合函数放在 WHERE 等非法或不合适的位置，混淆了行过滤与分组后过滤。",
            source="E_AST",
            signal="aggregate_illegal_location",
            weight=0.86,
            error_type="confusion",
        )

    # 4. Check for JOIN constructs missing ON join predicates
    if observed.get("features", {}).get("join_count", 0) > 0 and not observed.get("features", {}).get("has_join_on"):
        equivalent_join_rewrite = bool(is_correct and intended.get("features", {}).get("has_join_on"))
        add(
            category="Complication" if equivalent_join_rewrite else "Confusion",
            kp_id="join-on",
            detail=(
                "动态数据判定结果等价；学生使用 WHERE 中的连接谓词替代了显式 JOIN ON。"
                if equivalent_join_rewrite
                else "学生使用了 JOIN，但没有写 ON 连接条件，连接结构存在职责缺口。"
            ),
            source="E_AST",
            signal="equivalent_implicit_join" if equivalent_join_rewrite else "join_without_on",
            weight=0.32 if equivalent_join_rewrite else 0.95,
            error_type="complication" if equivalent_join_rewrite else "confusion",
        )

    # 5. Check for structural mismatches (such as mismatching ORDER BY or GROUP BY values)
    intended_clauses = intended.get("clause_sql") or {}
    observed_clauses = observed.get("clause_sql") or {}
    clause_to_kp = {
        "WHERE": "where",
        "GROUP BY": "group-by",
        "HAVING": "having",
        "ORDER BY": "order-by",
        "LIMIT": "limit",
        "JOIN ON": "join-on",
    }
    for clause, kp_id in clause_to_kp.items():
        if intended_clauses.get(clause) and observed_clauses.get(clause) and intended_clauses[clause] != observed_clauses[clause]:
            weight = 0.32 if is_correct else 0.66
            detail = (
                f"{clause} 写法与目标不同，但动态数据判定结果等价。"
                if is_correct
                else f"{clause} 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。"
            )
            if clause == "GROUP BY" and observed.get("features", {}).get("only_full_group_by_risk"):
                weight = 0.9
                detail = "学生 SELECT 中存在未聚合列不在 GROUP BY 中，在严格 SQL 模式下会报错；即使宽松执行，也会造成分组粒度错位。"
            if clause == "JOIN ON":
                weight = 0.86
                detail = "JOIN ON 结构存在但连接键与目标不一致，可能造成表之间的数据流断裂。"
            add(
                category="Complication" if is_correct else "Logical",
                kp_id=kp_id,
                detail=detail,
                source="E_MUT",
                signal=f"same_clause_mismatch:{clause}",
                weight=weight,
                error_type="complication" if is_correct else "logical",
                clause=clause,
            )

    # 6. Check for equivalence validation failures on generated counter-example test data
    generated_equiv = judge.get("is_equivalent_on_generated_data")
    if is_correct and generated_equiv is False:
        for kp_id in sorted((target_kps & observed_kps) or target_kps or {"where"}):
            add(
                category="Generality",
                kp_id=kp_id,
                detail="原始判题数据正确，但动态生成的反例数据暴露出泛化错误。",
                source="E_data",
                signal="counterexample_failed",
                weight=0.78,
                error_type="generality",
            )

    # 7. Check for redundant complexities (e.g. student used an unnecessary subquery)
    extra_complexity = sorted(set(observed.get("complexity_kps") or []) - set(intended.get("complexity_kps") or []))
    if is_correct and extra_complexity:
        for kp_id in extra_complexity:
            add(
                category="Complication",
                kp_id=kp_id,
                detail=f"结果正确，但学生使用了目标不需要的 {kp_id} 复杂结构，可归因为不必要复杂化。",
                source="E_AST",
                signal=f"unnecessary_complexity:{kp_id}",
                weight=0.42,
                error_type="complication",
            )

    return misalignments


def _judge_features(judge_detail: dict[str, Any] | None, error_message: str | None) -> dict[str, Any]:
    """Extracts execution and sandbox properties from the judge output metadata."""
    detail = judge_detail or {}
    comparison = detail.get("comparison") or {}
    return {
        "is_correct": bool(detail.get("is_correct", False)),
        "error_message": detail.get("error_message") or error_message,
        "student_rows": (detail.get("student_result_meta") or {}).get("row_count"),
        "correct_rows": (detail.get("correct_result_meta") or {}).get("row_count"),
        "student_columns": (detail.get("student_result_meta") or {}).get("columns"),
        "correct_columns": (detail.get("correct_result_meta") or {}).get("columns"),
        "ordered_compare": comparison.get("ordered"),
        "alias_enforced": comparison.get("enforce_aliases"),
        "sandbox_executed": comparison.get("sandbox_executed"),
        "sandbox_error": comparison.get("sandbox_error"),
        "row_count_match": comparison.get("row_count_match"),
        "standard_row_count": comparison.get("standard_row_count"),
        "student_row_count": comparison.get("student_row_count"),
        "columns_match": comparison.get("columns_match"),
        "standard_duplicate_row_count": comparison.get("standard_duplicate_row_count"),
        "student_duplicate_row_count": comparison.get("student_duplicate_row_count"),
        "suspected_cartesian_product": comparison.get("suspected_cartesian_product"),
        "is_equivalent_on_generated_data": detail.get("is_equivalent_on_generated_data") or comparison.get("is_equivalent_on_generated_data"),
    }


class _AttributionBuilder:
    """
    Builder utility consolidating multiple evidence records into discrete KP attributions.

    Keeps track of severity and confidence updates for each BKT knowledge point dimensions.
    """

    def __init__(self) -> None:
        self._items: dict[str, KPAttribution] = {}

    def add(self, kp_id: str, error_type: str, source: str, signal: str, detail: str, weight: float) -> None:
        """Adds or updates diagnostic evidence for a specific knowledge point ID.

        Deduplicates semantically equivalent signals (e.g. ``target_missing:where``
        and ``missing:has_where`` both mean "WHERE is absent") to prevent
        confidence inflation from repeated observations of the same fact.
        """
        meta = KP_META.get(kp_id, {"l1": "KP_BASIC", "l2": kp_id.upper(), "clause": kp_id.upper()})
        item = self._items.get(kp_id)
        # Canonicalise signal for dedup: strip source prefix, extract core token
        canon = signal.split(":", 1)[-1].lower().lstrip("has_")
        if item is not None:
            existing_canons = {
                ev.signal.split(":", 1)[-1].lower().lstrip("has_")
                for ev in item.evidence
            }
            if canon in existing_canons:
                # Still update severity from this evidence, but don't double-count confidence
                item.severity = round(min(1.0, max(item.severity, weight)), 3)
                if weight >= item.severity:
                    item.detail = detail
                return
        evidence = EvidenceItem(source=source, signal=signal, detail=detail, weight=round(weight, 3))
        if item is None:
            item = KPAttribution(
                knowledge_point_id=kp_id,
                l1_code=meta["l1"],
                l2_code=meta["l2"],
                clause=meta["clause"],
                error_type=error_type,
                severity=round(min(1.0, weight), 3),
                confidence=round(min(1.0, 0.45 + weight * 0.4), 3),
                detail=detail,
                evidence=[evidence],
            )
            self._items[kp_id] = item
        else:
            item.evidence.append(evidence)
            # Take the maximum severity seen among all related error signals
            item.severity = round(min(1.0, max(item.severity, weight)), 3)
            # Scale attribution confidence incrementally as more evidence reports arrive
            item.confidence = round(min(1.0, item.confidence + weight * 0.18), 3)
            if weight >= item.severity:
                item.detail = detail

    def cap(self, kp_id: str, max_severity: float, detail: str | None = None) -> None:
        """Caps a KP score when later analysis proves the signal is low-value."""
        item = self._items.get(kp_id)
        if item is None:
            return
        item.severity = round(min(item.severity, max_severity), 3)
        item.confidence = round(min(item.confidence, 0.65), 3)
        item.error_type = "complication"
        if detail:
            item.detail = detail
        # Avoid duplicate cap evidence
        if not any(ev.signal == "severity_cap:redundant_structure" for ev in item.evidence):
            item.evidence.append(EvidenceItem(
                source="E_AST",
                signal="severity_cap:redundant_structure",
                detail=detail or "该结构差异被判定为低优先级冗余差异。",
                weight=round(max_severity, 3),
            ))

    def build(self) -> list[KPAttribution]:
        """Finalizes the attribution records, sorting by severity and confidence levels."""
        items = list(self._items.values())
        items.sort(key=lambda item: (item.severity, item.confidence, len(item.evidence)), reverse=True)
        return items


def _add_ast_evidence(builder: _AttributionBuilder, std: dict[str, Any], stu: dict[str, Any]) -> None:
    """Infers missing AST constructs based on differences between student and reference features."""
    expected_flags = [
        ("where", "has_where", "标准答案需要 WHERE 过滤，但学生 SQL 缺少过滤条件"),
        ("order-by", "has_order", "标准答案需要 ORDER BY 排序，但学生 SQL 缺少排序结构"),
        ("limit", "has_limit", "标准答案限制返回行数，但学生 SQL 缺少 LIMIT/OFFSET"),
        ("distinct", "has_distinct", "标准答案需要 DISTINCT 去重，但学生 SQL 缺少去重"),
        ("agg-count", "has_agg", "标准答案使用聚合函数，但学生 SQL 缺少聚合结构"),
        ("group-by", "has_group", "标准答案需要 GROUP BY 分组，但学生 SQL 缺少分组"),
        ("having", "has_having", "标准答案需要 HAVING 分组后筛选，但学生 SQL 缺少 HAVING"),
        ("subquery-scalar", "has_subquery", "标准答案使用子查询，但学生 SQL 未体现子查询结构"),
        ("cte", "has_cte", "标准答案使用 WITH/CTE，但学生 SQL 缺少 CTE"),
        ("cte-recursive", "has_recursive_cte", "标准答案使用递归 CTE，但学生 SQL 未体现递归终止结构"),
        ("union", "has_union", "标准答案使用 UNION 集合操作，但学生 SQL 缺少集合操作"),
        ("intersect", "has_intersect", "标准答案使用 INTERSECT 集合操作，但学生 SQL 缺少交集操作"),
        ("except", "has_except", "标准答案使用 EXCEPT 集合操作，但学生 SQL 缺少差集操作"),
        ("window-row-number", "has_window", "标准答案使用窗口函数，但学生 SQL 缺少窗口结构"),
        ("case", "has_case", "标准答案使用 CASE 条件表达式，但学生 SQL 缺少 CASE"),
    ]
    for kp_id, key, detail in expected_flags:
        if std.get(key) and not stu.get(key):
            builder.add(kp_id, "missing_clause", "E_AST", f"missing:{key}", detail, 0.9)

    if std["join_count"] > stu["join_count"]:
        builder.add("join-inner", "missing_clause", "E_AST", "missing:join", "标准答案需要更多 JOIN 结构，学生 SQL 连接数量不足", 0.9)
    if std["has_join_on"] and stu["join_count"] > 0 and not stu["has_join_on"]:
        builder.add("join-on", "missing_clause", "E_AST", "missing:join_on", "学生写了 JOIN，但缺少 ON 连接条件，可能形成笛卡尔积或错误连接", 0.95)

    for side, kp_id in [("LEFT", "join-left"), ("RIGHT", "join-right"), ("FULL", "join-full")]:
        if side in std["join_sides"] and side not in stu["join_sides"]:
            builder.add(kp_id, "wrong_join_type", "E_AST", f"join_type:{side}", f"标准答案需要 {side} JOIN，但学生没有使用对应外连接类型", 0.85)

    if std["projection_count"] and stu["projection_count"] and stu["projection_count"] < std["projection_count"]:
        builder.add("select-basic", "missing_partial", "E_AST", "projection_count", "学生 SELECT 输出列数量少于标准答案，可能漏选目标列或表达式", 0.7)


def _add_data_evidence(
    builder: _AttributionBuilder,
    judge: dict[str, Any],
    std: dict[str, Any],
    stu: dict[str, Any],
) -> None:
    """Infers error causes based on sandbox execution result mismatches (e.g. row/column counts)."""
    message = str(judge.get("error_message") or "")
    row_count_mismatch = (
        judge.get("row_count_match") is False
        or (
            judge.get("standard_row_count") is not None
            and judge.get("student_row_count") is not None
            and judge.get("standard_row_count") != judge.get("student_row_count")
        )
    )
    empty_student_when_expected_rows = (
        (judge.get("standard_row_count") or 0) > 0
        and judge.get("student_row_count") == 0
    )
    join_on_changed = bool(std.get("join_on_sqls") and stu.get("join_on_sqls") and std.get("join_on_sqls") != stu.get("join_on_sqls"))
    if not message and not row_count_mismatch:
        return

    # Check for row count issues, pointing to filter conditions, joins or group/having boundaries
    if "行数" in message or row_count_mismatch:
        if std["has_where"]:
            builder.add("where", "data_mismatch", "E_data", "row_count", "结果行数不匹配，优先怀疑过滤条件边界、比较符或逻辑组合", 0.72)
        if std["has_distinct"] and not std.get("outer_distinct_likely_redundant"):
            builder.add("distinct", "data_mismatch", "E_data", "row_count_duplicate", "结果行数不匹配且标准答案需要 DISTINCT，可能漏掉去重", 0.7)
        if std["join_count"]:
            kp_id = "join-on" if stu["join_count"] and not stu["has_join_on"] else "join-inner"
            builder.add(kp_id, "data_mismatch", "E_data", "row_count_join", "结果行数不匹配且题目涉及 JOIN，可能连接条件或连接类型错误", 0.74)
        if std["join_count"] and empty_student_when_expected_rows and (join_on_changed or not stu.get("has_join_on")):
            builder.add(
                "join-on",
                "blocking",
                "E_data",
                "empty_result_join_blocker",
                "标准答案有结果但学生结果为空，且题目涉及 JOIN；连接键错误可能阻断了后续所有数据流。",
                0.93,
            )
        if std["has_group"]:
            builder.add("group-by", "data_mismatch", "E_data", "row_count_group", "结果行数不匹配且题目涉及分组，可能 GROUP BY 粒度错误", 0.72)
        if std["has_having"]:
            builder.add("having", "data_mismatch", "E_data", "row_count_having", "结果行数不匹配且题目涉及 HAVING，可能分组后筛选条件错误", 0.72)

    # Check for column schema problems or missing required aliases
    if "列结构" in message or "列数" in message or "缺少列" in message or "多余列" in message:
        builder.add("select-basic", "data_mismatch", "E_data", "column_shape", "结果列结构不匹配，可能 SELECT 投影列错误", 0.76)
        if judge.get("alias_enforced"):
            builder.add("alias", "data_mismatch", "E_data", "alias_required", "题目要求输出列名/别名，学生 SQL 的别名结构不一致", 0.72)

    # Check for order mismatches, indicating ORDER BY issues
    ordered_compare = bool(judge.get("ordered_compare"))
    if ("ORDER BY" in message or (ordered_compare and "顺序" in message)) and (std["has_order"] or stu["has_order"] or ordered_compare):
        builder.add("order-by", "data_mismatch", "E_data", "row_order", "结果顺序不一致，可能 ORDER BY 字段或 ASC/DESC 方向错误", 0.78)

    # Check for cell values mismatch, pointing to logic components like filter boundaries, subqueries, etc.
    if "结果不匹配" in message or "结果数据不匹配" in message or "不一致" in message:
        if std["has_where"]:
            builder.add("where", "data_mismatch", "E_data", "value_mismatch_filter", "结果值不匹配，且题目包含 WHERE，可能过滤语义错误", 0.58)
        if std["has_agg"]:
            builder.add("agg-count", "data_mismatch", "E_data", "value_mismatch_agg", "结果值不匹配，且题目包含聚合，可能聚合函数或聚合对象错误", 0.58)
        if std["has_subquery"]:
            builder.add("subquery-scalar", "data_mismatch", "E_data", "value_mismatch_subquery", "结果值不匹配，且题目包含子查询，可能子查询条件或返回集合错误", 0.58)


def _add_mutation_evidence(
    builder: _AttributionBuilder,
    standard_ast: exp.Expression | None,
    student_ast: exp.Expression | None,
    mutation_detail: dict[str, Any] | None = None,
) -> None:
    """Infers error attributions using clause mutation/replacement logic logs."""
    if _has_null_equality(student_ast):
        builder.add(
            "comp-null",
            "logical",
            "E_AST",
            "null_equality",
            "学生 SQL 使用 = NULL 或 <> NULL 进行空值比较，应使用 IS NULL / IS NOT NULL。",
            0.84,
        )

    std_projection = _projection_sql(standard_ast)
    stu_projection = _projection_sql(student_ast)
    if std_projection and stu_projection and std_projection != stu_projection:
        builder.add(
            "select-basic",
            "logical",
            "E_AST",
            "projection_mismatch",
            "SELECT 投影表达式与标准答案不一致，可能漏选、错选或改写了目标列。",
            0.76,
        )

    std_set_kp = _set_operator_kp(standard_ast)
    stu_set_kp = _set_operator_kp(student_ast)
    if std_set_kp and _node_sql(standard_ast) != _node_sql(student_ast):
        builder.add(
            std_set_kp,
            "logical",
            "E_AST",
            f"set_operator_mismatch:{std_set_kp}_vs_{stu_set_kp or 'none'}",
            "集合操作结构或去重语义与标准答案不一致，优先检查 UNION/UNION ALL/INTERSECT/EXCEPT。",
            0.82,
        )

    if _window_sqls(standard_ast) and _window_sqls(standard_ast) != _window_sqls(student_ast):
        builder.add(
            "window-row-number",
            "logical",
            "E_AST",
            "window_over_mismatch",
            "窗口函数 OVER 子句与标准答案不一致，优先检查 PARTITION BY 或 ORDER BY。",
            0.82,
        )

    if (bool(_nodes(standard_ast, exp.Subquery)) or bool(_nodes(standard_ast, exp.Exists))) and _node_sql(standard_ast) != _node_sql(student_ast):
        if _has_correlated_subquery(standard_ast):
            builder.add(
                "subquery-correlated",
                "logical",
                "E_AST",
                "correlated_subquery_mismatch",
                "相关子查询结构与标准答案不一致。",
                0.80,
            )
        else:
            builder.add(
                "subquery-scalar",
                "logical",
                "E_AST",
                "subquery_mismatch",
                "子查询结构与标准答案不一致。",
                0.80,
            )

    if _has_recursive_cte(standard_ast) and _node_sql(standard_ast) != _node_sql(student_ast):
        builder.add(
            "cte-recursive",
            "logical",
            "E_AST",
            "recursive_cte_boundary_mismatch",
            "递归 CTE 的终止边界或递推条件与标准答案不一致。",
            0.84,
        )

    if mutation_detail:
        for test in mutation_detail.get("tests") or []:
            kp_id = test.get("knowledge_point_id")
            if not kp_id:
                continue
            clause = test.get("clause") or kp_id
            action = test.get("action") or "mutation"
            # If replacing this specific student clause with the standard one passes the sandbox checks
            if test.get("fixed_by_replacement"):
                builder.add(
                    kp_id,
                    "logical",
                    "E_MUT",
                    f"replacement_fixed:{clause}",
                    f"把学生 SQL 的 {clause} 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
                    0.88,
                )
            elif test.get("replacement_exec_ok") and test.get("replacement_equivalent") is False:
                builder.add(
                    kp_id,
                    "logical",
                    "E_MUT",
                    f"replacement_not_enough:{clause}",
                    f"替换 {clause} 后仍未通过动态沙盒，说明该子句相关但可能不是唯一错因。",
                    0.56,
                )
            if test.get("removed_student_clause_equivalent"):
                builder.add(
                    kp_id,
                    "complication",
                    "E_MUT",
                    f"remove_keeps_correct:{clause}",
                    f"移除学生 SQL 中的 {clause} 后结果仍正确，说明该结构可能是不必要复杂化。",
                    0.48,
                )

    # Perform structural query component value diffs
    clause_map = [
        ("where", exp.Where, "WHERE 子句与标准答案不一致，替换该子句是优先隔离方向"),
        ("group-by", exp.Group, "GROUP BY 子句与标准答案不一致，分组粒度是优先隔离方向"),
        ("having", exp.Having, "HAVING 子句与标准答案不一致，分组后筛选是优先隔离方向"),
        ("order-by", exp.Order, "ORDER BY 子句与标准答案不一致，排序字段或方向是优先隔离方向"),
        ("limit", exp.Limit, "LIMIT/OFFSET 与标准答案不一致，返回范围是优先隔离方向"),
    ]
    for kp_id, node_type, detail in clause_map:
        std_node = _first(standard_ast, node_type)
        stu_node = _first(student_ast, node_type)
        if std_node is not None and stu_node is not None and _node_sql(std_node) != _node_sql(stu_node):
            weight = 0.62
            if kp_id == "group-by" and _has_only_full_group_by_risk(student_ast):
                weight = 0.9
                detail = "GROUP BY 字段与 SELECT 非聚合列不一致，在严格 SQL 模式下会报错，宽松模式下也会造成分组粒度错位"
            builder.add(kp_id, "clause_mismatch", "E_MUT", f"replace:{kp_id}", detail, weight)

    if _agg_names(standard_ast) and _agg_names(standard_ast) != _agg_names(student_ast):
        builder.add("agg-count", "clause_mismatch", "E_MUT", "replace:aggregation", "聚合函数集合与标准答案不一致，替换聚合表达式是优先隔离方向", 0.64)

    if _has_join_on(standard_ast) and _has_join_on(student_ast):
        std_join_sql = [_node_sql(node.args.get("on")) for node in _nodes(standard_ast, exp.Join)]
        stu_join_sql = [_node_sql(node.args.get("on")) for node in _nodes(student_ast, exp.Join)]
        if std_join_sql != stu_join_sql:
            builder.add("join-on", "clause_mismatch", "E_MUT", "replace:join_on", "JOIN ON 条件与标准答案不一致，连接谓词是优先隔离方向", 0.86)

    std_cases = [_node_sql(node) for node in _nodes(standard_ast, exp.Case)]
    stu_cases = [_node_sql(node) for node in _nodes(student_ast, exp.Case)]
    if std_cases and stu_cases and std_cases != stu_cases:
        builder.add("case", "clause_mismatch", "E_MUT", "replace:case", "CASE 条件表达式与标准答案不一致，优先检查 WHEN 条件、NULL 判断或 ELSE 分支", 0.68)


def _add_ast_diff_evidence(
    builder: _AttributionBuilder,
    ast_diffs: list[dict[str, Any]],
    student_ast: exp.Expression | None,
) -> None:
    """Infers error causes directly from the ASTDiffNode Diff Graph.

    Each diff node carries clause-level structural information computed by
    ``parseval_data_generator.extract_ast_diffs()``.  This function maps those
    diffs to knowledge-point evidence so the attribution pipeline consumes the
    same Diff Graph that drives data generation and mutation testing.
    """
    for diff in ast_diffs:
        kp_id = diff.get("knowledge_point_id") or "select-basic"
        clause = diff.get("clause") or ""
        diff_type = diff.get("diff_type") or ""
        compared_sql = f"{diff.get('standard_sql') or ''} {diff.get('student_sql') or ''}".upper()
        if clause == "SELECT" and any(name in compared_sql for name in ("NULLIF(", "COALESCE(")):
            kp_id = "null-handling"
        elif clause == "SELECT" and re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", compared_sql):
            kp_id = "agg-count"

        if "missing" in diff_type:
            builder.add(
                kp_id, "missing_clause", "E_AST",
                f"diff_missing:{clause}",
                f"Diff Graph: 学生 SQL 缺少 {clause} 结构",
                0.88,
            )
        elif "changed" in diff_type or "mismatch" in diff_type:
            weight = 0.72
            if clause == "JOIN ON":
                weight = 0.86
            elif clause in ("GROUP BY",) and student_ast and _has_only_full_group_by_risk(student_ast):
                weight = 0.90
            builder.add(
                kp_id, "clause_mismatch", "E_AST",
                f"diff_changed:{clause}",
                f"Diff Graph: {clause} 结构与标准答案不一致",
                weight,
            )
        elif "added" in diff_type:
            builder.add(
                kp_id, "complication", "E_AST",
                f"diff_added:{clause}",
                f"Diff Graph: 学生 SQL 额外添加了 {clause} 结构",
                0.55,
            )
        elif "type" in diff_type:
            builder.add(
                kp_id, "wrong_type", "E_AST",
                f"diff_type:{clause}",
                f"Diff Graph: {clause} 类型与标准答案不一致",
                0.82,
            )


def evidence_weights_from_observation(
    *,
    student_sql: str,
    answer_sql: str,
    is_correct: bool,
    error_message: str | None = None,
    judge_detail: dict[str, Any] | None = None,
    question_context: dict[str, Any] | None = None,
    mutation_detail: dict[str, Any] | None = None,
    ast_diffs: list[dict[str, Any]] | None = None,
) -> AttributionResult:
    """
    Fuses multiple diagnostic evidence elements into ranked Knowledge Point attributions.

    Args:
        student_sql (str): SQL statement submitted by the student.
        answer_sql (str): Reference standard solution SQL.
        is_correct (bool): Flag indicating execution correctness status.
        error_message (str | None, optional): Plain execution error string if failed.
        judge_detail (dict[str, Any] | None, optional): Standardized judge output logs.
        question_context (dict[str, Any] | None, optional): Metadata tags for the question.
        mutation_detail (dict[str, Any] | None, optional): Detail metrics from mutation runs.
        ast_diffs (list[dict] | None, optional): Diff Graph from extract_ast_diffs().

    Returns:
        AttributionResult: Packed diagnostic result structure.
    """
    standard_ast = _parse(answer_sql)
    student_ast = _parse(student_sql)
    std_features = _features(standard_ast)
    stu_features = _features(student_ast)
    judge_features = _judge_features(judge_detail, error_message)
    intended_kp = _kp_profile(role="intended", ast=standard_ast, features=std_features, question_context=question_context)
    observed_kp = _kp_profile(role="observed", ast=student_ast, features=stu_features)

    # Structure sensory telemetry dict
    # Exclude _ir (SQLStructureIR instance) from observation — it is an internal
    # Python object that must not appear in JSON-serialised API responses.
    observation = {
        "E_AST": {
            "student_parse_ok": stu_features["parse_ok"],
            "standard_parse_ok": std_features["parse_ok"],
            "student_features": {k: v for k, v in stu_features.items() if k != "_ir"},
            "standard_features": {k: v for k, v in std_features.items() if k != "_ir"},
            "intended_kp": intended_kp,
            "observed_kp": observed_kp,
            "ast_diffs": ast_diffs or [],
        },
        "E_data": judge_features,
        "E_MUT": {
            "enabled": bool(standard_ast and student_ast) and (not is_correct or bool(mutation_detail)),
            "mutation_tests": (mutation_detail or {}).get("tests") if mutation_detail else [],
            "mutation_summary": (mutation_detail or {}).get("summary") if mutation_detail else None,
        },
        "ablation_candidates": _ablation_candidates(intended_kp, observed_kp, judge_features, mutation_detail),
    }

    builder = _AttributionBuilder()
    misalignments: list[dict[str, Any]] = []
    
    # Check if student SQL query is syntactically invalid
    if student_ast is None:
        builder.add("select-basic", "syntax_fatal", "E_AST", "parse_error", "学生 SQL 无法解析为合法查询语法，先归因到 SELECT 基础结构", 1.0)
    else:
        misalignments = _add_misalignment_evidence(builder, intended_kp, observed_kp, judge_features, is_correct)
        if not is_correct:
            _add_ast_evidence(builder, std_features, stu_features)
            if ast_diffs:
                _add_ast_diff_evidence(builder, ast_diffs, student_ast)
            _add_data_evidence(builder, judge_features, std_features, stu_features)
            _add_mutation_evidence(builder, standard_ast, student_ast, mutation_detail)
            if std_features.get("outer_distinct_likely_redundant") and not stu_features.get("has_outer_distinct"):
                builder.cap(
                    "distinct",
                    0.24,
                    "标准 SQL 的顶层 DISTINCT 在当前 GROUP BY 语义下可能冗余，因此不作为主要错因。",
                )

    attributions = builder.build()
    
    # Formulate bundle input context for upstream LLM arbitration
    llm_input = {
        "question": (question_context or {}).get("q"),
        "question_context": question_context or {},
        "student_sql": student_sql,
        "answer_sql": answer_sql,
        "evidence": observation,
        "misalignment_comparison": misalignments,
        "ablation_candidates": observation["ablation_candidates"],
        "candidate_kps": [item.to_dict() for item in attributions],
        "instructions": {
            "intended_kp_source": "question tags + standard SQL AST",
            "observed_kp_source": "student SQL AST",
            "decision_categories": ["Lacking", "Confusion", "Logical", "Generality", "Complication"],
        },
    }
    return AttributionResult(observation=observation, attributions=attributions, llm_arbitration_input=llm_input)


__all__ = [
    "AttributionResult",
    "EvidenceItem",
    "KPAttribution",
    "evidence_weights_from_observation",
]
