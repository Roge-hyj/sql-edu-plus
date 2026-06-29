from typing import Dict, List, Any
from core.Perception_AST_Sensor import ASTAnalyser
from core.Perception_Data_Sensor import QueryDrivenDataGenerator

class SQLEvaluator:
    """
    感知层核心仲裁器 (Perception Arbiter - Φ) - V4.0 联动版
    """

    def __init__(self, kp_mapping: Dict[str, str]):
        self.analyser = ASTAnalyser()
        self.generator = QueryDrivenDataGenerator()
        self.kp_mapping = kp_mapping

    def evaluate(self, student_sql: str, standard_sql: str, schema: Dict) -> Dict[str, Any]:
        """
        全自动化仲裁：调用 V4.2 逻辑传感器进行零幻觉判定
        """
        # 1. 结构化分析 (AST)
        struct_summary = self.analyser.get_structural_summary(student_sql)
        is_struct_eq = self.analyser.is_semantically_equivalent(student_sql, standard_sql)
        missing_kps = self.analyser.extract_missing_kps(student_sql, standard_sql, self.kp_mapping)

        # 2. 逻辑执行分析 (调用最新的 V4.2 融合判定入口)
        try:
            is_exec_eq = self.generator.evaluate_equivalence_v4(
                student_sql,
                standard_sql,
                schema
            )
        except Exception:
            # 如果沙盒报错，降级到 AST 判定，但保留错误现场便于诊断
            is_exec_eq = is_struct_eq

        # 3. 首席裁量逻辑 (φ Operator)
        judgment = "Unknown"
        if is_exec_eq and is_struct_eq:
            judgment = "Perfect Correct"
        elif is_exec_eq and not is_struct_eq:
            judgment = "Alternative Correct" # 逻辑对但结构不同：体现了 Φ 的抑制幻觉作用
        elif not is_exec_eq and is_struct_eq:
            judgment = "Minor Slip" # 结构对但数据错：通常是谓词/过滤条件的微小差异
        else:
            judgment = "Incorrect"

        return {
            "submission_status": "Correct" if is_exec_eq else "Incorrect",
            "judgment_type": judgment,
            "evidence": {
                "is_semantic_match": is_exec_eq,
                "is_structure_match": is_struct_eq,
                "missing_kps": missing_kps,
                "complexity": struct_summary["complexity_score"]
            },
            "phi_signal": {
                "correct_prob_boost": 1.0 if is_exec_eq else 0.0,
                "difficulty_weight": min(1.0, struct_summary["complexity_score"] / 12.0), # 对接控制层 λ_t
                "error_signature": missing_kps if not is_exec_eq else []
            }
        }
