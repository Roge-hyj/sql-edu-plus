"""Bounded, auditable offline calibration for the Phase 3 BKT policy.

The runtime default remains the deliberately uncalibrated MVP parameter set.
This module only activates a parameter artifact when the artifact describes a
real learner-event export and passes every version, provenance, size, and
held-out-metric check.  Synthetic audit traces may be parsed for experiments,
but they can never become the active production policy.

The fitting helper is intentionally small and dependency-free so it can run as
an offline, bounded job.  It is not called by the API process.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping

from core.phase3_bkt import BKTParameters, BKT_PARAMETERS_V1, update_bkt


CALIBRATION_ARTIFACT_SCHEMA_VERSION = "phase3.bkt_calibration_artifact.v1"
CALIBRATED_OFFLINE_STATUS = "CALIBRATED_OFFLINE"
CALIBRATED_OFFLINE_SYNTHETIC_STATUS = "CALIBRATED_OFFLINE_SYNTHETIC"
UNCALIBRATED_STATUS = "UNCALIBRATED_MVP"
REAL_STUDENT_EVENTS = "REAL_STUDENT_EVENTS"
SYNTHETIC_AUDIT_EVENTS = "SYNTHETIC_AUDIT_EVENTS"
CALIBRATED_PARAMETER_PREFIX = "phase3.bkt_parameters.calibrated."

# These are hard safety limits for a calibration job or an optional runtime
# artifact.  They are intentionally modest: calibration belongs in a bounded
# offline worker, never in an API request or a model-loading process.
MAX_ARTIFACT_BYTES = 1 * 1024 * 1024
MAX_EVENT_FILE_BYTES = 128 * 1024 * 1024
MAX_EVENT_LINE_BYTES = 256 * 1024
MAX_CALIBRATION_SAMPLES = 100_000
MAX_FIT_SAMPLES = 25_000
MIN_CALIBRATION_SAMPLES = 100
MIN_HELD_OUT_SAMPLES = 10
MIN_CALIBRATION_STUDENTS = 2
MAX_IDENTIFIER_CHARS = 128
MAX_FITTING_METHOD_CHARS = 96
MAX_DETERMINISTIC_SEED = 2**31 - 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BKTCalibrationError(ValueError):
    """Raised when a calibration source or artifact fails closed."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_DUPLICATE_KEY",
                f"duplicate JSON key {key!r}",
            )
        result[key] = value
    return result


def _load_json_object(text: str, *, source: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except BKTCalibrationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_JSON_INVALID", f"invalid JSON in {source}"
        ) from exc
    if not isinstance(value, Mapping):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_OBJECT_REQUIRED", f"{source} must contain an object"
        )
    return value


def _open_bounded_file(
    path: str | os.PathLike[str], *, maximum: int, code: str
) -> Any:
    """Open a bounded regular file without a symlink/stat race.

    Calibration inputs are deployment-controlled rather than learner-controlled,
    but the loader still fails closed if a configured path is swapped while it
    is being inspected.  WSL exposes ``O_NOFOLLOW``; refusing platforms without
    it is safer than silently weakening this boundary.
    """

    if not isinstance(path, (str, os.PathLike)):
        raise BKTCalibrationError(code, "path must be a string or path-like value")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise BKTCalibrationError(code, "platform cannot enforce no-follow opens")
    candidate = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > maximum:
            raise BKTCalibrationError(code, "path is not a bounded regular file")
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        return handle
    except BKTCalibrationError:
        raise
    except (OSError, ValueError) as exc:
        raise BKTCalibrationError(code, "path cannot be inspected") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def sha256_file(path: str | os.PathLike[str], *, maximum: int = MAX_EVENT_FILE_BYTES) -> str:
    """Hash a bounded regular file without loading it into memory."""

    digest = sha256()
    try:
        with _open_bounded_file(
            path, maximum=maximum, code="BKT_CALIBRATION_SOURCE_INVALID"
        ) as handle:
            total_bytes = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > maximum:
                    raise BKTCalibrationError(
                        "BKT_CALIBRATION_SOURCE_INVALID",
                        "source grew beyond its bounded size",
                    )
                digest.update(chunk)
    except BKTCalibrationError:
        raise
    except OSError as exc:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_SOURCE_UNREADABLE", "source cannot be read"
        ) from exc
    return digest.hexdigest()


def _text_field(
    raw: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    field_name: str,
    maximum: int = MAX_IDENTIFIER_CHARS,
) -> str:
    value: Any = None
    for name in names:
        if raw.get(name) is not None:
            value = raw.get(name)
            break
    if isinstance(value, bool) or value is None:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_EVENT_INVALID", f"{field_name} is required"
        )
    text = str(value).strip()
    if not text or len(text) > maximum:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_EVENT_INVALID", f"{field_name} is out of bounds"
        )
    return text


def _event_correctness(raw: Mapping[str, Any]) -> bool:
    if "is_correct" in raw:
        value = raw.get("is_correct")
        if type(value) is not bool:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_EVENT_INVALID", "is_correct must be boolean"
            )
        return value
    value = raw.get("observation_result", raw.get("result"))
    normalized = str(getattr(value, "value", value) or "").strip().upper()
    if normalized == "CORRECT":
        return True
    if normalized == "INCORRECT":
        return False
    raise BKTCalibrationError(
        "BKT_CALIBRATION_EVENT_INVALID",
        "is_correct or observation_result must be present",
    )


@dataclass(frozen=True, slots=True)
class LabeledStudentEvent:
    """The minimum non-sensitive event shape used by offline calibration."""

    event_id: str
    student_id: str
    skill_id: str
    observed_at: str
    is_correct: bool


def _parse_event(raw: Mapping[str, Any], *, allow_synthetic: bool) -> LabeledStudentEvent:
    if "is_synthetic" in raw and type(raw.get("is_synthetic")) is not bool:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_EVENT_INVALID", "is_synthetic must be boolean"
        )
    if "answer_revealed" in raw and type(raw.get("answer_revealed")) is not bool:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_EVENT_INVALID", "answer_revealed must be boolean"
        )
    marker = raw.get("is_synthetic") is True or str(
        raw.get("source_kind") or ""
    ).strip().upper() in {"SYNTHETIC", SYNTHETIC_AUDIT_EVENTS}
    if marker and not allow_synthetic:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_SYNTHETIC_SOURCE",
            "synthetic events require an explicit synthetic calibration mode",
        )
    if raw.get("answer_revealed") is True:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_EVENT_INVALID",
            "answer-revealed events cannot calibrate answer knowledge",
        )
    return LabeledStudentEvent(
        event_id=_text_field(
            raw, ("event_id", "observation_event_id", "id"), field_name="event_id"
        ),
        student_id=_text_field(
            raw, ("student_id", "user_id"), field_name="student_id"
        ),
        skill_id=_text_field(raw, ("skill_id",), field_name="skill_id"),
        observed_at=_text_field(
            raw,
            ("observed_at", "created_at", "timestamp"),
            field_name="observed_at",
        ),
        is_correct=_event_correctness(raw),
    )


def read_labeled_student_events(
    path: str | os.PathLike[str],
    *,
    allow_synthetic: bool = False,
) -> tuple[tuple[LabeledStudentEvent, ...], str]:
    """Read a bounded JSONL learner-event source and return its raw digest.

    The source digest covers the exact input bytes, including line endings.
    Raw event contents never enter the artifact; only counts, metrics, and this
    digest are retained.
    """

    digest = sha256()
    events: list[LabeledStudentEvent] = []
    seen_event_ids: set[str] = set()
    try:
        with _open_bounded_file(
            path, maximum=MAX_EVENT_FILE_BYTES, code="BKT_CALIBRATION_SOURCE_INVALID"
        ) as handle:
            total_bytes = 0
            for line_number, raw_line in enumerate(handle, start=1):
                total_bytes += len(raw_line)
                if total_bytes > MAX_EVENT_FILE_BYTES:
                    raise BKTCalibrationError(
                        "BKT_CALIBRATION_SOURCE_INVALID",
                        "source grew beyond its bounded size",
                    )
                if len(raw_line) > MAX_EVENT_LINE_BYTES:
                    raise BKTCalibrationError(
                        "BKT_CALIBRATION_EVENT_LINE_TOO_LARGE",
                        f"line {line_number} exceeds the bounded line size",
                    )
                digest.update(raw_line)
                if not raw_line.strip():
                    continue
                try:
                    text = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BKTCalibrationError(
                        "BKT_CALIBRATION_SOURCE_ENCODING", "source must be UTF-8"
                    ) from exc
                raw = _load_json_object(text, source=f"line {line_number}")
                event = _parse_event(raw, allow_synthetic=allow_synthetic)
                if event.event_id in seen_event_ids:
                    raise BKTCalibrationError(
                        "BKT_CALIBRATION_DUPLICATE_EVENT",
                        f"duplicate event_id at line {line_number}",
                    )
                seen_event_ids.add(event.event_id)
                events.append(event)
                if len(events) > MAX_CALIBRATION_SAMPLES:
                    raise BKTCalibrationError(
                        "BKT_CALIBRATION_SAMPLE_LIMIT",
                        f"more than {MAX_CALIBRATION_SAMPLES} events",
                    )
    except BKTCalibrationError:
        raise
    except OSError as exc:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_SOURCE_UNREADABLE", "source cannot be read"
        ) from exc

    if len(events) < MIN_CALIBRATION_SAMPLES:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_SAMPLE_MINIMUM",
            f"at least {MIN_CALIBRATION_SAMPLES} labeled events are required",
        )
    student_count = len({event.student_id for event in events})
    if student_count < MIN_CALIBRATION_STUDENTS:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_STUDENT_MINIMUM",
            f"at least {MIN_CALIBRATION_STUDENTS} students are required",
        )
    return tuple(events), digest.hexdigest()


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", f"{field_name} must be numeric"
        )
    number = float(value)
    if not math.isfinite(number):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", f"{field_name} must be finite"
        )
    return number


def _probability(value: Any, *, field_name: str, strict: bool = False) -> float:
    number = _finite_number(value, field_name=field_name)
    if not 0.0 <= number <= 1.0 or (strict and number in {0.0, 1.0}):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID",
            f"{field_name} must be in the allowed probability interval",
        )
    return number


def _required_int(raw: Mapping[str, Any], field_name: str, *, minimum: int = 0) -> int:
    value = raw.get(field_name)
    if type(value) is not int or value < minimum:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID",
            f"{field_name} must be an integer >= {minimum}",
        )
    return value


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    """Validated, JSON-safe result of an offline BKT calibration job."""

    status: str
    source_kind: str
    source_digest_sha256: str
    sample_count: int
    student_count: int
    training_sample_count: int
    held_out_sample_count: int
    parameter_version: str
    parameters: BKTParameters
    fitting_method: str
    deterministic_seed: int
    held_out_log_loss: float
    held_out_brier_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or not isinstance(self.source_kind, str):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "status and source_kind must be strings",
            )
        if not isinstance(self.source_digest_sha256, str):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "source digest must be a string"
            )
        if not all(
            type(value) is int
            for value in (
                self.sample_count,
                self.student_count,
                self.training_sample_count,
                self.held_out_sample_count,
            )
        ):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "sample counts must be integers"
            )
        if not isinstance(self.parameter_version, str):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "parameter_version must be a string",
            )
        if not isinstance(self.parameters, BKTParameters):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "parameters must be BKTParameters"
            )
        if not isinstance(self.fitting_method, str):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "fitting_method must be a string"
            )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (self.held_out_log_loss, self.held_out_brier_score)
        ):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "held-out metrics must be numeric",
            )
        if self.status not in {
            CALIBRATED_OFFLINE_STATUS,
            CALIBRATED_OFFLINE_SYNTHETIC_STATUS,
        }:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "unsupported calibration status"
            )
        expected_kind = (
            REAL_STUDENT_EVENTS
            if self.status == CALIBRATED_OFFLINE_STATUS
            else SYNTHETIC_AUDIT_EVENTS
        )
        if self.source_kind != expected_kind:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "status and source_kind do not agree",
            )
        if not _SHA256.fullmatch(self.source_digest_sha256):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "source digest is not SHA-256"
            )
        if not MIN_CALIBRATION_SAMPLES <= self.sample_count <= MAX_CALIBRATION_SAMPLES:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "sample_count is outside the bounded calibration range",
            )
        if self.student_count < MIN_CALIBRATION_STUDENTS:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "student_count is below minimum"
            )
        if (
            self.training_sample_count < 1
            or self.training_sample_count > MAX_CALIBRATION_SAMPLES
            or self.held_out_sample_count < MIN_HELD_OUT_SAMPLES
            or self.held_out_sample_count > MAX_CALIBRATION_SAMPLES
            or self.training_sample_count + self.held_out_sample_count
            != self.sample_count
        ):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "training and held-out sample counts are inconsistent",
            )
        if not self.parameter_version.startswith(CALIBRATED_PARAMETER_PREFIX):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "calibrated parameters require an explicit version prefix",
            )
        if self.parameters.version != self.parameter_version:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID",
                "parameter version does not match BKT parameters",
            )
        if not self.fitting_method or len(self.fitting_method) > MAX_FITTING_METHOD_CHARS:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "fitting_method is out of bounds"
            )
        if (
            type(self.deterministic_seed) is not int
            or not 0 <= self.deterministic_seed <= MAX_DETERMINISTIC_SEED
        ):
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "deterministic_seed is invalid"
            )
        if not math.isfinite(self.held_out_log_loss) or self.held_out_log_loss < 0:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "held-out log loss is invalid"
            )
        if not 0.0 <= self.held_out_brier_score <= 1.0:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_ARTIFACT_INVALID", "held-out Brier score is invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CALIBRATION_ARTIFACT_SCHEMA_VERSION,
            "status": self.status,
            "source_kind": self.source_kind,
            "source_digest_sha256": self.source_digest_sha256,
            "sample_count": self.sample_count,
            "student_count": self.student_count,
            "training_sample_count": self.training_sample_count,
            "held_out_sample_count": self.held_out_sample_count,
            "parameter_version": self.parameter_version,
            "parameters": {
                "version": self.parameters.version,
                "initial_mastery": self.parameters.initial_mastery,
                "slip": self.parameters.slip,
                "guess": self.parameters.guess,
                "transition": self.parameters.transition,
            },
            "fitting_method": self.fitting_method,
            "deterministic_seed": self.deterministic_seed,
            "held_out": {
                "sample_count": self.held_out_sample_count,
                "log_loss": self.held_out_log_loss,
                "brier_score": self.held_out_brier_score,
            },
        }


def _parameters_from_artifact(raw: Mapping[str, Any]) -> BKTParameters:
    parameters = raw.get("parameters")
    if not isinstance(parameters, Mapping):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", "parameters object is required"
        )
    version = parameters.get("version")
    if not isinstance(version, str) or not version or len(version) > 64:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", "parameter version is invalid"
        )
    try:
        return BKTParameters(
            version=version,
            initial_mastery=_probability(
                parameters.get("initial_mastery"), field_name="initial_mastery"
            ),
            slip=_probability(parameters.get("slip"), field_name="slip", strict=True),
            guess=_probability(parameters.get("guess"), field_name="guess", strict=True),
            transition=_probability(
                parameters.get("transition"), field_name="transition"
            ),
        )
    except BKTCalibrationError:
        raise
    except (TypeError, ValueError) as exc:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", "BKT parameters are invalid"
        ) from exc


def calibration_artifact_from_dict(raw: Mapping[str, Any]) -> CalibrationArtifact:
    """Validate and materialize an artifact without activating it."""

    if not isinstance(raw, Mapping):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", "artifact must be an object"
        )
    if raw.get("schema_version") != CALIBRATION_ARTIFACT_SCHEMA_VERSION:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_VERSION_UNSUPPORTED",
            "unsupported calibration artifact schema",
        )
    held_out = raw.get("held_out")
    if not isinstance(held_out, Mapping):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID", "held_out metrics are required"
        )
    held_out_count = _required_int(held_out, "sample_count", minimum=MIN_HELD_OUT_SAMPLES)
    artifact = CalibrationArtifact(
        status=str(raw.get("status") or ""),
        source_kind=str(raw.get("source_kind") or ""),
        source_digest_sha256=str(raw.get("source_digest_sha256") or "").lower(),
        sample_count=_required_int(raw, "sample_count", minimum=MIN_CALIBRATION_SAMPLES),
        student_count=_required_int(raw, "student_count", minimum=MIN_CALIBRATION_STUDENTS),
        training_sample_count=_required_int(raw, "training_sample_count", minimum=1),
        held_out_sample_count=held_out_count,
        parameter_version=str(raw.get("parameter_version") or ""),
        parameters=_parameters_from_artifact(raw),
        fitting_method=str(raw.get("fitting_method") or ""),
        deterministic_seed=_required_int(raw, "deterministic_seed", minimum=0),
        held_out_log_loss=_finite_number(
            held_out.get("log_loss"), field_name="held_out.log_loss"
        ),
        held_out_brier_score=_probability(
            held_out.get("brier_score"), field_name="held_out.brier_score"
        ),
    )
    if artifact.held_out_sample_count > artifact.sample_count:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_INVALID",
            "held-out sample count exceeds total sample count",
        )
    return artifact


def load_calibration_artifact(
    path: str | os.PathLike[str],
    *,
    source_path: str | os.PathLike[str] | None = None,
) -> CalibrationArtifact:
    """Load an artifact and optionally verify its source export digest."""

    try:
        with _open_bounded_file(
            path,
            maximum=MAX_ARTIFACT_BYTES,
            code="BKT_CALIBRATION_ARTIFACT_INVALID",
        ) as handle:
            raw_bytes = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(raw_bytes) > MAX_ARTIFACT_BYTES:
                raise BKTCalibrationError(
                    "BKT_CALIBRATION_ARTIFACT_INVALID",
                    "artifact grew beyond its bounded size",
                )
            text = raw_bytes.decode("utf-8")
    except BKTCalibrationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_ARTIFACT_UNREADABLE", "artifact cannot be read"
        ) from exc
    artifact = calibration_artifact_from_dict(
        _load_json_object(text, source="calibration artifact")
    )
    if source_path is not None:
        actual_digest = sha256_file(source_path)
        if actual_digest != artifact.source_digest_sha256:
            raise BKTCalibrationError(
                "BKT_CALIBRATION_SOURCE_DIGEST_MISMATCH",
                "artifact does not match the supplied source export",
            )
    return artifact


@dataclass(frozen=True, slots=True)
class ActiveBKTPolicy:
    parameters: BKTParameters
    calibration_status: str
    artifact_digest_sha256: str | None = None
    rejection_code: str | None = None


def uncalibrated_bkt_policy(*, rejection_code: str | None = None) -> ActiveBKTPolicy:
    return ActiveBKTPolicy(
        parameters=BKT_PARAMETERS_V1,
        calibration_status=UNCALIBRATED_STATUS,
        rejection_code=rejection_code,
    )


def load_active_bkt_policy(
    artifact_path: str | os.PathLike[str] | None = None,
    *,
    source_path: str | os.PathLike[str] | None = None,
) -> ActiveBKTPolicy:
    """Return the active policy, failing closed to the uncalibrated default.

    An empty path means no artifact is configured.  When paths are omitted,
    the environment variables are read for standalone workers; API wiring
    passes the already validated Settings value explicitly.
    """

    configured = artifact_path
    if configured is None:
        configured = os.getenv("PHASE3_BKT_CALIBRATION_ARTIFACT", "")
    if not str(configured or "").strip():
        return uncalibrated_bkt_policy()
    configured_source = source_path
    if configured_source is None:
        configured_source = os.getenv("PHASE3_BKT_CALIBRATION_SOURCE", "") or None
    try:
        artifact = load_calibration_artifact(
            configured,
        )
        if artifact.status == CALIBRATED_OFFLINE_STATUS:
            if not str(configured_source or "").strip():
                return uncalibrated_bkt_policy(
                    rejection_code="BKT_CALIBRATION_SOURCE_REQUIRED"
                )
            artifact = load_calibration_artifact(
                configured,
                source_path=configured_source,
            )
    except BKTCalibrationError as exc:
        return uncalibrated_bkt_policy(rejection_code=exc.code)
    if artifact.status != CALIBRATED_OFFLINE_STATUS:
        # Synthetic artifacts remain useful for reports but are never active.
        return uncalibrated_bkt_policy(
            rejection_code="BKT_CALIBRATION_SYNTHETIC_NOT_ACTIVE"
        )
    return ActiveBKTPolicy(
        parameters=artifact.parameters,
        calibration_status=artifact.status,
        artifact_digest_sha256=artifact.source_digest_sha256,
    )


def _ordered_sequences(events: Iterable[LabeledStudentEvent]) -> tuple[tuple[LabeledStudentEvent, ...], ...]:
    grouped: dict[tuple[str, str], list[LabeledStudentEvent]] = {}
    for event in events:
        grouped.setdefault((event.student_id, event.skill_id), []).append(event)
    return tuple(
        tuple(sorted(group, key=lambda item: (item.observed_at, item.event_id)))
        for _, group in sorted(grouped.items())
    )


def _log_loss(parameters: BKTParameters, events: Iterable[LabeledStudentEvent]) -> float:
    total = 0.0
    count = 0
    for sequence in _ordered_sequences(events):
        prior = parameters.initial_mastery
        for event in sequence:
            probability_correct = (
                prior * (1.0 - parameters.slip)
                + (1.0 - prior) * parameters.guess
            )
            probability = probability_correct if event.is_correct else 1.0 - probability_correct
            total -= math.log(max(1e-12, min(1.0 - 1e-12, probability)))
            prior = update_bkt(
                prior, is_correct=event.is_correct, parameters=parameters
            ).next_prior
            count += 1
    return total / count if count else float("inf")


def _brier_score(parameters: BKTParameters, events: Iterable[LabeledStudentEvent]) -> float:
    total = 0.0
    count = 0
    for sequence in _ordered_sequences(events):
        prior = parameters.initial_mastery
        for event in sequence:
            probability_correct = (
                prior * (1.0 - parameters.slip)
                + (1.0 - prior) * parameters.guess
            )
            total += (probability_correct - float(event.is_correct)) ** 2
            prior = update_bkt(
                prior, is_correct=event.is_correct, parameters=parameters
            ).next_prior
            count += 1
    return total / count if count else 1.0


def _split_by_student(
    events: tuple[LabeledStudentEvent, ...], *, seed: int
) -> tuple[tuple[LabeledStudentEvent, ...], tuple[LabeledStudentEvent, ...]]:
    students = sorted({event.student_id for event in events})
    held_out_students = {
        student
        for student in students
        if int.from_bytes(
            sha256(f"{seed}\0{student}".encode("utf-8")).digest()[:4], "big"
        )
        % 5
        == 0
    }
    if not held_out_students:
        held_out_students = {students[-1]}
    if len(held_out_students) == len(students):
        held_out_students.remove(students[0])
    training = tuple(event for event in events if event.student_id not in held_out_students)
    held_out = tuple(event for event in events if event.student_id in held_out_students)
    return training, held_out


_FIT_GRIDS = {
    "initial_mastery": (0.05, 0.20, 0.35, 0.50, 0.65, 0.80, 0.95),
    "slip": (0.02, 0.05, 0.10, 0.15, 0.20, 0.30),
    "guess": (0.02, 0.05, 0.10, 0.20, 0.30, 0.40),
    "transition": (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40),
}


def _coordinate_fit(events: tuple[LabeledStudentEvent, ...]) -> BKTParameters:
    current = BKT_PARAMETERS_V1
    names = tuple(_FIT_GRIDS)
    for _ in range(3):
        for name in names:
            best = current
            best_score = _log_loss(current, events)
            for value in _FIT_GRIDS[name]:
                values = {
                    "initial_mastery": current.initial_mastery,
                    "slip": current.slip,
                    "guess": current.guess,
                    "transition": current.transition,
                }
                values[name] = value
                candidate = BKTParameters(
                    version="phase3.bkt_parameters.calibration_search",
                    **values,
                )
                score = _log_loss(candidate, events)
                if score < best_score - 1e-12 or (
                    math.isclose(score, best_score, abs_tol=1e-12)
                    and value < getattr(best, name)
                ):
                    best, best_score = candidate, score
            current = best
    return current


def fit_bkt_calibration(
    source_path: str | os.PathLike[str],
    *,
    deterministic_seed: int = 0,
    source_kind: str = REAL_STUDENT_EVENTS,
) -> CalibrationArtifact:
    """Fit a small deterministic BKT grid from a bounded JSONL source."""

    if (
        type(deterministic_seed) is not int
        or not 0 <= deterministic_seed <= MAX_DETERMINISTIC_SEED
    ):
        raise BKTCalibrationError(
            "BKT_CALIBRATION_SEED_INVALID", "deterministic_seed is invalid"
        )
    if source_kind not in {REAL_STUDENT_EVENTS, SYNTHETIC_AUDIT_EVENTS}:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_SOURCE_KIND_INVALID", "unsupported source kind"
        )
    events, digest = read_labeled_student_events(
        source_path, allow_synthetic=source_kind == SYNTHETIC_AUDIT_EVENTS
    )
    if len(events) > MAX_FIT_SAMPLES:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_FIT_LIMIT",
            f"fitting is bounded to {MAX_FIT_SAMPLES} events",
        )
    training, held_out = _split_by_student(events, seed=deterministic_seed)
    if len(held_out) < MIN_HELD_OUT_SAMPLES or not training:
        raise BKTCalibrationError(
            "BKT_CALIBRATION_HOLDOUT_MINIMUM",
            f"at least {MIN_HELD_OUT_SAMPLES} held-out events and one training event are required",
        )
    fitted = _coordinate_fit(training)
    parameter_version = f"{CALIBRATED_PARAMETER_PREFIX}{digest[:16]}"
    calibrated = BKTParameters(
        version=parameter_version,
        initial_mastery=fitted.initial_mastery,
        slip=fitted.slip,
        guess=fitted.guess,
        transition=fitted.transition,
    )
    return CalibrationArtifact(
        status=(
            CALIBRATED_OFFLINE_STATUS
            if source_kind == REAL_STUDENT_EVENTS
            else CALIBRATED_OFFLINE_SYNTHETIC_STATUS
        ),
        source_kind=source_kind,
        source_digest_sha256=digest,
        sample_count=len(events),
        student_count=len({event.student_id for event in events}),
        training_sample_count=len(training),
        held_out_sample_count=len(held_out),
        parameter_version=parameter_version,
        parameters=calibrated,
        fitting_method="deterministic_coordinate_grid.v1",
        deterministic_seed=deterministic_seed,
        held_out_log_loss=_log_loss(calibrated, held_out),
        held_out_brier_score=_brier_score(calibrated, held_out),
    )


__all__ = [
    "CALIBRATED_OFFLINE_STATUS",
    "CALIBRATED_OFFLINE_SYNTHETIC_STATUS",
    "CALIBRATED_PARAMETER_PREFIX",
    "CALIBRATION_ARTIFACT_SCHEMA_VERSION",
    "LabeledStudentEvent",
    "ActiveBKTPolicy",
    "BKTCalibrationError",
    "CalibrationArtifact",
    "MAX_CALIBRATION_SAMPLES",
    "MAX_EVENT_FILE_BYTES",
    "MAX_FIT_SAMPLES",
    "MIN_CALIBRATION_SAMPLES",
    "MIN_HELD_OUT_SAMPLES",
    "REAL_STUDENT_EVENTS",
    "SYNTHETIC_AUDIT_EVENTS",
    "UNCALIBRATED_STATUS",
    "calibration_artifact_from_dict",
    "fit_bkt_calibration",
    "load_active_bkt_policy",
    "load_calibration_artifact",
    "read_labeled_student_events",
    "sha256_file",
    "uncalibrated_bkt_policy",
]
