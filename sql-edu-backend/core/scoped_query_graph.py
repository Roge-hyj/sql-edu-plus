"""Bounded, evidence-only query-scope graph construction for Phase 2.

The Phase 1 contract currently supplies stable diff IDs and query-scope labels,
but it does not universally supply lexical parents, CTE consumers, derived
table consumers, correlated outer scopes, or set-operation parents.  This
module therefore never reconstructs those edges from names or SQL text.
Composition edges are emitted only when the input contains an explicit edge or
an unambiguous relation field.  Missing metadata produces a ``PARTIAL`` graph
with an auditable limitation.

The graph is an internal Phase 2 evidence artifact.  In particular, its
``standard``-side topology must not be copied into a learner-facing package
without the existing public sanitizer and answer-leakage gates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import heapq
from itertools import islice
import json
import re
from typing import Any


SCHEMA_VERSION = "phase2.scoped-query-graph.v1"

# These are relational execution stages, not a claim that every query is
# reducible to one flat six-step pipeline. Every scope exposes every slot so
# downstream serialization has a stable shape even when most slots are empty.
LOGICAL_STAGE_ORDER: tuple[str, ...] = (
    "PRECHECK",
    "CTE_PRODUCER",
    "SOURCE_JOIN",
    "ROW_FILTER",
    "GROUP_AGG",
    "GROUP_FILTER",
    "WINDOW",
    "PROJECTION",
    "DISTINCT",
    "SET_OP",
    "ROOT_ORDER",
    "PAGINATION",
    "EXTENSION",
)
_STAGE_SET = frozenset(LOGICAL_STAGE_ORDER)

SCOPE_KINDS = frozenset(
    {"ROOT", "CTE", "DERIVED", "SUBQUERY", "SET", "SET_BRANCH", "UNKNOWN"}
)
COMPOSITION_EDGE_TYPES = frozenset(
    {
        "CTE_FEEDS",
        "DERIVED_FEEDS",
        "SUBQUERY_OF",
        "CORRELATED_TO",
        "SET_MEMBER_OF",
    }
)

MAX_DIFFS = 256
MAX_SCOPES = 64
MAX_EDGES = 128
MAX_EVIDENCE_REFS = 32
MAX_METADATA_ITEMS_SCANNED = 2048
MAX_IDENTIFIER_LENGTH = 160


@dataclass(frozen=True)
class ScopeStage:
    name: str
    diff_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "diff_ids": list(self.diff_ids)}


@dataclass(frozen=True)
class QueryScopeNode:
    scope_id: str
    scope_kind: str
    side: str | None
    conceptual_scope_id: str | None
    stages: tuple[ScopeStage, ...]
    metadata_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "scope_kind": self.scope_kind,
            "side": self.side,
            "conceptual_scope_id": self.conceptual_scope_id,
            "metadata_complete": self.metadata_complete,
            "stages": [stage.to_dict() for stage in self.stages],
        }


@dataclass(frozen=True)
class CompositionEdge:
    edge_type: str
    source_scope_id: str
    target_scope_id: str
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_type": self.edge_type,
            "source_scope_id": self.source_scope_id,
            "target_scope_id": self.target_scope_id,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ParentEdge:
    """An explicit lexical parent declaration, separate from composition."""

    source_scope_id: str
    target_scope_id: str
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_type": "PARENT",
            "source_scope_id": self.source_scope_id,
            "target_scope_id": self.target_scope_id,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ConceptualBinding:
    diff_id: str
    conceptual_scope_id: str | None
    scope_ids: tuple[str, ...]
    binding_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "conceptual_scope_id": self.conceptual_scope_id,
            "scope_ids": list(self.scope_ids),
            "binding_status": self.binding_status,
        }


@dataclass(frozen=True)
class ScopedQueryGraph:
    status: str
    scopes: tuple[QueryScopeNode, ...]
    composition_edges: tuple[CompositionEdge, ...]
    parent_edges: tuple[ParentEdge, ...]
    conceptual_bindings: tuple[ConceptualBinding, ...]
    limitations: tuple[str, ...]
    input_diff_count: int
    retained_diff_count: int
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "logical_stage_order": list(LOGICAL_STAGE_ORDER),
            "scopes": [scope.to_dict() for scope in self.scopes],
            "composition_edges": [edge.to_dict() for edge in self.composition_edges],
            "parent_edges": [edge.to_dict() for edge in self.parent_edges],
            "conceptual_bindings": [
                binding.to_dict() for binding in self.conceptual_bindings
            ],
            "limitations": list(self.limitations),
            "counts": {
                "input_diffs": self.input_diff_count,
                "retained_diffs": self.retained_diff_count,
                "scopes": len(self.scopes),
                "composition_edges": len(self.composition_edges),
                "parent_edges": len(self.parent_edges),
                "edges": len(self.composition_edges) + len(self.parent_edges),
                "conceptual_bindings": len(self.conceptual_bindings),
            },
            "truncated": self.truncated,
        }

    def conceptual_scope_for_diff(self, diff_id: str) -> str | None:
        """Return only a proven side-neutral scope for a retained diff."""

        normalized = _identifier(diff_id)
        for binding in self.conceptual_bindings:
            if binding.diff_id == normalized and binding.binding_status in {
                "EXACT_PAIRED",
                "LEGACY_SIDE_NEUTRAL",
            }:
                return binding.conceptual_scope_id
        return None


@dataclass
class _ScopeAccumulator:
    kinds: set[str] = field(default_factory=set)
    diff_ids_by_stage: dict[str, set[str]] = field(
        default_factory=lambda: {stage: set() for stage in LOGICAL_STAGE_ORDER}
    )
    explicitly_declared: bool = False
    correlated: bool = False
    sides: set[str] = field(default_factory=set)
    conceptual_scope_ids: set[str] = field(default_factory=set)
    upstream_complete: bool = True


@dataclass(frozen=True)
class _ScopeMetadataParts:
    scopes: tuple[Any, ...] = ()
    composition_edges: tuple[Any, ...] = ()
    parent_edges: tuple[Any, ...] = ()
    diff_bindings: tuple[Any, ...] = ()
    status: str = ""
    limitations: tuple[str, ...] = ()
    bindings_declared: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class _ExactDiffBinding:
    scope_id: str
    side: str = ""
    conceptual_scope_id: str = ""


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bounded_items(value: Any, limit: int = MAX_METADATA_ITEMS_SCANNED) -> tuple[list[Any], bool]:
    if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
        return [], False
    if isinstance(value, Sequence):
        return list(value[:limit]), len(value) > limit
    if isinstance(value, Iterable):
        items = list(islice(value, limit + 1))
        return items[:limit], len(items) > limit
    return [], False


def _bounded_limit(
    value: Any,
    hard_limit: int,
    name: str,
    limitations: set[str],
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        limitations.add(f"invalid {name}; using hard limit")
        return hard_limit
    if parsed < 1:
        limitations.add(f"invalid {name}; using minimum 1")
        return 1
    return min(parsed, hard_limit)


def _identifier(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    return text[:MAX_IDENTIFIER_LENGTH]


def _bounded_text(value: Any, limit: int = 512) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _upper_identifier(value: Any) -> str:
    text = re.sub(r"[\s-]+", "_", str(value or "").strip())
    return text[:MAX_IDENTIFIER_LENGTH].upper()


def _canonical_json(value: Any) -> str:
    def safe(item: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[bounded]"
        if item is None or isinstance(item, (bool, int, float, str)):
            return item if not isinstance(item, str) else item[:512]
        if isinstance(item, Mapping):
            return {
                str(key)[:80]: safe(nested, depth + 1)
                for key, nested in sorted(item.items(), key=lambda pair: str(pair[0]))[:64]
            }
        if isinstance(item, (list, tuple)):
            return [safe(nested, depth + 1) for nested in item[:64]]
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            try:
                return safe(to_dict(), depth + 1)
            except Exception:
                return type(item).__name__
        return str(item)[:512]

    return json.dumps(safe(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _generated_id(prefix: str, payload: Any) -> str:
    digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _flatten_diff(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        data = dict(raw)
    else:
        to_dict = getattr(raw, "to_dict", None)
        if callable(to_dict):
            try:
                data = _mapping(to_dict())
            except Exception:
                data = {}
        else:
            data = {}
        for name in (
            "clause_category",
            "diff_type",
            "knowledge_point_id",
            "severity",
            "target_table",
            "target_column",
        ):
            value = _field(raw, name)
            if value is not None:
                data.setdefault(name, value)
        extra = _mapping(_field(raw, "extra", {}))
        if extra:
            data.setdefault("extra", extra)
    extra = _mapping(data.get("extra"))
    # Rich Phase 1 serialization already flattens ``extra``.  For the object
    # form, expose only metadata keys; SQL fragments remain irrelevant here.
    return {**extra, **{key: value for key, value in data.items() if key != "extra"}}


def _explicit_diff_id(raw: Any, flattened: Mapping[str, Any]) -> str:
    value = _identifier(flattened.get("diff_id"))
    if value:
        return value
    # ASTDiffNode's Phase 1 identity is stable and position-independent.
    try:
        from core.ast_schema import ASTDiffNode
        from core.witness_generation.obligations import stable_diff_id

        if isinstance(raw, ASTDiffNode):
            return stable_diff_id(raw)
    except Exception:
        pass
    return _generated_id(
        "diff_unbound",
        {
            key: flattened.get(key)
            for key in (
                "clause",
                "clause_category",
                "diff_type",
                "table",
                "target_table",
                "column",
                "target_column",
                "knowledge_point_id",
                "query_scope",
                "scope_id",
            )
        },
    )


def _scope_kind_from_id(scope_id: str) -> str:
    lowered = scope_id.lower()
    if lowered == "root":
        return "ROOT"
    if lowered.startswith("cte:"):
        return "CTE"
    if lowered.startswith("derived:"):
        return "DERIVED"
    if lowered.startswith("set_branch:") or lowered.startswith("set-branch:"):
        return "SET_BRANCH"
    if lowered.startswith("set:"):
        return "SET"
    if (
        lowered == "subquery"
        or lowered.startswith("subquery:")
        or lowered in {"nested_correlation", "nested_membership"}
    ):
        return "SUBQUERY"
    # ``nested:N`` is deliberately UNKNOWN: Phase 1 uses it for CTE bodies,
    # derived tables, subqueries and set branches alike.
    return "UNKNOWN"


def _scope_kind(metadata: Mapping[str, Any], scope_id: str) -> str:
    explicit = _upper_identifier(
        metadata.get("scope_kind") or metadata.get("query_scope_kind")
    )
    if explicit in SCOPE_KINDS:
        return explicit
    return _scope_kind_from_id(scope_id)


def _logical_stage(metadata: Mapping[str, Any]) -> str:
    explicit = _upper_identifier(metadata.get("logical_stage"))
    if explicit in _STAGE_SET:
        return explicit
    clause = _upper_identifier(
        metadata.get("clause") or metadata.get("clause_category")
    )
    diff_type = str(metadata.get("diff_type") or "").lower()
    if clause in {"PRECHECK", "SYNTAX", "SECURITY", "UNSUPPORTED"}:
        return "PRECHECK"
    if clause.startswith("CTE") or "cte_" in diff_type:
        return "CTE_PRODUCER"
    if clause in {"FROM", "JOIN", "JOIN_ON"} or any(
        token in diff_type
        for token in ("join_", "from_source", "correlated_")
    ):
        return "SOURCE_JOIN"
    if clause in {"WHERE", "PREDICATE", "IN", "CORRELATED_SUBQUERY"}:
        return "ROW_FILTER"
    if clause in {"GROUP", "GROUP_BY", "AGGREGATION"} or "group" in diff_type:
        return "GROUP_AGG"
    if clause == "HAVING" or "having" in diff_type:
        return "GROUP_FILTER"
    if clause == "WINDOW" or "window" in diff_type:
        return "WINDOW"
    if clause == "DISTINCT" or diff_type == "distinct_changed":
        return "DISTINCT"
    if clause in {"UNION", "INTERSECT", "EXCEPT", "SET", "SET_OP"} or diff_type.startswith("set_"):
        return "SET_OP"
    if clause in {"ORDER", "ORDER_BY"} or diff_type.startswith("order_"):
        return "ROOT_ORDER"
    if clause in {"LIMIT", "OFFSET"} or any(
        token in diff_type for token in ("limit_", "offset_")
    ):
        return "PAGINATION"
    if clause in {"SELECT", "PROJECTION", "CASE"} or any(
        token in diff_type
        for token in (
            "projection",
            "column_",
            "star_",
            "alias_",
            "case_",
            "aggregate_",
            "function_argument",
        )
    ):
        return "PROJECTION"
    return "EXTENSION"


def _mutation_scope_bindings(
    mutation_metadata: Any,
    limitations: set[str],
) -> tuple[dict[str, set[str]], list[tuple[dict[str, Any], str]]]:
    metadata = _mapping(mutation_metadata)
    tests, truncated = _bounded_items(metadata.get("tests"))
    if truncated:
        limitations.add("mutation metadata scan limit reached")
    bindings: dict[str, set[str]] = {}
    normalized_tests: list[tuple[dict[str, Any], str]] = []
    for raw in tests:
        test = _mapping(raw)
        if not test:
            continue
        test_id = _identifier(test.get("test_id") or test.get("mutation_test_id"))
        if not test_id:
            test_id = _generated_id(
                "mutation",
                {
                    key: test.get(key)
                    for key in ("action", "clause", "query_scope", "diff_ids")
                },
            )
        normalized_tests.append((test, test_id))
        scope_id = _identifier(test.get("scope_id") or test.get("query_scope"))
        diff_ids, refs_truncated = _bounded_items(test.get("diff_ids"), MAX_EVIDENCE_REFS)
        if refs_truncated:
            limitations.add(f"mutation evidence refs truncated: {test_id}")
        if scope_id:
            for diff_id_raw in diff_ids:
                diff_id = _identifier(diff_id_raw)
                if diff_id:
                    bindings.setdefault(diff_id, set()).add(scope_id)
    return bindings, normalized_tests


def _contract_scope_bindings(
    raw_bindings: Sequence[Any],
    limitations: set[str],
) -> tuple[dict[str, set[_ExactDiffBinding]], set[str]]:
    bindings: dict[str, set[_ExactDiffBinding]] = {}
    non_exact_diff_ids: set[str] = set()
    for raw in raw_bindings:
        binding = _mapping(raw)
        if not binding:
            limitations.add("invalid explicit diff binding ignored")
            continue
        diff_id = _identifier(binding.get("diff_id"))
        scope_id = _identifier(binding.get("scope_id") or binding.get("query_scope"))
        status = _upper_identifier(binding.get("binding_status"))
        side = str(binding.get("side") or "").strip().lower()[:32]
        conceptual_scope_id = _identifier(binding.get("conceptual_scope_id"))
        if status == "FALLBACK_LABEL":
            label = diff_id or "unknown"
            limitations.add(f"fallback scope binding ignored for diff {label}")
            if diff_id:
                non_exact_diff_ids.add(diff_id)
            continue
        if status and status not in {
            "EXACT_AST_ANCESTOR",
            "EXACT_AST_PATH",
            "EXACT_PAIRED_AST_PATH",
            "EXACT_SINGLE_SCOPE",
            "EXACT",
            "EXPLICIT",
        }:
            label = diff_id or "unknown"
            limitations.add(f"unsupported scope binding status ignored for diff {label}")
            if diff_id:
                non_exact_diff_ids.add(diff_id)
            continue
        if not diff_id or not scope_id:
            limitations.add("explicit diff binding missing diff_id or scope_id")
            continue
        if side and side not in {"standard", "student"}:
            limitations.add(f"unsupported scope binding side for diff {diff_id}")
        bindings.setdefault(diff_id, set()).add(
            _ExactDiffBinding(scope_id, side, conceptual_scope_id)
        )
    return bindings, non_exact_diff_ids


def _scope_id_for_diff(
    metadata: Mapping[str, Any],
    diff_id: str,
    mutation_bindings: Mapping[str, set[str]],
    limitations: set[str],
) -> str:
    explicit = _identifier(metadata.get("scope_id") or metadata.get("query_scope"))
    mutation_scopes = set(mutation_bindings.get(diff_id, set()))
    candidates = ({explicit} if explicit else set()) | mutation_scopes
    candidates.discard("")
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        limitations.add(f"conflicting query_scope metadata for diff {diff_id}")
    else:
        limitations.add(f"query_scope missing for diff {diff_id}")
    return f"unscoped:{diff_id}"[:MAX_IDENTIFIER_LENGTH]


def _reference_items(value: Any) -> tuple[list[Any], bool]:
    if isinstance(value, (str, bytes, bytearray)):
        return [value], False
    return _bounded_items(value, MAX_EVIDENCE_REFS)


def _edge_payloads(metadata: Mapping[str, Any], evidence_ref: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    nested, _ = _bounded_items(metadata.get("composition_edges"), MAX_EDGES)
    for item in nested:
        edge = _mapping(item)
        if edge:
            refs, _ = _reference_items(edge.get("evidence_refs") or [])
            if evidence_ref:
                refs.append(evidence_ref)
            payloads.append({**edge, "evidence_refs": refs})

    scope_id = _identifier(metadata.get("scope_id") or metadata.get("query_scope"))
    if not scope_id:
        return payloads
    relation_fields = (
        ("cte_consumer_scope_id", "CTE_FEEDS"),
        ("cte_consumer_scope", "CTE_FEEDS"),
        ("derived_consumer_scope_id", "DERIVED_FEEDS"),
        ("derived_consumer_scope", "DERIVED_FEEDS"),
        ("subquery_parent_scope_id", "SUBQUERY_OF"),
        ("subquery_parent_scope", "SUBQUERY_OF"),
        ("correlated_to_scope_id", "CORRELATED_TO"),
        ("correlated_to_scope", "CORRELATED_TO"),
        ("set_parent_scope_id", "SET_MEMBER_OF"),
        ("set_parent_scope", "SET_MEMBER_OF"),
    )
    for field_name, edge_type in relation_fields:
        target = _identifier(metadata.get(field_name))
        if target:
            payloads.append(
                {
                    "edge_type": edge_type,
                    "source_scope_id": scope_id,
                    "target_scope_id": target,
                    "evidence_refs": [evidence_ref] if evidence_ref else [],
                }
            )
    parent = _identifier(
        metadata.get("parent_scope_id") or metadata.get("parent_query_scope")
    )
    parent_edge_type = _upper_identifier(
        metadata.get("parent_edge_type") or metadata.get("composition_type")
    )
    if parent and parent_edge_type in COMPOSITION_EDGE_TYPES:
        payloads.append(
            {
                "edge_type": parent_edge_type,
                "source_scope_id": scope_id,
                "target_scope_id": parent,
                "evidence_refs": [evidence_ref] if evidence_ref else [],
            }
        )
    return payloads


def _parent_edge_payloads(
    metadata: Mapping[str, Any], evidence_ref: str
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    nested, _ = _bounded_items(metadata.get("parent_edges"), MAX_EDGES)
    for item in nested:
        edge = _mapping(item)
        if not edge:
            continue
        refs, _ = _reference_items(edge.get("evidence_refs") or [])
        if evidence_ref:
            refs.append(evidence_ref)
        payloads.append({**edge, "evidence_refs": refs})

    source = _identifier(metadata.get("scope_id") or metadata.get("query_scope"))
    target = _identifier(
        metadata.get("parent_scope_id") or metadata.get("parent_query_scope")
    )
    if source and target:
        payloads.append(
            {
                "edge_type": "PARENT",
                "source_scope_id": source,
                "target_scope_id": target,
                "evidence_refs": [evidence_ref] if evidence_ref else [],
            }
        )
    return payloads


def _normalize_edge(
    raw: Mapping[str, Any],
    limitations: set[str],
) -> CompositionEdge | None:
    edge_type = _upper_identifier(raw.get("edge_type") or raw.get("type") or raw.get("relation"))
    source = _identifier(
        raw.get("source_scope_id")
        or raw.get("source_scope")
        or raw.get("source")
        or raw.get("from_scope")
    )
    target = _identifier(
        raw.get("target_scope_id")
        or raw.get("target_scope")
        or raw.get("target")
        or raw.get("to_scope")
    )
    if edge_type not in COMPOSITION_EDGE_TYPES:
        limitations.add("unsupported or missing explicit composition edge type")
        return None
    if not source or not target:
        limitations.add(f"composition edge {edge_type} missing endpoint")
        return None
    # A recursive CTE is the one valid composition self-edge: Phase 1 can
    # prove that the CTE producer consumes its own output.  Other self-edges
    # are structurally meaningless and remain rejected.
    if source == target and edge_type != "CTE_FEEDS":
        limitations.add(f"self-referential composition edge rejected: {edge_type}:{source}")
        return None
    refs_raw = raw.get("evidence_refs") or raw.get("diff_ids") or raw.get("mutation_test_ids") or []
    refs, truncated = _reference_items(refs_raw)
    if truncated:
        limitations.add(f"composition edge evidence refs truncated: {edge_type}:{source}:{target}")
    evidence_refs = tuple(sorted({_identifier(item) for item in refs if _identifier(item)}))
    return CompositionEdge(edge_type, source, target, evidence_refs)


def _normalize_parent_edge(
    raw: Mapping[str, Any],
    limitations: set[str],
) -> ParentEdge | None:
    edge_type = _upper_identifier(
        raw.get("edge_type") or raw.get("type") or raw.get("relation") or "PARENT"
    )
    source = _identifier(
        raw.get("source_scope_id")
        or raw.get("child_scope_id")
        or raw.get("source_scope")
        or raw.get("source")
        or raw.get("from_scope")
    )
    target = _identifier(
        raw.get("target_scope_id")
        or raw.get("parent_scope_id")
        or raw.get("target_scope")
        or raw.get("target")
        or raw.get("to_scope")
    )
    if edge_type != "PARENT":
        limitations.add("unsupported explicit parent edge type")
        return None
    if not source or not target:
        limitations.add("parent edge missing endpoint")
        return None
    if source == target:
        limitations.add(f"self-referential parent edge rejected: {source}")
        return None
    refs_raw = raw.get("evidence_refs") or raw.get("diff_ids") or []
    refs, truncated = _reference_items(refs_raw)
    if truncated:
        limitations.add(f"parent edge evidence refs truncated: {source}:{target}")
    evidence_refs = tuple(sorted({_identifier(item) for item in refs if _identifier(item)}))
    return ParentEdge(source, target, evidence_refs)


def _scope_metadata_parts(scope_metadata: Any) -> _ScopeMetadataParts:
    if isinstance(scope_metadata, Mapping):
        scopes, scopes_truncated = _bounded_items(
            scope_metadata.get("scopes") or scope_metadata.get("query_scopes")
        )
        edges, edges_truncated = _bounded_items(scope_metadata.get("composition_edges"))
        parents, parents_truncated = _bounded_items(scope_metadata.get("parent_edges"))
        raw_bindings = scope_metadata.get("diff_bindings")
        if isinstance(raw_bindings, Mapping):
            bindings = [
                {"diff_id": diff_id, "scope_id": scope_id}
                for diff_id, scope_id in raw_bindings.items()
            ]
            bindings_truncated = len(bindings) > MAX_METADATA_ITEMS_SCANNED
            bindings = bindings[:MAX_METADATA_ITEMS_SCANNED]
        else:
            bindings, bindings_truncated = _bounded_items(raw_bindings)
        upstream_limitations, limitations_truncated = _bounded_items(
            scope_metadata.get("limitations"), MAX_EVIDENCE_REFS
        )
        return _ScopeMetadataParts(
            scopes=tuple(scopes),
            composition_edges=tuple(edges),
            parent_edges=tuple(parents),
            diff_bindings=tuple(bindings),
            status=_upper_identifier(scope_metadata.get("status")),
            limitations=tuple(
                _bounded_text(item)
                for item in upstream_limitations
                if _bounded_text(item)
            ),
            bindings_declared="diff_bindings" in scope_metadata,
            truncated=(
                scopes_truncated
                or edges_truncated
                or parents_truncated
                or bindings_truncated
                or limitations_truncated
            ),
        )
    scopes, truncated = _bounded_items(scope_metadata)
    return _ScopeMetadataParts(scopes=tuple(scopes), truncated=truncated)


def _source_parts(source: Any) -> tuple[Any, Any, Any]:
    data = _mapping(_field(source, "data_evidence", {}))
    mutations = _field(source, "mutation_evidence", None)
    ast_diffs = data.get("ast_diffs")
    if ast_diffs is None:
        ast_diffs = _field(source, "ast_diffs", None)
    scope_metadata = (
        data.get("scoped_query_graph_input")
        or data.get("scope_graph")
        or data.get("scope_metadata")
        or data.get("query_scopes")
        or _field(source, "scope_graph", None)
        or _field(source, "scope_metadata", None)
    )
    if isinstance(scope_metadata, list):
        scope_metadata = {
            "scopes": scope_metadata,
            "composition_edges": data.get("composition_edges") or [],
        }
    elif scope_metadata is None and data.get("composition_edges"):
        scope_metadata = {"scopes": [], "composition_edges": data.get("composition_edges")}
    return ast_diffs, mutations, scope_metadata


def _kind_from_edge(edge_type: str, endpoint: str) -> str | None:
    if endpoint == "source":
        return {
            "CTE_FEEDS": "CTE",
            "DERIVED_FEEDS": "DERIVED",
            "SUBQUERY_OF": "SUBQUERY",
            "SET_MEMBER_OF": "SET_BRANCH",
        }.get(edge_type)
    return None


def _scope_sort_key(node: QueryScopeNode) -> tuple[int, str]:
    rank = {
        "ROOT": 0,
        "CTE": 1,
        "DERIVED": 2,
        "SUBQUERY": 3,
        "SET": 4,
        "SET_BRANCH": 5,
        "UNKNOWN": 6,
    }
    return rank.get(node.scope_kind, 99), node.scope_id


def _topological_scope_order(
    nodes: Sequence[QueryScopeNode],
    edges: Sequence[CompositionEdge],
    limitations: set[str],
) -> list[QueryScopeNode]:
    """Stable producer-before-consumer order over explicit composition edges."""

    by_id = {node.scope_id: node for node in nodes}
    adjacency: dict[str, set[str]] = {scope_id: set() for scope_id in by_id}
    indegree = {scope_id: 0 for scope_id in by_id}
    usable_edge_count = 0
    for edge in edges:
        source = edge.source_scope_id
        target = edge.target_scope_id
        if source == target:
            # Recursive CTE evidence remains serialized, but a self-loop must
            # not prevent a stable topological projection.
            continue
        if source not in by_id or target not in by_id:
            continue
        if target in adjacency[source]:
            continue
        adjacency[source].add(target)
        indegree[target] += 1
        usable_edge_count += 1

    if usable_edge_count == 0:
        return sorted(nodes, key=_scope_sort_key)

    ready: list[tuple[tuple[int, str], str]] = []
    for scope_id, degree in indegree.items():
        if degree == 0:
            heapq.heappush(ready, (_scope_sort_key(by_id[scope_id]), scope_id))

    ordered_ids: list[str] = []
    while ready:
        _, scope_id = heapq.heappop(ready)
        ordered_ids.append(scope_id)
        for target in sorted(adjacency[scope_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, (_scope_sort_key(by_id[target]), target))

    if len(ordered_ids) != len(by_id):
        limitations.add("composition graph cycle prevents complete topological ordering")
        emitted = set(ordered_ids)
        ordered_ids.extend(
            node.scope_id
            for node in sorted(nodes, key=_scope_sort_key)
            if node.scope_id not in emitted
        )
    return [by_id[scope_id] for scope_id in ordered_ids]


def build_scoped_query_graph(
    source: Any = None,
    *,
    ast_diffs: Any = None,
    mutation_metadata: Any = None,
    scope_metadata: Any = None,
    max_diffs: int = MAX_DIFFS,
    max_scopes: int = MAX_SCOPES,
    max_edges: int = MAX_EDGES,
) -> ScopedQueryGraph:
    """Build a deterministic scope graph without parsing or executing SQL.

    ``source`` may be a Phase 1 ``SandboxRun`` or a mapping with
    ``data_evidence``/``mutation_evidence``.  Explicit keyword inputs override
    the corresponding source fields.  Limits can only reduce the hard caps.
    """

    source_diffs, source_mutations, source_scopes = _source_parts(source)
    if ast_diffs is None:
        ast_diffs = source_diffs
    if mutation_metadata is None:
        mutation_metadata = source_mutations
    if scope_metadata is None:
        scope_metadata = source_scopes

    limitations: set[str] = set()
    truncated = False
    diff_limit = _bounded_limit(max_diffs, MAX_DIFFS, "max_diffs", limitations)
    scope_limit = _bounded_limit(max_scopes, MAX_SCOPES, "max_scopes", limitations)
    edge_limit = _bounded_limit(max_edges, MAX_EDGES, "max_edges", limitations)

    metadata_parts = _scope_metadata_parts(scope_metadata)
    if metadata_parts.status == "PARTIAL":
        limitations.add("upstream scope metadata status is PARTIAL")
    elif metadata_parts.status and metadata_parts.status != "COMPLETE":
        limitations.add(f"unsupported upstream scope metadata status: {metadata_parts.status}")
    for item in metadata_parts.limitations:
        limitations.add(f"upstream scope metadata: {item}")
    if metadata_parts.truncated:
        limitations.add("scope metadata limit reached")
        truncated = True

    contract_bindings, non_exact_contract_bindings = _contract_scope_bindings(
        metadata_parts.diff_bindings, limitations
    )

    raw_diffs, scan_truncated = _bounded_items(ast_diffs)
    truncated |= scan_truncated
    if scan_truncated:
        limitations.add("AST diff metadata scan limit reached")
    input_diff_count = len(raw_diffs)
    if len(raw_diffs) > diff_limit:
        limitations.add(f"AST diff limit reached: retained {diff_limit}")
        truncated = True

    mutation_bindings, normalized_mutations = _mutation_scope_bindings(
        mutation_metadata, limitations
    )
    flattened_diffs: list[tuple[str, dict[str, Any]]] = []
    for raw in raw_diffs:
        flattened = _flatten_diff(raw)
        diff_id = _explicit_diff_id(raw, flattened)
        if not _identifier(flattened.get("diff_id")):
            limitations.add(f"stable diff_id missing; generated bounded ID {diff_id}")
        flattened_diffs.append((diff_id, flattened))

    # Canonical selection makes ordinary input permutations produce the same
    # result.  Conflicting duplicate IDs choose no arbitrary first record.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for diff_id, metadata in flattened_diffs:
        grouped.setdefault(diff_id, []).append(metadata)
    selected: list[tuple[str, dict[str, Any]]] = []
    for diff_id in sorted(grouped)[:diff_limit]:
        variants = grouped[diff_id]
        canonical_variants = [(_canonical_json(item), item) for item in variants]
        canonical_variants.sort(key=lambda item: item[0])
        if len({item[0] for item in canonical_variants}) > 1:
            limitations.add(f"conflicting metadata for duplicate diff_id {diff_id}")
        selected.append((diff_id, canonical_variants[0][1]))

    accumulators: dict[str, _ScopeAccumulator] = {}
    edge_payloads: list[dict[str, Any]] = []
    parent_edge_payloads: list[dict[str, Any]] = []
    diff_locations: dict[str, tuple[_ExactDiffBinding, ...]] = {}

    def ensure_scope(
        scope_id: str,
        kind: str,
        *,
        declared: bool = False,
        correlated: bool = False,
        side: str = "",
        conceptual_scope_id: str = "",
        upstream_complete: bool = True,
    ) -> None:
        if not scope_id:
            return
        accumulator = accumulators.setdefault(scope_id, _ScopeAccumulator())
        if kind in SCOPE_KINDS:
            accumulator.kinds.add(kind)
        accumulator.explicitly_declared |= declared
        accumulator.correlated |= correlated
        if side:
            accumulator.sides.add(side)
        if conceptual_scope_id:
            accumulator.conceptual_scope_ids.add(conceptual_scope_id)
        accumulator.upstream_complete &= upstream_complete

    for raw in metadata_parts.scopes:
        declaration = _mapping(raw)
        scope_id = _identifier(declaration.get("scope_id") or declaration.get("query_scope"))
        if not scope_id:
            limitations.add("scope declaration missing scope_id")
            continue
        declaration_side = str(declaration.get("side") or "").strip().lower()[:32]
        declaration_conceptual_scope_id = _identifier(
            declaration.get("conceptual_scope_id")
        )
        side_supported = not declaration_side or declaration_side in {
            "standard",
            "student",
        }
        if not side_supported:
            limitations.add(f"unsupported scope side metadata for {scope_id}")
        conceptual_complete = not (
            metadata_parts.bindings_declared
            and declaration_side
            and not declaration_conceptual_scope_id
        )
        if not conceptual_complete:
            limitations.add(f"conceptual scope metadata missing for {scope_id}")
        ensure_scope(
            scope_id,
            _scope_kind(declaration, scope_id),
            declared=True,
            correlated=declaration.get("is_correlated") is True,
            side=declaration_side,
            conceptual_scope_id=declaration_conceptual_scope_id,
            upstream_complete=(
                declaration.get("metadata_complete") is not False
                and side_supported
                and conceptual_complete
            ),
        )
        if declaration.get("metadata_complete") is False:
            limitations.add(f"upstream scope metadata incomplete for {scope_id}")
        edge_payloads.extend(_edge_payloads(declaration, f"scope:{scope_id}"))
        parent_edge_payloads.extend(
            _parent_edge_payloads(declaration, f"scope:{scope_id}")
        )
    edge_payloads.extend(
        _mapping(item)
        for item in metadata_parts.composition_edges
        if isinstance(item, Mapping)
    )
    parent_edge_payloads.extend(
        _mapping(item)
        for item in metadata_parts.parent_edges
        if isinstance(item, Mapping)
    )

    for diff_id, metadata in selected:
        exact_bindings = contract_bindings.get(diff_id, set())
        if exact_bindings:
            locations = sorted(exact_bindings, key=lambda item: (item.scope_id, item.side))
        elif diff_id in non_exact_contract_bindings:
            # Phase 1 explicitly says the label was a fallback, so do not
            # silently upgrade that same legacy query_scope label to proof.
            locations = [_ExactDiffBinding(f"unscoped:{diff_id}"[:MAX_IDENTIFIER_LENGTH])]
            limitations.add(f"query_scope lacks exact binding for diff {diff_id}")
        elif metadata_parts.bindings_declared:
            locations = [_ExactDiffBinding(f"unscoped:{diff_id}"[:MAX_IDENTIFIER_LENGTH])]
            limitations.add(f"exact scope binding missing for diff {diff_id}")
        else:
            legacy_scope_id = _scope_id_for_diff(
                metadata, diff_id, mutation_bindings, limitations
            )
            legacy_conceptual_scope_id = _identifier(
                metadata.get("conceptual_scope_id")
            )
            if (
                not legacy_conceptual_scope_id
                and not legacy_scope_id.startswith("unscoped:")
            ):
                legacy_conceptual_scope_id = legacy_scope_id
            locations = [
                _ExactDiffBinding(
                    legacy_scope_id,
                    "",
                    legacy_conceptual_scope_id,
                )
            ]
        diff_locations[diff_id] = tuple(locations)
        for binding in locations:
            scope_id = binding.scope_id
            kind = _scope_kind(metadata, scope_id)
            ensure_scope(
                scope_id,
                kind,
                correlated=metadata.get("is_correlated") is True,
                side=binding.side,
                conceptual_scope_id=binding.conceptual_scope_id,
            )
            accumulators[scope_id].diff_ids_by_stage[_logical_stage(metadata)].add(diff_id)
            scoped_metadata = {**metadata, "scope_id": scope_id}
            edge_payloads.extend(_edge_payloads(scoped_metadata, diff_id))
            parent_edge_payloads.extend(
                _parent_edge_payloads(scoped_metadata, diff_id)
            )

    all_diff_ids = set(grouped)
    for bound_diff_id in sorted(contract_bindings):
        if bound_diff_id not in all_diff_ids:
            limitations.add(
                f"explicit diff binding references missing AST diff {bound_diff_id}"
            )

    for mutation, test_id in normalized_mutations:
        if metadata_parts.bindings_declared:
            # The side-aware Phase 1 scope contract is authoritative.  Legacy
            # mutation ``query_scope`` labels are side-neutral fallbacks and
            # must not create a third, merged scope beside the exact pair.
            continue
        scope_id = _identifier(mutation.get("scope_id") or mutation.get("query_scope"))
        if scope_id:
            ensure_scope(
                scope_id,
                _scope_kind(mutation, scope_id),
                correlated=mutation.get("is_correlated") is True,
            )
        edge_payloads.extend(_edge_payloads(mutation, test_id))
        parent_edge_payloads.extend(_parent_edge_payloads(mutation, test_id))

    normalized_edges: dict[tuple[str, str, str], set[str]] = {}
    edge_scan = edge_payloads[:MAX_METADATA_ITEMS_SCANNED]
    if len(edge_payloads) > MAX_METADATA_ITEMS_SCANNED:
        limitations.add("composition edge metadata scan limit reached")
        truncated = True
    for payload in edge_scan:
        edge = _normalize_edge(payload, limitations)
        if edge is None:
            continue
        key = (edge.edge_type, edge.source_scope_id, edge.target_scope_id)
        normalized_edges.setdefault(key, set()).update(edge.evidence_refs)
        source_kind = _kind_from_edge(edge.edge_type, "source") or _scope_kind_from_id(
            edge.source_scope_id
        )
        ensure_scope(edge.source_scope_id, source_kind)
        ensure_scope(edge.target_scope_id, _scope_kind_from_id(edge.target_scope_id))

    edge_objects: list[CompositionEdge] = []
    for key, refs in sorted(normalized_edges.items()):
        if len(refs) > MAX_EVIDENCE_REFS:
            limitations.add(
                f"composition edge evidence refs truncated: {key[0]}:{key[1]}:{key[2]}"
            )
        edge_objects.append(
            CompositionEdge(
                edge_type=key[0],
                source_scope_id=key[1],
                target_scope_id=key[2],
                evidence_refs=tuple(sorted(refs)[:MAX_EVIDENCE_REFS]),
            )
        )

    normalized_parents: dict[tuple[str, str], set[str]] = {}
    parent_scan = parent_edge_payloads[:MAX_METADATA_ITEMS_SCANNED]
    if len(parent_edge_payloads) > MAX_METADATA_ITEMS_SCANNED:
        limitations.add("parent edge metadata scan limit reached")
        truncated = True
    for payload in parent_scan:
        parent_edge = _normalize_parent_edge(payload, limitations)
        if parent_edge is None:
            continue
        key = (parent_edge.source_scope_id, parent_edge.target_scope_id)
        normalized_parents.setdefault(key, set()).update(parent_edge.evidence_refs)
        ensure_scope(
            parent_edge.source_scope_id,
            _scope_kind_from_id(parent_edge.source_scope_id),
        )
        ensure_scope(
            parent_edge.target_scope_id,
            _scope_kind_from_id(parent_edge.target_scope_id),
        )
    parent_objects: list[ParentEdge] = []
    for key, refs in sorted(normalized_parents.items()):
        if len(refs) > MAX_EVIDENCE_REFS:
            limitations.add(f"parent edge evidence refs truncated: {key[0]}:{key[1]}")
        parent_objects.append(
            ParentEdge(
                source_scope_id=key[0],
                target_scope_id=key[1],
                evidence_refs=tuple(sorted(refs)[:MAX_EVIDENCE_REFS]),
            )
        )

    total_edges = len(edge_objects) + len(parent_objects)
    if total_edges > edge_limit:
        # Composition edges drive execution semantics, so retain their stable
        # prefix first and spend only the remaining budget on lexical parents.
        retained_composition = min(len(edge_objects), edge_limit)
        edge_objects = edge_objects[:retained_composition]
        parent_objects = parent_objects[: max(0, edge_limit - retained_composition)]
        limitations.add(f"edge limit reached: retained {edge_limit} total")
        truncated = True

    referenced_edges = {
        edge.source_scope_id for edge in edge_objects
    } | {edge.target_scope_id for edge in edge_objects} | {
        edge.source_scope_id for edge in parent_objects
    } | {edge.target_scope_id for edge in parent_objects}
    outgoing_types: dict[str, set[str]] = {}
    for edge in edge_objects:
        outgoing_types.setdefault(edge.source_scope_id, set()).add(edge.edge_type)

    conceptual_bindings: list[ConceptualBinding] = []
    for diff_id, _ in selected:
        locations = diff_locations.get(diff_id, ())
        scope_ids = tuple(sorted({item.scope_id for item in locations}))
        resolved_conceptual_ids: list[str] = []
        for location in locations:
            conceptual_scope_id = location.conceptual_scope_id
            if not conceptual_scope_id:
                accumulator = accumulators.get(location.scope_id)
                if accumulator and len(accumulator.conceptual_scope_ids) == 1:
                    conceptual_scope_id = next(iter(accumulator.conceptual_scope_ids))
            if conceptual_scope_id:
                resolved_conceptual_ids.append(conceptual_scope_id)

        conceptual_id_set = set(resolved_conceptual_ids)
        if metadata_parts.bindings_declared:
            sides = {item.side for item in locations if item.side}
            exact_pair = (
                bool(contract_bindings.get(diff_id))
                and len(locations) == 2
                and sides == {"standard", "student"}
                and len(resolved_conceptual_ids) == len(locations)
                and len(conceptual_id_set) == 1
            )
            if exact_pair:
                conceptual_scope_id = next(iter(conceptual_id_set))
                binding_status = "EXACT_PAIRED"
            else:
                conceptual_scope_id = None
                binding_status = "PARTIAL"
                limitations.add(
                    f"paired conceptual scope binding incomplete for diff {diff_id}"
                )
        elif len(locations) == 1 and len(conceptual_id_set) == 1:
            conceptual_scope_id = next(iter(conceptual_id_set))
            binding_status = "LEGACY_SIDE_NEUTRAL"
        else:
            conceptual_scope_id = None
            binding_status = "PARTIAL"
            limitations.add(f"conceptual scope binding unavailable for diff {diff_id}")
        conceptual_bindings.append(
            ConceptualBinding(
                diff_id=diff_id,
                conceptual_scope_id=conceptual_scope_id,
                scope_ids=scope_ids,
                binding_status=binding_status,
            )
        )

    nodes: list[QueryScopeNode] = []
    for scope_id, accumulator in accumulators.items():
        scope_complete = accumulator.upstream_complete
        non_unknown = accumulator.kinds - {"UNKNOWN"}
        if len(non_unknown) > 1:
            kind = "UNKNOWN"
            limitations.add(f"conflicting scope_kind metadata for {scope_id}")
            scope_complete = False
        elif non_unknown:
            kind = next(iter(non_unknown))
        else:
            kind = "UNKNOWN"
            limitations.add(f"scope_kind missing or ambiguous for {scope_id}")
            scope_complete = False

        if len(accumulator.sides) > 1:
            side = None
            limitations.add(f"conflicting scope side metadata for {scope_id}")
            scope_complete = False
        elif accumulator.sides:
            side = next(iter(accumulator.sides))
        else:
            side = None

        if len(accumulator.conceptual_scope_ids) > 1:
            conceptual_scope_id = None
            limitations.add(f"conflicting conceptual scope metadata for {scope_id}")
            scope_complete = False
        elif accumulator.conceptual_scope_ids:
            conceptual_scope_id = next(iter(accumulator.conceptual_scope_ids))
        elif metadata_parts.bindings_declared and side:
            conceptual_scope_id = None
            limitations.add(f"conceptual scope metadata missing for {scope_id}")
            scope_complete = False
        elif side is None and not scope_id.startswith("unscoped:"):
            conceptual_scope_id = scope_id
        else:
            conceptual_scope_id = None

        required_edges = {
            "CTE": {"CTE_FEEDS"},
            "DERIVED": {"DERIVED_FEEDS"},
            "SUBQUERY": {"SUBQUERY_OF"},
            "SET_BRANCH": {"SET_MEMBER_OF"},
        }.get(kind, set())
        present = outgoing_types.get(scope_id, set())
        if kind != "ROOT" and required_edges and not (required_edges & present):
            limitations.add(f"explicit composition metadata missing for scope {scope_id}")
            scope_complete = False
        if accumulator.correlated and "CORRELATED_TO" not in present:
            limitations.add(f"explicit correlation target missing for scope {scope_id}")
            scope_complete = False
        if kind != "ROOT" and not required_edges and scope_id not in referenced_edges:
            limitations.add(
                f"scope {scope_id} is disconnected without explicit composition metadata"
            )
            scope_complete = False

        stages = tuple(
            ScopeStage(stage, tuple(sorted(accumulator.diff_ids_by_stage[stage])))
            for stage in LOGICAL_STAGE_ORDER
        )
        nodes.append(
            QueryScopeNode(
                scope_id,
                kind,
                side,
                conceptual_scope_id,
                stages,
                scope_complete,
            )
        )

    nodes = _topological_scope_order(nodes, edge_objects, limitations)
    if len(nodes) > scope_limit:
        retained_ids = {node.scope_id for node in nodes[:scope_limit]}
        nodes = nodes[:scope_limit]
        edge_objects = [
            edge
            for edge in edge_objects
            if edge.source_scope_id in retained_ids and edge.target_scope_id in retained_ids
        ][:edge_limit]
        parent_objects = [
            edge
            for edge in parent_objects
            if edge.source_scope_id in retained_ids and edge.target_scope_id in retained_ids
        ][: max(0, edge_limit - len(edge_objects))]
        limitations.add(f"scope limit reached: retained {scope_limit}")
        truncated = True
    if not nodes:
        limitations.add("no explicit query scope evidence available")

    ordered_limitations = tuple(sorted(limitations))
    return ScopedQueryGraph(
        status="PARTIAL" if ordered_limitations or truncated else "COMPLETE",
        scopes=tuple(nodes),
        composition_edges=tuple(edge_objects),
        parent_edges=tuple(parent_objects),
        conceptual_bindings=tuple(conceptual_bindings),
        limitations=ordered_limitations,
        input_diff_count=input_diff_count,
        retained_diff_count=len(selected),
        truncated=truncated,
    )


__all__ = [
    "COMPOSITION_EDGE_TYPES",
    "ConceptualBinding",
    "CompositionEdge",
    "LOGICAL_STAGE_ORDER",
    "MAX_DIFFS",
    "MAX_EDGES",
    "MAX_SCOPES",
    "ParentEdge",
    "QueryScopeNode",
    "SCHEMA_VERSION",
    "SCOPE_KINDS",
    "ScopeStage",
    "ScopedQueryGraph",
    "build_scoped_query_graph",
]
