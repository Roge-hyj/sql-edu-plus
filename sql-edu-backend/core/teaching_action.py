"""Phase 4: deterministic teaching-action selection.

Phase 4 consumes only the learner-safe Phase 2 package and the trusted
Phase 3 plan.  It does not inspect reference SQL, rerun diagnosis, update BKT,
or generate prose with an LLM.  Its job is to choose one causal teaching
target and decide which already-safe narrative fragments may be delivered at
the recommended support depth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.support_policy import SUPPORT_POLICY_VERSION


TEACHING_ACTION_SCHEMA_VERSION = "phase4.teaching_action.v1"
TEACHING_ACTION_POLICY_VERSION = "phase4.action_selector.v1"
TEACHING_SUPPORT_SCHEMA_VERSION = "phase4.teaching_support.v1"
MAX_ACTIONS = 4
MAX_ACTION_TEXT_CHARS = 2000


class TeachingActionError(ValueError):
    """Raised when an invalid upstream contract reaches Phase 4."""


class TeachingActionKind(str, Enum):
    ACCEPTANCE = "ACCEPTANCE"
    STUDENT_BEHAVIOR = "STUDENT_BEHAVIOR"
    CONFLICT_WITNESS = "CONFLICT_WITNESS"
    REPAIR_REFLECTION = "REPAIR_REFLECTION"
    SOCRATIC_QUESTION = "SOCRATIC_QUESTION"
    SYSTEM_NOTICE = "SYSTEM_NOTICE"


@dataclass(frozen=True, slots=True)
class TeachingAction:
    action_id: str
    kind: TeachingActionKind
    text: str

    def __post_init__(self) -> None:
        if not self.action_id or len(self.action_id) > 32:
            raise TeachingActionError("action_id must be a bounded string")
        text = str(self.text or "").strip()
        if not text or len(text) > MAX_ACTION_TEXT_CHARS:
            raise TeachingActionError("teaching action text is invalid")
        object.__setattr__(self, "text", text)

    def to_dict(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class TeachingActionPlan:
    status: str
    verdict: str
    language: str
    support_need: float | None
    support_policy_version: str | None
    recommended_support_level: int | None
    delivered_support_level: int
    support_recommendation_applied: bool
    adaptive_target_selected: bool
    target_candidate_id: str | None
    target_rule_id: str | None
    target_observation_id: str | None
    target_skill_id: str | None
    target_taxonomy_version: str | None
    target_logical_stage: str | None
    target_source_role: str | None
    target_evidence_grade: str | None
    actions: tuple[TeachingAction, ...]

    def __post_init__(self) -> None:
        if self.verdict not in {"CORRECT", "INCORRECT"}:
            raise TeachingActionError("unsupported teaching-action verdict")
        if self.language not in {"zh-CN", "zh-TW", "en"}:
            raise TeachingActionError("unsupported teaching-action language")
        if not 1 <= self.delivered_support_level <= 4:
            raise TeachingActionError("delivered support level must be in [1, 4]")
        if self.recommended_support_level is not None and not (
            1 <= self.recommended_support_level <= 4
        ):
            raise TeachingActionError("recommended support level must be in [1, 4]")
        if self.support_need is not None and not 0.0 <= self.support_need <= 1.0:
            raise TeachingActionError("support_need must be in [0, 1]")
        if (self.recommended_support_level is None) is not (
            self.support_need is None
        ):
            raise TeachingActionError(
                "recommended support level and support_need must be paired"
            )
        if (
            self.recommended_support_level is not None
            and self.support_policy_version is None
        ):
            raise TeachingActionError(
                "a recommendation requires support_policy_version"
            )
        for field_name in (
            "status",
            "support_policy_version",
            "target_candidate_id",
            "target_rule_id",
            "target_observation_id",
            "target_skill_id",
            "target_taxonomy_version",
            "target_logical_stage",
            "target_source_role",
            "target_evidence_grade",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 160
            ):
                raise TeachingActionError(f"{field_name} must be a bounded string")
        if not self.actions or len(self.actions) > MAX_ACTIONS:
            raise TeachingActionError("teaching action count is invalid")
        if self.support_recommendation_applied and (
            self.recommended_support_level != self.delivered_support_level
            or not self.adaptive_target_selected
        ):
            raise TeachingActionError("applied support recommendation is inconsistent")
        if self.adaptive_target_selected and any(
            getattr(self, field_name) is None
            for field_name in (
                "target_candidate_id",
                "target_rule_id",
                "target_observation_id",
                "target_skill_id",
                "target_taxonomy_version",
                "target_logical_stage",
                "target_source_role",
                "target_evidence_grade",
            )
        ):
            raise TeachingActionError("adaptive teaching target provenance is incomplete")
        if self.verdict == "CORRECT" and (
            self.adaptive_target_selected
            or self.recommended_support_level is not None
        ):
            raise TeachingActionError("correct feedback cannot claim an error target")

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TEACHING_ACTION_SCHEMA_VERSION,
            "policy_version": TEACHING_ACTION_POLICY_VERSION,
            "status": self.status,
            "verdict": self.verdict,
            "language": self.language,
            "support_need": self.support_need,
            "support_policy_version": self.support_policy_version,
            "recommended_support_level": self.recommended_support_level,
            "delivered_support_level": self.delivered_support_level,
            "support_recommendation_applied": self.support_recommendation_applied,
            "adaptive_target_selected": self.adaptive_target_selected,
            "target_candidate_id": self.target_candidate_id,
            "target_rule_id": self.target_rule_id,
            "target_observation_id": self.target_observation_id,
            "target_skill_id": self.target_skill_id,
            "target_taxonomy_version": self.target_taxonomy_version,
            "target_logical_stage": self.target_logical_stage,
            "target_source_role": self.target_source_role,
            "target_evidence_grade": self.target_evidence_grade,
            "actions": [item.to_dict() for item in self.actions],
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return policy metadata without exposing causal IDs or action text."""

        return {
            "schema_version": TEACHING_ACTION_SCHEMA_VERSION,
            "policy_version": TEACHING_ACTION_POLICY_VERSION,
            "status": self.status,
            "recommended_support_level": self.recommended_support_level,
            "delivered_support_level": self.delivered_support_level,
            "support_recommendation_applied": self.support_recommendation_applied,
            "adaptive_target_selected": self.adaptive_target_selected,
            "action_count": len(self.actions),
        }


def _localized(language: str, zh_cn: str, zh_tw: str, en: str) -> str:
    if language == "en":
        return en
    if language == "zh-TW":
        return zh_tw
    return zh_cn


def _fallback_guidance(language: str) -> str:
    return _localized(
        language,
        "请从数据来源开始，依次检查行过滤、分组、组过滤、投影和最终排序，找出最早改变结果含义的步骤。",
        "請從資料來源開始，依次檢查列過濾、分組、組過濾、投影和最終排序，找出最早改變結果含義的步驟。",
        "Trace the query from data sources through filtering, grouping, projection, and final ordering. Which earliest step changes the intended result?",
    )


_REPAIR_REFLECTIONS: dict[str, tuple[str, str, str]] = {
    "S1_MISSING_BRIDGE": (
        "先画出目标实体之间的关联路径，检查每一跳是否都有真实业务关系支撑。",
        "先畫出目標實體之間的關聯路徑，檢查每一跳是否都有真實業務關係支撐。",
        "Sketch the relationship path between the target entities and verify that every hop has a real business relationship.",
    ),
    "S1_CARTESIAN_PRODUCT": (
        "逐个检查数据来源之间是否都有约束关系，并估算连接前后的行数变化。",
        "逐個檢查資料來源之間是否都有約束關係，並估算連接前後的列數變化。",
        "Check that every data source is constrained by a relationship and estimate the row count before and after each join.",
    ),
    "S1_OUTER_JOIN_MISUSE": (
        "先明确题目要求保留的主体，再检查当前连接是否会删除没有匹配记录的主体。",
        "先明確題目要求保留的主體，再檢查目前連接是否會刪除沒有匹配記錄的主體。",
        "Identify which entity must be preserved, then check whether unmatched instances are removed by the current join.",
    ),
    "S1_SUBQUERY_CARDINALITY": (
        "先判断子查询在业务上可能返回一个值还是一组值，再选择相匹配的比较语义。",
        "先判斷子查詢在業務上可能返回一個值還是一組值，再選擇相匹配的比較語義。",
        "Decide whether the subquery can return one value or a set, then use comparison semantics that match that cardinality.",
    ),
    "S2_BOUNDARY": (
        "把题目中的严格、至少、至多等词先写成开闭区间，再核对当前比较关系是否包含临界点。",
        "把題目中的嚴格、至少、至多等詞先寫成開閉區間，再核對目前比較關係是否包含臨界點。",
        "Translate words such as strictly, at least, or at most into open or closed boundaries before checking the comparison.",
    ),
    "S2_BOOLEAN_LOGIC": (
        "把复合条件拆成逻辑组并列出真值组合，再用括号明确希望的结合顺序。",
        "把複合條件拆成邏輯組並列出真值組合，再用括號明確希望的結合順序。",
        "Split the predicate into logical groups, inspect representative truth combinations, and make the intended grouping explicit.",
    ),
    "S2_NULL_LOGIC": (
        "分别推演真、假和未知三种结果，确认空值记录是否应被保留。",
        "分別推演真、假和未知三種結果，確認空值記錄是否應被保留。",
        "Reason through true, false, and unknown separately, then decide whether null-bearing rows should remain.",
    ),
    "S2_AGGREGATE_IN_WHERE": (
        "把单行条件与聚合后的统计条件分开，确认每个条件依赖的数据在何时才产生。",
        "把單列條件與聚合後的統計條件分開，確認每個條件依賴的資料在何時才產生。",
        "Separate row-level conditions from aggregate-result conditions and place each after the data it depends on exists.",
    ),
    "S3_GRAIN_ENTITY_MISMATCH": (
        "先用一句话定义结果中一行代表什么实体，再据此确定分组维度。",
        "先用一句話定義結果中一列代表什麼實體，再據此確定分組維度。",
        "State what one output row represents, then choose grouping dimensions that uniquely express that entity grain.",
    ),
    "S3_GROUP_KEY_MISSING": (
        "列出决定一行结果的全部非聚合维度，检查分组声明是否完整覆盖。",
        "列出決定一列結果的全部非聚合維度，檢查分組宣告是否完整覆蓋。",
        "List every non-aggregate dimension that determines one output row and verify that the grouping covers all of them.",
    ),
    "S3_GROUP_KEY_REDUNDANT": (
        "检查每个额外分组维度是否会把同一目标实体拆成多行，保留定义目标粒度所必需的维度。",
        "檢查每個額外分組維度是否會把同一目標實體拆成多列，保留定義目標粒度所必需的維度。",
        "Check whether each extra grouping dimension splits one target entity into multiple rows; retain only dimensions needed for the target grain.",
    ),
    "S4_HAVING_MISSING": (
        "确认题目对统计结果提出的约束，并在分组结果产生后执行该约束。",
        "確認題目對統計結果提出的約束，並在分組結果產生後執行該約束。",
        "Identify the condition on the aggregate result and apply it after groups and their metrics have been formed.",
    ),
    "S4_AGG_BOUNDARY": (
        "把统计条件中的至少、超过等词转换成明确边界，并使用临界组验证。",
        "把統計條件中的至少、超過等詞轉換成明確邊界，並使用臨界組驗證。",
        "Translate aggregate phrases such as at least or more than into explicit boundaries and test a boundary group.",
    ),
    "S4_ROW_FILTER_IN_HAVING": (
        "判断条件描述的是单条明细还是整个分组，并把它放到对应的数据阶段。",
        "判斷條件描述的是單條明細還是整個分組，並把它放到對應的資料階段。",
        "Decide whether the condition describes an individual row or an entire group, then place it at that data stage.",
    ),
    "S5_FANOUT_AGGREGATE": (
        "沿连接链追踪一个目标实体会复制成多少行，再决定聚合前需要怎样控制重复贡献。",
        "沿連接鏈追蹤一個目標實體會複製成多少列，再決定聚合前需要怎樣控制重複貢獻。",
        "Trace how many rows one target entity becomes across the joins, then prevent repeated contributions before aggregation.",
    ),
    "S5_COUNT_NULL_SENSITIVITY": (
        "先明确要统计的是物理行数还是非空有效值，再检查计数对象是否符合该定义。",
        "先明確要統計的是實體列數還是非空有效值，再檢查計數對象是否符合該定義。",
        "Decide whether the metric counts rows or non-null values, then verify that the counted target matches that definition.",
    ),
    "S5_CASE_INCOMPLETE": (
        "枚举互斥且穷尽的业务分支，并检查未命中任何显式分支时应产生什么结果。",
        "列舉互斥且窮盡的業務分支，並檢查未命中任何顯式分支時應產生什麼結果。",
        "Enumerate mutually exclusive and exhaustive business branches, including what should happen when no explicit branch matches.",
    ),
    "S5_TOP_LEVEL_DEDUP": (
        "检查连接后同一输出实体是否可能出现多次，并明确结果集要求的是实体集合还是明细行。",
        "檢查連接後同一輸出實體是否可能出現多次，並明確結果集要求的是實體集合還是明細列。",
        "Check whether one output entity can appear multiple times after joins and whether the task asks for entities or detail rows.",
    ),
    "S6_TOPN_WITHOUT_ORDER": (
        "先建立与题意一致且可重复的完整排序，再执行结果截断。",
        "先建立與題意一致且可重複的完整排序，再執行結果截斷。",
        "Establish a deterministic ordering that matches the task before truncating the result.",
    ),
    "S6_ORDER_OFFSET": (
        "把目标名次、排序方向和从零开始的偏移分别写出，再逐项核对。",
        "把目標名次、排序方向和從零開始的偏移分別寫出，再逐項核對。",
        "Write down the target rank, sort direction, and zero-based offset separately, then verify each one.",
    ),
}


def _candidate_by_id(package: Mapping[str, Any], candidate_id: str) -> Mapping[str, Any] | None:
    candidates: list[Any] = [package.get("primary")]
    secondary = package.get("secondary")
    if isinstance(secondary, (list, tuple)):
        candidates.extend(secondary)
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def _narrative_text(package: Mapping[str, Any], key: str) -> str:
    narrative = package.get("narrative")
    if not isinstance(narrative, Mapping):
        return ""
    value = narrative.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_ACTION_TEXT_CHARS]


def _action(kind: TeachingActionKind, text: str, index: int) -> TeachingAction:
    return TeachingAction(action_id=f"action_{index}", kind=kind, text=text)


def build_fixed_teaching_action(
    text: str,
    *,
    language: str,
    status: str,
    recommended_support_level: int | None = None,
    support_need: float | None = None,
    support_policy_version: str | None = None,
) -> TeachingActionPlan:
    """Build a level-1 audited action for syntax/safety/degraded feedback."""

    return TeachingActionPlan(
        status=status,
        verdict="INCORRECT",
        language=language,
        support_need=support_need,
        support_policy_version=support_policy_version,
        recommended_support_level=recommended_support_level,
        delivered_support_level=1,
        support_recommendation_applied=False,
        adaptive_target_selected=False,
        target_candidate_id=None,
        target_rule_id=None,
        target_observation_id=None,
        target_skill_id=None,
        target_taxonomy_version=None,
        target_logical_stage=None,
        target_source_role=None,
        target_evidence_grade=None,
        actions=(_action(TeachingActionKind.SYSTEM_NOTICE, text, 1),),
    )


def degrade_teaching_action(
    plan: TeachingActionPlan,
    text: str,
    *,
    status: str,
) -> TeachingActionPlan:
    """Fail closed to L1 while retaining recommendation/target audit lineage."""

    if not isinstance(plan, TeachingActionPlan):
        raise TeachingActionError("plan must be a TeachingActionPlan")
    return TeachingActionPlan(
        status=status,
        verdict=plan.verdict,
        language=plan.language,
        support_need=plan.support_need,
        support_policy_version=plan.support_policy_version,
        recommended_support_level=plan.recommended_support_level,
        delivered_support_level=1,
        support_recommendation_applied=False,
        adaptive_target_selected=False,
        target_candidate_id=plan.target_candidate_id,
        target_rule_id=plan.target_rule_id,
        target_observation_id=plan.target_observation_id,
        target_skill_id=plan.target_skill_id,
        target_taxonomy_version=plan.target_taxonomy_version,
        target_logical_stage=plan.target_logical_stage,
        target_source_role=plan.target_source_role,
        target_evidence_grade=plan.target_evidence_grade,
        actions=(_action(TeachingActionKind.SYSTEM_NOTICE, text, 1),),
    )


def select_teaching_actions(
    diagnostic_package: Mapping[str, Any],
    phase3_plan: Any,
    *,
    expected_is_correct: bool,
    language: str = "zh-CN",
) -> TeachingActionPlan:
    """Select one bounded teaching target and an applied support depth."""

    if not isinstance(diagnostic_package, Mapping):
        raise TeachingActionError("diagnostic_package must be a mapping")
    expected_verdict = "CORRECT" if expected_is_correct else "INCORRECT"
    if diagnostic_package.get("verdict") != expected_verdict:
        raise TeachingActionError("Phase 2 verdict conflicts with Phase 4 input")

    if expected_is_correct:
        text = _localized(
            language,
            "当前有界沙盒检查未发现反例，本次作答已获得教学性接受。你可以再想一想：这条查询的结果粒度和边界条件是否都能用一句话解释清楚？",
            "目前有界沙盒檢查未發現反例，本次作答已獲得教學性接受。你可以再想一想：這條查詢的結果粒度和邊界條件是否都能用一句話解釋清楚？",
            "No counterexample was found by the current bounded checks, so this submission is operationally accepted. Can you explain its result grain and boundary conditions in one sentence?",
        )
        return TeachingActionPlan(
            status="CORRECT_ACCEPTED",
            verdict="CORRECT",
            language=language,
            support_need=None,
            support_policy_version=None,
            recommended_support_level=None,
            delivered_support_level=1,
            support_recommendation_applied=False,
            adaptive_target_selected=False,
            target_candidate_id=None,
            target_rule_id=None,
            target_observation_id=None,
            target_skill_id=None,
            target_taxonomy_version=None,
            target_logical_stage=None,
            target_source_role=None,
            target_evidence_grade=None,
            actions=(_action(TeachingActionKind.ACCEPTANCE, text, 1),),
        )

    selected = getattr(phase3_plan, "selected", None) if phase3_plan is not None else None
    selected_target = (
        getattr(phase3_plan, "selected_target", None)
        if phase3_plan is not None
        else None
    )
    support = getattr(phase3_plan, "support", None) if phase3_plan is not None else None
    candidate_id = getattr(selected_target, "phase2_candidate_id", None)
    candidate = (
        _candidate_by_id(diagnostic_package, candidate_id)
        if isinstance(candidate_id, str)
        else None
    )
    target_rule_id = getattr(selected_target, "phase2_rule_id", None)
    target_logical_stage = getattr(selected_target, "logical_stage", None)
    adaptive = (
        candidate is not None
        and support is not None
        and selected is not None
        and isinstance(target_rule_id, str)
        and candidate.get("rule_id") == target_rule_id
        and isinstance(target_logical_stage, str)
        and candidate.get("logical_stage") == target_logical_stage
    )
    recommended = getattr(support, "support_level", None) if adaptive else None
    if isinstance(recommended, bool) or not isinstance(recommended, int) or not 1 <= recommended <= 4:
        adaptive = False
        recommended = None
    delivered = recommended if adaptive and recommended is not None else 1

    primary = diagnostic_package.get("primary")
    selected_is_primary = bool(
        adaptive
        and isinstance(primary, Mapping)
        and primary.get("candidate_id") == candidate_id
    )
    behavior = (
        _narrative_text(diagnostic_package, "student_behavior")
        if selected_is_primary or not adaptive
        else _localized(
            language,
            "可信执行证据表明，当前查询在选中的逻辑阶段产生了会改变结果的行为差异。",
            "可信執行證據表明，目前查詢在選中的邏輯階段產生了會改變結果的行為差異。",
            "Trusted execution evidence shows that the selected logical stage changes the result behavior.",
        )
    )
    conflict = (
        _narrative_text(diagnostic_package, "conflict_and_witness")
        if selected_is_primary
        else _localized(
            language,
            "该差异具有独立的强因果或修复证据，因此先处理它，再重新运行查询观察下游变化。",
            "該差異具有獨立的強因果或修復證據，因此先處理它，再重新執行查詢觀察下游變化。",
            "This difference has independent causal or repair evidence, so address it first and then rerun the query before considering downstream symptoms.",
        )
        if adaptive
        else ""
    )
    guidance = (
        _narrative_text(diagnostic_package, "guidance_question")
        if selected_is_primary or not adaptive
        else ""
    ) or _fallback_guidance(language)
    rule_id = target_rule_id if adaptive else None
    logical_stage = target_logical_stage if adaptive else None

    action_specs: list[tuple[TeachingActionKind, str]] = []
    if not adaptive:
        action_specs.append((TeachingActionKind.SOCRATIC_QUESTION, guidance))
    else:
        if delivered >= 2 and behavior:
            action_specs.append((TeachingActionKind.STUDENT_BEHAVIOR, behavior))
        if delivered >= 3 and conflict:
            action_specs.append((TeachingActionKind.CONFLICT_WITNESS, conflict))
        if delivered >= 4 and rule_id in _REPAIR_REFLECTIONS:
            zh_cn, zh_tw, en = _REPAIR_REFLECTIONS[rule_id]
            action_specs.append(
                (TeachingActionKind.REPAIR_REFLECTION, _localized(language, zh_cn, zh_tw, en))
            )
        action_specs.append((TeachingActionKind.SOCRATIC_QUESTION, guidance))

    actions = tuple(
        _action(kind, text, index)
        for index, (kind, text) in enumerate(action_specs[:MAX_ACTIONS], start=1)
    )
    return TeachingActionPlan(
        status="ADAPTIVE_READY" if adaptive else "DIAGNOSTIC_FALLBACK",
        verdict="INCORRECT",
        language=language,
        support_need=(float(getattr(support, "support_need")) if adaptive else None),
        support_policy_version=SUPPORT_POLICY_VERSION if adaptive else None,
        recommended_support_level=recommended,
        delivered_support_level=delivered,
        support_recommendation_applied=adaptive,
        adaptive_target_selected=adaptive,
        target_candidate_id=candidate_id if adaptive else None,
        target_rule_id=rule_id,
        target_observation_id=(
            getattr(selected_target, "observation_id", None) if adaptive else None
        ),
        target_skill_id=(
            getattr(selected_target, "skill_id", None) if adaptive else None
        ),
        target_taxonomy_version=(
            getattr(selected_target, "taxonomy_version", None) if adaptive else None
        ),
        target_logical_stage=logical_stage,
        target_source_role=(
            getattr(selected_target, "source_role", None) if adaptive else None
        ),
        target_evidence_grade=(
            getattr(selected_target, "evidence_grade", None) if adaptive else None
        ),
        actions=actions,
    )


__all__ = [
    "MAX_ACTIONS",
    "TEACHING_ACTION_POLICY_VERSION",
    "TEACHING_ACTION_SCHEMA_VERSION",
    "TEACHING_SUPPORT_SCHEMA_VERSION",
    "TeachingAction",
    "TeachingActionError",
    "TeachingActionKind",
    "TeachingActionPlan",
    "build_fixed_teaching_action",
    "degrade_teaching_action",
    "select_teaching_actions",
]
