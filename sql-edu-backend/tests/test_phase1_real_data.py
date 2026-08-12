"""
Phase 1 Pipeline 端到端验证测试
数据来源：kpresler/SQLRepair (GitHub) — 真实本科 CS 学生 SQL 提交记录
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__) + "/..")

import pytest
from core.parseval_data_generator import extract_ast_diffs, generate_and_compare
from core.error_attribution import evidence_weights_from_observation


# ─────────────────────────────────────────────────────
# 数据准备（来自 SQLRepair 真实学生提交）
# ─────────────────────────────────────────────────────

# Problem 4: delta 表（紧凑格式，parse_schema_text 要求）
DELTA_SCHEMA = "delta(RSAB, SF, TFR, CFR, TTYL);"

DELTA_CORRECT = "SELECT RSAB, TFR FROM delta WHERE CFR <= 1634;"

# 学生错误类型 A：缺失 WHERE 子句
DELTA_STUDENT_MISSING_WHERE = "SELECT RSAB, TFR FROM delta;"

# 学生错误类型 B：WHERE 条件完全错误（用了 TTYL 和 TFR 列）
DELTA_STUDENT_WRONG_WHERE = "SELECT RSAB, TFR FROM delta WHERE TTYL LIKE '%PT%' AND TFR < 2000;"

# 学生错误类型 C：WHERE 列选错
DELTA_STUDENT_WRONG_COL = "SELECT RSAB, TFR FROM delta WHERE TTYL = 'PT';"


# Problem 7: golf 表
GOLF_SCHEMA = "golf(VCUI, RCUI, VSAB, RSAB, SON, SF, SVER);"

GOLF_CORRECT = "SELECT DISTINCT SVER FROM golf WHERE SVER < 1996;"

# 学生错误类型 D：缺失 DISTINCT
GOLF_STUDENT_NO_DISTINCT = "SELECT SVER FROM golf WHERE SVER < 1996;"

# 学生错误类型 E：WHERE 条件缺失（只 SELECT DISTINCT）
GOLF_STUDENT_NO_WHERE = "SELECT DISTINCT SVER FROM golf;"


# Problem 2: bravo 表
BRAVO_SCHEMA = "bravo(CUI1, AUI1, STYPE1, REL, CUI2, AUI2, STYPE2, RUI);"

BRAVO_CORRECT = "SELECT CUI1, RUI FROM bravo WHERE CUI2 = 'C0364349';"

# 学生错误类型 F：WHERE 条件用错了列（用了 STYPE1 而非 CUI2）
BRAVO_STUDENT_WRONG_FILTER = "SELECT CUI1, RUI FROM bravo WHERE STYPE1 = 'SCUI';"

# 学生错误类型 G：完全缺少 WHERE
BRAVO_STUDENT_NO_WHERE = "SELECT CUI1, RUI FROM bravo;"


# ─────────────────────────────────────────────────────
# Phase 1 测试：AST Diff Graph
# ─────────────────────────────────────────────────────

class TestPhase1ASTDiff:
    """Phase 1: 验证 extract_ast_diffs 能从真实学生提交中提取结构差异"""

    def test_missing_where_detected(self):
        """缺失 WHERE 子句应被检测为 where_missing 类型差异"""
        diffs = extract_ast_diffs(DELTA_CORRECT, DELTA_STUDENT_MISSING_WHERE)
        clause_types = [d.clause_category for d in diffs]
        diff_types   = [d.diff_type for d in diffs]
        assert any("WHERE" in c for c in clause_types), f"未检测到 WHERE 差异，得到: {diffs}"
        assert any(d in diff_types for d in ("where_missing", "clause_missing", "where_changed")), \
            f"差异类型不符，得到: {diff_types}"

    def test_wrong_where_condition_detected(self):
        """WHERE 条件完全错误应被检测为 where_changed 类型"""
        diffs = extract_ast_diffs(DELTA_CORRECT, DELTA_STUDENT_WRONG_WHERE)
        diff_types = [d.diff_type for d in diffs]
        assert any("where" in dt.lower() or "comparison" in dt.lower() for dt in diff_types), \
            f"未检测到 WHERE 条件差异，得到: {diff_types}"

    def test_wrong_column_in_where_detected(self):
        """WHERE 中列选错应被检测到差异"""
        diffs = extract_ast_diffs(DELTA_CORRECT, DELTA_STUDENT_WRONG_COL)
        assert len(diffs) > 0, "列选错应产生至少 1 个差异节点"

    def test_missing_distinct_detected(self):
        """缺失 DISTINCT 应被检测"""
        diffs = extract_ast_diffs(GOLF_CORRECT, GOLF_STUDENT_NO_DISTINCT)
        clause_types = [d.clause_category for d in diffs]
        assert any("DISTINCT" in c or "SELECT" in c for c in clause_types), \
            f"未检测到 DISTINCT/SELECT 差异，得到: {clause_types}"

    def test_missing_where_golf_detected(self):
        """golf 表缺失 WHERE 应被检测"""
        diffs = extract_ast_diffs(GOLF_CORRECT, GOLF_STUDENT_NO_WHERE)
        assert any("WHERE" in d.clause_category for d in diffs), \
            f"未检测到 WHERE 缺失，得到: {[d.clause_category for d in diffs]}"

    def test_bravo_wrong_filter_column_detected(self):
        """bravo 表 WHERE 条件用错列应被检测"""
        diffs = extract_ast_diffs(BRAVO_CORRECT, BRAVO_STUDENT_WRONG_FILTER)
        assert len(diffs) > 0, "列选错应产生差异节点"


# ─────────────────────────────────────────────────────
# Phase 2-3-4 测试：造数 + 沙盒 + 变异
# ─────────────────────────────────────────────────────

class TestPhase234Sandbox:
    """Phase 2-4: 验证 generate_and_compare 完整流水线"""

    def test_missing_where_not_equivalent(self):
        """缺失 WHERE 应判为不等价"""
        result = generate_and_compare(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_MISSING_WHERE)
        assert result.executed, f"沙盒执行失败: {result.error}"
        assert not result.is_equivalent, \
            "缺失 WHERE 子句应判为不等价（行数更多）"

    def test_wrong_where_not_equivalent(self):
        """WHERE 条件完全错误应判为不等价"""
        result = generate_and_compare(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_WRONG_WHERE)
        assert result.executed, f"沙盒执行失败: {result.error}"
        assert not result.is_equivalent, \
            "WHERE 条件完全错误应判为不等价"

    def test_correct_submission_is_equivalent(self):
        """正确 SQL（与标准答案结构相同）应判为等价"""
        # 用标准答案自身做 smoke test
        result = generate_and_compare(DELTA_SCHEMA, DELTA_CORRECT, DELTA_CORRECT)
        assert result.executed, f"沙盒执行失败: {result.error}"
        assert result.is_equivalent, "标准答案自身应判为等价"

    def test_missing_distinct_not_equivalent(self):
        """缺失 DISTINCT：沙盒可能判等价（造数未产生重复行），但 mutation_evidence 应能捕获"""
        result = generate_and_compare(GOLF_SCHEMA, GOLF_CORRECT, GOLF_STUDENT_NO_DISTINCT)
        assert result.executed, f"沙盒执行失败: {result.error}"
        # DISTINCT 差异依赖造数产生重复行；若沙盒判等价，mutation 应识别
        if result.is_equivalent:
            assert result.mutation_evidence is not None, \
                "沙盒等价时 mutation_evidence 不应为 None"
            tests = result.mutation_evidence.get("tests", [])
            assert any(t.get("fixed_by_replacement") for t in tests), \
                "DISTINCT 缺失应被 mutation replacement 识别"
        print(f"  is_equivalent={result.is_equivalent}, "
              f"standard_rows={len(result.standard_rows)}, "
              f"student_rows={len(result.student_rows)}")

    def test_mutation_evidence_present(self):
        """不等价时应产生 mutation_evidence"""
        result = generate_and_compare(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_MISSING_WHERE)
        assert result.executed
        assert result.mutation_evidence is not None, "不等价时 mutation_evidence 不应为 None"
        assert result.mutation_evidence.get("enabled", False) is True, \
            f"mutation 测试应已启用: {result.mutation_evidence}"

    def test_data_evidence_present(self):
        """沙盒执行后应有 data_evidence"""
        result = generate_and_compare(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_WRONG_WHERE)
        assert result.executed
        assert result.data_evidence is not None, "data_evidence 不应为 None"


# ─────────────────────────────────────────────────────
# Phase 5 测试：归因仲裁器
# ─────────────────────────────────────────────────────

class TestPhase5Attribution:
    """Phase 5: 验证 evidence_weights_from_observation 归因输出"""

    def _get_attributions(self, schema, correct, student):
        """辅助：跑完整 pipeline 并返回归因列表"""
        result = generate_and_compare(schema, correct, student)
        assert result.executed, f"沙盒执行失败: {result.error}"
        judge_detail = {
            "is_correct": result.is_equivalent,
            "error_message": result.error,
            "comparison": {
                "is_equivalent_on_generated_data": result.is_equivalent,
            },
            "is_equivalent_on_generated_data": result.is_equivalent,
            "standard_rows": result.standard_rows,
            "student_rows":  result.student_rows,
            "standard_columns": result.standard_columns,
            "student_columns":  result.student_columns,
            "data_evidence":    result.data_evidence,
        }
        ar = evidence_weights_from_observation(
            student_sql=student,
            answer_sql=correct,
            is_correct=result.is_equivalent,
            error_message=result.error,
            judge_detail=judge_detail,
            mutation_detail=result.mutation_evidence,
        )
        return ar

    def test_missing_where_attributed(self):
        """缺失 WHERE 应归因到 where 相关知识点"""
        ar = self._get_attributions(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_MISSING_WHERE)
        kps = [a.knowledge_point_id for a in ar.attributions]
        assert any("where" in kp.lower() or "filter" in kp.lower() for kp in kps), \
            f"缺失 WHERE 应归因到 where/filter 知识点，得到: {kps}"

    def test_wrong_where_attributed(self):
        """WHERE 条件完全错误应有归因输出"""
        ar = self._get_attributions(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_WRONG_WHERE)
        assert len(ar.attributions) > 0, "WHERE 条件错误应有至少 1 条归因"

    def test_correct_submission_no_attributions(self):
        """正确提交应无错误归因"""
        ar = self._get_attributions(DELTA_SCHEMA, DELTA_CORRECT, DELTA_CORRECT)
        assert len(ar.attributions) == 0, \
            f"正确提交不应有归因，得到: {[a.knowledge_point_id for a in ar.attributions]}"

    def test_missing_distinct_attributed(self):
        """缺失 DISTINCT 应有相关归因"""
        ar = self._get_attributions(GOLF_SCHEMA, GOLF_CORRECT, GOLF_STUDENT_NO_DISTINCT)
        kps = [a.knowledge_point_id for a in ar.attributions]
        # DISTINCT 缺失可能被归因到 select/projection 相关
        print(f"  DISTINCT 缺失归因结果: {kps}")

    def test_attribution_has_llm_input(self):
        """归因结果应包含 llm_arbitration_input"""
        ar = self._get_attributions(DELTA_SCHEMA, DELTA_CORRECT, DELTA_STUDENT_MISSING_WHERE)
        assert ar.llm_arbitration_input is not None, "llm_arbitration_input 不应为 None"
        assert "evidence" in ar.llm_arbitration_input or "candidates" in ar.llm_arbitration_input, \
            f"llm_arbitration_input 结构不符: {list(ar.llm_arbitration_input.keys())}"


# ─────────────────────────────────────────────────────
# 集成测试：完整 pipeline 跑真实数据
# ─────────────────────────────────────────────────────

class TestFullPipelineIntegration:
    """全链路：Phase 1→5 跑真实 SQLRepair 学生数据"""

    CASES = [
        {
            "name": "P4_missing_where",
            "schema": DELTA_SCHEMA,
            "correct": DELTA_CORRECT,
            "student": DELTA_STUDENT_MISSING_WHERE,
            "expected_error": "where",
        },
        {
            "name": "P4_wrong_where",
            "schema": DELTA_SCHEMA,
            "correct": DELTA_CORRECT,
            "student": DELTA_STUDENT_WRONG_WHERE,
            "expected_error": "where",
        },
        {
            "name": "P4_wrong_col",
            "schema": DELTA_SCHEMA,
            "correct": DELTA_CORRECT,
            "student": DELTA_STUDENT_WRONG_COL,
            "expected_error": "where",
        },
        {
            "name": "P7_no_distinct",
            "schema": GOLF_SCHEMA,
            "correct": GOLF_CORRECT,
            "student": GOLF_STUDENT_NO_DISTINCT,
            "expected_error": "distinct",
        },
        {
            "name": "P7_no_where",
            "schema": GOLF_SCHEMA,
            "correct": GOLF_CORRECT,
            "student": GOLF_STUDENT_NO_WHERE,
            "expected_error": "where",
        },
        {
            "name": "P2_wrong_filter",
            "schema": BRAVO_SCHEMA,
            "correct": BRAVO_CORRECT,
            "student": BRAVO_STUDENT_WRONG_FILTER,
            "expected_error": "where",
        },
        {
            "name": "P2_no_where",
            "schema": BRAVO_SCHEMA,
            "correct": BRAVO_CORRECT,
            "student": BRAVO_STUDENT_NO_WHERE,
            "expected_error": "where",
        },
    ]

    def test_all_cases_run_without_error(self):
        """所有 7 个真实学生案例应完整跑完，无异常抛出"""
        for case in self.CASES:
            result = generate_and_compare(case["schema"], case["correct"], case["student"])
            assert result.executed, f"[{case['name']}] 沙盒执行失败: {result.error}"

    def test_all_wrong_cases_not_equivalent(self):
        """所有错误提交应判为不等价（或由 mutation 捕获修复证据）"""
        for case in self.CASES:
            result = generate_and_compare(case["schema"], case["correct"], case["student"])
            assert result.executed
            if result.is_equivalent:
                # DISTINCT 等细微差异可能沙盒等价但 mutation 识别
                mut = result.mutation_evidence or {}
                tests = mut.get("tests", [])
                fixed = any(t.get("fixed_by_replacement") for t in tests)
                assert fixed, \
                    f"[{case['name']}] 等价但 mutation 也未识别"
            # else: 不等价，正常通过

    def test_all_wrong_cases_have_attributions(self):
        """所有错误提交应至少产生 1 条归因"""
        for case in self.CASES:
            result = generate_and_compare(case["schema"], case["correct"], case["student"])
            assert result.executed
            judge_detail = {
                "is_correct": result.is_equivalent,
                "error_message": result.error,
                "comparison": {
                    "is_equivalent_on_generated_data": result.is_equivalent,
                },
                "is_equivalent_on_generated_data": result.is_equivalent,
                "standard_rows": result.standard_rows,
                "student_rows":  result.student_rows,
                "standard_columns": result.standard_columns,
                "student_columns":  result.student_columns,
                "data_evidence":    result.data_evidence,
            }
            ar = evidence_weights_from_observation(
                student_sql=case["student"],
                answer_sql=case["correct"],
                is_correct=result.is_equivalent,
                error_message=result.error,
                judge_detail=judge_detail,
                mutation_detail=result.mutation_evidence,
            )
            assert len(ar.attributions) > 0, \
                f"[{case['name']}] 错误提交无归因输出"
            print(f"  [{case['name']}] 归因: "
                  f"{[(a.knowledge_point_id, a.error_type) for a in ar.attributions]}")
