"""
SQL Parser Utility.

This module provides helper utilities for parsing SQL text, such as extracting output
column names/aliases from standard SELECT statements.
"""

import re
from typing import List


def infer_output_columns_from_sql(sql: str) -> str | None:
    """
    Extracts the output column names/aliases from a standard SELECT query.

    Resolves cases like:
    - Explicit aliases: "SELECT id AS order_id" -> "order_id"
    - Quoted aliases: "SELECT amount AS `order_amount`" -> "order_amount"
    - Unaliased columns: "SELECT user_id" -> "user_id"
    - Complex columns: "SELECT orders.id" -> "id"
    - Fails back to None if query contains "SELECT *" or is unparseable.

    Args:
        sql (str): Standard SQL SELECT statement.

    Returns:
        str | None: Semicolon or comma-separated list of column names, or None if invalid.
    """
    if not sql or not sql.strip():
        return None
    s = sql.strip()
    
    # 1. Strip comments (both single-line -- and multiline /* */)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"/\*[\s\S]*?\*/", " ", s)
    s = re.sub(r"\s+", " ", s)
    lower = s.lower()
    if not lower.startswith("select"):
        return None
        
    # 2. Extract SELECT portion before the outer FROM clause
    # Tracking parentheses depth is required to skip subqueries inside the projections
    start = 6  # len("select")
    depth = 0
    i = start
    end_from = -1
    while i < len(s):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and lower[i : i + 5] == " from":
            # Match only when we are outside any nested parenthesis blocks
            end_from = i
            break
        i += 1
    if end_from < 0:
        return None
        
    select_list = s[start:end_from].strip()
    if not select_list or select_list.strip() == "*":
        return None
        
    # 3. Partition projections by comma at the root level (depth == 0)
    segments: List[str] = []
    depth = 0
    start_idx = 0
    for i, c in enumerate(select_list):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            segments.append(select_list[start_idx:i].strip())
            start_idx = i + 1
    segments.append(select_list[start_idx:].strip())
    
    # 4. Resolve output identifiers from each projection segment
    names: List[str] = []
    for seg in segments:
        if not seg:
            continue
        # Check for AS alias token
        as_match = re.search(r"\s+[Aa][Ss]\s+", seg)
        if as_match:
            alias_part = seg[as_match.end() :].strip()
            # Clean up quotation characters
            if alias_part and alias_part[0] in ('"', "'", "`"):
                q = alias_part[0]
                end = alias_part.find(q, 1)
                if end > 0:
                    alias_part = alias_part[1:end].replace("\\" + q, q)
                else:
                    alias_part = alias_part.lstrip(q).rstrip()
            else:
                alias_part = re.sub(r"^[\"`']|[\"`']$", "", alias_part.strip())
            if alias_part and re.match(r"^[\w\s]+$", alias_part):
                names.append(alias_part.strip())
                continue
                
        # If no alias is specified, default to the last word/field identifier
        seg_clean = seg.strip()
        if re.match(r"^\*$", seg_clean):
            return None
        # Remove trailing parentheses before grabbing the last identifier
        last_id = re.findall(r"[\w]+", seg_clean)
        if last_id:
            names.append(last_id[-1])
            
    if not names:
        return None
    return ", ".join(names)
