import random
import sqlite3
from typing import List, Dict, Any, Optional, Set
from collections import Counter
import sqlglot
from sqlglot import exp, parse_one, optimizer

class QueryDrivenDataGenerator:
    """
    感知层 - 逻辑传感器 (Logical Sensor) V4.2
    实现全场景谓词识别（CTE/Union/Join）与拓扑对齐造数。
    """

    @staticmethod
    def extract_predicates_v4(sql: str, dialect: str = "mysql") -> List[Dict[str, Any]]:
        """
        全场景猎手：抓取包含 Literal 在内的所有谓词约束。
        V4.2 增强：支持 Between, Is, Like
        """
        predicates = []
        try:
            expression = parse_one(sql, read=dialect)

            # 扫描所有比较和谓词节点
            for node in expression.find_all(exp.Comparison, exp.In, exp.Between, exp.Is, exp.Like):
                left = node.left if hasattr(node, 'left') else node.this
                right = node.right if hasattr(node, 'right') else None

                # 1. 处理 IN
                if isinstance(node, exp.In):
                    vals = [l.this for l in node.expressions if isinstance(l, exp.Literal)]
                    if isinstance(node.this, exp.Column):
                        predicates.append({"type": "set", "column": node.this.name, "values": vals})

                # 2. 处理 BETWEEN (V4.2 新增)
                elif isinstance(node, exp.Between):
                    if isinstance(node.this, exp.Column):
                        predicates.append({
                            "type": "range",
                            "column": node.this.name,
                            "low": node.args['low'].this,
                            "high": node.args['high'].this
                        })

                # 3. 处理 IS NULL / IS NOT NULL (V4.2 新增)
                elif isinstance(node, exp.Is):
                    if isinstance(node.this, exp.Column):
                        predicates.append({"type": "null_check", "column": node.this.name, "is_not": isinstance(node.expression, exp.Not)})

                # 4. 处理 LIKE (V4.2 新增)
                elif isinstance(node, exp.Like):
                    if isinstance(node.this, exp.Column):
                        predicates.append({"type": "pattern", "column": node.this.name, "regex": node.args['expression'].this})

                # 5. 处理基础比较 (eq, gt, lt, etc.)
                elif isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                    predicates.append({
                        "type": "boundary",
                        "column": left.name,
                        "op": type(node).__name__.lower(),
                        "value": right.this,
                        "is_number": right.is_number
                    })

                # 6. 处理 Join Link
                elif isinstance(left, exp.Column) and isinstance(right, exp.Column):
                    predicates.append({"type": "link", "left_col": left.name, "right_col": right.name})

            return predicates
        except Exception:
            return []

    @staticmethod
    def generate_topology_data(sql_standard: str, schema_dict: Dict[str, str], num_rows: int = 20) -> List[Dict[str, Any]]:
        """
        拓扑感知型造数：确保连接必对齐，边界必触发。
        """
        all_preds = QueryDrivenDataGenerator.extract_predicates_v4(sql_standard, dialect="mysql")
        mock_data = []

        shared_pool = { "id_pool": [random.randint(1, 100) for _ in range(5)], "text_pool": ["node_A", "node_B", "node_C"] }

        for i in range(num_rows):
            row = {}
            for col, ctype in schema_dict.items():
                if "ID" in col.upper() or "SSN" in col.upper() or "NO" in col.upper():
                    row[col] = random.choice(shared_pool["id_pool"])
                elif "INT" in ctype.upper():
                    row[col] = random.randint(1, 1000)
                else:
                    row[col] = f"sample_{i}"

            # 边界攻击升级
            for p in all_preds:
                col = p.get("column")
                if not col or col not in row: continue

                if p["type"] == "boundary":
                    val = p["value"]
                    row[col] = float(val) if p["is_number"] and random.random() > 0.5 else val
                elif p["type"] == "range":
                    row[col] = float(p["low"]) if random.random() > 0.5 else float(p["high"])
                elif p["type"] == "null_check":
                    row[col] = None if not p["is_not"] else row[col]
                elif p["type"] == "set":
                    row[col] = random.choice(p["values"]) if random.random() > 0.5 else "out_of_set"

            # 链路重写
            for p in all_preds:
                if p["type"] == "link":
                    l, r = p["left_col"], p["right_col"]
                    if l in row and r in row: row[r] = row[l]

            mock_data.append(row)
        return mock_data

    @staticmethod
    def run_on_sandbox_v4(sql: str, table_names: Set[str], data: List[Dict[str, Any]], schema_dict: Dict) -> List[Any]:
        """
        分布式沙盒：为每一个被引用的表都注入拓扑对齐的数据。
        引入方言转译：将 MySQL 转译为 SQLite 执行。
        """
        if not data: return []
        try:
            try:
                sqlite_sql = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            except Exception:
                sqlite_sql = sql

            conn = sqlite3.connect(":memory:")
            cursor = conn.cursor()

            for t_name in table_names:
                # 强化类型映射：将 MySQL 类型转换为 SQLite 对应的亲和类型，确保数值运算正确
                typed_cols = []
                for k, v in schema_dict.items():
                    ctype = v.upper()
                    if any(t in ctype for t in ["INT", "BIT", "SERIAL"]):
                        stype = "INTEGER"
                    elif any(t in ctype for t in ["DECIMAL", "FLOAT", "DOUBLE", "REAL", "NUMERIC"]):
                        stype = "REAL"
                    else:
                        stype = "TEXT"
                    typed_cols.append(f"\"{k}\" {stype}")

                cols = ", ".join(typed_cols)
                cursor.execute(f"CREATE TABLE \"{t_name}\" ({cols})")
                placeholders = ", ".join(["?" for _ in schema_dict])
                # 修复：直接注入 row.get(k)，让 sqlite3 处理 None -> NULL，而非写入 "NULL" 字符串
                cursor.executemany(f"INSERT INTO \"{t_name}\" VALUES ({placeholders})", [tuple(d.get(k) for k in schema_dict.keys()) for d in data])

            cursor.execute(sqlite_sql)
            return cursor.fetchall()
        except Exception as e:
            return [f"SANDBOX_ERR: {str(e)}"]
        finally:
            conn.close()

    @staticmethod
    def evaluate_equivalence_v4(student_sql: str, standard_sql: str, schema: Dict) -> bool:
        """
        融合判定入口 V4.2：支持无序容错与列名脱敏。
        """
        try:
            std_ast = parse_one(standard_sql, read="mysql")
            tables = {t.name.upper() for t in std_ast.find_all(exp.Table)}
            tables.update({t.name.upper() for t in parse_one(student_sql, read="mysql").find_all(exp.Table)})
            has_order_by = any(isinstance(node, exp.Order) for node in std_ast.find_all(exp.Order))
        except:
            tables = {"UNKNOWN_TABLE"}
            has_order_by = False

        probe_data = QueryDrivenDataGenerator.generate_topology_data(standard_sql, schema)
        res_std = QueryDrivenDataGenerator.run_on_sandbox_v4(standard_sql, tables, probe_data, schema)
        res_stu = QueryDrivenDataGenerator.run_on_sandbox_v4(student_sql, tables, probe_data, schema)

        if any("SANDBOX_ERR" in str(r) for r in res_std + res_stu):
            raise RuntimeError("Dialect mismatch in Sandbox")

        # 核心判定逻辑升级
        if has_order_by:
            # 有序比对：精确匹配
            return res_std == res_stu
        else:
            # 无序比对：利用 Counter 进行频次匹配
            return Counter(res_std) == Counter(res_stu)
