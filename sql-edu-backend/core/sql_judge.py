"""SQL 判题引擎：安全执行 SQL 并对比结果。"""

from decimal import Decimal
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import re


class SQLJudgeError(Exception):
    """SQL 判题过程中的自定义异常。"""
    pass


class SQLSafetyError(SQLJudgeError):
    """SQL 包含危险操作（DROP/DELETE/INSERT 等），系统拒绝执行。"""
    def __init__(self, message: str, detected_keyword: str | None = None):
        super().__init__(message)
        self.detected_keyword = detected_keyword


class SQLJudgeService:
    """SQL 判题服务，负责安全执行 SQL 并对比结果。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    def _check_sql_safety(self, sql: str) -> tuple[bool, str | None]:
        """检查 SQL 语句的安全性。

        危险操作仅指：DROP、DELETE、TRUNCATE、ALTER、CREATE、INSERT、UPDATE、GRANT、REVOKE、EXEC/EXECUTE 等改库/删库操作。
        纯 SELECT 查询一律视为安全。

        :param sql: SQL 语句
        :return: (是否安全, 若不安全则返回检测到的危险关键字，否则 None)
        """
        if not sql or not sql.strip():
            return False, None
        s = sql.strip()
        s_lower = s.lower()
        # 去掉首部注释，避免 -- 或 /* */ 导致误判
        while True:
            s_lower = s_lower.lstrip()
            if s_lower.startswith("--"):
                idx = s_lower.find("\n")
                s_lower = s_lower[idx + 1 :] if idx >= 0 else ""
            elif s_lower.startswith("/*"):
                idx = s_lower.find("*/", 2)
                s_lower = s_lower[idx + 2 :] if idx >= 0 else ""
            else:
                break
        s_lower = s_lower.strip()

        dangerous_keywords = [
            "drop", "delete", "truncate", "alter", "create", "insert", "update",
            "grant", "revoke", "exec", "execute",
        ]
        for keyword in dangerous_keywords:
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, s_lower):
                return False, keyword

        # 允许 SELECT 或 WITH ... SELECT（CTE 写法）
        if s_lower.startswith("select"):
            return True, None
        if s_lower.startswith("with") and re.search(r"\bselect\b", s_lower):
            return True, None
        return False, None

    async def execute_sql_safely(self, sql: str) -> list[dict[str, Any]]:
        """安全执行 SQL 语句并返回结果。

        :param sql: SQL 语句
        :return: 查询结果列表（每行是一个字典）
        :raises SQLJudgeError: 如果 SQL 不安全或执行失败
        """
        safe, keyword = self._check_sql_safety(sql)
        if not safe:
            if keyword:
                raise SQLSafetyError(
                    f"SQL 包含危险操作（检测到关键字：{keyword.upper()}）。练习环境仅允许 SELECT 查询，禁止 DROP/DELETE/INSERT/UPDATE 等改库删库操作。",
                    detected_keyword=keyword,
                )
            raise SQLSafetyError(
                "SQL 必须以 SELECT 开头。练习环境仅允许 SELECT 查询语句。",
                detected_keyword=None,
            )

        try:
            # 执行 SQL（使用 text() 包装原始 SQL）
            result = await self.session.execute(text(sql))
            rows = result.fetchall()

            # 转换为字典列表
            columns = result.keys()
            result_list = [dict(zip(columns, row)) for row in rows]

            return result_list
        except Exception as e:
            raise SQLJudgeError(f"SQL 执行失败: {str(e)}")

    def _sql_has_order_by(self, sql: str) -> bool:
        """粗略判断 SQL 是否包含 ORDER BY（标准答案若要求顺序，则判题需按行序比较）。"""
        sql_lower = sql.lower().strip()
        sql_lower = re.sub(r"--[^\n]*", " ", sql_lower)
        sql_lower = re.sub(r"/\*[\s\S]*?\*/", " ", sql_lower)
        return "order" in sql_lower and " by " in sql_lower

    def _normalize_value(self, value: Any) -> Any:
        """标准化单个值：MySQL 返回 Decimal，需与 float 统一；浮点保留6位小数。"""
        if value is None:
            return None
        if isinstance(value, (int, float, Decimal)):
            f = float(value)
            r = round(f, 6)
            return str(int(r)) if r == int(r) else str(r)
        s = str(value).strip()
        return s.lower() if s else ""

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """单行标准化：数值（含 Decimal）统一保留6位小数；字符串去首尾空格并统一小写比较。"""
        return {k: self._normalize_value(v) for k, v in row.items()}

    def _normalize_result(self, result: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """标准化结果集（用于无序对比）：统一值类型并按行排序，使顺序无关。"""
        normalized = [self._normalize_row(row) for row in result]
        try:
            normalized.sort(key=lambda x: tuple(
                str(v) if v is not None else "" for v in x.values()
            ))
        except Exception:
            pass
        return normalized

    def _normalize_result_keep_order(self, result: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """标准化结果集但保持行序（用于 ORDER BY 题目的顺序敏感对比）。"""
        return [self._normalize_row(row) for row in result]

    def compare_results(
        self, student_result: list[dict[str, Any]], correct_result: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """对比学生结果和标准答案（无序，向后兼容）。含 ORDER BY 的题目请用 judge_sql 自动按序比较。"""
        return self.compare_results_unordered(student_result, correct_result)

    def _compare_normalized(
        self,
        student_norm: list[dict[str, Any]],
        correct_norm: list[dict[str, Any]],
        *,
        ordered: bool,
    ) -> tuple[bool, str]:
        """在已标准化后的结果上做对比。ordered=True 时要求行序一致（等价性+逻辑性）。"""
        if len(student_norm) != len(correct_norm):
            return (
                False,
                f"结果行数不匹配：期望 {len(correct_norm)} 行，实际 {len(student_norm)} 行。",
            )
        if student_norm and correct_norm:
            sk = set(student_norm[0].keys())
            ck = set(correct_norm[0].keys())
            if sk != ck:
                missing = ck - sk
                extra = sk - ck
                error_msg = "列结构不匹配。"
                if missing:
                    error_msg += f" 缺少列: {', '.join(missing)}"
                if extra:
                    error_msg += f" 多余列: {', '.join(extra)}"
                return False, error_msg
        if ordered:
            for i, (sr, cr) in enumerate(zip(student_norm, correct_norm)):
                if sr != cr:
                    return False, f"第 {i + 1} 行与标准答案不一致（顺序或数据有误，如 ORDER BY 方向相反）。"
            return True, "结果匹配（含顺序）。"
        try:
            student_set = {tuple(sorted(row.items())) for row in student_norm}
            correct_set = {tuple(sorted(row.items())) for row in correct_norm}
            if student_set != correct_set:
                return False, "结果数据不匹配（可能顺序不同或数据有误）。"
        except Exception:
            if student_norm != correct_norm:
                return False, "结果数据不匹配。"
        return True, "结果匹配。"

    def compare_results_unordered(
        self, student_result: list[dict[str, Any]], correct_result: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """对比学生结果和标准答案（无序：行集相等即可）。"""
        student_norm = self._normalize_result(student_result)
        correct_norm = self._normalize_result(correct_result)
        return self._compare_normalized(student_norm, correct_norm, ordered=False)

    def compare_results_ordered(
        self, student_result: list[dict[str, Any]], correct_result: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """按行序严格对比（用于含 ORDER BY 的题目：等价性+逻辑性+顺序一致）。"""
        student_norm = self._normalize_result_keep_order(student_result)
        correct_norm = self._normalize_result_keep_order(correct_result)
        return self._compare_normalized(student_norm, correct_norm, ordered=True)

    def compare_results_by_values_only(
        self, student_result: list[dict[str, Any]], correct_result: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """按列值对比，忽略列名。题目无别名要求时使用。"""
        student_norm = self._normalize_result(student_result)
        correct_norm = self._normalize_result(correct_result)
        # 转为值元组（按列顺序），列名不参与比较
        def row_to_tuple(row: dict[str, Any]) -> tuple:
            return tuple(str(v) if v is not None else "" for v in row.values())

        student_tuples = sorted(row_to_tuple(r) for r in student_norm)
        correct_tuples = sorted(row_to_tuple(r) for r in correct_norm)
        if len(student_tuples) != len(correct_tuples):
            return False, f"结果行数不匹配：期望 {len(correct_tuples)} 行，实际 {len(student_tuples)} 行。"
        if student_norm and correct_norm:
            sc = len(student_norm[0])
            cc = len(correct_norm[0])
            if sc != cc:
                return False, f"列数不匹配：期望 {cc} 列，实际 {sc} 列。"
        if student_tuples != correct_tuples:
            return False, "结果数据不匹配（可能顺序不同或数据有误）。"
        return True, "结果匹配。"

    async def judge_sql(
        self, student_sql: str, correct_sql: str, required_output_columns: str | None = None
    ) -> tuple[bool, str]:
        """完整的 SQL 判题流程。

        规则总结：
        - **题目有别名要求时**（后端为其填充了 `required_output_columns`）：
          - 既看结果是否等价，也要求列名/别名结构一致（学生必须按要求起别名）；
        - **题目无别名要求时**：
          - 完全忽略列名，只比较列值是否等价；
        - 若标准答案含 ORDER BY，则在各自模式下额外要求行顺序一致。

        :param student_sql: 学生的 SQL 语句
        :param correct_sql: 标准答案 SQL 语句
        :param required_output_columns: 若非空，表示题目对输出列名/别名有明确要求
        :return: (是否正确, 错误描述)
        """
        detail = await self.judge_sql_detailed(
            student_sql=student_sql,
            correct_sql=correct_sql,
            required_output_columns=required_output_columns,
        )
        return detail["is_correct"], detail["error_message"]

    async def judge_sql_detailed(
        self, student_sql: str, correct_sql: str, required_output_columns: str | None = None
    ) -> dict[str, Any]:
        """完整判题并返回阶段 1/2 所需的数据层证据。

        返回值只包含结果集元信息，不直接暴露完整行数据，避免在提示上下文里塞入过大的数据。
        """
        try:
            student_result = await self.execute_sql_safely(student_sql)
        except SQLSafetyError:
            raise
        except SQLJudgeError as e:
            return {
                "is_correct": False,
                "error_message": f"学生 SQL 执行失败: {str(e)}",
                "student_result_meta": None,
                "correct_result_meta": None,
                "comparison": {
                    "ordered": self._sql_has_order_by(correct_sql),
                    "enforce_aliases": bool(required_output_columns and str(required_output_columns).strip()),
                    "student_exec_ok": False,
                    "correct_exec_ok": None,
                },
            }

        try:
            correct_result = await self.execute_sql_safely(correct_sql)
        except SQLJudgeError as e:
            return {
                "is_correct": False,
                "error_message": f"标准答案 SQL 执行失败: {str(e)}",
                "student_result_meta": self._result_meta(student_result),
                "correct_result_meta": None,
                "comparison": {
                    "ordered": self._sql_has_order_by(correct_sql),
                    "enforce_aliases": bool(required_output_columns and str(required_output_columns).strip()),
                    "student_exec_ok": True,
                    "correct_exec_ok": False,
                },
            }

        enforce_aliases = bool(required_output_columns and str(required_output_columns).strip())
        ordered = self._sql_has_order_by(correct_sql)
        if enforce_aliases:
            if ordered:
                is_correct, error_msg = self.compare_results_ordered(student_result, correct_result)
            else:
                is_correct, error_msg = self.compare_results_unordered(student_result, correct_result)
        else:
            if ordered:
                is_correct, error_msg = self._compare_by_values_ordered(student_result, correct_result)
            else:
                is_correct, error_msg = self.compare_results_by_values_only(student_result, correct_result)

        return {
            "is_correct": is_correct,
            "error_message": error_msg,
            "student_result_meta": self._result_meta(student_result),
            "correct_result_meta": self._result_meta(correct_result),
            "comparison": {
                "ordered": ordered,
                "enforce_aliases": enforce_aliases,
                "student_exec_ok": True,
                "correct_exec_ok": True,
            },
        }

    def _compare_by_values_ordered(
        self, student_result: list[dict[str, Any]], correct_result: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """按行序、列值对比，忽略列名（无别名要求且含 ORDER BY 时）。"""
        student_norm = self._normalize_result_keep_order(student_result)
        correct_norm = self._normalize_result_keep_order(correct_result)
        if len(student_norm) != len(correct_norm):
            return False, f"结果行数不匹配：期望 {len(correct_norm)} 行，实际 {len(student_norm)} 行。"
        if student_norm and correct_norm:
            if len(student_norm[0]) != len(correct_norm[0]):
                return False, f"列数不匹配：期望 {len(correct_norm[0])} 列，实际 {len(student_norm[0])} 列。"
        for i, (sr, cr) in enumerate(zip(student_norm, correct_norm)):
            sv = tuple(str(v) if v is not None else "" for v in sr.values())
            cv = tuple(str(v) if v is not None else "" for v in cr.values())
            if sv != cv:
                return False, f"第 {i + 1} 行与标准答案不一致（顺序或数据有误）。"
        return True, "结果匹配（含顺序）。"

    def _result_meta(self, result: list[dict[str, Any]]) -> dict[str, Any]:
        """提取结果集元信息，供错误归因使用。"""
        columns = list(result[0].keys()) if result else []
        normalized = self._normalize_result_keep_order(result)
        unique_rows = {
            tuple(sorted(row.items()))
            for row in self._normalize_result(result)
        } if result else set()
        return {
            "row_count": len(result),
            "column_count": len(columns),
            "columns": columns,
            "duplicate_row_count": max(0, len(normalized) - len(unique_rows)),
            "sample_rows": normalized[:3],
        }


__all__ = ["SQLJudgeService", "SQLJudgeError", "SQLSafetyError"]
