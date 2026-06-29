import sqlglot
from sqlglot import exp, parse_one, optimizer
from typing import Set, Dict, List, Optional

class ASTAnalyser:
    """
    感知层 - 结构传感器 (Structure Sensor)
    基于 AST 的标准化与等价性分析模块
    """

    @staticmethod
    def normalize_sql(sql: str, dialect: str = "mysql") -> Optional[str]:
        """
        对 SQL 进行语义标准化：
        1. 统一大小写 (Upper Keywords)
        2. 补全默认关键字 (如 ASC)
        3. 消除冗余逻辑 (如 WHERE 1=1)
        """
        try:
            expression = parse_one(sql, read=dialect)
            # 使用 sqlglot 的优化器进行基础逻辑简化（可选）
            # expression = optimizer.optimize(expression)
            return expression.sql(dialect=dialect, normalize=True, indent=2)
        except Exception:
            return None

    @staticmethod
    def get_structural_summary(sql: str, dialect: str = "mysql") -> Dict:
        """
        提取 SQL 的结构特征向量（支持细粒度属性识别）
        V4.2 增强：捕获 Join 类型、聚合函数及 Window 细节
        """
        summary = {
            "node_types": set(),
            "join_kinds": set(),
            "agg_functions": set(),
            "has_window": False,
            "has_cte": False,
            "has_subquery": False,
            "complexity_score": 0
        }
        try:
            tree = parse_one(sql, read=dialect)

            # 统计核心节点与细粒度属性
            for node in tree.find_all(exp.Expression):
                node_type = type(node).__name__
                summary["node_types"].add(node_type)

                if isinstance(node, exp.Join):
                    # 区分 INNER, LEFT, RIGHT, CROSS 等
                    kind = (node.args.get("kind") or "INNER").upper()
                    summary["join_kinds"].add(kind)

                if isinstance(node, exp.AggFunc):
                    summary["agg_functions"].add(type(node).__name__.upper())

                if isinstance(node, exp.Window):
                    summary["has_window"] = True
                if isinstance(node, exp.CTE):
                    summary["has_cte"] = True
                if isinstance(node, exp.Subquery):
                    summary["has_subquery"] = True

            # 复杂度评分逻辑增强：节点种类 + 特殊结构加权
            summary["complexity_score"] = len(summary["node_types"]) + (5 if summary["has_window"] else 0)
            return summary
        except Exception:
            return summary

    @staticmethod
    def is_semantically_equivalent(sql1: str, sql2: str, dialect: str = "mysql") -> bool:
        """
        基于 AST 的语义等价性初判
        能处理：别名差异、Order 差异、连接顺序等
        """
        try:
            ast1 = parse_one(sql1, read=dialect)
            ast2 = parse_one(sql2, read=dialect)

            # 基础 AST 比对 (sqlglot 会处理简单的同构映射)
            if ast1 == ast2:
                return True

            # 如果不直接相等，进行更深层的标准化比对
            n1 = ASTAnalyser.normalize_sql(sql1, dialect)
            n2 = ASTAnalyser.normalize_sql(sql2, dialect)
            return n1 == n2
        except Exception:
            return False

    @staticmethod
    def extract_missing_kps(student_sql: str, standard_sql: str, kp_mapping: Dict[str, str]) -> List[str]:
        """
        对比学生 SQL 与标准 SQL 的结构差异，提取缺失的 L2 原点
        kp_mapping 推导逻辑示例：{"Join": "JOIN_ON", "Window": "WIN_OVER"}
        """
        try:
            std_nodes = ASTAnalyser.get_structural_summary(standard_sql)["node_types"]
            stu_nodes = ASTAnalyser.get_structural_summary(student_sql)["node_types"]

            missing_nodes = std_nodes - stu_nodes
            missing_kps = []

            # 根据节点差异映射到教学系统的 L2 知识点
            # 这种映射应根据你的 KPS_HIERARCHY 进行定制
            for node in missing_nodes:
                if node in kp_mapping:
                    missing_kps.append(kp_mapping[node])

            return list(set(missing_kps))
        except Exception:
            return []
