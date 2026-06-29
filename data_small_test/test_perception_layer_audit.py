import json
import os
import sys
import sqlglot

# 将 backend 路径加入 sys.path
sys.path.append(os.path.join(os.getcwd(), "sql-edu-backend"))

from core.Perception_Arbiter_Phi import SQLEvaluator
from core.Perception_Data_Sensor import QueryDrivenDataGenerator

def audit_to_json():
    """
    全量深度扫描：将 AST 对比、ParSEval 数据生成及仲裁推导保存为 JSON
    """
    # 初始化
    kp_mapping = {"Join": "JOIN_ON", "Window": "WIN_OVER", "CTE": "CTE_SIMPLE"}
    phi = SQLEvaluator(kp_mapping)
    generator = QueryDrivenDataGenerator()

    # 1. 加载数据
    if not os.path.exists("data_std.json") or not os.path.exists("data_student.json"):
        print("❌ 错误：缺少仿真数据基础文件。请先运行 data.py")
        return

    with open("data_std.json", "r", encoding="utf-8") as f:
        std_dict = {q["id"]: q for q in json.load(f)}

    with open("data_student.json", "r", encoding="utf-8") as f:
        student_results = json.load(f)

    audit_logs = []

    print("🚀 启动深度感知审计 (修复接口对齐版)，正在生成 JSON 报告...")

    # 2. 遍历全量仿真数据并记录审计过程
    for student in student_results:
        p_name = student["persona"]

        for record in student["records"]:
            q_id = record["q_id"]
            q_meta = std_dict[q_id]
            student_sql = record["sql"]
            standard_sql = q_meta["ans_sql"]

            # -- A. AST 结构化审计数据 --
            try:
                # 尝试标准化，展示核心拓扑
                s_ast = sqlglot.parse_one(student_sql).sql(normalize=True)
                std_ast = sqlglot.parse_one(standard_sql).sql(normalize=True)
            except Exception:
                s_ast = "PARSE_ERROR"
                std_ast = "PARSE_ERROR"

            # -- B. ParSEval 逻辑审计数据 --
            # 使用对齐后的新接口：extract_all_predicates
            predicates = generator.extract_all_predicates(standard_sql)

            # 生成测试探测数据 (针对 ParSEval 的边界攻击展示)
            # 根据 Q5 的实际 schema 提供参数
            schema_dict = {"ID": "INT", "salary": "INT", "dept_name": "TEXT", "year": "INT", "student_id": "INT"}
            mock_data = generator.generate_targeted_data(standard_sql, schema_dict, num_rows=5)

            # -- C. 感知算子 Φ 仲裁结果 --
            try:
                # 真实调用仲裁器 (含沙盒比对)
                res = phi.evaluate(student_sql, standard_sql, "instructor", schema_dict)
                j_type = res["judgment_type"]
                missing_kps = res["evidence"]["missing_kps"]
                is_exec_match = res["evidence"]["is_semantic_match"]
            except Exception as e:
                # 捕获 SQLite 环境异常并退避
                j_type = f"PHI_ROBUST_JUDGE: {str(e)}"
                missing_kps = []
                is_exec_match = False

            # -- D. 整合单条审计条目 --
            entry = {
                "q_id": q_id,
                "persona": p_name,
                "difficulty": q_meta["difficulty"],
                "simulation_status": record["status"],
                "ast_sensor": {
                    "student_normalized_sample": s_ast[:100] + "...",
                    "standard_normalized_sample": std_ast[:100] + "..."
                },
                "logical_sensor": {
                    "boundary_predicates_found": predicates,
                    "parseval_probe_sample": mock_data[:2] # 只存两条进 JSON 减小体积
                },
                "phi_arbiter": {
                    "final_judgment": j_type,
                    "extracted_missing_kps": missing_kps,
                    "is_execution_consistent": is_exec_match
                }
            }
            audit_logs.append(entry)

    # 3. 持久化到 JSON 文件
    with open("perception_audit_log.json", "w", encoding="utf-8") as f:
        json.dump(audit_logs, f, indent=2, ensure_ascii=False)

    print(f"✅ 审计完成。请查阅物理文件：perception_audit_log.json (共包含 {len(audit_logs)} 个全证据链记录)")

if __name__ == "__main__":
    audit_to_json()
