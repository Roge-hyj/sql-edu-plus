import csv
import html
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_STD = ROOT / "outputs" / "data_std_full.json"
OUT_CSV = ROOT / "outputs" / "data_std_full_tag_audit.csv"
OUT_MD = ROOT / "outputs" / "data_std_full_tag_audit.md"


FUNC_L2 = {
    "STR_CASE",
    "STR_SUB",
    "NUM_ROUND",
    "DATE_EXT",
    "DATE_DIFF",
    "CASE_SIMPLE",
    "CASE_SEARCH",
    "TYPE_CAST",
}

ADVANCED_L2 = {
    "WIN_OVER",
    "WIN_RANK",
    "WIN_LEAD_LAG",
    "WIN_FRAME",
    "CTE_SIMPLE",
    "CTE_RECURSIVE",
    "SET_UNION",
    "SET_INTERSECT",
    "SET_EXCEPT",
    "NULL_COAL",
}

HIGH_CONFIDENCE_MISSING = {
    "DISTINCT_SET",
    "LIMIT_OFF",
    "COMP_NULL",
    "LOGIC_AND_OR",
    "LOGIC_NOT",
    "RANGE_BET",
    "SET_IN",
    "LIKE_STR",
    "SORT_ASC",
    "SORT_DESC",
    "SORT_MULTI",
    "SORT_NULLS",
    "AGG_BASIC",
    "AGG_DISTINCT",
    "GB_SIMPLE",
    "GB_MULTI",
    "HV_SIMPLE",
    "HV_COMPLEX",
    "JOIN_LEFT",
    "JOIN_RIGHT",
    "JOIN_FULL",
    "JOIN_SELF",
    "JOIN_CROSS",
    "JOIN_ON",
    "JOIN_USING",
    "JOIN_NATURAL",
    "SUB_SCALAR",
    "SUB_TABLE",
    "SUB_IN_ALL_ANY",
    "SUB_EXISTS",
    "STR_CASE",
    "STR_SUB",
    "NUM_ROUND",
    "DATE_EXT",
    "DATE_DIFF",
    "CASE_SIMPLE",
    "CASE_SEARCH",
    "TYPE_CAST",
    "WIN_OVER",
    "WIN_RANK",
    "WIN_LEAD_LAG",
    "WIN_FRAME",
    "CTE_SIMPLE",
    "CTE_RECURSIVE",
    "SET_UNION",
    "SET_INTERSECT",
    "SET_EXCEPT",
    "NULL_COAL",
}

TAG_TO_L1 = {
    "PROJ_COL": "KP_BASIC",
    "PROJ_EXPR": "KP_BASIC",
    "ALIAS_COL": "KP_BASIC",
    "ALIAS_TAB": "KP_BASIC",
    "DISTINCT_SET": "KP_BASIC",
    "LIMIT_OFF": "KP_BASIC",
    "COMP_VAL": "KP_FILTER",
    "COMP_NULL": "KP_FILTER",
    "LOGIC_AND_OR": "KP_FILTER",
    "LOGIC_NOT": "KP_FILTER",
    "RANGE_BET": "KP_FILTER",
    "SET_IN": "KP_FILTER",
    "LIKE_STR": "KP_FILTER",
    "SORT_ASC": "KP_ORDER",
    "SORT_DESC": "KP_ORDER",
    "SORT_MULTI": "KP_ORDER",
    "SORT_NULLS": "KP_ORDER",
    "AGG_BASIC": "KP_AGG",
    "AGG_DISTINCT": "KP_AGG",
    "GB_SIMPLE": "KP_AGG",
    "GB_MULTI": "KP_AGG",
    "HV_SIMPLE": "KP_AGG",
    "HV_COMPLEX": "KP_AGG",
    "JOIN_INNER": "KP_JOIN",
    "JOIN_LEFT": "KP_JOIN",
    "JOIN_RIGHT": "KP_JOIN",
    "JOIN_FULL": "KP_JOIN",
    "JOIN_SELF": "KP_JOIN",
    "JOIN_CROSS": "KP_JOIN",
    "JOIN_ON": "KP_JOIN",
    "JOIN_USING": "KP_JOIN",
    "JOIN_NATURAL": "KP_JOIN",
    "SUB_SCALAR": "KP_SUBQUERY",
    "SUB_ROW": "KP_SUBQUERY",
    "SUB_TABLE": "KP_SUBQUERY",
    "SUB_IN_ALL_ANY": "KP_SUBQUERY",
    "SUB_EXISTS": "KP_SUBQUERY",
    "SUB_CORR": "KP_SUBQUERY",
    "STR_CASE": "KP_FUNC",
    "STR_SUB": "KP_FUNC",
    "NUM_ROUND": "KP_FUNC",
    "DATE_EXT": "KP_FUNC",
    "DATE_DIFF": "KP_FUNC",
    "CASE_SIMPLE": "KP_FUNC",
    "CASE_SEARCH": "KP_FUNC",
    "TYPE_CAST": "KP_FUNC",
    "WIN_OVER": "KP_ADVANCED",
    "WIN_RANK": "KP_ADVANCED",
    "WIN_LEAD_LAG": "KP_ADVANCED",
    "WIN_FRAME": "KP_ADVANCED",
    "CTE_SIMPLE": "KP_ADVANCED",
    "CTE_RECURSIVE": "KP_ADVANCED",
    "SET_UNION": "KP_ADVANCED",
    "SET_INTERSECT": "KP_ADVANCED",
    "SET_EXCEPT": "KP_ADVANCED",
    "NULL_COAL": "KP_ADVANCED",
}


CLAUSES = ["WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT"]


def normalize_sql(sql):
    return re.sub(r"\s+", " ", sql.strip())


def has(pattern, text):
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def find_select_lists(sql):
    lists = []
    upper = sql.upper()
    index = 0
    while True:
        pos = upper.find("SELECT", index)
        if pos < 0:
            break
        depth = 0
        end = len(sql)
        scan = pos + len("SELECT")
        while scan < len(sql):
            ch = sql[scan]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and upper.startswith(" FROM ", scan):
                end = scan
                break
            scan += 1
        lists.append(sql[pos + len("SELECT"):end].strip())
        index = pos + len("SELECT")
    return lists


def split_top_level_csv(text):
    values = []
    current = []
    depth = 0
    in_quote = False
    quote_char = ""
    for ch in text:
        if ch in {"'", '"'}:
            if not in_quote:
                in_quote = True
                quote_char = ch
            elif quote_char == ch:
                in_quote = False
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                values.append("".join(current).strip())
                current = []
                continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        values.append(tail)
    return values


def first_clause_after(sql, start):
    upper = sql.upper()
    positions = []
    for clause in CLAUSES:
        pos = upper.find(f" {clause} ", start)
        if pos >= 0:
            positions.append(pos)
    close_pos = upper.find(")", start)
    if close_pos >= 0:
        positions.append(close_pos)
    return min(positions) if positions else len(sql)


def group_by_items(sql):
    items = []
    upper = sql.upper()
    start = 0
    while True:
        pos = upper.find(" GROUP BY ", start)
        if pos < 0:
            break
        begin = pos + len(" GROUP BY ")
        end = first_clause_after(sql, begin)
        items.extend(split_top_level_csv(sql[begin:end]))
        start = begin
    return [item for item in items if item]


def order_by_items(sql):
    items = []
    upper = sql.upper()
    start = 0
    while True:
        pos = upper.find(" ORDER BY ", start)
        if pos < 0:
            break
        begin = pos + len(" ORDER BY ")
        end = first_clause_after(sql, begin)
        items.extend(split_top_level_csv(sql[begin:end]))
        start = begin
    return [item for item in items if item]


def extract_table_refs(sql):
    refs = []
    table_ref = re.compile(
        r"\b(FROM|JOIN)\s+([A-Za-z_][\w.]*)(?:\s+(?:AS\s+)?([A-Za-z_][\w]*))?",
        re.IGNORECASE,
    )
    reserved = {
        "WHERE",
        "JOIN",
        "ON",
        "USING",
        "GROUP",
        "HAVING",
        "ORDER",
        "LEFT",
        "RIGHT",
        "FULL",
        "INNER",
        "CROSS",
        "NATURAL",
        "LIMIT",
        "UNION",
        "INTERSECT",
        "EXCEPT",
    }
    for match in table_ref.finditer(sql):
        table = match.group(2)
        alias = match.group(3)
        if alias and alias.upper() in reserved:
            alias = None
        refs.append((table, alias))
    return refs


def expected_l2(sql):
    normalized = normalize_sql(sql)
    upper = normalized.upper()
    tags = set()
    reasons = {}

    def add(tag, reason):
        tags.add(tag)
        reasons.setdefault(tag, reason)

    if "SELECT" in upper:
        add("PROJ_COL", "SELECT query projects columns or expressions.")

    select_lists = find_select_lists(normalized)
    select_text = " ".join(select_lists)
    if has(r"\bSELECT\s+DISTINCT\b", normalized):
        add("DISTINCT_SET", "SELECT DISTINCT is used.")
    if has(r"\b(LIMIT|OFFSET)\b", normalized) or has(r"\bFETCH\s+FIRST\b", normalized):
        add("LIMIT_OFF", "LIMIT/OFFSET/FETCH FIRST is used.")

    expression_patterns = [
        r"\bCASE\b",
        r"\b(COALESCE|NULLIF|ISNULL|LOWER|UPPER|SUBSTRING|SUBSTR|CONCAT|LENGTH|ROUND|ABS|CEIL|FLOOR|EXTRACT|DATE_PART|DATEDIFF|DATEADD|YEAR|MONTH|DAY|CAST|CONVERT)\s*\(",
        r"[A-Za-z_][\w.]*\s*[-+*/]\s*[A-Za-z_0-9(']",
    ]
    if any(has(pattern, select_text) for pattern in expression_patterns):
        add("PROJ_EXPR", "Projection contains CASE, scalar function, or arithmetic expression.")
    if has(r"\bAS\s+[A-Za-z_][\w]*\b", select_text):
        add("ALIAS_COL", "Projected expression or column uses AS alias.")

    refs = extract_table_refs(normalized)
    if any(alias for _, alias in refs):
        add("ALIAS_TAB", "FROM/JOIN table reference uses an alias.")

    if has(r"\bWHERE\b|\bON\b|\bHAVING\b", normalized) and has(r"(?<![<>!])=(?!=)|<>|!=|>=|<=|(?<!<)<(?!>)|(?<!>)>(?!=)", normalized):
        add("COMP_VAL", "Comparison operator appears in WHERE/ON/HAVING.")
    if has(r"\bIS\s+(?:NOT\s+)?NULL\b", normalized):
        add("COMP_NULL", "IS NULL or IS NOT NULL is used.")
    if has(r"\b(AND|OR)\b", normalized):
        add("LOGIC_AND_OR", "AND/OR logical composition is used.")
    if has(r"\bNOT\b", normalized):
        add("LOGIC_NOT", "NOT is used.")
    if has(r"\bBETWEEN\b", normalized):
        add("RANGE_BET", "BETWEEN range condition is used.")
    if has(r"\b(?:NOT\s+)?IN\s*\(", normalized):
        add("SET_IN", "IN/NOT IN predicate is used.")
    if has(r"\bLIKE\b", normalized):
        add("LIKE_STR", "LIKE predicate is used.")

    orders = order_by_items(normalized)
    if orders:
        if any(has(r"\bDESC\b", item) for item in orders):
            add("SORT_DESC", "ORDER BY DESC is used.")
        else:
            add("SORT_ASC", "ORDER BY is used without DESC.")
        if len(orders) > 1:
            add("SORT_MULTI", "ORDER BY contains multiple top-level sort keys.")
        if has(r"\bNULLS\s+(FIRST|LAST)\b", normalized):
            add("SORT_NULLS", "ORDER BY NULLS FIRST/LAST is used.")

    if has(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", normalized):
        add("AGG_BASIC", "Basic aggregate function is used.")
    if has(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*DISTINCT\b", normalized):
        add("AGG_DISTINCT", "Aggregate DISTINCT is used.")
    groups = group_by_items(normalized)
    if groups:
        if len(groups) > 1:
            add("GB_MULTI", "GROUP BY contains multiple top-level keys.")
        else:
            add("GB_SIMPLE", "GROUP BY contains one key.")
    if has(r"\bHAVING\b", normalized):
        having_complex = has(r"\bHAVING\b.*\b(SELECT|AND|OR)\b", normalized) or has(r"\bHAVING\b.*\b(COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\)\s*[+\-*/]", normalized)
        add("HV_COMPLEX" if having_complex else "HV_SIMPLE", "HAVING clause is used.")

    if has(r"\bNATURAL\s+JOIN\b", normalized):
        add("JOIN_NATURAL", "NATURAL JOIN is used.")
    if has(r"\bLEFT(?:\s+OUTER)?\s+JOIN\b", normalized):
        add("JOIN_LEFT", "LEFT JOIN is used.")
    if has(r"\bRIGHT(?:\s+OUTER)?\s+JOIN\b", normalized):
        add("JOIN_RIGHT", "RIGHT JOIN is used.")
    if has(r"\bFULL(?:\s+OUTER)?\s+JOIN\b", normalized):
        add("JOIN_FULL", "FULL JOIN is used.")
    if has(r"\bCROSS\s+JOIN\b", normalized) or has(r"\bFROM\s+[A-Za-z_][\w.]*\s*,\s*[A-Za-z_][\w.]*", normalized):
        add("JOIN_CROSS", "CROSS JOIN or comma join is used.")
    if has(r"\bJOIN\b", normalized) and not tags.intersection({"JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL", "JOIN_CROSS", "JOIN_NATURAL"}):
        add("JOIN_INNER", "JOIN is used without an outer/cross/natural modifier.")
    if has(r"\bJOIN\b.*\bON\b", normalized):
        add("JOIN_ON", "JOIN ON condition is used.")
    if has(r"\bJOIN\b.*\bUSING\s*\(", normalized):
        add("JOIN_USING", "JOIN USING clause is used.")
    tables = [table.lower() for table, _ in refs]
    if len(tables) != len(set(tables)) and has(r"\bJOIN\b", normalized):
        add("JOIN_SELF", "Same table name appears in multiple FROM/JOIN references.")

    if has(r"\bFROM\s*\(\s*SELECT\b", normalized):
        add("SUB_TABLE", "FROM subquery/derived table is used.")
    if has(r"\bEXISTS\s*\(\s*SELECT\b", normalized):
        add("SUB_EXISTS", "EXISTS subquery is used.")
    if has(r"\b(?:IN|ALL|ANY|SOME)\s*\(\s*SELECT\b", normalized):
        add("SUB_IN_ALL_ANY", "IN/ALL/ANY/SOME subquery is used.")
    if has(r"(?:=|<>|!=|>=|<=|<|>)\s*\(\s*SELECT\b", normalized):
        add("SUB_SCALAR", "Comparison against scalar subquery is used.")

    if has(r"\bLOWER\s*\(|\bUPPER\s*\(", normalized):
        add("STR_CASE", "LOWER/UPPER string case function is used.")
    if has(r"\b(SUBSTRING|SUBSTR|CONCAT|LENGTH)\s*\(", normalized):
        add("STR_SUB", "String substring/concat/length function is used.")
    if has(r"\b(ROUND|ABS|CEIL|FLOOR)\s*\(", normalized):
        add("NUM_ROUND", "Numeric function is used.")
    if has(r"\b(EXTRACT|DATE_PART|YEAR|MONTH|DAY)\s*\(", normalized):
        add("DATE_EXT", "Date extraction function is used.")
    if has(r"\b(DATEDIFF|DATEADD)\s*\(", normalized):
        add("DATE_DIFF", "Date difference/addition function is used.")
    if has(r"\bCASE\s+WHEN\b", normalized):
        add("CASE_SEARCH", "Searched CASE expression is used.")
    if has(r"\bCASE\s+[A-Za-z_][\w.]*\s+WHEN\b", normalized):
        add("CASE_SIMPLE", "Simple CASE expression is used.")
    if has(r"\b(CAST|CONVERT)\s*\(", normalized):
        add("TYPE_CAST", "CAST/CONVERT is used.")

    if has(r"\bOVER\s*\(", normalized):
        add("WIN_OVER", "OVER window clause is used.")
    if has(r"\b(RANK|DENSE_RANK|ROW_NUMBER)\s*\(", normalized):
        add("WIN_RANK", "Ranking window function is used.")
    if has(r"\b(LEAD|LAG)\s*\(", normalized):
        add("WIN_LEAD_LAG", "LEAD/LAG window function is used.")
    if has(r"\b(ROWS|RANGE)\s+BETWEEN\b", normalized):
        add("WIN_FRAME", "Window frame is used.")
    if has(r"^\s*WITH\s+RECURSIVE\b", normalized):
        add("CTE_RECURSIVE", "WITH RECURSIVE CTE is used.")
    elif has(r"^\s*WITH\b", normalized):
        add("CTE_SIMPLE", "WITH CTE is used.")
    if has(r"\bUNION\b", normalized):
        add("SET_UNION", "UNION set operation is used.")
    if has(r"\bINTERSECT\b", normalized):
        add("SET_INTERSECT", "INTERSECT set operation is used.")
    if has(r"\bEXCEPT\b|\bMINUS\b", normalized):
        add("SET_EXCEPT", "EXCEPT/MINUS set operation is used.")
    if has(r"\b(COALESCE|NULLIF|ISNULL)\s*\(", normalized):
        add("NULL_COAL", "COALESCE/NULLIF/ISNULL is used.")

    return tags, reasons


def suggested_l1(expected_tags, current_l1):
    if expected_tags.intersection({"CTE_SIMPLE", "CTE_RECURSIVE", "WIN_OVER", "WIN_RANK", "WIN_LEAD_LAG", "WIN_FRAME", "SET_UNION", "SET_INTERSECT", "SET_EXCEPT", "NULL_COAL"}):
        return "KP_ADVANCED"
    if expected_tags.intersection({"SUB_TABLE", "SUB_EXISTS", "SUB_IN_ALL_ANY", "SUB_SCALAR", "SUB_ROW", "SUB_CORR"}):
        return "KP_SUBQUERY"
    if expected_tags.intersection({"AGG_BASIC", "AGG_DISTINCT", "GB_SIMPLE", "GB_MULTI", "HV_SIMPLE", "HV_COMPLEX"}):
        if expected_tags.intersection({"JOIN_INNER", "JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL"}) and current_l1 == "KP_JOIN":
            return current_l1
        return "KP_AGG"
    if expected_tags.intersection({"JOIN_INNER", "JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL", "JOIN_SELF", "JOIN_CROSS", "JOIN_ON", "JOIN_USING", "JOIN_NATURAL"}):
        return "KP_JOIN"
    if expected_tags.intersection(FUNC_L2):
        return "KP_FUNC"
    if expected_tags.intersection({"SORT_ASC", "SORT_DESC", "SORT_MULTI", "SORT_NULLS"}):
        return "KP_ORDER"
    if expected_tags.intersection({"COMP_VAL", "COMP_NULL", "LOGIC_AND_OR", "LOGIC_NOT", "RANGE_BET", "SET_IN", "LIKE_STR"}):
        return "KP_FILTER"
    return "KP_BASIC"


def audit_question(question):
    expected, reasons = expected_l2(question["ans_sql"])
    current = set(question.get("l2", []))

    missing = sorted(expected - current)
    high_confidence_missing = sorted(set(missing).intersection(HIGH_CONFIDENCE_MISSING))
    review_missing = sorted(set(missing) - HIGH_CONFIDENCE_MISSING)
    extra = sorted(current - expected)
    missing_func = sorted(set(missing).intersection(FUNC_L2))
    missing_advanced = sorted(set(missing).intersection(ADVANCED_L2))
    current_l1 = question.get("l1")
    inferred_l1 = suggested_l1(expected, current_l1)

    notes = []
    if missing:
        notes.append(f"missing {len(missing)} expected L2")
    if extra:
        notes.append(f"{len(extra)} current L2 not inferred by syntax heuristic")
    if missing_func:
        notes.append("function/expression tags missing")
    if missing_advanced:
        notes.append("advanced tags missing")
    l1_review = inferred_l1 != current_l1
    if l1_review:
        notes.append(f"l1 review: current {current_l1}, inferred {inferred_l1}")

    return {
        "id": question["id"],
        "source": question.get("source", ""),
        "current_l1": current_l1,
        "inferred_l1": inferred_l1,
        "l1_review": "yes" if l1_review else "no",
        "current_l2": "|".join(question.get("l2", [])),
        "expected_l2": "|".join(sorted(expected)),
        "missing_l2": "|".join(missing),
        "high_confidence_missing_l2": "|".join(high_confidence_missing),
        "review_missing_l2": "|".join(review_missing),
        "extra_or_semantic_l2": "|".join(extra),
        "missing_func_l2": "|".join(missing_func),
        "missing_advanced_l2": "|".join(missing_advanced),
        "notes": "; ".join(notes),
        "q": question.get("q", ""),
        "ans_sql": question.get("ans_sql", ""),
        "reasons": reasons,
    }


def write_csv(rows):
    fieldnames = [
        "id",
        "source",
        "current_l1",
        "inferred_l1",
        "l1_review",
        "current_l2",
        "expected_l2",
        "missing_l2",
        "high_confidence_missing_l2",
        "review_missing_l2",
        "extra_or_semantic_l2",
        "missing_func_l2",
        "missing_advanced_l2",
        "notes",
        "q",
        "ans_sql",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fieldnames})


def md_escape(text):
    return html.escape(str(text)).replace("|", "\\|")


def write_markdown(rows):
    total = len(rows)
    rows_with_missing = [row for row in rows if row["missing_l2"]]
    rows_with_high_confidence_missing = [row for row in rows if row["high_confidence_missing_l2"]]
    rows_with_review_missing = [row for row in rows if row["review_missing_l2"]]
    rows_with_func = [row for row in rows if row["missing_func_l2"]]
    rows_with_advanced = [row for row in rows if row["missing_advanced_l2"]]
    rows_with_l1_review = [row for row in rows if row["l1_review"] == "yes"]
    missing_counter = Counter(tag for row in rows for tag in row["missing_l2"].split("|") if tag)
    high_confidence_counter = Counter(tag for row in rows for tag in row["high_confidence_missing_l2"].split("|") if tag)
    review_counter = Counter(tag for row in rows for tag in row["review_missing_l2"].split("|") if tag)
    extra_counter = Counter(tag for row in rows for tag in row["extra_or_semantic_l2"].split("|") if tag)

    lines = [
        "# data_std_full.json 标注充分性审计",
        "",
        "## 审计口径",
        "",
        "- 本审计根据 `ans_sql` 的显式 SQL 结构推断应出现的 L2 标签。",
        "- `missing_l2` 表示 SQL 中能明确看出的标签，但当前 `l2` 未标注。",
        "- `extra_or_semantic_l2` 表示当前已标注、但正则语法审计没有推断出的标签；这类不一定错误，可能来自题意、教材语义或更宽松的标注规则。",
        "- `inferred_l1` 只用于复核核心考点，不自动覆盖原 `l1`，因为 taxonomy 允许按题目核心意图选择一级知识点。",
        "",
        "## 总览",
        "",
        f"- 题目总数：{total}",
        f"- 存在漏标候选 L2 的题目：{len(rows_with_missing)}",
        f"- 高置信漏标题目：{len(rows_with_high_confidence_missing)}",
        f"- 需人工复核的漏标候选题目：{len(rows_with_review_missing)}",
        f"- 存在函数/表达式类漏标的题目：{len(rows_with_func)}",
        f"- 存在高级查询类漏标的题目：{len(rows_with_advanced)}",
        f"- 建议复核 L1 的题目：{len(rows_with_l1_review)}",
        "",
        "## 高频高置信漏标 L2",
        "",
        "| L2 | 次数 |",
        "| --- | ---: |",
    ]
    for tag, count in high_confidence_counter.most_common(30):
        lines.append(f"| `{tag}` | {count} |")

    lines.extend([
        "",
        "## 需人工复核的漏标候选",
        "",
        "| L2 | 次数 | 说明 |",
        "| --- | ---: | --- |",
    ])
    for tag, count in review_counter.most_common(30):
        lines.append(f"| `{tag}` | {count} | 启发式可见，但可能受标注口径影响 |")

    lines.extend([
        "",
        "## 函数/表达式类漏标题目",
        "",
        "| ID | 当前 L1 | 漏标 L2 | SQL 证据 |",
        "| ---: | --- | --- | --- |",
    ])
    for row in rows_with_func:
        evidence = []
        for tag in row["missing_func_l2"].split("|"):
            if tag:
                evidence.append(row["reasons"].get(tag, ""))
        lines.append(
            f"| {row['id']} | `{row['current_l1']}` | `{row['missing_func_l2'].replace('|', '`, `')}` | "
            f"{md_escape('; '.join(evidence))} |"
        )

    lines.extend([
        "",
        "## L1 建议复核题目",
        "",
        "| ID | 当前 L1 | 推断 L1 | 主要漏标 |",
        "| ---: | --- | --- | --- |",
    ])
    for row in rows_with_l1_review[:80]:
        missing = row["missing_l2"] or "-"
        lines.append(f"| {row['id']} | `{row['current_l1']}` | `{row['inferred_l1']}` | `{missing.replace('|', '`, `')}` |")
    if len(rows_with_l1_review) > 80:
        lines.append(f"| ... | ... | ... | 其余 {len(rows_with_l1_review) - 80} 条见 CSV |")

    lines.extend([
        "",
        "## 逐题审计表",
        "",
        "| ID | 当前 L1 | 推断 L1 | 高置信缺失 L2 | 复核候选 L2 | 当前多余/语义 L2 |",
        "| ---: | --- | --- | --- | --- | --- |",
    ])
    for row in rows:
        high_missing = row["high_confidence_missing_l2"] or "-"
        review_missing = row["review_missing_l2"] or "-"
        extra = row["extra_or_semantic_l2"] or "-"
        lines.append(
            f"| {row['id']} | `{row['current_l1']}` | `{row['inferred_l1']}` | "
            f"`{high_missing.replace('|', '`, `')}` | `{review_missing.replace('|', '`, `')}` | `{extra.replace('|', '`, `')}` |"
        )

    lines.extend([
        "",
        "## 高频当前多余/语义标签",
        "",
        "| L2 | 次数 | 说明 |",
        "| --- | ---: | --- |",
    ])
    for tag, count in extra_counter.most_common(30):
        l1 = TAG_TO_L1.get(tag, "")
        lines.append(f"| `{tag}` | {count} | 属于 `{l1}`；可能是题意语义或启发式未覆盖 |")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    questions = json.loads(DATA_STD.read_text(encoding="utf-8"))
    rows = [audit_question(question) for question in questions]
    write_csv(rows)
    write_markdown(rows)

    print(f"questions: {len(rows)}")
    print(f"rows with missing l2: {sum(1 for row in rows if row['missing_l2'])}")
    print(f"rows with missing func l2: {sum(1 for row in rows if row['missing_func_l2'])}")
    print(f"rows with l1 review: {sum(1 for row in rows if row['l1_review'] == 'yes')}")
    print(f"wrote {OUT_CSV}")
    print(f"wrote {OUT_MD}")


if __name__ == "__main__":
    main()
