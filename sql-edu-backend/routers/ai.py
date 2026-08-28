"""
AI 相关路由（/ai）

包含：
- `/ai/sql-hint`: 仅生成提示（已下线，返回静态内容）
- `/ai/check-sql`: 判题 + 证据约束诊断 + 可选 LLM 文案（Stage 1 Observe 驱动）
- `/ai/mastery-radar`: 获取版本化的原始 BKT 学习画像
- `/ai/chat/*`: 多轮对话历史与本地 Socratic 辅导
"""

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import multiprocessing
import os
import pickle
import re
import signal
import tempfile
import time
from threading import Lock
from uuid import UUID

try:
    import resource
except ImportError:  # pragma: no cover - WSL/Linux production path has resource
    resource = None

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List, Dict, Any, Literal

from core.native_query_safety import (
    NATIVE_SQL_PARSE_ERROR,
    NativeQuerySafetyError,
    validate_native_query_safety,
)
from core.public_schema_preview import sanitize_schema_preview_object
from repository import QuestionRepository, SubmissionRepository, ChatRepository
from schemas.submission import SubmissionCreate, SubmissionOut
from schemas.chat import ChatMessageOut, ChatSendIn, ChatSendOut
from dependencies import get_session
from core.auth import AuthHandler
from models.user import User

# 数学闭环第一阶段（Observe/感知）核心引擎库
from core.error_attribution import evidence_weights_from_observation, KPAttribution
from core.error_diagnosis import (
    DIAGNOSIS_VERSION as PHASE2_DIAGNOSIS_VERSION,
    PUBLIC_SCHEMA_VERSION as PHASE2_PUBLIC_SCHEMA_VERSION,
    RULE_CATALOG_VERSION as PHASE2_RULE_CATALOG_VERSION,
)
from core.support_policy import SUPPORT_POLICY_VERSION
from core.sql_dialect_resolver import (
    DialectResolutionError,
    GENERIC_SQLGLOT_DIALECT,
    resolve_sql_dialect_or_raise,
)
from core.phase1_verdict import is_teachable_wrong
from schemas.agent import SQLCheckResultSchema
from settings.config import settings

router = APIRouter(prefix="/ai", tags=["ai"])
auth_handler = AuthHandler()
logger = logging.getLogger(__name__)
_ORIGINAL_ASYNCIO_TO_THREAD = asyncio.to_thread

_NATIVE_EXECUTOR_URL_SETTINGS = {
    "mysql": "PARSEVAL_MYSQL_URL",
    "postgres": "PARSEVAL_POSTGRES_URL",
    "tsql": "PARSEVAL_TSQL_URL",
    "oracle": "PARSEVAL_ORACLE_URL",
}
_NATIVE_EXECUTOR_VERSION_SETTINGS = {
    "mysql": "PARSEVAL_MYSQL_VERSION",
    "postgres": "PARSEVAL_POSTGRES_VERSION",
    "tsql": "PARSEVAL_TSQL_VERSION",
    "oracle": "PARSEVAL_ORACLE_VERSION",
}

_PHASE1_MAX_CONCURRENCY = int(
    getattr(settings, "PARSEVAL_WORKER_MAX_CONCURRENCY", 2)
)
_PHASE1_QUEUE_LIMIT = int(
    getattr(settings, "PARSEVAL_WORKER_QUEUE_LIMIT", 8)
)
_PHASE1_QUEUE_TIMEOUT_SECONDS = 5.0
_PHASE1_STARTUP_TIMEOUT_SECONDS = 5.0
_PHASE1_RUN_TIMEOUT_SECONDS = 45.0
_PHASE1_WORK_SLOTS = asyncio.Semaphore(_PHASE1_MAX_CONCURRENCY)
# Admission capacity covers both active workers and bounded waiters. Keeping
# this separate from work slots prevents an unbounded number of coroutines from
# waiting on the worker semaphore under load.
_PHASE1_ADMISSION_SLOTS = asyncio.Semaphore(
    _PHASE1_MAX_CONCURRENCY + _PHASE1_QUEUE_LIMIT
)


@dataclass(slots=True)
class _AttemptFlight:
    lock: asyncio.Lock
    participant_count: int = 0


# Coalesce identical attempt identities before they consume a scarce Phase 1
# worker.  This is deliberately process-local; the database uniqueness and
# current-read checks remain the cross-process correctness boundary.
_ATTEMPT_FLIGHTS: dict[tuple[int, int, str], _AttemptFlight] = {}
_ATTEMPT_FLIGHTS_GUARD = Lock()


@asynccontextmanager
async def _serialize_attempt_in_process(
    user_id: int,
    question_id: int,
    attempt_id: str,
):
    key = (user_id, question_id, attempt_id)
    with _ATTEMPT_FLIGHTS_GUARD:
        flight = _ATTEMPT_FLIGHTS.get(key)
        if flight is None:
            flight = _AttemptFlight(lock=asyncio.Lock())
            _ATTEMPT_FLIGHTS[key] = flight
        flight.participant_count += 1

    acquired = False
    try:
        await flight.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            flight.lock.release()
        with _ATTEMPT_FLIGHTS_GUARD:
            flight.participant_count -= 1
            if (
                flight.participant_count == 0
                and _ATTEMPT_FLIGHTS.get(key) is flight
            ):
                del _ATTEMPT_FLIGHTS[key]


def _release_phase1_slot(
    task: asyncio.Task[Any],
    slots: asyncio.Semaphore,
    admission: asyncio.Semaphore | None = None,
) -> None:
    """Release capacity only when the worker thread has actually finished."""
    slots.release()
    if admission is not None:
        admission.release()
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        # The request path reports the original failure.  This callback also
        # consumes failures from a task that outlived a timed-out request.
        pass


def _phase1_process_entry(
    function: Any,
    kwargs: dict[str, Any],
    result_path: str,
    memory_mb: int,
    cpu_seconds: int,
    started_event: Any,
) -> None:
    """Execute one pure Phase 1 job in a killable child process."""
    try:
        # Make the child the leader of a private process group before running
        # user-controlled parsing/witness code.  A timeout can then terminate
        # descendants too (for example a helper process accidentally spawned
        # by a native parser), instead of leaving them outside the API's
        # capacity accounting.
        if os.name == "posix":
            try:
                os.setsid()
            except OSError:
                # The parent verifies the process group before using killpg;
                # failure here therefore falls back to killing only this child.
                pass
        if resource is not None:
            memory_mb = max(128, int(memory_mb))
            cpu_seconds = max(1, int(cpu_seconds))
            memory_bytes = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # ``spawn`` may spend more than a short request timeout importing the
        # API module on a busy WSL filesystem.  Signal readiness only after
        # the process group and resource limits are in place; the caller then
        # applies the requested timeout to user work while retaining a hard,
        # separately bounded startup window.
        started_event.set()
        result = function(**kwargs)
        envelope = ("ok", result)
    except BaseException as exc:  # propagate a safe, picklable error envelope
        envelope = ("error", type(exc).__name__, str(exc))
    with open(result_path, "wb") as handle:
        pickle.dump(envelope, handle, protocol=pickle.HIGHEST_PROTOCOL)


def _terminate_phase1_process(process: multiprocessing.Process) -> None:
    """Kill a Phase 1 child and its descendants without touching our group."""
    if not process.is_alive():
        process.join(timeout=1.0)
        return
    if os.name == "posix" and process.pid is not None:
        try:
            process_group = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            process_group = None
        # Never call killpg on an inherited API process group.  The child calls
        # setsid() before user work, so equality is a proof that the group is
        # private to this request.
        if process_group == process.pid:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
    else:
        process.kill()
    process.join(timeout=1.0)


def _should_use_phase1_process(function: Any) -> bool:
    """Use processes for the real parser; keep test doubles easy to patch."""
    mode = str(getattr(settings, "PARSEVAL_WORKER_MODE", "process")).lower()
    # Tests replace asyncio.to_thread with a deterministic coroutine. Preserve
    # that seam; production uses the stdlib implementation.
    thread_is_patched = asyncio.to_thread is not _ORIGINAL_ASYNCIO_TO_THREAD
    return (
        mode == "process"
        and not thread_is_patched
        and getattr(function, "__module__", "") == "core.parseval_data_generator"
    )


async def _run_phase1_process_bounded(function: Any, *, timeout_seconds: float, **kwargs: Any) -> Any:
    """Run a real Phase 1 job in an isolated process with a hard timeout."""
    start_method = str(
        getattr(settings, "PARSEVAL_WORKER_START_METHOD", "spawn")
    ).strip().lower() or "spawn"
    try:
        context = multiprocessing.get_context(start_method)
    except ValueError as exc:
        raise RuntimeError(
            f"unsupported Phase 1 worker start method: {start_method}"
        ) from exc
    result_fd, result_path = tempfile.mkstemp(prefix="sql-edu-phase1-worker-", suffix=".pickle")
    os.close(result_fd)
    started_event = context.Event()
    process = context.Process(
        target=_phase1_process_entry,
        args=(
            function,
            kwargs,
            result_path,
            int(getattr(settings, "PARSEVAL_WORKER_MEMORY_MB", 2048)),
            int(getattr(settings, "PARSEVAL_WORKER_CPU_SECONDS", 50)),
            started_event,
        ),
        daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        startup_deadline = time.monotonic() + _PHASE1_STARTUP_TIMEOUT_SECONDS
        while process.is_alive() and not started_event.is_set():
            if time.monotonic() >= startup_deadline:
                _terminate_phase1_process(process)
                raise TimeoutError(
                    "Phase 1 worker startup exceeded "
                    f"{_PHASE1_STARTUP_TIMEOUT_SECONDS:g} seconds"
                )
            await asyncio.sleep(0.01)
        # A child that exited before signalling readiness is handled by the
        # normal result-envelope path below, preserving crash diagnostics.
        deadline = time.monotonic() + timeout_seconds
        while process.is_alive():
            if time.monotonic() >= deadline:
                _terminate_phase1_process(process)
                raise TimeoutError(
                    f"Phase 1 verification exceeded {timeout_seconds:g} seconds"
                )
            await asyncio.sleep(0.01)
        process.join(timeout=0)
        try:
            with open(result_path, "rb") as handle:
                envelope = pickle.load(handle)
        except Exception as exc:
            # RLIMIT_CPU may terminate a busy child with SIGXCPU/SIGKILL
            # before it can write the envelope.  Surface this as a bounded
            # resource timeout instead of an opaque worker crash.
            resource_exit_codes = {
                -int(getattr(signal, "SIGXCPU", 24)),
                -int(getattr(signal, "SIGKILL", 9)),
            }
            if process.exitcode in resource_exit_codes:
                raise TimeoutError(
                    "Phase 1 worker exceeded its CPU or memory resource limit"
                ) from exc
            raise RuntimeError(
                f"Phase 1 worker exited without a result (exitcode={process.exitcode})"
            ) from exc
        if envelope[0] == "ok":
            return envelope[1]
        raise RuntimeError(f"Phase 1 worker failed: {envelope[1]}: {envelope[2]}")
    finally:
        if started:
            _terminate_phase1_process(process)
        try:
            os.unlink(result_path)
        except FileNotFoundError:
            pass


async def _run_phase1_bounded(function: Any, **kwargs: Any) -> Any:
    """Run one Phase 1 job with bounded admission and wall-clock waiting.

    Real Phase 1 jobs use a child process so a timeout can reclaim CPU and
    memory. Test doubles retain the thread adapter to keep unit tests
    deterministic and patchable.
    """
    slots = _PHASE1_WORK_SLOTS
    admission = _PHASE1_ADMISSION_SLOTS
    try:
        await asyncio.wait_for(
            admission.acquire(),
            timeout=_PHASE1_QUEUE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise TimeoutError("Phase 1 worker queue is full") from exc

    admission_released = False
    try:
        await asyncio.wait_for(
            slots.acquire(),
            timeout=_PHASE1_QUEUE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        admission.release()
        admission_released = True
        raise TimeoutError("Phase 1 worker capacity is temporarily exhausted") from exc
    except asyncio.CancelledError:
        admission.release()
        admission_released = True
        raise

    try:
        if _should_use_phase1_process(function):
            try:
                return await _run_phase1_process_bounded(
                    function,
                    timeout_seconds=_PHASE1_RUN_TIMEOUT_SECONDS,
                    **kwargs,
                )
            finally:
                admission.release()
                admission_released = True
        try:
            task = asyncio.create_task(asyncio.to_thread(function, **kwargs))
        except Exception:
            slots.release()
            admission.release()
            admission_released = True
            raise
        task.add_done_callback(
            lambda completed, bound_slots=slots, bound_admission=admission: _release_phase1_slot(
                completed,
                bound_slots,
                bound_admission,
            )
        )
        return await asyncio.wait_for(asyncio.shield(task), timeout=_PHASE1_RUN_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise TimeoutError(
            f"Phase 1 verification exceeded {_PHASE1_RUN_TIMEOUT_SECONDS:g} seconds"
        ) from exc
    finally:
        # Thread jobs release their slot from the completion callback because
        # a timed-out request must not admit another thread. Process jobs are
        # already terminated or completed before this function returns.
        if _should_use_phase1_process(function):
            slots.release()
            if not admission_released:
                admission.release()


def _native_executor_url_for_dialect(dialect: str | None) -> str | None:
    setting_name = _NATIVE_EXECUTOR_URL_SETTINGS.get(dialect or "")
    if setting_name is None:
        return None
    return getattr(settings, setting_name, "").strip() or None


def _validate_native_engine_version(
    dialect: str | None,
    required_version: str | None,
) -> None:
    required = str(required_version or "").strip().lower()
    if not required:
        return
    setting_name = _NATIVE_EXECUTOR_VERSION_SETTINGS.get(dialect or "")
    configured = str(getattr(settings, setting_name, "") if setting_name else "").strip().lower()
    if configured:
        if (
            required == configured
            or required.startswith(configured + ".")
            or configured.startswith(required + ".")
        ):
            return
        # Vendor patch/minor labels vary (for example MySQL 8.0 vs 8.4),
        # while the execution contract is pinned at the major engine family.
        required_major = re.match(r"^(\d+)", required)
        configured_major = re.match(r"^(\d+)", configured)
        if (
            required_major is not None
            and configured_major is not None
            and required_major.group(1) == configured_major.group(1)
        ):
            return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "ENGINE_VERSION_UNAVAILABLE",
            "judge_status": "UNSUPPORTED",
            "message": (
                f"Question requires {dialect or 'unknown'} {required_version}, "
                f"but the configured runner version is {configured or 'not declared'}."
            ),
            "dialect_resolution": None,
        },
    )


def _raise_platform_judge_error(
    *,
    judge_status: str,
    error_message: str | None,
    error_code: str | None = None,
    dialect_resolution: dict[str, Any] | None = None,
) -> None:
    raw_code = error_code or judge_status or "ENGINE_ERROR"
    client_error_codes = {
        "DIALECT_CONFLICT",
        "SECURITY_REJECTED",
        "UNSUPPORTED",
        "UNSUPPORTED_DIALECT",
        "UNSUPPORTED_DIALECT_FEATURE",
    }
    http_status = (
        status.HTTP_422_UNPROCESSABLE_ENTITY
        if raw_code in client_error_codes
        or judge_status in {"UNSUPPORTED", "SECURITY_REJECTED"}
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    # Runner/database errors can contain credentials, network addresses, or the
    # submitted SQL.  Keep those details in server logs, never in the API
    # response.  Security rejections likewise use a stable policy message so a
    # parser or runner cannot echo user-controlled text back to clients.
    logger.error(
        "SQL judge platform failure: status=%s code=%s error=%s",
        judge_status,
        raw_code,
        error_message,
    )
    if http_status == status.HTTP_503_SERVICE_UNAVAILABLE:
        code = judge_status if judge_status in {"ENGINE_ERROR", "TIMEOUT"} else "ENGINE_ERROR"
        public_message = (
            "SQL judge execution timed out. Please try again later."
            if judge_status == "TIMEOUT"
            else "SQL judge service is temporarily unavailable. Please try again later."
        )
    elif judge_status == "SECURITY_REJECTED" or raw_code == "SECURITY_REJECTED":
        code = (
            raw_code
            if raw_code in client_error_codes or raw_code.startswith("NATIVE_SQL_")
            else "SECURITY_REJECTED"
        )
        public_message = "SQL rejected by the sandbox safety policy."
    else:
        code = raw_code
        public_message = error_message or "SQL judge platform failed before producing a verdict."
    public_dialect_resolution = dialect_resolution
    if http_status == status.HTTP_503_SERVICE_UNAVAILABLE and dialect_resolution:
        # Resolver diagnostics can include parser text; preserve routing fields
        # while dropping the free-form error from an infrastructure response.
        public_dialect_resolution = dict(dialect_resolution)
        public_dialect_resolution["error"] = None
    raise HTTPException(
        status_code=http_status,
        detail={
            "code": code,
            "judge_status": judge_status,
            "message": public_message,
            "dialect_resolution": public_dialect_resolution,
        },
    )


def _student_sql_safety_error(
    sql: str,
    declared_dialect: str | None,
) -> NativeQuerySafetyError | None:
    """Return an AST-level safety violation, leaving syntax errors to resolver."""
    try:
        validate_native_query_safety(sql, declared_dialect)
    except NativeQuerySafetyError as exc:
        if exc.code == NATIVE_SQL_PARSE_ERROR:
            return None
        # The standalone safety API intentionally rejects every
        # schema/catalog-qualified table because it has no question-specific
        # authoritative catalog.  ``check-sql`` performs that strict
        # namespace check again inside Phase 1, where the standard answer and
        # schema catalog can authorize (and rewrite) a source namespace such as
        # ``cd``.  Defer only this one shape; all side effects, catalog access,
        # external sources and unknown unqualified tables remain blocked here.
        if (
            exc.code == "NATIVE_SQL_UNSAFE_OBJECT"
            and "schema/catalog-qualified table" in str(exc)
        ):
            return None
        return exc
    return None


class SQLRequest(BaseModel):
    sql: str

class SQLCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_sql: str = Field(..., min_length=1, max_length=32768)
    question_id: int
    attempt_id: UUID
    language: Literal["zh-CN", "zh-TW", "en"] = "zh-CN"

class SQLCheckResponse(BaseModel):
    is_correct: bool
    hint: dict
    submission_id: int
    attempt_id: str
    # True only when this HTTP response was restored from a previously
    # committed snapshot. Clients can suppress duplicate UI effects when set.
    idempotency_replayed: bool = False
    error_message: str | None = None
    judge_status: str = "UNKNOWN"
    is_safety_blocked: bool = False
    lambda_t: float | None = None
    # Phase 3 v1 deliberately exposes an auditable decision summary instead
    # of overloading the legacy ``lambda_t`` field.  Skill identifiers and
    # authoritative Q-matrix rows stay server-side.
    phase3_learning: dict | None = None
    # Learner-safe Phase 4/5 delivery metadata.  It deliberately omits rule,
    # skill, candidate, witness, and Q-matrix identities.
    teaching_support: dict | None = None
    observation: dict | None = None
    error_attributions: list[dict] = Field(default_factory=list)
    diagnostic_package: dict | None = None


async def _lock_check_sql_user(session: AsyncSession, user_id: int) -> User:
    """Serialize post-verdict mutations for attempts belonging to one user."""

    locked_user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user no longer exists",
        )
    return locked_user


def _attempt_request_fingerprint(payload: SQLCheckRequest) -> str:
    canonical = json.dumps(
        {
            "student_sql": payload.student_sql,
            "language": payload.language,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _restore_attempt_response(
    submission: Any,
    *,
    request_fingerprint: str,
) -> SQLCheckResponse:
    """Validate and return the exact learner-safe snapshot for a replay."""

    if getattr(submission, "request_fingerprint", None) != request_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ATTEMPT_ID_REUSED",
                "message": "attempt_id was already used for a different request.",
            },
        )
    snapshot = getattr(submission, "response_snapshot", None)
    if not isinstance(snapshot, dict):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ATTEMPT_RESULT_UNAVAILABLE",
                "message": "This attempt exists but has no committed response snapshot.",
            },
        )
    try:
        response = SQLCheckResponse.model_validate(snapshot)
    except Exception as exc:
        logger.error(
            "Invalid persisted SQL-check response snapshot for submission=%s",
            getattr(submission, "id", None),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ATTEMPT_RESULT_INVALID",
                "message": "This attempt's committed response cannot be restored.",
            },
        ) from exc
    if (
        response.submission_id != getattr(submission, "id", None)
        or response.attempt_id != getattr(submission, "attempt_id", None)
        or response.is_correct is not getattr(submission, "is_correct", None)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "ATTEMPT_RESULT_CONFLICT",
                "message": "This attempt's response identity is inconsistent.",
            },
        )
    return response.model_copy(update={"idempotency_replayed": True})


def _localized_question_context(question: Any, language: str) -> dict[str, Any]:
    """Build the student-facing question context without crossing locales."""
    if language == "en":
        title = getattr(question, "title_en", None) or getattr(question, "title", "")
        content = getattr(question, "content_en", None) or getattr(question, "content", "")
    elif language == "zh-TW":
        title = getattr(question, "title_zh_tw", None) or getattr(question, "title", "")
        content = getattr(question, "content_zh_tw", None) or getattr(question, "content", "")
    else:
        title = getattr(question, "title", "")
        content = getattr(question, "content", "")
    return {
        "q": content,
        "title": title,
        "required_output_columns": getattr(question, "required_output_columns", None),
        "language": language,
    }


def _authoritative_phase1_decision(sandbox_run: Any) -> tuple[bool | None, str]:
    """Resolve the rich Phase 1 verdict without trusting legacy booleans.

    ``is_equivalent`` records only the bounded world's observed equality.  It
    must never turn a semantic boundary or known gap into a correctness claim.
    ``None`` means that Phase 1 did not produce a teachable verdict.
    """
    evidence = getattr(sandbox_run, "data_evidence", None) or {}
    conclusion = str(
        getattr(sandbox_run, "equivalence_conclusion", None)
        or evidence.get("equivalence_conclusion")
        or ""
    ).upper()
    verdict_status = str(
        getattr(sandbox_run, "status", None) or evidence.get("status") or ""
    ).upper()
    judge_status = str(
        getattr(sandbox_run, "judge_status", None)
        or evidence.get("judge_status")
        or ""
    ).upper()
    executed = getattr(sandbox_run, "executed", False) is True
    boundary_evidence = (
        getattr(sandbox_run, "boundary_evidence", None)
        or evidence.get("boundary_evidence")
        or {}
    )
    verdict_guard = evidence.get("verdict_guard") or {}
    supported = verdict_status in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}

    if conclusion == "NOT_EQUIVALENT":
        # A student-side execution failure is a supported WRONG outcome even
        # when the comparison run could not mark itself fully executed.
        if is_teachable_wrong(
            status=verdict_status,
            conclusion=conclusion,
            judge_status=judge_status,
        ):
            return False, "WRONG"
        return None, "UNDECIDED"
    if conclusion == "NO_COUNTEREXAMPLE_FOUND":
        if (
            executed
            and supported
            and judge_status == "CORRECT"
            and not boundary_evidence
            and not verdict_guard
        ):
            # Preserve the Phase 1 judge status on the public judge field.  The
            # Phase 2 learner narrative describes the narrower policy meaning:
            # operational acceptance within bounded checks, not a proof.
            return True, "CORRECT"
        return None, "UNDECIDED"
    if conclusion == "UNDECIDED":
        return None, "UNDECIDED"

    # Compatibility for pre-rich-contract test doubles only.  A boundary/gap
    # remains undecided even if an old judge field says CORRECT.
    if verdict_status in {
        "SEMANTIC_BOUNDARY",
        "KNOWN_GAP",
        "ENGINE_GAP",
        "INPUT_GAP",
    }:
        return None, "UNDECIDED"
    if judge_status == "WRONG":
        return False, "WRONG"
    if (
        judge_status in {"CORRECT", "OPERATIONALLY_ACCEPTED"}
        and executed
        and getattr(sandbox_run, "is_equivalent", None) is True
        and not boundary_evidence
        and not verdict_guard
    ):
        return True, judge_status
    return None, "UNDECIDED"


def _raise_undecided_judge(sandbox_run: Any) -> None:
    """Stop before submissions, BKT counters, or chat history are written."""
    evidence = getattr(sandbox_run, "data_evidence", None) or {}
    verdict_status = str(
        getattr(sandbox_run, "status", None) or evidence.get("status") or "UNKNOWN"
    ).upper()
    conclusion = str(
        getattr(sandbox_run, "equivalence_conclusion", None)
        or evidence.get("equivalence_conclusion")
        or "UNDECIDED"
    ).upper()
    logger.info(
        "SQL judge returned no teachable verdict: status=%s conclusion=%s",
        verdict_status,
        conclusion,
    )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "JUDGE_UNDECIDED",
            "judge_status": "UNDECIDED",
            "message": (
                "The bounded SQL judge could not reach a reliable teaching verdict. "
                "No submission or learning-state update was recorded."
            ),
            "phase1_status": verdict_status,
            "equivalence_conclusion": conclusion,
        },
    )


_PUBLIC_DIAGNOSTIC_REQUIRED_KEYS = {
    "schema_version",
    "diagnosis_version",
    "rule_catalog_version",
    "verdict",
    "diagnosis_status",
    "phase1",
    "ordered_diff_pipeline",
    "primary",
    "secondary",
    "secondary_count",
    "suppressed_symptoms",
    "unresolved_count",
    "witness",
    "qss",
    "narrative",
    "boundary_notes",
}
_PUBLIC_DIAGNOSTIC_FORBIDDEN_KEY_FRAGMENTS = {
    "answer_sql",
    "correct_sql",
    "standard_sql",
    "standard_node",
    "standard_fragment",
    "reference_sql",
    "replacement_sql",
    "mutation_sql",
    "test_database",
    "witness_world",
    "raw_observation",
    "error_attributions",
}
_MAX_PUBLIC_DIAGNOSTIC_BYTES = 128 * 1024


def _fallback_phase2_result(
    *,
    is_correct: bool,
    language: str,
) -> tuple[dict[str, Any], str]:
    """Return a deterministic learner-safe package when diagnosis degrades."""
    if is_correct:
        if language == "en":
            feedback = (
                "Supported: no counterexample was found in the current bounded "
                "sandbox checks. This submission is operationally accepted for teaching."
            )
            behavior = "The current bounded checks did not distinguish this submission."
        elif language == "zh-TW":
            feedback = "目前的有界沙盒檢查未發現反例，本次作答獲教學性接受；這不代表已證明全域等價。"
            behavior = "目前的有界檢查未區分出本次作答的結果差異。"
        else:
            feedback = "当前有界沙盒检查未发现反例，本次作答获教学性接受；这不代表已证明全局等价。"
            behavior = "当前有界检查未区分出本次作答的结果差异。"
        package = {
            "schema_version": PHASE2_PUBLIC_SCHEMA_VERSION,
            "diagnosis_version": PHASE2_DIAGNOSIS_VERSION,
            "rule_catalog_version": PHASE2_RULE_CATALOG_VERSION,
            "verdict": "CORRECT",
            "diagnosis_status": "OPERATIONALLY_ACCEPTED",
            "phase1": {
                "status": "SUPPORTED",
                "equivalence_conclusion": "NO_COUNTEREXAMPLE_FOUND",
                "judge_status": "CORRECT",
            },
            "ordered_diff_pipeline": [],
            "primary": None,
            "secondary": [],
            "secondary_count": 0,
            "suppressed_symptoms": [],
            "unresolved_count": 0,
            "witness": None,
            "qss": {},
            "narrative": {
                "student_behavior": behavior,
                "conflict_and_witness": "",
                "guidance_question": "",
            },
            "boundary_notes": ["DIAGNOSIS_LAYER_FALLBACK"],
        }
        return package, feedback

    if language == "en":
        scope_label = "Root query"
        behavior = "The bounded sandbox distinguished this submission from the task behavior."
        conflict = "The diagnosis layer could not safely isolate a primary causal witness."
        guidance = "Can you trace the query from data sources through filters, grouping, projection, and final ordering?"
        feedback = f"{behavior}\n\n{conflict}\n\n{guidance}"
    elif language == "zh-TW":
        scope_label = "主查詢"
        behavior = "有界沙盒已確認本次作答的行為與題目要求存在差異。"
        conflict = "診斷層暫時無法安全定位首要因果物證。"
        guidance = "可以依次檢查資料來源、過濾、分組、投影與最終排序各階段嗎？"
        feedback = f"{behavior}\n\n{conflict}\n\n{guidance}"
    else:
        scope_label = "主查询"
        behavior = "有界沙盒已确认本次作答的行为与题目要求存在差异。"
        conflict = "诊断层暂时无法安全定位首要因果物证。"
        guidance = "可以依次检查数据来源、过滤、分组、投影与最终排序各阶段吗？"
        feedback = f"{behavior}\n\n{conflict}\n\n{guidance}"
    package = {
        "schema_version": PHASE2_PUBLIC_SCHEMA_VERSION,
        "diagnosis_version": PHASE2_DIAGNOSIS_VERSION,
        "rule_catalog_version": PHASE2_RULE_CATALOG_VERSION,
        "verdict": "INCORRECT",
        "diagnosis_status": "DEGRADED",
        "phase1": {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NOT_EQUIVALENT",
            "judge_status": "WRONG",
        },
        "ordered_diff_pipeline": [],
        "primary": {
            "rule_id": "UNCLASSIFIED",
            "stage": "UNMAPPED",
            "scope_label": scope_label,
            "knowledge_points": [],
        },
        "secondary": [],
        "secondary_count": 0,
        "suppressed_symptoms": [],
        "unresolved_count": 0,
        "witness": {"availability": "UNAVAILABLE", "cases": []},
        "qss": {},
        "narrative": {
            "student_behavior": behavior,
            "conflict_and_witness": conflict,
            "guidance_question": guidance,
        },
        "boundary_notes": ["DIAGNOSIS_LAYER_FALLBACK"],
    }
    return package, feedback


def _validated_public_diagnostic_package(
    package: dict[str, Any],
    *,
    correct_sql: str,
    expected_is_correct: bool,
    allowed_public_context: List[str] | None = None,
) -> dict[str, Any]:
    """Validate JSON safety and obvious answer leaks before any DB commit."""
    if not isinstance(package, dict):
        raise ValueError("diagnostic package must be an object")
    missing = _PUBLIC_DIAGNOSTIC_REQUIRED_KEYS - set(package)
    if missing or package.get("schema_version") != PHASE2_PUBLIC_SCHEMA_VERSION:
        raise ValueError("invalid public diagnostic package contract")
    expected_verdict = "CORRECT" if expected_is_correct else "INCORRECT"
    if package.get("verdict") != expected_verdict:
        raise ValueError("diagnostic package conflicts with the Phase 1 verdict")
    phase1 = package.get("phase1")
    expected_phase1 = (
        {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NO_COUNTEREXAMPLE_FOUND",
            "judge_status": "CORRECT",
        }
        if expected_is_correct
        else {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NOT_EQUIVALENT",
            "judge_status": "WRONG",
        }
    )
    if not isinstance(phase1, dict) or any(
        phase1.get(key) != value for key, value in expected_phase1.items()
    ):
        raise ValueError("diagnostic package conflicts with the Phase 1 verdict")

    def walk_keys(value: Any):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield str(key).lower()
                yield from walk_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from walk_keys(nested)

    for key in walk_keys(package):
        if any(fragment in key for fragment in _PUBLIC_DIAGNOSTIC_FORBIDDEN_KEY_FRAGMENTS):
            raise ValueError("internal diagnostic field reached the public package")

    serialized = json.dumps(package, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_PUBLIC_DIAGNOSTIC_BYTES:
        raise ValueError("public diagnostic package exceeds size limit")
    normalized_secret = re.sub(r"\s+", " ", correct_sql).strip().lower()

    # Check decoded string values rather than only JSON text: quoted SQL is
    # escaped during JSON encoding and could otherwise bypass a substring gate.
    public_strings: list[str] = []

    def collect_strings(value: Any) -> None:
        if isinstance(value, str):
            public_strings.append(re.sub(r"\s+", " ", value).strip().lower())
        elif isinstance(value, dict):
            for nested in value.values():
                collect_strings(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_strings(nested)

    collect_strings(package)
    secret_fragments = {normalized_secret} if normalized_secret else set()
    normalized_sql = normalized_secret.rstrip(";")
    clause_pattern = re.compile(
        r"\b(select|where|group\s+by|having|order\s+by|limit|offset)\b"
        r"\s+(.+?)(?=\bfrom\b|\bwhere\b|\bgroup\s+by\b|\bhaving\b|"
        r"\border\s+by\b|\blimit\b|\boffset\b|\bunion\b|$)",
        flags=re.IGNORECASE,
    )
    for match in clause_pattern.finditer(normalized_sql):
        fragment = re.sub(r"\s+", " ", match.group(2)).strip(" ;")
        if len(fragment) >= 6:
            secret_fragments.add(fragment)
    normalized_allowed = " ".join(
        re.sub(r"\s+", " ", str(value)).strip().lower()
        for value in (allowed_public_context or [])
    )
    if any(
        secret
        and secret not in normalized_allowed
        and secret in public_value
        for secret in secret_fragments
        for public_value in public_strings
    ):
        raise ValueError("reference SQL or a reference-only fragment reached the public package")
    return json.loads(serialized)


def _validated_learner_feedback(
    feedback: str,
    *,
    correct_sql: str,
    allowed_public_context: List[str] | None = None,
) -> str:
    text = str(feedback or "").strip()
    if not text or len(text.encode("utf-8")) > 16 * 1024:
        raise ValueError("invalid learner feedback")
    normalized_text = re.sub(r"\s+", " ", text).strip().lower()
    normalized_secret = re.sub(r"\s+", " ", correct_sql).strip().lower()
    normalized_sql = normalized_secret.rstrip(";")
    secret_fragments = {normalized_secret} if normalized_secret else set()
    clause_pattern = re.compile(
        r"\b(select|where|group\s+by|having|order\s+by|limit|offset)\b"
        r"\s+(.+?)(?=\bfrom\b|\bwhere\b|\bgroup\s+by\b|\bhaving\b|"
        r"\border\s+by\b|\blimit\b|\boffset\b|\bunion\b|$)",
        flags=re.IGNORECASE,
    )
    for match in clause_pattern.finditer(normalized_sql):
        fragment = re.sub(r"\s+", " ", match.group(2)).strip(" ;")
        if len(fragment) >= 6:
            secret_fragments.add(fragment)
    normalized_allowed = " ".join(
        re.sub(r"\s+", " ", str(value)).strip().lower()
        for value in (allowed_public_context or [])
    )
    if any(
        secret
        and secret not in normalized_allowed
        and secret in normalized_text
        for secret in secret_fragments
    ):
        raise ValueError("reference SQL reached learner feedback")
    return text


def _phase45_degraded_feedback(*, is_correct: bool, language: str) -> str:
    """Answer-free local fallback when adaptive action rendering fails."""

    if is_correct:
        if language == "en":
            return "The bounded checks found no counterexample, so this submission is accepted for this exercise."
        if language == "zh-TW":
            return "目前的有界檢查未發現反例，本次作答已獲教學性接受。"
        return "当前的有界检查未发现反例，本次作答已获教学性接受。"
    if language == "en":
        return (
            "The bounded checks found a behavioral mismatch, but the adaptive "
            "explanation is temporarily unavailable. Trace the query in logical "
            "execution order and identify the earliest step that changes the result."
        )
    if language == "zh-TW":
        return (
            "有界檢查已發現行為不一致，但個人化解釋暫時不可用。請按照邏輯執行順序逐步檢查，"
            "找出最早改變結果的階段。"
        )
    return (
        "有界检查已发现行为不一致，但个性化解释暂时不可用。请按照逻辑执行顺序逐步检查，"
        "找出最早改变结果的阶段。"
    )


async def _persist_teaching_delivery_audit(
    session: AsyncSession,
    *,
    submission_id: int,
    teaching_plan: Any,
    feedback_artifact: Any,
    phase2_llm_review: Mapping[str, Any] | None = None,
) -> None:
    """Bind Phase 2--5 decisions and delivery atomically.

    The optional Phase 2 model trace is bounded metadata only.  It is stored in
    the private JSON audit snapshot and is never copied into the learner
    response.
    """

    from core.student_feedback import STUDENT_FEEDBACK_POLICY_VERSION
    from core.teaching_action import TEACHING_ACTION_POLICY_VERSION
    from repository.submission_teaching_audit_repo import (
        SubmissionTeachingAuditInput,
        SubmissionTeachingAuditRepository,
    )

    if teaching_plan.support_recommendation_applied:
        recommendation_status = "APPLIED"
    elif teaching_plan.recommended_support_level is not None:
        recommendation_status = "OVERRIDDEN"
    else:
        recommendation_status = "NOT_APPLICABLE"
    action_snapshot = teaching_plan.to_audit_dict()
    if phase2_llm_review is not None:
        try:
            encoded_review = json.dumps(
                dict(phase2_llm_review),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("invalid Phase 2 LLM review audit metadata")
        if len(encoded_review) > 16 * 1024:
            raise ValueError("Phase 2 LLM review audit metadata is too large")
        action_snapshot["phase2_llm_review"] = json.loads(
            encoded_review.decode("utf-8")
        )
    audit = SubmissionTeachingAuditInput(
        recommendation_status=recommendation_status,
        support_need=teaching_plan.support_need,
        recommended_support_level=teaching_plan.recommended_support_level,
        delivered_support_level=teaching_plan.delivered_support_level,
        support_recommendation_applied=(
            teaching_plan.support_recommendation_applied
        ),
        support_policy_version=teaching_plan.support_policy_version,
        action_policy_version=TEACHING_ACTION_POLICY_VERSION,
        feedback_policy_version=STUDENT_FEEDBACK_POLICY_VERSION,
        generation_source=feedback_artifact.feedback_source,
        feedback_status=feedback_artifact.feedback_status,
        degradation_code=feedback_artifact.degradation_code,
        answer_revealed=feedback_artifact.answer_revealed,
        feedback_sha256=feedback_artifact.content_digest,
        action_snapshot=action_snapshot,
        target_candidate_id=teaching_plan.target_candidate_id,
        target_rule_id=teaching_plan.target_rule_id,
        target_observation_id=teaching_plan.target_observation_id,
        target_skill_id=teaching_plan.target_skill_id,
        target_taxonomy_version=teaching_plan.target_taxonomy_version,
        target_logical_stage=teaching_plan.target_logical_stage,
        target_source_role=teaching_plan.target_source_role,
        target_evidence_grade=teaching_plan.target_evidence_grade,
    )
    await SubmissionTeachingAuditRepository(session).create_once_or_validate(
        submission_id,
        audit,
    )


def _generate_local_feedback(
    is_correct: bool,
    is_safety_blocked: bool,
    error_message: str | None,
    attributions: List[KPAttribution],
    language: str = "zh-CN"
) -> str:
    if is_safety_blocked:
        if language == "en":
            return f"Security Blocked: Direct database modification operations (e.g. DROP, DELETE, INSERT, UPDATE) are prohibited.\nDetails: {error_message or 'Only SELECT queries are allowed.'}"
        elif language == "zh-TW":
            return f"安全攔截：禁止直接執行 DROP/DELETE/INSERT/UPDATE 等修改或刪除數據庫操作。\n詳情：{error_message or '練習環境僅允許 SELECT 查詢。'}"
        else:
            return f"安全拦截：禁止直接执行 DROP/DELETE/INSERT/UPDATE 等修改或删除数据库操作。\n详情：{error_message or '练习环境仅允许 SELECT 查询。'}"

    if is_correct:
        if language == "en":
            return "Supported: no counterexample was found in the current bounded sandbox checks. This submission is operationally accepted for teaching."
        elif language == "zh-TW":
            return "恭喜！目前的有界沙盒檢查未發現反例，本次作答獲教學性接受；這不代表已證明全域等價。"
        else:
            return "恭喜！当前有界沙盒检查未发现反例，本次作答获教学性接受；这不代表已证明全局等价。"

    if not attributions:
        if error_message:
            if language == "en":
                return f"Incorrect. Sandbox execution returned an error:\n\n```\n{error_message}\n```\n\nPlease check your query syntax and logic."
            elif language == "zh-TW":
                return f"作答不正確。執行沙盒返回了錯誤信息：\n\n```\n{error_message}\n```\n\n請仔細檢查你的 SQL 語法與邏輯。"
            else:
                return f"作答不正确。执行沙盒返回了错误信息：\n\n```\n{error_message}\n```\n\n请仔细检查你的 SQL 语法与逻辑。"
        
        if language == "en":
            return "Incorrect. The execution output does not match the standard solution. Please check your SELECT projection, JOIN conditions, or WHERE filters."
        elif language == "zh-TW":
            return "作答不正確。執行結果與標準答案不匹配。請重新檢查 SELECT 投影項、JOIN 條件或 WHERE 過濾子句的邏輯是否完全正確。"
        else:
            return "作答不正确。执行结果与标准答案不匹配。请重新检查 SELECT 投影项、JOIN 条件或 WHERE 过滤子句的逻辑是否完全正确。"

    # 针对不正确且有归因的情况
    if language == "en":
        lines = ["I analyzed your submission and found the following issues:"]
        for attr in attributions:
            lines.append(f"- **{attr.clause}** aspect: {attr.detail}")
        lines.append("\nPlease revise your query based on these points and try again!")
    elif language == "zh-TW":
        lines = ["分析了你的提交後，發現以下幾個需要注意的問題："]
        for attr in attributions:
            lines.append(f"- **{attr.clause}** 方面：{attr.detail}")
        lines.append("\n請根據上述提示修改你的 SQL 後重新提交。")
    else:
        lines = ["分析了你的提交后，发现以下几个需要注意的问题："]
        for attr in attributions:
            lines.append(f"- **{attr.clause}** 方面：{attr.detail}")
        lines.append("\n请根据上述提示修改你的 SQL 后重新提交。")

    return "\n".join(lines)


def _parseval_schema_columns(columns: list[Any]) -> list[str]:
    result: list[str] = []
    for column in columns:
        if isinstance(column, str):
            result.append(column)
            continue
        if isinstance(column, dict):
            name = str(column.get("name") or "").strip()
            if not name:
                continue
            type_hint = str(
                column.get("data_type") or column.get("type") or ""
            ).strip()
            nullable = column.get("nullable")
            suffix_parts: list[str] = []
            if type_hint:
                suffix_parts.append(type_hint)
            if nullable is False:
                suffix_parts.append("NOT NULL")
            result.append(" ".join([name, *suffix_parts]))
    return result


@router.post("/sql-hint")
async def sql_hint(payload: SQLRequest):
    """获取 SQL 提示（已下线，仅返回静态内容）。"""
    return {"hint": {
        "diagnoses": [],
        "overall_comment": "AI 提示服务已下线，练习界面现在采用本地分析引擎生成更精准的实时诊断提示。"
    }}


@router.get("/mastery-radar")
async def get_mastery_radar(
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """Return raw Phase 3 BKT state without feeding display smoothing back."""
    from core.phase3_calibration import load_active_bkt_policy
    from core.phase3_skill_catalog import (
        ATOMIC_SKILL_TAXONOMY_VERSION,
        RULE_SKILL_CATALOG,
    )
    from core.sql_knowledge_points import SQL_KNOWLEDGE_POINTS
    from models.question_skill import SQL_KNOWLEDGE_TAXONOMY_VERSION
    from repository.phase3_learning_repo import Phase3LearningRepository

    bkt_policy = load_active_bkt_policy(
        getattr(settings, "PHASE3_BKT_CALIBRATION_ARTIFACT", ""),
        source_path=(
            getattr(settings, "PHASE3_BKT_CALIBRATION_SOURCE", "") or None
        ),
    )
    bkt_parameters = bkt_policy.parameters
    states = await Phase3LearningRepository(session).list_states(user_id)
    broad_state = {
        str(item["id"]): bkt_parameters.initial_mastery
        for item in SQL_KNOWLEDGE_POINTS
    }
    atomic_state = {
        item.skill_id: bkt_parameters.initial_mastery
        for item in RULE_SKILL_CATALOG
    }
    details: list[dict[str, Any]] = []
    for state in states:
        if state.taxonomy_version == SQL_KNOWLEDGE_TAXONOMY_VERSION:
            broad_state[state.skill_id] = state.posterior_mastery
        elif state.taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION:
            atomic_state[state.skill_id] = state.posterior_mastery
        details.append(
            {
                "taxonomy_version": state.taxonomy_version,
                "skill_id": state.skill_id,
                "posterior_mastery": state.posterior_mastery,
                "next_prior": state.next_prior,
                "observation_count": state.observation_count,
                "bkt_parameter_version": state.bkt_parameter_version,
                "state_version": state.state_version,
            }
        )
    return {
        "schema_version": "phase3.mastery_profile.v1",
        # Legacy key retained for broad-course radar clients.  Unobserved
        # values now use the explicit BKT P(L0), not an arbitrary static 0.5.
        "mastery_state": broad_state,
        "atomic_mastery_state": atomic_state,
        "state_details": details,
        "display_value": "RAW_BKT_POSTERIOR",
        "unobserved_prior": bkt_parameters.initial_mastery,
        "bkt_parameter_version": bkt_parameters.version,
        "bkt_calibration_status": bkt_policy.calibration_status,
        "bkt_calibration_artifact_digest": bkt_policy.artifact_digest_sha256,
        "calibration_status": "UNCALIBRATED_MVP",
    }


@router.post("/check-sql", response_model=SQLCheckResponse)
async def check_sql(
    payload: SQLCheckRequest,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """Coalesce one submit action, then execute the authoritative judge flow."""

    attempt_id = str(payload.attempt_id)
    async with _serialize_attempt_in_process(
        user_id,
        payload.question_id,
        attempt_id,
    ):
        return await _check_sql_impl(
            payload=payload,
            user_id=user_id,
            session=session,
        )


async def _check_sql_impl(
    *,
    payload: SQLCheckRequest,
    user_id: int,
    session: AsyncSession,
) -> SQLCheckResponse:
    """检查学生提交的 SQL 是否正确，并生成本地诊断提示。"""
    # 1. 查询题目
    question_repo = QuestionRepository(session)
    question = await question_repo.get_by_id(payload.question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {payload.question_id} 不存在",
        )
    question_context = _localized_question_context(question, payload.language)
    attempt_id = str(payload.attempt_id)
    request_fingerprint = _attempt_request_fingerprint(payload)
    submission_repo = SubmissionRepository(session)
    replay = await submission_repo.get_by_attempt_id(
        user_id,
        payload.question_id,
        attempt_id,
    )
    if replay is not None:
        return _restore_attempt_response(
            replay,
            request_fingerprint=request_fingerprint,
        )

    # 1.1 安全性与语法检查 (T_SAFE & T_SYNTAX)
    safety_error = _student_sql_safety_error(
        payload.student_sql,
        getattr(question, "sql_dialect", None),
    )
    keyword = None
    is_safety_blocked = safety_error is not None

    is_syntax_error = False
    syntax_error_msg = ""
    dialect_resolution = None
    if not is_safety_blocked:
        try:
            dialect_resolution = resolve_sql_dialect_or_raise(
                declared_dialect=getattr(question, "sql_dialect", None),
                standard_sql=question.correct_sql,
                student_sql=payload.student_sql,
                default_dialect=settings.PARSEVAL_DEFAULT_DIALECT,
            )
            _validate_native_engine_version(
                dialect_resolution.resolved_dialect,
                getattr(question, "engine_version", None),
            )
        except DialectResolutionError as exc:
            if exc.code == "STUDENT_SQL_PARSE_ERROR":
                is_syntax_error = True
                syntax_error_msg = str(exc)
            else:
                _raise_platform_judge_error(
                    judge_status=(
                        "UNSUPPORTED"
                        if exc.code
                        in {
                            "DIALECT_CONFLICT",
                            "UNSUPPORTED_DIALECT",
                            "UNSUPPORTED_DIALECT_FEATURE",
                        }
                        else "ENGINE_ERROR"
                    ),
                    error_message=str(exc),
                    error_code=exc.code,
                    dialect_resolution=(
                        exc.resolution.to_dict() if exc.resolution is not None else None
                    ),
                )

    # 如果是语法错误，直接调用语法纠错提示并结束 (O_SYNTAX_EXIT)
    if is_syntax_error:
        if payload.language == "en":
            ai_hint_text = f"Your SQL has syntax errors. Parser reported:\n\n```\n{syntax_error_msg}\n```\n\nPlease check keywords, parentheses, or commas."
        elif payload.language == "zh-TW":
            ai_hint_text = f"你的 SQL 語法書寫不正確，解析報錯：\n\n```\n{syntax_error_msg}\n```\n\n請檢查拼寫、括號匹配或逗號等基礎語法。"
        else:
            ai_hint_text = f"你的 SQL 语法书写不正确，解析报错：\n\n```\n{syntax_error_msg}\n```\n\n请检查拼写、括号匹配或逗号等基础语法。"

        from core.student_feedback import (
            build_teaching_support_summary,
            render_student_feedback,
        )
        from core.teaching_action import build_fixed_teaching_action

        teaching_plan = build_fixed_teaching_action(
            ai_hint_text,
            language=payload.language,
            status="SYNTAX_FEEDBACK",
        )
        feedback_artifact = render_student_feedback(teaching_plan)
        ai_hint_text = _validated_learner_feedback(
            feedback_artifact.text,
            correct_sql=question.correct_sql,
            allowed_public_context=[
                question_context.get("q") or "",
                question_context.get("title") or "",
                payload.student_sql,
                syntax_error_msg,
            ],
        )
        teaching_support = build_teaching_support_summary(
            teaching_plan,
            feedback_artifact,
        )

        # Serialize the side effects and re-check with a MySQL current read in
        # case an identical transport retry completed while parsing ran.
        await _lock_check_sql_user(session, user_id)
        replay = await submission_repo.get_by_attempt_id(
            user_id,
            payload.question_id,
            attempt_id,
            for_update=True,
        )
        if replay is not None:
            return _restore_attempt_response(
                replay,
                request_fingerprint=request_fingerprint,
            )

        # 保存提交记录与对话历史
        submission_data = SubmissionCreate(
            user_id=user_id,
            question_id=payload.question_id,
            attempt_id=attempt_id,
            request_fingerprint=request_fingerprint,
            student_sql=payload.student_sql,
            ai_hint=ai_hint_text,
            is_correct=False,
            hint_level=teaching_plan.delivered_support_level,
        )
        submission = await submission_repo.create(submission_data)
        await _persist_teaching_delivery_audit(
            session,
            submission_id=submission.id,
            teaching_plan=teaching_plan,
            feedback_artifact=feedback_artifact,
        )

        # Syntax is a separately audited behavioral event.  It is visible to
        # the proxy's syntax counter, but it is deliberately not a
        # SkillObservationEvent and can never update BKT.
        try:
            from core.phase3_runtime import (
                Phase3LearningSummary,
                summarize_skill_history,
            )
            from models.phase3_learning import Phase3BehaviorEventKind
            from repository.phase3_behavior_repo import Phase3BehaviorEventRepository

            behavior_repo = Phase3BehaviorEventRepository(session)
            async with session.begin_nested():
                await behavior_repo.record_once(
                    submission_id=submission.id,
                    user_id=user_id,
                    question_id=payload.question_id,
                    event_kind=Phase3BehaviorEventKind.SYNTAX_ERROR,
                )
            behavior_history = await behavior_repo.list_recent_events(
                user_id,
                limit=10,
            )
            history = summarize_skill_history(behavior_history)
            phase3_summary = Phase3LearningSummary(
                status="SKIP_SYNTAX_ERROR",
                observation_count=0,
                state_update_count=0,
                priority_score=None,
                support_need=None,
                recommended_support_level=None,
                challenge_readiness=None,
                behavioral_support_need=history.behavioral_support_need,
                behavioral_session_reset=history.session_reset,
                semantic_failure_count=history.semantic_failure_count,
                syntax_error_count=history.syntax_error_count,
            )
        except Exception:
            logger.exception(
                "Phase 3 syntax behavior audit failed; preserving the syntax verdict"
            )
            from core.phase3_runtime import degraded_learning_summary

            phase3_summary = degraded_learning_summary()

        chat_repo = ChatRepository(session)
        await chat_repo.add_message(
            user_id=user_id,
            question_id=payload.question_id,
            role="system",
            content="【新一轮提交】结果：不正确 (语法错误)",
        )
        await chat_repo.add_message(
            user_id=user_id,
            question_id=payload.question_id,
            role="user",
            content=f"我提交的 SQL：\n\n```sql\n{payload.student_sql}\n```",
        )
        await chat_repo.add_message(
            user_id=user_id,
            question_id=payload.question_id,
            role="assistant",
            content=ai_hint_text,
        )
        ai_hint_result = SQLCheckResultSchema(
            diagnoses=[],
            overall_comment=ai_hint_text
        )

        response = SQLCheckResponse(
            is_correct=False,
            hint=ai_hint_result.model_dump(),
            submission_id=submission.id,
            attempt_id=attempt_id,
            error_message=f"SQL 语法错误: {syntax_error_msg}",
            judge_status="WRONG",
            is_safety_blocked=False,
            lambda_t=None,
            phase3_learning=(
                phase3_summary.to_public_dict(
                    support_recommendation_applied=False,
                    delivered_support_level=teaching_plan.delivered_support_level,
                )
                if phase3_summary is not None
                else None
            ),
            teaching_support=teaching_support,
            observation=None,
            error_attributions=[],
            diagnostic_package=None,
        )
        submission.response_snapshot = response.model_dump(mode="json")
        await session.commit()
        return response

    # 2. ParSEval 造数 + 变异（唯一判题来源）；Phase 2 仅负责解释与归因
    is_correct = False
    error_message = None
    judge_detail = None
    judge_status = "WRONG"
    platform_judge_failure = False
    observation = None
    error_attributions: list[dict] = []
    attribution_result = None
    diagnostic_package: dict | None = None
    phase2_llm_review: dict[str, Any] | None = None
    phase2_feedback: str | None = None
    schema_json: dict[str, Any] = {}
    learner_feedback_context: list[str] = [
        question_context.get("q") or "",
        question_context.get("title") or "",
        question_context.get("required_output_columns") or "",
        payload.student_sql,
    ]

    if is_safety_blocked:
        error_message = f"SQL 安全拦截：{safety_error}"

    # ------------------------------------------------------------
    # Phase 1: ParSEval 造数验证 + 证据采集与归因 (Observe/感知)
    # ------------------------------------------------------------
    if not is_safety_blocked:
        # Convert schema JSON to parseval compact schema string
        schema_preview_str = getattr(question, "schema_preview", None)
        parseval_schema = ""
        schema_json = (
            sanitize_schema_preview_object(
                schema_preview_str,
                forbidden_sql=getattr(question, "correct_sql", "") or "",
            )
            or {}
        )
        if schema_preview_str:
            try:
                tables = schema_json.get("tables", [])
                parseval_schema = "; ".join(
                    f"{tbl.get('name')}({', '.join(_parseval_schema_columns(tbl.get('columns', [])))})"
                    for tbl in tables if tbl.get('name') and tbl.get('columns')
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to convert schema_preview to parseval format: {e}"
                )

        mutation_detail = None
        ast_diffs_detail: list[dict] = []
        is_equivalent: bool | None = None
        sandbox_run = None
        if parseval_schema:
            try:
                from core.parseval_data_generator import generate_and_compare
                assert dialect_resolution is not None
                # Preserve the structured, sanitized catalog when the
                # question supplies one.  Legacy previews containing only
                # column-name strings still use the compact schema fallback;
                # they must not be treated as an authoritative typed catalog.
                phase1_schema_catalog = None
                catalog_tables = schema_json.get("tables") if isinstance(schema_json, dict) else None
                if isinstance(catalog_tables, list) and catalog_tables and all(
                    isinstance(table, dict)
                    and isinstance(table.get("columns"), list)
                    and all(isinstance(column, dict) for column in table.get("columns", []))
                    for table in catalog_tables
                ):
                    phase1_schema_catalog = schema_json
                sandbox_run = await _run_phase1_bounded(
                    generate_and_compare,
                    schema_text=parseval_schema,
                    standard_sql=question.correct_sql,
                    student_sql=payload.student_sql,
                    sql_dialect=getattr(question, "sql_dialect", None),
                    default_sql_dialect=settings.PARSEVAL_DEFAULT_DIALECT,
                    dialect_resolution=dialect_resolution,
                    execution_backend=settings.PARSEVAL_EXECUTION_BACKEND,
                    native_executor_url=_native_executor_url_for_dialect(
                        dialect_resolution.resolved_dialect
                    ),
                    schema_catalog=phase1_schema_catalog,
                )
                if sandbox_run and sandbox_run.executed:
                    is_equivalent = sandbox_run.is_equivalent
                    authoritative_is_correct, judge_status = _authoritative_phase1_decision(
                        sandbox_run
                    )
                    is_correct = authoritative_is_correct is True
                    std_count = len(sandbox_run.standard_rows)
                    stu_count = len(sandbox_run.student_rows)
                    if not is_correct:
                        if std_count != stu_count:
                            error_message = f"结果数据不匹配，行数不一致（标准 {std_count} 行，学生 {stu_count} 行）"
                        else:
                            error_message = "结果数据不匹配"
                    judge_detail = {
                        "is_correct": is_correct,
                        "judge_status": judge_status,
                        "phase1_status": getattr(sandbox_run, "status", None),
                        "equivalence_conclusion": getattr(
                            sandbox_run, "equivalence_conclusion", None
                        ),
                        "error_message": error_message,
                        "dialect_resolution": dialect_resolution.to_dict(),
                        "student_result_meta": {
                            "row_count": stu_count,
                            "columns": sandbox_run.student_columns,
                        },
                        "correct_result_meta": {
                            "row_count": std_count,
                            "columns": sandbox_run.standard_columns,
                        },
                        "comparison": {
                            "is_equivalent_on_generated_data": is_equivalent,
                            "row_count_match": std_count == stu_count,
                            "standard_row_count": std_count,
                            "student_row_count": stu_count,
                            "columns_match": len(sandbox_run.standard_columns) == len(sandbox_run.student_columns),
                            "column_names_match": sandbox_run.standard_columns == sandbox_run.student_columns,
                        },
                    }
                    mutation_detail = sandbox_run.mutation_evidence
                    ast_diffs_detail = [d.to_dict() for d in sandbox_run.ast_diffs]
                elif sandbox_run:
                    error_message = sandbox_run.error or "SQL 语法解析失败"
                    judge_status = (
                        getattr(sandbox_run, "judge_status", None)
                        or (sandbox_run.data_evidence or {}).get("judge_status")
                        or "ENGINE_ERROR"
                    )
                    platform_judge_failure = judge_status in {
                        "UNSUPPORTED",
                        "SECURITY_REJECTED",
                        "ENGINE_ERROR",
                        "TIMEOUT",
                    }
                    judge_detail = {
                        "is_correct": None if platform_judge_failure else False,
                        "judge_status": judge_status,
                        "error_message": error_message,
                        "dialect_resolution": dialect_resolution.to_dict(),
                        "comparison": {
                            "sandbox_executed": False,
                            "sandbox_error": error_message,
                            "unsupported_features": (sandbox_run.data_evidence or {}).get("unsupported_features", []),
                        },
                    }
                else:
                    error_message = "ParSEval verification returned no judge result"
                    judge_status = "ENGINE_ERROR"
                    platform_judge_failure = True
                    judge_detail = {
                        "is_correct": None,
                        "judge_status": judge_status,
                        "error_message": error_message,
                        "dialect_resolution": dialect_resolution.to_dict(),
                        "comparison": {
                            "sandbox_executed": False,
                            "sandbox_error": error_message,
                        },
                    }
            except TimeoutError as e:
                logger.warning("ParSEval verification timed out: %s", e)
                error_message = str(e)
                judge_status = "TIMEOUT"
                platform_judge_failure = True
                judge_detail = {
                    "is_correct": None,
                    "judge_status": judge_status,
                    "error_message": error_message,
                    "comparison": {
                        "sandbox_executed": False,
                        "sandbox_error": error_message,
                    },
                }
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"ParSEval verification failed: {e}", exc_info=True)
                error_message = f"ParSEval verification failed: {e}"
                judge_status = "ENGINE_ERROR"
                platform_judge_failure = True
                judge_detail = {
                    "is_correct": None,
                    "judge_status": judge_status,
                    "error_message": error_message,
                    "comparison": {"sandbox_executed": False, "sandbox_error": error_message},
                }
        else:
            error_message = "题目缺少可执行的 schema_preview，无法进行造数判题"
            judge_status = "ENGINE_ERROR"
            platform_judge_failure = True
            judge_detail = {
                "is_correct": None,
                "judge_status": judge_status,
                "error_message": error_message,
                "comparison": {"sandbox_executed": False, "sandbox_error": error_message},
            }

        if platform_judge_failure:
            _raise_platform_judge_error(
                judge_status=judge_status,
                error_message=error_message,
                error_code=(
                    getattr(sandbox_run, "error_code", None)
                    if sandbox_run is not None
                    else None
                ) or (
                    (sandbox_run.data_evidence or {}).get("error_code")
                    if sandbox_run is not None
                    else None
                ),
                dialect_resolution=(
                    dialect_resolution.to_dict() if dialect_resolution is not None else None
                ),
            )
        else:
            if sandbox_run is None:
                _raise_platform_judge_error(
                    judge_status="ENGINE_ERROR",
                    error_message="ParSEval verification returned no judge result",
                    dialect_resolution=(
                        dialect_resolution.to_dict()
                        if dialect_resolution is not None
                        else None
                    ),
                )
            authoritative_is_correct, judge_status = _authoritative_phase1_decision(
                sandbox_run
            )
            if authoritative_is_correct is None:
                _raise_undecided_judge(sandbox_run)
            is_correct = authoritative_is_correct
            if isinstance(judge_detail, dict):
                judge_detail["is_correct"] = is_correct
                judge_detail["judge_status"] = judge_status
                judge_detail["phase1_status"] = getattr(sandbox_run, "status", None)
                judge_detail["equivalence_conclusion"] = getattr(
                    sandbox_run, "equivalence_conclusion", None
                )

            # A supported correct verdict takes the strict 2.1 fast path: do
            # not run fault attribution.  For WRONG, attribution remains
            # explanatory and is independently degradable.
            attributions_list = []
            if not is_correct:
                try:
                    attribution_result = evidence_weights_from_observation(
                        student_sql=payload.student_sql,
                        answer_sql=question.correct_sql,
                        is_correct=False,
                        error_message=error_message,
                        judge_detail=judge_detail,
                        question_context=question_context,
                        mutation_detail=mutation_detail,
                        ast_diffs=ast_diffs_detail,
                        sql_dialect=(
                            dialect_resolution.parse_dialect
                            or GENERIC_SQLGLOT_DIALECT
                            if dialect_resolution is not None
                            else None
                        ),
                        dialect_resolution=(
                            dialect_resolution.to_dict()
                            if dialect_resolution is not None
                            else None
                        ),
                    )
                    observation = attribution_result.observation
                    if isinstance(observation, dict):
                        observation["judge_status"] = judge_status
                        observation["phase1_verdict"] = {
                            "status": getattr(sandbox_run, "status", None),
                            "equivalence_conclusion": getattr(
                                sandbox_run, "equivalence_conclusion", None
                            ),
                            "bounded_world_is_equivalent": is_equivalent,
                        }
                    error_attributions = [
                        item.to_dict() for item in attribution_result.attributions
                    ]
                    attributions_list = attribution_result.attributions
                except Exception:
                    attribution_result = None
                    logger.exception(
                        "Phase 2 attribution failed; continuing from Phase 1 evidence"
                    )

            # Phase 2 consumes the in-memory Phase 1 objects directly so stable
            # evidence IDs and selected witnesses survive the route boundary.
            try:
                from core.error_diagnosis import (
                    diagnose_record,
                    render_diagnostic_feedback,
                )
                from core.llm_teaching import (
                    arbitrate_phase2_evidence,
                    merge_phase2_llm_assessment,
                )

                package = diagnose_record(
                    sandbox_run=sandbox_run,
                    attribution_result=attribution_result,
                    question=question_context["q"],
                    schema=schema_json,
                    student_sql=payload.student_sql,
                    language=payload.language,
                )
                # The model is an evidence reviewer, not a second judge.  It
                # may only reorder already blocking candidates and polish the
                # narrative; a provider failure leaves the deterministic
                # Phase 2 package untouched.
                package_public = package.to_dict()
                llm_assessment = None
                try:
                    llm_assessment = await arbitrate_phase2_evidence(
                        package=package,
                        sandbox_run=sandbox_run,
                        attribution_result=attribution_result,
                        question=question_context["q"],
                        schema=schema_json,
                        standard_sql=question.correct_sql,
                        student_sql=payload.student_sql,
                        language=payload.language,
                    )
                    package_public = merge_phase2_llm_assessment(
                        package_public,
                        llm_assessment,
                    )
                except Exception:
                    # LLM is deliberately downstream of the authoritative
                    # evidence path.  Never degrade a valid deterministic
                    # diagnosis merely because the optional reviewer failed.
                    logger.exception(
                        "Phase 2 LLM review failed; using deterministic diagnosis"
                    )
                public_diagnostic_context = [
                    question_context.get("q") or "",
                    question_context.get("title") or "",
                    question_context.get("required_output_columns") or "",
                    payload.student_sql,
                    json.dumps(schema_json, ensure_ascii=False, default=str),
                ]
                learner_feedback_context = public_diagnostic_context
                diagnostic_package = _validated_public_diagnostic_package(
                    package_public,
                    correct_sql=question.correct_sql,
                    expected_is_correct=is_correct,
                    allowed_public_context=public_diagnostic_context,
                )
                if llm_assessment is not None:
                    phase2_llm_review = llm_assessment.to_internal_dict()
                phase2_feedback = _validated_learner_feedback(
                    render_diagnostic_feedback(
                        package_public,
                        language=payload.language,
                    ),
                    correct_sql=question.correct_sql,
                    allowed_public_context=public_diagnostic_context,
                )
            except Exception:
                logger.exception(
                    "Phase 2 diagnosis failed; preserving the authoritative Phase 1 verdict"
                )
                phase2_llm_review = None
                diagnostic_package, phase2_feedback = _fallback_phase2_result(
                    is_correct=is_correct,
                    language=payload.language,
                )
    else:
        attributions_list = []
        judge_status = "WRONG"

    # Deterministic baseline retained only for safety/degradation.  The normal
    # learner response is selected after Phase 3 so its actual depth can match
    # the current support recommendation.
    baseline_feedback = phase2_feedback or _generate_local_feedback(
        is_correct=is_correct,
        is_safety_blocked=is_safety_blocked,
        error_message=error_message,
        attributions=attributions_list,
        language=payload.language,
    )

    # Phase 3 selects exactly the trusted atomic target chosen by the validated
    # Phase 2 package.  Build a read-only provisional plan before the optional
    # Phase 5 network call so no external request runs while a user row is
    # locked.  A current, locked plan is recomputed immediately before writes.
    from core.phase3_runtime import degraded_learning_summary, prepare_phase3_attempt
    from core.phase3_calibration import load_active_bkt_policy
    from core.student_feedback import (
        build_teaching_support_summary,
        render_emergency_feedback,
        render_llm_student_feedback,
        render_student_feedback,
    )
    from core.llm_teaching import generate_phase5_feedback
    from core.teaching_action import (
        build_fixed_teaching_action,
        degrade_teaching_action,
        select_teaching_actions,
    )

    active_bkt_policy = None
    if not is_safety_blocked and diagnostic_package is not None:
        try:
            active_bkt_policy = load_active_bkt_policy(
                getattr(settings, "PHASE3_BKT_CALIBRATION_ARTIFACT", ""),
                source_path=(
                    getattr(settings, "PHASE3_BKT_CALIBRATION_SOURCE", "")
                    or None
                ),
            )
        except Exception:
            logger.exception("Phase 3 policy loading failed; preserving verdict")

    async def _prepare_phase3(*, locked: bool) -> tuple[Any, Any]:
        if is_safety_blocked or diagnostic_package is None:
            return None, None
        try:
            plan = await prepare_phase3_attempt(
                session,
                user_id=user_id,
                question_id=payload.question_id,
                expected_is_correct=is_correct,
                diagnostic_package=diagnostic_package,
                answer_revealed=False,
                bkt_policy=active_bkt_policy,
                lock_for_update=locked,
            )
            return plan, plan.no_update_summary()
        except Exception:
            # Phase 3 is downstream of the verdict.  Its failure must never
            # change Phase 1 correctness or create an untrusted target.
            logger.exception(
                "Phase 3 planning failed; preserving verdict without a learning update"
            )
            return None, degraded_learning_summary()

    def _build_delivery(plan: Any) -> tuple[Any, Any]:
        """Build Phase 4 plus deterministic Phase 5 output for one plan."""

        teaching_plan = None
        try:
            if is_safety_blocked:
                teaching_plan = build_fixed_teaching_action(
                    baseline_feedback,
                    language=payload.language,
                    status="SAFETY_FEEDBACK",
                )
            elif diagnostic_package is not None:
                teaching_plan = select_teaching_actions(
                    diagnostic_package,
                    plan,
                    expected_is_correct=is_correct,
                    language=payload.language,
                )
            else:
                teaching_plan = build_fixed_teaching_action(
                    baseline_feedback,
                    language=payload.language,
                    status="PHASE45_DEGRADED_NO_DIAGNOSIS",
                )
            return teaching_plan, render_student_feedback(teaching_plan)
        except Exception:
            logger.exception(
                "Phase 4/5 delivery failed; falling back to an answer-free L1 action"
            )
            fallback_text = _phase45_degraded_feedback(
                is_correct=is_correct,
                language=payload.language,
            )
            if teaching_plan is not None:
                teaching_plan = degrade_teaching_action(
                    teaching_plan,
                    fallback_text,
                    status="PHASE45_DEGRADED_RENDER_OR_SAFETY",
                )
            else:
                support = getattr(plan, "support", None)
                recommended = getattr(support, "support_level", None)
                support_need = getattr(support, "support_need", None)
                has_valid_recommendation = (
                    isinstance(recommended, int)
                    and not isinstance(recommended, bool)
                    and 1 <= recommended <= 4
                    and isinstance(support_need, (int, float))
                    and not isinstance(support_need, bool)
                    and 0.0 <= float(support_need) <= 1.0
                )
                teaching_plan = build_fixed_teaching_action(
                    fallback_text,
                    language=payload.language,
                    status="PHASE45_DEGRADED_SELECTION",
                    recommended_support_level=(
                        recommended if has_valid_recommendation else None
                    ),
                    support_need=(
                        float(support_need)
                        if has_valid_recommendation
                        else None
                    ),
                    support_policy_version=(
                        SUPPORT_POLICY_VERSION
                        if has_valid_recommendation
                        else None
                    ),
                )
            return teaching_plan, render_emergency_feedback(teaching_plan)

    def _delivery_fingerprint(plan: Any) -> str:
        if plan is None:
            return ""
        try:
            return json.dumps(
                plan.to_audit_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            return repr(plan)

    async def _maybe_llm_feedback(teaching_plan: Any, artifact: Any) -> Any:
        # Keep the deterministic artifact as the immediate fallback.  A bad
        # model response must not enter the emergency path merely because the
        # optional editor failed.
        try:
            llm_feedback = await generate_phase5_feedback(teaching_plan)
            if llm_feedback is not None:
                return render_llm_student_feedback(teaching_plan, llm_feedback)
        except Exception:
            logger.exception(
                "Phase 5 LLM feedback failed; using deterministic safe renderer"
            )
        return artifact

    provisional_phase3_plan, _provisional_phase3_summary = await _prepare_phase3(
        locked=False
    )
    provisional_teaching_plan, provisional_artifact = _build_delivery(
        provisional_phase3_plan
    )
    provisional_artifact = await _maybe_llm_feedback(
        provisional_teaching_plan,
        provisional_artifact,
    )

    # From this point onward the route mutates submission/chat/BKT state.
    # Serialize by user, then perform a current read so concurrent transport
    # retries cannot both create submissions or learning observations.
    await _lock_check_sql_user(session, user_id)
    chat_repo = ChatRepository(session)
    replay = await submission_repo.get_by_attempt_id(
        user_id,
        payload.question_id,
        attempt_id,
        for_update=True,
    )
    if replay is not None:
        return _restore_attempt_response(
            replay,
            request_fingerprint=request_fingerprint,
        )

    # Only this locked plan is allowed to reach BKT persistence.  If history
    # changed while the model was editing the provisional response, use a
    # fresh deterministic artifact rather than applying stale LLM text.
    phase3_plan, phase3_summary = await _prepare_phase3(locked=True)
    final_teaching_plan, final_deterministic_artifact = _build_delivery(
        phase3_plan
    )
    if _delivery_fingerprint(final_teaching_plan) == _delivery_fingerprint(
        provisional_teaching_plan
    ):
        teaching_plan = final_teaching_plan
        feedback_artifact = provisional_artifact
    else:
        teaching_plan = final_teaching_plan
        feedback_artifact = final_deterministic_artifact
    ai_hint_text = _validated_learner_feedback(
        feedback_artifact.text,
        correct_sql=question.correct_sql,
        allowed_public_context=learner_feedback_context,
    )
    teaching_support = build_teaching_support_summary(
        teaching_plan,
        feedback_artifact,
    )

    # 6. 保存提交记录
    submission_data = SubmissionCreate(
        user_id=user_id,
        question_id=payload.question_id,
        attempt_id=attempt_id,
        request_fingerprint=request_fingerprint,
        student_sql=payload.student_sql,
        ai_hint=ai_hint_text,
        is_correct=is_correct,
        hint_level=teaching_plan.delivered_support_level,
    )
    submission = await submission_repo.create(submission_data)
    await _persist_teaching_delivery_audit(
        session,
        submission_id=submission.id,
        teaching_plan=teaching_plan,
        feedback_artifact=feedback_artifact,
        phase2_llm_review=phase2_llm_review,
    )

    if is_safety_blocked:
        # Safety interception is recorded for audit/window boundaries only.  A
        # safety event is never a semantic failure and never updates BKT.
        try:
            from models.phase3_learning import Phase3BehaviorEventKind
            from repository.phase3_behavior_repo import Phase3BehaviorEventRepository

            async with session.begin_nested():
                await Phase3BehaviorEventRepository(session).record_once(
                    submission_id=submission.id,
                    user_id=user_id,
                    question_id=payload.question_id,
                    event_kind=Phase3BehaviorEventKind.SAFETY_BLOCKED,
                )
        except Exception:
            logger.exception(
                "Phase 3 safety behavior audit failed; preserving the safety response"
            )

    if phase3_plan is not None:
        try:
            from core.phase3_runtime import apply_phase3_attempt

            # A savepoint keeps optional learning-state persistence isolated
            # from the submission and chat-history transaction.
            async with session.begin_nested():
                phase3_summary = await apply_phase3_attempt(
                    session,
                    plan=phase3_plan,
                    submission_id=submission.id,
                    user_id=user_id,
                    question_id=payload.question_id,
                    delivered_assistance_level=(
                        teaching_plan.delivered_support_level
                    ),
                    answer_revealed=False,
                )
        except Exception:
            logger.exception(
                "Phase 3 persistence failed; rolling back only the learning update"
            )
            from core.phase3_runtime import degraded_learning_summary

            phase3_summary = degraded_learning_summary()

    if is_safety_blocked:
        system_result = "【新一轮提交】代码包含危险操作，系统已拒绝执行。"
    else:
        system_result = f"【新一轮提交】结果：{'正确' if is_correct else '不正确'}"

    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="system",
        content=system_result,
    )
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="user",
        content=f"我提交的 SQL：\n\n```sql\n{payload.student_sql}\n```",
    )
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="assistant",
        content=ai_hint_text,
    )

    ai_hint_result = SQLCheckResultSchema(
        diagnoses=[],
        overall_comment=ai_hint_text
    )

    response = SQLCheckResponse(
        is_correct=is_correct,
        hint=ai_hint_result.model_dump(),
        submission_id=submission.id,
        attempt_id=attempt_id,
        error_message=error_message,
        judge_status=judge_status,
        is_safety_blocked=is_safety_blocked,
        lambda_t=None,
        phase3_learning=(
            phase3_summary.to_public_dict(
                support_recommendation_applied=(
                    teaching_plan.support_recommendation_applied
                ),
                delivered_support_level=(
                    teaching_plan.delivered_support_level
                ),
            )
            if phase3_summary is not None
            else None
        ),
        teaching_support=teaching_support,
        # Keep the legacy fields in the schema, but do not expose raw Phase 1
        # telemetry to learners: AST diffs can contain reference-side SQL.
        observation=None,
        error_attributions=[],
        # The full Phase 2 package would bypass L1/L2 disclosure control.
        # Keep it server-side; learners receive only the selected Phase 5 text.
        diagnostic_package=None,
    )
    submission.response_snapshot = response.model_dump(mode="json")
    await session.commit()
    return response


@router.get("/chat/messages", response_model=list[ChatMessageOut])
async def get_chat_messages(
    question_id: int = Query(..., description="题目 ID"),
    limit: int = Query(50, ge=1, le=200, description="限制数量"),
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    chat_repo = ChatRepository(session)
    msgs = await chat_repo.list_messages(user_id=user_id, question_id=question_id, limit=limit)
    return msgs


@router.delete("/chat/messages")
async def clear_chat_messages(
    question_id: int = Query(..., description="题目 ID"),
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """清除当前用户在该题目下的所有对话历史。"""
    chat_repo = ChatRepository(session)
    deleted = await chat_repo.delete_messages_by_user_question(user_id=user_id, question_id=question_id)
    await session.commit()
    return {"deleted": deleted}


@router.post("/chat", response_model=ChatSendOut)
async def chat(
    payload: ChatSendIn,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    # 题目上下文
    question_repo = QuestionRepository(session)
    question = await question_repo.get_by_id(payload.question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {payload.question_id} 不存在",
        )

    # 最近一次提交（可选）
    submission_repo = SubmissionRepository(session)
    latest_list = await submission_repo.get_user_submissions(user_id, payload.question_id, limit=1)
    latest = latest_list[0] if latest_list else None

    # 先写入用户消息
    chat_repo = ChatRepository(session)
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="user",
        content=payload.message,
    )

    # 本地生成苏格拉底助教对话回复
    if latest:
        if latest.is_correct:
            if payload.language == "en":
                reply = "Great job! Your previous SQL submission is correct. Feel free to ask if you have any questions or optimizations."
            elif payload.language == "zh-TW":
                reply = "太棒了！你的上一輪 SQL 提交是完全正確的。如果有關於該題目的優化或其他疑問，可以隨時與我交流。"
            else:
                reply = "太棒了！你的上一轮 SQL 提交是完全正确的。如果有关于该题目的优化或其他疑问，可以随时跟我交流。"
        else:
            # 提取上一次的本地 Socratic 提示信息并反馈
            if payload.language == "en":
                reply = f"Hello! I am your SQL teaching assistant. Regarding your last SQL submission, we identified the following structural or logical issues:\n\n{latest.ai_hint}\n\nPlease revise your query based on this analysis and submit again."
            elif payload.language == "zh-TW":
                reply = f"你好！我是你的 SQL 助教。關於你上一輪提交的 SQL 語句，我們發現了以下結構或邏輯問題：\n\n{latest.ai_hint}\n\n你可以根據提示重點檢查對應的子句和過濾條件。修改完後，請重新提交運行。"
            else:
                reply = f"你好！我是你的 SQL 助教。关于你上一轮提交的 SQL 语句，我们发现了以下结构或逻辑问题：\n\n{latest.ai_hint}\n\n你可以根据提示重点检查对应的子句和过滤条件。修改完后，请重新提交运行。"
    else:
        if payload.language == "en":
            reply = "Hello! I am your SQL teaching assistant. You haven't submitted any queries for this question yet. Please write your SQL and click 'Submit & Run', and I will analyze it for you."
        elif payload.language == "zh-TW":
            reply = "你好！我是你的 SQL 助教。你還沒有針對這道題提交任何作答。請先編寫你的查詢並點擊『提交運行』，我將為你分析具體問題。"
        else:
            reply = "你好！我是你的 SQL 助教。你还没有针对这道题提交任何作答。请先编写你的查询并点击『提交运行』，我将为你分析具体问题。"

    # 写入 AI 回复
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="assistant",
        content=reply,
    )
    await session.commit()

    return ChatSendOut(reply=reply)


@router.get("/submissions", response_model=list[SubmissionOut])
async def get_my_submissions(
    question_id: int | None = Query(None, description="题目 ID（可选，过滤特定题目）"),
    limit: int = Query(100, ge=1, le=200, description="限制数量"),
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """获取当前用户的提交记录。

    支持按题目 ID 过滤，返回按时间倒序排列的提交记录。
    """
    repo = SubmissionRepository(session)
    submissions = await repo.get_user_submissions(
        user_id, question_id, limit
    )
    return submissions


@router.get("/submissions/{submission_id}", response_model=SubmissionOut)
async def get_submission(
    submission_id: int,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """获取单条提交记录详情。

    只能查看自己的提交记录。
    """
    repo = SubmissionRepository(session)
    submission = await repo.get_by_id(submission_id)
    
    if not submission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"提交记录 ID {submission_id} 不存在"
        )
    
    # 验证是否是自己的提交记录
    if submission.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此提交记录"
        )
    
    return submission


__all__ = ["router"]
