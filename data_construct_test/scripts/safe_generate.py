import os
from pathlib import Path

DOCS_DIR = Path("/home/roge/projects/sql-edu-main/docs")

CATEGORIES=["SELECT", "DISTINCT", "WHERE", "Comparison", "NULL", "IN / BETWEEN / LIKE", "Logic", "JOIN", "JOIN ON", "GROUP BY", "HAVING", "Aggregate", "ORDER BY", "LIMIT / OFFSET", "Subquery", "Correlated Subquery", "CTE", "Recursive CTE", "Set Operation", "CASE", "Window", "Dialect Boundary"]

data = {
    "SELECT": {
        "supp": ("投影缺列", "SELECT a, b FROM t;", "SELECT a FROM t;"),
        "unsupp": ("暂无结构性不支持", "无", "无"),
        "doc10": {"scur": "ASTDiff：projection_changed, column_dropped", "scon": "支持基础投影增删诊断。", "ucur": "无", "ucon": "SELECT 主能力基石，无明显的结构性不支持场景。"},
        "doc11": {"scur": "造数引擎精准识别，输出列不同。", "scon": "造数完美穿透。", "ucur": "无", "ucon": "全链路稳定。"},
        "doc14": {"scur": "节点替换后沙盒执行恢复一致，fixed_by_replacement = True。", "scon": "精准定位列错误。", "ucur": "无", "ucon": "变异验证完美闭环。"},
        "doc15": {"scur": "提取 projection_changed -> 造数输出截断 -> 变异自证。", "scon": "全链路阻击闭环。", "ucur": "无", "ucon": "主能力基石。"}
    },
    "DISTINCT": {
        "supp": ("全局 DISTINCT 缺失", "SELECT DISTINCT a FROM t;", "SELECT a FROM t;"),
        "unsupp": ("嵌套去重造数未穿透", "SELECT a, COUNT(DISTINCT b) FROM t GROUP BY a;", "SELECT a, COUNT(b) FROM t GROUP BY a;"),
        "doc10": {"scur": "ASTDiff：distinct_removed", "scon": "支持顶层去重判定。", "ucur": "提取到 distinct_removed，但附带冗余结构变动。", "ucon": "嵌套去重结构解析不够干净。"},
        "doc11": {"scur": "引擎随机插入重复 a 的数据，输出不同。", "scon": "全局去重造数穿透成功。", "ucur": "数据规模受限，碰巧没有重复 b，输出一致。", "ucon": "强约束数据分布造数易漏判。"},
        "doc14": {"scur": "替换 DISTINCT 节点后输出等价，定位成功。", "scon": "完美隔离去重错因。", "ucur": "受造数连累，替换前后输出相同，无法证实。", "ucon": "造数未穿透导致变异瘫痪。"},
        "doc15": {"scur": "提取 -> 插入重复行 -> 变异证明缺失 DISTINCT。", "scon": "全局去重完美闭环。", "ucur": "提取成功 -> 造数无差异 -> 变异失败。", "ucon": "局部策略碰撞未穿透。"}
    },
    "WHERE": {
        "supp": ("谓词完全缺失", "SELECT * FROM t WHERE a > 1;", "SELECT * FROM t;"),
        "unsupp": ("复杂逻辑代数等价未规范化", "SELECT * FROM t WHERE (a>1 AND b=1) OR b=1;", "SELECT * FROM t WHERE b=1;"),
        "doc10": {"scur": "ASTDiff：predicate_missing", "scon": "支持 WHERE 缺失诊断。", "ucur": "ASTDiff 报多重 logic_changed。", "ucon": "逻辑代数化简未支持。"},
        "doc11": {"scur": "造出 a <= 1 的数据，反例穿透。", "scon": "造数稳定穿透。", "ucur": "部分深层逻辑边界造数引擎无法穷尽真值表。", "ucon": "复合逻辑边界碰撞弱。"},
        "doc14": {"scur": "替换 WHERE 树后结果等价。", "scon": "隔离验证成功。", "ucur": "结构差异过大，无法单节点替换。", "ucon": "单点变异失效。"},
        "doc15": {"scur": "提取 -> 生成越界行 -> 变异闭环。", "scon": "基础条件全链路成功。", "ucur": "提取误报 -> 造数未穿透 -> 变异断裂。", "ucon": "复杂组合逻辑断链。"}
    },
    "Comparison": {
        "supp": ("边界运算符错写", "SELECT * FROM t WHERE a >= 18;", "SELECT * FROM t WHERE a > 18;"),
        "unsupp": ("字符串 Collation 隐式比较", "SELECT * FROM t WHERE word > 'Apple';", "SELECT * FROM t WHERE word >= 'apple';"),
        "doc10": {"scur": "ASTDiff：comparison_operator_changed", "scon": "运算符改写完美提取。", "ucur": "提取到比较符变化，但未考虑字符集。", "ucon": "跨方言隐式规则不支持。"},
        "doc11": {"scur": "强制生成 a = 18 的极值数据。", "scon": "边界反例完美穿透。", "ucur": "SQLite 大小写比较规则不同于 MySQL，造数失败。", "ucon": "执行环境方言限制。"},
        "doc14": {"scur": "替换 >= 后等价，确诊边界错因。", "scon": "变异完美闭环。", "ucur": "沙盒执行差异导致变异误判。", "ucon": "底层环境差异连累变异。"},
        "doc15": {"scur": "提取 -> 极值数据 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取遗漏语义 -> 执行环境报错 -> 变异断裂。", "ucon": "严重依赖原生沙盒。"}
    },
    "NULL": {
        "supp": ("比较符错写", "SELECT * FROM t WHERE a IS NULL;", "SELECT * FROM t WHERE a = NULL;"),
        "unsupp": ("三值逻辑陷阱", "SELECT * FROM a WHERE id NOT IN (SELECT b_id FROM b WHERE b_id IS NOT NULL);", "SELECT * FROM a WHERE id NOT IN (SELECT b_id FROM b);"),
        "doc10": {"scur": "ASTDiff：comparison_to_null", "scon": "IS NULL 提取稳定。", "ucur": "结构提取正常，但未标记三值逻辑高危。", "ucon": "深层语义特征缺失。"},
        "doc11": {"scur": "插入 NULL 数据，证明后者返回空。", "scon": "NULL 造数稳定穿透。", "ucur": "随机生成极难正好在 b 表造 NULL 并在 a 表造探测行。", "ucon": "极值碰撞率低。"},
        "doc14": {"scur": "替换为 IS NULL 验证等价。", "scon": "隔离验证成功。", "ucur": "受造数连累未穿透。", "ucon": "变异依附于造数失效。"},
        "doc15": {"scur": "提取 -> 造 NULL 行 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取成功 -> 造数无差异 -> 变异失败。", "ucon": "中等瓶颈。"}
    },
    "IN / BETWEEN / LIKE": {
        "supp": ("IN 列表成员变动", "SELECT * FROM t WHERE a IN (1, 2);", "SELECT * FROM t WHERE a IN (1);"),
        "unsupp": ("复杂 LIKE 正则", "SELECT * FROM t WHERE name LIKE 'A_%_Z';", "SELECT * FROM t WHERE name LIKE 'A%Z';"),
        "doc10": {"scur": "ASTDiff：in_list_changed", "scon": "集合元素增删提取稳定。", "ucur": "ASTDiff：pattern_changed", "ucon": "正则模式识别较粗。"},
        "doc11": {"scur": "强制造出 a = 2 的数据。", "scon": "反例稳定穿透。", "ucur": "10 行内极难撞出匹配特定正则的随机字符串。", "ucon": "正则极值碰撞失败。"},
        "doc14": {"scur": "替换 IN 列表验证等价。", "scon": "隔离验证成功。", "ucur": "造数未穿透，变异引擎以为两者等价。", "ucon": "变异误报等价。"},
        "doc15": {"scur": "提取 -> 极值数据 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取粗糙 -> 造数未穿透 -> 变异假阳性。", "ucon": "中等瓶颈。"}
    },
    "Logic": {
        "supp": ("AND 与 OR 错用", "SELECT * FROM t WHERE a=1 OR b=2;", "SELECT * FROM t WHERE a=1 AND b=2;"),
        "unsupp": ("长串条件树乱序", "SELECT * FROM t WHERE a=1 AND (b=2 OR c=3);", "SELECT * FROM t WHERE (c=3 OR b=2) AND a=1;"),
        "doc10": {"scur": "ASTDiff：logic_operator_changed", "scon": "基础布尔逻辑提取稳定。", "ucur": "AST 树翻转，误报大量结构错位。", "ucon": "逻辑树排序未规范化。"},
        "doc11": {"scur": "生成满一缺一数据，反例穿透。", "scon": "逻辑改变容易触发反例。", "ucur": "本质等价，造不出反例。", "ucon": "造数正确验证了等价性。"},
        "doc14": {"scur": "替换运算符后验证等价。", "scon": "隔离验证成功。", "ucur": "造数验证等价，压制了 ASTDiff 的误报。", "ucon": "变异引擎兜底成功。"},
        "doc15": {"scur": "提取 -> 反例碰撞 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取误报 -> 造数证明等价 -> 变异取消报错。", "ucon": "兜底成功，正例保持等价。"}
    },
    "JOIN": {
        "supp": ("JOIN 类型错用", "SELECT * FROM a LEFT JOIN b ON a.id = b.id;", "SELECT * FROM a INNER JOIN b ON a.id = b.id;"),
        "unsupp": ("隐式 JOIN 与显式 JOIN", "SELECT * FROM a JOIN b ON a.id = b.id;", "SELECT * FROM a, b WHERE a.id = b.id;"),
        "doc10": {"scur": "ASTDiff：join_type_changed", "scon": "显式连接类型提取稳定。", "ucur": "报 FROM 变化和 WHERE 新增，丢失 JOIN 语义。", "ucon": "隐式表连接未展平规范化。"},
        "doc11": {"scur": "插入悬空外键数据，反例穿透。", "scon": "悬空数据构造稳定。", "ucur": "本质等价，造不出反例。", "ucon": "造数验证了等价。"},
        "doc14": {"scur": "替换 JOIN 节点后验证等价。", "scon": "隔离验证成功。", "ucur": "结构变化太大，单点替换变异失效。", "ucon": "变异引擎瘫痪。"},
        "doc15": {"scur": "提取 -> 悬空反例 -> 变异闭环。", "scon": "显式关联主能力。", "ucur": "提取彻底断裂 -> 变异无法接管。", "ucon": "严重盲区。"}
    },
    "JOIN ON": {
        "supp": ("连接键写错", "SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id;", "SELECT * FROM emp e JOIN dept d ON e.id = d.id;"),
        "unsupp": ("ON 与 WHERE 过滤下推", "SELECT * FROM A LEFT JOIN B ON A.id = B.id AND B.status = 1;", "SELECT * FROM A LEFT JOIN B ON A.id = B.id WHERE B.status = 1;"),
        "doc10": {"scur": "ASTDiff：join_on_changed", "scon": "显式键变化可覆盖。", "ucur": "误报为 WHERE 新增。", "ucon": "外连接条件深层语义未归一化。"},
        "doc11": {"scur": "生成主外键不同的探测数据。", "scon": "造数稳定穿透。", "ucur": "能造出差异，执行层面有区分度。", "ucon": "造数成功暴露逻辑错误。"},
        "doc14": {"scur": "替换 ON 子句后验证等价。", "scon": "隔离验证成功。", "ucur": "结构对齐失败导致替换错误节点。", "ucon": "变异错位失效。"},
        "doc15": {"scur": "提取 -> 反例穿透 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取错位 -> 变异错位 -> 全链路降级。", "ucon": "严重盲区。"}
    },
    "GROUP BY": {
        "supp": ("缺少分组键", "SELECT a, b, COUNT(*) FROM t GROUP BY a, b;", "SELECT a, b, COUNT(*) FROM t GROUP BY a;"),
        "unsupp": ("主键函数依赖等价", "SELECT user_id, COUNT(*) FROM orders GROUP BY user_id;", "SELECT user_id, name, COUNT(*) FROM orders JOIN users u ON user_id = u.id GROUP BY user_id, name;"),
        "doc10": {"scur": "ASTDiff：group_by_changed", "scon": "基础分组键提取稳定。", "ucur": "误报分组粒度过细。", "ucon": "缺乏 Schema FD 推导。"},
        "doc11": {"scur": "造出 a 相同 b 不同的数据。", "scon": "分组改变造数穿透稳定。", "ucur": "id 是主键，造不出同 id 不同 name 的行。", "ucon": "造数正确验证了等价。"},
        "doc14": {"scur": "替换 GROUP BY 后验证等价。", "scon": "隔离验证成功。", "ucur": "造数无差异压制了结构误报。", "ucon": "变异兜底成功。"},
        "doc15": {"scur": "提取 -> 反例碰撞 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取误报 -> 造数证明等价 -> 变异通过。", "ucon": "正例等价保护成功。"}
    },
    "HAVING": {
        "supp": ("HAVING 阈值错误", "SELECT a, SUM(b) FROM t GROUP BY a HAVING SUM(b) > 10;", "SELECT a, SUM(b) FROM t GROUP BY a HAVING SUM(b) > 5;"),
        "unsupp": ("前置 WHERE 误放导致拦截", "SELECT a, SUM(b) FROM t GROUP BY a HAVING SUM(b) > 10;", "SELECT a, SUM(b) FROM t WHERE b > 10 GROUP BY a;"),
        "doc10": {"scur": "ASTDiff：having_changed", "scon": "阈值变动提取稳定。", "ucur": "提取到 where_changed, having_changed。", "ucon": "错位提取成功。"},
        "doc11": {"scur": "生成 SUM 在 5~10 之间的数据。", "scon": "阈值造数穿透成功。", "ucur": "随机造数极易被前置 WHERE 过滤导致输出全空。", "ucon": "强过滤导致未穿透。"},
        "doc14": {"scur": "替换 HAVING 后验证等价。", "scon": "隔离验证成功。", "ucur": "受造数全空连累，替换前后无差异。", "ucon": "变异瘫痪。"},
        "doc15": {"scur": "提取 -> 聚合碰撞 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取错位 -> 造数全空 -> 变异假阴性。", "ucon": "中等瓶颈。"}
    },
    "Aggregate": {
        "supp": ("函数写反", "SELECT MAX(a) FROM t;", "SELECT MIN(a) FROM t;"),
        "unsupp": ("深层 CASE 参数变异", "SELECT SUM(CASE WHEN a=1 THEN b ELSE 0 END) FROM t;", "SELECT SUM(CASE WHEN a=0 THEN b ELSE 0 END) FROM t;"),
        "doc10": {"scur": "ASTDiff：aggregate_function_changed", "scon": "聚合函数名提取极稳。", "ucur": "仅报 parameter_changed。", "ucon": "深层复合表达式诊断粗。"},
        "doc11": {"scur": "多行数据碰撞出最大最小值不同。", "scon": "聚合反例造数稳定。", "ucur": "10 行内极难撞出满足 CASE 特定组合的数据。", "ucon": "复合表达式极值碰撞弱。"},
        "doc14": {"scur": "替换函数节点验证等价。", "scon": "隔离验证成功。", "ucur": "造数未穿透连累验证。", "ucon": "变异受限。"},
        "doc15": {"scur": "提取 -> 反例穿透 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取粗糙 -> 造数未穿透 -> 变异断裂。", "ucon": "中等瓶颈。"}
    },
    "ORDER BY": {
        "supp": ("排序方向错写", "SELECT a FROM t ORDER BY a DESC;", "SELECT a FROM t ORDER BY a ASC;"),
        "unsupp": ("纯聚合输出无意义排序", "SELECT MAX(a) FROM t ORDER BY id;", "SELECT MAX(a) FROM t;"),
        "doc10": {"scur": "ASTDiff：order_direction_changed", "scon": "方向判定完美提取。", "ucur": "误报 order_by_missing。", "ucon": "未结合基数推导无用排序。"},
        "doc11": {"scur": "造出乱序数据，输出列序不同。", "scon": "乱序造数稳定。", "ucur": "因为只有单行聚合输出，怎么排序结果都一样。", "ucon": "造数证明等价。"},
        "doc14": {"scur": "替换 ORDER BY 验证等价。", "scon": "隔离验证成功。", "ucur": "等价压制了 ASTDiff 的误报。", "ucon": "变异兜底成功。"},
        "doc15": {"scur": "提取 -> 反例乱序 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取误报 -> 造数证明单行等价 -> 变异放行。", "ucon": "正例兜底成功。"}
    },
    "LIMIT / OFFSET": {
        "supp": ("偏置错误", "SELECT * FROM t LIMIT 10 OFFSET 5;", "SELECT * FROM t LIMIT 10 OFFSET 0;"),
        "unsupp": ("高级方言执行崩溃", "SELECT TOP 5 WITH TIES * FROM t;", "SELECT TOP 5 * FROM t;"),
        "doc10": {"scur": "ASTDiff：offset_changed", "scon": "偏移量提取极稳。", "ucur": "底层执行崩溃导致解析失败。", "ucon": "方言支持弱。"},
        "doc11": {"scur": "压入 10 行以上数据，触发截断。", "scon": "造数完美穿透。", "ucur": "抛出 EXEC_ERROR。", "ucon": "方言沙盒不可用。"},
        "doc14": {"scur": "替换 LIMIT 节点验证等价。", "scon": "隔离验证成功。", "ucur": "执行全崩，变异瘫痪。", "ucon": "变异断裂。"},
        "doc15": {"scur": "提取 -> 截断反例 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取崩 -> 执行崩 -> 变异崩。", "ucon": "严重盲区。"}
    },
    "Subquery": {
        "supp": ("标量子查询内部错写", "SELECT * FROM a WHERE val > (SELECT AVG(val) FROM a);", "SELECT * FROM a WHERE val > (SELECT MAX(val) FROM a);"),
        "unsupp": ("子查询改为显式 JOIN", "SELECT name FROM u WHERE id IN (SELECT uid FROM vip);", "SELECT DISTINCT name FROM u JOIN vip ON u.id = vip.uid;"),
        "doc10": {"scur": "ASTDiff：aggregate_function_changed (内部)", "scon": "内部子树提取稳定。", "ucur": "报海量结构错位。", "ucon": "未支持子查询解套(Unnesting)。"},
        "doc11": {"scur": "成功碰撞出大于 AVG 但小于 MAX 的行。", "scon": "聚合反例造数稳定。", "ucur": "本质等价，造不出差异。", "ucon": "造数证明等价。"},
        "doc14": {"scur": "替换子查询验证等价。", "scon": "隔离验证成功。", "ucur": "结构断裂无法单节点替换。", "ucon": "变异瘫痪。"},
        "doc15": {"scur": "提取内部错 -> 极值造数 -> 变异闭环。", "scon": "标量子查询能力强。", "ucur": "提取断裂 -> 变异无法接管。", "ucon": "严重盲区。"}
    },
    "Correlated Subquery": {
        "supp": ("关联字段错写", "SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.id = b.a_id);", "SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.name = b.name);"),
        "unsupp": ("NOT EXISTS 改反连接", "SELECT * FROM a WHERE NOT EXISTS (SELECT 1 FROM b WHERE a.id = b.a_id);", "SELECT a.* FROM a LEFT JOIN b ON a.id = b.a_id WHERE b.a_id IS NULL;"),
        "doc10": {"scur": "ASTDiff：correlated_predicate_changed", "scon": "关联条件提取中等。", "ucur": "树结构巨变，未映射。", "ucon": "高级改写不支持。"},
        "doc11": {"scur": "造出同 id 但不同 name 的数据，穿透。", "scon": "极值碰撞勉强穿透。", "ucur": "本质等价，造不出差异。", "ucon": "造数证明等价。"},
        "doc14": {"scur": "替换 EXISTS 验证等价。", "scon": "隔离验证成功。", "ucur": "结构断裂单节点无法替换。", "ucon": "变异引擎失效。"},
        "doc15": {"scur": "提取 -> 反例碰撞 -> 变异闭环。", "scon": "普通关联支持较好。", "ucur": "结构无法对齐。", "ucon": "严重盲区。"}
    },
    "CTE": {
        "supp": ("内部投影缺失", "WITH c AS (SELECT id, val FROM t) SELECT val FROM c;", "WITH c AS (SELECT id FROM t) SELECT val FROM c;"),
        "unsupp": ("单次 CTE 内联展开", "WITH c AS (SELECT * FROM t WHERE val=1) SELECT * FROM c;", "SELECT * FROM (SELECT * FROM t WHERE val=1) AS c;"),
        "doc10": {"scur": "ASTDiff：column_dropped", "scon": "内部解析稳定。", "ucur": "报严重结构差异。", "ucon": "内联视图未规范化。"},
        "doc11": {"scur": "沙盒抛出 Column Not Found 报错。", "scon": "运行期发现缺失列。", "ucur": "等价逻辑无差异。", "ucon": "造数等价。"},
        "doc14": {"scur": "替换 CTE 内部节点后通过。", "scon": "隔离验证成功。", "ucur": "结构断裂变异失效。", "ucon": "变异失效。"},
        "doc15": {"scur": "提取缺列 -> 报错验证 -> 变异恢复。", "scon": "基础 CTE 稳定。", "ucur": "未扁平化断链。", "ucon": "中等瓶颈。"}
    },
    "Recursive CTE": {
        "supp": ("递归步长错误", "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT * FROM t;", "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+2 FROM t WHERE n < 5) SELECT * FROM t;"),
        "unsupp": ("复杂多重锚点失效", "WITH RECURSIVE t AS (SELECT 1 AS n UNION SELECT 2 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT * FROM t;", "..."),
        "doc10": {"scur": "ASTDiff：recursive_step_changed", "scon": "递归运算提取精准。", "ucur": "树结构错位。", "ucon": "复杂语法支持弱。"},
        "doc11": {"scur": "递归计算输出序列显著不同。", "scon": "步长反例穿透极稳。", "ucur": "SQLite 本地执行易错乱。", "ucon": "降级环境支持不佳。"},
        "doc14": {"scur": "替换递归体后验证等价。", "scon": "隔离验证成功。", "ucur": "执行错乱变异失效。", "ucon": "变异瘫痪。"},
        "doc15": {"scur": "提取 -> 步长差异序列 -> 变异闭环。", "scon": "主能力基石。", "ucur": "执行环境崩溃连累全链路。", "ucon": "中等瓶颈。"}
    },
    "Set Operation": {
        "supp": ("算子混用", "SELECT id FROM a UNION ALL SELECT id FROM b;", "SELECT id FROM a UNION SELECT id FROM b;"),
        "unsupp": ("INTERSECT 改 EXISTS", "SELECT id FROM a INTERSECT SELECT id FROM b;", "SELECT id FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.id = b.id);"),
        "doc10": {"scur": "ASTDiff：set_operator_changed", "scon": "基础算子改变提取极稳。", "ucur": "无映射。", "ucon": "高级改写未规范化。"},
        "doc11": {"scur": "造出重复行数据触发 ALL 差异。", "scon": "随机极易造出不重复数据导致漏判。", "ucur": "等价无差异。", "ucon": "造数等价。"},
        "doc14": {"scur": "替换 UNION ALL 验证等价。", "scon": "隔离验证成功。", "ucur": "结构断裂失效。", "ucon": "变异瘫痪。"},
        "doc15": {"scur": "提取 -> 重复数据碰撞 -> 变异闭环。", "scon": "依赖数据碰撞。", "ucur": "结构断裂全挂。", "ucon": "严重盲区。"}
    },
    "CASE": {
        "supp": ("缺少 ELSE", "SELECT CASE WHEN a=1 THEN 'A' ELSE 'B' END FROM t;", "SELECT CASE WHEN a=1 THEN 'A' END FROM t;"),
        "unsupp": ("互斥 WHEN 打乱", "SELECT CASE WHEN a=1 THEN 'x' WHEN a=2 THEN 'y' ELSE 'z' END FROM t;", "SELECT CASE WHEN a=2 THEN 'y' WHEN a=1 THEN 'x' ELSE 'z' END FROM t;"),
        "doc10": {"scur": "ASTDiff：case_else_missing", "scon": "CASE 结构提取极稳。", "ucur": "误报 case_changed。", "ucon": "互斥条件无序比对未支持。"},
        "doc11": {"scur": "插入 a=3 的数据，触发 NULL 差异。", "scon": "极值穿透稳定。", "ucur": "逻辑等价无差异。", "ucon": "造数证明等价。"},
        "doc14": {"scur": "替换 CASE 后验证等价。", "scon": "隔离验证成功。", "ucur": "造数等价压制结构误报。", "ucon": "变异兜底成功。"},
        "doc15": {"scur": "提取缺 ELSE -> 触发 NULL -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取误报 -> 造数等价 -> 变异兜底。", "ucon": "正例等价保护成功。"}
    },
    "Window": {
        "supp": ("缺少 PARTITION BY", "SELECT RANK() OVER (PARTITION BY d ORDER BY s) FROM t;", "SELECT RANK() OVER (ORDER BY s) FROM t;"),
        "unsupp": ("显式默认 Frame 误报", "SELECT SUM(s) OVER (ORDER BY id) FROM t;", "SELECT SUM(s) OVER (ORDER BY id RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t;"),
        "doc10": {"scur": "ASTDiff：window_partition_missing", "scon": "开窗核心组件判定极稳。", "ucur": "误报 window_frame_added。", "ucon": "默认语义行为未补全。"},
        "doc11": {"scur": "多部门乱序数据成功触发不同排名。", "scon": "窗口反例穿透极佳。", "ucur": "等价逻辑无差异。", "ucon": "造数等价。"},
        "doc14": {"scur": "替换窗口子句后验证等价。", "scon": "隔离验证成功。", "ucur": "造数等价压制误报。", "ucon": "变异兜底成功。"},
        "doc15": {"scur": "提取 -> 排序反例 -> 变异闭环。", "scon": "主能力基石。", "ucur": "提取误报 -> 造数等价 -> 变异兜底。", "ucon": "正例等价保护成功。"}
    },
    "Dialect Boundary": {
        "supp": ("简单降级执行", "SELECT TOP 10 * FROM t;", "SELECT * FROM t LIMIT 10;"),
        "unsupp": ("原生强依赖崩溃", "SELECT * FROM sales PIVOT (SUM(amt) FOR m IN ('Jan')) p;", "SELECT ..."),
        "doc10": {"scur": "通过转译粗略匹配近似。", "scon": "部分简单方言可兜底。", "ucur": "解析崩溃。", "ucon": "复杂方言无法提取 IR。"},
        "doc11": {"scur": "转译为 SQLite LIMIT 后执行成功。", "scon": "降级转译成功。", "ucur": "EXEC_ERROR (PIVOT unsupported)。", "ucon": "完全未穿透。"},
        "doc14": {"scur": "勉强支持节点替换验证。", "scon": "部分存活。", "ucur": "执行环境死机。", "ucon": "变异瘫痪。"},
        "doc15": {"scur": "勉强转译通过。", "scon": "少量存活。", "ucur": "沙盒报错中断。", "ucon": "严重盲区。"}
    }
}

def build_examples(doc_type):
    lines = []
    lines.append("\n可以支撑“目前支持”的样例\n")
    for cat in CATEGORIES:
        info = data[cat]
        lines.append(f"【{cat}】\n")
        lines.append(f"支持的样例：{info['supp'][0]}\n\n标准：{info['supp'][1]}\n\n学生：{info['supp'][2]}\n\n当前表现：{info[doc_type]['scur']}\n\n结论：{info[doc_type]['scon']}\n\n")

    lines.append("\n不支持的（失败/未穿透/未对齐的）样例\n")
    for cat in CATEGORIES:
        info = data[cat]
        lines.append(f"【{cat}】\n")
        lines.append(f"不支持的样例：{info['unsupp'][0]}\n\n标准：{info['unsupp'][1]}\n\n学生：{info['unsupp'][2]}\n\n当前表现：{info[doc_type]['ucur']}\n\n结论：{info[doc_type]['ucon']}\n\n")
        
    return "\n".join(lines)


# Read existing files, keeping everything before the '可以支撑“目前支持”的样例'
for f_name, dtype in [("10-Phase1-结构IR与ASTDiff支持矩阵.md", "doc10"),
                      ("11-Phase1-测试造数支持矩阵.md", "doc11"),
                      ("14-Phase1-测试变异支持矩阵.md", "doc14"),
                      ("15-Phase1-端到端完整支持矩阵.md", "doc15")]:
    p = DOCS_DIR / f_name
    text = p.read_text(encoding="utf-8")
    header = text.split("可以支撑“目前支持”的样例")[0].rstrip()
    
    new_content = header + "\n" + build_examples(dtype)
    p.write_text(new_content, encoding="utf-8")

print("Files successfully updated with all 176 examples!")
