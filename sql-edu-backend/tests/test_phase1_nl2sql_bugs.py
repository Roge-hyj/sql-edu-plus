"""
Phase 1 Pipeline 真实数据验证（~760 道题目，来自 NL2SQL-BUGs + BIRD）
数据来源:
  - NL2SQL-BUGs (HKUSTDial/NL2SQL-Bugs-Benchmark): 999 条真实 LLM/学生错误 SQL
  - BIRD mini dev: 500 条正确 SQL
  - 11 个真实数据库，76 张表
Schema 从正确 SQL 推断（列名小写归一化）
"""
import sys, os, json, time, re
sys.path.insert(0, os.path.dirname(__file__) + "/..")

import pytest
from core.parseval_data_generator import generate_and_compare


# ─────────────────────────────────────────────────────────────────────
# 数据加载
# ─────────────────────────────────────────────────────────────────────

_BIRD_PATH = "/tmp/bird_mini_dev.json"
_BUGS_PATH = "/tmp/nl2sql_bugs_evidence.json"
_SCHEMA_PATH = "/tmp/bird_schemas_clean.json"


def _load_data():
    """加载所有数据，返回 (schemas, bird, bugs, test_cases)"""
    if not all(os.path.exists(p) for p in [_BIRD_PATH, _BUGS_PATH, _SCHEMA_PATH]):
        return None, None, None, []

    with open(_SCHEMA_PATH) as f:
        schemas = json.load(f)
    with open(_BIRD_PATH) as f:
        bird = json.load(f)
    with open(_BUGS_PATH) as f:
        bugs = json.load(f)

    # 构建正确 SQL 索引: (db_id, question_lower) → correct_sql
    correct_index = {}
    for d in bird:
        key = (d['db_id'], d['question'].strip().lower())
        correct_index[key] = d['SQL']
    for d in bugs:
        if d['label']:
            key = (d['db_id'], d['question'].strip().lower())
            correct_index[key] = d['sql']

    # 构建测试用例：每条 buggy SQL 配对正确 SQL
    test_cases = []
    seen_pairs = set()

    for d in bugs:
        if d['label']:
            continue  # 只处理错误 SQL
        db = d['db_id']
        q = d['question'].strip().lower()
        key = (db, q)

        if key not in correct_index:
            continue
        if db not in schemas:
            continue

        correct_sql = correct_index[key]
        buggy_sql = d['sql']

        # 去重
        pair_key = (db, correct_sql.lower(), buggy_sql.lower())
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # 清理 SQL（去掉 backticks，统一小写表/列名与 schema 对齐）
        correct_clean = _clean_sql(correct_sql)
        buggy_clean = _clean_sql(buggy_sql)

        if not correct_clean or not buggy_clean:
            continue
        if correct_clean.lower() == buggy_clean.lower():
            continue  # 跳过标注为错误但实际相同的

        test_cases.append({
            "db_id": db,
            "question": d['question'],
            "schema": schemas[db],
            "correct": correct_clean,
            "student": buggy_clean,
            "error_types": d.get('error_types', []),
        })

    # 也加入正确 SQL 自身作为等价性 baseline（去重后）
    seen_correct = set()
    for d in bird:
        db = d['db_id']
        if db not in schemas:
            continue
        sql_clean = _clean_sql(d['SQL'])
        if not sql_clean:
            continue
        ckey = (db, sql_clean.lower())
        if ckey in seen_correct:
            continue
        seen_correct.add(ckey)
        test_cases.append({
            "db_id": db,
            "question": d['question'],
            "schema": schemas[db],
            "correct": sql_clean,
            "student": sql_clean,
            "error_types": [],
            "is_baseline": True,
        })

    return schemas, bird, bugs, test_cases


def _clean_sql(sql: str) -> str:
    """清理 SQL：去 backticks，去多余空白"""
    if not sql:
        return ""
    sql = sql.replace('`', '')
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql.rstrip(';')


# ─────────────────────────────────────────────────────────────────────
# 测试
# ─────────────────────────────────────────────────────────────────────

class TestPhase1RealNL2SQL:
    """
    真实 NL2SQL-BUGs 数据测试:
    - ~760 道有真实错误的题目（LLM 生成 SQL，人工标注错误类型）
    - ~500 道正确 SQL baseline
    - 11 个真实数据库，76 张表
    """

    @pytest.fixture(scope="class", autouse=True)
    def _load(self, request):
        cls = request.cls
        schemas, bird, bugs, cases = _load_data()
        if not cases:
            pytest.skip("数据文件不存在，请先运行数据下载脚本")
        cls._schemas = schemas
        cls._cases = cases
        cls._buggy = [c for c in cases if not c.get("is_baseline")]
        cls._baseline = [c for c in cases if c.get("is_baseline")]
        print(f"\n加载: {len(cls._buggy)} 错误用例 + {len(cls._baseline)} 正确 baseline "
              f"= {len(cls._cases)} 总用例")
        print(f"数据库: {len(schemas)} ({', '.join(sorted(schemas.keys()))})")

    def test_data_loaded(self):
        """数据加载检查"""
        assert len(self._buggy) >= 500, f"错误用例不足: {len(self._buggy)}"
        assert len(self._baseline) >= 200, f"正确 baseline 不足: {len(self._baseline)}"

    def test_pipeline_no_crash(self):
        """全部用例：pipeline 不崩溃（崩溃率 < 20%）"""
        errors = []
        t0 = time.time()
        for i, c in enumerate(self._cases):
            try:
                generate_and_compare(c["schema"], c["correct"], c["student"])
            except Exception as e:
                errors.append((i, c["db_id"], c["correct"][:50], str(e)[:80]))
        elapsed = time.time() - t0
        rate = len(self._cases) / elapsed if elapsed > 0 else 0
        crash_rate = len(errors) / len(self._cases)
        print(f"\n  {len(self._cases)} 用例, {elapsed:.1f}s, {rate:.0f}/s")
        print(f"  崩溃: {len(errors)} ({crash_rate:.1%})")
        if errors[:5]:
            for idx, db, sql, err in errors[:5]:
                print(f"    [{idx}] {db}: {sql!r} → {err}")
        assert crash_rate < 0.20, f"崩溃率 {crash_rate:.1%} 超过 20%"

    def test_correct_baseline_equivalence(self):
        """正确 SQL 自身比较：等价率 ≥ 70%"""
        eq = 0
        executed = 0
        for c in self._baseline:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if result.executed:
                    executed += 1
                    if result.is_equivalent:
                        eq += 1
            except Exception:
                continue
        rate = eq / executed if executed > 0 else 0
        print(f"\n  正确等价率: {eq}/{executed} = {rate:.1%} (执行成功 {executed}/{len(self._baseline)})")
        assert rate >= 0.70, f"正确等价率 {rate:.1%} 低于 70%"

    def test_buggy_detection_rate(self):
        """错误检出率：沙盒判不等价 OR mutation 识别（≥ 50%）"""
        detected = 0
        total = 0
        for c in self._buggy:
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    continue
                total += 1
                if not result.is_equivalent:
                    detected += 1
                else:
                    mut = result.mutation_evidence or {}
                    tests = mut.get("tests", [])
                    if any(t.get("fixed_by_replacement") for t in tests):
                        detected += 1
            except Exception:
                continue
        rate = detected / total if total > 0 else 0
        print(f"\n  错误检出率: {detected}/{total} = {rate:.1%}")
        assert rate >= 0.50, f"错误检出率 {rate:.1%} 低于 50%"

    def test_detection_by_error_type(self):
        """按错误类型分析检出率"""
        from collections import defaultdict
        type_stats = defaultdict(lambda: {"total": 0, "detected": 0})

        for c in self._buggy:
            etypes = [et['error_type'] for et in c.get('error_types', [])]
            if not etypes:
                etypes = ['Unknown']
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    continue
                is_detected = not result.is_equivalent
                if result.is_equivalent:
                    mut = result.mutation_evidence or {}
                    tests = mut.get("tests", [])
                    if any(t.get("fixed_by_replacement") for t in tests):
                        is_detected = True
                for et in etypes:
                    type_stats[et]["total"] += 1
                    if is_detected:
                        type_stats[et]["detected"] += 1
            except Exception:
                continue

        print(f"\n  {'错误类型':45s} {'检出':>5s}/{'总数':>5s} {'检出率':>8s}")
        print(f"  {'-'*70}")
        for et, stats in sorted(type_stats.items(), key=lambda x: -x[1]['total']):
            rate = stats['detected'] / stats['total'] if stats['total'] > 0 else 0
            print(f"  {et:45s} {stats['detected']:5d}/{stats['total']:5d} {rate:7.1%}")

    def test_detection_by_database(self):
        """按数据库分析检出率"""
        from collections import defaultdict
        db_stats = defaultdict(lambda: {"total": 0, "detected": 0, "crash": 0})

        for c in self._buggy:
            db = c['db_id']
            try:
                result = generate_and_compare(c["schema"], c["correct"], c["student"])
                if not result.executed:
                    db_stats[db]["crash"] += 1
                    continue
                db_stats[db]["total"] += 1
                is_detected = not result.is_equivalent
                if result.is_equivalent:
                    mut = result.mutation_evidence or {}
                    tests = mut.get("tests", [])
                    if any(t.get("fixed_by_replacement") for t in tests):
                        is_detected = True
                if is_detected:
                    db_stats[db]["detected"] += 1
            except Exception:
                db_stats[db]["crash"] += 1

        print(f"\n  {'数据库':30s} {'检出':>5s}/{'总数':>5s} {'检出率':>8s} {'崩溃':>5s}")
        print(f"  {'-'*65}")
        for db in sorted(db_stats):
            s = db_stats[db]
            rate = s['detected'] / s['total'] if s['total'] > 0 else 0
            print(f"  {db:30s} {s['detected']:5d}/{s['total']:5d} {rate:7.1%} {s['crash']:5d}")

    def test_summary_report(self):
        """打印完整统计摘要"""
        stats = {
            "total_cases": len(self._cases),
            "buggy_cases": len(self._buggy),
            "baseline_cases": len(self._baseline),
            "syntax_error": 0,
            "executed": 0,
            "correct_equivalent": 0,
            "correct_not_equivalent": 0,
            "error_detected_not_eq": 0,
            "error_detected_mutation": 0,
            "error_missed": 0,
            "mutation_fixed_total": 0,
        }
        db_counts = {}

        for c in self._cases:
            db = c['db_id']
            db_counts.setdefault(db, 0)
            db_counts[db] += 1

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

                if c.get("is_baseline"):
                    if result.is_equivalent:
                        stats["correct_equivalent"] += 1
                    else:
                        stats["correct_not_equivalent"] += 1
                else:
                    if not result.is_equivalent:
                        stats["error_detected_not_eq"] += 1
                    elif mutation_caught:
                        stats["error_detected_mutation"] += 1
                    else:
                        stats["error_missed"] += 1
            except Exception:
                stats["syntax_error"] += 1

        total_buggy_executed = (stats["error_detected_not_eq"]
                                + stats["error_detected_mutation"]
                                + stats["error_missed"])
        total_baseline_executed = stats["correct_equivalent"] + stats["correct_not_equivalent"]

        print("\n" + "=" * 70)
        print("Phase 1 NL2SQL-BUGs 真实数据测试摘要")
        print("=" * 70)
        print(f"题目总数:          {stats['total_cases']} "
              f"(错误 {stats['buggy_cases']} + 正确 baseline {stats['baseline_cases']})")
        print(f"数据库数:          {len(db_counts)}")
        print(f"语法解析失败:      {stats['syntax_error']} "
              f"({stats['syntax_error']/stats['total_cases']:.1%})")
        print(f"沙盒成功执行:      {stats['executed']} "
              f"({stats['executed']/stats['total_cases']:.1%})")
        print(f"")
        if total_baseline_executed > 0:
            print(f"正确 baseline 等价: {stats['correct_equivalent']}/{total_baseline_executed} "
                  f"({stats['correct_equivalent']/total_baseline_executed:.1%})")
        print(f"")
        if total_buggy_executed > 0:
            total_detected = stats["error_detected_not_eq"] + stats["error_detected_mutation"]
            print(f"错误检出(不等价):  {stats['error_detected_not_eq']}/{total_buggy_executed} "
                  f"({stats['error_detected_not_eq']/total_buggy_executed:.1%})")
            print(f"错误检出(mutation): {stats['error_detected_mutation']}/{total_buggy_executed} "
                  f"({stats['error_detected_mutation']/total_buggy_executed:.1%})")
            print(f"错误漏检:          {stats['error_missed']}/{total_buggy_executed} "
                  f"({stats['error_missed']/total_buggy_executed:.1%})")
            print(f"总检出率:          {total_detected}/{total_buggy_executed} "
                  f"({total_detected/total_buggy_executed:.1%})")
        print(f"")
        print(f"mutation 修复总计: {stats['mutation_fixed_total']}")
        print(f"\n按数据库分布: {db_counts}")
        print("=" * 70)
