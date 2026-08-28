"""Resolve one SQL dialect for parsing and native sandbox execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Sequence

import sqlglot
from sqlglot import ErrorLevel, exp


STANDARD_SQL_DIALECT = "standard"
EXECUTION_SQL_DIALECTS = frozenset({"mysql", "postgres", "tsql", "sqlite", "oracle"})
SUPPORTED_SQL_DIALECTS = EXECUTION_SQL_DIALECTS | {STANDARD_SQL_DIALECT}
GENERIC_SQLGLOT_DIALECT = "__generic__"

_DIALECT_ALIASES = {
    "ansi": STANDARD_SQL_DIALECT,
    "ansi_sql": STANDARD_SQL_DIALECT,
    "generic": STANDARD_SQL_DIALECT,
    "sql": STANDARD_SQL_DIALECT,
    "mariadb": "mysql",
    "postgresql": "postgres",
    "pg": "postgres",
    "sqlserver": "tsql",
    "sql_server": "tsql",
    "mssql": "tsql",
    "ora": "oracle",
    "oracle23ai": "oracle",
    "oracle_free": "oracle",
}


class DialectResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    DIALECT_CONFLICT = "DIALECT_CONFLICT"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    UNSUPPORTED_DIALECT = "UNSUPPORTED_DIALECT"
    UNSUPPORTED_DIALECT_FEATURE = "UNSUPPORTED_DIALECT_FEATURE"


class DialectResolutionSource(str, Enum):
    DECLARED = "declared"
    DETECTED = "detected"
    DEFAULT = "default"


class StrictSQLParseError(ValueError):
    """SQL is not exactly one complete query."""


class UnsupportedSQLDialectError(ValueError):
    """A dialect is outside the explicit support list."""


class DialectResolutionError(ValueError):
    """Exception form used by request and Phase 1 orchestration code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        sql_role: str | None = None,
        resolution: SQLDialectResolution | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.sql_role = sql_role
        self.resolution = resolution


@dataclass(frozen=True)
class DialectFeature:
    name: str
    dialects: tuple[str, ...]
    high_confidence: bool

    @property
    def is_exclusive(self) -> bool:
        return self.high_confidence and len(self.dialects) == 1


@dataclass(frozen=True)
class SQLDialectResolution:
    status: DialectResolutionStatus
    dialect: str | None
    parse_dialect: str | None
    source: DialectResolutionSource | None
    candidates: tuple[str, ...] = ()
    detected_features: tuple[str, ...] = ()
    asts: tuple[exp.Query, ...] = ()
    error: str | None = None
    requested_dialect: str | None = None
    generic_parse_ok: tuple[bool, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.status == DialectResolutionStatus.RESOLVED

    @property
    def selected_dialect(self) -> str | None:
        return self.dialect

    @property
    def resolved_dialect(self) -> str | None:
        return self.dialect

    @property
    def execution_dialect(self) -> str | None:
        return self.dialect

    @property
    def execution_engine(self) -> str | None:
        return self.dialect

    @property
    def ast(self) -> exp.Query | None:
        return self.asts[0] if self.asts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "requested_dialect": self.requested_dialect,
            "resolved_dialect": self.dialect,
            "execution_engine": self.execution_engine,
            "source": self.source.value if self.source else None,
            "candidates": list(self.candidates),
            "detected_features": list(self.detected_features),
            "generic_parse_ok": list(self.generic_parse_ok),
            "error": self.error,
        }


DialectResolution = SQLDialectResolution


@dataclass(frozen=True)
class _FeatureRule:
    name: str
    dialects: tuple[str, ...]
    pattern: re.Pattern[str]
    high_confidence: bool = True


def _rule(
    name: str,
    dialects: tuple[str, ...],
    pattern: str,
    *,
    high_confidence: bool = True,
) -> _FeatureRule:
    return _FeatureRule(
        name=name,
        dialects=dialects,
        pattern=re.compile(pattern, re.IGNORECASE | re.DOTALL),
        high_confidence=high_confidence,
    )


_MYSQL_SELECT_PREFIX_MODIFIERS = (
    r"(?:ALL|DISTINCT|DISTINCTROW|HIGH_PRIORITY|STRAIGHT_JOIN|"
    r"SQL_SMALL_RESULT|SQL_BIG_RESULT|SQL_BUFFER_RESULT|SQL_NO_CACHE|"
    r"SQL_CALC_FOUND_ROWS)"
)
_MYSQL_SELECT_EXPRESSION_FOLLOWS = (
    r"(?!(?:AS|FROM|INTO|WHERE|GROUP|HAVING|QUALIFY|ORDER|LIMIT|FETCH|OFFSET|"
    r"UNION|INTERSECT|EXCEPT)\b|[,;)])"
)
_SQL_IDENTIFIER = r"(?:[A-Z_][A-Z0-9_$]*|`[^`]+`)"
_QUALIFIED_SQL_IDENTIFIER = rf"{_SQL_IDENTIFIER}(?:\s*\.\s*{_SQL_IDENTIFIER})*"
_ORACLE_IDENTIFIER = r'(?:[A-Z_][A-Z0-9_$#]*|"[^"]*")'
_ORACLE_QUALIFIED_IDENTIFIER = (
    rf"{_ORACLE_IDENTIFIER}(?:\s*\.\s*{_ORACLE_IDENTIFIER})*"
)


# Shared features remain evidence and never override the configured default.
_FEATURE_RULES: tuple[_FeatureRule, ...] = (
    _rule("MYSQL_BACKTICK_IDENTIFIER", ("mysql",), r"`[^`]+`"),
    _rule(
        "MYSQL_SQL_CALC_FOUND_ROWS",
        ("mysql",),
        rf"\bSELECT\s+(?:{_MYSQL_SELECT_PREFIX_MODIFIERS}\s+)*"
        rf"SQL_CALC_FOUND_ROWS\s+{_MYSQL_SELECT_EXPRESSION_FOLLOWS}",
    ),
    _rule(
        "MYSQL_STRAIGHT_JOIN",
        ("mysql",),
        rf"(?:\bSELECT\s+(?:{_MYSQL_SELECT_PREFIX_MODIFIERS}\s+)*"
        rf"STRAIGHT_JOIN\s+{_MYSQL_SELECT_EXPRESSION_FOLLOWS}|"
        rf"\b(?:FROM|JOIN)\s+{_QUALIFIED_SQL_IDENTIFIER}"
        rf"(?:\s+(?:AS\s+)?{_SQL_IDENTIFIER})?\s+STRAIGHT_JOIN\b)",
    ),
    _rule(
        "MYSQL_INDEX_HINT",
        ("mysql",),
        r"\b(?:USE|FORCE|IGNORE)\s+(?:INDEX|KEY)\s*"
        r"(?:FOR\s+(?:JOIN|ORDER\s+BY|GROUP\s+BY)\s*)?\(",
    ),
    _rule(
        "MYSQL_SELECT_MODIFIER",
        ("mysql",),
        rf"\bSELECT\s+(?=(?:{_MYSQL_SELECT_PREFIX_MODIFIERS}\s+)*"
        rf"(?:DISTINCTROW|HIGH_PRIORITY|"
        rf"SQL_(?:SMALL_RESULT|BIG_RESULT|BUFFER_RESULT|NO_CACHE))"
        rf"\s+{_MYSQL_SELECT_EXPRESSION_FOLLOWS})",
    ),
    _rule(
        "MYSQL_LIMIT_COMMA",
        ("mysql",),
        r"\bLIMIT\s+(?:\d+|\?|:[A-Z_][A-Z0-9_]*)\s*,\s*"
        r"(?:\d+|\?|:[A-Z_][A-Z0-9_]*)",
    ),
    _rule("MYSQL_NULL_SAFE_EQUAL", ("mysql",), r"<=>"),
    _rule("MYSQL_WITH_ROLLUP", ("mysql",), r"\bGROUP\s+BY\b.*?\bWITH\s+ROLLUP\b"),
    _rule("MYSQL_IF_FUNCTION", ("mysql",), r"\bIF\s*\("),
    _rule("MYSQL_DATE_FORMAT_FUNCTION", ("mysql",), r"\bDATE_FORMAT\s*\("),
    _rule("MYSQL_FIND_IN_SET_FUNCTION", ("mysql",), r"\bFIND_IN_SET\s*\("),
    _rule(
        "MYSQL_GROUP_CONCAT_OPTIONS",
        ("mysql",),
        r"\bGROUP_CONCAT\s*\([^)]*\b(?:ORDER\s+BY|SEPARATOR)\b",
    ),
    _rule("MYSQL_REGEXP_OPERATOR", ("mysql",), r"\b(?:REGEXP|RLIKE)\b"),
    _rule("MYSQL_INTEGER_DIVISION", ("mysql",), r"\bDIV\b"),
    _rule("POSTGRES_CAST_OPERATOR", ("postgres",), r"::\s*[A-Z_][A-Z0-9_]*(?:\s*\[\s*\])?"),
    _rule("POSTGRES_DISTINCT_ON", ("postgres",), r"\bDISTINCT\s+ON\s*\("),
    _rule("POSTGRES_ILIKE", ("postgres",), r"\bILIKE\b"),
    _rule("POSTGRES_REGEX_OPERATOR", ("postgres",), r"(?:!~\*?|(?<![!<>=])~\*?)"),
    _rule("POSTGRES_JSON_TABLE_FUNCTION", ("postgres",), r"\bJSONB?_ARRAY_ELEMENTS(?:_TEXT)?\s*\("),
    _rule("POSTGRES_JSON_PATH_OPERATOR", ("postgres",), r"#>>|#>"),
    _rule("POSTGRES_FROM_ONLY", ("postgres",), r"\b(?:FROM|JOIN)\s+ONLY\b"),
    _rule(
        "TSQL_TOP",
        ("tsql",),
        r"\bSELECT\s+(?:ALL\s+|DISTINCT\s+)?TOP\s*(?:\(\s*[^)]+\s*\)|\d+)",
    ),
    _rule(
        "TSQL_SELECT_VARIABLE_ASSIGNMENT",
        ("tsql",),
        r"\bSELECT\s+@[A-Z_][A-Z0-9_$]*\s*=",
    ),
    _rule("SHARED_APPLY", ("tsql", "oracle"), r"\b(?:CROSS|OUTER)\s+APPLY\b"),
    _rule("TSQL_BRACKET_IDENTIFIER", ("tsql",), r"(?:\bSELECT|\bFROM|\bJOIN|\bBY|,|\.)\s*\[[^\]]+\]"),
    _rule("TSQL_TABLE_HINT", ("tsql",), r"\bWITH\s*\(\s*(?:NOLOCK|UPDLOCK|HOLDLOCK)\b"),
    _rule("TSQL_MAXRECURSION", ("tsql",), r"\bOPTION\s*\(\s*MAXRECURSION\b"),
    _rule("TSQL_ISNULL_FUNCTION", ("tsql",), r"\bISNULL\s*\("),
    _rule("TSQL_GETDATE_FUNCTION", ("tsql",), r"\bGETDATE\s*\(\s*\)"),
    _rule("TSQL_DATEADD_FUNCTION", ("tsql",), r"\bDATEADD\s*\("),
    # MySQL also has a two-argument DATEDIFF(date, date).  Only the T-SQL
    # three-argument form has a date-part token followed by a comma, so keep
    # automatic detection narrow enough not to reject legitimate MySQL
    # queries that happen to use DATEDIFF.
    _rule(
        "TSQL_DATEDIFF_FUNCTION",
        ("tsql",),
        r"\bDATEDIFF(?:_BIG)?\s*\(\s*"
        r"(?:YEAR|QUARTER|MONTH|WEEK|DAY|HOUR|MINUTE|SECOND|"
        r"MILLISECOND|MICROSECOND|NANOSECOND)\s*,",
    ),
    _rule("TSQL_LEN_FUNCTION", ("tsql",), r"\bLEN\s*\("),
    _rule("TSQL_IIF_FUNCTION", ("tsql",), r"\bIIF\s*\("),
    _rule("SQLITE_GLOB", ("sqlite",), r"\bGLOB\b"),
    _rule("SQLITE_INDEXED_BY", ("sqlite",), r"\bINDEXED\s+BY\b"),
    _rule("ORACLE_CONNECT_BY", ("oracle",), r"\bCONNECT\s+BY\b"),
    _rule("ORACLE_ROWNUM", ("oracle",), r"\bROWNUM\b"),
    _rule("ORACLE_NVL_FUNCTION", ("oracle",), r"\bNVL\s*\("),
    _rule("ORACLE_NVL2_FUNCTION", ("oracle",), r"\bNVL2\s*\("),
    _rule("ORACLE_DECODE_FUNCTION", ("oracle",), r"\bDECODE\s*\("),
    _rule("ORACLE_SYSDATE", ("oracle",), r"\bSYSDATE\b"),
    _rule("ORACLE_MINUS", ("oracle",), r"\bMINUS\b"),
    _rule("ORACLE_OUTER_JOIN_MARKER", ("oracle",), r"\(\s*\+\s*\)"),
    _rule(
        "ORACLE_SAMPLE",
        ("oracle",),
        rf"\b(?:FROM|JOIN)\s+{_ORACLE_QUALIFIED_IDENTIFIER}"
        r"\s+SAMPLE(?:\s+BLOCK)?\s*\(",
    ),
    _rule("SHARED_PIVOT", ("tsql", "oracle"), r"\b(?:PIVOT|UNPIVOT)\s*\("),
    _rule("SHARED_LIMIT", ("mysql", "postgres", "sqlite"), r"\bLIMIT\b", high_confidence=False),
    _rule("SHARED_QUALIFY", tuple(sorted(EXECUTION_SQL_DIALECTS)), r"\bQUALIFY\b", high_confidence=False),
    _rule("SHARED_JSON_ARROW", ("mysql", "postgres", "sqlite"), r"->>?", high_confidence=False),
    _rule("SHARED_JSON_EXTRACT", ("mysql", "sqlite"), r"\bJSON_EXTRACT\s*\(", high_confidence=False),
    _rule("SHARED_IFNULL", ("mysql", "sqlite"), r"\bIFNULL\s*\(", high_confidence=False),
    _rule("SHARED_FETCH_FIRST", ("postgres", "tsql", "oracle"), r"\bFETCH\s+(?:FIRST|NEXT)\b", high_confidence=False),
    _rule("SHARED_GENERATE_SERIES", ("postgres", "tsql"), r"\bGENERATE_SERIES\s*\(", high_confidence=False),
)

# SQLite deliberately accepts MySQL-style backtick identifiers as a
# compatibility syntax.  Keep the feature as MySQL evidence for automatic
# resolution, but do not reject it when SQLite is explicitly selected.
_DIALECT_COMPATIBLE_FEATURES = {
    "sqlite": frozenset({"MYSQL_BACKTICK_IDENTIFIER"}),
}

# FETCH FIRST is part of the portable teaching subset. Other entries in the
# feature table are vendor syntax even when more than one engine accepts them.
_STANDARD_SQL_FEATURES = frozenset({"SHARED_FETCH_FIRST"})


def normalize_sql_dialect(value: str | None, *, allow_none: bool = True) -> str | None:
    """Normalize aliases without silently coercing null or unknown values."""
    if value is None or not str(value).strip():
        if allow_none:
            return None
        raise UnsupportedSQLDialectError("SQL dialect cannot be empty")
    lowered = str(value).strip().lower()
    normalized = _DIALECT_ALIASES.get(lowered, lowered)
    if normalized not in SUPPORTED_SQL_DIALECTS:
        supported = ", ".join(sorted(SUPPORTED_SQL_DIALECTS))
        raise UnsupportedSQLDialectError(
            f"Unsupported SQL dialect {value!r}; expected one of: {supported}"
        )
    return normalized


def parse_single_query(
    sql: str,
    *,
    dialect: str | None = None,
    enforce_dialect_compatibility: bool = False,
) -> exp.Query:
    """Parse exactly one complete DQL query with recovery disabled."""
    normalized = normalize_sql_dialect(dialect) if dialect is not None else None
    if not isinstance(sql, str) or not sql.strip():
        raise StrictSQLParseError("SQL must contain exactly one query")
    parse_dialect = None if normalized == STANDARD_SQL_DIALECT else normalized
    try:
        statements = sqlglot.parse(sql, read=parse_dialect, error_level=ErrorLevel.RAISE)
    except Exception as exc:
        raise StrictSQLParseError(str(exc)) from exc
    parsed = [
        statement
        for statement in statements
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if len(parsed) != 1:
        raise StrictSQLParseError(
            f"SQL must contain exactly one query; parsed {len(parsed)} statements"
        )
    if not isinstance(parsed[0], exp.Query):
        raise StrictSQLParseError(
            f"SQL statement must be a query, got {type(parsed[0]).__name__}"
        )
    if normalized is not None and enforce_dialect_compatibility:
        if normalized == STANDARD_SQL_DIALECT:
            incompatible = tuple(
                feature.name
                for feature in detect_dialect_features(sql)
                if feature.name not in _STANDARD_SQL_FEATURES
            )
        else:
            incompatible = tuple(
                feature.name
                for feature in detect_dialect_features(sql)
                if normalized not in feature.dialects
                and feature.name not in _DIALECT_COMPATIBLE_FEATURES.get(normalized, ())
            )
        if incompatible:
            feature_text = ", ".join(dict.fromkeys(incompatible))
            raise StrictSQLParseError(
                f"SQL uses syntax incompatible with {normalized}: {feature_text}"
            )
    return parsed[0]


def parse_query_strict(sql: str, dialect: str | None, *, sql_role: str) -> exp.Query:
    """Exception-based strict parser carrying standard/student attribution."""
    try:
        return parse_single_query(
            sql,
            dialect=dialect,
            enforce_dialect_compatibility=dialect is not None,
        )
    except (StrictSQLParseError, UnsupportedSQLDialectError) as exc:
        code = "STANDARD_SQL_PARSE_ERROR" if sql_role == "standard" else "STUDENT_SQL_PARSE_ERROR"
        raise DialectResolutionError(code, str(exc), sql_role=sql_role) from exc


def detect_dialect_features(sql: str) -> tuple[DialectFeature, ...]:
    """Detect dialect evidence outside comments and quoted string contents."""
    code = _mask_non_code(sql)
    return tuple(
        DialectFeature(rule.name, rule.dialects, rule.high_confidence)
        for rule in _FEATURE_RULES
        if rule.pattern.search(code)
    )


def resolve_sql_dialect(
    sql: str | Sequence[str] | None = None,
    *,
    declared_dialect: str | None = None,
    default_dialect: str = "mysql",
    standard_sql: str | None = None,
    student_sql: str | None = None,
) -> SQLDialectResolution:
    """Resolve standard and student queries to one parsing/execution dialect."""
    if standard_sql is not None or student_sql is not None:
        sql_items = tuple(item for item in (standard_sql, student_sql) if item is not None)
    elif isinstance(sql, str):
        sql_items = (sql,)
    else:
        sql_items = tuple(sql or ())
    if not sql_items:
        return _failure(DialectResolutionStatus.SYNTAX_ERROR, "At least one SQL query is required")

    try:
        requested = normalize_sql_dialect(declared_dialect)
    except UnsupportedSQLDialectError as exc:
        return _failure(DialectResolutionStatus.UNSUPPORTED_DIALECT, str(exc))

    generic_asts: list[exp.Query] = []
    generic_ok: list[bool] = []
    for item in sql_items:
        try:
            generic_asts.append(parse_single_query(item))
            generic_ok.append(True)
        except StrictSQLParseError:
            generic_ok.append(False)

    if requested is not None:
        if requested == STANDARD_SQL_DIALECT:
            try:
                default = normalize_sql_dialect(default_dialect, allow_none=False)
            except UnsupportedSQLDialectError as exc:
                return _failure(DialectResolutionStatus.UNSUPPORTED_DIALECT, str(exc))
            if default == STANDARD_SQL_DIALECT:
                return _failure(
                    DialectResolutionStatus.UNSUPPORTED_DIALECT,
                    "The standard SQL teaching dialect requires a concrete default execution engine",
                    requested=requested,
                    generic_ok=tuple(generic_ok),
                )
            parsed, error = _parse_pair(
                sql_items,
                requested,
                enforce_dialect_compatibility=True,
            )
            features = tuple(
                feature for item in sql_items for feature in detect_dialect_features(item)
            )
            feature_names = tuple(dict.fromkeys(feature.name for feature in features))
            if error:
                return SQLDialectResolution(
                    status=DialectResolutionStatus.SYNTAX_ERROR,
                    dialect=default,
                    parse_dialect=None,
                    source=None,
                    detected_features=feature_names,
                    error=error,
                    requested_dialect=requested,
                    generic_parse_ok=tuple(generic_ok),
                )
            return SQLDialectResolution(
                status=DialectResolutionStatus.RESOLVED,
                dialect=default,
                parse_dialect=None,
                source=DialectResolutionSource.DECLARED,
                detected_features=feature_names,
                asts=parsed,
                requested_dialect=requested,
                generic_parse_ok=tuple(generic_ok),
            )
        parsed, error = _parse_pair(
            sql_items,
            requested,
            enforce_dialect_compatibility=True,
        )
        if error:
            return _failure(
                DialectResolutionStatus.SYNTAX_ERROR,
                error,
                dialect=requested,
                requested=requested,
                generic_ok=tuple(generic_ok),
            )
        return SQLDialectResolution(
            status=DialectResolutionStatus.RESOLVED,
            dialect=requested,
            parse_dialect=requested,
            source=DialectResolutionSource.DECLARED,
            asts=parsed,
            requested_dialect=requested,
            generic_parse_ok=tuple(generic_ok),
        )

    try:
        default = normalize_sql_dialect(default_dialect, allow_none=False)
    except UnsupportedSQLDialectError as exc:
        return _failure(DialectResolutionStatus.UNSUPPORTED_DIALECT, str(exc))
    assert default is not None
    if default == STANDARD_SQL_DIALECT:
        return _failure(
            DialectResolutionStatus.UNSUPPORTED_DIALECT,
            "The default SQL dialect must name a concrete execution engine",
        )

    features = tuple(feature for item in sql_items for feature in detect_dialect_features(item))
    feature_names = tuple(dict.fromkeys(feature.name for feature in features))
    unsupported = tuple(dict.fromkeys(feature.name for feature in features if not feature.dialects))
    if unsupported:
        return _failure(
            DialectResolutionStatus.UNSUPPORTED_DIALECT_FEATURE,
            "No configured native engine supports: " + ", ".join(unsupported),
            features=feature_names,
            generic_ok=tuple(generic_ok),
        )
    candidates = tuple(sorted({feature.dialects[0] for feature in features if feature.is_exclusive}))
    if len(candidates) > 1:
        return _failure(
            DialectResolutionStatus.DIALECT_CONFLICT,
            "Conflicting dialect-specific features: " + ", ".join(candidates),
            candidates=candidates,
            features=feature_names,
            generic_ok=tuple(generic_ok),
        )

    ambiguous_sets = [
        set(feature.dialects)
        for feature in features
        if feature.high_confidence and len(feature.dialects) > 1
    ]
    if not candidates and ambiguous_sets:
        ambiguous = set.intersection(*ambiguous_sets)
        if default not in ambiguous:
            ambiguous_candidates = tuple(sorted(ambiguous))
            return _failure(
                DialectResolutionStatus.DIALECT_CONFLICT,
                "Dialect declaration required for shared vendor syntax: "
                + ", ".join(ambiguous_candidates),
                candidates=ambiguous_candidates,
                features=feature_names,
                generic_ok=tuple(generic_ok),
            )

    target = candidates[0] if candidates else default
    source = DialectResolutionSource.DETECTED if candidates else DialectResolutionSource.DEFAULT
    if candidates:
        parsed, error = _parse_pair(sql_items, target)
        if error:
            return _failure(
                DialectResolutionStatus.SYNTAX_ERROR,
                error,
                dialect=target,
                candidates=candidates,
                features=feature_names,
                generic_ok=tuple(generic_ok),
            )
        parse_dialect = target
    else:
        if not all(generic_ok):
            return _failure(
                DialectResolutionStatus.SYNTAX_ERROR,
                "SQL cannot be parsed by the generic SQLGlot dialect",
                dialect=target,
                features=feature_names,
                generic_ok=tuple(generic_ok),
            )
        parsed = tuple(generic_asts)
        parse_dialect = None
    return SQLDialectResolution(
        status=DialectResolutionStatus.RESOLVED,
        dialect=target,
        parse_dialect=parse_dialect,
        source=source,
        candidates=candidates,
        detected_features=feature_names,
        asts=parsed,
        generic_parse_ok=tuple(generic_ok),
    )


def resolve_sql_dialect_or_raise(**kwargs: Any) -> SQLDialectResolution:
    """Resolve and convert status results into typed orchestration errors."""
    resolution = resolve_sql_dialect(**kwargs)
    if resolution.is_resolved:
        return resolution
    code = resolution.status.value
    role = None
    if resolution.status == DialectResolutionStatus.SYNTAX_ERROR:
        standard_sql = kwargs.get("standard_sql")
        student_sql = kwargs.get("student_sql")
        sql_roles = tuple(
            (name, item)
            for name, item in (("standard", standard_sql), ("student", student_sql))
            if item is not None
        )
        # With no declared or uniquely detected dialect, syntax validity is
        # defined by the generic SQLGlot pass.  Re-parsing with the configured
        # execution default here could misclassify a dialect-only construct.
        use_generic_result = (
            resolution.requested_dialect is None
            and not resolution.candidates
            and len(resolution.generic_parse_ok) == len(sql_roles)
        )
        if use_generic_result:
            for (name, _), parsed_ok in zip(sql_roles, resolution.generic_parse_ok):
                if not parsed_ok:
                    code = f"{name.upper()}_SQL_PARSE_ERROR"
                    role = name
                    break
        else:
            dialect = resolution.dialect
            for name, item in sql_roles:
                try:
                    parse_single_query(
                        item,
                        dialect=dialect,
                        enforce_dialect_compatibility=(
                            resolution.requested_dialect is not None
                        ),
                    )
                except StrictSQLParseError:
                    code = f"{name.upper()}_SQL_PARSE_ERROR"
                    role = name
                    break
        if role is None:
            code = "SQL_PARSE_ERROR"
    raise DialectResolutionError(
        code,
        resolution.error or code,
        sql_role=role,
        resolution=resolution,
    )


def _parse_pair(
    sql_items: tuple[str, ...],
    dialect: str,
    *,
    enforce_dialect_compatibility: bool = False,
) -> tuple[tuple[exp.Query, ...], str | None]:
    parsed: list[exp.Query] = []
    for item in sql_items:
        try:
            parsed.append(
                parse_single_query(
                    item,
                    dialect=dialect,
                    enforce_dialect_compatibility=enforce_dialect_compatibility,
                )
            )
        except StrictSQLParseError as exc:
            return (), str(exc)
    return tuple(parsed), None


def _failure(
    status: DialectResolutionStatus,
    error: str,
    *,
    dialect: str | None = None,
    requested: str | None = None,
    candidates: tuple[str, ...] = (),
    features: tuple[str, ...] = (),
    generic_ok: tuple[bool, ...] = (),
) -> SQLDialectResolution:
    return SQLDialectResolution(
        status=status,
        dialect=dialect,
        parse_dialect=dialect,
        source=None,
        candidates=candidates,
        detected_features=features,
        error=error,
        requested_dialect=requested,
        generic_parse_ok=generic_ok,
    )


def _mask_non_code(sql: str) -> str:
    """Mask comments and quoted contents while preserving identifier delimiters."""
    chars = list(sql or "")
    masked = list(chars)
    length = len(chars)
    index = 0

    def blank(start: int, end: int, keep: frozenset[int] = frozenset()) -> None:
        for position in range(start, end):
            if position not in keep and masked[position] not in {"\r", "\n"}:
                masked[position] = " "

    while index < length:
        char = chars[index]
        following = chars[index + 1] if index + 1 < length else ""
        if char == "$":
            delimiter_match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
            if delimiter_match:
                delimiter = delimiter_match.group(0)
                close = sql.find(delimiter, index + len(delimiter))
                if close >= 0:
                    end = close + len(delimiter)
                    blank(index, end)
                    index = end
                    continue
        if char == "-" and following == "-":
            end = index + 2
            while end < length and chars[end] not in {"\r", "\n"}:
                end += 1
            blank(index, end)
            index = end
            continue
        if char == "#" and following not in {">", ""}:
            end = index + 1
            while end < length and chars[end] not in {"\r", "\n"}:
                end += 1
            blank(index, end)
            index = end
            continue
        if char == "/" and following == "*":
            close = sql.find("*/", index + 2)
            end = length if close < 0 else close + 2
            blank(index, end)
            index = end
            continue
        if char in {"'", '"', "`"}:
            quote = char
            end = index + 1
            while end < length:
                if chars[end] == "\\" and end + 1 < length:
                    end += 2
                    continue
                if chars[end] == quote:
                    if end + 1 < length and chars[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            keep = (
                frozenset({index, end - 1})
                if quote in {'"', "`"} and end > index + 1
                else frozenset()
            )
            blank(index, end, keep)
            index = end
            continue
        if char == "[":
            end = index + 1
            close = None
            while end < length:
                if chars[end] != "]":
                    end += 1
                    continue
                if end + 1 < length and chars[end + 1] == "]":
                    end += 2
                    continue
                close = end
                break
            if close is not None:
                blank(index, close + 1, frozenset({index, close}))
                index = close + 1
                continue
        index += 1
    return "".join(masked)


__all__ = [
    "DialectFeature",
    "DialectResolutionError",
    "DialectResolution",
    "DialectResolutionSource",
    "DialectResolutionStatus",
    "EXECUTION_SQL_DIALECTS",
    "GENERIC_SQLGLOT_DIALECT",
    "SQLDialectResolution",
    "SUPPORTED_SQL_DIALECTS",
    "STANDARD_SQL_DIALECT",
    "StrictSQLParseError",
    "UnsupportedSQLDialectError",
    "detect_dialect_features",
    "normalize_sql_dialect",
    "parse_query_strict",
    "parse_single_query",
    "resolve_sql_dialect",
    "resolve_sql_dialect_or_raise",
]
