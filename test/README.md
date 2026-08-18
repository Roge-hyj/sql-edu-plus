# Phase 1 Capability Tests

This folder records the test process used to quantify the Phase 1 SQL judgment system.

The tests are organized by system block instead of as one opaque end-to-end score.

## Layout

- `plan1.md`: Phase 1 structure capability evaluation plan.
- `phase1_structure/supported_sql_fragment_cfg.md`: CFG scope for the IR benchmark.
- `phase1_structure/`: IR and AST structure-recognition tests.
- `phase1_data_generation/`: planned counterexample database generation tests.
- `phase1_mutation/`: planned mutation and ablation tests.
- `phase1_sandbox/`: planned SQL execution and result-comparison tests.
- `phase1_attribution/`: planned evidence fusion and attribution tests.

## Current Test

Run the complete strict Phase 1 gate from the project root. It runs the
structure reports, curated end-to-end samples, strict data-generation suite,
deterministic fuzzer, and focused backend regression tests in sequence. It
uses the current Python executable and does not start Docker or external
database services:

```bash
python data_construct_test/scripts/run_phase1_full_flow_gate.py
```

The gate stops at the first failed command or report invariant and returns a
non-zero exit code.

Run the IR structure capability benchmark:

```bash
python test/phase1_structure/run_phase1_ir_structure_capability.py
```

Run the AST diff capability benchmark:

```bash
python test/phase1_structure/run_phase1_ast_diff_capability.py
```

Generate AST diff pairs from all IR cases:

```bash
python test/phase1_structure/run_phase1_ast_diff_from_ir_cases.py
```

The script writes reports and evidence to:

- `test/phase1_structure/outputs/phase1_ir_structure_cases.jsonl`
- `test/phase1_structure/outputs/phase1_ir_structure_detailed_evidence.jsonl`
- `test/phase1_structure/outputs/phase1_ir_structure_capability.json`
- `test/phase1_structure/outputs/phase1_ir_structure_capability.md`
- `test/phase1_structure/outputs/phase1_ast_diff_cases.jsonl`
- `test/phase1_structure/outputs/phase1_ast_diff_detailed_evidence.jsonl`
- `test/phase1_structure/outputs/phase1_ast_diff_capability.json`
- `test/phase1_structure/outputs/phase1_ast_diff_capability.md`
- `test/phase1_structure/outputs/phase1_ast_diff_from_ir_cases.jsonl`
- `test/phase1_structure/outputs/phase1_ast_diff_from_ir_detailed_evidence.jsonl`
- `test/phase1_structure/outputs/phase1_ast_diff_from_ir_capability.json`
- `test/phase1_structure/outputs/phase1_ast_diff_from_ir_capability.md`

## Current Result

The current IR benchmark covers 77 CFG production-alternative cases:

- 77/77 structures are captured by first-class typed IR fields.
- 0 structures are retained as weak textual evidence.
- 0 in-scope or near-scope structures are recorded as known gaps.
- 0 unexpected failures.
- Separately, 6 cases are marked as SQLite execution boundaries:
  `GROUPING SETS`, `LATERAL`, `ROLLUP`, `CUBE`, `INTERSECT ALL`, and
  `EXCEPT ALL`. These remain part of the 77/77 typed structure result.

The independent AST Diff benchmark contains 53 targeted standard/student
pairs:

- 53/53 structural differences are supported.
- 0 cases are recorded as known gaps and 0 are unexpected failures.
- The same 6 SQLite execution boundaries are tracked independently from AST
  Diff support.

The current AST diff-from-IR benchmark covers all 77 IR cases with linked standard/student SQL pairs:

- 77/77 structural-difference pairs are supported.
- 0 cases are recorded as known gaps.
- 0 unexpected failures.
- The same 6 SQLite execution boundaries are carried as execution metadata;
  they do not reduce AST Diff support.
- The AST diff report includes an `IR To AST Diff Continuity` matrix showing how IR-recognized categories are carried forward into standard/student difference tests.
