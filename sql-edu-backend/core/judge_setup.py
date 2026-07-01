"""
Judge Environment Initialization and Setup.

This module automates the generation and execution of SQL scripts in the sandbox database
prior to evaluating student answers. It reconstructs and populates mock tables as defined
by the question's `schema_preview` JSON structure.
"""

import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger(__name__)


def _infer_mysql_type(col_name: str, sample_value: Any) -> str:
    """
    Infers the MySQL data type of a column based on its name and a sample value.

    Args:
        col_name (str): The name of the column.
        sample_value (Any): A sample value from rows to help with type inference.

    Returns:
        str: String representation of the inferred SQL type (e.g., "INT NOT NULL").
    """
    name_lower = (col_name or "").lower()
    
    # 1. Primary keys pattern matching
    if name_lower == "id":
        return "INT NOT NULL AUTO_INCREMENT PRIMARY KEY"
        
    # 2. Foreign keys pattern matching
    if name_lower.endswith("_id"):
        return "INT NOT NULL"
        
    # 3. Numeric amounts and financial data pattern matching
    if "amount" in name_lower or "price" in name_lower or "sum" in name_lower:
        return "DECIMAL(12,2) DEFAULT NULL"
        
    # 4. Dates and timestamps pattern matching
    if name_lower.endswith("_at") or name_lower in ("created_at", "updated_at", "date", "time"):
        return "DATETIME DEFAULT NULL"
        
    # 5. Fallback type matching based on value instances
    if sample_value is None:
        return "VARCHAR(255) DEFAULT NULL"
    if isinstance(sample_value, bool):
        return "TINYINT(1) DEFAULT NULL"
    if isinstance(sample_value, int):
        return "INT DEFAULT NULL"
    if isinstance(sample_value, (float,)):
        return "DECIMAL(12,2) DEFAULT NULL"
    
    # Regex detection for datetime formatted strings
    if isinstance(sample_value, str) and re.match(r"^\d{4}-\d{2}-\d{2}[T ]?\d{2}:\d{2}", sample_value):
        return "DATETIME DEFAULT NULL"
        
    return "VARCHAR(255) DEFAULT NULL"


def _escape_sql_value(v: Any) -> str:
    """
    Safely converts a Python value into its corresponding SQL literal value.

    Guards against basic SQL injection by escaping single quotes and backslashes.

    Args:
        v (Any): The Python value to escape.

    Returns:
        str: The SQL-safe string literal representing the value.
    """
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    # Double single quotes and double backslashes for SQL escaping
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    return f"'{s}'"


def generate_init_sql_from_schema_preview(schema_preview: str | None) -> str | None:
    """
    Generates dynamic DROP TABLE, CREATE TABLE, and INSERT statements from a schema preview JSON.

    Each table structure is inferred dynamically, and rows are escaped to prevent errors.
    The table structures must be dropped and recreated before every judge run to ensure isolation.

    Args:
        schema_preview (str | None): A JSON string representing tables, columns, and rows.
            Example: {"tables": [{"name": "orders", "columns": ["id", "amount"], "rows": [{"id": 1, "amount": 10.5}]}]}

    Returns:
        str | None: Semicolon-joined SQL setup script, or None if the input is empty or invalid.
    """
    if not schema_preview or not schema_preview.strip():
        return None
    try:
        data = json.loads(schema_preview)
    except json.JSONDecodeError:
        return None
    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        return None

    statements: list[str] = []
    for tbl in tables:
        if not isinstance(tbl, dict):
            continue
        name = tbl.get("name")
        columns = tbl.get("columns")
        rows = tbl.get("rows")
        if not name or not isinstance(columns, list) or not columns:
            continue
        if not isinstance(rows, list):
            rows = []
            
        # Clean table and column names using regex to only allow alphanumeric characters and underscores
        safe_name = re.sub(r"[^\w]", "", str(name))
        if not safe_name:
            continue
        col_defs: list[str] = []
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                continue
            safe_col = re.sub(r"[^\w]", "", col)
            if not safe_col:
                continue
            sample = None
            # Find a sample value to assist in SQL type inference
            for row in rows:
                if isinstance(row, dict) and col in row:
                    sample = row[col]
                    break
            type_str = _infer_mysql_type(safe_col, sample)
            col_defs.append(f"`{safe_col}` {type_str}")
        if not col_defs:
            continue
            
        # Clean up database state to prevent interference from prior student solutions
        drop_sql = f"DROP TABLE IF EXISTS `{safe_name}`"
        statements.append(drop_sql)
        create_sql = f"CREATE TABLE `{safe_name}` (\n  " + ",\n  ".join(col_defs) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        statements.append(create_sql)

        if not rows:
            continue
            
        # Check column names inside rows
        insert_cols = [c for c in columns if isinstance(c, str) and re.match(r"^\w+$", c)]
        if not insert_cols:
            continue
        cols_str = ", ".join(f"`{c}`" for c in insert_cols)
        value_rows: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            vals = [_escape_sql_value(row.get(c)) for c in insert_cols]
            value_rows.append("(" + ", ".join(vals) + ")")
        if not value_rows:
            continue
            
        # Construct INSERT statement based on primary key existence
        has_pk = "id" in [c.lower() for c in insert_cols]
        if has_pk:
            update_parts = [f"`{c}`=VALUES(`{c}`)" for c in insert_cols]
            insert_sql = (
                f"INSERT INTO `{safe_name}` ({cols_str}) VALUES\n  "
                + ",\n  ".join(value_rows)
                + "\nON DUPLICATE KEY UPDATE " + ", ".join(update_parts)
            )
        else:
            insert_sql = (
                f"INSERT IGNORE INTO `{safe_name}` ({cols_str}) VALUES\n  "
                + ",\n  ".join(value_rows)
            )
        statements.append(insert_sql)

    if not statements:
        return None
    return ";\n".join(statements) + ";"


def _is_safe_setup_statement(stmt: str) -> bool:
    """
    Validates if a setup SQL statement is secure and conforms to sandbox constraints.

    Allows only specific statement patterns:
    - DROP TABLE IF EXISTS `table_name`
    - CREATE TABLE `table_name` (...)
    - INSERT INTO / INSERT IGNORE INTO `table_name` (...)

    Args:
        stmt (str): The SQL statement to check.

    Returns:
        bool: True if the statement meets safety criteria, False otherwise.
    """
    s = stmt.strip()
    if not s:
        return False
    lower = s.lower()
    
    # 1. DROP TABLE validation
    if lower.startswith("drop table if exists"):
        return bool(re.match(r"drop\s+table\s+if\s+exists\s+`?\w+`?\s*$", lower))
        
    # 2. CREATE TABLE validation
    if lower.startswith("create table"):
        return bool(re.match(r"create\s+table\s+(?:if\s+not\s+exists\s+)?`?\w+`?\s*\(", lower))
        
    # 3. INSERT INTO validation
    if "insert" in lower[:20] and "into" in lower[:25]:
        return bool(re.match(r"insert\s+(?:ignore\s+)?into\s+`?\w+`?\s*\(", lower))
        
    return False


async def execute_setup_sql(session: AsyncSession, init_sql: str) -> None:
    """
    Executes setup statements sequentially inside the sandbox database.

    Only statements verified by `_is_safe_setup_statement` are allowed.

    Args:
        session (AsyncSession): SQLAlchemy asynchronous database session.
        init_sql (str): Multi-statement setup SQL script.
    """
    if not init_sql or not init_sql.strip():
        return
    # Split queries by semicolon, preserving semicolons wrapped inside quotes
    parts = re.split(r";\s*(?=(?:[^']*'[^']*')*[^']*$)", init_sql)
    for part in parts:
        stmt = part.strip()
        if not stmt or stmt.startswith("--"):
            continue
        if not _is_safe_setup_statement(stmt):
            continue
        try:
            await session.execute(text(stmt))
        except Exception as e:
            # Warnings are logged; execution continues since malformed schemas fail naturally during judging
            logger.warning(f"执行建表/插入语句失败: {stmt[:100]}... 错误: {e}")
    await session.flush()
