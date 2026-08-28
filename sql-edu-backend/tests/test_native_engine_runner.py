"""Unit coverage for native runner lifecycle without requiring live databases."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote_plus

import pytest
from sqlglot import parse_one

import core.native_engine_runner as runner
import core.parseval_data_generator as generator


class FakeCursor:
    def __init__(self, log: list[tuple], *, fail_query: bool = False):
        self.log = log
        self.fail_query = fail_query
        self.description = None
        self.closed = False
        self._fetched = False

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
        if self.fail_query and sql in {
            "SELECT answer FROM t",
            "SELECT broken FROM t",
        }:
            raise RuntimeError("query exploded")
        if sql.startswith("SELECT ") and "set_config" not in sql:
            self.description = [("answer",)]
            self._fetched = False
        else:
            self.description = None
        return self

    def executemany(self, sql, values):
        self.log.append(("executemany", sql, list(values)))
        self.description = None
        return self

    def fetchall(self):
        self.log.append(("fetchall",))
        if self._fetched:
            return []
        self._fetched = True
        return [(Decimal("42.00"),)]

    def fetchmany(self, _size):
        self.log.append(("fetchmany", _size))
        return self.fetchall()

    def close(self):
        self.closed = True
        self.log.append(("cursor_close",))


class FakeConnection:
    def __init__(
        self,
        log: list[tuple],
        *,
        autocommit=False,
        fail_query: bool = False,
        fail_rollback: bool = False,
    ):
        self.log = log
        self.autocommit = autocommit
        self.fail_query = fail_query
        self.fail_rollback = fail_rollback
        self.closed = False
        self.timeout = None
        self.call_timeout = None
        self.cursors: list[FakeCursor] = []

    def cursor(self):
        cursor = FakeCursor(self.log, fail_query=self.fail_query and not self.cursors)
        self.cursors.append(cursor)
        self.log.append(("cursor",))
        return cursor

    def rollback(self):
        self.log.append(("rollback",))
        if self.fail_rollback:
            raise RuntimeError("rollback exploded")

    def close(self):
        self.closed = True
        self.log.append(("connection_close",))


def _fixture():
    return (
        {"T": ["answer", "payload"]},
        {"T": {"answer": "NUMERIC(12,2)", "payload": "JSON"}},
        {"T": [{"answer": Decimal("42.00"), "payload": {"ok": True}}]},
    )


def test_native_backend_available_checks_url_and_supported_driver(monkeypatch):
    imported: list[str] = []

    def import_driver(name):
        imported.append(name)
        if name == "psycopg":
            return SimpleNamespace(__name__=name)
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(runner.importlib, "import_module", import_driver)

    assert runner.native_backend_available("postgresql") is True
    assert runner.native_backend_available("pg", "postgresql://user:pw@db/test") is True
    assert runner.native_backend_available("postgres", "mysql://db/test") is False
    assert runner.native_backend_available("sqlite") is False
    assert imported == ["psycopg", "psycopg"]


def test_native_backend_available_rejects_unloadable_native_library(monkeypatch):
    def broken_import(name):
        raise ImportError(f"{name}: libodbc.so.2 cannot be opened")

    monkeypatch.setattr(runner.importlib, "import_module", broken_import)

    assert runner.native_backend_available(
        "tsql",
        "mssql://sa:pw@db/master",
    ) is False


@pytest.mark.parametrize(
    ("backend", "url", "code"),
    [
        ("postgres", "", "NATIVE_CONNECTION_URL_REQUIRED"),
        ("tsql", "postgresql://db/test", "NATIVE_CONNECTION_URL_INVALID"),
        ("sqlite", "sqlite:///:memory:", "NATIVE_BACKEND_UNSUPPORTED"),
    ],
)
def test_execute_native_query_rejects_bad_configuration_before_driver_import(
    backend, url, code
):
    with pytest.raises(runner.NativeConfigurationError) as caught:
        runner.execute_native_query(backend, {}, {}, {}, "SELECT 1", url)

    assert caught.value.code == code


def test_execute_native_query_reports_missing_driver(monkeypatch):
    def missing(_name):
        raise ModuleNotFoundError("missing")

    monkeypatch.setattr(runner.importlib, "import_module", missing)

    with pytest.raises(runner.NativeDriverUnavailableError) as caught:
        runner.execute_native_query(
            "postgres", {}, {}, {}, "SELECT 1", "postgresql://user:pw@db/test"
        )

    assert caught.value.code == "NATIVE_DRIVER_UNAVAILABLE"
    assert caught.value.backend == "postgres"


def test_submitted_query_error_has_stable_query_level_type():
    class RejectingCursor:
        description = None

        def execute(self, _sql):
            raise RuntimeError("column does not exist")

    with pytest.raises(runner.NativeQueryExecutionError) as caught:
        runner._execute_submitted_query(
            RejectingCursor(), "postgres", "SELECT missing FROM users"
        )

    assert caught.value.code == "NATIVE_QUERY_FAILED"
    assert caught.value.backend == "postgres"
    assert "column does not exist" in str(caught.value)


def test_postgres_uses_transaction_scoped_schema_and_rolls_back(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log)
    driver = SimpleNamespace(connect=lambda url, **kwargs: _record_connect(log, connection, url, kwargs))
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)
    schema, types, rows = _fixture()

    columns, result_rows = runner.execute_native_query(
        "postgres",
        schema,
        types,
        rows,
        "SELECT answer FROM t",
        "postgresql+psycopg://user:pw@db/course",
    )

    assert columns == ["answer"]
    assert result_rows == [(Decimal("42.00"),)]
    statements = [entry[1] for entry in log if entry[0] == "execute"]
    assert any(statement.startswith('CREATE SCHEMA "parseval_') for statement in statements)
    assert any(statement.startswith('SET LOCAL search_path TO "parseval_') for statement in statements)
    assert 'CREATE TABLE "t" ("answer" NUMERIC(12,2), "payload" JSONB)' in statements
    insert = next(entry for entry in log if entry[0] == "executemany")
    assert insert[1] == 'INSERT INTO "t" ("answer", "payload") VALUES (%s, %s::jsonb)'
    assert insert[2] == [(Decimal("42.00"), '{"ok":true}')]
    assert ("rollback",) in log
    assert connection.closed is True


def test_primary_postgres_error_survives_cleanup_failure(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log, fail_query=True, fail_rollback=True)
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)
    schema, types, rows = _fixture()

    with pytest.raises(RuntimeError, match="query exploded") as caught:
        runner.execute_native_query(
            "postgres",
            schema,
            types,
            rows,
            "SELECT answer FROM t",
            "postgresql://user:pw@db/course",
        )

    assert any("cleanup also failed" in note for note in getattr(caught.value, "__notes__", []))
    assert connection.closed is True


def test_sqlserver_uses_isolated_database_and_always_drops_it(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log, autocommit=True)
    driver = SimpleNamespace(
        connect=lambda connection_string, **kwargs: _record_connect(
            log, connection, connection_string, kwargs
        )
    )
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)

    columns, result_rows = runner.execute_native_query(
        "sqlserver",
        {"t": ["answer"]},
        {"t": {"answer": "BIGINT"}},
        {"t": [{"answer": 42}]},
        "SELECT answer FROM t",
        "mssql+pyodbc://sa:p%40ss@db:1433/master?driver=ODBC+Driver+18+for+SQL+Server",
    )

    assert columns == ["answer"]
    assert result_rows == [(Decimal("42.00"),)]
    statements = [entry[1] for entry in log if entry[0] == "execute"]
    create = next(statement for statement in statements if statement.startswith("CREATE DATABASE"))
    database = create.removeprefix("CREATE DATABASE ")
    assert database.startswith("[parseval_") and database.endswith("]")
    assert f"ALTER DATABASE {database} SET SINGLE_USER WITH ROLLBACK IMMEDIATE" in statements
    assert f"DROP DATABASE {database}" in statements
    assert "CREATE TABLE [t] ([answer] BIGINT)" in statements
    option_positions = [
        statements.index(statement)
        for statement in runner._TSQL_DETERMINISTIC_SESSION_STATEMENTS
    ]
    assert max(option_positions) < statements.index("CREATE TABLE [t] ([answer] BIGINT)")
    assert max(option_positions) < next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("EXECUTE AS USER")
    )
    connect_string = log[0][1]
    assert "PWD={p@ss}" in connect_string
    assert connection.autocommit is True
    assert connection.closed is True


def test_sqlserver_accepts_url_encoded_odbc_connection_string():
    raw = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=db;UID=sa;PWD=p@ss"
    url = "mssql+pyodbc:///?odbc_connect=" + quote_plus(raw)

    assert runner._sqlserver_odbc_connection_string(url) == raw


def test_oracle_executes_as_read_only_reader_and_drops_both_users(monkeypatch):
    log: list[tuple] = []

    class TaggedCursor(FakeCursor):
        def __init__(self, tag):
            super().__init__(log)
            self.tag = tag
            self._single_row = None

        def execute(self, sql, params=None):
            log.append((self.tag, "execute", sql, params))
            if sql == "SELECT VERSION(), @@lower_case_table_names":
                self.description = [("version",), ("lower_case_table_names",)]
                self._single_row = ("8.0.46", 0)
                return self
            if sql.startswith("SELECT "):
                self.description = [("answer",)]
                self._fetched = False
            else:
                self.description = None
            return self

        def executemany(self, sql, values):
            log.append((self.tag, "executemany", sql, list(values)))
            self.description = None
            return self

    class TaggedConnection(FakeConnection):
        def __init__(self, tag):
            super().__init__(log)
            self.tag = tag

        def cursor(self):
            cursor = TaggedCursor(self.tag)
            self.cursors.append(cursor)
            log.append((self.tag, "cursor"))
            return cursor

        def commit(self):
            log.append((self.tag, "commit"))

        def rollback(self):
            log.append((self.tag, "rollback"))

        def close(self):
            self.closed = True
            log.append((self.tag, "connection_close"))

    admin = TaggedConnection("admin")
    owner = TaggedConnection("owner")
    reader = TaggedConnection("reader")

    def connect(**kwargs):
        log.append(("connect", kwargs))
        if kwargs["user"] == "sys":
            return admin
        if kwargs["user"].startswith("PV_OWNER_"):
            return owner
        if kwargs["user"].startswith("PV_READER_"):
            return reader
        raise AssertionError(f"unexpected Oracle user {kwargs['user']!r}")

    driver = SimpleNamespace(
        AUTH_MODE_SYSDBA=99,
        connect=connect,
        makedsn=lambda host, port, **kwargs: f"{host}:{port}/{kwargs}",
    )
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)

    columns, result_rows = runner.execute_native_query(
        "oracle",
        {"t": ["answer"]},
        {"t": {"answer": "NUMBER(12,2)"}},
        {"t": [{"answer": Decimal("42.00")}]},
        "SELECT answer FROM t",
        "oracle://sys:secret@db:1521/FREEPDB1?mode=sysdba",
    )

    assert columns == ["answer"]
    assert result_rows == [(Decimal("42.00"),)]
    admin_statements = [entry[2] for entry in log if entry[:2] == ("admin", "execute")]
    owner_statements = [entry[2] for entry in log if entry[:2] == ("owner", "execute")]
    reader_statements = [entry[2] for entry in log if entry[:2] == ("reader", "execute")]
    create_users = [
        statement.split()[2]
        for statement in admin_statements
        if statement.startswith("CREATE USER")
    ]
    owner_username = next(name for name in create_users if name.startswith('"PV_OWNER_'))
    reader_username = next(name for name in create_users if name.startswith('"PV_READER_'))
    assert f"DROP USER {reader_username} CASCADE" in admin_statements
    assert f"DROP USER {owner_username} CASCADE" in admin_statements
    assert admin_statements.index(f"DROP USER {reader_username} CASCADE") < admin_statements.index(
        f"DROP USER {owner_username} CASCADE"
    )
    assert 'CREATE TABLE "T" ("ANSWER" NUMBER(12,2))' in owner_statements
    assert any(
        statement.startswith('GRANT SELECT ON "T" TO "PV_READER_')
        for statement in owner_statements
    )
    assert any(
        statement.startswith('ALTER SESSION SET CURRENT_SCHEMA = "PV_OWNER_')
        for statement in reader_statements
    )
    assert "SELECT answer FROM t" in reader_statements
    assert "SELECT answer FROM t" not in owner_statements
    assert "SELECT answer FROM t" not in admin_statements
    assert not any(statement.startswith("CREATE TABLE") for statement in reader_statements)
    assert not any(statement.startswith("GRANT ") for statement in reader_statements)
    connections = [entry[1] for entry in log if entry[0] == "connect"]
    assert connections[0]["mode"] == 99
    assert all("mode" not in connection for connection in connections[1:])
    assert len({connection["user"] for connection in connections}) == 3
    assert admin.call_timeout == 3_000
    assert owner.call_timeout == 3_000
    assert reader.call_timeout == 3_000
    assert ("owner", "commit") in log
    assert ("reader", "rollback") in log
    assert owner.closed is True
    assert reader.closed is True
    assert admin.closed is True


def test_oracle_partial_reader_setup_still_drops_reader_and_owner(monkeypatch):
    log: list[tuple] = []

    class FailingProvisionCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith('GRANT CREATE SESSION TO "PV_READER_'):
                raise RuntimeError("reader grant failed")
            return self

    class AdminConnection(FakeConnection):
        def cursor(self):
            cursor = (
                FailingProvisionCursor(log)
                if not self.cursors
                else FakeCursor(log)
            )
            self.cursors.append(cursor)
            log.append(("cursor",))
            return cursor

    admin = AdminConnection(log)
    driver = SimpleNamespace(
        AUTH_MODE_SYSDBA=99,
        connect=lambda **_kwargs: admin,
        makedsn=lambda host, port, **kwargs: f"{host}:{port}/{kwargs}",
    )
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)

    with pytest.raises(RuntimeError, match="reader grant failed"):
        runner.execute_native_query(
            "oracle",
            {},
            {},
            {},
            "SELECT 1 FROM dual",
            "oracle://sys:secret@db:1521/FREEPDB1?mode=sysdba",
        )

    statements = [entry[1] for entry in log if entry[0] == "execute"]
    owner_username = next(
        statement.split()[2]
        for statement in statements
        if statement.startswith('CREATE USER "PV_OWNER_')
    )
    reader_username = next(
        statement.split()[2]
        for statement in statements
        if statement.startswith('CREATE USER "PV_READER_')
    )
    assert f"DROP USER {reader_username} CASCADE" in statements
    assert f"DROP USER {owner_username} CASCADE" in statements
    assert statements.index(f"DROP USER {reader_username} CASCADE") < statements.index(
        f"DROP USER {owner_username} CASCADE"
    )
    assert admin.closed is True


def test_fixture_identifiers_are_quoted_and_native_case_folded():
    assert runner._quote_ident('x"; DROP TABLE y;--', "postgres") == '"x""; DROP TABLE y;--"'
    assert runner._quote_ident("x]; DROP TABLE y;--", "tsql") == "[x]]; DROP TABLE y;--]"
    assert runner._fold_fixture_identifier("Mixed_Name", "postgres") == "mixed_name"
    assert runner._fold_fixture_identifier("Mixed_Name", "oracle") == "MIXED_NAME"
    assert runner._fold_fixture_identifier("Mixed_Name", "mysql") == "Mixed_Name"
    assert runner._MYSQL_TARGET_VERSION == "8.0.46"
    assert runner._MYSQL_REQUIRED_LOWER_CASE_TABLE_NAMES == 0


def test_mysql_fixture_preserves_indexable_text_types_and_fits_generated_values():
    code_spec = runner._column_spec(
        "mysql", "CHAR(4)", ["Code_1"], "Code", indexed=True
    )
    provider_spec = runner._column_spec(
        "mysql", "VARCHAR(40)", ["HAL"], "Provider", indexed=True
    )
    text_key_spec = runner._column_spec(
        "mysql", "TEXT", ["unbounded-key"], "Code", indexed=True
    )

    assert code_spec.sql_type == "CHAR(4)"
    assert provider_spec.sql_type == "VARCHAR(40)"
    assert text_key_spec.sql_type == "VARCHAR(768)"
    assert runner._coerce_parameter("Code_1", "mysql", code_spec) == "Cod1"
    assert runner._coerce_parameter(
        "Movie_1", "mysql", runner._ColumnSpec("BIGINT", "int")
    ) == 1


def test_mysql_mutation_replay_restores_authoritative_table_qualifier_case():
    ast = parse_one(
        "SELECT boxes.code FROM warehouses "
        "LEFT JOIN boxes ON boxes.warehouse = warehouses.code",
        read="mysql",
    )

    generator._restore_native_table_spelling(ast, ["Warehouses", "Boxes"])
    rendered = ast.sql(dialect="mysql")

    assert "Warehouses" in rendered
    assert "Boxes" in rendered


def test_successful_execution_with_failed_cleanup_is_explicit(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log, fail_rollback=True)
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)

    with pytest.raises(runner.NativeCleanupError) as caught:
        runner.execute_native_query(
            "postgres",
            {},
            {},
            {},
            "SELECT answer FROM t",
            "postgresql://user:pw@db/course",
        )

    assert caught.value.code == "NATIVE_CLEANUP_FAILED"
    assert connection.closed is True


def test_batch_reuses_one_postgres_fixture_and_cleans_up_once(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log)
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)
    schema, types, rows = _fixture()

    outcomes = runner.execute_native_queries(
        "postgres",
        schema,
        types,
        rows,
        ["SELECT answer FROM t", "SELECT answer + 1 FROM t"],
        "postgresql://user:pw@db/course",
    )

    assert [outcome.succeeded for outcome in outcomes] == [True, True]
    assert [outcome.rows for outcome in outcomes] == [
        ((Decimal("42.00"),),),
        ((Decimal("42.00"),),),
    ]
    statements = [entry[1] for entry in log if entry[0] == "execute"]
    assert sum(statement.startswith("CREATE SCHEMA") for statement in statements) == 1
    assert sum(statement.startswith("CREATE TABLE") for statement in statements) == 1
    assert sum(statement.startswith("SAVEPOINT") for statement in statements) == 2
    assert log.count(("rollback",)) == 1
    assert log.count(("connection_close",)) == 1


def test_batch_can_report_query_error_and_continue_in_same_session(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log, fail_query=True)
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)

    outcomes = runner.execute_native_queries(
        "postgres",
        {},
        {},
        {},
        ["SELECT broken FROM t", "SELECT 2"],
        "postgresql://user:pw@db/course",
        continue_on_error=True,
    )

    assert outcomes[0].succeeded is False
    assert outcomes[0].error.code == "NATIVE_QUERY_FAILED"
    assert outcomes[1].succeeded is True
    statements = [entry[1] for entry in log if entry[0] == "execute"]
    assert any(statement.startswith("ROLLBACK TO SAVEPOINT") for statement in statements)


def test_sqlserver_sets_connection_timeout_from_session_option(monkeypatch):
    log: list[tuple] = []
    connection = FakeConnection(log, autocommit=True)
    driver = SimpleNamespace(connect=lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(runner, "_load_driver", lambda _backend: driver)

    runner.execute_native_queries(
        "tsql",
        {},
        {},
        {},
        ["SELECT 1", "SELECT 2"],
        "mssql://sa:pw@db/master",
        query_timeout_seconds=7,
    )

    assert connection.timeout == 7
    assert "SET LOCK_TIMEOUT 7000" in [
        entry[1] for entry in log if entry[0] == "execute"
    ]


@pytest.mark.parametrize(
    ("result_rows", "row_limit", "byte_limit", "code"),
    [
        ([(1,), (2,)], 1, 1_000, "NATIVE_RESULT_ROW_LIMIT_EXCEEDED"),
        ([("long payload",)], 10, 4, "NATIVE_RESULT_BYTE_LIMIT_EXCEEDED"),
    ],
)
def test_result_limits_raise_stable_errors(result_rows, row_limit, byte_limit, code):
    class ResultCursor:
        description = [("value",)]

        def __init__(self):
            self.pending = list(result_rows)

        def execute(self, _sql):
            return self

        def fetchmany(self, size):
            batch, self.pending = self.pending[:size], self.pending[size:]
            return batch

    with pytest.raises(runner.NativeResultLimitError) as caught:
        runner._execute_submitted_query(
            ResultCursor(),
            "postgres",
            "SELECT value",
            max_result_rows=row_limit,
            max_result_bytes=byte_limit,
        )

    assert caught.value.code == code


def test_recursive_result_normalization_is_hashable_and_deterministic():
    left = {
        "list": [1, {"b": 2, "a": [3, 4]}],
        "set": {"z", "a"},
    }
    right = {
        "set": {"a", "z"},
        "list": [1, {"a": [3, 4], "b": 2}],
    }
    normalized_left = runner._normalize_cell(left)
    normalized_right = runner._normalize_cell(right)

    assert normalized_left == normalized_right
    assert hash(normalized_left) == hash(normalized_right)
    assert runner._normalize_cell('{"b":[2,1],"a":true}') == (
        ("a", True),
        ("b", (2, 1)),
    )


@pytest.mark.parametrize(
    "driver_error",
    [
        ConnectionError("connection reset by peer"),
        RuntimeError("08006 connection failure"),
        RuntimeError(2006, "MySQL server has gone away"),
        RuntimeError("ORA-03113: end-of-file on communication channel"),
    ],
)
def test_driver_connection_failure_is_not_reported_as_query_rejection(driver_error):
    class BrokenCursor:
        def execute(self, _sql):
            raise driver_error

    with pytest.raises(runner.NativeInfrastructureError) as caught:
        runner._execute_submitted_query(BrokenCursor(), "postgres", "SELECT 1")

    assert caught.value.code == "NATIVE_CONNECTION_LOST"


def test_mysql_loads_with_admin_and_runs_queries_as_temporary_reader(monkeypatch):
    log: list[tuple] = []

    class TaggedCursor(FakeCursor):
        def __init__(self, tag):
            super().__init__(log)
            self.tag = tag
            self._single_row = None

        def execute(self, sql, params=None):
            log.append((self.tag, "execute", sql, params))
            if sql == "SELECT VERSION(), @@lower_case_table_names":
                self.description = [("version",), ("lower_case_table_names",)]
                self._single_row = ("8.0.46", 0)
                return self
            if sql.startswith("SELECT "):
                self.description = [("answer",)]
                self._fetched = False
            else:
                self.description = None
            return self

        def executemany(self, sql, values):
            log.append((self.tag, "executemany", sql, list(values)))
            return self

        def fetchone(self):
            log.append((self.tag, "fetchone"))
            return self._single_row

    class TaggedConnection(FakeConnection):
        def __init__(self, tag, *, autocommit):
            super().__init__(log, autocommit=autocommit)
            self.tag = tag

        def cursor(self):
            cursor = TaggedCursor(self.tag)
            self.cursors.append(cursor)
            return cursor

    admin = TaggedConnection("admin", autocommit=True)
    reader = TaggedConnection("reader", autocommit=False)

    def connect(**kwargs):
        log.append(("connect", kwargs))
        return reader if kwargs["user"].startswith("pv_") else admin

    monkeypatch.setattr(
        runner, "_load_driver", lambda _backend: SimpleNamespace(connect=connect)
    )

    outcomes = runner.execute_native_queries(
        "mysql",
        {"t": ["answer"]},
        {"t": {"answer": "BIGINT"}},
        {"t": [{"answer": 42}]},
        ["SELECT answer FROM t", "SELECT answer FROM t"],
        "mysql+pymysql://root:pw@db:3306/course",
    )

    assert all(outcome.succeeded for outcome in outcomes)
    admin_statements = [entry[2] for entry in log if entry[:2] == ("admin", "execute")]
    reader_statements = [entry[2] for entry in log if entry[:2] == ("reader", "execute")]
    assert sum(sql.startswith("CREATE DATABASE") for sql in admin_statements) == 1
    assert "SELECT VERSION(), @@lower_case_table_names" in admin_statements
    assert sum(sql.startswith("CREATE TABLE") for sql in admin_statements) == 1
    assert sum(sql.startswith("DROP DATABASE") for sql in admin_statements) == 1
    assert sum(sql.startswith("DROP USER") for sql in admin_statements) == 1
    assert "SELECT answer FROM t" not in admin_statements
    assert reader_statements.count("SELECT answer FROM t") == 2
    assert admin.closed is True
    assert reader.closed is True


def _record_connect(log, connection, connection_url, kwargs):
    log.append(("connect", connection_url, kwargs))
    return connection
