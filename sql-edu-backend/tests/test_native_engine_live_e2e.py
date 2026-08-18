"""Live Phase 1 E2E checks for the four development native judge engines.

The test module covers MySQL, PostgreSQL, SQL Server, and Oracle services from
``compose.native-engines.yml``. It never invokes Docker Compose or starts an
engine. A normal pytest run skips a missing/unreachable engine;
``run_native_engine_live_gate.py`` sets ``PARSEVAL_NATIVE_LIVE_STRICT`` so the
same condition fails the release gate.

Connection URLs are read from the backend ``.env`` (with an environment
variable taking precedence), but are kept out of test ids, assertion messages,
and output. Each backend fixture requires an empty temporary namespace before
and after three complete data-generation pipeline runs. This catches both
routing/verdict regressions and fixture leakage.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import re
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
from dotenv import dotenv_values

import core.native_engine_runner as native_runner
from core.native_engine_runner import (
    execute_native_query,
    native_backend_available,
)
from core.parseval_data_generator import (
    SandboxRun,
    extract_ast_diffs,
    generate_and_compare,
)
from core.sql_dialect_resolver import resolve_sql_dialect_or_raise


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = _BACKEND_ROOT / ".env"
_STRICT_ENV = "PARSEVAL_NATIVE_LIVE_STRICT"
_STRICT_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class _LiveCase:
    backend: str
    url_key: str
    schema: str
    standard_sql: str
    student_sql: str
    declared_dialect: str | None
    expected_resolution_source: str
    expected_version: re.Pattern[str]
    version_probe_sql: str
    expected_mutation_clause: str
    expected_kp: str


@dataclass(frozen=True)
class _NamespaceSnapshot:
    databases_or_schemas: frozenset[str]
    query_users_or_roles: frozenset[str]


@dataclass(frozen=True)
class _LiveEvidence:
    case: _LiveCase
    engine_version: str
    before: _NamespaceSnapshot
    after: _NamespaceSnapshot
    correct: SandboxRun
    wrong: SandboxRun
    repeated_wrong: SandboxRun


_CASES = (
    _LiveCase(
        backend="mysql",
        url_key="PARSEVAL_MYSQL_URL",
        schema="items(item_id BIGINT, nickname VARCHAR(64));",
        # MySQL's NULL-safe equality is preserved as source SQL. Ordinary
        # equality with NULL is never TRUE, so this is deterministic.
        standard_sql=(
            "SELECT item_id FROM items "
            "WHERE nickname <=> NULL ORDER BY item_id"
        ),
        student_sql=(
            "SELECT item_id FROM items "
            "WHERE nickname = NULL ORDER BY item_id"
        ),
        declared_dialect="mysql",
        expected_resolution_source="declared",
        expected_version=re.compile(r"^8\.4(?:\.|$)"),
        version_probe_sql="SELECT VERSION()",
        expected_mutation_clause="WHERE",
        expected_kp="comp-null",
    ),
    _LiveCase(
        backend="postgres",
        url_key="PARSEVAL_POSTGRES_URL",
        schema="orders(customer_id BIGINT, amount BIGINT);",
        # DISTINCT ON is detected as PostgreSQL without a declared dialect.
        # Reversing its order changes which row survives per customer.
        standard_sql=(
            "SELECT DISTINCT ON (customer_id) customer_id, amount "
            "FROM orders ORDER BY customer_id, amount DESC"
        ),
        student_sql=(
            "SELECT DISTINCT ON (customer_id) customer_id, amount "
            "FROM orders ORDER BY customer_id, amount ASC"
        ),
        declared_dialect=None,
        expected_resolution_source="detected",
        expected_version=re.compile(r"^PostgreSQL\s+16(?:\.|\s|$)", re.IGNORECASE),
        version_probe_sql="SELECT VERSION()",
        expected_mutation_clause="ORDER BY",
        expected_kp="order-by",
    ),
    _LiveCase(
        backend="tsql",
        url_key="PARSEVAL_TSQL_URL",
        schema="scores(student_id BIGINT, score BIGINT);",
        # TOP is detected as T-SQL. The generated score boundaries make the
        # first row differ when the primary sort direction is reversed.
        standard_sql=(
            "SELECT TOP (1) student_id, score FROM scores "
            "ORDER BY score DESC, student_id"
        ),
        student_sql=(
            "SELECT TOP (1) student_id, score FROM scores "
            "ORDER BY score ASC, student_id"
        ),
        declared_dialect=None,
        expected_resolution_source="detected",
        expected_version=re.compile(r"^16(?:\.|$)"),
        version_probe_sql=(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128))"
        ),
        expected_mutation_clause="ORDER BY",
        expected_kp="order-by",
    ),
    _LiveCase(
        backend="oracle",
        url_key="PARSEVAL_ORACLE_URL",
        schema="employees(employee_id NUMBER, nickname VARCHAR2(64));",
        # ROWNUM is detected as Oracle. The two boundaries produce different
        # row counts while remaining deterministic after the root ORDER BY.
        standard_sql=(
            "SELECT employee_id FROM employees "
            "WHERE ROWNUM <= 2 ORDER BY employee_id"
        ),
        student_sql=(
            "SELECT employee_id FROM employees "
            "WHERE ROWNUM <= 1 ORDER BY employee_id"
        ),
        declared_dialect=None,
        expected_resolution_source="detected",
        expected_version=re.compile(r"^23(?:\.|ai(?:\s|$))", re.IGNORECASE),
        version_probe_sql=(
            "SELECT VERSION_FULL FROM PRODUCT_COMPONENT_VERSION "
            "WHERE PRODUCT LIKE 'Oracle Database%'"
        ),
        expected_mutation_clause="WHERE",
        expected_kp="where",
    ),
)


def _strict_mode() -> bool:
    return os.environ.get(_STRICT_ENV, "").strip().lower() in _STRICT_VALUES


def _unavailable(case: _LiveCase, reason: str) -> NoReturn:
    # Keep this message deliberately free of exception text and connection URL.
    message = f"live {case.backend} native engine unavailable ({reason})"
    if _strict_mode():
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _contract_failure(case: _LiveCase, reason: str) -> NoReturn:
    pytest.fail(f"live {case.backend} native gate contract failed ({reason})", pytrace=False)


def _safe_error_reason(exc: BaseException) -> str:
    """Return a stable error identifier without rendering driver messages."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        return code
    class_name = type(exc).__name__
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", class_name):
        return class_name
    return "Exception"


def _snapshot_is_empty(snapshot: _NamespaceSnapshot) -> bool:
    return not snapshot.databases_or_schemas and not snapshot.query_users_or_roles


def _assert_no_connection_details(
    case: _LiveCase,
    url: str,
    result: SandboxRun,
) -> None:
    """Fail without echoing the payload or the connection URL."""
    rendered = repr(result)
    parsed = urlsplit(url.strip())
    sensitive_values = {url.strip(), case.url_key}
    if parsed.username or parsed.password:
        sensitive_values.add(parsed.netloc)
    if parsed.password:
        passwords = (parsed.password, unquote(parsed.password))
        sensitive_values.update(password for password in passwords if len(password) >= 6)
    if any(value and value in rendered for value in sensitive_values):
        _contract_failure(case, "connection_details_exposed_in_evidence")


def _connection_url(case: _LiveCase) -> str | None:
    value = os.environ.get(case.url_key)
    if value is None:
        try:
            value = dotenv_values(_ENV_PATH).get(case.url_key)
        except (OSError, UnicodeError):
            value = None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _probe(case: _LiveCase, url: str) -> str:
    """Exercise provisioning and return the vendor version without logging DSNs."""
    if not native_backend_available(case.backend, url):
        raise RuntimeError("driver_or_url_unavailable")
    _, rows = execute_native_query(
        case.backend,
        {},
        {},
        {},
        case.version_probe_sql,
        url,
    )
    if not rows or not rows[0]:
        raise RuntimeError("version_probe_empty")
    return str(rows[0][0])


def _mysql_connection(url: str) -> Any:
    driver = importlib.import_module("pymysql")
    parsed = urlsplit(url.strip())
    return driver.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=3,
        read_timeout=3,
        write_timeout=3,
    )


def _postgres_connection(url: str) -> Any:
    try:
        driver = importlib.import_module("psycopg")
    except ImportError:
        driver = importlib.import_module("psycopg2")
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower().split("+", 1)[0]
    if scheme == "postgres":
        scheme = "postgresql"
    normalized_url = urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return driver.connect(normalized_url, connect_timeout=3)


def _tsql_connection(url: str) -> Any:
    driver = importlib.import_module("pyodbc")
    connection = driver.connect(
        native_runner._sqlserver_odbc_connection_string(url),
        autocommit=True,
        timeout=3,
    )
    try:
        connection.timeout = 3
    except (AttributeError, TypeError):
        pass
    return connection


def _oracle_connection(url: str) -> Any:
    driver = importlib.import_module("oracledb")
    params = native_runner._oracle_connection_params(driver, url)
    connect_kwargs = dict(params["connect_kwargs"])
    connect_kwargs.update(user=params["user"], password=params["password"])
    connection = driver.connect(**connect_kwargs)
    if hasattr(connection, "call_timeout"):
        connection.call_timeout = 3_000
    return connection


def _snapshot_tsql(cursor: Any) -> _NamespaceSnapshot:
    cursor.execute(
        "SELECT name FROM sys.databases WHERE name LIKE N'parseval[_]%'"
    )
    databases = frozenset(str(row[0]) for row in cursor.fetchall())
    users: set[str] = set()
    for database in databases:
        # Only names produced by the runner are interpolated. Unexpected names
        # remain in the database snapshot and therefore still fail the gate.
        if not re.fullmatch(r"parseval_[0-9a-f]{20}", database):
            continue
        quoted_database = "[" + database.replace("]", "]]") + "]"
        cursor.execute(
            f"SELECT name FROM {quoted_database}.sys.database_principals "
            "WHERE name LIKE N'parseval[_]user[_]%'"
        )
        users.update(f"{database}:{row[0]}" for row in cursor.fetchall())
    return _NamespaceSnapshot(databases, frozenset(users))


def _snapshot_oracle(cursor: Any) -> _NamespaceSnapshot:
    cursor.execute(
        "SELECT username FROM dba_users "
        "WHERE username LIKE 'PV\\_%' ESCAPE '\\'"
    )
    users = frozenset(str(row[0]) for row in cursor.fetchall())
    # In Oracle each generated user owns the schema with the same name.
    return _NamespaceSnapshot(users, users)


def _namespace_snapshot(case: _LiveCase, url: str) -> _NamespaceSnapshot:
    """Return only generated namespace identifiers, never credentials."""
    connection_factory = {
        "mysql": _mysql_connection,
        "postgres": _postgres_connection,
        "tsql": _tsql_connection,
        "oracle": _oracle_connection,
    }[case.backend]
    connection = connection_factory(url)
    cursor = None
    try:
        cursor = connection.cursor()
        if case.backend == "mysql":
            cursor.execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
                "WHERE SCHEMA_NAME LIKE %s",
                (r"parseval\_%",),
            )
            databases = frozenset(str(row[0]) for row in cursor.fetchall())
            cursor.execute(
                "SELECT User, Host FROM mysql.user WHERE User LIKE %s",
                (r"pv\_%",),
            )
            users = frozenset(f"{row[0]}@{row[1]}" for row in cursor.fetchall())
            return _NamespaceSnapshot(databases, users)
        if case.backend == "postgres":
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE %s",
                (r"parseval\_%",),
            )
            databases = frozenset(str(row[0]) for row in cursor.fetchall())
            cursor.execute(
                "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname LIKE %s",
                (r"parseval_role\_%",),
            )
            users = frozenset(str(row[0]) for row in cursor.fetchall())
            return _NamespaceSnapshot(databases, users)
        if case.backend == "tsql":
            return _snapshot_tsql(cursor)
        return _snapshot_oracle(cursor)
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            connection.close()


def _run_pipeline(case: _LiveCase, url: str, student_sql: str) -> SandboxRun:
    return generate_and_compare(
        case.schema,
        case.standard_sql,
        student_sql,
        sql_dialect=case.declared_dialect,
        default_sql_dialect="mysql",
        execution_backend="auto",
        native_executor_url=url,
        max_rows_per_table=8,
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: f"{case.backend}-definition")
def test_live_case_definition_has_expected_dialect_and_structure(
    case: _LiveCase,
) -> None:
    """Keep the live matrix meaningful even when its engines are unavailable."""
    resolution = resolve_sql_dialect_or_raise(
        declared_dialect=case.declared_dialect,
        standard_sql=case.standard_sql,
        student_sql=case.student_sql,
        default_dialect="mysql",
    )
    assert resolution.resolved_dialect == case.backend
    assert resolution.to_dict().get("source") == case.expected_resolution_source
    diffs = extract_ast_diffs(
        case.standard_sql,
        case.student_sql,
        dialect=resolution.parse_dialect,
    )
    assert any(
        diff.to_dict().get("clause") == case.expected_mutation_clause
        for diff in diffs
    )


@pytest.fixture(scope="module", params=_CASES, ids=lambda case: case.backend)
def live_evidence(request: pytest.FixtureRequest) -> _LiveEvidence:
    case: _LiveCase = request.param
    url = _connection_url(case)
    if url is None:
        _unavailable(case, f"missing_{case.url_key.lower()}")
    startup_failure: str | None = None
    try:
        version = _probe(case, url)
        before = _namespace_snapshot(case, url)
    except Exception as exc:
        # Only expose a stable exception class/code; driver messages may echo a
        # DSN or other deployment-specific details.
        startup_failure = _safe_error_reason(exc)
    if startup_failure is not None:
        _unavailable(case, startup_failure)

    if not case.expected_version.search(version):
        _contract_failure(case, "unexpected_engine_version")
    if not _snapshot_is_empty(before):
        _contract_failure(case, "temporary_namespace_not_clean_before_run")

    pipeline_failure: str | None = None
    try:
        correct = _run_pipeline(case, url, case.standard_sql)
        wrong = _run_pipeline(case, url, case.student_sql)
        repeated_wrong = _run_pipeline(case, url, case.student_sql)
    except Exception as exc:
        pipeline_failure = _safe_error_reason(exc)

    cleanup_probe_failure: str | None = None
    try:
        after = _namespace_snapshot(case, url)
    except Exception as exc:
        cleanup_probe_failure = _safe_error_reason(exc)
    if cleanup_probe_failure is not None:
        _contract_failure(case, f"cleanup_probe_{cleanup_probe_failure}")
    if not _snapshot_is_empty(after):
        _contract_failure(case, "temporary_namespace_left_after_run")
    if before != after:
        _contract_failure(case, "fixture_cleanup_changed_namespace")
    if pipeline_failure is not None:
        _contract_failure(case, f"pipeline_{pipeline_failure}")

    # A native runner failure is a gate failure, not a wrong student answer.
    for label, result in (
        ("correct", correct),
        ("wrong", wrong),
        ("repeated_wrong", repeated_wrong),
    ):
        _assert_no_connection_details(case, url, result)
        if not result.executed:
            reason = result.error_code or result.judge_status or "not_executed"
            if not isinstance(reason, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{0,63}", reason
            ):
                reason = "not_executed"
            _contract_failure(case, f"{label}_{reason}")

    return _LiveEvidence(case, version, before, after, correct, wrong, repeated_wrong)


def test_live_native_routes_to_expected_dialect_and_engine(live_evidence: _LiveEvidence) -> None:
    case = live_evidence.case
    assert case.expected_version.search(live_evidence.engine_version)
    for result in (
        live_evidence.correct,
        live_evidence.wrong,
        live_evidence.repeated_wrong,
    ):
        assert result.data_evidence.get("execution_backend") == case.backend
        assert result.data_evidence.get("sql_dialect") == case.backend
        resolution = result.data_evidence.get("dialect_resolution") or {}
        assert resolution.get("resolved_dialect") == case.backend
        assert resolution.get("source") == case.expected_resolution_source


def test_live_native_correct_and_wrong_verdicts(live_evidence: _LiveEvidence) -> None:
    correct = live_evidence.correct
    wrong = live_evidence.wrong
    assert correct.judge_status == "CORRECT"
    assert correct.is_equivalent is True
    assert wrong.judge_status == "WRONG"
    assert wrong.is_equivalent is False
    assert wrong.data_evidence.get("student_exec_ok") is True


def test_live_native_wrong_case_has_expected_structure_diff(
    live_evidence: _LiveEvidence,
) -> None:
    case = live_evidence.case
    diffs = [diff.to_dict() for diff in live_evidence.wrong.ast_diffs]
    assert diffs
    assert any(
        diff.get("clause") == case.expected_mutation_clause
        and diff.get("knowledge_point_id")
        for diff in diffs
    )


def test_live_native_wrong_case_has_row_value_counterexample(
    live_evidence: _LiveEvidence,
) -> None:
    wrong = live_evidence.wrong
    evidence = wrong.data_evidence
    assert wrong.test_database
    assert any(len(table_rows) >= 2 for table_rows in wrong.test_database.values())
    assert evidence.get("is_equivalent_on_generated_data") is False
    assert evidence.get("student_exec_ok") is True
    assert wrong.standard_rows != wrong.student_rows
    assert (
        evidence.get("only_in_standard_sample")
        or evidence.get("only_in_student_sample")
        or evidence.get("standard_sample_rows") != evidence.get("student_sample_rows")
    )


def test_live_native_wrong_case_has_mutation_repair_evidence(
    live_evidence: _LiveEvidence,
) -> None:
    for result in (live_evidence.wrong, live_evidence.repeated_wrong):
        summary = result.mutation_evidence.get("summary") or {}
        assert summary.get("executed", 0) > 0
        assert summary.get("fixed_by_replacement", 0) > 0
        case = live_evidence.case
        localized_tests = []
        for test in result.mutation_evidence.get("tests", []):
            scope = test.get("mutation_scope")
            normalized_scope = (
                {
                    str(item).strip().upper()
                    for item in scope
                    if str(item).strip()
                }
                if isinstance(scope, (list, tuple, set))
                else set()
            )
            if (
                test.get("fixed_by_replacement")
                and test.get("clause") == case.expected_mutation_clause
                and test.get("knowledge_point_id") == case.expected_kp
                and normalized_scope == {case.expected_mutation_clause}
                and test.get("query_scope") == "root"
            ):
                localized_tests.append(test)
        assert localized_tests


def test_live_native_repeated_run_is_stable_and_fixture_isolated(
    live_evidence: _LiveEvidence,
) -> None:
    first = live_evidence.wrong
    second = live_evidence.repeated_wrong
    assert first.is_equivalent == second.is_equivalent is False
    assert first.judge_status == second.judge_status == "WRONG"
    assert first.standard_rows == second.standard_rows
    assert first.student_rows == second.student_rows
    assert live_evidence.before == live_evidence.after
