"""Derive a bounded, explainable student-error layer for every Phase 1 family.

Requirement 4 of the Phase 1 acceptance plan needs one interpretable
single-point mutation per standard query plus an equivalence-preserving control
rewrite, and requirement 5 needs those pairs to be counted per question family
rather than per generated string.  This builder therefore reads one corpus
snapshot partition and emits at most two evaluation rows per family: one
``not_equivalent`` mutation and one ``equivalent`` rewrite.  Both rows keep the
family id of their source record, so family-level independence accounting stays
exact and the family denominator never grows.

Every mutation comes from an explicit operator in the fifteen teaching error
families, is applied to the parsed AST instead of to raw text, and is re-parsed
and compared before it is accepted.  Operator assignment is a deterministic
global balancing pass, so rare operators (recursive step, window frame, CASE
branch) are not starved by common ones.

Nothing here consults an oracle verdict or an evaluation outcome, so the same
command may be run over train, public, and hidden partitions.  Hidden runs
still never emit SQL into a report: only counts and digests are summarised.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterator

import sqlglot
from sqlglot import exp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1

# sqlglot dialect names for the dialects the corpus records use.
DIALECT_READ = {
    "generic": None,
    "standard": None,
    "sqlite": "sqlite",
    "mysql": "mysql",
    "postgres": "postgres",
    "tsql": "tsql",
    "oracle": "oracle",
}

MutationResult = tuple[str, str, str]

def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pick_index(family_id: str, salt: str, size: int) -> int:
    """Deterministic index in ``range(size)`` derived from the family id."""
    if size <= 0:
        return 0
    digest = _sha256_text(f"{family_id}\0{salt}")
    return int(digest[:16], 16) % size


def _numeric_schema_identifiers(schema_text: str) -> set[str]:
    """Return schema names that need quoting before AST parsing.

    Teaching snapshots legitimately contain WikiSQL headers such as
    ``2006_07`` and ``1st_m``.  Those are valid quoted SQL identifiers but are
    not valid bare tokens for sqlglot's parser.  Restrict the fallback to names
    beginning with a digit; ordinary SQL keywords and literals must never be
    rewritten by a heuristic parser repair.
    """
    names: set[str] = set()
    for table, columns in _schema_columns(schema_text).items():
        if table and table[0].isdigit():
            names.add(table.lower())
        for column in columns:
            if column and column[0].isdigit():
                names.add(column.lower())
    return names


def _quote_numeric_schema_identifiers(sql: str, schema_text: str) -> str:
    """Quote only matching numeric-leading schema identifiers outside literals."""
    names = _numeric_schema_identifiers(schema_text)
    if not names:
        return sql
    output: list[str] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            output.append(char)
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            output.append(char)
            if char == "*" and next_char == "/":
                output.append(next_char)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            output.extend((char, next_char))
            index += 2
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            output.extend((char, next_char))
            index += 2
            block_comment = True
            continue
        if char in "'\"`[":
            quote = "]" if char == "[" else char
            output.append(char)
            index += 1
            continue
        if char.isdigit() and (
            index == 0
            or not (sql[index - 1].isalnum() or sql[index - 1] in "_$")
        ):
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            token = sql[index:end]
            if token.lower() in names:
                output.append('"' + token.replace('"', '""') + '"')
            else:
                output.append(token)
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


# WikiSQL and scraped teaching tables frequently use SQL keywords as column
# headers (``from``, ``for``, ``order``, ``count``...).  sqlglot quite
# correctly rejects those names when they are left bare, but the source SQL is
# still a valid teaching query once the header is quoted.  Keep this set
# intentionally conservative: it is only applied to names that are declared
# in the supplied schema, never to arbitrary words in prose or string values.
_RESERVED_SCHEMA_IDENTIFIERS = {
    "all", "and", "any", "as", "asc", "between", "by", "case", "check",
    "count", "create", "cross", "current", "default", "delete", "desc",
    "distinct", "drop", "else", "end", "except", "exists", "for", "from", "full",
    "group", "having", "in", "inner", "insert", "intersect", "is", "join",
    "like", "limit", "natural", "not", "null", "offset", "on", "only",
    "or", "order", "outer", "over", "partition", "percent", "primary",
    "recursive", "references", "right", "select", "set", "some", "sum",
    "table", "then", "top", "union", "unique", "update", "using", "values",
    "when", "where", "with", "window", "for", "avg", "min", "max", "rank",
    "returning",
}


def _quote_reserved_schema_identifiers(
    sql: str,
    schema_text: str,
    dialect: str | None,
) -> str:
    """Quote schema-declared keyword columns without touching SQL syntax.

    This is a deliberately bounded lexer rather than a regex replacement.  It
    skips strings/comments/quoted identifiers and quotes only a token that is
    both declared in the schema and in an expression-like position.  Clause
    keywords such as ``FROM`` in ``FROM table`` and ``TOP`` in ``TOP 1`` are
    left intact using small look-ahead guards.
    """
    schema = _schema_columns(schema_text)
    names = {
        column.lower()
        for columns in schema.values()
        for column in columns
        if column and column.lower() in _RESERVED_SCHEMA_IDENTIFIERS
    }
    if not names:
        return sql

    tokens: list[tuple[int, int, str]] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            index += 2
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            index += 2
            block_comment = True
            continue
        if char in "'\"`[":
            quote = "]" if char == "[" else char
            index += 1
            continue
        if char.isalnum() or char in "_$":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            tokens.append((index, end, sql[index:end]))
            index = end
            continue
        if not char.isspace():
            # Punctuation is useful for expression-position checks.  Collapse
            # two-character comparisons into one token where possible.
            if char in "<>=!" and next_char == "=":
                tokens.append((index, index + 2, sql[index:index + 2]))
                index += 2
            else:
                tokens.append((index, index + 1, char))
                index += 1
            continue
        index += 1

    expression_predecessors = {
        "select", "where", "and", "or", "on", "by", "as", "(", ",",
        "=", "<", ">", "<=", ">=", "<>", "!=", "+", "-", "*", "/", "%",
        "when", "then", "else", "case", "is", "in", "not", "between",
        "like", "from", "join",
    }
    expression_successors = {
        "=", "<", ">", "<=", ">=", "<>", "!=", ",", ")", "and", "or", "is",
        "in", "like", "between", "asc", "desc", "nulls", "from", "where",
        "group", "order", "having", "limit", "offset", "union", "intersect",
        "except", "then", "else", "end",
    }
    syntax_followers = {
        "from": {"(", "lateral", "select", "only"},
        "for": {"update", "share", "no", "json", "xml"},
        "only": {"("},
        "order": {"by"},
        "group": {"by"},
        "count": {"("},
        "sum": {"("},
        "avg": {"("},
        "min": {"("},
        "max": {"("},
        "rank": {"("},
        "case": {"when"},
        "when": {"then"},
    }

    def lower(value: str | None) -> str:
        return str(value or "").lower()

    replacements: list[tuple[int, int, str]] = []
    for position, (start, end, value) in enumerate(tokens):
        name = value.lower()
        if name not in names:
            continue
        previous = lower(tokens[position - 1][2] if position else None)
        following = lower(tokens[position + 1][2] if position + 1 < len(tokens) else None)
        previous_previous = lower(tokens[position - 2][2] if position > 1 else None)
        if following in syntax_followers.get(name, set()):
            continue
        if name == "top" and (following.isdigit() or following in {"percent", "with"}):
            continue
        if name == "desc" and following in {"", ",", ")", "nulls", "limit", "offset"}:
            continue
        if name == "null" and previous in {"is", "not"} and previous_previous == "is":
            continue
        if previous in expression_predecessors or following in expression_successors:
            quote_char = "`" if dialect == "mysql" else '"'
            replacements.append((start, end, quote_char + value + quote_char))

    if not replacements:
        return sql
    output = sql
    for start, end, replacement in reversed(replacements):
        output = output[:start] + replacement + output[end:]
    return output


def _strip_leading_prose(sql: str) -> str:
    """Recover the SQL suffix from scraped ``description */ SELECT ...`` rows.

    A few teaching pages store the exercise sentence and the answer query in
    the same field.  Only a suffix following an explicit block-comment close
    and beginning with a statement keyword is eligible; ordinary CTEs and
    comments inside a valid query are left untouched.
    """
    marker = sql.rfind("*/")
    if marker < 0:
        return sql
    suffix = sql[marker + 2 :].lstrip()
    if re.match(r"(?is)^(?:SELECT|WITH|VALUES|INSERT|UPDATE|DELETE)\b", suffix):
        return suffix
    return sql


def _fallback_read_dialects(sql: str, dialect: str | None) -> tuple[str | None, ...]:
    """Return bounded parser dialect fallbacks for explicit vendor syntax.

    Some teaching rows are tagged generic even though they use SQL Server's
    ``TOP``/``UNPIVOT`` syntax.  Plain ``TOP n`` and ``TOP n WITH TIES`` are
    parsed with the bounded T-SQL fallback.  The latter is deliberately kept
    as a T-SQL AST feature; it must not be silently reduced to ``LIMIT`` (the
    native executor/coverage layer will classify unsupported engines as an
    ``ENGINE_GAP`` instead).
    """
    dialects: list[str | None] = [dialect]
    if dialect is None and re.search(r"(?is)\bUNPIVOT\b", sql):
        dialects.append("tsql")
    if dialect is None and re.search(
        r"(?is)\bTOP\s+(?:\d+|\d+(?:\.\d+)?)\s+WITH\s+TIES\b",
        sql,
    ):
        dialects.append("tsql")
    if (
        dialect is None
        and re.search(r"(?is)\bTOP\s+(?:\d+|\d+(?:\.\d+)?)\b", sql)
        and not re.search(r"(?is)\bTOP\s+[^\s]+\s+WITH\s+TIES\b", sql)
    ):
        dialects.append("tsql")
    return tuple(dict.fromkeys(dialects))


def _parse(
    sql: str,
    dialect: str | None,
    schema_text: str = "",
) -> exp.Expression | None:
    # A bare numeric-leading WikiSQL header is accepted by sqlglot as a
    # numeric literal with an alias (``20_questions`` -> ``20 AS
    # _questions``), so the raw parse can succeed while changing the query's
    # meaning.  Prefer the schema-owned repair before trying the raw spelling;
    # keep the raw candidate as a bounded fallback for dialect syntax that the
    # repair does not affect.
    forms = [sql]
    suffix = _strip_leading_prose(sql)
    if suffix not in forms:
        forms.append(suffix)
    read_dialects = _fallback_read_dialects(sql, dialect)
    for form in forms:
        repaired = _quote_numeric_schema_identifiers(form, schema_text)
        candidates = [repaired]
        reserved_repaired = _quote_reserved_schema_identifiers(
            repaired,
            schema_text,
            dialect,
        )
        if reserved_repaired not in candidates:
            candidates.append(reserved_repaired)
        if form not in candidates:
            candidates.append(form)
        for candidate in candidates:
            for read_dialect in read_dialects:
                try:
                    return sqlglot.parse_one(candidate, read=read_dialect)
                except Exception:  # noqa: BLE001 - try the next bounded repair.
                    continue
    return None


def _render(
    tree: exp.Expression,
    dialect: str | None,
    schema_text: str = "",
) -> str | None:
    try:
        rendered = tree.sql(dialect=dialect)
        # sqlglot's SQLite generator intentionally drops named columns on a
        # CTE alias (``nums(n)`` becomes ``nums``), even though SQLite itself
        # accepts and requires that column list for recursive CTEs whose anchor
        # expression has no name.  Keep the dialect-specific renderer for
        # ordinary statements, but use sqlglot's generic SQL renderer when a
        # named CTE alias would otherwise be lost.  Generic output remains
        # valid SQLite syntax and preserves the source AST exactly.
        if dialect == "sqlite" and any(
            isinstance(cte.args.get("alias"), exp.TableAlias)
            and bool(cte.args["alias"].args.get("columns"))
            for cte in tree.find_all(exp.CTE)
        ):
            generic_rendered = tree.sql(dialect=None)
            if generic_rendered.strip():
                rendered = generic_rendered
    except Exception:  # noqa: BLE001 - a mutation that cannot be printed is rejected.
        return None
    rendered = rendered.strip()
    if not rendered:
        return None
    # A mutation is only accepted when the printed form parses back cleanly,
    # so no downstream stage ever receives syntactically dead SQL.
    if _parse(rendered, dialect, schema_text) is None:
        return None
    return rendered


def _schema_columns(schema_text: str) -> dict[str, list[str]]:
    """Map lowercase table name -> declared column names.

    The corpus stores schemas as ``table(col, col, ...);`` lines, sometimes
    prefixed by ``--`` comments.  Column-level mutations need this map to pick a
    replacement column that actually exists; when the map is unusable the
    affected operators simply report themselves inapplicable.
    """
    tables: dict[str, list[str]] = {}
    for statement in str(schema_text or "").split(";"):
        chunk = "\n".join(
            line for line in statement.splitlines() if not line.strip().startswith("--")
        ).strip()
        if not chunk or "(" not in chunk or not chunk.endswith(")"):
            continue
        head, _, tail = chunk.partition("(")
        name = head.strip().strip('"`[]').lower()
        if not name:
            continue
        columns = []
        for raw in tail[:-1].split(","):
            column = raw.strip().split()[0].strip('"`[]') if raw.strip() else ""
            if column:
                columns.append(column)
        if columns:
            tables[name] = columns
    return tables


def _schema_non_nullable_columns(schema_text: str) -> dict[str, set[str]]:
    """Return columns whose compact-schema declaration guarantees non-NULL.

    COUNT(*) -> COUNT(column) is only a useful counterexample when ``column``
    may be NULL.  Compact schemas commonly encode this as an inline
    ``PRIMARY KEY`` or ``NOT NULL`` constraint.  The parser deliberately stays
    conservative: table-level constraints and unknown types do not make a
    column non-null unless the column declaration itself says so.
    """
    result: dict[str, set[str]] = {}
    for statement in str(schema_text or "").split(";"):
        chunk = "\n".join(
            line for line in statement.splitlines() if not line.strip().startswith("--")
        ).strip()
        if not chunk or "(" not in chunk or not chunk.endswith(")"):
            continue
        head, _, tail = chunk.partition("(")
        table_name = head.strip().strip('"`[]').lower()
        if not table_name:
            continue
        non_nullable: set[str] = set()
        for raw in tail[:-1].split(","):
            tokens = raw.strip().split()
            if not tokens:
                continue
            column = tokens[0].strip('"`[]').lower()
            declaration = raw.casefold()
            if "primary key" in declaration or re.search(r"\bnot\s+null\b", declaration):
                non_nullable.add(column)
        if non_nullable:
            result[table_name] = non_nullable
    return result


def _schema_unique_columns(schema_text: str) -> dict[str, set[str]]:
    """Return columns proven unique by compact-schema constraints.

    Only single-column PRIMARY KEY/UNIQUE constraints are recorded.  A
    composite key does not make either component unique by itself, and the
    mutation layer intentionally stays conservative rather than attempting to
    infer functional dependencies from query predicates.
    """
    result: dict[str, set[str]] = {}
    for statement in str(schema_text or "").split(";"):
        chunk = "\n".join(
            line for line in statement.splitlines() if not line.strip().startswith("--")
        ).strip()
        if not chunk or "(" not in chunk or not chunk.endswith(")"):
            continue
        head, _, tail = chunk.partition("(")
        table_name = head.strip().strip('"`[]').lower()
        if not table_name:
            continue
        unique: set[str] = set()
        # Inline declarations, e.g. ``id INT PRIMARY KEY`` or ``email TEXT
        # UNIQUE``.  This deliberately checks the declaration for the column,
        # not merely the table text, to avoid marking every column unique.
        for raw in tail[:-1].split(","):
            tokens = raw.strip().split()
            if not tokens:
                continue
            column = tokens[0].strip('"`[]').lower()
            declaration = raw.casefold()
            if re.search(r"\bprimary\s+key\b", declaration) or re.search(
                r"\bunique\b", declaration
            ):
                # A table-level constraint begins with PRIMARY/UNIQUE and is
                # handled by the regex pass below, rather than treating the
                # keyword itself as a column name.
                if column not in {"primary", "unique", "constraint"}:
                    unique.add(column)

        # Table-level single-column constraints, e.g. ``PRIMARY KEY (id)`` or
        # ``UNIQUE (email)``.  Composite constraints are intentionally ignored.
        for match in re.finditer(
            r"\b(?:primary\s+key|unique(?:\s+key)?)\s*\(([^)]*)\)",
            tail,
            flags=re.IGNORECASE,
        ):
            columns = [
                item.strip().strip('"`[]').lower()
                for item in match.group(1).split(",")
                if item.strip()
            ]
            if len(columns) == 1:
                unique.add(columns[0])
        if unique:
            result[table_name] = unique
    return result


def _select_projects_unique_column(select: exp.Select, ctx: "Context") -> bool:
    """Prove a DISTINCT projection cannot contain duplicate rows.

    The proof is deliberately narrow: one direct table, no JOIN, and one
    projected column declared as a single-column PRIMARY KEY/UNIQUE.  This is
    enough to avoid generating a false teaching mutation while preserving
    fail-closed behaviour for aliases, expressions, joins, and composites.
    """
    tables = [
        table
        for table in select.find_all(exp.Table)
        if table.find_ancestor(exp.Select) is select
    ]
    if len(tables) != 1 or select.args.get("joins"):
        return False
    table = tables[0]
    table_name = str(table.name or "").lower()
    unique = ctx.schema_unique.get(table_name, set())
    if not unique or len(select.expressions) != 1:
        return False
    projection = select.expressions[0]
    if isinstance(projection, exp.Alias):
        projection = projection.this
    if not isinstance(projection, exp.Column):
        return False
    column_name = str(projection.name or "").lower()
    if column_name not in unique:
        return False
    qualifier = str(projection.table or "").lower()
    alias = str(table.alias or "").lower()
    return not qualifier or qualifier in {table_name, alias}


class Context:
    """Read-only side information a few operators need beyond the AST."""

    __slots__ = (
        "schema_columns",
        "schema_non_nullable",
        "schema_unique",
        "alias_tables",
        "referenced",
        "reserved",
    )

    def __init__(self, schema_text: str, tree: exp.Expression) -> None:
        self.schema_columns = _schema_columns(schema_text)
        self.schema_non_nullable = _schema_non_nullable_columns(schema_text)
        self.schema_unique = _schema_unique_columns(schema_text)
        alias_tables: dict[str, str] = {}
        reserved: set[str] = set()
        for table in tree.find_all(exp.Table):
            name = str(table.name or "").lower()
            if not name:
                continue
            alias_tables[name] = name
            reserved.add(name)
            alias = str(table.alias or "").lower()
            if alias:
                alias_tables[alias] = name
                reserved.add(alias)
        referenced: dict[str, list[str]] = defaultdict(list)
        for column in tree.find_all(exp.Column):
            qualifier = str(column.table or "").lower()
            name = str(column.name or "")
            if qualifier and name and name not in referenced[qualifier]:
                referenced[qualifier].append(name)
        self.alias_tables = alias_tables
        self.referenced = dict(referenced)
        self.reserved = reserved

    def sibling_column(self, column: exp.Column, avoid: set[str]) -> str | None:
        """A different column of the same table, for wrong-key mutations.

        Several corpus sources ship a degraded schema in which every table
        repeats the same union of column names, so a name taken straight from
        the schema text may not exist on the table and would make the mutated
        query fail to run instead of returning a different result.  Columns the
        query itself already references with the same qualifier are therefore
        preferred, and table names or aliases are never used as columns.
        """
        qualifier = str(column.table or "").lower()
        table = self.alias_tables.get(qualifier, qualifier)
        lowered_avoid = {value.lower() for value in avoid} | self.reserved
        for candidate in self.referenced.get(qualifier, ()):
            if candidate.lower() not in lowered_avoid:
                return candidate
        candidates = self.schema_columns.get(table) or []
        if not candidates and len(self.schema_columns) == 1:
            candidates = next(iter(self.schema_columns.values()))
        for candidate in candidates:
            if candidate.lower() not in lowered_avoid:
                return candidate
        return None


def _constraint_table_for_column(column: exp.Column, ctx: Context) -> str | None:
    """Resolve a column to one schema table for conservative proofs."""
    name = str(column.name or "").lower()
    if not name:
        return None
    qualifier = str(column.table or "").lower()
    if qualifier:
        table = ctx.alias_tables.get(qualifier, qualifier)
        return table if name in ctx.schema_columns.get(table, ()) else None
    candidates = [
        table
        for table, columns in ctx.schema_columns.items()
        if name in {str(item).lower() for item in columns}
    ]
    return candidates[0] if len(candidates) == 1 else None


def _column_has_constraint(
    column: exp.Expression,
    ctx: Context,
    constraints: dict[str, set[str]],
) -> bool:
    if not isinstance(column, exp.Column):
        return False
    table = _constraint_table_for_column(column, ctx)
    return bool(table and str(column.name or "").lower() in constraints.get(table, set()))


# --- operator 1: > <-> >= (and < <-> <=) -----------------------------------

_STRICTNESS_FLIP: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.GT: exp.GTE,
    exp.GTE: exp.GT,
    exp.LT: exp.LTE,
    exp.LTE: exp.LT,
}


def _op_comparison_boundary(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE)
        if node.this is not None and node.expression is not None
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    replacement = _STRICTNESS_FLIP[type(target)](
        this=target.this.copy(), expression=target.expression.copy()
    )
    label = f"{type(target).__name__.lower()}_to_{type(replacement).__name__.lower()}"
    target.replace(replacement)
    return f"comparison_strictness_{label}"


# --- operator 2: AND <-> OR -----------------------------------------------


def _op_logical_connector(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    ands = list(tree.find_all(exp.And))
    ors = list(tree.find_all(exp.Or))
    if ands:
        target, replacement_cls, label = ands[index % len(ands)], exp.Or, "and_to_or"
    elif ors:
        target, replacement_cls, label = ors[index % len(ors)], exp.And, "or_to_and"
    else:
        return None
    target.replace(
        replacement_cls(this=target.this.copy(), expression=target.expression.copy())
    )
    return f"logical_connector_{label}"


# --- operator 3: INNER <-> LEFT -------------------------------------------


def _op_join_type(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    joins = [
        join
        for join in tree.find_all(exp.Join)
        if join.args.get("on") is not None
        and str(join.args.get("kind") or "").upper() != "CROSS"
    ]
    if not joins:
        return None
    target = joins[index % len(joins)]
    side = str(target.args.get("side") or "").upper()
    if side in {"LEFT", "RIGHT", "FULL"}:
        target.set("side", None)
        target.set("kind", "INNER")
        return f"join_type_{side.lower()}_to_inner"
    target.set("side", "LEFT")
    target.set("kind", None)
    return "join_type_inner_to_left"


# --- operator 4: wrong JOIN column ----------------------------------------


def _op_join_key_column(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    candidates: list[tuple[exp.Column, exp.Column]] = []
    for join in tree.find_all(exp.Join):
        condition = join.args.get("on")
        if condition is None:
            continue
        for equality in condition.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                candidates.append((left, right))
    if not candidates:
        return None
    left, right = candidates[index % len(candidates)]
    replacement = ctx.sibling_column(right, {right.name, left.name})
    if replacement is None:
        return None
    right.set("this", exp.to_identifier(replacement))
    return "join_key_column_changed"


# --- operator 5: drop a GROUP BY column -----------------------------------


def _op_group_by_column(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    groups = [
        group
        for group in tree.find_all(exp.Group)
        if len(group.args.get("expressions") or ()) >= 2
    ]
    if not groups:
        return None
    target = groups[index % len(groups)]
    expressions = list(target.args["expressions"])
    dropped = index % len(expressions)
    del expressions[dropped]
    target.set("expressions", expressions)
    return "group_by_key_dropped"


# --- operator 6: HAVING threshold change ----------------------------------


def _numeric_literals(node: exp.Expression) -> list[exp.Literal]:
    return [
        literal
        for literal in node.find_all(exp.Literal)
        if not literal.args.get("is_string") and str(literal.this or "").lstrip("-").isdigit()
    ]


def _op_having_threshold(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    for having in tree.find_all(exp.Having):
        literals = _numeric_literals(having)
        if not literals:
            continue
        target = literals[index % len(literals)]
        target.set("this", str(int(str(target.this)) + 1))
        return "having_threshold_changed"
    return None


# --- operator 7: DISTINCT removed -----------------------------------------


def _multiplicity_observable(select: exp.Select) -> bool:
    """False when duplicate rows of this SELECT cannot change the answer.

    ``x IN (SELECT DISTINCT c ...)`` and ``EXISTS (SELECT DISTINCT c ...)`` are
    membership tests, so adding or removing DISTINCT inside them is an
    equivalence-preserving rewrite rather than a student error.
    """
    return select.find_ancestor(exp.In, exp.Exists) is None


def _op_distinct_removed(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    selects = [
        select
        for select in tree.find_all(exp.Select)
        if select.args.get("distinct") and _multiplicity_observable(select)
        and not _select_projects_unique_column(select, ctx)
    ]
    if not selects:
        return None
    selects[index % len(selects)].set("distinct", None)
    return "distinct_removed"


# --- operator 8: ORDER BY direction ---------------------------------------


def _statement_ordered(tree: exp.Expression) -> list[exp.Ordered]:
    """ORDER BY items of a statement, excluding ORDER BY inside OVER()."""
    ordered: list[exp.Ordered] = []
    for item in tree.find_all(exp.Ordered):
        parent = item.parent
        if parent is None or not isinstance(parent, exp.Order):
            continue
        if isinstance(parent.parent, exp.Window):
            continue
        ordered.append(item)
    return ordered


def _flip_direction(item: exp.Ordered) -> str:
    descending = bool(item.args.get("desc"))
    item.set("desc", not descending)
    # sqlglot only suppresses the NULLS clause when it matches the parse default
    # of the new direction, so pinning it keeps the printed mutation a single
    # ORDER BY direction change instead of also moving NULLs.
    item.set("nulls_first", descending)
    return "desc_to_asc" if descending else "asc_to_desc"


def _op_order_direction(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    ordered = _statement_ordered(tree)
    if not ordered:
        return None
    label = _flip_direction(ordered[index % len(ordered)])
    return f"order_direction_{label}"


# --- operator 9: LIMIT / OFFSET change ------------------------------------


def _op_limit_offset(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    for node_type, label in ((exp.Limit, "limit_count_changed"), (exp.Offset, "offset_changed")):
        nodes = [
            node
            for node in tree.find_all(node_type)
            if _numeric_literals(node.args.get("expression") or exp.Null())
        ]
        if not nodes:
            continue
        target = nodes[index % len(nodes)]
        literal = _numeric_literals(target.args["expression"])[0]
        literal.set("this", str(int(str(literal.this)) + 1))
        return label
    # An ordered query without LIMIT still supports the classic "forgot the row
    # cap" error; requiring ORDER BY keeps the mutated query deterministic.
    if tree.args.get("order") is not None and tree.args.get("limit") is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(1)))
        return "limit_added"
    return None


# --- operator 10: UNION <-> UNION ALL -------------------------------------

_SET_OPERATIONS = tuple(
    cls
    for cls in (getattr(exp, name, None) for name in ("Union", "Except", "Intersect"))
    if cls is not None
)


def _unwrap_parens(node: exp.Expression | None) -> exp.Expression | None:
    """Return the expression beneath any number of transparent parentheses."""
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _null_clause_signature(node: exp.Expression) -> tuple[str, bool] | None:
    """Return ``(column, is_null_polarity)`` for a simple NULL predicate.

    ``sqlglot`` represents both ``IS NOT NULL`` and ``NOT x IS NULL`` as a
    ``Not(Is(...))`` pair.  Keeping this helper limited to a bare column and
    ``NULL`` avoids attempting to prove arbitrary three-valued-logic formulae.
    """
    node = _unwrap_parens(node)
    positive = True
    if isinstance(node, exp.Not):
        positive = False
        node = _unwrap_parens(node.this)
    if not isinstance(node, exp.Is) or not isinstance(node.expression, exp.Null):
        return None
    if not isinstance(node.this, exp.Column):
        return None
    return node.this.sql(dialect=None).lower(), positive


def _and_conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """Flatten only conjunctions, preserving OR/NOT boundaries."""
    node = _unwrap_parens(node) or node
    if isinstance(node, exp.And):
        return _and_conjuncts(node.this) + _and_conjuncts(node.expression)
    return [node]


def _under_contradictory_null_conjunction(target: exp.Expression) -> bool:
    """Whether a NULL predicate is inside ``x IS NULL AND x IS NOT NULL``.

    In that context changing one spelling to ``= NULL`` cannot create a
    distinguishing row: both sides remain an unsatisfiable predicate.  Such a
    mutation is therefore not a valid ``NOT_EQUIVALENT`` teaching pair and is
    rejected before it enters the public or hidden evaluation layer.
    """
    node: exp.Expression | None = target
    while node is not None:
        if isinstance(node, exp.And):
            polarities: dict[str, set[bool]] = defaultdict(set)
            for conjunct in _and_conjuncts(node):
                signature = _null_clause_signature(conjunct)
                if signature is not None:
                    column, positive = signature
                    polarities[column].add(positive)
            if any(values == {True, False} for values in polarities.values()):
                return True
        node = node.parent
    return False


def _simple_recursive_union_is_unique(node: exp.Expression) -> bool:
    """Prove uniqueness for the bounded monotone recursive sequence pattern.

    ``UNION ALL`` is only observably different from ``UNION`` when a recursive
    member can emit a duplicate row.  For the common teaching form
    ``anchor UNION ALL SELECT n +/- k FROM c WHERE n <|> bound`` with a
    positive integer step, each generated value is strictly monotone and the
    anchor is disjoint from the recursive sequence.  We prove only this narrow
    shape; all other recursive queries remain eligible for the mutation.
    """
    if not isinstance(node, exp.Union) or bool(node.args.get("distinct")):
        return False
    cte = node.find_ancestor(exp.CTE)
    if cte is None or cte.this is not node:
        return False
    with_node = cte.find_ancestor(exp.With)
    if with_node is None or not bool(with_node.args.get("recursive")):
        return False

    left = _unwrap_parens(node.this)
    right = _unwrap_parens(node.expression)
    if not isinstance(left, exp.Select) or not isinstance(right, exp.Select):
        return False
    if len(left.expressions) != 1 or not isinstance(left.expressions[0], exp.Literal):
        return False
    if left.expressions[0].args.get("is_string"):
        return False

    cte_name = str(cte.alias_or_name or "").lower()
    if not cte_name:
        return False
    from_clause = right.args.get("from") or right.args.get("from_")
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    if not isinstance(source, exp.Table) or str(source.name or "").lower() != cte_name:
        return False
    if right.args.get("joins"):
        return False
    if len(right.expressions) != 1:
        return False

    projection = right.expressions[0]
    if not isinstance(projection, (exp.Add, exp.Sub)):
        return False
    recursive_column = projection.this
    step = projection.expression
    if not isinstance(recursive_column, exp.Column) or not isinstance(step, exp.Literal):
        return False
    if step.args.get("is_string"):
        return False
    try:
        step_value = int(str(step.this))
    except (TypeError, ValueError):
        return False
    if step_value <= 0:
        return False

    where = right.args.get("where")
    predicate = _unwrap_parens(where.this) if isinstance(where, exp.Where) else None
    if not isinstance(predicate, (exp.LT, exp.GT)):
        return False
    if not isinstance(predicate.this, exp.Column) or not isinstance(predicate.expression, exp.Literal):
        return False
    if predicate.expression.args.get("is_string"):
        return False
    if predicate.this.sql(dialect=None).lower() != recursive_column.sql(dialect=None).lower():
        return False
    try:
        int(str(predicate.expression.this))
    except (TypeError, ValueError):
        return False

    # Addition must advance toward a strict upper bound; subtraction must
    # advance toward a strict lower bound.  This is the only direction for
    # which the simple shape proves that generated rows cannot repeat.
    return (isinstance(projection, exp.Add) and isinstance(predicate, exp.LT)) or (
        isinstance(projection, exp.Sub) and isinstance(predicate, exp.GT)
    )


def _op_set_all_modifier(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    if not _SET_OPERATIONS:
        return None
    nodes = [
        node
        for node in tree.find_all(*_SET_OPERATIONS)
        if isinstance(node.args.get("distinct"), bool)
        # ``INTERSECT ALL`` and ``EXCEPT ALL`` are not valid SQLite, so mutating
        # them would only produce an ENGINE_GAP instead of a teaching pair.  The
        # required family is UNION versus UNION ALL, which stays covered.
        and (isinstance(node, exp.Union) or not node.args["distinct"])
        and not _simple_recursive_union_is_unique(node)
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    distinct = bool(target.args["distinct"])
    target.set("distinct", not distinct)
    kind = type(target).__name__.lower()
    return f"set_{kind}_{'all_added' if distinct else 'all_removed'}"


# --- operator 11: IN / EXISTS / NOT IN / NOT EXISTS -----------------------


def _op_membership_predicate(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    # Dropping the membership column altogether is the sharpest form of the
    # IN/EXISTS confusion, so it is preferred when a subquery form exists.
    subquery_ins = [
        node
        for node in tree.find_all(exp.In)
        if node.args.get("query") is not None and not isinstance(node.parent, exp.Not)
    ]
    if subquery_ins:
        target = subquery_ins[index % len(subquery_ins)]
        # In.query is normally an exp.Subquery wrapper. Passing that wrapper
        # directly to Exists renders EXISTS((SELECT ...)); unwrap it so the
        # generated pair is valid across SQLite and vendor parsers.
        query = target.args["query"].copy()
        if isinstance(query, exp.Subquery):
            query = query.this.copy()
        target.replace(exp.Exists(this=query))
        return "in_subquery_to_exists"
    nodes = list(tree.find_all(exp.In, exp.Exists))
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    kind = "in" if isinstance(target, exp.In) else "exists"
    if isinstance(target.parent, exp.Not):
        target.parent.replace(target.copy())
        return f"not_{kind}_to_{kind}"
    target.replace(exp.Not(this=target.copy()))
    return f"{kind}_to_not_{kind}"


# --- operator 12: NULL predicate ------------------------------------------


def _op_null_predicate(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.Is)
        if isinstance(node.expression, exp.Null) and node.this is not None
        and not _under_contradictory_null_conjunction(node)
    ]
    # ``IS NULL`` -> ``= NULL`` is an equivalent rewrite on a proven
    # non-nullable column.  Keep the inverse ``IS NOT NULL`` -> ``IS NULL``
    # mutation available, but never emit the false teaching pair.
    if index % 2 == 0:
        nodes = [
            node
            for node in nodes
            if not _column_has_constraint(node.this, ctx, ctx.schema_non_nullable)
        ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    if isinstance(target.parent, exp.Not):
        target.parent.replace(target.copy())
        return "is_not_null_to_is_null"
    if index % 2 == 0:
        # ``= NULL`` is the canonical three-valued-logic mistake and always
        # yields an empty predicate, so it is kept as a first-class operator.
        target.replace(exp.EQ(this=target.this.copy(), expression=exp.Null()))
        return "is_null_to_equals_null"
    target.replace(exp.Not(this=target.copy()))
    return "is_null_to_is_not_null"


# --- operator 13: window PARTITION BY / ORDER BY --------------------------


def _op_window_specification(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    windows = list(tree.find_all(exp.Window))
    if not windows:
        return None
    partitioned = [window for window in windows if window.args.get("partition_by")]
    if partitioned:
        target = partitioned[index % len(partitioned)]
        keys = list(target.args["partition_by"])
        if len(keys) >= 2:
            del keys[index % len(keys)]
            target.set("partition_by", keys)
            return "window_partition_key_dropped"
        target.set("partition_by", None)
        return "window_partition_dropped"
    ordered = [
        item
        for window in windows
        for item in (window.args.get("order").expressions if window.args.get("order") else ())
        if isinstance(item, exp.Ordered)
    ]
    if not ordered:
        return None
    label = _flip_direction(ordered[index % len(ordered)])
    return f"window_order_direction_{label}"


# --- operator 14: CASE branch ---------------------------------------------


def _op_case_branch(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    cases = list(tree.find_all(exp.Case))
    if not cases:
        return None
    multi = [case for case in cases if len(case.args.get("ifs") or ()) >= 2]
    if multi:
        target = multi[index % len(multi)]
        branches = list(target.args["ifs"])
        del branches[index % len(branches)]
        target.set("ifs", branches)
        return "case_when_branch_dropped"
    defaulted = [case for case in cases if case.args.get("default") is not None]
    if not defaulted:
        return None
    defaulted[index % len(defaulted)].set("default", None)
    return "case_else_dropped"


# --- operator 15: recursive CTE step condition ----------------------------


def _op_recursive_step(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    recursive = [node for node in tree.find_all(exp.With) if node.args.get("recursive")]
    if not recursive:
        return None
    target_with = recursive[index % len(recursive)]
    steps = [
        node for node in target_with.find_all(exp.Add, exp.Sub) if _numeric_literals(node)
    ]
    if steps:
        literal = _numeric_literals(steps[index % len(steps)])[0]
        literal.set("this", str(int(str(literal.this)) + 1))
        return "recursive_step_expression_changed"
    guards = [node for node in target_with.find_all(exp.Where) if _numeric_literals(node)]
    if not guards:
        return None
    literal = _numeric_literals(guards[index % len(guards)])[0]
    literal.set("this", str(int(str(literal.this)) + 1))
    return "recursive_termination_changed"


# --- supplementary operators ----------------------------------------------
# The fifteen required families cannot touch a flat ``SELECT c FROM t WHERE a =
# 'v'`` query, which is the single most common shape in the corpus.  These four
# operators are equally single-point and equally explainable; they are counted
# separately so the fifteen-family gate stays honest.

_AGGREGATE_FLIP: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.Min: exp.Max,
    exp.Max: exp.Min,
    exp.Sum: exp.Avg,
    exp.Avg: exp.Sum,
}


def _op_aggregate_function(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    nodes = [
        node
        for node in tree.find_all(*_AGGREGATE_FLIP)
        if node.this is not None and type(node) in _AGGREGATE_FLIP
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    replacement = _AGGREGATE_FLIP[type(target)]
    label = f"{type(target).__name__.lower()}_to_{replacement.__name__.lower()}"
    target.replace(replacement(this=target.this.copy()))
    return f"aggregate_function_{label}"


def _op_count_star_to_count_column(
    tree: exp.Expression,
    index: int,
    ctx: Context,
) -> str | None:
    """Turn COUNT(*) into COUNT(column), exposing NULL-row sensitivity."""
    nodes = [
        node
        for node in tree.find_all(exp.Count)
        if isinstance(node.this, exp.Star) and not node.args.get("expressions")
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    select = target.find_ancestor(exp.Select)
    if select is None:
        return None
    table = select.find(exp.Table)
    column: str | None = None
    qualifier: str | None = None
    if table is not None:
        table_name = str(table.name or "").lower()
        candidates = ctx.schema_columns.get(table_name) or []
        guaranteed_non_null = ctx.schema_non_nullable.get(table_name, set())
        # A primary-key/NOT NULL column makes COUNT(*) and COUNT(column)
        # semantically identical.  Do not emit a mutation that can only
        # produce an equivalent pair; leave the family to another applicable
        # operator or to an explicit UNDECIDED boundary.
        candidates = [
            candidate
            for candidate in candidates
            if candidate.casefold() not in guaranteed_non_null
        ]
        if candidates:
            column = candidates[index % len(candidates)]
            qualifier = str(table.alias or table.name or "")
    if column is None:
        referenced = [
            item.name
            for item in select.find_all(exp.Column)
            if item.name and not isinstance(item, exp.Star)
        ]
        if not referenced:
            return None
        column = referenced[index % len(referenced)]
    replacement = exp.Column(
        this=exp.to_identifier(column),
        table=exp.to_identifier(qualifier) if qualifier else None,
    )
    target.set("this", replacement)
    return "count_star_to_count_column"


def _op_count_column_to_count_star(
    tree: exp.Expression,
    index: int,
    ctx: Context,
) -> str | None:
    """Turn COUNT(column) into COUNT(*), exposing NULL-row sensitivity.

    COUNT(column) ignores NULL values while COUNT(*) counts rows.  This is a
    bounded, single-point teaching mutation and is the inverse of the existing
    supplementary COUNT(*) -> COUNT(column) operator.  DISTINCT inputs are
    deliberately excluded because they have different multiplicity semantics.
    """
    nodes = [
        node
        for node in tree.find_all(exp.Count)
        if isinstance(node.this, exp.Column)
        and not _column_has_constraint(node.this, ctx, ctx.schema_non_nullable)
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.set("this", exp.Star())
    return "count_column_to_count_star"


def _op_aggregate_distinct_removed(
    tree: exp.Expression,
    index: int,
    ctx: Context,
) -> str | None:
    """Remove DISTINCT from duplicate-sensitive aggregate input."""
    nodes = []
    for node in tree.find_all(exp.Count, exp.Sum, exp.Avg):
        distinct = node.this
        if not isinstance(distinct, exp.Distinct):
            continue
        expressions = list(distinct.args.get("expressions") or ())
        if not expressions:
            continue
        # DISTINCT is redundant for an aggregate over a single-column UNIQUE
        # or PRIMARY KEY.  Removing it would create an equivalent pair, so
        # prove only this narrow direct-column case and otherwise stay fail
        # closed.
        if len(expressions) == 1 and _column_has_constraint(
            expressions[0], ctx, ctx.schema_unique
        ):
            continue
        nodes.append(node)
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    distinct = target.this
    expressions = list(distinct.args.get("expressions") or ())
    target.set("this", expressions[0].copy())
    return "aggregate_distinct_removed"


def _op_where_predicate(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    clauses = [clause for clause in tree.find_all(exp.Where) if clause.this is not None]
    if not clauses:
        return None
    target = clauses[index % len(clauses)]
    predicate = target.this
    if isinstance(predicate, exp.And):
        kept = predicate.this if index % 2 else predicate.expression
        predicate.replace(kept.copy())
        return "where_conjunct_dropped"
    parent = target.parent
    if parent is None or parent.args.get("where") is not target:
        return None
    parent.set("where", None)
    return "where_clause_dropped"


def _op_equality_predicate(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.EQ)
        if node.this is not None
        and node.expression is not None
        and node.find_ancestor(exp.Join) is None
        and not isinstance(node.expression, exp.Null)
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.replace(exp.NEQ(this=target.this.copy(), expression=target.expression.copy()))
    return "equality_to_inequality"


def _op_projection_distinct(tree: exp.Expression, index: int, ctx: Context) -> str | None:
    selects = [
        select
        for select in tree.find_all(exp.Select)
        if not select.args.get("distinct")
        and select.args.get("group") is None
        and _multiplicity_observable(select)
        and not any(True for _ in select.find_all(exp.AggFunc))
        and not _select_projects_unique_column(select, ctx)
    ]
    if not selects:
        return None
    selects[index % len(selects)].set("distinct", exp.Distinct())
    return "distinct_added"


# The fifteen teaching error families of requirement 4, declared rarest first.
# Declaration order is the tie-break of the balancing pass, so a family that can
# only host a rare operator is never spent on a common one.
REQUIRED_OPERATORS: tuple[tuple[str, Callable[[exp.Expression, int, Context], str | None]], ...] = (
    ("recursive_step", _op_recursive_step),
    ("case_branch", _op_case_branch),
    ("window_specification", _op_window_specification),
    ("set_all_modifier", _op_set_all_modifier),
    ("membership_predicate", _op_membership_predicate),
    ("null_predicate", _op_null_predicate),
    ("limit_offset", _op_limit_offset),
    ("distinct_removed", _op_distinct_removed),
    ("order_direction", _op_order_direction),
    ("having_threshold", _op_having_threshold),
    ("group_by_key", _op_group_by_column),
    ("join_key_column", _op_join_key_column),
    ("join_type", _op_join_type),
    ("logical_connector", _op_logical_connector),
    ("comparison_strictness", _op_comparison_boundary),
)

SUPPLEMENTARY_OPERATORS: tuple[tuple[str, Callable[[exp.Expression, int, Context], str | None]], ...] = (
    ("aggregate_function", _op_aggregate_function),
    ("count_star_to_count_column", _op_count_star_to_count_column),
    ("count_column_to_count_star", _op_count_column_to_count_star),
    ("aggregate_distinct_removed", _op_aggregate_distinct_removed),
    ("equality_predicate", _op_equality_predicate),
    ("where_predicate", _op_where_predicate),
    ("projection_distinct", _op_projection_distinct),
)

MUTATION_OPERATORS = REQUIRED_OPERATORS + SUPPLEMENTARY_OPERATORS
REQUIRED_FAMILY_NAMES = tuple(name for name, _ in REQUIRED_OPERATORS)

MUTATION_FAMILY_ORDER = {name: rank for rank, (name, _) in enumerate(MUTATION_OPERATORS)}
MUTATION_OPERATOR_BY_NAME = dict(MUTATION_OPERATORS)


def apply_mutation(
    sql: str,
    family: str,
    *,
    dialect: str | None,
    index: int,
    schema_text: str,
) -> MutationResult | None:
    """Apply one error-family operator.

    Returns ``(normalised_gold_sql, mutated_sql, operator)``.  The gold side is
    re-printed from its own AST so the emitted pair differs by the mutation
    alone: without that, sqlglot's own formatting (``count(*)`` -> ``COUNT(*)``,
    ``e`` -> ``AS e``) would show up as extra AST diffs and break the exact
    diff-to-obligation binding the chain audit requires.
    """
    tree = _parse(sql, dialect, schema_text)
    if tree is None:
        return None
    operator = MUTATION_OPERATOR_BY_NAME.get(family)
    if operator is None:
        return None
    working = tree.copy()
    try:
        label = operator(working, index, Context(schema_text, tree))
    except Exception:  # noqa: BLE001 - an operator that trips is inapplicable.
        return None
    if not label:
        return None
    rendered = _render(working, dialect, schema_text)
    if rendered is None:
        return None
    baseline = _render(tree, dialect, schema_text)
    if baseline is None or rendered == baseline:
        return None
    return baseline, rendered, label


# --- equivalence-preserving control rewrites ------------------------------
# Every tactic below is equivalence-preserving under three-valued logic *and*
# bag semantics, so a control row that the judge calls NOT_EQUIVALENT is a real
# defect rather than generator noise.  Rewrites that only look safe (for
# example ``NOT (a = b)`` -> ``a <> b``, which changes NULL handling) are
# deliberately excluded.


def _eq_connector_commutation(tree: exp.Expression, index: int) -> str | None:
    nodes = list(tree.find_all(exp.And, exp.Or))
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.replace(
        type(target)(this=target.expression.copy(), expression=target.this.copy())
    )
    return "connector_operands_commuted"


def _eq_between_expanded(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.Between)
        if not isinstance(node.parent, exp.Not)
        and node.args.get("low") is not None
        and node.args.get("high") is not None
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.replace(
        exp.Paren(
            this=exp.And(
                this=exp.GTE(this=target.this.copy(), expression=target.args["low"].copy()),
                expression=exp.LTE(
                    this=target.this.copy(), expression=target.args["high"].copy()
                ),
            )
        )
    )
    return "between_expanded_to_range"


def _eq_in_list_expanded(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.In)
        if (node.args.get("expressions") or ()) and not isinstance(node.parent, exp.Not)
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    chain: exp.Expression | None = None
    for value in target.args["expressions"]:
        equality = exp.EQ(this=target.this.copy(), expression=value.copy())
        chain = equality if chain is None else exp.Or(this=chain, expression=equality)
    if chain is None:
        return None
    target.replace(exp.Paren(this=chain))
    return "in_list_expanded_to_or_chain"


_MIRROR_COMPARISON: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
}


def _eq_comparison_mirrored(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.GT, exp.LT, exp.GTE, exp.LTE)
        if node.this is not None and node.expression is not None
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.replace(
        _MIRROR_COMPARISON[type(target)](
            this=target.expression.copy(), expression=target.this.copy()
        )
    )
    return "comparison_operands_mirrored"


def _eq_explicit_ascending(tree: exp.Expression, index: int) -> str | None:
    nodes = [item for item in _statement_ordered(tree) if not item.args.get("desc")]
    if not nodes:
        return None
    nodes[index % len(nodes)].set("desc", False)
    return "order_by_ascending_made_explicit"


def _eq_inner_keyword_explicit(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        join
        for join in tree.find_all(exp.Join)
        if join.args.get("on") is not None
        and not join.args.get("side")
        and not join.args.get("kind")
    ]
    if not nodes:
        return None
    nodes[index % len(nodes)].set("kind", "INNER")
    return "inner_join_keyword_made_explicit"


def _eq_predicate_parenthesised(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        clause
        for clause in tree.find_all(exp.Where, exp.Having)
        if clause.this is not None and not isinstance(clause.this, exp.Paren)
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.set("this", exp.Paren(this=target.this.copy()))
    return "predicate_parenthesised"


def _eq_redundant_true_predicate(tree: exp.Expression, index: int) -> str | None:
    """Add a tautological predicate without changing SQL bag semantics."""
    selects = list(tree.find_all(exp.Select))
    if not selects:
        return None
    target = selects[index % len(selects)]
    tautology = exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1))
    existing = target.args.get("where")
    if isinstance(existing, exp.Where) and existing.this is not None:
        target.set(
            "where",
            exp.Where(this=exp.And(this=existing.this.copy(), expression=tautology)),
        )
    else:
        target.set("where", exp.Where(this=tautology))
    return "redundant_true_predicate"


def _eq_equality_to_singleton_in(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.EQ)
        if node.this is not None
        and node.expression is not None
        and node.find_ancestor(exp.Join) is None
        and not isinstance(node.expression, exp.Null)
        and isinstance(node.expression, (exp.Literal, exp.Boolean))
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    target.replace(
        exp.In(this=target.this.copy(), expressions=[target.expression.copy()])
    )
    return "equality_rewritten_as_singleton_in"


def _eq_count_star_to_count_one(tree: exp.Expression, index: int) -> str | None:
    nodes = [
        node
        for node in tree.find_all(exp.Count)
        if isinstance(node.this, exp.Star) and not node.args.get("expressions")
    ]
    if not nodes:
        return None
    nodes[index % len(nodes)].set("this", exp.Literal.number(1))
    return "count_star_rewritten_as_count_one"


def _eq_count_column_to_count_case(tree: exp.Expression, index: int) -> str | None:
    """Rewrite COUNT(column) to an equivalent non-NULL CASE count."""
    nodes = [
        node
        for node in tree.find_all(exp.Count)
        if isinstance(node.this, exp.Column)
    ]
    if not nodes:
        return None
    target = nodes[index % len(nodes)]
    column = target.this.copy()
    condition = exp.Not(this=exp.Is(this=column.copy(), expression=exp.Null()))
    target.set(
        "this",
        exp.Case(ifs=[exp.If(this=condition, true=exp.Literal.number(1))]),
    )
    return "count_column_rewritten_as_count_case"


EQUIVALENCE_TACTICS: tuple[tuple[str, Callable[[exp.Expression, int], str | None]], ...] = (
    ("between_expanded", _eq_between_expanded),
    ("in_list_expanded", _eq_in_list_expanded),
    ("comparison_mirrored", _eq_comparison_mirrored),
    ("equality_to_singleton_in", _eq_equality_to_singleton_in),
    ("count_star_to_count_one", _eq_count_star_to_count_one),
    ("count_column_to_count_case", _eq_count_column_to_count_case),
    ("connector_commuted", _eq_connector_commutation),
    ("explicit_ascending", _eq_explicit_ascending),
    ("inner_keyword_explicit", _eq_inner_keyword_explicit),
    ("predicate_parenthesised", _eq_predicate_parenthesised),
    ("redundant_true_predicate", _eq_redundant_true_predicate),
)


def apply_equivalence(
    sql: str,
    *,
    dialect: str | None,
    index: int,
    schema_text: str = "",
) -> MutationResult | None:
    """Apply the first applicable equivalence tactic; structural ones first."""
    tree = _parse(sql, dialect, schema_text)
    if tree is None:
        return None
    baseline = _render(tree, dialect, schema_text)
    if baseline is None:
        return None
    for _, tactic in EQUIVALENCE_TACTICS:
        working = tree.copy()
        try:
            label = tactic(working, index)
        except Exception:  # noqa: BLE001 - an unusable tactic falls through.
            continue
        if not label:
            continue
        rendered = _render(working, dialect, schema_text)
        if rendered is None or rendered == baseline:
            continue
        return baseline, rendered, label
    return None


def _iter_records(paths: list[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record


def _dialect_of(record: dict[str, Any]) -> str | None:
    name = str(record.get("dialect") or "generic").strip().lower()
    return DIALECT_READ.get(name, None)


def _usable(record: dict[str, Any]) -> bool:
    sql = record.get("sql")
    family_id = record.get("family_id")
    return isinstance(sql, str) and bool(sql.strip()) and isinstance(family_id, str) and bool(family_id)


def _applicable_families(
    tree: exp.Expression,
    baseline: str,
    *,
    dialect: str | None,
    index: int,
    schema_text: str,
) -> list[str]:
    context = Context(schema_text, tree)
    applicable: list[str] = []
    for name, operator in MUTATION_OPERATORS:
        working = tree.copy()
        try:
            label = operator(working, index, context)
        except Exception:  # noqa: BLE001 - inapplicable operators are expected.
            continue
        if not label:
            continue
        rendered = _render(working, dialect, schema_text)
        if rendered is None or rendered == baseline:
            continue
        applicable.append(name)
    return applicable


def _balance(applicable: dict[str, list[str]]) -> dict[str, str]:
    """Assign one operator family per family, maximising the rarest coverage."""
    counts: Counter[str] = Counter()
    assignment: dict[str, str] = {}
    for family_id in sorted(applicable, key=lambda key: (len(applicable[key]), key)):
        names = applicable[family_id]
        if not names:
            continue
        chosen = min(names, key=lambda name: (counts[name], MUTATION_FAMILY_ORDER[name]))
        assignment[family_id] = chosen
        counts[chosen] += 1
    return assignment


CARRIED_FIELDS = (
    "family_id",
    "lineage_family_id",
    "family_identity",
    "structural_family_id",
    "source_id",
    "partition",
    "dialect",
    "categories",
    "labels",
    "schema",
    "schema_catalog",
    "schema_trust",
    "schema_sha256",
    "replay_eligible",
    "captured_at",
    "scenario_candidates",
    "verified_scenario_axes",
    "observed_scenario_axes",
)


def _row(
    record: dict[str, Any],
    *,
    role: str,
    gold_sql: str,
    student_sql: str,
    operator_family: str,
    operator: str,
    salt: str,
) -> dict[str, Any]:
    family_id = str(record["family_id"])
    observed_axes = set(record.get("observed_scenario_axes") or ())
    verified_axes = set(record.get("verified_scenario_axes") or ())
    axes = sorted({*(record.get("scenario_axes") or ["base"]), "paired_mutation"})
    if role == "mutation":
        axes = sorted({*axes, "mutation_ready"})
    row = {field: record.get(field) for field in CARRIED_FIELDS}
    row.update(
        {
            "schema_version": SCHEMA_VERSION,
            "record_id": f"{family_id[:22]}:{role}",
            "source_record_id": record.get("record_id"),
            "sql": gold_sql,
            "sql_source_raw": record.get("sql"),
            "student_sql": student_sql,
            "expectation": "not_equivalent" if role == "mutation" else "equivalent",
            "attack_kind": operator,
            "mutation_layer_role": role,
            "mutation_operator_family": operator_family,
            "mutation_operator": operator,
            "scenario_axes": axes,
            "verified_scenario_axes": sorted(verified_axes),
            "observed_scenario_axes": sorted(observed_axes),
            "generator": "build_phase1_mutation_layer",
            "generator_salt": salt,
            "generator_version": SCHEMA_VERSION,
            "derivation": "single_point_ast_operator" if role == "mutation" else "equivalence_preserving_rewrite",
        }
    )
    return row


INDEX_SPACE = 1_000_003


def build(
    inputs: list[Path],
    output: Path,
    manifest_path: Path,
    *,
    salt: str,
    max_families: int,
    emit_equivalence: bool,
    progress_every: int,
) -> dict[str, Any]:
    seen: set[str] = set()
    applicable: dict[str, list[str]] = {}
    partitions: Counter[str] = Counter()
    stats = Counter()

    for record in _iter_records(inputs):
        if not _usable(record):
            stats["skipped_unusable_record"] += 1
            continue
        family_id = str(record["family_id"])
        if family_id in seen:
            stats["skipped_duplicate_family"] += 1
            continue
        seen.add(family_id)
        if max_families and len(seen) > max_families:
            seen.discard(family_id)
            break
        partitions[str(record.get("partition") or "unknown")] += 1
        dialect = _dialect_of(record)
        tree = _parse(
            str(record["sql"]),
            dialect,
            str(record.get("schema") or ""),
        )
        if tree is None:
            stats["parse_failed"] += 1
            continue
        baseline = _render(tree, dialect, str(record.get("schema") or ""))
        if baseline is None:
            stats["render_failed"] += 1
            continue
        index = _pick_index(family_id, salt, INDEX_SPACE)
        names = _applicable_families(
            tree,
            baseline,
            dialect=dialect,
            index=index,
            schema_text=str(record.get("schema") or ""),
        )
        if not names:
            stats["no_applicable_operator"] += 1
            continue
        applicable[family_id] = names
        if progress_every and len(applicable) % progress_every == 0:
            print(f"planned {len(applicable)} families", file=sys.stderr, flush=True)

    assignment = _balance(applicable)
    applicability_counts = Counter(name for names in applicable.values() for name in names)

    family_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    tactic_counts: Counter[str] = Counter()
    category_counts: dict[str, Counter[str]] = defaultdict(Counter)
    dialect_counts: dict[str, Counter[str]] = defaultdict(Counter)
    emitted = Counter()
    written: set[str] = set()

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in _iter_records(inputs):
            if not _usable(record):
                continue
            family_id = str(record["family_id"])
            if family_id in written or family_id not in assignment:
                continue
            written.add(family_id)
            dialect = _dialect_of(record)
            index = _pick_index(family_id, salt, INDEX_SPACE)
            schema_text = str(record.get("schema") or "")
            operator_family = assignment[family_id]
            result = apply_mutation(
                str(record["sql"]),
                operator_family,
                dialect=dialect,
                index=index,
                schema_text=schema_text,
            )
            if result is None:
                # Deterministic re-application should never fail; fall back to
                # the first applicable operator instead of dropping the family.
                for candidate in applicable[family_id]:
                    result = apply_mutation(
                        str(record["sql"]),
                        candidate,
                        dialect=dialect,
                        index=index,
                        schema_text=schema_text,
                    )
                    if result is not None:
                        operator_family = candidate
                        emitted["reassigned_operator"] += 1
                        break
            if result is None:
                emitted["mutation_lost"] += 1
                continue
            mutated_gold, mutated_sql, operator = result
            row = _row(
                record,
                role="mutation",
                gold_sql=mutated_gold,
                student_sql=mutated_sql,
                operator_family=operator_family,
                operator=operator,
                salt=salt,
            )
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            emitted["mutation_rows"] += 1
            family_counts[operator_family] += 1
            operator_counts[operator] += 1
            dialect_counts[str(record.get("dialect") or "generic")]["mutation"] += 1
            for category in record.get("categories") or ():
                category_counts[str(category)]["mutation"] += 1

            if not emit_equivalence:
                continue
            control = apply_equivalence(
                str(record["sql"]),
                dialect=dialect,
                index=index,
                schema_text=schema_text,
            )
            if control is None:
                emitted["equivalence_unavailable"] += 1
                continue
            control_gold, rewritten_sql, tactic = control
            control_row = _row(
                record,
                role="equivalence",
                gold_sql=control_gold,
                student_sql=rewritten_sql,
                operator_family="equivalence_preserving",
                operator=tactic,
                salt=salt,
            )
            handle.write(json.dumps(control_row, ensure_ascii=False, sort_keys=True) + "\n")
            emitted["equivalence_rows"] += 1
            tactic_counts[tactic] += 1
            dialect_counts[str(record.get("dialect") or "generic")]["equivalence"] += 1
            for category in record.get("categories") or ():
                category_counts[str(category)]["equivalence"] += 1


    families_read = len(seen)
    covered = [name for name in REQUIRED_FAMILY_NAMES if family_counts[name] > 0]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator": "build_phase1_mutation_layer",
        "generator_salt": salt,
        "inputs": [str(path) for path in inputs],
        "output": str(output),
        "contains_sql": False,
        "families_read": families_read,
        "families_with_applicable_operator": len(applicable),
        "families_assigned": len(assignment),
        "skips": dict(sorted(stats.items())),
        "emitted": dict(sorted(emitted.items())),
        "coverage": {
            "mutation_coverage_rate": (
                emitted["mutation_rows"] / families_read if families_read else 0.0
            ),
            "equivalence_coverage_rate": (
                emitted["equivalence_rows"] / families_read if families_read else 0.0
            ),
            "required_families_covered": len(covered),
            "required_families_total": len(REQUIRED_FAMILY_NAMES),
            "all_fifteen_families_covered": len(covered) == len(REQUIRED_FAMILY_NAMES),
            "missing_required_families": [
                name for name in REQUIRED_FAMILY_NAMES if family_counts[name] == 0
            ],
            "required_family_rows": sum(family_counts[name] for name in REQUIRED_FAMILY_NAMES),
            "supplementary_family_rows": sum(
                family_counts[name] for name, _ in SUPPLEMENTARY_OPERATORS
            ),
        },
        "by_operator_family": {
            name: family_counts[name] for name, _ in MUTATION_OPERATORS
        },
        "by_operator_family_applicability": {
            name: applicability_counts[name] for name, _ in MUTATION_OPERATORS
        },
        "by_concrete_operator": dict(sorted(operator_counts.items())),
        "by_equivalence_tactic": dict(sorted(tactic_counts.items())),
        "by_partition": dict(sorted(partitions.items())),
        "by_category": {
            key: dict(sorted(value.items())) for key, value in sorted(category_counts.items())
        },
        "by_dialect": {
            key: dict(sorted(value.items())) for key, value in sorted(dialect_counts.items())
        },
        "notes": {
            "family_denominator": "one mutation row and at most one control row per family id",
            "expectation_semantics": "not_equivalent means the pair differs on some database, not on every database",
            "hidden_discipline": "this manifest carries counts only, never SQL",
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, dest="inputs", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--salt", default="phase1-mutation-layer-v1")
    parser.add_argument("--max-families", type=int, default=0)
    parser.add_argument("--no-equivalence", action="store_true")
    parser.add_argument("--progress-every", type=int, default=5000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_families < 0:
        raise SystemExit("max-families must not be negative")
    missing = [str(path) for path in args.inputs if not path.is_file()]
    if missing:
        raise SystemExit(f"input not found: {', '.join(missing)}")
    manifest = build(
        args.inputs,
        args.output,
        args.manifest,
        salt=args.salt,
        max_families=args.max_families,
        emit_equivalence=not args.no_equivalence,
        progress_every=max(0, args.progress_every),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
