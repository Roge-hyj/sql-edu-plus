"""
控制策略（ControlStrategy）

闭环中的“控制器”：根据学习状态变化与认知负荷计算 lambda_t，
并将连续 lambda_t 映射到兼容系统的离散 hint_level（1/2/3）。
"""

import math
from typing import Dict, List
from datetime import datetime

class ControlStrategy:
    """
    控制策略（J[lambda] 的离散近似）

    这个模块只负责把“学习状态变化 + 认知负荷”映射成一个连续系数 lambda_t ∈ [0, 1]：
    - lambda_t 越大：越“严厉/挑战”，提示更抽象（更少直接语法）
    - lambda_t 越小：越“温和/扶持”，提示更具体（必要时给通用示例）

    输入信号：
    - current_mastery / previous_mastery：来自 BKT 的能力向量（概率）
    - session_duration_minutes：近 1 小时内的练习时长（疲劳）
    - consecutive_failures：连续失败次数（挫折）

    重要：lambda_t 不是“对错”，而是“提示策略开关”，因此我们会 clamp 到 [0.05, 0.95]，
    避免出现“永远不给帮助”或“一上来就喂答案”的极端行为。
    """

    @staticmethod
    def compute_lambda(
        current_mastery: Dict[str, float],         # L_t
        previous_mastery: Dict[str, float],        # L_{t-1}
        session_duration_minutes: float,           # 当前会话消耗的时间
        consecutive_failures: int,                 # 连续失败次数（挫折感）
        alpha: float = 0.6,                        # 成长权值
        beta: float = 0.4,                         # 认知负荷权值
        rho: float = 0.05,                         # 会话疲劳衰减率
    ) -> float:
        """
        在 [0, 1] 范围内计算动态教学控制系数 lambda_t。

        - lambda_t 接近 1.0 -> 极高挑战，提供最少提示
        - lambda_t 接近 0.0 -> 低挑战，提供保姆级分步引导
        """

        # 1. 计算成长率 (Delta L)
        # 我们用相关知识点掌握度的平均变化来近似 dL/dt
        relevant_kps = set(current_mastery.keys()) & set(previous_mastery.keys())
        if relevant_kps:
            delta_L = sum(current_mastery[k] - previous_mastery[k] for k in relevant_kps) / len(relevant_kps)
        else:
            delta_L = 0.0  # 无重叠知识点或为首次提交

        # 2. 计算认知负荷 C(lambda, t)
        # 疲劳度：随时间指数级增加 (1 - e^{-rho * t})
        # 会话时间越长，疲劳感越接近 1.0
        fatigue = 1.0 - math.exp(-rho * session_duration_minutes)

        # 挫折感：基于连续失败次数的线性缩放，最高上限 1.0 (5 次失败视为最大挫折)
        frustration = min(1.0, consecutive_failures / 5.0)

        # 总认知负荷是基于时间的疲劳感和基于错误的挫折感的加权融合
        cognitive_load = 0.5 * fatigue + 0.5 * frustration

        # 3. 目标平衡
        # 成长信号：对 delta_L 进行归一化。假设每步的最大预期 delta 约为 0.1
        # 如果 delta_L 为 0，信号为 0.5（中性）。如果 delta_L > 0，则向 1.0 推送
        growth_signal = max(0.0, min(1.0, 0.5 + delta_L * 5.0))

        # 基础 lambda 以 0.5 为中心。
        # 高成长增加 lambda（增加挑战）。高认知负荷降低 lambda（提供更多帮助）。
        lambda_t = alpha * growth_signal - beta * cognitive_load + 0.5

        # 严格限制在 [0.05, 0.95] 之间，确保系统永远不会进入“拒不提供帮助”或“直接透漏答案”的极端状态
        lambda_t = max(0.05, min(0.95, lambda_t))

        return lambda_t

    @staticmethod
    def get_session_duration_minutes(timestamps: List[datetime]) -> float:
        """
        辅助函数：根据提交时间戳列表计算活跃会话时长。
        仅考虑过去 60 分钟内的时间戳作为“当前”会话。
        """
        if len(timestamps) < 2:
            return 0.0

        now = datetime.utcnow()
        # 过滤最近 60 分钟内的会话记录
        session_stamps = [ts for ts in timestamps if (now - ts).total_seconds() < 3600]

        if len(session_stamps) < 2:
            return 0.0

        # 确保按时间顺序排列（旧在前）
        session_stamps.sort()

        duration_seconds = (session_stamps[-1] - session_stamps[0]).total_seconds()
        return duration_seconds / 60.0

    @staticmethod
    def lambda_to_hint_level(lambda_t: float) -> int:
        """
        向后兼容映射器：将数学上的连续系数 lambda 映射为系统中已有的 1, 2, 3 离散支架等级。
        """
        if lambda_t > 0.65:
            return 1  # 高 lambda = 低支架引导 / 高挑战模式
        elif lambda_t > 0.35:
            return 2  # 中等模式
        else:
            return 3  # 低 lambda = 高支架引导 / 低挑战模式
