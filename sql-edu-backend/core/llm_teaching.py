"""Evidence-bounded LLM adapters for Phase 2 and Phase 5.

The SQL judge remains the authority for the Phase 1 verdict.  This module
gives the configured OpenAI-compatible model two deliberately narrow jobs:

* Phase 2 may arbitrate among evidence-backed diagnostic candidates and may
  rewrite the already validated narrative.  It cannot invent a rule, change
  the judge verdict, or introduce a new evidence reference.
* Phase 5 may rewrite approved Phase 4 action text.  It does not receive the
  reference SQL, Phase 1 rows, mutation worlds, or hidden target metadata.

Every provider failure, malformed response, timeout, or policy violation is a
normal fallback condition.  No caller should need an LLM in order to return a
safe learner response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import inspect
import json
import logging
import math
import re
import threading
from difflib import SequenceMatcher
from typing import Any

from openai import AsyncOpenAI

from core.teaching_action import TeachingActionKind, TeachingActionPlan
from settings.config import settings


logger = logging.getLogger(__name__)


LLM_PROVIDER_KIND = "OPENAI_COMPATIBLE"
PHASE2_LLM_POLICY_VERSION = "phase2.llm_arbitration.v1"
PHASE5_LLM_POLICY_VERSION = "phase5.llm_feedback.v1"
PHASE2_LLM_SCHEMA_VERSION = "phase2.llm_assessment.v1"
PHASE5_LLM_SCHEMA_VERSION = "phase5.llm_segments.v1"

_PHASE2_SYSTEM = """你是 SQL 教学系统的内部证据复核器。
你只能依据输入中的 Phase 1 有界执行证据和已经生成的候选诊断做判断。
Phase 1 的 authoritative_verdict 是硬约束：不能把 INCORRECT 改成 CORRECT，
不能把 CORRECT 改成 INCORRECT，也不能把 UNDECIDED 变成任何确定结论。
只能从 permitted_candidate_ids 中选择已有候选；如果证据不足，选择
UNDECIDED。不要创建规则、技能、证据 ID 或事实。不要在输出中写 SQL、谓词、
表名/列名组合、答案片段或数据库行值。只能输出一个 JSON 对象，不要 markdown。

输出格式必须严格为：
{
  "decision": "SUPPORTED_WRONG" | "OPERATIONALLY_EQUIVALENT" | "UNDECIDED",
  "primary_candidate_id": string | null,
  "secondary_candidate_ids": [string],
  "evidence_ids": [string],
  "confidence": number,
  "rationale": string,
  "uncertainty": string,
  "narrative": {
    "student_behavior": string,
    "conflict_and_witness": string,
    "guidance_question": string
  } | null
}
"""

_PHASE5_SYSTEM = """你是 SQL 教学系统的安全反馈编辑器。
输入的 action_text 是服务器已经批准的教学内容，不是让你重新判题的材料。
保持每个 action_id 的顺序、教学意图和事实不变，只做清楚、自然、启发式的
语言润色。不要增加任何新事实，不要给出修复 SQL、SQL 片段、谓词、表名/列名
组合、精确值或直接改写指令。不要泄露参考答案。不要改变问题动作的方向。
只能输出一个 JSON 对象，不要 markdown。

输出格式必须严格为：
{
  "segments": [
    {"action_id": "action_1", "text": "..."}
  ]
}
segments 必须与输入 actions 一一对应，不能增加、删除或重排。
"""

_SQL_SHAPED_TEXT = re.compile(
    r"(?:```|\b(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b[\s\S]{0,240}\b(?:FROM|SET|AS)\b|"
    r"\b(?:WHERE|HAVING|JOIN|GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET)\b\s+[A-Za-z_\"`]|"
    r"[A-Za-z_][A-Za-z0-9_.\"`]*\s*(?:<>|!=|<=|>=|=|<|>)\s*[-+A-Za-z_\"`0-9])",
    flags=re.IGNORECASE,
)
_SECRET_FIELD = re.compile(
    r"(?:answer_sql|correct_sql|standard_sql|reference_sql|replacement_sql|"
    r"mutation_sql|test_database|witness_world|raw_observation)",
    flags=re.IGNORECASE,
)
_LLM_WORKER_SLOTS = threading.BoundedSemaphore(4)


@dataclass(frozen=True, slots=True)
class Phase2LLMAssessment:
    """A validated, internal-only Phase 2 model result."""

    decision: str
    authoritative_verdict: str
    primary_candidate_id: str | None
    secondary_candidate_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float
    rationale: str
    uncertainty: str
    narrative: dict[str, str] | None
    model: str
    provider: str = LLM_PROVIDER_KIND
    schema_version: str = PHASE2_LLM_SCHEMA_VERSION
    policy_version: str = PHASE2_LLM_POLICY_VERSION

    def to_internal_dict(self) -> dict[str, Any]:
        """Return bounded audit metadata; never use this as learner content."""

        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "provider": self.provider,
            "model": self.model,
            "decision": self.decision,
            "authoritative_verdict": self.authoritative_verdict,
            "primary_candidate_id": self.primary_candidate_id,
            "secondary_candidate_ids": list(self.secondary_candidate_ids),
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "rationale": self.rationale,
            "uncertainty": self.uncertainty,
            "narrative_applied": self.narrative is not None,
        }


@dataclass(frozen=True, slots=True)
class Phase5LLMFeedback:
    """Validated segment replacements for one approved Phase 4 plan."""

    segments: tuple[tuple[str, str], ...]
    model: str
    provider: str = LLM_PROVIDER_KIND
    schema_version: str = PHASE5_LLM_SCHEMA_VERSION
    policy_version: str = PHASE5_LLM_POLICY_VERSION

    def text_by_action_id(self) -> dict[str, str]:
        return {action_id: text for action_id, text in self.segments}


def _bounded_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:maximum]


def _llm_enabled(stage: str) -> bool:
    """Enable only through an explicit feature flag and a configured key.

    The flag defaults to false so local tests and deployments carrying an
    ``AI_API_KEY`` do not unexpectedly make paid network calls.  Production
    configuration can enable both Phase 2 and Phase 5 independently.
    """

    if not bool(getattr(settings, "LLM_TEACHING_ENABLED", False)):
        return False
    if not bool(getattr(settings, f"LLM_{stage}_ENABLED", True)):
        return False
    return bool(str(getattr(settings, "AI_API_KEY", "") or "").strip()) and bool(
        str(getattr(settings, "AI_MODEL_NAME", "") or "").strip()
    )


def _model_name() -> str:
    return str(getattr(settings, "AI_MODEL_NAME", "") or "").strip() or "gpt-4o-mini"


def _wire_api() -> str:
    """Return the configured OpenAI-compatible protocol."""

    value = str(getattr(settings, "AI_WIRE_API", "chat_completions") or "")
    return "responses" if value.strip().lower() == "responses" else "chat_completions"


def _request_limits() -> tuple[float, int, int]:
    timeout = getattr(settings, "LLM_TIMEOUT_SECONDS", 8.0)
    max_tokens = getattr(settings, "LLM_MAX_OUTPUT_TOKENS", 1200)
    max_input = getattr(settings, "LLM_MAX_INPUT_BYTES", 48 * 1024)
    try:
        timeout = max(1.0, min(60.0, float(timeout)))
    except (TypeError, ValueError):
        timeout = 8.0
    try:
        max_tokens = max(128, min(8192, int(max_tokens)))
    except (TypeError, ValueError):
        max_tokens = 1200
    try:
        max_input = max(4096, min(256 * 1024, int(max_input)))
    except (TypeError, ValueError):
        max_input = 48 * 1024
    return timeout, max_tokens, max_input


def _client() -> AsyncOpenAI:
    kwargs: dict[str, Any] = {
        "api_key": str(getattr(settings, "AI_API_KEY", "") or "").strip(),
        "timeout": _request_limits()[0],
        "max_retries": 0,
    }
    base_url = str(getattr(settings, "AI_BASE_URL", "") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def _response_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, Mapping):
        choices = response.get("choices")
    if not isinstance(choices, Sequence) or not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None and isinstance(first, Mapping):
        message = first.get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, Mapping):
        content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def _responses_content(response: Any) -> str:
    """Extract text from an OpenAI Responses API object or JSON mapping."""

    output_text = getattr(response, "output_text", None)
    if output_text is None and isinstance(response, Mapping):
        output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = getattr(response, "output", None)
    if output is None and isinstance(response, Mapping):
        output = response.get("output")
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
        return ""

    parts: list[str] = []
    for item in output:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, Mapping):
            content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            continue
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, Mapping):
                text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _parse_json_object(content: str) -> Mapping[str, Any] | None:
    text = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    if not text or len(text.encode("utf-8")) > _request_limits()[2]:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


async def _provider_request_content(
    *,
    deliver: Callable[[tuple[str | None, str | None]], None],
    system_prompt: str,
    user_text: str,
    max_tokens: int,
) -> None:
    """Run one provider request inside the disposable worker event loop."""

    client = _client()
    response_received = False
    try:
        if _wire_api() == "responses":
            # Responses has a separate instructions channel and does not
            # accept temperature consistently across reasoning models.
            response = await client.responses.create(
                model=_model_name(),
                instructions=system_prompt,
                input=user_text,
                max_output_tokens=max_tokens,
                store=False,
            )
            content = _responses_content(response)
        else:
            response = await client.chat.completions.create(
                model=_model_name(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.0,
                max_tokens=max_tokens,
            )
            content = _response_content(response)
        response_received = True
        deliver((content, None))
    finally:
        # On a provider failure the transport may still be unwinding its own
        # timeout.  Do not start a second network operation in that failure
        # path.  Successful calls get a bounded best-effort close in the
        # disposable worker loop.
        if response_received:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    try:
                        await asyncio.wait_for(result, timeout=1.0)
                    except (Exception, asyncio.CancelledError):
                        logger.debug("LLM client close failed", exc_info=True)


def _deliver_provider_result(
    future: asyncio.Future[tuple[str | None, str | None]],
    result: tuple[str | None, str | None],
) -> None:
    if not future.done():
        future.set_result(result)


def _provider_worker(
    loop: asyncio.AbstractEventLoop,
    future: asyncio.Future[tuple[str | None, str | None]],
    *,
    system_prompt: str,
    user_text: str,
    max_tokens: int,
) -> None:
    """Execute provider I/O off-loop so a broken transport cannot pin ASGI."""

    try:
        asyncio.run(
            _provider_request_content(
                deliver=lambda result: loop.call_soon_threadsafe(
                    _deliver_provider_result,
                    future,
                    result,
                ),
                system_prompt=system_prompt,
                user_text=user_text,
                max_tokens=max_tokens,
            )
        )
    except asyncio.CancelledError:
        result = (None, "CancelledError")
    except Exception as exc:  # provider failures are safe fallback signals
        result = (None, type(exc).__name__)
    else:
        # Successful responses are delivered before the best-effort client
        # close, so cleanup latency cannot consume the request's LLM budget.
        return
    finally:
        _LLM_WORKER_SLOTS.release()
    try:
        loop.call_soon_threadsafe(_deliver_provider_result, future, result)
    except RuntimeError:
        # The request loop may have closed after a caller cancellation.
        pass


async def _request_json(
    *,
    stage: str,
    system_prompt: str,
    user_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if not _llm_enabled(stage):
        return None
    timeout, max_tokens, max_input = _request_limits()
    try:
        user_text = json.dumps(
            user_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    if len(user_text.encode("utf-8")) > max_input:
        return None

    if not _LLM_WORKER_SLOTS.acquire(blocking=False):
        logger.warning("%s LLM worker capacity exhausted", stage)
        return None
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[tuple[str | None, str | None]] = loop.create_future()
    try:
        worker = threading.Thread(
            target=_provider_worker,
            kwargs={
                "loop": loop,
                "future": result_future,
                "system_prompt": system_prompt,
                "user_text": user_text,
                "max_tokens": max_tokens,
            },
            name="sql-edu-llm-provider",
            daemon=True,
        )
        worker.start()
    except Exception:
        _LLM_WORKER_SLOTS.release()
        logger.warning("%s LLM worker could not start", stage)
        return None

    try:
        content, error_type = await asyncio.wait_for(
            asyncio.shield(result_future),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        result_future.cancel()
        logger.warning("%s LLM call unavailable (TimeoutError)", stage)
        return None
    except asyncio.CancelledError:
        result_future.cancel()
        raise
    if error_type is not None:
        logger.warning("%s LLM call unavailable (%s)", stage, error_type)
        return None
    return _parse_json_object(content or "")


def _bounded_json(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Copy evidence into a small prompt-safe JSON shape.

    Raw databases, SQL-bearing internal fields, and unbounded witness payloads
    are intentionally excluded.  Scalar counts and bounded evidence summaries
    remain available to the model.
    """

    if budget is None:
        budget = [12000]
    if budget[0] <= 0 or depth > 5:
        return "[truncated]"
    budget[0] -= 1
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        return value[:320]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:80]:
            key = str(raw_key)
            key_lower = key.lower()
            if any(token in key_lower for token in ("test_database", "raw_observation", "standard_sqlite", "student_sqlite")):
                continue
            if key_lower.endswith("_sql") or key_lower in {"sql", "query", "query_text"}:
                result[key] = "[redacted from evidence summary]"
                continue
            if key_lower in {"rows", "cases", "worlds", "witness_world"}:
                # Keep cardinality but not raw data values.
                if isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes)):
                    result[f"{key}_count"] = len(raw_value)
                elif isinstance(raw_value, Mapping):
                    result[f"{key}_keys"] = list(raw_value)[:16]
                continue
            result[key] = _bounded_json(raw_value, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _bounded_json(item, depth=depth + 1, budget=budget)
            for item in list(value)[:40]
        ]
    return str(value)[:320]


def _package_dict(package: Any) -> dict[str, Any]:
    if isinstance(package, Mapping):
        value = dict(package)
    else:
        to_dict = getattr(package, "to_dict", None)
        value = to_dict() if callable(to_dict) else {}
    return value if isinstance(value, dict) else {}


def _candidate_records(package: Any, public: Mapping[str, Any]) -> list[dict[str, Any]]:
    trace = getattr(package, "candidate_trace", None)
    records: list[dict[str, Any]] = []
    if isinstance(trace, Sequence):
        for candidate in list(trace)[:64]:
            internal = getattr(candidate, "internal_dict", None)
            if callable(internal):
                value = internal()
                if isinstance(value, Mapping):
                    records.append(_bounded_json(dict(value)))
    if records:
        return records
    values: list[Any] = [public.get("primary")]
    secondary = public.get("secondary")
    if isinstance(secondary, Sequence) and not isinstance(secondary, (str, bytes)):
        values.extend(secondary)
    return [
        dict(value)
        for value in values
        if isinstance(value, Mapping) and isinstance(value.get("candidate_id"), str)
    ]


def _phase1_summary(sandbox_run: Any) -> dict[str, Any]:
    data = getattr(sandbox_run, "data_evidence", {}) or {}
    mutations = getattr(sandbox_run, "mutation_evidence", {}) or {}
    ast_diffs = getattr(sandbox_run, "ast_diffs", []) or []
    bounded_data = _bounded_json(data)
    data_status = bounded_data.get("status") if isinstance(bounded_data, Mapping) else None
    diff_summary: list[dict[str, Any]] = []
    for diff in list(ast_diffs)[:64]:
        if isinstance(diff, Mapping):
            raw = dict(diff)
        else:
            raw = {}
            for name in (
                "diff_id", "obligation_id", "clause", "diff_type", "query_scope",
                "logical_stage", "teaching_stage", "evidence_grade",
            ):
                if hasattr(diff, name):
                    raw[name] = getattr(diff, name)
        if raw:
            diff_summary.append(_bounded_json(raw))
    return {
        "status": getattr(sandbox_run, "status", None) or data_status,
        "equivalence_conclusion": getattr(sandbox_run, "equivalence_conclusion", None),
        "judge_status": getattr(sandbox_run, "judge_status", None),
        "executed": getattr(sandbox_run, "executed", None),
        "data_evidence": _bounded_json(data),
        "mutation_evidence": _bounded_json(mutations),
        "ast_diffs": diff_summary,
    }


def _witness_for_internal_review(value: Any) -> Any:
    """Keep a tiny selected-witness sample for Phase 2 reasoning.

    The generic evidence copier omits every ``rows``/``cases`` collection to
    stay conservative.  Phase 2 is an internal review, however, and needs the
    actual bounded witness values to distinguish a boundary, NULL, join, or
    CASE branch.  This function admits only the already selected public
    witness, strips SQL-bearing fields, and caps every collection.
    """

    def copy_node(node: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[truncated]"
        if node is None or isinstance(node, (bool, int, float)):
            if isinstance(node, float) and not math.isfinite(node):
                return None
            return node
        if isinstance(node, str):
            return node[:240]
        if isinstance(node, Mapping):
            output: dict[str, Any] = {}
            for raw_key, raw_value in list(node.items())[:32]:
                key = str(raw_key)
                lowered = key.lower()
                if (
                    lowered.endswith("_sql")
                    or lowered in {"sql", "query", "query_text", "test_database"}
                ):
                    continue
                output[key] = copy_node(raw_value, depth + 1)
            return output
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            return [copy_node(item, depth + 1) for item in list(node)[:8]]
        return str(node)[:240]

    return copy_node(value)


def _forbidden_text(text: str) -> bool:
    return bool(
        not text
        or len(text) > 800
        or "```" in text
        or ";" in text
        or _SECRET_FIELD.search(text)
        or _SQL_SHAPED_TEXT.search(text)
    )


def _valid_narrative(
    value: Any,
    *,
    required_guidance: str,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "student_behavior", "conflict_and_witness", "guidance_question"
    }:
        return None
    result: dict[str, str] = {}
    for key in ("student_behavior", "conflict_and_witness", "guidance_question"):
        text = value.get(key)
        if not isinstance(text, str):
            return None
        text = text.strip()
        if _forbidden_text(text):
            return None
        result[key] = text
    # Phase 2 owns the direction of the Socratic question.  The LLM can
    # improve the explanation around it but cannot turn it into a repair hint.
    # Models often paraphrase the question even when asked not to.  Validate
    # that proposed text is safe, then keep the server-owned wording instead
    # of rejecting the whole otherwise-valid evidence review.
    if not required_guidance or _forbidden_text(required_guidance):
        return None
    result["guidance_question"] = required_guidance
    return result


def _candidate_ids(records: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    all_ids: set[str] = set()
    eligible_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for record in records:
        candidate_id = record.get("candidate_id")
        if isinstance(candidate_id, str):
            all_ids.add(candidate_id)
            grade = str(record.get("evidence_grade") or "")
            blocking = record.get("blocking") is True or grade in {
                "REPAIR_VERIFIED", "CAUSAL_VERIFIED"
            }
            if blocking:
                eligible_ids.add(candidate_id)
        refs = record.get("evidence_refs")
        if isinstance(refs, Mapping):
            for key in ("diff_ids", "verified_diff_ids", "obligation_ids", "mutation_test_ids"):
                values = refs.get(key)
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    evidence_ids.update(item for item in values if isinstance(item, str))
    return all_ids, eligible_ids, evidence_ids


def _valid_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0.0 <= result <= 1.0 else None


async def arbitrate_phase2_evidence(
    *,
    package: Any,
    sandbox_run: Any,
    attribution_result: Any = None,
    question: str = "",
    schema: Mapping[str, Any] | None = None,
    standard_sql: str = "",
    student_sql: str = "",
    language: str = "zh-CN",
) -> Phase2LLMAssessment | None:
    """Ask the model to review Phase 1 evidence without granting authority.

    ``None`` means disabled/unavailable/malformed.  A returned assessment has
    already passed all candidate, evidence, verdict, and narrative checks.
    """

    public = _package_dict(package)
    authoritative_verdict = str(public.get("verdict") or "UNDECIDED").upper()
    if authoritative_verdict not in {"CORRECT", "INCORRECT"}:
        return None
    records = _candidate_records(package, public)
    all_candidate_ids, eligible_candidate_ids, available_evidence_ids = _candidate_ids(records)
    attribution_input = getattr(attribution_result, "llm_arbitration_input", None)
    user_payload = {
        "authoritative_verdict": authoritative_verdict,
        "language": language,
        "task_description": _bounded_text(question, 4000),
        "schema_summary": _bounded_json(schema or {}),
        "reference_sql_for_internal_review_only": _bounded_text(standard_sql, 12000),
        "student_sql_for_internal_review_only": _bounded_text(student_sql, 12000),
        "phase1_summary": _phase1_summary(sandbox_run),
        "phase2_deterministic_package": {
            "phase1": _bounded_json(public.get("phase1") or {}),
            "ordered_diff_pipeline": _bounded_json(public.get("ordered_diff_pipeline") or []),
            "candidates": records,
            "witness": _witness_for_internal_review(public.get("witness")),
            "boundary_notes": _bounded_json(public.get("boundary_notes") or []),
        },
        "attribution_evidence": _bounded_json(attribution_input or {}),
        "permitted_candidate_ids": sorted(all_candidate_ids),
        "permitted_strong_candidate_ids": sorted(eligible_candidate_ids),
        "permitted_evidence_ids": sorted(available_evidence_ids),
    }
    raw = await _request_json(
        stage="PHASE2",
        system_prompt=_PHASE2_SYSTEM,
        user_payload=user_payload,
    )
    if raw is None:
        return None
    required = {
        "decision",
        "primary_candidate_id",
        "secondary_candidate_ids",
        "evidence_ids",
        "confidence",
        "rationale",
        "uncertainty",
        "narrative",
    }
    if set(raw) != required:
        return None
    decision = raw.get("decision")
    expected_decision = (
        "OPERATIONALLY_EQUIVALENT" if authoritative_verdict == "CORRECT" else None
    )
    if authoritative_verdict == "CORRECT":
        if decision != expected_decision:
            return None
    elif decision not in {"SUPPORTED_WRONG", "UNDECIDED"}:
        return None

    primary = raw.get("primary_candidate_id")
    if primary is not None and (
        not isinstance(primary, str) or primary not in eligible_candidate_ids
    ):
        return None
    if decision == "SUPPORTED_WRONG" and primary is None:
        # A positive wrong-answer assessment without a concrete strong target
        # is not actionable; represent that uncertainty explicitly instead.
        return None
    secondary_raw = raw.get("secondary_candidate_ids")
    if not isinstance(secondary_raw, Sequence) or isinstance(secondary_raw, (str, bytes)):
        return None
    secondary = tuple(item for item in secondary_raw if isinstance(item, str))
    if len(secondary) != len(secondary_raw) or len(secondary) > 2 or len(set(secondary)) != len(secondary):
        return None
    if any(item not in eligible_candidate_ids or item == primary for item in secondary):
        return None

    evidence_raw = raw.get("evidence_ids")
    if not isinstance(evidence_raw, Sequence) or isinstance(evidence_raw, (str, bytes)):
        return None
    evidence_ids = tuple(item for item in evidence_raw if isinstance(item, str))
    if len(evidence_ids) != len(evidence_raw) or len(evidence_ids) > 24:
        return None
    if any(item not in available_evidence_ids for item in evidence_ids):
        return None
    if decision == "SUPPORTED_WRONG" and not evidence_ids:
        return None
    confidence = _valid_confidence(raw.get("confidence"))
    rationale = _bounded_text(raw.get("rationale"), 800)
    uncertainty = _bounded_text(raw.get("uncertainty"), 800)
    if (
        confidence is None
        or not rationale
        or not uncertainty
        or _forbidden_text(rationale)
        or _forbidden_text(uncertainty)
    ):
        return None
    narrative = _valid_narrative(
        raw.get("narrative"),
        required_guidance=str(
            (_package_dict(package).get("narrative") or {}).get("guidance_question") or ""
        ).strip(),
    )
    if raw.get("narrative") is not None and narrative is None:
        return None
    if decision == "UNDECIDED" and (primary is not None or secondary or narrative is not None):
        return None
    return Phase2LLMAssessment(
        decision=decision,
        authoritative_verdict=authoritative_verdict,
        primary_candidate_id=primary,
        secondary_candidate_ids=secondary,
        evidence_ids=evidence_ids,
        confidence=confidence,
        rationale=rationale,
        uncertainty=uncertainty,
        narrative=narrative,
        model=_model_name(),
    )


def merge_phase2_llm_assessment(
    package: Mapping[str, Any],
    assessment: Phase2LLMAssessment | None,
) -> dict[str, Any]:
    """Apply only a validated candidate ordering/narrative overlay.

    The function intentionally accepts a public package and returns a public
    package.  It never copies the model's rationale or raw evidence to the
    learner-facing contract.
    """

    result = dict(package)
    if assessment is None or assessment.authoritative_verdict != result.get("verdict"):
        return result
    if assessment.decision == "SUPPORTED_WRONG" and assessment.primary_candidate_id:
        candidates: list[dict[str, Any]] = []
        primary = result.get("primary")
        if isinstance(primary, Mapping):
            candidates.append(dict(primary))
        secondary = result.get("secondary")
        if isinstance(secondary, Sequence) and not isinstance(secondary, (str, bytes)):
            candidates.extend(dict(item) for item in secondary if isinstance(item, Mapping))
        selected = next(
            (item for item in candidates if item.get("candidate_id") == assessment.primary_candidate_id),
            None,
        )
        if selected is not None:
            by_id = {
                item.get("candidate_id"): item
                for item in candidates
                if isinstance(item.get("candidate_id"), str)
            }
            ordered_ids = [
                item
                for item in assessment.secondary_candidate_ids
                if item in by_id and item != assessment.primary_candidate_id
            ]
            ordered = [by_id[item] for item in ordered_ids]
            ordered.extend(
                item
                for item in candidates
                if item.get("candidate_id") not in {
                    assessment.primary_candidate_id,
                    *ordered_ids,
                }
            )
            result["primary"] = selected
            result["secondary"] = ordered[: max(0, len(candidates) - 1)]
            result["secondary_count"] = len(result["secondary"])
    if assessment.narrative is not None:
        result["narrative"] = dict(assessment.narrative)
    return result


def _normalised_for_similarity(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _feedback_text_allowed(text: str, original: str) -> bool:
    if _forbidden_text(text):
        return False
    if len(text) > max(800, int(len(original) * 1.8) + 180):
        return False
    original_norm = _normalised_for_similarity(original)
    text_norm = _normalised_for_similarity(text)
    if not original_norm or not text_norm:
        return False
    # A rewrite must remain recognisably anchored to the approved action.  A
    # low overlap means the model likely introduced an unrelated instruction.
    similarity = SequenceMatcher(None, original_norm, text_norm).ratio()
    return similarity >= 0.24 or original_norm in text_norm


async def generate_phase5_feedback(
    plan: TeachingActionPlan,
) -> Phase5LLMFeedback | None:
    """Generate safe replacements for an already selected Phase 4 plan."""

    if not isinstance(plan, TeachingActionPlan) or not _llm_enabled("PHASE5"):
        return None
    actions = [
        {
            "action_id": action.action_id,
            "kind": action.kind.value if hasattr(action.kind, "value") else str(action.kind),
            "action_text": _bounded_text(action.text, 1000),
        }
        for action in plan.actions
    ]
    if not actions:
        return None
    fixed_kinds = {
        TeachingActionKind.SOCRATIC_QUESTION.value,
        TeachingActionKind.ACCEPTANCE.value,
        TeachingActionKind.SYSTEM_NOTICE.value,
    }
    # A one-question L1 response and fixed system notices have no editable
    # prose.  Avoid a paid call whose only safe result would be byte-for-byte
    # identical text.
    if all(action["kind"] in fixed_kinds for action in actions):
        return None
    editable_actions = [
        action for action in actions if action["kind"] not in fixed_kinds
    ]
    raw = await _request_json(
        stage="PHASE5",
        system_prompt=_PHASE5_SYSTEM,
        user_payload={
            "language": plan.language,
            "delivered_support_level": plan.delivered_support_level,
            # Fixed actions are intentionally omitted from the model request.
            # They are copied back below, so a model cannot accidentally
            # rewrite a Socratic question, acceptance, or system notice.
            "actions": editable_actions,
            "answer_revealed": False,
        },
    )
    if raw is None or set(raw) != {"segments"}:
        return None
    segments_raw = raw.get("segments")
    if not isinstance(segments_raw, Sequence) or isinstance(segments_raw, (str, bytes)):
        return None
    if len(segments_raw) != len(editable_actions):
        return None
    editable_segments: dict[str, str] = {}
    for action, raw_segment in zip(editable_actions, segments_raw):
        if not isinstance(raw_segment, Mapping) or set(raw_segment) != {"action_id", "text"}:
            return None
        if raw_segment.get("action_id") != action["action_id"]:
            return None
        text = raw_segment.get("text")
        if not isinstance(text, str):
            return None
        text = text.strip()
        if not _feedback_text_allowed(text, action["action_text"]):
            return None
        editable_segments[action["action_id"]] = text
    segments = [
        (
            action["action_id"],
            action["action_text"]
            if action["kind"] in fixed_kinds
            else editable_segments[action["action_id"]],
        )
        for action in actions
    ]
    return Phase5LLMFeedback(
        segments=tuple(segments),
        model=_model_name(),
    )


__all__ = [
    "LLM_PROVIDER_KIND",
    "PHASE2_LLM_POLICY_VERSION",
    "PHASE2_LLM_SCHEMA_VERSION",
    "PHASE5_LLM_POLICY_VERSION",
    "PHASE5_LLM_SCHEMA_VERSION",
    "Phase2LLMAssessment",
    "Phase5LLMFeedback",
    "arbitrate_phase2_evidence",
    "generate_phase5_feedback",
    "merge_phase2_llm_assessment",
]
