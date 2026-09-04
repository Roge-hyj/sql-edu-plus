"""Bounded regular-expression helpers for SQL witness generation.

The SQL comes from users, so Python's unbounded ``re`` engine is not suitable
for executing arbitrary teaching examples.  The third-party ``regex`` engine
provides a per-match timeout; the additional input caps keep both candidate
generation and SQLite UDF execution deterministic.
"""

from __future__ import annotations

import re
import fnmatch
from typing import Any, Iterable

import regex


MAX_REGEX_PATTERN_LENGTH = 256
MAX_REGEX_VALUE_LENGTH = 128
MAX_REGEX_CANDIDATES = 512
REGEX_TIMEOUT_SECONDS = 0.01


class RegexEvaluationError(ValueError):
    """A regex is invalid, too large, or exceeded its execution budget."""


def regex_matches(
    pattern: Any,
    value: Any,
    *,
    flags: str = "",
) -> bool | None:
    """Evaluate SQL REGEXP_LIKE semantics within a strict resource budget."""
    if pattern is None or value is None:
        return None
    pattern_text = str(pattern)
    value_text = str(value)
    if len(pattern_text) > MAX_REGEX_PATTERN_LENGTH:
        raise RegexEvaluationError("regex_pattern_too_long")
    if len(value_text) > MAX_REGEX_VALUE_LENGTH:
        raise RegexEvaluationError("regex_value_too_long")

    mode = 0
    normalized_flags = str(flags or "").lower()
    if "i" in normalized_flags and "c" not in normalized_flags:
        mode |= regex.IGNORECASE
    if "m" in normalized_flags:
        mode |= regex.MULTILINE
    if "n" in normalized_flags or "s" in normalized_flags:
        mode |= regex.DOTALL
    try:
        return regex.search(
            pattern_text,
            value_text,
            flags=mode,
            timeout=REGEX_TIMEOUT_SECONDS,
        ) is not None
    except TimeoutError as exc:
        raise RegexEvaluationError("regex_match_timeout") from exc
    except regex.error as exc:
        raise RegexEvaluationError(f"invalid_regex_pattern:{exc}") from exc


def like_matches(
    pattern: Any,
    value: Any,
    *,
    escape: str = "\\",
    case_insensitive: bool = False,
) -> bool | None:
    """Evaluate a bounded SQL LIKE pattern without unbounded backtracking.

    ``%`` and ``_`` are wildcards only when they are not preceded by the SQL
    escape character.  The generated regular expression is still executed by
    the same timeout-limited engine as REGEXP predicates.
    """
    if pattern is None or value is None:
        return None
    pattern_text = str(pattern)
    value_text = str(value)
    if len(pattern_text) > MAX_REGEX_PATTERN_LENGTH:
        raise RegexEvaluationError("like_pattern_too_long")
    if len(value_text) > MAX_REGEX_VALUE_LENGTH:
        raise RegexEvaluationError("like_value_too_long")

    escape_text = str(escape)
    if len(escape_text) > 1:
        raise RegexEvaluationError("invalid_like_escape")
    escape_char = escape_text
    pieces: list[str] = []
    index = 0
    while index < len(pattern_text):
        character = pattern_text[index]
        if escape_char and character == escape_char:
            index += 1
            if index >= len(pattern_text):
                raise RegexEvaluationError("invalid_like_pattern:trailing_escape")
            pieces.append(regex.escape(pattern_text[index]))
        elif character == "%":
            pieces.append(".*")
        elif character == "_":
            pieces.append(".")
        else:
            pieces.append(regex.escape(character))
        index += 1

    flags = regex.IGNORECASE if case_insensitive else 0
    try:
        return regex.fullmatch(
            "".join(pieces),
            value_text,
            flags=flags,
            timeout=REGEX_TIMEOUT_SECONDS,
        ) is not None
    except TimeoutError as exc:
        raise RegexEvaluationError("like_match_timeout") from exc
    except regex.error as exc:
        raise RegexEvaluationError(f"invalid_like_pattern:{exc}") from exc


def like_candidate_domain(*patterns: str) -> list[str]:
    """Return a small deterministic domain for common SQL LIKE examples."""
    candidates: dict[str, None] = {}

    def add(value: str) -> None:
        if len(candidates) < MAX_REGEX_CANDIDATES and len(value) <= MAX_REGEX_VALUE_LENGTH:
            candidates.setdefault(value, None)

    for value in (
        "",
        "a",
        "A",
        "b",
        "ab",
        "abc",
        "ABC",
        "a_b",
        "a%b",
        "foo",
        "foobar",
        "barfoo",
        "Alice",
        "Bob",
        "Data Science",
    ):
        add(value)

    for pattern in patterns:
        if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
            raise RegexEvaluationError("like_pattern_too_long")
        literal: list[str] = []
        index = 0
        while index < len(pattern):
            character = pattern[index]
            if character == "\\" and index + 1 < len(pattern):
                index += 1
                literal.append(pattern[index])
            elif character not in {"%", "_"}:
                literal.append(character)
            index += 1
        core = "".join(literal)
        if core:
            add(core)
            add(f"x{core}")
            add(f"{core}x")
            add(core.lower())
            add(core.upper())

    return list(candidates)


def like_separating_values(
    standard_pattern: str,
    student_pattern: str,
    *,
    standard_escape: str = "\\",
    student_escape: str = "\\",
    case_insensitive: bool = False,
) -> list[tuple[str, bool, bool]]:
    """Find bounded values on which two LIKE predicates disagree."""
    separated: list[tuple[str, bool, bool]] = []
    for candidate in like_candidate_domain(standard_pattern, student_pattern):
        standard = like_matches(
            standard_pattern,
            candidate,
            escape=standard_escape,
            case_insensitive=case_insensitive,
        )
        student = like_matches(
            student_pattern,
            candidate,
            escape=student_escape,
            case_insensitive=case_insensitive,
        )
        if standard is not None and student is not None and standard != student:
            separated.append((candidate, standard, student))
    return separated


def glob_matches(pattern: Any, value: Any) -> bool | None:
    """Evaluate SQLite GLOB semantics within the same bounded regex budget."""
    if pattern is None or value is None:
        return None
    pattern_text = str(pattern)
    value_text = str(value)
    if len(pattern_text) > MAX_REGEX_PATTERN_LENGTH:
        raise RegexEvaluationError("glob_pattern_too_long")
    if len(value_text) > MAX_REGEX_VALUE_LENGTH:
        raise RegexEvaluationError("glob_value_too_long")
    try:
        return regex.fullmatch(
            fnmatch.translate(pattern_text),
            value_text,
            timeout=REGEX_TIMEOUT_SECONDS,
        ) is not None
    except TimeoutError as exc:
        raise RegexEvaluationError("glob_match_timeout") from exc
    except regex.error as exc:
        raise RegexEvaluationError(f"invalid_glob_pattern:{exc}") from exc


def glob_candidate_domain(*patterns: str) -> list[str]:
    """Return a bounded domain for common SQLite GLOB teaching patterns."""
    candidates: dict[str, None] = {}

    def add(value: str) -> None:
        if len(candidates) < MAX_REGEX_CANDIDATES and len(value) <= MAX_REGEX_VALUE_LENGTH:
            candidates.setdefault(value, None)

    for value in (
        "",
        "a",
        "A",
        "b",
        "ab",
        "abc",
        "ABC",
        "a1",
        "foo",
        "foobar",
        "barfoo",
        "Alice",
        "Bob",
    ):
        add(value)
    for pattern in patterns:
        if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
            raise RegexEvaluationError("glob_pattern_too_long")
        literal = re.sub(r"[*?\[\]]", "", pattern)
        if literal:
            add(literal)
            add(f"x{literal}")
            add(f"{literal}x")
            add(literal.lower())
            add(literal.upper())
        if "?" in pattern:
            add(pattern.replace("?", "a").replace("*", ""))
            add(pattern.replace("?", "b").replace("*", ""))
        if "[" in pattern:
            add(pattern.replace("[", "").replace("]", "").replace("*", ""))
    return list(candidates)


def glob_separating_values(
    standard_pattern: str,
    student_pattern: str,
) -> list[tuple[str, bool, bool]]:
    """Find bounded values on which two GLOB predicates disagree."""
    separated: list[tuple[str, bool, bool]] = []
    for candidate in glob_candidate_domain(standard_pattern, student_pattern):
        standard = glob_matches(standard_pattern, candidate)
        student = glob_matches(student_pattern, candidate)
        if standard is not None and student is not None and standard != student:
            separated.append((candidate, standard, student))
    return separated


def similar_to_matches(
    pattern: Any,
    value: Any,
    *,
    escape: str = "\\",
) -> bool | None:
    """Evaluate a bounded SQL SIMILAR TO pattern."""
    if pattern is None or value is None:
        return None
    pattern_text = str(pattern)
    value_text = str(value)
    if len(pattern_text) > MAX_REGEX_PATTERN_LENGTH:
        raise RegexEvaluationError("similar_pattern_too_long")
    if len(value_text) > MAX_REGEX_VALUE_LENGTH:
        raise RegexEvaluationError("similar_value_too_long")
    escape_text = str(escape)
    if len(escape_text) > 1:
        raise RegexEvaluationError("invalid_similar_escape")
    pieces: list[str] = []
    index = 0
    while index < len(pattern_text):
        character = pattern_text[index]
        if escape_text and character == escape_text:
            index += 1
            if index >= len(pattern_text):
                raise RegexEvaluationError("invalid_similar_pattern:trailing_escape")
            pieces.append(regex.escape(pattern_text[index]))
        elif character == "%":
            pieces.append(".*")
        elif character == "_":
            pieces.append(".")
        else:
            # SIMILAR TO deliberately retains POSIX/regular-expression
            # operators such as alternation and character classes.
            pieces.append(character)
        index += 1
    try:
        return regex.fullmatch(
            "".join(pieces),
            value_text,
            timeout=REGEX_TIMEOUT_SECONDS,
        ) is not None
    except TimeoutError as exc:
        raise RegexEvaluationError("similar_match_timeout") from exc
    except regex.error as exc:
        raise RegexEvaluationError(f"invalid_similar_pattern:{exc}") from exc


def _similar_pattern_examples(pattern: str, escape: str) -> list[str]:
    """Build a few finite examples from one SIMILAR TO pattern.

    This is deliberately an example generator, rather than a second regex
    parser.  Its important job is to preserve SQL wildcard semantics around
    an explicit escape character.  For example, ``a#%b`` with ``#`` must
    produce ``a%b``, while the same pattern with ``$`` must produce values
    such as ``a#b`` and ``a#xb``.  The bounded matcher remains the authority
    for deciding whether a generated value actually matches.
    """
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        raise RegexEvaluationError("similar_pattern_too_long")
    escape_text = str(escape)
    if len(escape_text) > 1:
        raise RegexEvaluationError("invalid_similar_escape")

    # Keep a small beam of examples.  A beam avoids Cartesian growth for
    # patterns containing several percent/underscore wildcards.
    examples = [""]
    escape_char = escape_text
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if escape_char and character == escape_char:
            index += 1
            if index >= len(pattern):
                raise RegexEvaluationError("invalid_similar_pattern:trailing_escape")
            literal = pattern[index]
            additions = [literal]
        elif character == "%":
            additions = ["", "a", "ab"]
        elif character == "_":
            additions = ["a", "b"]
        else:
            # Retain regex punctuation in the sample.  It is useful for
            # literal-heavy teaching patterns and harmless when punctuation
            # is an operator: similar_separating_values rechecks every value
            # with similar_to_matches before accepting it.
            additions = [character]
        examples = [
            prefix + addition
            for prefix in examples
            for addition in additions
            if len(prefix) + len(addition) <= MAX_REGEX_VALUE_LENGTH
        ][:32]
        if not examples:
            break
        index += 1

    return examples


def similar_candidate_domain(
    *patterns: str,
    escapes: tuple[str, ...] = (),
) -> list[str]:
    """Return a bounded domain for common SIMILAR TO examples.

    ``escapes`` is optional so existing callers that only provide patterns
    keep the old API.  When supplied, examples are generated for each
    pattern/escape pair, which closes the otherwise invisible escape-only
    difference between two predicates with identical pattern text.
    """
    targeted: list[str] = []
    escape_values = list(escapes) if escapes else ["\\"] * len(patterns)
    if len(escape_values) < len(patterns):
        escape_values.extend(["\\"] * (len(patterns) - len(escape_values)))
    for pattern, escape in zip(patterns, escape_values):
        targeted.extend(_similar_pattern_examples(pattern, escape))
        for token in re.findall(r"[A-Za-z0-9]+", pattern):
            if len(token) <= MAX_REGEX_VALUE_LENGTH:
                targeted.extend((token, token.lower(), token.upper()))

    # Keep targeted examples ahead of the broad fallback domain.  Otherwise a
    # pattern with many quantifier-derived candidates could fill the fixed
    # budget before escape-sensitive values are tested.
    candidates = targeted + regex_candidate_domain(*patterns)
    return list(dict.fromkeys(candidates))[:MAX_REGEX_CANDIDATES]


def similar_separating_values(
    standard_pattern: str,
    student_pattern: str,
    *,
    standard_escape: str = "\\",
    student_escape: str = "\\",
) -> list[tuple[str, bool, bool]]:
    """Find bounded values on which two SIMILAR TO predicates disagree."""
    separated: list[tuple[str, bool, bool]] = []
    for candidate in similar_candidate_domain(
        standard_pattern,
        student_pattern,
        escapes=(standard_escape, student_escape),
    ):
        standard = similar_to_matches(
            standard_pattern,
            candidate,
            escape=standard_escape,
        )
        student = similar_to_matches(
            student_pattern,
            candidate,
            escape=student_escape,
        )
        if standard is not None and student is not None and standard != student:
            separated.append((candidate, standard, student))
    return separated


def regex_candidate_domain(*patterns: str) -> list[str]:
    """Return a small deterministic domain aimed at common teaching regexes."""
    candidates: dict[str, None] = {}

    def add(value: str) -> None:
        if (
            len(candidates) < MAX_REGEX_CANDIDATES
            and len(value) <= MAX_REGEX_VALUE_LENGTH
        ):
            candidates.setdefault(value, None)

    for value in (
        "",
        "a",
        "A",
        "0",
        "ab",
        "ABC",
        "123",
        "A1",
        "A12",
        "test",
        "Test",
        "alice@example.com",
        "a@b.co",
        "AB12",
        "000-000-0000",
        "123-456-7890",
        "foo",
        "foobar",
        "barfoo",
    ):
        add(value)

    requested_lengths = set(range(0, 13))
    for pattern in patterns:
        for low, high in re.findall(r"\{(\d+)(?:,(\d*))?\}", pattern):
            low_value = min(int(low), 32)
            high_value = min(int(high), 32) if high else low_value
            requested_lengths.update(
                value
                for value in (
                    low_value - 1,
                    low_value,
                    low_value + 1,
                    high_value,
                    high_value + 1,
                )
                if 0 <= value <= 32
            )

        # Literal runs often expose alternation, prefix and suffix changes.
        for token in re.findall(r"[A-Za-z0-9]+", pattern):
            if token.isdigit() or len(token) > 32:
                continue
            add(token)
            add(token.lower())
            add(token.upper())
            add(f"x{token}")
            add(f"{token}x")

    for length in sorted(requested_lengths):
        for character in ("a", "A", "0", "9", "_", "-"):
            add(character * length)
        add(("A0" * ((length + 1) // 2))[:length])
        add(("a1" * ((length + 1) // 2))[:length])

    return list(candidates)


def regex_separating_values(
    standard_pattern: str,
    student_pattern: str,
) -> list[tuple[str, bool, bool]]:
    """Find bounded values on which two regex predicates disagree."""
    separated: list[tuple[str, bool, bool]] = []
    for candidate in regex_candidate_domain(standard_pattern, student_pattern):
        standard = regex_matches(standard_pattern, candidate)
        student = regex_matches(student_pattern, candidate)
        if standard is not None and student is not None and standard != student:
            separated.append((candidate, standard, student))
    return separated


def first_regex_non_match(patterns: Iterable[str]) -> str | None:
    """Return a bounded value rejected by every supplied pattern."""
    pattern_list = list(patterns)
    for candidate in regex_candidate_domain(*pattern_list):
        if all(regex_matches(pattern, candidate) is False for pattern in pattern_list):
            return candidate
    return None


__all__ = [
    "RegexEvaluationError",
    "first_regex_non_match",
    "like_candidate_domain",
    "like_matches",
    "like_separating_values",
    "glob_candidate_domain",
    "glob_matches",
    "glob_separating_values",
    "similar_candidate_domain",
    "similar_separating_values",
    "similar_to_matches",
    "regex_candidate_domain",
    "regex_matches",
    "regex_separating_values",
]
