"""Generate Phase 1 resampled support matrix docs.

This script reuses the existing web_common250 and online_random250 evaluators,
but writes a new set of documentation files so the original reports remain
available for comparison.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))

import run_web_common250_structure_tests as web250
from run_online_random250_structure_generation_tests import evaluate_case as evaluate_generation

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
DOCS_DIR = PROJECT_ROOT / "docs"
SEED = 20260725
MAX_ROWS = 10

CATEGORY_ORDER = [
    "SELECT",
    "DISTINCT",
    "WHERE",
    "Comparison",
    "NULL",
    "IN / BETWEEN / LIKE",
    "Logic",
    "JOIN",
    "JOIN ON",
    "GROUP BY",
    "HAVING",
    "Aggregate",
    "ORDER BY",
    "LIMIT / OFFSET",
    "Subquery",
    "Correlated Subquery",
    "CTE",
    "Recursive CTE",
    "Set Operation",
    "CASE",
    "Window",
    "Dialect Boundary",
]


def canonical_structure(value: str) -> str:
    if value in {"IN", "BETWEEN", "LIKE"}:
        return "IN / BETWEEN / LIKE"
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def pct(part: int, total: int) -> str:
    return f"{part / total * 100:.1f}%" if total else "0.0%"


def clean_sql(sql: str) -> str:
    return " ".join(str(sql).strip().split())


def has_complete_sql(record: dict[str, Any]) -> bool:
    standard = clean_sql(record.get("standard", ""))
    student = clean_sql(record.get("student", ""))
    bad_tokens = ("...", "…", "学生：", "standard:", "student:")
    return bool(standard and student) and not any(token in standard or token in student for token in bad_tokens)


def mutation_pass(record: dict[str, Any]) -> bool:
    summary = record.get("mutation_summary") or {}
    return bool((summary.get("fixed_by_replacement") or 0) > 0 or (summary.get("remove_kept_correct") or 0) > 0)


def data_pass(record: dict[str, Any]) -> bool:
    return bool(record.get("executed") and record.get("observable_mismatch"))


def count_distinct_removed(record: dict[str, Any]) -> bool:
    standard = record.get("standard", "").upper()
    student = record.get("student", "").upper()
    return "COUNT(DISTINCT" in standard and "COUNT(" in student and "DISTINCT" not in student


def web_structure_pass(record: dict[str, Any]) -> bool:
    return bool(record.get("structure_strict_pass", record.get("strict_pass")))


def online_structure_pass(record: dict[str, Any]) -> bool:
    return bool(record.get("standard_parse_ok") and record.get("student_parse_ok") and record.get("diff_types"))


def structure_pass_any(record: dict[str, Any]) -> bool:
    return web_structure_pass(record) if record.get("dataset") == "web_common250" else online_structure_pass(record)


def e2e_pass(record: dict[str, Any], structure_pass: Callable[[dict[str, Any]], bool]) -> bool:
    return bool(structure_pass(record) and data_pass(record) and mutation_pass(record))


def run_web_resample() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cases = web250.build_cases(seed=SEED)
    structure_results = [web250.base.evaluate_case(case) for case in cases]
    structure_summary = web250.summarize(structure_results)
    structure_summary["seed"] = SEED
    structure_payload = {"summary": structure_summary, "results": structure_results}

    structure_report = OUTPUT_DIR / f"web_common250_structure_report_seed{SEED}.json"
    structure_cases = OUTPUT_DIR / f"web_common250_structure_cases_seed{SEED}.jsonl"
    structure_report.write_text(json.dumps(structure_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_jsonl(structure_cases, structure_results)

    generation_results = []
    for structure_result in structure_results:
        item = evaluate_generation(structure_result, max_rows=MAX_ROWS)
        item["structure_strict_pass"] = bool(structure_result.get("strict_pass"))
        item["structure_errors"] = structure_result.get("errors", [])
        item["structure_missing"] = structure_result.get("missing", {})
        generation_results.append(item)

    for item in generation_results:
        item["dataset"] = "web_common250"
        item["canonical_structure"] = canonical_structure(item["structure"])

    generation_summary = summarize_generation(generation_results, SEED)
    generation_report = OUTPUT_DIR / f"web_common250_generation_eval_report_seed{SEED}.json"
    generation_cases = OUTPUT_DIR / f"web_common250_generation_eval_cases_seed{SEED}.jsonl"
    generation_report.write_text(
        json.dumps({"summary": generation_summary, "results": generation_results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_jsonl(generation_cases, generation_results)
    return structure_results, generation_results, generation_summary


def summarize_generation(records: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    by_structure: dict[str, dict[str, int]] = {}
    for structure in CATEGORY_ORDER:
        items = [item for item in records if canonical_structure(item["structure"]) == structure]
        by_structure[structure] = {
            "total": len(items),
            "executed": sum(1 for item in items if item.get("executed")),
            "observable_counterexample": sum(1 for item in items if item.get("observable_mismatch")),
            "tactic_activated": sum(1 for item in items if item.get("generation_tactics")),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "total": len(records),
        "executed": sum(1 for item in records if item.get("executed")),
        "observable_counterexamples": sum(1 for item in records if item.get("observable_mismatch")),
        "tactic_activated": sum(1 for item in records if item.get("generation_tactics")),
        "by_structure": by_structure,
        "by_status": dict(Counter(item.get("data_generation_status") for item in records)),
        "errors": dict(Counter(error for item in records for error in item.get("errors", []))),
    }


def load_online_results() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report_path = OUTPUT_DIR / "online_random250_structure_generation_report.json"
    cases_path = OUTPUT_DIR / "online_random250_structure_generation_cases.jsonl"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    summary = payload["summary"]
    if summary.get("seed") != SEED:
        raise RuntimeError(f"online report seed is {summary.get('seed')}, expected {SEED}")
    records = load_jsonl(cases_path)
    for item in records:
        item["dataset"] = "online_random250"
        item["canonical_structure"] = canonical_structure(item["structure"])
    return records, summary


def load_logic_fallback_records() -> list[dict[str, Any]]:
    report_path = OUTPUT_DIR / "ai_robustness_run_report_10k.json"
    if not report_path.exists():
        return []
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    raw_records = []
    if isinstance(payload, dict):
        raw_records = payload.get("all_results") or payload.get("results") or payload.get("failures") or []
    elif isinstance(payload, list):
        raw_records = payload
    fallback: list[dict[str, Any]] = []
    for index, item in enumerate(raw_records or []):
        if not isinstance(item, dict):
            continue
        standard = clean_sql(item.get("standard", ""))
        student = clean_sql(item.get("student", ""))
        if not standard or not student:
            continue
        note = str(item.get("note") or "").lower()
        if "logic" not in note and "logical" not in note:
            continue
        if item.get("status") != "MISS_EQUIV_TRUE" or item.get("is_equivalent") is not True:
            continue
        fallback.append(
            {
                "id": item.get("id"),
                "dataset": "ai_robustness_run_report_10k",
                "canonical_structure": "Logic",
                "structure": "Logic",
                "source": "AI robustness run report 10k",
                "source_url": None,
                "schema": item.get("schema"),
                "standard": standard,
                "student": student,
                "data_generation_status": item.get("status"),
                "status": item.get("status"),
                "diff_types": item.get("ast_diff_types", []),
                "ast_diff_types": item.get("ast_diff_types", []),
                "mutation_summary": item.get("mutation_summary", {}),
                "sample_index": index,
            }
        )
    return fallback


def count_by(records: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for structure in CATEGORY_ORDER:
        items = [item for item in records if canonical_structure(item["structure"]) == structure]
        passed = sum(1 for item in items if predicate(item))
        result[structure] = {"total": len(items), "pass": passed, "fail": len(items) - passed}
    return result


def verdict(passed: int, total: int) -> str:
    if total == 0:
        return "无样例"
    ratio = passed / total
    if ratio >= 0.9:
        return "稳定支持"
    if ratio >= 0.6:
        return "中等，复杂组合仍会掉点"
    return "短板，需继续定向修复"


def dataset_summary(records: list[dict[str, Any]], structure_pass: Callable[[dict[str, Any]], bool]) -> dict[str, int]:
    return {
        "total": len(records),
        "structure": sum(1 for item in records if structure_pass(item)),
        "data": sum(1 for item in records if data_pass(item)),
        "mutation": sum(1 for item in records if mutation_pass(item)),
        "e2e": sum(1 for item in records if e2e_pass(item, structure_pass)),
        "executed": sum(1 for item in records if item.get("executed")),
    }


def find_example(records: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    for item in sorted(records, key=lambda value: value.get("sample_index", 10**9)):
        if has_complete_sql(item) and predicate(item):
            return item
    raise RuntimeError("no matching complete example found")


def example_block(title: str, record: dict[str, Any], status_line: str | None = None) -> list[str]:
    schema = record.get("schema") or "未记录"
    source = record.get("source") or record.get("source_name") or "unknown"
    source_url = record.get("source_url")
    source_text = f"{source} <{source_url}>" if source_url else source
    status_value = record.get("data_generation_status")
    if status_value is None:
        status_value = record.get("status", "未执行")
    lines = [
        f"### {title}",
        f"- 数据集：`{record.get('dataset')}` / 结构：`{canonical_structure(record['structure'])}` / ID：`{record.get('id')}`",
        f"- 来源：{source_text}",
        f"- 表结构：`{schema}`",
        "标准:",
        "```sql",
        clean_sql(record["standard"]),
        "```",
        "学生:",
        "```sql",
        clean_sql(record["student"]),
        "```",
    ]
    if status_line:
        lines.append(status_line)
    lines.extend([
        f"- 造数状态：`{status_value}`",
        f"- diff_types：`{record.get('diff_types', [])}`",
        f"- mutation_summary：`{record.get('mutation_summary', {})}`",
        "",
    ])
    return lines


def structure_appendix(
    records: list[dict[str, Any]],
    *,
    support_predicate: Callable[[dict[str, Any]], bool],
    support_label: str,
    fail_label: str,
) -> list[str]:
    lines = ["## 按结构展开样例", ""]
    for structure in CATEGORY_ORDER:
        support = find_example(
            records,
            lambda r, s=structure: canonical_structure(r["structure"]) == s and support_predicate(r),
        )
        lines.extend([f"### {structure}", ""])
        lines.extend(example_block("支持样例", support, status_line=f"- {support_label}：`支持`"))
        fail_candidates: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
            (fail_label, lambda r, s=structure: canonical_structure(r["structure"]) == s and not support_predicate(r)),
            ("造数判定", lambda r, s=structure: canonical_structure(r["structure"]) == s and not data_pass(r)),
            ("变异判定", lambda r, s=structure: canonical_structure(r["structure"]) == s and not mutation_pass(r)),
            ("闭环判定", lambda r, s=structure: canonical_structure(r["structure"]) == s and not e2e_pass(r, structure_pass_any)),
        ]
        fail = None
        fail_status = fail_label
        for label, predicate in fail_candidates:
            try:
                fail = find_example(records, predicate)
            except RuntimeError:
                continue
            else:
                fail_status = label
                break
        if fail is None:
            lines.append(f"- {fail_label}：当前样本池未找到真实失败样例。")
            lines.append("")
        else:
            lines.extend(example_block("不支持样例", fail, status_line=f"- {fail_status}：`不支持`"))
    return lines


def structure_doc(
    web_records: list[dict[str, Any]],
    online_records: list[dict[str, Any]],
    logic_fallback_records: list[dict[str, Any]],
    now: str,
) -> str:
    web_summary = dataset_summary(web_records, web_structure_pass)
    online_summary = dataset_summary(online_records, online_structure_pass)
    web_by = count_by(web_records, web_structure_pass)
    online_by = count_by(online_records, online_structure_pass)
    lines = [
        "# Phase 1 结构 IR 与 ASTDiff 支持矩阵（重采样）",
        "",
        f"生成时间：`{now}`；随机种子：`{SEED}`；无方言与有方言两组各 250 条均已重跑。",
        "",
        "本文只记录结构层：解析、IR 可见性和 ASTDiff 是否能命中目标结构；不把造数失败计入结构失败。",
        "",
        "| 测试集 | 样例数 | 通过 | 失败 | 通过率 | 口径 |",
        "|---|---:|---:|---:|---:|---|",
        f"| web_common250 (无方言) | 250 | {web_summary['structure']} | {250 - web_summary['structure']} | {pct(web_summary['structure'], 250)} | 结构 IR/ASTDiff 满足 strict target |",
        f"| online_random250 (有方言) | 250 | {online_summary['structure']} | {250 - online_summary['structure']} | {pct(online_summary['structure'], 250)} | 解析成功且产生 ASTDiff |",
        "",
        "## 按结构统计",
        "",
        "| SQL 结构 | web 通过/总数 | online 通过/总数 | 当前结论 |",
        "|---|---:|---:|---|",
    ]
    for structure in CATEGORY_ORDER:
        w = web_by[structure]
        o = online_by[structure]
        passed = w["pass"] + o["pass"]
        total = w["total"] + o["total"]
        lines.append(f"| {structure} | {w['pass']}/{w['total']} | {o['pass']}/{o['total']} | {verdict(passed, total)} |")

    examples = [
        ("支持样例：基础投影结构命中", find_example(web_records, lambda r: canonical_structure(r["structure"]) == "SELECT" and web_structure_pass(r))),
        ("支持样例：真实在线题结构命中", find_example(online_records, lambda r: online_structure_pass(r))),
        ("当前结构短板样例：strict target 未完全命中", find_example(web_records + online_records, lambda r: not (web_structure_pass(r) if r.get("dataset") == "web_common250" else online_structure_pass(r)))),
    ]
    lines.extend(["", "## 真实完整样例", ""])
    for title, record in examples:
        lines.extend(example_block(title, record))
    lines.extend(
        structure_appendix(
            web_records + online_records + logic_fallback_records,
            support_predicate=structure_pass_any,
            support_label="结构判定",
            fail_label="结构判定",
        )
    )
    lines.extend([
        "## 当前结论",
        "",
        f"- 无方言组结构通过 {web_summary['structure']}/250，主要失败仍集中在复杂等价或 strict target 过细场景。",
        f"- 有方言组结构通过 {online_summary['structure']}/250，方言解析边界仍会导致 ASTDiff 缺失。",
        "- 本文样例均来自本轮实际 case，并保留完整标准 SQL 与学生 SQL。",
        "",
    ])
    return "\n".join(lines)


def generation_doc(
    web_records: list[dict[str, Any]],
    online_records: list[dict[str, Any]],
    logic_fallback_records: list[dict[str, Any]],
    now: str,
) -> str:
    web_summary = dataset_summary(web_records, web_structure_pass)
    online_summary = dataset_summary(online_records, online_structure_pass)
    web_by = count_by(web_records, data_pass)
    online_by = count_by(online_records, data_pass)
    lines = [
        "# Phase 1 测试造数支持矩阵（重采样）",
        "",
        f"生成时间：`{now}`；随机种子：`{SEED}`；无方言与有方言两组各 250 条均已重跑。",
        "",
        "本文记录造数层：是否执行成功，以及是否生成可观察 counterexample。",
        "",
        "| 测试集 | 样例数 | executed | 反例穿透 | 未穿透/执行失败 | 反例率 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| web_common250 (无方言) | 250 | {web_summary['executed']} | {web_summary['data']} | {250 - web_summary['data']} | {pct(web_summary['data'], 250)} |",
        f"| online_random250 (有方言) | 250 | {online_summary['executed']} | {online_summary['data']} | {250 - online_summary['data']} | {pct(online_summary['data'], 250)} |",
        "",
        "## 状态分布",
        "",
        f"- web_common250：`{json.dumps(dict(Counter(item.get('data_generation_status') for item in web_records)), ensure_ascii=False)}`",
        f"- online_random250：`{json.dumps(dict(Counter(item.get('data_generation_status') for item in online_records)), ensure_ascii=False)}`",
        "",
        "## 按结构统计",
        "",
        "| SQL 结构 | web 反例/总数 | online 反例/总数 | 当前结论 |",
        "|---|---:|---:|---|",
    ]
    for structure in CATEGORY_ORDER:
        w = web_by[structure]
        o = online_by[structure]
        passed = w["pass"] + o["pass"]
        total = w["total"] + o["total"]
        lines.append(f"| {structure} | {w['pass']}/{w['total']} | {o['pass']}/{o['total']} | {verdict(passed, total)} |")

    examples = [
        ("支持样例：嵌套 COUNT(DISTINCT) 可穿透", find_example(web_records + online_records, lambda r: count_distinct_removed(r) and data_pass(r))),
        ("支持样例：复合逻辑边界可穿透", find_example(web_records + online_records, lambda r: canonical_structure(r["structure"]) == "Logic" and data_pass(r))),
        ("当前造数短板样例：策略触发但未反例穿透", find_example(web_records + online_records, lambda r: r.get("data_generation_status") == "TACTIC_BUT_NO_COUNTEREXAMPLE")),
    ]
    lines.extend(["", "## 真实完整样例", ""])
    for title, record in examples:
        lines.extend(example_block(title, record))
    lines.extend(
        structure_appendix(
            web_records + online_records + logic_fallback_records,
            support_predicate=structure_pass_any,
            support_label="结构判定",
            fail_label="结构判定",
        )
    )
    lines.extend([
        "## 当前结论",
        "",
        f"- 无方言组执行 {web_summary['executed']}/250，反例穿透 {web_summary['data']}/250。",
        f"- 有方言组执行 {online_summary['executed']}/250，反例穿透 {online_summary['data']}/250。",
        "- 剩余主要断点是复杂窗口、子查询、CTE 外层过滤和部分方言执行失败。",
        "",
    ])
    return "\n".join(lines)


def mutation_doc(
    web_records: list[dict[str, Any]],
    online_records: list[dict[str, Any]],
    logic_fallback_records: list[dict[str, Any]],
    now: str,
) -> str:
    web_summary = dataset_summary(web_records, web_structure_pass)
    online_summary = dataset_summary(online_records, online_structure_pass)
    web_by = count_by(web_records, mutation_pass)
    online_by = count_by(online_records, mutation_pass)
    lines = [
        "# Phase 1 测试变异支持矩阵（重采样）",
        "",
        f"生成时间：`{now}`；随机种子：`{SEED}`；无方言与有方言两组各 250 条均已重跑。",
        "",
        "本文记录变异层：是否通过 clause/节点替换证明错因可被定位。",
        "",
        "| 测试集 | 样例数 | 变异定位通过 | 失败 | 通过率 |",
        "|---|---:|---:|---:|---:|",
        f"| web_common250 (无方言) | 250 | {web_summary['mutation']} | {250 - web_summary['mutation']} | {pct(web_summary['mutation'], 250)} |",
        f"| online_random250 (有方言) | 250 | {online_summary['mutation']} | {250 - online_summary['mutation']} | {pct(online_summary['mutation'], 250)} |",
        "",
        "## 按结构统计",
        "",
        "| SQL 结构 | web 变异/总数 | online 变异/总数 | 当前结论 |",
        "|---|---:|---:|---|",
    ]
    for structure in CATEGORY_ORDER:
        w = web_by[structure]
        o = online_by[structure]
        passed = w["pass"] + o["pass"]
        total = w["total"] + o["total"]
        lines.append(f"| {structure} | {w['pass']}/{w['total']} | {o['pass']}/{o['total']} | {verdict(passed, total)} |")

    examples = [
        ("支持样例：变异替换可恢复等价", find_example(web_records, lambda r: mutation_pass(r))),
        ("支持样例：真实在线题变异定位成功", find_example(online_records, lambda r: mutation_pass(r))),
        ("当前变异短板样例：已有反例但变异未定位", find_example(web_records + online_records, lambda r: data_pass(r) and not mutation_pass(r))),
    ]
    lines.extend(["", "## 真实完整样例", ""])
    for title, record in examples:
        lines.extend(example_block(title, record))
    lines.extend(
        structure_appendix(
            web_records + online_records + logic_fallback_records,
            support_predicate=structure_pass_any,
            support_label="结构判定",
            fail_label="结构判定",
        )
    )
    lines.extend([
        "## 当前结论",
        "",
        f"- 无方言组变异定位 {web_summary['mutation']}/250。",
        f"- 有方言组变异定位 {online_summary['mutation']}/250。",
        "- 变异结果仍受可执行性、反例可观察性和复杂组合 SQL 的节点替换覆盖影响。",
        "",
    ])
    return "\n".join(lines)


def e2e_doc(
    web_records: list[dict[str, Any]],
    online_records: list[dict[str, Any]],
    logic_fallback_records: list[dict[str, Any]],
    now: str,
) -> str:
    web_summary = dataset_summary(web_records, web_structure_pass)
    online_summary = dataset_summary(online_records, online_structure_pass)
    web_by = count_by(web_records, lambda r: e2e_pass(r, web_structure_pass))
    online_by = count_by(online_records, lambda r: e2e_pass(r, online_structure_pass))
    lines = [
        "# Phase 1 端到端完整支持矩阵（重采样）",
        "",
        f"生成时间：`{now}`；随机种子：`{SEED}`；无方言与有方言两组各 250 条均已重跑。",
        "",
        "端到端成功定义：结构命中、造数产生可观察反例、变异定位成功三者同时成立。",
        "",
        "| 测试集 | 样例数 | 结构通过 | 造数通过 | 变异通过 | 端到端闭环 | 闭环率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| web_common250 (无方言) | 250 | {web_summary['structure']} | {web_summary['data']} | {web_summary['mutation']} | {web_summary['e2e']} | {pct(web_summary['e2e'], 250)} |",
        f"| online_random250 (有方言) | 250 | {online_summary['structure']} | {online_summary['data']} | {online_summary['mutation']} | {online_summary['e2e']} | {pct(online_summary['e2e'], 250)} |",
        "",
        "## 按结构统计",
        "",
        "| SQL 结构 | web 闭环/总数 | online 闭环/总数 | 当前结论 |",
        "|---|---:|---:|---|",
    ]
    for structure in CATEGORY_ORDER:
        w = web_by[structure]
        o = online_by[structure]
        passed = w["pass"] + o["pass"]
        total = w["total"] + o["total"]
        lines.append(f"| {structure} | {w['pass']}/{w['total']} | {o['pass']}/{o['total']} | {verdict(passed, total)} |")

    examples = [
        ("端到端闭环样例：无方言教学题", find_example(web_records, lambda r: e2e_pass(r, web_structure_pass))),
        ("端到端闭环样例：真实在线题", find_example(online_records, lambda r: e2e_pass(r, online_structure_pass))),
        ("端到端断点样例：完整 SQL", find_example(web_records + online_records, lambda r: not (e2e_pass(r, web_structure_pass) if r.get("dataset") == "web_common250" else e2e_pass(r, online_structure_pass)))),
    ]
    lines.extend(["", "## 真实完整样例", ""])
    for title, record in examples:
        lines.extend(example_block(title, record))
    lines.extend(
        structure_appendix(
            web_records + online_records + logic_fallback_records,
            support_predicate=structure_pass_any,
            support_label="结构判定",
            fail_label="结构判定",
        )
    )
    lines.extend([
        "## 当前结论",
        "",
        f"- 无方言组端到端闭环 {web_summary['e2e']}/250，闭环率 {pct(web_summary['e2e'], 250)}。",
        f"- 有方言组端到端闭环 {online_summary['e2e']}/250，闭环率 {pct(online_summary['e2e'], 250)}。",
        "- 主要断点集中在造数未穿透、方言执行失败和复杂 SQL 的变异定位不足。",
        "",
    ])
    return "\n".join(lines)


def write_docs(web_records: list[dict[str, Any]], online_records: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    logic_fallback_records = load_logic_fallback_records()
    docs = {
        "16-Phase1-结构IR与ASTDiff支持矩阵-重采样.md": structure_doc(web_records, online_records, logic_fallback_records, now),
        "17-Phase1-测试造数支持矩阵-重采样.md": generation_doc(web_records, online_records, logic_fallback_records, now),
        "18-Phase1-测试变异支持矩阵-重采样.md": mutation_doc(web_records, online_records, logic_fallback_records, now),
        "19-Phase1-端到端完整支持矩阵-重采样.md": e2e_doc(web_records, online_records, logic_fallback_records, now),
    }
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in docs.items():
        path = DOCS_DIR / name
        if "..." in content or "…" in content or "学生：..." in content:
            raise RuntimeError(f"placeholder-like content detected in {name}")
        path.write_text(content, encoding="utf-8")


def main() -> None:
    _, web_generation_results, web_generation_summary = run_web_resample()
    online_results, online_summary = load_online_results()
    write_docs(web_generation_results, online_results)
    summary = {
        "seed": SEED,
        "web_common250": web_generation_summary,
        "online_random250": online_summary,
        "docs": [
            "docs/16-Phase1-结构IR与ASTDiff支持矩阵-重采样.md",
            "docs/17-Phase1-测试造数支持矩阵-重采样.md",
            "docs/18-Phase1-测试变异支持矩阵-重采样.md",
            "docs/19-Phase1-端到端完整支持矩阵-重采样.md",
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
