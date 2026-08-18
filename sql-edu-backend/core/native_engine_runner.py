"""Synchronous native database runners used by the Phase 1 sandbox.

The orchestration layer owns SQL validation and dialect resolution.  This
module owns only native fixture isolation, execution, and cleanup.  Drivers
are imported lazily so installations that use the SQLite compatibility runner
do not need every vendor client installed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import importlib
import json
import math
import re
import secrets
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
import uuid


NativeQueryResult = tuple[list[str], list[tuple[Any, ...]]]

_BACKEND_ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "sql_server": "tsql",
    "oracle_free": "oracle",
    "oracle23ai": "oracle",
}
_DRIVER_MODULES = {
    "mysql": ("pymysql",),
    "postgres": ("psycopg", "psycopg2"),
    "tsql": ("pyodbc",),
    "oracle": ("oracledb",),
}
_URL_SCHEMES = {
    "mysql": frozenset({"mysql"}),
    "postgres": frozenset({"postgres", "postgresql"}),
    "tsql": frozenset({"mssql", "sqlserver", "tsql"}),
    "oracle": frozenset({"oracle"}),
}
_STATEMENT_TIMEOUT_MS = 3_000
_MAX_RESULT_ROWS = 10_000
_MAX_RESULT_BYTES = 8 * 1024 * 1024
_TSQL_DETERMINISTIC_SESSION_STATEMENTS = (
    "SET LANGUAGE us_english",
    "SET DATEFORMAT ymd",
    "SET DATEFIRST 7",
    "SET ANSI_NULLS ON",
    "SET ANSI_WARNINGS ON",
    "SET ANSI_PADDING ON",
    "SET ANSI_NULL_DFLT_ON ON",
    "SET ANSI_NULL_DFLT_OFF OFF",
    "SET QUOTED_IDENTIFIER ON",
    "SET ARITHABORT ON",
    "SET CONCAT_NULL_YIELDS_NULL ON",
    "SET NUMERIC_ROUNDABORT OFF",
    "SET IMPLICIT_TRANSACTIONS OFF",
    "SET XACT_ABORT OFF",
    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "SET NOCOUNT ON",
)


class NativeRunnerError(RuntimeError):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, backend: str, message: str):
        self.code = code
        self.backend = backend
        super().__init__(f"{code}: {message}")


class NativeConfigurationError(NativeRunnerError):
    """The selected native runner has missing or invalid configuration."""


class NativeDriverUnavailableError(NativeRunnerError):
    """No supported Python DB-API driver can be imported."""


class NativeQueryExecutionError(NativeRunnerError):
    """The native engine rejected the submitted query after setup succeeded."""


class NativeInfrastructureError(NativeRunnerError):
    """The native connection failed, so the query cannot receive a verdict."""


class NativeResultLimitError(NativeRunnerError):
    """A query result exceeded a configured sandbox resource limit."""


class NativeCleanupError(NativeRunnerError):
    """Fixture execution succeeded, but its isolated namespace did not clean up."""


def native_backend_available(backend: str, connection_url: str | None = None) -> bool:
    """Return whether a supported backend has a driver and usable URL shape.

    Omitting ``connection_url`` checks driver availability only.  Passing an
    empty or malformed URL returns ``False``; ``execute_native_query`` exposes
    the corresponding detailed exception.
    """

    normalized = _normalize_backend(backend)
    if normalized not in _DRIVER_MODULES:
        return False
    if connection_url is not None:
        try:
            _validate_connection_url(normalized, connection_url)
        except NativeConfigurationError:
            return False
    try:
        _load_driver(normalized)
    except NativeDriverUnavailableError:
        return False
    return True


@dataclass(frozen=True)
class NativeQueryOutcome:
    """One query's result or query-local failure from a shared native session."""

    sql: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    error: NativeRunnerError | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass
class NativeQuerySession:
    """A loaded native fixture that isolates every submitted query."""

    backend: str
    cursor: Any
    max_result_rows: int
    max_result_bytes: int
    _query_index: int = 0

    def execute(self, sql: str) -> NativeQueryResult:
        if not isinstance(sql, str) or not sql.strip():
            raise NativeConfigurationError(
                "NATIVE_SQL_REQUIRED", self.backend, "SQL must be a non-empty string"
            )
        self._query_index += 1
        savepoint = f"parseval_query_{self._query_index}"
        _create_query_savepoint(self.cursor, self.backend, savepoint)
        primary: BaseException | None = None
        try:
            return _execute_submitted_query(
                self.cursor,
                self.backend,
                sql,
                max_result_rows=self.max_result_rows,
                max_result_bytes=self.max_result_bytes,
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                _restore_query_savepoint(self.cursor, self.backend, savepoint)
            except BaseException as recovery_error:
                if primary is not None:
                    recovery = NativeRunnerError(
                        "NATIVE_SESSION_RECOVERY_FAILED",
                        self.backend,
                        str(recovery_error) or type(recovery_error).__name__,
                    )
                    if hasattr(recovery, "add_note"):
                        recovery.add_note(f"Original query failure: {primary}")
                    raise recovery from recovery_error
                raise NativeRunnerError(
                    "NATIVE_SESSION_RECOVERY_FAILED",
                    self.backend,
                    str(recovery_error) or type(recovery_error).__name__,
                ) from recovery_error


def execute_native_query(
    backend: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    sql: str,
    connection_url: str,
) -> NativeQueryResult:
    """Load one fixture and execute one query in an isolated native namespace.

    MySQL and SQL Server use short-lived databases, PostgreSQL uses an
    uncommitted transaction-scoped schema, and Oracle uses separate short-lived
    fixture-owner and read-only query users. Cleanup runs on every path and
    preserves primary errors.
    """

    outcomes = execute_native_queries(
        backend,
        schema,
        schema_types,
        rows,
        [sql],
        connection_url,
    )
    outcome = outcomes[0]
    if outcome.error is not None:  # defensive: the single-query wrapper stops on errors
        raise outcome.error
    return list(outcome.columns), list(outcome.rows)


def execute_native_queries(
    backend: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    queries: Iterable[str],
    connection_url: str,
    *,
    query_timeout_seconds: int = _STATEMENT_TIMEOUT_MS // 1000,
    max_result_rows: int = _MAX_RESULT_ROWS,
    max_result_bytes: int = _MAX_RESULT_BYTES,
    continue_on_error: bool = False,
) -> list[NativeQueryOutcome]:
    """Run several queries against one provisioned and loaded native fixture.

    Query-local failures can be returned in sequence when ``continue_on_error``
    is enabled.  A savepoint restores the fixture after each query so one
    submission cannot affect the next. Provisioning, fixture, and cleanup
    failures always raise because no later query can be evaluated reliably.
    """

    materialized_queries = list(queries)
    normalized = _normalize_backend(backend)
    if not materialized_queries:
        raise NativeConfigurationError(
            "NATIVE_SQL_REQUIRED", normalized, "at least one SQL query is required"
        )
    if any(not isinstance(sql, str) or not sql.strip() for sql in materialized_queries):
        raise NativeConfigurationError(
            "NATIVE_SQL_REQUIRED", normalized, "SQL must be a non-empty string"
        )
    outcomes: list[NativeQueryOutcome] = []
    with native_query_session(
        backend,
        schema,
        schema_types,
        rows,
        connection_url,
        query_timeout_seconds=query_timeout_seconds,
        max_result_rows=max_result_rows,
        max_result_bytes=max_result_bytes,
    ) as session:
        for sql in materialized_queries:
            try:
                columns, result_rows = session.execute(sql)
                outcomes.append(
                    NativeQueryOutcome(sql, tuple(columns), tuple(result_rows))
                )
            except NativeRunnerError as exc:
                if not continue_on_error or exc.code == "NATIVE_SESSION_RECOVERY_FAILED":
                    raise
                outcomes.append(NativeQueryOutcome(sql, error=exc))
    return outcomes


@contextmanager
def native_query_session(
    backend: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    connection_url: str,
    *,
    query_timeout_seconds: int = _STATEMENT_TIMEOUT_MS // 1000,
    max_result_rows: int = _MAX_RESULT_ROWS,
    max_result_bytes: int = _MAX_RESULT_BYTES,
) -> Iterator[NativeQuerySession]:
    """Provision one isolated native namespace and yield its reusable session."""

    normalized = _normalize_backend(backend)
    if normalized not in _DRIVER_MODULES:
        raise NativeConfigurationError(
            "NATIVE_BACKEND_UNSUPPORTED",
            normalized,
            f"unsupported native backend {backend!r}",
        )
    _validate_connection_url(normalized, connection_url)
    _validate_execution_limits(
        normalized, query_timeout_seconds, max_result_rows, max_result_bytes
    )
    driver = _load_driver(normalized)
    provisioner = {
        "mysql": _mysql_session,
        "postgres": _postgres_session,
        "tsql": _tsql_session,
        "oracle": _oracle_session,
    }[normalized]
    with provisioner(
        driver,
        schema,
        schema_types,
        rows,
        connection_url,
        query_timeout_seconds,
    ) as cursor:
        yield NativeQuerySession(
            normalized, cursor, max_result_rows, max_result_bytes
        )


def _validate_execution_limits(
    backend: str,
    query_timeout_seconds: int,
    max_result_rows: int,
    max_result_bytes: int,
) -> None:
    limits = {
        "query_timeout_seconds": query_timeout_seconds,
        "max_result_rows": max_result_rows,
        "max_result_bytes": max_result_bytes,
    }
    for name, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise NativeConfigurationError(
                "NATIVE_LIMIT_INVALID", backend, f"{name} must be a positive integer"
            )


def _normalize_backend(backend: str) -> str:
    value = str(backend or "").strip().lower().replace("-", "_")
    return _BACKEND_ALIASES.get(value, value)


def _validate_connection_url(backend: str, connection_url: str) -> None:
    if not isinstance(connection_url, str) or not connection_url.strip():
        raise NativeConfigurationError(
            "NATIVE_CONNECTION_URL_REQUIRED",
            backend,
            f"a connection URL is required for {backend}",
        )
    try:
        parsed = urlsplit(connection_url.strip())
        scheme = parsed.scheme.lower().split("+", 1)[0]
        parsed.port
    except (TypeError, ValueError) as exc:
        raise NativeConfigurationError(
            "NATIVE_CONNECTION_URL_INVALID", backend, "connection URL cannot be parsed"
        ) from exc
    if scheme not in _URL_SCHEMES[backend]:
        allowed = ", ".join(sorted(_URL_SCHEMES[backend]))
        raise NativeConfigurationError(
            "NATIVE_CONNECTION_URL_INVALID",
            backend,
            f"expected one of these URL schemes: {allowed}",
        )


def _load_driver(backend: str) -> Any:
    failures: list[Exception] = []
    for module_name in _DRIVER_MODULES[backend]:
        try:
            return importlib.import_module(module_name)
        except Exception as exc:  # import can fail when a native client library is absent
            failures.append(exc)
    modules = " or ".join(_DRIVER_MODULES[backend])
    error = NativeDriverUnavailableError(
        "NATIVE_DRIVER_UNAVAILABLE",
        backend,
        f"install and configure {modules}",
    )
    if failures:
        raise error from failures[-1]
    raise error


@contextmanager
def _mysql_session(
    driver: Any,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    connection_url: str,
    query_timeout_seconds: int,
) -> Iterator[Any]:
    params = _mysql_connection_params(connection_url)
    database = f"parseval_{uuid.uuid4().hex[:20]}"
    query_user = f"pv_{uuid.uuid4().hex[:20]}"
    query_password = secrets.token_urlsafe(24)
    admin_conn = driver.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=3,
        read_timeout=query_timeout_seconds,
        write_timeout=query_timeout_seconds,
    )
    admin_cursor = None
    query_conn = None
    query_cursor = None
    database_created = False
    user_created = False
    primary: BaseException | None = None
    try:
        admin_cursor = admin_conn.cursor()
        admin_cursor.execute(
            f"CREATE DATABASE {_quote_ident(database, 'mysql')} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        database_created = True
        admin_cursor.execute(f"USE {_quote_ident(database, 'mysql')}")
        _load_fixture(admin_cursor, "mysql", schema, schema_types, rows)
        account = f"{_quote_mysql_string(query_user)}@'%'"
        admin_cursor.execute(
            f"CREATE USER {account} IDENTIFIED BY {_quote_mysql_string(query_password)}"
        )
        user_created = True
        admin_cursor.execute(
            f"GRANT SELECT ON {_quote_ident(database, 'mysql')}.* TO {account}"
        )

        query_conn = driver.connect(
            host=params["host"],
            port=params["port"],
            user=query_user,
            password=query_password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=3,
            read_timeout=query_timeout_seconds,
            write_timeout=query_timeout_seconds,
        )
        query_cursor = query_conn.cursor()
        query_cursor.execute(
            f"SET SESSION MAX_EXECUTION_TIME = {query_timeout_seconds * 1000}"
        )
        yield query_cursor
    except BaseException as exc:
        primary = exc
        raise
    finally:
        steps: list[tuple[str, Callable[[], Any]]] = []
        if query_conn is not None:
            steps.append(("rollback query transaction", query_conn.rollback))
        if query_cursor is not None:
            steps.append(("close query cursor", query_cursor.close))
        if query_conn is not None:
            steps.append(("close query connection", query_conn.close))
        if database_created:
            steps.append(
                (
                    "drop isolated database",
                    lambda: admin_cursor.execute(
                        f"DROP DATABASE IF EXISTS {_quote_ident(database, 'mysql')}"
                    ),
                )
            )
        if user_created:
            steps.append(
                (
                    "drop isolated user",
                    lambda: admin_cursor.execute(
                        f"DROP USER IF EXISTS {_quote_mysql_string(query_user)}@'%'"
                    ),
                )
            )
        if admin_cursor is not None:
            steps.append(("close admin cursor", admin_cursor.close))
        steps.append(("close admin connection", admin_conn.close))
        _finish_cleanup("mysql", steps, primary)


def _mysql_connection_params(connection_url: str) -> dict[str, Any]:
    parsed = urlsplit(connection_url.strip())
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _quote_mysql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


@contextmanager
def _postgres_session(
    driver: Any,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    connection_url: str,
    query_timeout_seconds: int,
) -> Iterator[Any]:
    namespace = f"parseval_{uuid.uuid4().hex[:20]}"
    conn = driver.connect(_postgres_driver_url(connection_url), connect_timeout=3)
    cursor = None
    primary: BaseException | None = None
    try:
        if hasattr(conn, "autocommit"):
            conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(query_timeout_seconds * 1000),),
        )
        cursor.execute(
            "SELECT set_config('lock_timeout', %s, true)",
            (str(query_timeout_seconds * 1000),),
        )
        role = f"parseval_role_{uuid.uuid4().hex[:16]}"
        cursor.execute(f"CREATE ROLE {_quote_ident(role, 'postgres')} NOLOGIN")
        cursor.execute(f"CREATE SCHEMA {_quote_ident(namespace, 'postgres')}")
        # SET ROLE requires explicit membership when the admin connection is
        # not a superuser. Membership is transaction-scoped here and rolls
        # back with the isolated schema/role on cleanup.
        cursor.execute(
            f"GRANT {_quote_ident(role, 'postgres')} TO CURRENT_USER"
        )
        cursor.execute(
            f"SET LOCAL search_path TO {_quote_ident(namespace, 'postgres')}, pg_catalog"
        )
        _load_fixture(cursor, "postgres", schema, schema_types, rows)
        cursor.execute(
            f"GRANT USAGE ON SCHEMA {_quote_ident(namespace, 'postgres')} "
            f"TO {_quote_ident(role, 'postgres')}"
        )
        cursor.execute(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {_quote_ident(namespace, 'postgres')} "
            f"TO {_quote_ident(role, 'postgres')}"
        )
        cursor.execute(f"SET LOCAL ROLE {_quote_ident(role, 'postgres')}")
        yield cursor
    except BaseException as exc:
        primary = exc
        raise
    finally:
        steps: list[tuple[str, Callable[[], Any]]] = []
        if cursor is not None:
            steps.append(("close cursor", cursor.close))
        steps.extend((("rollback transaction", conn.rollback), ("close connection", conn.close)))
        _finish_cleanup("postgres", steps, primary)


def _postgres_driver_url(connection_url: str) -> str:
    parsed = urlsplit(connection_url.strip())
    scheme = parsed.scheme.lower()
    base_scheme = scheme.split("+", 1)[0]
    if base_scheme == "postgres":
        base_scheme = "postgresql"
    return urlunsplit((base_scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


@contextmanager
def _tsql_session(
    driver: Any,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    connection_url: str,
    query_timeout_seconds: int,
) -> Iterator[Any]:
    database = f"parseval_{uuid.uuid4().hex[:20]}"
    conn = driver.connect(
        _sqlserver_odbc_connection_string(connection_url),
        autocommit=True,
        timeout=3,
    )
    cursor = None
    database_created = False
    impersonating = False
    primary: BaseException | None = None
    try:
        # pyodbc exposes query timeout on the connection, not Cursor. Setting
        # cursor.timeout raises AttributeError with the real driver.
        try:
            conn.timeout = query_timeout_seconds
        except (AttributeError, TypeError):
            pass
        cursor = conn.cursor()
        cursor.execute("USE [master]")
        cursor.execute(f"CREATE DATABASE {_quote_ident(database, 'tsql')}")
        database_created = True
        cursor.execute(f"USE {_quote_ident(database, 'tsql')}")
        conn.autocommit = False
        for statement in _TSQL_DETERMINISTIC_SESSION_STATEMENTS:
            cursor.execute(statement)
        cursor.execute(f"SET LOCK_TIMEOUT {query_timeout_seconds * 1000}")
        _load_fixture(cursor, "tsql", schema, schema_types, rows)
        query_user = f"parseval_user_{uuid.uuid4().hex[:16]}"
        cursor.execute(
            f"CREATE USER {_quote_ident(query_user, 'tsql')} WITHOUT LOGIN"
        )
        cursor.execute(
            f"GRANT SELECT ON SCHEMA::[dbo] TO {_quote_ident(query_user, 'tsql')}"
        )
        cursor.execute(
            f"EXECUTE AS USER = {_quote_sqlserver_string(query_user)}"
        )
        impersonating = True
        yield cursor
    except BaseException as exc:
        primary = exc
        raise
    finally:
        steps: list[tuple[str, Callable[[], Any]]] = []
        if cursor is not None and impersonating:
            steps.append(("revert query user", lambda: cursor.execute("REVERT")))
        if not getattr(conn, "autocommit", True):
            steps.append(("rollback transaction", conn.rollback))
        if cursor is not None:
            steps.append(("close cursor", cursor.close))
        steps.append(("enable autocommit", lambda: setattr(conn, "autocommit", True)))
        if database_created:
            steps.append(("drop isolated database", lambda: _drop_sqlserver_database(conn, database)))
        steps.append(("close connection", conn.close))
        _finish_cleanup("tsql", steps, primary)


def _sqlserver_odbc_connection_string(connection_url: str) -> str:
    parsed = urlsplit(connection_url.strip())
    query = {key.lower(): values[-1] for key, values in parse_qs(parsed.query).items() if values}
    raw = query.get("odbc_connect")
    if raw:
        return raw

    driver = query.get("driver", "ODBC Driver 18 for SQL Server")
    server = parsed.hostname or query.get("server", "127.0.0.1")
    if parsed.port:
        server = f"{server},{parsed.port}"
    database = (parsed.path or "").lstrip("/") or query.get("database") or "master"
    parts = [
        f"DRIVER={_odbc_value(driver)}",
        f"SERVER={_odbc_value(server)}",
        f"DATABASE={_odbc_value(database)}",
    ]
    if parsed.username:
        parts.extend(
            (
                f"UID={_odbc_value(unquote(parsed.username))}",
                f"PWD={_odbc_value(unquote(parsed.password or ''))}",
            )
        )
    else:
        parts.append(f"Trusted_Connection={_odbc_value(query.get('trusted_connection', 'yes'))}")
    parts.extend(
        (
            f"Encrypt={_odbc_value(query.get('encrypt', 'yes'))}",
            "TrustServerCertificate="
            + _odbc_value(query.get("trustservercertificate", "yes")),
        )
    )
    return ";".join(parts)


def _odbc_value(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def _quote_sqlserver_string(value: str) -> str:
    return "N'" + value.replace("'", "''") + "'"


def _drop_sqlserver_database(conn: Any, database: str) -> None:
    cursor = conn.cursor()
    try:
        quoted = _quote_ident(database, "tsql")
        cursor.execute("USE [master]")
        cursor.execute(f"ALTER DATABASE {quoted} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cursor.execute(f"DROP DATABASE {quoted}")
    finally:
        cursor.close()


@contextmanager
def _oracle_session(
    driver: Any,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
    connection_url: str,
    query_timeout_seconds: int,
) -> Iterator[Any]:
    params = _oracle_connection_params(driver, connection_url)
    admin_kwargs = dict(params["connect_kwargs"])
    admin_kwargs.update(user=params["user"], password=params["password"])
    admin_conn = driver.connect(**admin_kwargs)
    if hasattr(admin_conn, "call_timeout"):
        admin_conn.call_timeout = query_timeout_seconds * 1000
    owner_username = f"PV_OWNER_{uuid.uuid4().hex[:16].upper()}"
    reader_username = f"PV_READER_{uuid.uuid4().hex[:16].upper()}"
    owner_password = secrets.token_urlsafe(24)
    reader_password = secrets.token_urlsafe(24)
    owner_conn = None
    owner_cursor = None
    reader_conn = None
    reader_cursor = None
    owner_created = False
    reader_created = False
    primary: BaseException | None = None
    try:
        admin_cursor = admin_conn.cursor()
        admin_primary: BaseException | None = None
        try:
            admin_cursor.execute(
                f"CREATE USER {_quote_ident(owner_username, 'oracle')} "
                f"IDENTIFIED BY {_quote_oracle_string(owner_password)}"
            )
            owner_created = True
            admin_cursor.execute(
                f"GRANT CREATE SESSION, CREATE TABLE "
                f"TO {_quote_ident(owner_username, 'oracle')}"
            )
            admin_cursor.execute(
                f"ALTER USER {_quote_ident(owner_username, 'oracle')} QUOTA 64M "
                f"ON {_quote_ident(params['tablespace'], 'oracle')}"
            )
            admin_cursor.execute(
                f"CREATE USER {_quote_ident(reader_username, 'oracle')} "
                f"IDENTIFIED BY {_quote_oracle_string(reader_password)}"
            )
            reader_created = True
            admin_cursor.execute(
                f"GRANT CREATE SESSION TO {_quote_ident(reader_username, 'oracle')}"
            )
        except BaseException as exc:
            admin_primary = exc
            raise
        finally:
            _finish_cleanup(
                "oracle", [("close admin cursor", admin_cursor.close)], admin_primary
            )

        owner_kwargs = dict(params["connect_kwargs"])
        owner_kwargs.pop("mode", None)
        owner_kwargs.update(user=owner_username, password=owner_password)
        owner_conn = driver.connect(**owner_kwargs)
        if hasattr(owner_conn, "call_timeout"):
            owner_conn.call_timeout = query_timeout_seconds * 1000
        owner_cursor = owner_conn.cursor()
        _configure_oracle_session(owner_cursor)
        _load_fixture(owner_cursor, "oracle", schema, schema_types, rows)
        if hasattr(owner_conn, "commit"):
            owner_conn.commit()
        for table in schema:
            if table not in rows:
                continue
            fixture_table = _fold_fixture_identifier(table, "oracle")
            owner_cursor.execute(
                f"GRANT SELECT ON {_quote_ident(fixture_table, 'oracle')} "
                f"TO {_quote_ident(reader_username, 'oracle')}"
            )

        reader_kwargs = dict(params["connect_kwargs"])
        reader_kwargs.pop("mode", None)
        reader_kwargs.update(user=reader_username, password=reader_password)
        reader_conn = driver.connect(**reader_kwargs)
        if hasattr(reader_conn, "call_timeout"):
            reader_conn.call_timeout = query_timeout_seconds * 1000
        reader_cursor = reader_conn.cursor()
        _configure_oracle_session(
            reader_cursor,
            current_schema=owner_username,
        )
        yield reader_cursor
    except BaseException as exc:
        primary = exc
        raise
    finally:
        steps: list[tuple[str, Callable[[], Any]]] = []
        if reader_conn is not None:
            steps.append(("rollback reader transaction", reader_conn.rollback))
        if reader_cursor is not None:
            steps.append(("close reader cursor", reader_cursor.close))
        if reader_conn is not None:
            steps.append(("close reader connection", reader_conn.close))
        if owner_conn is not None:
            steps.append(("rollback owner transaction", owner_conn.rollback))
        if owner_cursor is not None:
            steps.append(("close owner cursor", owner_cursor.close))
        if owner_conn is not None:
            steps.append(("close owner connection", owner_conn.close))
        if reader_created:
            steps.append(
                (
                    "drop isolated reader",
                    lambda: _drop_oracle_user(admin_conn, reader_username),
                )
            )
        if owner_created:
            steps.append(
                (
                    "drop isolated owner",
                    lambda: _drop_oracle_user(admin_conn, owner_username),
                )
            )
        steps.append(("close admin connection", admin_conn.close))
        _finish_cleanup("oracle", steps, primary)


def _configure_oracle_session(
    cursor: Any,
    *,
    current_schema: str | None = None,
) -> None:
    cursor.execute("ALTER SESSION SET TIME_ZONE = '+00:00'")
    cursor.execute("ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'")
    if current_schema is not None:
        cursor.execute(
            f"ALTER SESSION SET CURRENT_SCHEMA = "
            f"{_quote_ident(current_schema, 'oracle')}"
        )


def _oracle_connection_params(driver: Any, connection_url: str) -> dict[str, Any]:
    parsed = urlsplit(connection_url.strip())
    query = {key.lower(): values[-1] for key, values in parse_qs(parsed.query).items() if values}
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not user:
        raise NativeConfigurationError(
            "NATIVE_CONNECTION_URL_INVALID", "oracle", "Oracle admin user is required"
        )

    dsn = query.get("dsn")
    if not dsn:
        host = parsed.hostname
        service_name = query.get("service_name") or (parsed.path or "").strip("/")
        sid = query.get("sid")
        if not host or not (service_name or sid):
            raise NativeConfigurationError(
                "NATIVE_CONNECTION_URL_INVALID",
                "oracle",
                "Oracle host and service name (or sid/dsn) are required",
            )
        dsn = driver.makedsn(
            host,
            parsed.port or 1521,
            **({"sid": sid} if sid else {"service_name": service_name}),
        )

    connect_kwargs: dict[str, Any] = {"dsn": dsn}
    for option in ("config_dir", "wallet_location", "wallet_password"):
        if query.get(option):
            connect_kwargs[option] = query[option]
    mode = query.get("mode", "").lower()
    if mode == "sysdba" and hasattr(driver, "AUTH_MODE_SYSDBA"):
        connect_kwargs["mode"] = driver.AUTH_MODE_SYSDBA
    return {
        "user": user,
        "password": password,
        "tablespace": query.get("tablespace", "USERS"),
        "connect_kwargs": connect_kwargs,
    }


def _quote_oracle_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _drop_oracle_user(admin_conn: Any, username: str) -> None:
    cursor = admin_conn.cursor()
    try:
        cursor.execute(f"DROP USER {_quote_ident(username, 'oracle')} CASCADE")
    finally:
        cursor.close()


def _load_fixture(
    cursor: Any,
    backend: str,
    schema: dict[str, list[str]],
    schema_types: dict[str, dict[str, str]],
    rows: dict[str, list[dict[str, Any]]],
) -> None:
    placeholder = {"mysql": "%s", "postgres": "%s", "tsql": "?", "oracle": None}[
        backend
    ]
    for table, columns in schema.items():
        if table not in rows:
            continue
        if not columns:
            raise NativeConfigurationError(
                "NATIVE_FIXTURE_INVALID", backend, f"table {table!r} has no columns"
            )
        fixture_table = _fold_fixture_identifier(table, backend)
        fixture_columns = [_fold_fixture_identifier(column, backend) for column in columns]
        specs = [
            _column_spec(
                backend,
                (schema_types.get(table) or {}).get(column),
                (row.get(column) for row in rows.get(table, [])),
                column,
            )
            for column in columns
        ]
        definitions = ", ".join(
            f"{_quote_ident(fixture_column, backend)} {spec.sql_type}"
            for fixture_column, spec in zip(fixture_columns, specs)
        )
        cursor.execute(f"CREATE TABLE {_quote_ident(fixture_table, backend)} ({definitions})")
        values = [
            tuple(
                _coerce_parameter(row.get(column), backend, spec)
                for column, spec in zip(columns, specs)
            )
            for row in rows.get(table, [])
        ]
        if not values:
            continue
        quoted_columns = ", ".join(
            _quote_ident(fixture_column, backend) for fixture_column in fixture_columns
        )
        if backend == "oracle":
            placeholders = ", ".join(f":{index}" for index in range(1, len(columns) + 1))
        elif backend == "postgres":
            placeholders = ", ".join(_postgres_placeholder(spec) for spec in specs)
        else:
            placeholders = ", ".join(placeholder for _ in columns)
        cursor.executemany(
            f"INSERT INTO {_quote_ident(fixture_table, backend)} ({quoted_columns}) "
            f"VALUES ({placeholders})",
            values,
        )


def _quote_ident(name: str, backend: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise NativeConfigurationError(
            "NATIVE_IDENTIFIER_INVALID", backend, "table and column names must be non-empty"
        )
    if backend == "tsql":
        return "[" + name.replace("]", "]]") + "]"
    if backend == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _fold_fixture_identifier(name: str, backend: str) -> str:
    """Match each engine's unquoted identifier folding for parsed schema names.

    The compact schema parser intentionally removes source quote markers, so
    native fixture DDL cannot distinguish a quoted mixed-case identifier from
    an unquoted one.  Educational schemas overwhelmingly use unquoted names;
    folding here preserves their native lookup semantics deterministically.
    """

    if backend == "postgres":
        return name.lower()
    if backend == "oracle":
        return name.upper()
    return name


@dataclass(frozen=True)
class _ColumnSpec:
    sql_type: str
    kind: str


def _postgres_placeholder(spec: _ColumnSpec) -> str:
    if spec.kind == "json":
        return "%s::jsonb"
    if spec.kind == "uuid":
        return "%s::uuid"
    return "%s"


def _column_spec(
    backend: str,
    type_hint: str | None,
    values: Iterable[Any],
    column: str,
) -> _ColumnSpec:
    materialized = [value for value in values if value is not None]
    hint = (type_hint or "").strip().upper()
    kind = _kind_from_hint(hint, backend) or _infer_kind(materialized, column)
    precision = _numeric_precision(hint)

    if backend == "mysql":
        types = {
            "bool": "BOOLEAN",
            "int": "BIGINT",
            "decimal": f"DECIMAL({precision[0]},{precision[1]})" if precision else "DECIMAL(38,10)",
            "float": "DOUBLE",
            "date": "DATE",
            "time": "TIME(6)",
            "datetime": "DATETIME(6)",
            "datetime_tz": "DATETIME(6)",
            "binary": "LONGBLOB",
            "json": "JSON",
            "uuid": "CHAR(36)",
            "text": "LONGTEXT",
        }
        return _ColumnSpec(types.get(kind, "LONGTEXT"), kind)
    if backend == "postgres":
        types = {
            "bool": "BOOLEAN",
            "int": "BIGINT",
            "decimal": f"NUMERIC({precision[0]},{precision[1]})" if precision else "NUMERIC(38,10)",
            "float": "DOUBLE PRECISION",
            "date": "DATE",
            "time": "TIME",
            "datetime": "TIMESTAMP",
            "datetime_tz": "TIMESTAMPTZ",
            "binary": "BYTEA",
            "json": "JSONB",
            "uuid": "UUID",
            "text": "TEXT",
        }
        if kind.startswith("array_"):
            array_base = {
                "array_int": "BIGINT[]",
                "array_decimal": "NUMERIC[]",
                "array_bool": "BOOLEAN[]",
                "array_text": "TEXT[]",
            }.get(kind, "TEXT[]")
            return _ColumnSpec(array_base, kind)
        return _ColumnSpec(types[kind], kind)
    if backend == "tsql":
        types = {
            "bool": "BIT",
            "int": "BIGINT",
            "decimal": f"DECIMAL({precision[0]},{precision[1]})" if precision else "DECIMAL(38,10)",
            "float": "FLOAT",
            "date": "DATE",
            "time": "TIME(6)",
            "datetime": "DATETIME2(6)",
            "datetime_tz": "DATETIMEOFFSET(6)",
            "binary": "VARBINARY(MAX)",
            "json": "NVARCHAR(MAX)",
            "uuid": "UNIQUEIDENTIFIER",
            "text": "NVARCHAR(MAX)",
        }
        return _ColumnSpec(types.get(kind, "NVARCHAR(MAX)"), kind)
    types = {
        "bool": "NUMBER(1)",
        "int": "NUMBER(19)",
        "decimal": f"NUMBER({precision[0]},{precision[1]})" if precision else "NUMBER(38,10)",
        "float": "BINARY_DOUBLE",
        "date": "DATE",
        "time": "VARCHAR2(32 CHAR)",
        "datetime": "TIMESTAMP(6)",
        "datetime_tz": "TIMESTAMP(6) WITH TIME ZONE",
        "binary": "BLOB",
        "json": "CLOB",
        "uuid": "VARCHAR2(36 CHAR)",
        "text": "VARCHAR2(4000 CHAR)",
    }
    return _ColumnSpec(types.get(kind, "VARCHAR2(4000 CHAR)"), kind)


def _kind_from_hint(hint: str, backend: str) -> str | None:
    if not hint:
        return None
    is_array = backend == "postgres" and bool(re.search(r"\[\s*\]", hint))
    if re.search(r"\bJSONB?\b", hint):
        return "array_text" if is_array else "json"
    if re.search(r"\b(UUID|UNIQUEIDENTIFIER)\b", hint):
        return "array_text" if is_array else "uuid"
    if re.search(r"\b(BLOB|BYTEA|BINARY|VARBINARY|RAW)\b", hint):
        return "array_text" if is_array else "binary"
    if re.search(r"\b(BOOL|BOOLEAN|BIT)\b", hint):
        base = "bool"
    elif re.search(r"\b(TIMESTAMPTZ|DATETIMEOFFSET)\b|TIMESTAMP\s+WITH\s+TIME\s+ZONE", hint):
        base = "datetime_tz"
    elif re.search(r"\b(TIMESTAMP|DATETIME|DATETIME2)\b", hint):
        base = "datetime"
    elif re.search(r"\bDATE\b", hint):
        base = "date"
    elif re.search(r"\bTIME\b", hint):
        base = "time"
    elif re.search(r"\b(DECIMAL|NUMERIC|NUMBER|MONEY)\b", hint):
        base = "decimal"
    elif re.search(r"\b(DOUBLE|FLOAT|REAL|BINARY_DOUBLE)\b", hint):
        base = "float"
    elif re.search(r"\b(SMALLINT|BIGINT|INTEGER|INT|SERIAL)\b", hint):
        base = "int"
    elif re.search(r"\b(CHAR|VARCHAR|VARCHAR2|NVARCHAR|TEXT|CLOB|ENUM)\b", hint):
        base = "text"
    else:
        return None
    return f"array_{base}" if is_array else base


def _numeric_precision(hint: str) -> tuple[int, int] | None:
    match = re.search(r"\b(?:DECIMAL|NUMERIC|NUMBER)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", hint)
    if not match:
        return None
    precision = max(1, min(38, int(match.group(1))))
    scale = max(0, min(precision, int(match.group(2) or 0)))
    return precision, scale


def _infer_kind(values: list[Any], column: str) -> str:
    if values:
        if all(isinstance(value, bool) for value in values):
            return "bool"
        if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return "int"
        if all(isinstance(value, Decimal) for value in values):
            return "decimal"
        if all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in values):
            return "float"
        if all(isinstance(value, datetime) for value in values):
            return "datetime"
        if all(isinstance(value, date) and not isinstance(value, datetime) for value in values):
            return "date"
        if all(isinstance(value, time) for value in values):
            return "time"
        if all(isinstance(value, (bytes, bytearray, memoryview)) for value in values):
            return "binary"
        if all(isinstance(value, (dict, list)) for value in values):
            return "json"
        if all(_looks_like_datetime(value) for value in values):
            return "datetime"
    normalized = re.sub(r"[^a-z0-9_]", "", column.lower())
    if normalized == "id" or normalized.endswith("_id") or re.search(
        r"(?:count|age|rank|year|month|day)$", normalized
    ):
        return "int"
    if re.search(r"(?:amount|price|salary|score|total)$", normalized):
        return "float"
    if re.search(r"(?:date|time|_at)$", normalized):
        return "datetime"
    return "text"


def _looks_like_datetime(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?", value.strip())
    )


def _coerce_parameter(value: Any, backend: str, spec: _ColumnSpec) -> Any:
    if value is None:
        return None
    if spec.kind.startswith("array_"):
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    if spec.kind == "json":
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if spec.kind == "uuid" and backend == "postgres" and isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    if spec.kind == "binary" and isinstance(value, memoryview):
        return value.tobytes()
    if spec.kind == "binary" and isinstance(value, bytearray):
        return bytes(value)
    if spec.kind == "bool" and backend == "oracle":
        return 1 if bool(value) else 0
    if spec.kind in {"date", "datetime", "datetime_tz"} and isinstance(value, str):
        try:
            if spec.kind == "date":
                return date.fromisoformat(value.strip()[:10])
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _read_result(
    cursor: Any,
    backend: str = "unknown",
    *,
    max_result_rows: int = _MAX_RESULT_ROWS,
    max_result_bytes: int = _MAX_RESULT_BYTES,
) -> NativeQueryResult:
    description = cursor.description or []
    columns = [str(item[0]) for item in description]
    if not description:
        return columns, []
    result_rows: list[tuple[Any, ...]] = []
    approximate_bytes = sum(len(column.encode("utf-8")) for column in columns)
    while True:
        if hasattr(cursor, "fetchmany"):
            batch = cursor.fetchmany(min(256, max_result_rows - len(result_rows) + 1))
        else:
            batch = cursor.fetchall() if not result_rows else []
        if not batch:
            break
        for raw_row in batch:
            if len(result_rows) >= max_result_rows:
                raise NativeResultLimitError(
                    "NATIVE_RESULT_ROW_LIMIT_EXCEEDED",
                    backend,
                    f"query returned more than {max_result_rows} rows",
                )
            row = tuple(_normalize_cell(value) for value in raw_row)
            approximate_bytes += _approximate_result_size(row)
            if approximate_bytes > max_result_bytes:
                raise NativeResultLimitError(
                    "NATIVE_RESULT_BYTE_LIMIT_EXCEEDED",
                    backend,
                    f"query result exceeded {max_result_bytes} bytes",
                )
            result_rows.append(row)
    return columns, result_rows


def _execute_submitted_query(
    cursor: Any,
    backend: str,
    sql: str,
    *,
    max_result_rows: int = _MAX_RESULT_ROWS,
    max_result_bytes: int = _MAX_RESULT_BYTES,
) -> NativeQueryResult:
    """Separate query rejection from runner provisioning and fixture failures."""
    try:
        cursor.execute(sql)
        return _read_result(
            cursor,
            backend,
            max_result_rows=max_result_rows,
            max_result_bytes=max_result_bytes,
        )
    except NativeRunnerError:
        raise
    except Exception as exc:
        if _is_connection_failure(exc):
            raise NativeInfrastructureError(
                "NATIVE_CONNECTION_LOST",
                backend,
                str(exc) or type(exc).__name__,
            ) from exc
        raise NativeQueryExecutionError(
            "NATIVE_QUERY_FAILED",
            backend,
            str(exc) or type(exc).__name__,
        ) from exc


def _is_connection_failure(exc: BaseException) -> bool:
    """Recognize DB-API transport/session failures without exposing drivers here."""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (ConnectionError, BrokenPipeError)):
            return True
        sqlstate = str(getattr(current, "sqlstate", "") or "").upper()
        if sqlstate.startswith("08"):
            return True
        for arg in getattr(current, "args", ()):
            if isinstance(arg, str) and arg.upper().startswith("08"):
                return True
            if isinstance(arg, int) and arg in {2002, 2003, 2006, 2013, 2055}:
                return True
        text = str(current).lower()
        if any(
            marker in text
            for marker in (
                "connection refused",
                "connection reset",
                "connection is closed",
                "connection already closed",
                "server has gone away",
                "lost connection",
                "communication link failure",
                "broken pipe",
                "not connected to oracle",
                "ora-01012",
                "ora-03113",
                "ora-03114",
                "ora-03135",
                "dpy-4011",
            )
        ):
            return True
        current = current.__cause__
    return False


def _normalize_cell(value: Any) -> Any:
    if "oracledb" in type(value).__module__ and hasattr(value, "read"):
        return _normalize_cell(value.read())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return _normalize_cell(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return value
    if isinstance(value, dict):
        items = [
            (_normalize_cell(key), _normalize_cell(item)) for key, item in value.items()
        ]
        return tuple(sorted(items, key=lambda item: _stable_sort_key(item[0])))
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_cell(item) for item in value)
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_cell(item) for item in value]
        return tuple(sorted(normalized, key=_stable_sort_key))
    return value


def _stable_sort_key(value: Any) -> tuple[str, str]:
    return type(value).__qualname__, repr(value)


def _approximate_result_size(row: tuple[Any, ...]) -> int:
    return len(repr(row).encode("utf-8", errors="replace"))


def _create_query_savepoint(cursor: Any, backend: str, savepoint: str) -> None:
    quoted = _quote_ident(savepoint, backend)
    if backend == "tsql":
        cursor.execute(f"SAVE TRANSACTION {quoted}")
    else:
        cursor.execute(f"SAVEPOINT {quoted}")


def _restore_query_savepoint(cursor: Any, backend: str, savepoint: str) -> None:
    quoted = _quote_ident(savepoint, backend)
    if backend == "tsql":
        cursor.execute(f"ROLLBACK TRANSACTION {quoted}")
        return
    if backend == "oracle":
        cursor.execute(f"ROLLBACK TO {quoted}")
        return
    cursor.execute(f"ROLLBACK TO SAVEPOINT {quoted}")
    cursor.execute(f"RELEASE SAVEPOINT {quoted}")


def _finish_cleanup(
    backend: str,
    steps: list[tuple[str, Callable[[], Any]]],
    primary: BaseException | None,
) -> None:
    failures: list[tuple[str, BaseException]] = []
    for label, action in steps:
        try:
            action()
        except BaseException as exc:
            failures.append((label, exc))
    if not failures:
        return
    summary = "; ".join(f"{label}: {type(exc).__name__}: {exc}" for label, exc in failures)
    if primary is not None:
        if hasattr(primary, "add_note"):
            primary.add_note(f"Native {backend} cleanup also failed: {summary}")
        return
    raise NativeCleanupError(
        "NATIVE_CLEANUP_FAILED", backend, summary
    ) from failures[0][1]
