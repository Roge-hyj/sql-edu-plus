"""
Phase 1 Pipeline 大规模验证测试（~1000 道不同题目）
策略：程序自动生成 ~1000 道独立的 SQL 练习题
  - 每道题有独立的 schema（表名/列名/类型均不同）
  - 正确 SQL 覆盖：WHERE, JOIN, GROUP BY, HAVING, ORDER BY, LIMIT,
    DISTINCT, 子查询, CASE WHEN, LIKE, IN, BETWEEN, 聚合函数等
  - 每道题生成 1-3 个模拟学生错误（基于真实常见错误模式）
  - 所有数据均为合成数据，不修改 Phase 1 任何逻辑
"""
import sys, os, random, hashlib, time
sys.path.insert(0, os.path.dirname(__file__) + "/..")

import pytest
from core.parseval_data_generator import generate_and_compare


# ─────────────────────────────────────────────────────────────────────
# 伪随机种子（保证可复现）
# ─────────────────────────────────────────────────────────────────────

SEED = 42
_rng = random.Random(SEED)

# ─────────────────────────────────────────────────────────────────────
# 名字池（用于生成不重复的表名/列名）
# ─────────────────────────────────────────────────────────────────────

_TABLE_PREFIXES = [
    "staff", "product", "order", "customer", "invoice", "shipment",
    "department", "project", "task", "review", "payment", "contract",
    "warehouse", "vehicle", "route", "ticket", "reservation", "listing",
    "vendor", "supplier", "branch", "region", "category", "item",
    "employee", "manager", "intern", "client", "account", "transaction",
    "asset", "budget", "expense", "revenue", "profit", "loss",
    "schedule", "meeting", "event", "session", "course", "grade",
    "enrollment", "faculty", "lab", "library", "book", "journal",
    "article", "paper", "author", "editor", "publisher", "reader",
    "subscriber", "member", "team", "league", "match", "player",
    "coach", "stadium", "fan", "score", "record", "album",
    "artist", "track", "genre", "concert", "venue", "festival",
    "movie", "actor", "director", "studio", "rating", "review_score",
    "hospital", "doctor", "patient", "nurse", "ward", "clinic",
    "pharmacy", "drug", "prescription", "diagnosis", "symptom", "treatment",
    "flight", "airport", "airline", "pilot", "passenger", "baggage",
    "hotel_room", "guest", "check_in", "amenity", "floor", "suite",
    "recipe", "ingredient", "chef", "menu", "dish", "restaurant",
    "table_booking", "waiter", "tip", "bill", "delivery", "takeout",
    "tree", "plant", "garden", "plot", "seed", "harvest",
    "sensor", "reading", "device", "alert", "threshold", "log",
    "server", "process", "thread", "memory", "disk", "network",
    "user", "role", "permission", "group", "token", "audit",
]

_COL_PREFIXES = [
    "id", "name", "code", "type", "status", "date", "amount",
    "price", "cost", "quantity", "total", "count", "rate",
    "score", "grade", "rank", "level", "tier", "class", "group",
    "city", "state", "country", "region", "zone", "area",
    "age", "year", "month", "day", "hour", "minute",
    "email", "phone", "address", "url", "note", "desc",
    "weight", "height", "width", "length", "volume", "size",
    "lat", "lng", "altitude", "depth", "speed", "temp",
    "is_active", "is_deleted", "is_verified", "is_premium", "is_open", "is_closed",
    "created_at", "updated_at", "start_date", "end_date", "due_date", "ship_date",
    "priority", "severity", "urgency", "impact", "risk", "likelihood",
    "rating", "review", "comment", "feedback", "suggestion", "complaint",
    "balance", "credit", "debit", "tax", "discount", "bonus",
    "dept_id", "mgr_id", "org_id", "team_id", "proj_id", "cust_id",
    "vendor_id", "product_id", "order_id", "invoice_id", "payment_id", "ship_id",
    "first_name", "last_name", "full_name", "nickname", "title", "suffix",
]

_TEXT_VALUES = [
    "'A'", "'B'", "'C'", "'X'", "'Y'", "'Z'",
    "'active'", "'pending'", "'closed'", "'open'",
    "'high'", "'medium'", "'low'",
    "'US'", "'UK'", "'CN'", "'JP'", "'DE'", "'FR'",
    "'red'", "'blue'", "'green'", "'yellow'",
    "'yes'", "'no'",
]


def _pick(items, k=1):
    return _rng.sample(items, min(k, len(items)))


def _pick_one(items):
    return _rng.choice(items)


def _rand_int(lo=1, hi=100):
    return _rng.randint(lo, hi)


# ─────────────────────────────────────────────────────────────────────
# Schema 生成器
# ─────────────────────────────────────────────────────────────────────

def _gen_schema(table_idx: int, num_tables: int = 1) -> tuple[str, list[dict]]:
    """
    生成紧凑格式 schema，返回 (schema_text, table_metas)
    table_metas: [{"name": ..., "cols": [...], "col_types": [...]}]
    """
    used_tables = set()
    used_cols_per_table = {}
    table_metas = []
    schema_parts = []

    for t in range(num_tables):
        # 选表名
        tname = _TABLE_PREFIXES[(table_idx * 3 + t) % len(_TABLE_PREFIXES)]
        suffix = ""
        while tname + suffix in used_tables:
            suffix = str(_rng.randint(2, 99))
        tname = tname + suffix
        used_tables.add(tname)

        # 选列名
        ncols = _rng.randint(3, 8)
        cols = []
        col_types = []
        used_cols = set()
        for ci in range(ncols):
            cname = _COL_PREFIXES[(table_idx * 7 + ci + t * 13) % len(_COL_PREFIXES)]
            csuffix = ""
            while cname + csuffix in used_cols:
                csuffix = str(_rng.randint(2, 99))
            cname = cname + csuffix
            used_cols.add(cname)
            cols.append(cname)
            # 列类型：0=int, 1=text
            col_types.append(_rng.randint(0, 1))

        schema_parts.append(f"{tname}({', '.join(cols)})")
        table_metas.append({"name": tname, "cols": cols, "col_types": col_types})
        used_cols_per_table[tname] = cols

    schema_text = "; ".join(schema_parts)
    return schema_text, table_metas


def _int_col(meta, idx=0):
    """从 table meta 中选一个 int 类型列"""
    for i, ct in enumerate(meta["col_types"]):
        if ct == 0 and i != idx:
            return meta["cols"][i]
    return meta["cols"][min(idx, len(meta["cols"]) - 1)]


def _text_col(meta):
    """从 table meta 中选一个 text 类型列"""
    for i, ct in enumerate(meta["col_types"]):
        if ct == 1:
            return meta["cols"][i]
    return meta["cols"][0]  # fallback


# ─────────────────────────────────────────────────────────────────────
# 正确 SQL 模板（覆盖多种 SQL 知识点）
# ─────────────────────────────────────────────────────────────────────

def _sql_where_simple(meta):
    """简单 WHERE 过滤"""
    t = meta["name"]
    col = _int_col(meta)
    val = _rand_int()
    op = _pick_one(["<", ">", "<=", ">=", "="])
    return f"SELECT * FROM {t} WHERE {col} {op} {val}"


def _sql_where_text(meta):
    """WHERE 文本列"""
    t = meta["name"]
    col = _text_col(meta)
    val = _pick_one(_TEXT_VALUES)
    return f"SELECT * FROM {t} WHERE {col} = {val}"


def _sql_where_and(meta):
    """WHERE 多条件 AND"""
    t = meta["name"]
    c1 = _int_col(meta, 0)
    c2 = _int_col(meta, 1) if len(meta["cols"]) > 1 else c1
    v1, v2 = _rand_int(), _rand_int()
    return f"SELECT * FROM {t} WHERE {c1} > {v1} AND {c2} < {v2}"


def _sql_where_or(meta):
    """WHERE OR"""
    t = meta["name"]
    c1 = _int_col(meta, 0)
    c2 = _int_col(meta, 1) if len(meta["cols"]) > 1 else c1
    v1, v2 = _rand_int(), _rand_int()
    return f"SELECT * FROM {t} WHERE {c1} > {v1} OR {c2} < {v2}"


def _sql_select_cols(meta):
    """SELECT 部分列"""
    t = meta["name"]
    k = max(2, len(meta["cols"]) // 2)
    cols = _pick(meta["cols"], k)
    col = _int_col(meta)
    val = _rand_int()
    return f"SELECT {', '.join(cols)} FROM {t} WHERE {col} > {val}"


def _sql_order_by(meta):
    """ORDER BY"""
    t = meta["name"]
    col = _int_col(meta)
    direction = _pick_one(["ASC", "DESC"])
    return f"SELECT * FROM {t} ORDER BY {col} {direction}"


def _sql_order_by_limit(meta):
    """ORDER BY + LIMIT"""
    t = meta["name"]
    col = _int_col(meta)
    n = _pick_one([3, 5, 10])
    return f"SELECT * FROM {t} ORDER BY {col} DESC LIMIT {n}"


def _sql_distinct(meta):
    """DISTINCT"""
    t = meta["name"]
    col = _text_col(meta)
    return f"SELECT DISTINCT {col} FROM {t}"


def _sql_group_by_count(meta):
    """GROUP BY + COUNT"""
    t = meta["name"]
    grp = _text_col(meta)
    return f"SELECT {grp}, COUNT(*) FROM {t} GROUP BY {grp}"


def _sql_group_by_having(meta):
    """GROUP BY + HAVING"""
    t = meta["name"]
    grp = _text_col(meta)
    n = _rand_int(2, 10)
    return f"SELECT {grp}, COUNT(*) FROM {t} GROUP BY {grp} HAVING COUNT(*) > {n}"


def _sql_aggregate(meta):
    """聚合函数：SUM/AVG/MIN/MAX"""
    t = meta["name"]
    col = _int_col(meta)
    func = _pick_one(["SUM", "AVG", "MIN", "MAX"])
    return f"SELECT {func}({col}) FROM {t}"


def _sql_join_2table(metas):
    """两表 JOIN"""
    m1, m2 = metas[0], metas[1]
    t1, t2 = m1["name"], m2["name"]
    # 选一个共享语义的 join 列名（用 id 后缀）
    j1 = m1["cols"][0]
    j2 = m2["cols"][0]
    sel_col = m2["cols"][-1]
    cond_col = _int_col(m1, 1) if len(m1["cols"]) > 1 else m1["cols"][0]
    val = _rand_int()
    return (
        f"SELECT a.*, b.{sel_col} FROM {t1} a "
        f"JOIN {t2} b ON a.{j1} = b.{j2} "
        f"WHERE a.{cond_col} > {val}"
    )


def _sql_subquery_in(meta):
    """子查询 IN"""
    t = meta["name"]
    col = _int_col(meta, 0)
    col2 = _int_col(meta, 1) if len(meta["cols"]) > 1 else col
    val = _rand_int()
    return f"SELECT * FROM {t} WHERE {col} IN (SELECT {col} FROM {t} WHERE {col2} > {val})"


def _sql_between(meta):
    """BETWEEN"""
    t = meta["name"]
    col = _int_col(meta)
    v1, v2 = _rand_int(1, 50), _rand_int(51, 100)
    return f"SELECT * FROM {t} WHERE {col} BETWEEN {v1} AND {v2}"


def _sql_like(meta):
    """LIKE"""
    t = meta["name"]
    col = _text_col(meta)
    pattern = _pick_one(["'A%'", "'%B'", "'%X%'", "'test%'"])
    return f"SELECT * FROM {t} WHERE {col} LIKE {pattern}"


def _sql_count_where(meta):
    """COUNT + WHERE"""
    t = meta["name"]
    col = _int_col(meta)
    val = _rand_int()
    return f"SELECT COUNT(*) FROM {t} WHERE {col} > {val}"


def _sql_case_when(meta):
    """CASE WHEN"""
    t = meta["name"]
    col = _int_col(meta)
    val = _rand_int()
    return (
        f"SELECT {col}, CASE WHEN {col} > {val} THEN 'high' ELSE 'low' END "
        f"FROM {t}"
    )


def _sql_group_by_avg(meta):
    """GROUP BY + AVG"""
    t = meta["name"]
    grp = _text_col(meta)
    col = _int_col(meta)
    return f"SELECT {grp}, AVG({col}) FROM {t} GROUP BY {grp}"


def _sql_not_null(meta):
    """IS NOT NULL"""
    t = meta["name"]
    col = _text_col(meta)
    col2 = _int_col(meta)
    val = _rand_int()
    return f"SELECT * FROM {t} WHERE {col} IS NOT NULL AND {col2} > {val}"


# ─────────────────────────────────────────────────────────────────────
# 学生错误模板（模拟真实常见错误）
# ─────────────────────────────────────────────────────────────────────

def _err_missing_where(correct_sql: str, meta) -> str:
    """错误类型：缺失 WHERE"""
    import re
    return re.sub(r'\s+WHERE\s+.*', '', correct_sql, flags=re.IGNORECASE).rstrip(";")


def _err_wrong_column(correct_sql: str, meta) -> str:
    """错误类型：WHERE 中用错列"""
    t = meta["name"]
    wrong_col = _text_col(meta)
    val = _pick_one(_TEXT_VALUES)
    # 简单替换 WHERE 后的条件
    import re
    return re.sub(
        r'WHERE\s+.*',
        f'WHERE {wrong_col} = {val}',
        correct_sql,
        flags=re.IGNORECASE,
    ).rstrip(";")


def _err_missing_distinct(correct_sql: str, meta) -> str:
    """错误类型：缺失 DISTINCT"""
    return correct_sql.replace("DISTINCT ", "").replace("distinct ", "")


def _err_wrong_operator(correct_sql: str, meta) -> str:
    """错误类型：比较运算符写错"""
    import re
    def flip(m):
        op = m.group(0)
        return {">": "<", "<": ">", ">=": "<=", "<=": ">=", "=": "!="}.get(op, op)
    return re.sub(r'>=|<=|!=|>|<|=', flip, correct_sql, count=1)


def _err_select_star_instead_of_cols(correct_sql: str, meta) -> str:
    """错误类型：应该 SELECT 部分列却用了 *"""
    import re
    return re.sub(r'SELECT\s+[^F]+FROM', 'SELECT * FROM', correct_sql,
                  flags=re.IGNORECASE).rstrip(";")


def _err_missing_order_by(correct_sql: str, meta) -> str:
    """错误类型：缺失 ORDER BY"""
    import re
    return re.sub(r'\s+ORDER\s+BY\s+.*', '', correct_sql, flags=re.IGNORECASE).rstrip(";")


def _err_missing_group_by(correct_sql: str, meta) -> str:
    """错误类型：有聚合但缺 GROUP BY"""
    import re
    return re.sub(r'\s+GROUP\s+BY\s+.*', '', correct_sql, flags=re.IGNORECASE).rstrip(";")


def _err_wrong_join_direction(correct_sql: str, metas) -> str:
    """错误类型：JOIN 条件写反"""
    # 简单处理：把 JOIN 替换为 CROSS JOIN
    import re
    return re.sub(r'JOIN\s+\w+\s+\w+\s+ON\s+[^ ]+\s*=\s*[^ ]+',
                  f'CROSS JOIN {metas[1]["name"]}',
                  correct_sql, flags=re.IGNORECASE).rstrip(";")


def _err_missing_having(correct_sql: str, meta) -> str:
    """错误类型：缺 HAVING"""
    import re
    return re.sub(r'\s+HAVING\s+.*', '', correct_sql, flags=re.IGNORECASE).rstrip(";")


def _err_wrong_aggregate(correct_sql: str, meta) -> str:
    """错误类型：聚合函数用错"""
    import re
    wrong = _pick_one(["SUM", "AVG", "MIN", "MAX", "COUNT"])
    return re.sub(r'(SUM|AVG|MIN|MAX|COUNT)\(', f'{wrong}(', correct_sql, count=1)


def _err_extra_column(correct_sql: str, meta) -> str:
    """错误类型：SELECT 中多了不该选的列"""
    import re
    extra_col = _text_col(meta)
    return re.sub(r'SELECT\s+', f'SELECT {extra_col}, ', correct_sql, count=1)


# 错误生成器池
_ERROR_GENERATORS_1TABLE = [
    _err_missing_where,
    _err_wrong_column,
    _err_wrong_operator,
    _err_select_star_instead_of_cols,
    _err_missing_order_by,
    _err_missing_group_by,
    _err_missing_having,
    _err_wrong_aggregate,
    _err_extra_column,
]


# ─────────────────────────────────────────────────────────────────────
# 问题生成主函数
# ─────────────────────────────────────────────────────────────────────

# SQL 模板池（单表）
_SQL_TEMPLATES_1TABLE = [
    _sql_where_simple,
    _sql_where_text,
    _sql_where_and,
    _sql_where_or,
    _sql_select_cols,
    _sql_order_by,
    _sql_order_by_limit,
    _sql_distinct,
    _sql_group_by_count,
    _sql_group_by_having,
    _sql_aggregate,
    _sql_subquery_in,
    _sql_between,
    _sql_like,
    _sql_count_where,
    _sql_case_when,
    _sql_group_by_avg,
    _sql_not_null,
]


def generate_problems(n: int = 1000) -> list[dict]:
    """
    生成 n 道不同 SQL 题目。
    返回 [{"id", "schema", "correct", "student_errors": [str]}]
    """
    problems = []
    _rng.seed(SEED)

    for i in range(n):
        # 70% 单表，30% 双表
        num_tables = 1 if _rng.random() < 0.7 else 2
        schema_text, metas = _gen_schema(i, num_tables)

        # 生成正确 SQL
        if num_tables == 2:
            correct_sql = _sql_join_2table(metas)
        else:
            template = _pick_one(_SQL_TEMPLATES_1TABLE)
            correct_sql = template(metas[0])

        correct_sql = correct_sql.rstrip(";")

        # 生成 1-2 个学生错误
        num_errors = _rng.randint(1, 2)
        student_errors = []
        error_gens = list(_ERROR_GENERATORS_1TABLE)
        _rng.shuffle(error_gens)
        for eg in error_gens[:num_errors]:
            try:
                if eg == _err_wrong_join_direction and num_tables == 2:
                    err = eg(correct_sql, metas)
                elif eg == _err_wrong_join_direction:
                    continue
                else:
                    err = eg(correct_sql, metas[0])
                err = err.strip()
                if err and err.lower() != correct_sql.lower():
                    student_errors.append(err)
            except Exception:
                continue

        if not student_errors:
            # 保底：至少生成一个缺失 WHERE 的错误
            err = _err_missing_where(correct_sql, metas[0])
            if err and err.lower() != correct_sql.lower():
                student_errors.append(err)

        problems.append({
            "id": i,
            "schema": schema_text,
            "correct": correct_sql,
            "student_errors": student_errors,
        })

    return problems


# ─────────────────────────────────────────────────────────────────────
# 展开为测试用例
# ─────────────────────────────────────────────────────────────────────

def build_cases() -> list[dict]:
    """展开所有 problems 为 (schema, correct, student) 用例"""
    problems = generate_problems(1000)
    cases = []
    for p in problems:
        # 正确 SQL 自身作为 baseline
        cases.append({
            "id": p["id"],
            "schema": p["schema"],
            "correct": p["correct"],
            "student": p["correct"],
            "expected": "equivalent",
        })
        # 学生错误
        for j, err in enumerate(p["student_errors"]):
            cases.append({
                "id": p["id"],
                "schema": p["schema"],
                "correct": p["correct"],
                "student": err,
                "expected": "not_equivalent_or_mutation",
            })
    return cases


# ─────────────────────────────────────────────────────────────────────
# 测试类
# ─────────────────────────────────────────────────────────────────────

class TestPhase1KProblems:
    """~1000 道不同题目的 Phase 1 pipeline 验证"""

    @pytest.fixture(scope="class", autouse=True)
    def _load(self, request):
        cls = request.cls
        cls._cases = build_cases()
        cls._problems = generate_problems(1000)
        n_correct = sum(1 for c in cls._cases if c["expected"] == "equivalent")
        n_error = sum(1 for c in cls._cases if c["expected"] != "equivalent")
        print(f"\n共 {len(cls._problems)} 道题目, "
              f"{n_correct} 正确用例 + {n_error} 错误用例 = {len(cls._cases)} 总用例")

    def test_problem_count(self):
        """应有 ≥ 1000 道不同题目"""
        assert len(self._problems) >= 1000

    def test_pipeline_no_crash(self):
        """全部用例沙盒能跑完，崩溃率 < 10%"""
        errors = []
        t0 = time.time()
        for i, c in enumerate(self._cases):
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
            except Exception as e:
                errors.append((i, c["id"], str(e)[:80]))
        elapsed = time.time() - t0
        rate = len(self._cases) / elapsed if elapsed > 0 else 0
        crash_rate = len(errors) / len(self._cases)
        print(f"\n  {len(self._cases)} 用例, 耗时 {elapsed:.1f}s, "
              f"速率 {rate:.0f}/s, 崩溃 {len(errors)} ({crash_rate:.1%})")
        if errors[:5]:
            for idx, pid, err in errors[:5]:
                print(f"    [{idx}] prob={pid}: {err}")
        assert crash_rate < 0.10, f"崩溃率 {crash_rate:.1%} 超过 10%"

    def test_correct_equivalence_rate(self):
        """正确 SQL 应与自身等价（≥ 90%）"""
        correct_cases = [c for c in self._cases if c["expected"] == "equivalent"]
        eq = 0
        total = 0
        for c in correct_cases:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                total += 1
                if result.executed and result.is_equivalent:
                    eq += 1
            except Exception:
                total += 1
        rate = eq / total if total > 0 else 0
        print(f"\n  正确等价率: {eq}/{total} = {rate:.1%}")
        assert rate >= 0.90, f"正确等价率 {rate:.1%} 低于 90%"

    def test_error_detection_rate(self):
        """学生错误：沙盒判不等价 OR mutation 识别修复（≥ 60%）"""
        error_cases = [c for c in self._cases if c["expected"] == "not_equivalent_or_mutation"]
        detected = 0
        total = 0
        for c in error_cases:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    continue
                total += 1
                if not result.is_equivalent:
                    detected += 1
                else:
                    # 沙盒等价时检查 mutation 是否捕获
                    mut = result.mutation_evidence or {}
                    tests = mut.get("tests", [])
                    if any(t.get("fixed_by_replacement") for t in tests):
                        detected += 1
            except Exception:
                continue
        rate = detected / total if total > 0 else 0
        print(f"\n  错误检出率: {detected}/{total} = {rate:.1%}")
        assert rate >= 0.60, f"错误检出率 {rate:.1%} 低于 60%"

    def test_summary_report(self):
        """打印完整统计摘要"""
        stats = {
            "total_problems": len(self._problems),
            "total_cases": len(self._cases),
            "syntax_error": 0,
            "executed": 0,
            "correct_equivalent": 0,
            "correct_not_equivalent": 0,
            "error_not_equivalent": 0,
            "error_equivalent_no_mutation": 0,
            "error_equivalent_mutation_caught": 0,
            "mutation_fixed_total": 0,
        }

        for c in self._cases:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    stats["syntax_error"] += 1
                    continue
                stats["executed"] += 1

                mut = result.mutation_evidence or {}
                mutation_caught = any(
                    t.get("fixed_by_replacement") for t in mut.get("tests", [])
                )
                if mutation_caught:
                    stats["mutation_fixed_total"] += 1

                if c["expected"] == "equivalent":
                    if result.is_equivalent:
                        stats["correct_equivalent"] += 1
                    else:
                        stats["correct_not_equivalent"] += 1
                else:
                    if not result.is_equivalent:
                        stats["error_not_equivalent"] += 1
                    elif mutation_caught:
                        stats["error_equivalent_mutation_caught"] += 1
                    else:
                        stats["error_equivalent_no_mutation"] += 1
            except Exception:
                stats["syntax_error"] += 1

        total_correct = stats["correct_equivalent"] + stats["correct_not_equivalent"]
        total_error = (stats["error_not_equivalent"]
                       + stats["error_equivalent_mutation_caught"]
                       + stats["error_equivalent_no_mutation"])

        print("\n" + "=" * 60)
        print("Phase 1 ~1000题 大规模测试摘要")
        print("=" * 60)
        print(f"题目总数:          {stats['total_problems']}")
        print(f"测试用例总数:      {stats['total_cases']}")
        print(f"语法解析失败:      {stats['syntax_error']} "
              f"({stats['syntax_error']/stats['total_cases']:.1%})")
        print(f"沙盒成功执行:      {stats['executed']} "
              f"({stats['executed']/stats['total_cases']:.1%})")
        print(f"")
        print(f"正确 SQL 等价:     {stats['correct_equivalent']}/{total_correct} "
              f"({stats['correct_equivalent']/max(total_correct,1):.1%})")
        print(f"正确 SQL 不等价:   {stats['correct_not_equivalent']}/{total_correct}")
        print(f"")
        print(f"错误 检出(不等价): {stats['error_not_equivalent']}/{total_error} "
              f"({stats['error_not_equivalent']/max(total_error,1):.1%})")
        print(f"错误 mutation捕获: {stats['error_equivalent_mutation_caught']}/{total_error}")
        print(f"错误 漏检:         {stats['error_equivalent_no_mutation']}/{total_error} "
              f"({stats['error_equivalent_no_mutation']/max(total_error,1):.1%})")
        print(f"")
        print(f"mutation 修复总计: {stats['mutation_fixed_total']}")
        print("=" * 60)
