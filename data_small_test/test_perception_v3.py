import json
import os
import sys
import sqlglot

# 核心后端路径注入
sys.path.append(os.path.join(os.getcwd(), "sql-edu-backend"))

from core.Perception_Arbiter_Phi import SQLEvaluator
from core.Perception_Data_Sensor import QueryDrivenDataGenerator

def final_perception_deep_audit():
    """
    终极感知层测试：展示每一道题的 AST 差异向量、数据生成边界以及 Phi 判定自洽性。
    """
    # 建立映射
    kp_mapping = {"Join": "JOIN_ON", "Window": "WIN_OVER", "CTE": "CTE_SIMPLE", "Recursive": "CTE_RECURSIVE"}
    phi = SQLEvaluator(kp_mapping)
    generator = QueryDrivenDataGenerator()

    # 加载标准化资产
    with open("data_std.json", "r", encoding="utf-8") as f:
        std_dict = {q["id"]: q for q in json.load(f)}
    with open("data_student.json", "r", encoding="utf-8") as f:
        student_results = json.load(f)

    audit_logs = []
    print("\n" + "="*80)
    print("💎 感知层全量深度审计报告 (V3 Production Final)")
    print("="*80)

    # 抽取具有代表性的画像进行展示
    for student in student_results:
        p_name = student["persona"]
        print(f"\n【画像审计：{p_name}】")

        for record in student["records"]:
            q_id = record["q_id"]
            q_meta = std_dict[q_id]
            student_sql = record["sql"]
            standard_sql = q_meta["ans_sql"]
            schema = {"ID": "INT", "salary": "INT", "dept_name": "TEXT", "year": "INT", "student_id": "INT", "credits": "INT"}

            # --- A. AST 传感器执行 ---
            try:
                s_ast_norm = sqlglot.parse_one(student_sql).sql(normalize=True)
                std_ast_norm = sqlglot.parse_one(standard_sql).sql(normalize=True)
            except Exception:
                s_ast_norm = "PARSE_ERROR"

            # --- B. Data 传感器执行 (ParSEval 逻辑) ---
            predicates = generator.extract_all_predicates(standard_sql)
            # 生成区分度数据
            mock_data = generator.generate_targeted_data(standard_sql, schema, num_rows=8)

            # --- C. Phi 感知仲裁 ---
            try:
                res = phi.evaluate(student_sql, standard_sql, "source_table", schema)
                final_j = res["judgment_type"]
                is_match = res["evidence"]["is_semantic_match"]
            except Exception as e:
                final_j = f"FALLBACK_JUDGE: {str(e)[:50]}"
                is_match = False

            # 打印关键审计项到终端（仅展示高价值细节）
            if q_id in [5, 13, 18]:
                print(f"\n--- Q{q_id} (Diff: {q_meta['difficulty']}) ---")
                print(f"   [AST] 学生写法: {s_ast_norm[:60]}...")
                print(f"   [LOGIC] 识别谓词: {predicates}")
                print(f"   [DATA] ParSEval 探测记录(首条): {mock_data[0] if mock_data else 'None'}")
                print(f"   [SIGNAL] Phi 判定结果: {final_j} (逻辑对齐: {is_match})")

            # 存入 JSON
            entry = {
                "q_id": q_id,
                "persona": p_name,
                "ast": {"student": s_ast_norm},
                "parseval": {"predicates": predicates, "data": mock_data[:3]},
                "judgment": final_j
            }
            audit_logs.append(entry)

    with open("perception_audit_log.json", "w", encoding="utf-8") as f:
        json.dump(audit_logs, f, indent=2, ensure_ascii=False)

    print("\n" + "="*80)
    print(f"✅ 感知层审计大成！数据记录已保存至: perception_audit_log.json")

if __name__ == "__main__":
    final_perception_deep_audit()
