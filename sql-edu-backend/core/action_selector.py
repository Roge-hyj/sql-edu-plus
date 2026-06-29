"""
提示动作选择（ActionSelector）

这是闭环中的“行动搜索”模块：把结构缺陷（ASTError）+ 掌握度（BKT）+ 严厉度（lambda_t）
组合成少量、明确、可执行的提示动作（HintAction），用于注入到 LLM 的 system prompt。
"""

from typing import List, Dict

from core.ast_analyzer import ASTError
from core.hint_actions import HintAction, build_hint_action

class ActionSelector:
    """
    提示动作选择（f_Search）

    输入：
    - error_vector: ASTError[]，表示本次“结构性缺陷”的候选集合
    - mastery_state: Dict[kp_id, p_mastery]，表示学生当前各知识点掌握度
    - lambda_t: 控制策略输出，代表“严厉度/扶持度”

    输出：
    - HintAction[]：一组可注入到 LLM system prompt 的“原子提示动作”

    设计意图：
    - 不直接让 LLM 自己“猜学生错在哪”，而是先用规则+状态评估选出 1～2 个最关键的方向，
      以减少认知负荷、稳定提示质量。
    """

    @staticmethod
    def select_hint_actions(
        error_vector: List[ASTError],
        mastery_state: Dict[str, float],
        lambda_t: float,
        max_hints: int = 2,  # 限制同时产生的逻辑提示数量，避免认知过载
    ) -> List[HintAction]:
        """
        基于以下维度动态构建最优的 LLM 注入指令数组：
        1. E_t (AST 结构错误严重度)
        2. L_t (当前 BKT 知识掌握概率)
        3. lambda_t (计算出的疲劳/成长比例系数)

        启发式优先级算法：严重度 / max(掌握度, 0.01)
        - 优先级指向：学生掌握度极低且缺失关键子句的逻辑断层。
        """
        if not error_vector:
            return []

        scored_errors = []
        for error in error_vector:
            kp_id = error.knowledge_point_id
            mastery = mastery_state.get(kp_id, 0.1)  # 若无记录，默认为初学者状态

            # 优先级计算：A* 双向自适应估价函数 f(KP)
            # - 当学生状态佳且专注 (λ_t -> 1) -> 越严重、越不掌握越优先 (挑战模式)
            # - 当学生疲劳且挫败 (λ_t -> 0) -> 越简单 (1-Severity)、越熟练 (Mastery) 越优先 (脚手架保护模式)
            challenge_term = error.severity + (1.0 - mastery)
            scaffolding_term = (1.0 - error.severity) + mastery
            priority = lambda_t * challenge_term + (1.0 - lambda_t) * scaffolding_term

            scored_errors.append((error, priority, mastery))

        # 按 A* 启发式优先级降序排列
        scored_errors.sort(key=lambda x: x[1], reverse=True)

        # 截断至最大允许提示数，防止一次给太多提示导致学生混乱
        selected_targets = scored_errors[:max_hints]
        actions = []

        for error, _, mastery in selected_targets:
            # 根据 lambda_t（全局疲劳度/严厉度）和 mastery（特定技能掌握度）决定最优提示深度
            if lambda_t > 0.7:
                # 学生状态极佳且专注 -> 给予高难度挑战 (仅逻辑引导)
                depth = 1
            elif lambda_t > 0.5:
                # 学生处于中等状态 -> 取决于是否熟悉该特定技能
                depth = 2 if mastery < 0.4 else 1
            elif lambda_t > 0.3:
                # 学生开始感到沮丧 -> 提供更多支架辅助
                depth = 3 if mastery < 0.3 else 2
            else:
                # 学生接近失败或极度疲劳 -> 最大程度的细节引导
                depth = 3

            # 创建原子级的 Prompt 注入指令
            action = build_hint_action(
                kp_id=error.knowledge_point_id,
                clause=error.clause,
                depth=depth,
                detail=error.detail,
            )
            actions.append(action)

        return actions
