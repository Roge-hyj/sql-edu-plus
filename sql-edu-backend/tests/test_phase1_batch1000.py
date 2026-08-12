"""
Phase 1 Pipeline 大批量验证测试（~1000 条真实学生提交）
数据来源：kpresler/SQLRepair (GitHub)
"""
import sys, os, re, csv, time
sys.path.insert(0, os.path.dirname(__file__) + "/..")

import pytest
from core.parseval_data_generator import generate_and_compare


# ─────────────────────────────────────────────────────────────────────
# 数据准备：Problem schema + correct SQL
# ─────────────────────────────────────────────────────────────────────

PROBLEMS = {
    "Problem 1": {
        "schema": "alpha(COL, DES, MIN, AV, MAX, FIL)",
        "solution": "SELECT * FROM alpha WHERE MIN = 0",
    },
    "Problem 2": {
        "schema": "bravo(CUI1, AUI1, STYPE1, REL, CUI2, AUI2, STYPE2, RUI)",
        "solution": "SELECT CUI1, RUI FROM bravo WHERE CUI2 = 'C0364349'",
    },
    "Problem 3": {
        "schema": "charlie(RSAB, SF, TFR, CFR, TTYL)",
        "solution": "SELECT * FROM charlie WHERE CFR <= 1634",
    },
    "Problem 4": {
        "schema": "delta(RSAB, SF, TFR, CFR, TTYL)",
        "solution": "SELECT RSAB, TFR FROM delta WHERE CFR <= 1634",
    },
    "Problem 5": {
        "schema": "echo(MRRANK_RANK, SAB, TTY, SUPPRESS)",
        "solution": "SELECT * FROM echo WHERE MRRANK_RANK < 380 OR TTY = 'CD'",
    },
    "Problem 6": {
        "schema": "foxtrot(MRRANK_RANK, SAB, TTY, SUPPRESS)",
        "solution": "SELECT * FROM foxtrot WHERE MRRANK_RANK > 400 AND TTY = 'SY' OR TTY = 'PT'",
    },
    "Problem 7": {
        "schema": "golf(VCUI, RCUI, VSAB, RSAB, SON, SF, SVER)",
        "solution": "SELECT DISTINCT SVER FROM golf WHERE SVER < 1996",
    },
    "Problem 8": {
        "schema": "hotel(CUI, TUI, STN)",
        "solution": "SELECT * FROM hotel ORDER BY TUI DESC, CUI ASC",
    },
    "Problem 9": {
        "schema": "india(CUI, TUI, CVF); juliett(CUI, LAT, TS, LUI, STT, SUI, ISPREF)",
        "solution": "SELECT LAT, STT, ISPREF FROM india a, juliett b WHERE CVF != 256 AND a.CUI = b.CUI",
    },
    "Problem 10": {
        "schema": "kilo(LUI, CUI)",
        "solution": "SELECT * FROM kilo GROUP BY LUI",
    },
}

# 表名 → problem 映射（用于推断 su19 CSV 中缺失的 problem 列）
TABLE_TO_PROBLEM = {
    "alpha": "Problem 1", "alpha2": "Problem 1", "alpha3": "Problem 1",
    "bravo": "Problem 2", "bravo2": "Problem 2",
    "charlie": "Problem 3", "charlie2": "Problem 3", "charlie3": "Problem 3",
    "delta": "Problem 4",
    "echo": "Problem 5", "echo2": "Problem 5",
    "foxtrot": "Problem 6", "foxtrot2": "Problem 6",
    "golf": "Problem 7", "golf2": "Problem 7",
    "hotel": "Problem 8", "hotel2": "Problem 8",
    "india": "Problem 9", "juliett": "Problem 9",
    "kilo": "Problem 10", "kilo2": "Problem 10",
}


def _infer_problem(student_sql: str) -> str | None:
    """从 SQL 文本推断 problem（用于 su19 CSV）"""
    sql_lower = student_sql.lower()
    for table, prob in TABLE_TO_PROBLEM.items():
        if table in sql_lower:
            return prob
    return None


def _load_csv(path: str) -> list[dict]:
    """加载 CSV，统一字段名"""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for r in reader:
            stmt = r.get("statement", "").strip()
            if not stmt:
                continue
            problem = r.get("problem") or _infer_problem(stmt)
            correct = r.get("correct")  # None if column absent
            rows.append({
                "statement": stmt,
                "problem": problem,
                "correct": correct,
            })
    return rows


# ─────────────────────────────────────────────────────────────────────
# 构建测试用例（去重）
# ─────────────────────────────────────────────────────────────────────

def build_cases() -> list[dict]:
    all_rows = []
    for csvf in [
        "/tmp/sqlrepair_216_f20.csv",
        "/tmp/sqlrepair_216_su19.csv",
        "/tmp/sqlrepair_326_f20.csv",
    ]:
        if os.path.exists(csvf):
            all_rows.extend(_load_csv(csvf))

    cases = []
    seen = set()
    for r in all_rows:
        prob = r["problem"]
        if prob not in PROBLEMS:
            continue
        student_sql = r["statement"].rstrip(";").strip()
        if not student_sql:
            continue
        # 跳过纯语法错误（缺少 FROM 等 sqlglot 无法解析的）
        key = (prob, student_sql.lower())
        if key in seen:
            continue
        seen.add(key)
        cases.append({
            "problem": prob,
            "schema": PROBLEMS[prob]["schema"],
            "correct": PROBLEMS[prob]["solution"],
            "student": student_sql,
            "label_correct": r["correct"],  # 原始标注（可能为 None）
        })
    return cases


# ─────────────────────────────────────────────────────────────────────
# 测试
# ─────────────────────────────────────────────────────────────────────

class TestPhase1Batch1000:
    """大批量：~1000 条真实学生提交的 Phase 1 流水线稳定性测试"""

    @pytest.fixture(scope="class", autouse=True)
    def _load_cases(self, request):
        cls = request.cls
        cls._cases = build_cases()
        print(f"\n共加载 {len(cls._cases)} 条去重测试用例")

    def test_pipeline_does_not_crash(self):
        """全部用例：沙盒能跑完，不抛异常"""
        errors = []
        t0 = time.time()
        for i, c in enumerate(self._cases):
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
            except Exception as e:
                errors.append((i, c["problem"], c["student"][:60], str(e)[:80]))
        elapsed = time.time() - t0
        rate = len(self._cases) / elapsed if elapsed > 0 else 0
        print(f"\n  {len(self._cases)} 条跑完，耗时 {elapsed:.1f}s，速率 {rate:.0f} 条/s")
        if errors:
            print(f"  异常 {len(errors)} 条（前 5 条）：")
            for idx, prob, sql, err in errors[:5]:
                print(f"    [{idx}] {prob}: {sql!r} → {err}")
        # 允许少量语法解析失败（学生 SQL 有严重语法错误时 sqlglot 无法解析）
        crash_rate = len(errors) / len(self._cases)
        assert crash_rate < 0.15, \
            f"崩溃率 {crash_rate:.1%} 超过 15%（{len(errors)}/{len(self._cases)}）"

    def test_executed_rate_high(self):
        """沙盒执行成功率应 ≥ 70%（部分学生 SQL 语法错误无法解析是正常的）"""
        executed_count = 0
        total = 0
        for c in self._cases:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                total += 1
                if result.executed:
                    executed_count += 1
            except Exception:
                total += 1
        rate = executed_count / total if total > 0 else 0
        print(f"\n  执行成功率: {executed_count}/{total} = {rate:.1%}")
        assert rate >= 0.70, f"沙盒执行成功率 {rate:.1%} 低于 70%"

    def test_labeled_wrong_not_equivalent_or_mutation_catches(self):
        """标注为 correct=0 的提交：沙盒判不等价，或 mutation 识别修复"""
        wrong_cases = [c for c in self._cases if c["label_correct"] == "0"]
        missed = []
        total = 0
        for c in wrong_cases:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    continue  # 语法错误无法进入沙盒，不计入
                total += 1
                if result.is_equivalent:
                    mut = result.mutation_evidence or {}
                    tests = mut.get("tests", [])
                    fixed = any(t.get("fixed_by_replacement") for t in tests)
                    if not fixed:
                        missed.append(c)
            except Exception:
                continue
        miss_rate = len(missed) / total if total > 0 else 0
        print(f"\n  错误检出: {total - len(missed)}/{total}，漏检率 {miss_rate:.1%}")
        if missed:
            print(f"  漏检案例（前 5）：")
            for c in missed[:5]:
                print(f"    [{c['problem']}] {c['student'][:70]}")
        # 允许 ≤ 30% 漏检（造数不一定覆盖所有边缘错误模式）
        assert miss_rate < 0.30, f"漏检率 {miss_rate:.1%} 超过 30%"

    def test_correct_labeled_equivalent(self):
        """标注为 correct=1 的提交：大多数应判为等价（允许等价多解）"""
        correct_cases = [c for c in self._cases if c["label_correct"] == "1"]
        if not correct_cases:
            pytest.skip("无 correct=1 标注数据")
        eq_count = 0
        total = 0
        for c in correct_cases:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    continue
                total += 1
                if result.is_equivalent:
                    eq_count += 1
            except Exception:
                continue
        rate = eq_count / total if total > 0 else 0
        print(f"\n  正确提交等价率: {eq_count}/{total} = {rate:.1%}")
        # 正确提交不一定与标准答案结构相同（CTE/子查询等等价写法）
        # 随机造数不一定能证明所有等价变体，≥ 40% 即可
        assert rate >= 0.40, f"正确提交等价率 {rate:.1%} 低于 40%"

    def test_summary_report(self):
        """输出统计摘要（不 assert，只打印）"""
        stats = {
            "total_cases": len(self._cases),
            "by_problem": {},
            "syntax_error": 0,
            "executed": 0,
            "is_equivalent": 0,
            "not_equivalent": 0,
            "mutation_fixed": 0,
        }
        for c in self._cases:
            prob = c["problem"]
            stats["by_problem"].setdefault(prob, 0)
            stats["by_problem"][prob] += 1
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    stats["syntax_error"] += 1
                    continue
                stats["executed"] += 1
                if result.is_equivalent:
                    stats["is_equivalent"] += 1
                else:
                    stats["not_equivalent"] += 1
                mut = result.mutation_evidence or {}
                if any(t.get("fixed_by_replacement") for t in mut.get("tests", [])):
                    stats["mutation_fixed"] += 1
            except Exception:
                stats["syntax_error"] += 1

        print("\n" + "=" * 60)
        print("Phase 1 批量测试摘要")
        print("=" * 60)
        print(f"总用例数:       {stats['total_cases']}")
        print(f"语法解析失败:   {stats['syntax_error']} ({stats['syntax_error']/stats['total_cases']:.1%})")
        print(f"沙盒成功执行:   {stats['executed']} ({stats['executed']/stats['total_cases']:.1%})")
        print(f"  等价:         {stats['is_equivalent']}")
        print(f"  不等价:       {stats['not_equivalent']}")
        print(f"mutation 修复:  {stats['mutation_fixed']}")
        print(f"按 Problem 分布: {stats['by_problem']}")
        print("=" * 60)
