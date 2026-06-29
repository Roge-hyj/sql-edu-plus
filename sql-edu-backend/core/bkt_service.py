"""
Bayesian Knowledge Tracing（BKT）服务（f_BKT）

目标：
- 把“本次练习暴露的结构缺陷”（ASTError）映射成“知识点层面的对/错观测”，并更新学生对每个知识点的掌握概率 p_mastery。
- 为后续控制策略（lambda_t）提供状态向量 L_t / L_{t-1}。

使用方式（在路由层完成事务）：
- 路由调用 update_mastery_from_errors() 更新 ORM 对象，但不在这里 commit；
  由上层在一次请求结束时统一 commit，保证与 submission/chat 的写入一致。

重要约束：
- 如果请求因为危险 SQL 被安全拒绝（SQLSafetyError），不应该调用本服务，
  否则会把“违规操作”误记成“能力不足”。
"""

from typing import Dict, List, Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.knowledge_mastery import KnowledgeMastery
from core.ast_analyzer import ASTError

def _bkt_update_math(
    p_mastery: float,
    p_transit: float,
    p_guess: float,
    p_slip: float,
    observed_correct: bool,
) -> float:
    r"""
    【贝叶斯后验概率更新公式 (Bayesian Knowledge Tracing)】
    实现数学设计函数 P(L_n | Obs) 的推导，求取学生当前真实的隐藏掌握度。

    已知：
    - L_n (mastery): 先验掌握概率
    - S (slip): 会做但失误的概率
    - G (guess): 不会做但蒙对的概率
    - T (transit): 做过后学会该知识的转移概率

    若当前探测为正确 (Obs = Correct), 后验概率为：
        P(L_n | Obs) = \frac{P(L_{n-1}) \cdot (1 - P(S))}{P(L_{n-1}) \cdot (1 - P(S)) + (1 - P(L_{n-1})) \cdot P(G)}

    若当前探测为错误 (Obs = Wrong), 后验概率为：
        P(L_n | Obs) = \frac{P(L_{n-1}) \cdot P(S)}{P(L_{n-1}) \cdot P(S) + (1 - P(L_{n-1})) \cdot (1 - P(G))}
    """
    if observed_correct:
        numerator = p_mastery * (1 - p_slip)
        denominator = numerator + (1 - p_mastery) * p_guess
        # 处理除以 0 的情况
        p_posterior = numerator / denominator if denominator > 0 else p_mastery
    else:
        numerator = p_mastery * p_slip
        denominator = numerator + (1 - p_mastery) * (1 - p_guess)
        # 处理除以 0 的情况
        p_posterior = numerator / denominator if denominator > 0 else p_mastery

    # 纳入由于参与或尝试该题而产生的“学会”概率
    # P(L_n) = P(posterior) + (1 - P(posterior)) * P(Transit)
    p_new = p_posterior + (1 - p_posterior) * p_transit

    # 引入惯性平滑阻尼器（Low-pass Filter），平抑单次失误导致的掌握度数值急剧波动，提高系统控制的稳定性
    # 保留 60% 的历史掌握度惯性，融合 40% 的本轮新推导概率
    smoothing_factor = 0.6
    p_new_smoothed = smoothing_factor * p_mastery + (1 - smoothing_factor) * p_new

    # 约束边界，防止概率锁定在 0.0 或 1.0 导致数学公式失效
    return max(0.001, min(0.999, p_new_smoothed))


async def get_or_create_mastery(
    session: AsyncSession,
    user_id: int,
    kp_id: str
) -> KnowledgeMastery:
    """
    获取学生在特定知识点上的 BKT 后台统计数据。
    如果学生从未接触过该知识点，则按初始基准值创建一条新记录。
    """
    query = select(KnowledgeMastery).where(
        KnowledgeMastery.user_id == user_id,
        KnowledgeMastery.knowledge_point_id == kp_id
    )
    result = await session.execute(query)
    record = result.scalars().first()

    if not record:
        record = KnowledgeMastery(
            user_id=user_id,
            knowledge_point_id=kp_id,
            p_mastery=0.1,  # 从现实的“初学者”基准开始
            # 假设 SQL 的猜测概率不如选择题高
            p_transit=0.1,
            p_guess=0.2,
            p_slip=0.1
        )
        session.add(record)
        # 记录将由上层逻辑稍后统一持久化

    return record


async def update_mastery_from_errors(
    session: AsyncSession,
    user_id: int,
    error_vector: List[ASTError],
    question_knowledge_points: List[str],  # 题目实际考察的知识点列表
    overall_is_correct: bool,
) -> Dict[str, float]:
    """
    接收 AST 错误对象（结构缺陷），将其映射回题目的核心要求，
    计算学生在概念层面是否“回答”了该部分，并计算其当前的数学状态映射 (L_t)。

    返回：L_t（所有知识点当前 P(mastery) 的映射字典）
    """
    updated_mastery_map: Dict[str, float] = {}

    # 从 AST 差异向量中查找失败部分的 O(1) 检索
    failed_kp_ids = {err.knowledge_point_id for err in error_vector}

    # 处理该题目中涵盖的所有 SQL 知识点
    for kp_id in question_knowledge_points:
        record = await get_or_create_mastery(session, user_id, kp_id)

        # 知识点更新决策：
        # - 若整体判题正确，则所有被考查知识点都视为 correct（本题达标），更新上调
        # - 若整体不正确，且该知识点被明确归因有错，则视为 wrong，更新下调
        # - 若整体不正确，但该知识点未被明确标记有错，则保持不变（不进行 BKT 迭代，不污染历史数据）
        if overall_is_correct:
            new_mastery_prob = _bkt_update_math(
                p_mastery=record.p_mastery,
                p_transit=record.p_transit,
                p_guess=record.p_guess,
                p_slip=record.p_slip,
                observed_correct=True
            )
            record.p_mastery = new_mastery_prob
            record.total_attempts += 1
            record.correct_attempts += 1
            record.last_updated = datetime.utcnow()
        elif kp_id in failed_kp_ids:
            new_mastery_prob = _bkt_update_math(
                p_mastery=record.p_mastery,
                p_transit=record.p_transit,
                p_guess=record.p_guess,
                p_slip=record.p_slip,
                observed_correct=False
            )
            record.p_mastery = new_mastery_prob
            record.total_attempts += 1
            record.last_updated = datetime.utcnow()
        else:
            # 保持不变，不记录为该知识点的一次尝试，不更改 p_mastery
            pass

        updated_mastery_map[kp_id] = record.p_mastery

    # 提交操作将在 FastAPI 路由层更高一级完成
    return updated_mastery_map


async def get_user_mastery_state(
    session: AsyncSession,
    user_id: int
) -> Dict[str, float]:
    """
    返回学生完整的技能掌握向量图。
    用于为控制泛函 J[lambda] 提供上下文输入。
    """
    query = select(KnowledgeMastery.knowledge_point_id, KnowledgeMastery.p_mastery).where(
        KnowledgeMastery.user_id == user_id
    )
    res = await session.execute(query)
    return {row[0]: row[1] for row in res}
