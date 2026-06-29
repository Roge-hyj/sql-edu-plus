"""
提示动作（HintAction）

HintAction 是“给 LLM 的原子指令”，它不会包含本题答案，
而是描述“应该如何引导学生思考/修正哪个结构点”。

本模块把 hint 的“深度”离散化为 1/2/3：
- 1：更抽象（更挑战）
- 2：明确结构，但不提供示例代码
- 3：允许给通用示例（必须与本题字段无关），用于学生非常挫败时的扶持
"""

from dataclasses import dataclass
from typing import List

@dataclass
class HintAction:
    """A candidate hint strategy for the LLM to execute based on AST logic."""
    target_knowledge_point: str   # Which KP this hint addresses (e.g. "group-by")
    hint_depth: int               # 1=vague, 2=structural, 3=explicit
    clause_name: str              # Friendly name of the clause missing (e.g. "GROUP BY")
    prompt_fragment: str          # Injected into the LLM system prompt

def build_hint_action(kp_id: str, clause: str, depth: int, detail: str) -> HintAction:
    """Generate the specific prompt payload based on the depth requested."""

    # Depth 1: Vague direction (High lambda)
    if depth == 1:
        fragment = (
            f"指出逻辑上的缺陷，但不提及具体的 '{clause}' 关键字。 "
            f"让学生思考这部分数据的组织方式。参考缺陷：{detail}"
        )
    # Depth 2: Structural direction (Medium lambda)
    elif depth == 2:
        fragment = (
            f"明确告知学生在 SQL 结构上缺失或写错了 '{clause}' 子句，但不提供代码示例。 "
            f"用通俗的语言解释为什么这里必须用这个子句。参考缺陷：{detail}"
        )
    # Depth 3: Explicit example (Low lambda)
    else:
        fragment = (
            f"明确指出此处需要使用 '{clause}'。提供一个该语法的通用示例（必须与本题字段无关的例子）。 "
            f"详细解释这个语法的执行顺序或原理。参考缺陷：{detail}"
        )

    return HintAction(
        target_knowledge_point=kp_id,
        hint_depth=depth,
        clause_name=clause,
        prompt_fragment=fragment
    )
