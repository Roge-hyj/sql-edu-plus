"""Generate the bounded Phase 1 formalization from machine-readable contracts.

This artifact is intentionally a partial current-implementation formalization.
It describes the predicates and bounds that the running code exposes; it does
not promote an unverified capability or claim global SQL equivalence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CURRENT_CONTRACT = PROJECT_ROOT / "contracts/phase1_current_implementation.json"
TARGET_CONTRACT = PROJECT_ROOT / "contracts/phase1_product_target.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data_construct_test/outputs/phase1_current_formalization.json"

# The current implementation contract retains the internal names used by the
# runtime capability inventory.  The product target uses the formal names
# ENGINE/RESOURCE; keep the translation explicit and machine-readable rather
# than silently producing unmapped formal layers.
DEFAULT_LAYER_ALIASES = {
    "EXECUTOR": "ENGINE",
    "RESOURCES": "RESOURCE",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_cfg_grammar(target: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    relative_path = str(target["formalization"]["cfg_grammar_artifact"])
    grammar_path = (PROJECT_ROOT / relative_path).resolve()
    if PROJECT_ROOT not in grammar_path.parents:
        raise ValueError("CFG grammar artifact must remain inside the repository")
    grammar = json.loads(grammar_path.read_text(encoding="utf-8"))
    return grammar, grammar_path


def _validate_cfg_grammar(grammar: dict[str, Any], target: dict[str, Any]) -> None:
    nonterminals = list(grammar.get("nonterminals") or [])
    terminals = list(grammar.get("terminals") or [])
    if not nonterminals or not terminals:
        raise ValueError("CFG grammar must declare nonterminals and terminals")
    if len(nonterminals) != len(set(nonterminals)):
        raise ValueError("CFG nonterminals must be unique")
    if len(terminals) != len(set(terminals)):
        raise ValueError("CFG terminals must be unique")
    nonterminal_set = set(nonterminals)
    terminal_set = set(terminals)
    if nonterminal_set & terminal_set:
        raise ValueError("CFG terminals and nonterminals must be disjoint")
    if grammar.get("start_symbol") not in nonterminal_set:
        raise ValueError("CFG start symbol must be a declared nonterminal")
    if grammar.get("query_start_symbol") not in nonterminal_set:
        raise ValueError("CFG query start symbol must be a declared nonterminal")
    if list(grammar.get("submission_shape") or []) != ["Query", "Query", "SchemaText"]:
        raise ValueError("CFG submission shape must remain Query x Query x SchemaText")

    productions = list(grammar.get("productions") or [])
    production_ids = [str(item.get("id") or "") for item in productions]
    if not productions or any(not item_id for item_id in production_ids):
        raise ValueError("CFG productions require stable ids")
    if len(production_ids) != len(set(production_ids)):
        raise ValueError("CFG production ids must be unique")
    symbols = nonterminal_set | terminal_set
    for production in productions:
        if production.get("lhs") not in nonterminal_set:
            raise ValueError(f"CFG production has undeclared lhs: {production.get('id')}")
        if any(symbol not in symbols for symbol in production.get("rhs") or []):
            raise ValueError(f"CFG production has undeclared rhs symbol: {production.get('id')}")

    allowed_families = set(target["support_boundary"]["allowed_feature_families"])
    allowed_families.update({"parser.strict_single_dql", "schema.compact_and_catalog"})
    if any(production.get("feature_family") not in allowed_families for production in productions):
        raise ValueError("CFG production is outside the target feature-family boundary")
    bindings = grammar.get("feature_family_bindings") or {}
    missing_bindings = set(target["support_boundary"]["allowed_feature_families"]) - set(bindings)
    if missing_bindings:
        raise ValueError(f"CFG feature families lack bindings: {sorted(missing_bindings)}")
    production_id_set = set(production_ids)
    if any(
        production_id not in production_id_set
        for production_ids_for_family in bindings.values()
        for production_id in production_ids_for_family
    ):
        raise ValueError("CFG feature-family binding references an unknown production")


def _validate_capability_bindings(
    current: dict[str, Any],
    grammar: dict[str, Any],
    *,
    layer_aliases: dict[str, str],
    required_layers: list[str],
) -> dict[str, dict[str, Any]]:
    """Require every current capability to have one machine-readable binding.

    Runtime layers account for the ordinary parser/IR/schema/witness/engine/
    resource/verdict capabilities.  FEATURE capabilities must additionally
    name the CFG feature families they own, the IR capability may own a purely
    structural family, and FREEZE is represented by the explicit gate fields.
    This prevents ``unmapped_layers=[]`` from hiding unbound feature IDs.
    """
    current_by_id = {str(item["id"]): item for item in current["capabilities"]}
    current_ids = set(current_by_id)
    bindings = dict(grammar.get("capability_bindings") or {})
    binding_ids = set(bindings)
    unknown_bindings = binding_ids - current_ids
    if unknown_bindings:
        raise ValueError(
            "CFG capability bindings reference unknown current capabilities: "
            f"{sorted(unknown_bindings)}"
        )

    canonical_layers = {
        capability_id: layer_aliases.get(str(item["layer"]), str(item["layer"]))
        for capability_id, item in current_by_id.items()
    }
    missing_required_layers = set(required_layers) - set(canonical_layers.values())
    # Constraint-only layers are allowed to have no runtime capability IDs.
    constraint_only = {
        layer
        for layer in required_layers
        if layer in (current.get("formalization_constraints") or {})
    }
    if missing_required_layers - constraint_only:
        raise ValueError(
            "current contract has no capability or formalization constraint for "
            f"required layers: {sorted(missing_required_layers - constraint_only)}"
        )

    explicitly_bound = {
        capability_id
        for capability_id, layer in canonical_layers.items()
        if layer in {"FEATURE", "FREEZE"}
    }
    # General expressions are a structural CFG family, not a separate product
    # feature capability; bind that family explicitly to IR/ASTDiff as well.
    explicitly_bound.add("ir.sql_structure_and_astdiff")
    missing_explicit = explicitly_bound - binding_ids
    if missing_explicit:
        raise ValueError(
            "feature/freeze/structural capabilities lack explicit CFG bindings: "
            f"{sorted(missing_explicit)}"
        )

    family_bindings = dict(grammar.get("feature_family_bindings") or {})
    family_owners: dict[str, str] = {}
    expanded: dict[str, dict[str, Any]] = {}
    productions = list(grammar.get("productions") or [])
    for capability_id, capability in current_by_id.items():
        canonical_layer = canonical_layers[capability_id]
        binding = dict(bindings.get(capability_id) or {})
        binding_kind = str(binding.get("binding_kind") or "runtime_layer")
        if capability_id not in bindings:
            if canonical_layer in {"FEATURE", "FREEZE"}:
                raise ValueError(
                    f"{capability_id} requires an explicit CFG/freeze binding"
                )
            expanded[capability_id] = {
                "layer": canonical_layer,
                "binding_kind": binding_kind,
                "status": capability.get("status"),
                "verification_status": capability.get("verification_status"),
            }
            continue

        if binding_kind == "cfg_feature_families":
            if canonical_layer not in {"FEATURE", "IR_ASTDIFF"}:
                raise ValueError(
                    f"{capability_id} cannot own CFG feature families from layer "
                    f"{canonical_layer}"
                )
            families = [str(item) for item in binding.get("feature_families") or []]
            if not families:
                raise ValueError(f"{capability_id} must bind at least one CFG family")
            unknown_families = set(families) - set(family_bindings)
            if unknown_families:
                raise ValueError(
                    f"{capability_id} references unknown CFG families: "
                    f"{sorted(unknown_families)}"
                )
            for family in families:
                previous = family_owners.get(family)
                if previous and previous != capability_id:
                    raise ValueError(
                        f"CFG family {family} is multiply owned by {previous} and "
                        f"{capability_id}"
                    )
                family_owners[family] = capability_id
            binding["production_ids"] = [
                str(production["id"])
                for production in productions
                if production.get("feature_family") in families
            ]
            if not binding["production_ids"]:
                raise ValueError(f"{capability_id} binds no CFG productions")
        elif binding_kind == "freeze_gate":
            if canonical_layer != "FREEZE":
                raise ValueError(
                    f"{capability_id} freeze binding must belong to FREEZE, got "
                    f"{canonical_layer}"
                )
            gate_fields = [str(item) for item in binding.get("gate_fields") or []]
            missing_gate_fields = set(gate_fields) - set(current["freeze_baseline"])
            if not gate_fields or missing_gate_fields:
                raise ValueError(
                    f"{capability_id} has invalid freeze gate fields: "
                    f"{sorted(missing_gate_fields)}"
                )
            if binding.get("machine_predicate") != current["scope_model"][
                "FrozenPairScope"
            ]["machine_predicate"]:
                raise ValueError(
                    f"{capability_id} freeze predicate diverges from FrozenPairScope"
                )
        else:
            raise ValueError(
                f"{capability_id} has unsupported binding kind {binding_kind!r}"
            )

        expanded[capability_id] = {
            "layer": canonical_layer,
            "status": capability.get("status"),
            "verification_status": capability.get("verification_status"),
            **binding,
        }

    missing_families = set(family_bindings) - set(family_owners)
    if missing_families:
        raise ValueError(
            "CFG feature families lack capability ownership: "
            f"{sorted(missing_families)}"
        )
    if set(expanded) != current_ids:
        raise ValueError(
            "formal capability binding coverage diverges from current contract: "
            f"missing={sorted(current_ids - set(expanded))}, "
            f"extra={sorted(set(expanded) - current_ids)}"
        )
    return expanded


def build_formalization(
    current: dict[str, Any],
    target: dict[str, Any],
    *,
    current_sha256: str,
    current_path: str,
    target_sha256: str,
    target_path: str,
) -> dict[str, Any]:
    cfg_grammar, cfg_grammar_path = _load_cfg_grammar(target)
    _validate_cfg_grammar(cfg_grammar, target)
    cfg_boundary = dict(current.get("cfg_implementation_boundary") or {})
    grammar_boundary = dict(cfg_grammar.get("implementation_boundary") or {})
    grammar_boundary.update(
        {
            "start_symbol": cfg_grammar.get("start_symbol"),
            "query_start_symbol": cfg_grammar.get("query_start_symbol"),
            "submission_shape": cfg_grammar.get("submission_shape"),
        }
    )
    for key in (
        "start_symbol",
        "query_start_symbol",
        "submission_shape",
        "parser_predicate",
        "structural_predicate",
        "semantic_predicate",
        "cfg_acceptance_does_not_imply",
        "unrepresentable_or_unsupported_is",
    ):
        if cfg_boundary.get(key) != grammar_boundary.get(key):
            raise ValueError(f"current CFG implementation boundary diverges for {key}")
    required_layers = list(target["formalization"]["required_layers"])
    capabilities_by_layer: dict[str, list[str]] = {
        layer: [] for layer in required_layers
    }
    layer_aliases = {
        **DEFAULT_LAYER_ALIASES,
        **dict(current.get("formalization_layer_aliases") or {}),
    }
    capability_bindings = _validate_capability_bindings(
        current,
        cfg_grammar,
        layer_aliases=layer_aliases,
        required_layers=required_layers,
    )
    formalization_constraints = dict(current.get("formalization_constraints") or {})
    for capability in current["capabilities"]:
        canonical_layer = layer_aliases.get(capability["layer"], capability["layer"])
        capabilities_by_layer.setdefault(canonical_layer, []).append(
            capability["id"]
        )

    status_counts = {
        layer: dict(
            Counter(
                capability["status"]
                for capability in current["capabilities"]
                if layer_aliases.get(capability["layer"], capability["layer"]) == layer
            )
        )
        for layer in required_layers
    }
    unmapped_layers = [
        layer
        for layer in required_layers
        if not capabilities_by_layer.get(layer)
        and not formalization_constraints.get(layer)
    ]
    freeze = current["freeze_baseline"]
    return {
        "schema_version": "phase1.current-formalization.v1",
        "formalization_status": "PARTIAL_CURRENT_IMPLEMENTATION",
        "generated_from": {
            "current_contract": current_path,
            "current_contract_sha256": current_sha256,
            "target_contract": target_path,
            "target_contract_sha256": target_sha256,
        },
        "predicates": {
            "DeclaredSupport": current["scope_model"]["PolicyScope"]["machine_predicate"],
            "RunnableSupport": current["scope_model"]["RunnableScope"]["machine_predicate"],
            "FrozenPairSupport": current["scope_model"]["FrozenPairScope"]["machine_predicate"],
        },
        "scope_relation": current["scope_model"]["relation"],
        "cfg_grammar_artifact": str(cfg_grammar_path.relative_to(PROJECT_ROOT)),
        "cfg_grammar_sha256": _sha256(cfg_grammar_path),
        "cfg_grammar": cfg_grammar,
        "cfg_implementation_boundary": cfg_boundary,
        "layer_aliases": layer_aliases,
        "formalization_constraints": formalization_constraints,
        "capability_bindings": capability_bindings,
        "capability_coverage": {
            "current_capability_ids": sorted(capabilities_by_id := {
                str(item["id"]) for item in current["capabilities"]
            }),
            "bound_capability_ids": sorted(capability_bindings),
            "unmapped_capability_ids": sorted(
                capabilities_by_id - set(capability_bindings)
            ),
        },
        "required_layers": required_layers,
        "layers": {
            layer: {
                "capability_ids": capabilities_by_layer.get(layer, []),
                "status_counts": status_counts[layer],
                "materialization": (
                    "runtime_capabilities"
                    if capabilities_by_layer.get(layer)
                    else "derived_formalization_constraints"
                    if formalization_constraints.get(layer)
                    else "not_separately_materialized"
                ),
                "constraint_ids": [
                    str(item.get("id"))
                    for item in formalization_constraints.get(layer, [])
                    if item.get("id")
                ],
            }
            for layer in required_layers
        },
        "unmapped_layers": unmapped_layers,
        "resource_bounds": current["resource_policy"],
        "engine_versions": current["databases"],
        "verdict_projection": current["verdict_mapping"],
        "freeze_gate": {
            "generation_failures": freeze["generation_failures"],
            "determinate_label_mismatches": freeze["determinate_label_mismatches"],
            "repeat_run_stable": freeze["repeat_run_stable"],
            "acceptance_pass": freeze["acceptance_pass"],
            "promotion_blocked": (
                freeze["generation_failures"] != 0
                or freeze["determinate_label_mismatches"] != 0
                or not freeze["repeat_run_stable"]
                or not freeze["acceptance_pass"]
            ),
        },
        "consistency_rules": [
            "formalized capability ids must come from the current implementation contract",
            "resource_bounds must come from current_contract.resource_policy",
            "verdict_projection must come from current_contract.verdict_mapping",
            "cfg grammar symbols and productions must be closed and remain inside target feature families",
            "current capability ids must have exactly one runtime-layer or explicit CFG/freeze binding",
            "every CFG feature family must have exactly one capability owner",
            "current_contract.cfg_implementation_boundary must equal the CFG artifact implementation boundary",
            "no capability is VERIFIED while the freeze gate is blocked",
            "this artifact is not a global SQL equivalence proof",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-contract", type=Path, default=CURRENT_CONTRACT)
    parser.add_argument("--target-contract", type=Path, default=TARGET_CONTRACT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    current = json.loads(args.current_contract.read_text(encoding="utf-8"))
    target = json.loads(args.target_contract.read_text(encoding="utf-8"))
    payload = build_formalization(
        current,
        target,
        current_sha256=_sha256(args.current_contract),
        current_path=str(args.current_contract.relative_to(PROJECT_ROOT)),
        target_sha256=_sha256(args.target_contract),
        target_path=str(args.target_contract.relative_to(PROJECT_ROOT)),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
