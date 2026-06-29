import requests
import json
import os
import re
import random

# --- 0. 环境加固 ---
os.environ["no_proxy"] = "localhost,127.0.0.1,0.0.0.0"
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""

API_URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL_NAME = "qwen2.5-coder-32b"

# --- 1. 完整 56 个 L2 原子知识点 ---
KPS_HIERARCHY = {
    "KP_BASIC": ["PROJ_COL", "PROJ_EXPR", "ALIAS_COL", "ALIAS_TAB", "DISTINCT_SET", "LIMIT_OFF"],
    "KP_FILTER": ["COMP_VAL", "COMP_NULL", "LOGIC_AND_OR", "LOGIC_NOT", "RANGE_BET", "SET_IN", "LIKE_STR"],
    "KP_ORDER": ["SORT_ASC", "SORT_DESC", "SORT_MULTI", "SORT_NULLS"],
    "KP_AGG": ["AGG_BASIC", "AGG_DISTINCT", "GB_SIMPLE", "GB_MULTI", "HV_SIMPLE", "HV_COMPLEX"],
    "KP_JOIN": ["JOIN_INNER", "JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL", "JOIN_SELF", "JOIN_CROSS", "JOIN_ON", "JOIN_USING", "JOIN_NATURAL"],
    "KP_SUBQUERY": ["SUB_SCALAR", "SUB_ROW", "SUB_TABLE", "SUB_IN_ALL_ANY", "SUB_EXISTS", "SUB_CORR"],
    "KP_FUNC": ["STR_CASE", "STR_SUB", "NUM_ROUND", "DATE_EXT", "DATE_DIFF", "CASE_SIMPLE", "CASE_SEARCH", "TYPE_CAST"],
    "KP_ADVANCED": ["WIN_OVER", "WIN_RANK", "WIN_LEAD_LAG", "WIN_FRAME", "CTE_SIMPLE", "CTE_RECURSIVE", "SET_UNION", "SET_INTERSECT", "SET_EXCEPT", "NULL_COAL"]
}

def ask_qwen(p_name, p_desc, q_data, target_status):
    """
    自适应 Prompt：强制锁定 Schema 以防幻觉
    """
    schema_hint = f"SCHEMA (EXTRACTED): {q_data['schema']}"
    if target_status == "Correct":
        inst = "Write the CORRECT SQL. Follow semantic logic exactly as per the book's requirements."
    else:
        inst = f"Write an INCORRECT SQL. Persona: {p_name}. Action: Make a logical flaw typical for an intermediate student (e.g. mix up JOIN columns, use wrong aggregation)."

    prompt = f"Role: {p_desc}\nContext: {inst}\n{schema_hint}\nQ: {q_data['q']}\nOutput ONLY JSON: {{\"thought\": \"...\", \"sql\": \"...\"}}"

    try:
        resp = requests.post(API_URL, json={"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}, timeout=35)
        content = resp.json()['choices'][0]['message']['content'].strip()
        return json.loads(re.sub(r'```(?:json)?|```', '', content).strip())
    except Exception:
        return {"thought": "API Error Fallback", "sql": "SELECT 'MOCK_ERROR'"}

def run_simulation():
    with open("data_std.json", "r", encoding="utf-8") as f:
        questions = json.load(f)

    personas = {
        "Newbie": "Strict Beginner. Only understands single-table CRUD. Fails at ANY Join/Subquery.",
        "Logic_Master": "Top-tier student. Always correct, uses advanced features properly.",
        "Agg_Join_Struggler": "Can do basic queries, but completely fails at JOIN logic and GROUP BY calculations."
    }

    all_data = []
    for p_name, p_desc in personas.items():
        print(f"🔄正在物理仿真画像: {p_name}")
        results = []
        l2_stats = {l2: {"c": 0, "t": 0} for sub in KPS_HIERARCHY.values() for l2 in sub}

        for q in questions:
            diff = q.get("difficulty", 5.0)
            is_correct = True

            # --- 下发画像判定指令 ---
            if p_name == "Newbie":
                if diff > 4.0 or q["l1"] in ["KP_JOIN", "KP_SUBQUERY", "KP_ADVANCED"]: is_correct = False
            elif p_name == "Agg_Join_Struggler":
                if q["l1"] in ["KP_AGG", "KP_JOIN"] or diff > 7.0: is_correct = False
            elif p_name == "Logic_Master":
                is_correct = True

            ans = ask_qwen(p_name, p_desc, q, "Correct" if is_correct else "Incorrect")

            results.append({
                "q_id": q["id"], "l1": q["l1"], "l2": q["l2"], "status": "Correct" if is_correct else "Incorrect",
                "sql": ans["sql"], "thought": ans.get("thought", "")
            })

            for kp in q["l2"]:
                l2_stats[kp]["t"] += 1
                if is_correct: l2_stats[kp]["c"] += 1

        # 后验概率平滑
        kp2_m = {}
        for l1, l2_list in KPS_HIERARCHY.items():
            g_perf = (sum(l2_stats[k]["c"] for k in l2_list) + 1) / (sum(l2_stats[k]["t"] for k in l2_list) + 2)
            for kp in l2_list:
                kp2_m[kp] = round((l2_stats[kp]["c"] + 1) / (l2_stats[kp]["t"] + 2), 3) if l2_stats[kp]["t"] > 0 else round(g_perf, 3)

        kp1_m = {l1: round(sum(kp2_m[k] for k in l2_list)/len(l2_list), 3) for l1, l2_list in KPS_HIERARCHY.items()}
        all_data.append({"persona": p_name, "kp1_matrix": kp1_m, "kp2_matrix": kp2_m, "records": results})

    with open("data_student.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    print("✅ data_student.json 物理重写成功。")

if __name__ == "__main__":
    run_simulation()
