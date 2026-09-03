# 文档 45：技术类 Full Paper 思考模板填写稿

更新时间：2026-08-31

## 0. 文档定位

本文档逐项填写[技术类 Full Paper 思考模板](/home/roge/projects/sql-edu-main/论文写作 - 技术类Full Paper思考模板（讨论用） 副本.pdf)，目标是把当前 SQL 教学系统压缩成一条可讨论、可验证、可继续扩写为论文 Introduction 的技术故事。

本文档依据以下当前材料整理：

- [文档 39：系统能力评估闭环](/home/roge/projects/sql-edu-main/docs/39-系统能力评估闭环.md)
- [文档 40：历史进度复盘](/home/roge/projects/sql-edu-main/docs/40-历史进度复盘.md)
- [文档 41：真实后端完整执行流程](/home/roge/projects/sql-edu-main/docs/41-真实后端完整执行流程.md)
- [文档 42：工作回顾与当前能力对照表](/home/roge/projects/sql-edu-main/docs/42-工作回顾与当前能力对照表-初版.md)
- [文档 43：论文形式化表达与真实实现对照](/home/roge/projects/sql-edu-main/docs/43-论文形式化表达与真实实现对照-初版.md)
- 当前后端代码、测试和原生 PostgreSQL/MySQL 审计结果。

它不是最终论文，也不是已经完成的实验报告。文中把内容区分为三种：


| 标记      | 含义                             |
| ------- | ------------------------------ |
| 当前代码已支撑 | 已有实现、契约测试或原生运行证据，可以作为系统设计与实现事实 |
| 论文候选论点  | 适合作为论文叙事，但仍需与相关工作做系统对比         |
| 待实验验证   | 不能仅凭代码存在下结论，必须补充外部数据、人工标注或学生实验 |


---



## 1. 快速定位：这篇论文是什么类型



### 1.1 推荐定位

- [ ] 主类型：Technique paper——以一个新算法解决既有 SQL hint generation 问题
- [x] 主类型：Propose a New Research Problem/Setting——把新的受约束教学任务定义本身作为贡献
- [x] 实现形态：System/Technique——用三个技术模块证明该问题设定可以被实现和评测；这不是第二个并列主类型

推荐的主定位是：

> 提出“证据有界的自适应 SQL 教学”问题设定，并实现一个从 SQL 语义证据、作用域诊断、学习状态更新到答案安全反馈的闭环系统。

这里“新问题设定”是候选论文定位，不是已经完成的文献新颖性结论。正式投稿前必须证明：已有 SQL 判题、SQL repair/hint、LLM tutor 和 student modeling 工作尚未同时满足本文定义的证据、诊断、适应性和安全约束。

因此 Introduction 的主轴顺序应当是：

> Research Setting → Limitations → Our Goal/Problem Formulation → Key Ideas → Non-triviality → Methodology → Contributions

不能把文章写成“我们提出了三个模块”，然后到后面才解释这些模块共同解决的研究问题。

### 1.2 为什么不建议只写成“我们做了一个 SQL 教学平台”

如果只写成平台集成，AST、造数、BKT 和 LLM 容易被评审理解为已有组件的简单拼接。论文需要把共同的技术约束抽象成一个清晰任务：

> 系统只有在能够说明“为什么判错、错在何处、该错误是否足以形成学习观测、可以安全透露多少信息”时，才允许进入相应教学动作；否则必须显式放弃判定、归因或学习更新。

这使研究对象从“生成一段 SQL 提示”变成：


Evidence
\rightarrow Diagnosis
\rightarrow Learning\ Observation
\rightarrow Scaffold
\rightarrow Safe\ Feedback


每个箭头都必须带可审计准入条件。

### 1.3 一句话版本

研究问题：

> 如何在 SQL 等价性不可一般判定、方言与 schema 语义异构、学生错误可能相互遮蔽且提示不能泄露答案的条件下，生成证据可追溯、可主动未决并能随学习历史调整深度的教学反馈？

核心洞见：

> 不让 LLM 或表面 AST 差异直接决定教学，而是把“可执行证据”作为跨阶段通行证：先用结构差异驱动定向反例和变异验证，再把强证据绑定到查询作用域、教学路径和原子技能，最后只让 LLM 改写已经批准的教学动作。

方法总览：

> Evidence Compiler（Phase1）→ Scope-aware Diagnostic Planner（Phase2）→ Adaptive and Answer-safe Feedback Controller（Phase3–5）。

结论边界：

> 本系统形成的是具体方言、权威 schema 和有界 witness worlds 下的 operational judgment，不是任意 SQL 的数学等价性证明。

---



## 2. 严格按照 Flowchart 逐格实例化



### 2.1 Flowchart 填写结果


| Flowchart 逻辑阶段                 | 模板要求                              | 本项目的直接填写内容                                                                                                                                                                                                                       | 在 Introduction 中承担的作用                                              | 当前论证状态                                                              |
| ------------------------------ | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------- |
| 论文类型                           | Technique，还是 New Problem/Setting？ | 主类型选择 Propose a New Research Problem/Setting。本文定义 evidence-bounded adaptive SQL tutoring；三个技术模块用于实例化和验证该新设定，而不是把论文降为普通平台集成。                                                                                                      | 决定 Introduction 必须以 Our Goal/Problem Formulation 为主轴，Key Idea 为支撑。 | 论文候选定位；仍需系统文献检索证明设定新颖性。                                             |
| 研究背景                           | 场景是什么，为什么重要？                      | 在数据库课程、在线练习和企业培训中，学生经常提交语法合法但语义错误的 SQL。教师需要判断语义、寻找反例、识别本轮最值得教学的偏离，并决定提示深度；人工过程成本高且难以规模化。本文场景明确给出任务文本、方言、权威 schema/catalog、reference SQL 和服务端题目技能映射，接收 student SQL 与历史状态。                                                         | 建立具体使用场景、输入条件和研究动机，避免泛泛讨论“LLM 教 SQL”。                              | 场景与当前系统输入合同一致；“重要性”仍需课程规模或文献数据支撑。                                   |
| Limitation 1                   | 最新判题或修复方法有什么局限？                   | 固定测试数据可能漏掉比较边界、NULL、重复、无匹配连接行和 CASE 分支；只比较 AST 又会把安全等价改写误当错误。兼容数据库中的执行结果也不能冒充目标原生方言。因此，结构不同和有限数据结果相同都不足以直接形成教学结论。                                                                                                                | 说明为什么需要“结构驱动、原生执行、能够 abstain”的证据机制。                                | 当前代码和审计已证明这些机制可运行；与最新 test-generation/equivalence baseline 的优势仍待实验。 |
| Limitation 2                   | 最新诊断或 hint 方法有什么局限？               | 一个提交可能同时出现 JOIN、WHERE、GROUP、SELECT 等多个差异，其中一些是独立错误，另一些只是上游错误造成的下游症状。扁平 diff、文本顺序或单个 repair 无法可靠区分 ROOT、CTE、派生表、子查询和集合分支中的首要教学偏离。                                                                                                 | 说明为什么需要查询块作用域、证据等级、路径排序和因果抑制。                                      | 作用域图和 20 条规则已实现；真实学生根因标注准确率尚待评测。                                    |
| Limitation 3                   | 最新自适应或 LLM 教学方法有什么局限？             | 直接让 LLM 同时决定正确性、错因、支架和语言，可能捏造证据、改变 verdict 或泄露 reference SQL。把平台失败、弱差异和未映射复杂结构写入 BKT 又会污染学习状态。                                                                                                                                   | 说明为什么必须分离事实权威、学习观测准入和语言表达。                                         | validator、确定性 fallback、观测门控和单场景在线 LLM gate 已实现；语言质量、校准和学习效果未完成。     |
| Our Goal / Problem Formulation | 新问题/新设定到底定义什么？                    | 给定教学任务 \tau=(d,C,q^r,Q,QM)、第 t 次 student SQL q_t^s 和历史 H_t，产生有界判定 J_t、诊断 D_t、支持需求 N_t、L1–L4 等级 L_t、动作 A_t 和反馈 F_t。判题、归因、技能观测和反馈分别具有独立准入条件；证据不足时允许在对应层级主动停止。                                                                      | 这是本文 Introduction 的中心句，也是候选第一项贡献。                                  | 问题合同已由系统实例化；“新问题”本身是否新颖仍需 related-work novelty audit。               |
| Key Idea 1                     | 为什么这个 Goal 合理且可实现？                | 把证据视为跨阶段的 typed capability token：AST diff 只能申请 obligation；真实 witness 才能支撑结果差异；mutation 和 scope binding 才能升级具体错因；精确 rule→skill 才允许更新学习状态。任一级不足就保留 UNDECIDED、GAP、unresolved 或 SKIP。                                                | 同时回应 Limitation 1、2、3，而不是与某一个局限机械一一对应。                             | 核心机制已体现在当前代码合同；学术新颖性和消融收益待验证。                                       |
| Key Idea 2                     | 第二个核心洞见是什么？                       | 分离 authority from language：Phase1 决定有界 verdict，Phase2 决定证据支持的教学目标，Phase3/4 决定 skill 与 scaffold，LLM 只在已有候选中复核或改写批准片段。                                                                                                             | 解释为什么系统既可以使用 LLM，又不把事实权威交给 LLM。                                    | 代码、mock validator 和一次在线 provider gate 已存在；大规模在线质量尚未验证。              |
| 非平凡性                           | 这个 Idea 是否拍脑袋即可实现？                | 不是。其一，diff→witness 是受方言、约束、NULL、bag semantics 和有限预算影响的有界搜索，多 obligation 还会冲突；其二，嵌套 query blocks 与错误传播形成图而非单线顺序；其三，个性化程度与答案泄漏风险存在约束性 trade-off，任何上游缺口都必须阻断学习更新和高强度反馈。                                                             | 证明本文不是把 SQLGlot、数据库、BKT 和 LLM 简单串联。                                | 工程复杂性已真实存在；论文仍需复杂度、成本、失败分布和消融数据。                                    |
| Our Methodology 总领句            | 基于 Goal 和挑战，提出什么？                 | 我们提出一个 evidence-bounded adaptive SQL tutoring framework：Evidence Compiler 形成有界原生语义物证，Scope-aware Diagnostic Planner 选择可证实教学偏离，Adaptive and Answer-safe Feedback Controller 将可信观测转为 L1–L4 动作和安全自然语言。                              | 从问题与洞见自然过渡到方法概览。                                                   | 三个模块均有当前实现；能力覆盖和教育效果必须分别报告。                                         |
| 第一个技术点                         | 方法怎样解决证据搜索问题？                     | Evidence Compiler：完成严格解析与方言决议、schema qualification、AST diff、obligation 编译、ConstraintLedger、multi-world 定向造数、PostgreSQL/MySQL native 或明确标记的 SQLite compatibility 执行、结果比较和 runtime mutation，输出反例、有限未发现反例或显式 gap。                   | 主要解决 Limitation 1 和搜索/方言挑战。                                        | 当前代码已支撑；外部 mutation 集仍存在 witness coverage 缺口。                       |
| 第二个技术点                         | 方法怎样解决首要偏离选择？                     | Scope-aware Diagnostic Planner：利用 Phase1 后置生成的 scope_metadata 构建 ROOT/CTE/DERIVED/SUBQUERY/SET_BRANCH 图，把差异绑定到 14 个逻辑阶段；20 条证据门控规则输出 primary/FDP、independent secondary、suppressed symptom 和 unresolved，只有可靠因果边才抑制下游症状。           | 主要解决 Limitation 2 和嵌套作用域挑战。                                        | 规则、图合同和自动化验收已完成；人工标注的外部诊断准确率未完成。                                    |
| 第三个技术点                         | 方法怎样实现自适应且安全的反馈？                  | Adaptive and Answer-safe Feedback Controller：服务端 Q-matrix 控制正观测，强证据 primary 或符合合同的 independent secondary 经精确 rule→atomic skill 映射后才能形成负观测；历史和原始 BKT 计算 support need 与 L1–L4，Phase4 选择一个批准动作，Phase5 用确定性模板或受约束 LLM 表达，失败时回退安全 L1。 | 主要解决 Limitation 3 以及个性化—安全 trade-off。                              | 工程闭环已实现；BKT 仍为 UNCALIBRATED_MVP，学习收益待学生实验。                          |
| 总结贡献点                          | 常规性总结写什么？                         | 候选贡献为：第一，定义带分级 abstention 和学习观测门控的新教学设定；第二，提出由 obligation、multi-world、native evidence 和 mutation 组成的证据编译机制；第三，提出作用域感知的首要教学偏离选择；第四，实现可审计的 L1–L4 与受约束 LLM 闭环，并设计覆盖语义、诊断、安全和学习效果的分层评测。                                              | 在 Introduction 末段重述“问题—方法—系统—评测”四层贡献。                              | 前三项有工程事实；第四项中的真实学习效果不能在实验完成前用过去式声称。                                 |




### 2.2 Flowchart 对应的 Introduction 段落顺序


| Introduction 段落 | Flowchart 节点                 | 这一段应完成的叙事动作                                                                                     | 不应提前写什么                   |
| --------------- | ---------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------- |
| 第 1 段           | 研究背景                         | 定义 SQL 教学场景、教师实际任务以及为什么自动反馈重要。                                                                  | 不提前罗列 Phase1–5、类名和测试数。    |
| 第 2 段           | Limitation 1                 | 从固定数据、AST-only 和 compatibility/native 差异说明语义证据缺口。                                               | 不把“没找到反例”写成数学等价。          |
| 第 3 段           | Limitation 2                 | 从多错误传播和嵌套 query blocks 说明首要教学偏离难以定位。                                                            | 不声称识别学生心理上的唯一根因。          |
| 第 4 段           | Limitation 3                 | 从不可信观测、支架选择和 LLM 越权说明闭环安全问题。                                                                    | 不声称 BKT 已校准或 LLM 已提升学习效果。 |
| 第 5 段           | Our Goal + Key Ideas         | 正式定义 evidence-bounded adaptive SQL tutoring，并提出 typed evidence 与 authority/language separation。 | 不把新设定的新颖性当作已由文献证明。        |
| 第 6 段           | Non-triviality + Methodology | 用搜索空间、作用域依赖和个性化—安全 trade-off 引出三个技术模块。                                                          | 不展开到所有工程字段和状态码。           |
| 第 7 段           | Contributions                | 概括问题定义、三个方法贡献、系统实现和计划评测。                                                                        | 没有真实学生实验时不写“显著提高学习效果”。    |


这一顺序体现了 New Problem/Setting paper 的写法：先让读者接受“为什么需要重新定义任务”，再说明方法如何使这个任务可实现。第 11 节给出的 Introduction 初稿应以这张映射表为修改基准。

---



## 3. 研究背景：用一个运行例子引出问题



### 3.1 典型场景

假设题目要求保留所有左侧业务实体，即使它们没有右侧匹配记录。学生提交使用 INNER JOIN，而 reference SQL 使用 LEFT JOIN。

在普通练习数据中，每一行恰好都有匹配记录：

固定测试只能说明“当前数据没有暴露差异”，不能说明两条查询在题目语义下等价。反过来，AST 又只能看到 LEFT 与 INNER 不同，不能单独证明学生错误，因为 schema 约束或题目语义有时可能让二者在目标域中等价。

当前系统把该结构差异编译为“至少构造一条左侧存在、右侧无匹配”的 obligation，并生成 dangling-row witness。在原生数据库中：

Phase2 才据此形成 S1_OUTER_JOIN_MISUSE 候选，询问“哪一侧实体即使没有匹配记录也必须保留”；Phase3 根据历史决定 L1–L4，Phase5 不返回 reference SQL。

### 3.2 这个例子同时说明了什么

1. 结果相同不等于已经证明等价。
2. AST 不同不等于已经证明学生错误。
3. 有反例不等于已经证明具体结构是唯一根因，还需要 mutation/causal evidence。
4. 找到错因不等于一定允许更新 BKT，还要有唯一 atomic skill 映射。
5. 允许教学不等于可以展示 reference SQL；反馈仍受支架预算和答案安全约束。

这五个“不等于”就是全文的研究动机。

---



## 4. Problem Formulation



### 4.1 教学任务与输入

定义教学任务：


\tau=(d,C,q^r,Q,QM)


其中：

- d：声明的 SQL 方言与版本；
- C：权威 schema/catalog，包括表、列、类型和约束；
- q^r：reference SQL；
- Q：自然语言查询任务；
- QM：服务端维护的题目—原子技能映射。

第 t 次学生请求为：


x_t=(q_t^s,u,a,\ell,H_t)


其中 q_t^s 是 student SQL，u 是用户，a 是 attempt identity，\ell 是语言，H_t 是提交、提示和行为历史。

### 4.2 输出


\mathcal{T}(\tau,x_t)
\rightarrow
(J_t,D_t,N_t,L_t,A_t,F_t,H_{t+1})


其中：

- J_t：CORRECT、WRONG、UNDECIDED 或输入/引擎边界状态；
- D_t：带证据谱系的诊断集合；
- N_t：支持需求；
- L_t\inL1,L2,L3,L4：支架等级；
- A_t：已批准教学动作；
- F_t：学生可见反馈；
- H_{t+1}：在可信观测门控下更新后的状态。



### 4.3 必须同时满足的约束



#### 证据忠实


J_t=WRONG
\Rightarrow
\exists w\in W:
Result_d(q^r,w)\neq Result_d(q_t^s,w)


这里的含义是：WRONG 必须由当前受支持路径中的真实结果差异支撑。它不是对任意 SQL 非等价性的完备定义。

#### 主动未决


\neg SufficientEvidence
\Rightarrow
J_t=UNDECIDED/GAP


没有找到反例、runner 不可达、schema 不可信、scope 无法配对或复杂结构不能安全映射时，系统不得把缺口改写成学生错误。

#### 学习观测可信


NegativeObservation(k)
\Rightarrow
PrimaryOrIndependent(k)
\land StrongEvidence(k)
\land ExactAtomicMapping(k)


syntax、安全、平台失败、弱候选和未映射复杂结构不产生负向 BKT 观测。

#### 答案安全


F_t
=Render(A_t,\ell)



A_t
\not\supset
q^r,\text{replacement SQL},\text{private witness database}


LLM 不能改变 J_t,D_t,N_t,L_t，也不能创造 evidence、rule 或 skill。

### 4.4 与普通 SQL repair 任务的区别

普通 repair 任务常写成：


Repair(q^s,q^r)\rightarrow q'


本文任务不要求直接给学生返回修复后 SQL，而是：


Evidence
\rightarrow
TeachableDiagnosis
\rightarrow
ScaffoldedAction
\rightarrow
AnswerSafeFeedback


目标不是替学生完成查询，而是在证据允许的范围内帮助学生自行完成修复。

---



## 5. 三个 Limitation 的展开



### 5.1 Limitation 1：判题证据与教学结论之间存在断层

现有思路通常依赖以下一种或多种信号：

- 固定测试数据库上的结果比较；
- SQL 文本或 AST 距离；
- 受限 fragment 上的符号等价；
- 从 target query 推导 repair；
- LLM 对两条 SQL 的直接比较。

这些信号分别有价值，但不能被无条件升级为教学结论：


| 信号         | 能说明什么       | 不能单独说明什么     |
| ---------- | ----------- | ------------ |
| 固定数据结果相同   | 当前数据未发现差异   | 所有允许数据库状态下等价 |
| AST 不同     | 两种写法的结构存在差异 | 学生一定语义错误     |
| 生成了 repair | 存在一条可行修复路径  | 该位置是学生的唯一根因  |
| LLM 判断     | 语言模型认为某解释合理 | 真实数据库语义已被验证  |


Qr-Hint 已明确指出 SQL query equivalence 的不可一般判定以及多种等价写法带来的 hint 难题，并通过可行 repair 序列提供强保证；本文不应把“沿逻辑路径给提示”或“避免只看语法差异”声称为首次提出。本文需要证明的差异化方向是：在更广的有界、多方言教学运行环境中，把 counterexample、abstention、scope-aware diagnosis、学习观测和答案安全反馈统一成一条证据契约。

参考：

- [Qr-Hint: Actionable Hints Towards Correcting Wrong SQL Queries](/home/roge/projects/sql-edu-main/Actionable Hints Towards Correcting Wrong SQL.pdf)



### 5.2 Limitation 2：局部差异不等于首要教学根因

SQL 中经常存在传播关系：

如果把这些差异一次性平铺给学生，会增加认知负担；如果仅选择 SQL 文本中最早出现的子句，又会忽略 CTE、子查询、集合分支和真实证据。

本文关注的不是“绝对心理根因”，而是一个更可验证的目标：

> 在可靠配对的 query scope 和证据支持的 causal roots 中，选择教学路径上最早、最值得本轮处理的偏离点。

这一定义主动排除“从一次 SQL 猜学生是粗心还是不会”的心理推断。

### 5.3 Limitation 3：个性化与生成式反馈缺少证据准入和安全边界

学生模型需要可靠观测，LLM 需要受限权限。否则会出现两类错误：

1. 系统缺口污染学习状态：例如 native runner 不可达，却被记录成学生不会 PostgreSQL。
2. 语言模型越权：例如 LLM 改变 WRONG/CORRECT、捏造 witness、选择未授权 skill，或输出 reference SQL。

因此，本文不是简单地“在最后加一个 LLM”，而是把 LLM 放在权威判定、诊断、学习目标和支架等级都已冻结之后，并对输入字段、输出动作、答案片段和失败回退建立约束。

---



## 6. Key Idea 与非平凡性



### 6.1 Key Idea 1：Evidence as a Typed Capability Token

系统把证据视为进入下一阶段的能力令牌：

每次升级都检查前置条件。一个差异可以被识别但无法造数；可以被执行区分但无法原子归因；可以被归因但无法映射到 skill。系统允许链路停在任何位置，而不是用猜测补齐。

### 6.2 Key Idea 2：Separate Authority from Language

系统将“谁有权决定事实”与“谁负责表达事实”分开：


| 内容     | 权威来源                          |
| ------ | ----------------------------- |
| SQL 判定 | Phase1 执行证据与安全守卫              |
| 首要错因   | Phase2 规则、作用域、证据和因果边          |
| 是否更新技能 | Phase3 Q-matrix/rule→skill 准入 |
| 支架等级   | Phase3 历史状态和固定策略              |
| 教学动作   | Phase4 版本化 action plan        |
| 自然语言   | Phase5 确定性渲染或受约束 LLM          |


LLM 负责“怎样说”，不负责“事实是什么”。

### 6.3 为什么这不是一个直接拼接即可完成的系统



#### 挑战一：有界数据搜索与真实方言语义同时存在

对每个 AST diff 进行随机造数会造成搜索空间膨胀，也容易生成无法触发差异的数据。定向策略又必须处理 PK/FK、nullable、类型、bag semantics、边界、冲突 obligation 和行数上限。SQLite 能复现的现象不能冒充 PostgreSQL/MySQL 原生语义。

#### 挑战二：诊断关系不是简单线性排序

不同 query block 各自有 FROM/WHERE/GROUP 等路径；CTE feeds、相关子查询和 LATERAL 又构成跨块依赖。阶段靠前不自动意味着因果上游，AST 相似也不自动意味着可配对。系统需要区分 CAUSES、MASKS、RELOCATES_TO 和仅共现的 CO_OCCURS。

#### 挑战三：安全退出必须贯穿整条教学闭环

UNDECIDED 不能只是一条日志；它必须阻止 Phase2 伪归因、Phase3 BKT 更新和 Phase4/5 个性化反馈。类似地，LLM validator 失败后不能让事务留下“已按 L4 教学但实际只交付空文本”的不一致状态。

---



## 7. Our Methodology：三个技术点如何组成一条真实流程



### 7.1 总体架构

论文中的方法不应写成五个彼此孤立的功能，而应写成一条带准入门槛的数据流：

这里最关键的系统属性不是“每次都能给出具体提示”，而是每个模块都能拒绝把能力缺口升级为更强结论：


Parseable
\not\Rightarrow Executable
\not\Rightarrow Equivalent
\not\Rightarrow RootCause
\not\Rightarrow LearningObservation
\not\Rightarrow AdaptiveHint


### 7.2 Technical Point 1：Evidence Compiler



#### 输入

- reference SQL 与 student SQL；
- 题目声明方言；
- 权威 schema/catalog；
- 执行 backend 与资源上限。



#### 真实处理顺序

1. 输入门禁、安全检查、严格解析与方言决议，确保两侧是单条只读 Query，且声明方言与检测到的方言特征不冲突。
2. schema qualification 分别检查标准侧和学生侧所引用的物理表、列、限定名、别名和可见作用域。标准侧无法由权威 catalog 支撑时，作为 INPUT_GAP 停止，而不是归咎学生。
3. extract_ast_diffs() 在当前支持的结构上生成 ast_diffs，并为差异分配稳定 diff_id。
4. generate_witness_suite() 内部调用 compile_obligations()，把非冗余结构差异编译成“什么数据条件可能让两侧结果分开”的 obligation；缺少专用模板时，通用 fallback obligation 只能进入后续验证，不能单独充当语义证据。
5. witness planner 将 obligation 分配给一个或多个独立 world。ConstraintLedger 记录表、行、列、硬约束和软约束；相互冲突的要求被拆到不同 world。
6. runner 在隔离空间物化 fixture，并在已选择的 SQLite compatibility、PostgreSQL native 或 MySQL native backend 上执行 SQL pair。限定 schema 名只有经过权威 catalog 授权才可映射。
7. 结果比较保留列、行、重复、多重集差集和顶层排序语义。任一 world 产生真实差异即可形成 NOT_EQUIVALENT 反例；没有反例只表示所测 worlds 内 NO_COUNTEREXAMPLE_FOUND。
8. runtime mutation 在相同数据条件下替换或移除单个候选结构，检查修复后是否恢复 reference 结果；多错误遮蔽或变异不可执行时保留不确定证据。
9. 最后生成 scope_metadata，并挂到 SandboxRun.data_evidence：登记 ROOT、CTE、DERIVED、SUBQUERY、SET_BRANCH，生成 parent/composition edges，把 diff_id 绑定到可靠配对的 conceptual scope，供 Phase2 建图。



#### 输出


E_1 =
(J,\Delta,O,W,R,M,S,L)


其中：

- J：有界 verdict；
- \Delta：带 stable diff_id 的 ast_diffs；
- O：obligations；
- W：witness worlds 与生成 provenance；
- R：原生或兼容执行结果；
- M：mutation/repair evidence；
- S：scope_metadata；
- L：limitations、uncovered obligations 和 engine/input gaps。



#### 必须写清的当前边界

SQLStructureIR 已实现，但它当前主要在 Phase1 判为 WRONG 后的 error-attribution 辅助路径中归纳单侧结构特征；它不是 obligation/witness 主链的统一中间表示，也不是 Phase2 的主诊断图。Phase2 的作用域定位主要消费后置生成的 scope_metadata，并据此构建 ScopedQueryGraph。论文不能把这两个对象写成同一个组件。

### 7.3 Technical Point 2：Scope-aware Diagnostic Planner



#### 输入

Phase2 只接收已经通过 authoritative Phase1 gate 的结果。它不会重新执行 SQL，也不能把 Phase1 的 UNDECIDED 改成 WRONG。

#### 作用域建图

系统从 scope_metadata 构建 ScopedQueryGraph：

- scope node 表示 ROOT、CTE、DERIVED、SUBQUERY 或 SET_BRANCH 查询块；
- PARENT 表示词法包含；
- CTE_FEEDS 与 DERIVED_FEEDS 表示数据生产者向消费者供数；
- SUBQUERY_OF 表示子查询属于哪个外层块；
- CORRELATED_TO 表示内层查询真实引用外层字段；
- SET_MEMBER_OF 表示 SELECT 属于哪个集合分支；
- LATERAL_TO 表示 LATERAL 查询对外层或左侧来源的依赖；
- diff binding 把证据差异挂到具体 scope 与逻辑阶段。

仅当标准侧和学生侧查询块具有可靠的结构路径与类型对应时才建立 conceptual_scope_id。无法配对的块不被强行合并，对应候选保持 unscoped 或 unresolved。

#### 教学路径与候选生成

每个作用域内部按以下 14 个逻辑阶段组织证据：

逻辑阶段再映射到 S1–S6 教学阶段。当前 20 条确定性规则只在满足自己的结构、作用域和证据门槛时生成候选，例如：

- S1_OUTER_JOIN_MISUSE 需要连接保留语义相关差异及可用反例/因果证据；
- S2_BOUNDARY 需要行过滤比较边界差异及可区分 witness；
- S4_AGG_BOUNDARY 需要组级筛选边界证据；
- S5_CASE_INCOMPLETE 需要 CASE 分支差异及可区分输入；
- 没有安全 rule→atomic skill 映射的窗口等复杂候选可以被识别，但不会被伪装成可学习原子错因。



#### 首要偏离点

候选根据 query scope、逻辑阶段、证据等级和依赖关系分类为：

- primary/FDP：当前证据图中最早且最适合作为本轮教学焦点的可证实偏离；
- independent secondary：有独立证据、不能被 primary 解释的次要错误；
- suppressed symptom：有可靠 CAUSES、MASKS 或 RELOCATES_TO 关系证明是下游症状；
- unresolved：证据、作用域或规则映射不足。

阶段更早本身不构成因果证明。只有已建立的可靠依赖边才允许抑制下游候选；CO_OCCURS 只表示共同出现，不能据此删除候选。因此本文应使用“当前证据图中的最早可证实教学偏离点”，不应使用“学生心理上的唯一根因”。

#### LLM 在 Phase2 的真实位置

当配置启用且 provider 可用时，确定性诊断包可以交给证据约束的 LLM reviewer。模型只能：

- 在已有强候选中选择教学主候选；
- 引用已有 evidence/candidate ID；
- 复核并改写限定的诊断叙述。

模型不能改变 Phase1 verdict、增加 rule/skill/evidence、选择弱候选，或把 SQL、谓词和 witness 行值泄露给学生。格式、ID、证据或安全校验失败时使用确定性诊断结果。

### 7.4 Technical Point 3：Adaptive and Answer-safe Feedback Controller



#### Phase3：可信观测与支持需求

系统把“诊断结果”与“允许写入学生模型的观测”分开：

- 正确提交只有在服务端存在权威题目 Q-matrix 且该映射允许观测时，才能形成正观测；
- 错误提交只有在 primary 或 independent secondary 候选具有强证据，并且 rule 能一对一映射到 atomic skill 时，才能形成负观测；
- syntax、安全拦截、INPUT_GAP、ENGINE_GAP、UNDECIDED、unresolved、unscoped-only 和弱候选均跳过技能观测。

已准入观测与历史提交、提示、停留等行为共同进入当前 BKT/support policy，产生 support need 和 L1–L4。相同 attempt 的重放通过幂等键避免重复学习。

当前应严格称为“工程闭环已实现的原始 BKT 支架策略”，并持续标注 UNCALIBRATED_MVP；没有真实学生轨迹时，不能把 mastery 概率解释为经过教育测量验证的能力。

#### Phase4：从等级到一个受控教学动作

Phase4 不是直接生成文本，而是把 verdict、primary candidate、support need 和 L1–L4 编译成版本化 TeachingAction：

- L1：最少方向提示或反思问题；
- L2：指出应检查的逻辑阶段；
- L3：给出更具体的语义冲突和检查步骤；
- L4：在仍不提供 reference SQL 的前提下，给出最强的分步支架。

错误提交每轮只聚焦一个获准教学目标。若 candidate、rule、stage、verdict 或 lineage 不一致，则拒绝自适应动作并回退安全 L1。

#### Phase5：确定性渲染与受约束 LLM

Phase5 首先存在不依赖模型的确定性 renderer，因此 provider 不可用时仍可返回非空、安全、可审计反馈。启用 LLM 时，模型只对批准的 action segments 做自然语言改写，并经过以下校验：

- 不改变 verdict、target、skill 或 L1–L4；
- 不新增证据事实；
- 不输出 reference SQL、可复制 SQL-shaped 答案、内部 ID 或私有 witness；
- 满足长度、结构和语言要求；
- 超时、协议、JSON 或安全检查失败时回退确定性模板。

最终反馈、动作版本、证据 lineage、模型来源和回退原因与 Submission、行为事件和学习事件一起在最终数据库锁下持久化。

### 7.5 挑战与方法的一一对应


| 模板中的挑战                 | 对应机制                                                  | 当前可验证结果                                           | 尚不能声称                         |
| ---------------------- | ----------------------------------------------------- | ------------------------------------------------- | ----------------------------- |
| Challenge 1：如何获得真实语义证据 | diff→obligation→multi-world→native execution→mutation | 能在明确 schema/方言/资源边界内产生反例、记录无反例和显式 gap             | 任意 SQL 的完备等价证明                |
| Challenge 2：如何找首要教学偏离  | scope graph、14 阶段、20 规则、证据分级和因果抑制                     | 能对已覆盖形状输出 primary、secondary、suppressed、unresolved | 学生真实心理根因或所有复杂 SQL 的精确归因       |
| Challenge 3：如何自适应且不泄漏  | 观测准入、BKT/support need、L1–L4、TeachingAction、受限 LLM     | 工程合同、回退、幂等和单场景在线 LLM gate 已存在                     | 已证明提升学习效果、BKT 已校准或 LLM 语言质量领先 |


---



## 8. Contribution：论文可以怎样写贡献



### 8.1 推荐的四项贡献


| 候选贡献       | 推荐表述                                                                           | 当前证据状态                                     | 投稿前还需要什么                                           |
| ---------- | ------------------------------------------------------------------------------ | ------------------------------------------ | -------------------------------------------------- |
| C1 问题设定    | 定义 evidence-bounded adaptive SQL tutoring：允许判题、归因、学习更新和反馈分别 abstain，并要求跨阶段证据谱系 | 论文候选论点；系统契约已实例化                            | 系统文献检索，证明不是已有 SQL hint、repair、KT 与 LLM tutor 的同义重述 |
| C2 证据编译    | 提出把 AST 差异编译为 obligation、冲突隔离 witness worlds、原生执行和 mutation evidence 的有界判题管线   | 当前代码已支撑；已有 PostgreSQL/MySQL 与多组回归          | 统一公开 benchmark、强 baseline、覆盖/准确率/成本和消融实验           |
| C3 作用域诊断   | 提出 scope-aware evidence graph，把差异绑定到嵌套查询块和教学路径，并只用可靠因果边选择首要偏离                  | 20 条规则、scope contract 与自动化验收已存在            | 人工标注的真实学生错因集、标注一致性和 root-selection 对比              |
| C4 自适应安全反馈 | 提出从可信 skill observation 到 L1–L4 TeachingAction，再到受约束 LLM 的答案安全控制器              | 工程闭环、validator、fallback、审计与一次在线集成 gate 已存在 | 学生实验、BKT 校准、教师盲评、红队泄漏和模型漂移实验                       |




### 8.2 可直接用于英文论文的贡献草稿

以下是写作草稿，不是当前即可提交的最终 claim：

1. We formulate evidence-bounded adaptive SQL tutoring, in which semantic judgment, fault attribution, learning-state update, and feedback generation have separate evidence admission and abstention conditions.
2. We design an evidence compiler that turns dialect-aware, schema-qualified AST differences into distinguishing obligations, isolated witness worlds, native-engine counterexamples, and mutation-based repair evidence.
3. We introduce a scope-aware diagnostic planner that binds evidence to nested query blocks and selects the earliest teachable, evidence-supported deviation without treating every downstream symptom as an independent root cause.
4. We implement an adaptive, answer-safe controller that gates skill observations, selects L1–L4 scaffolds, and constrains LLMs to rewrite approved teaching actions rather than decide correctness or invent evidence.



### 8.3 不能包装成贡献的常见组件

以下组件单独看都不宜声称为本文创新：

- 使用 SQLGlot 解析 AST；
- 在测试数据库执行 reference/student SQL；
- 随机或定向生成测试数据；
- 按 SQL 逻辑顺序组织提示；
- 使用 BKT 记录掌握度；
- 使用 LLM 润色反馈；
- 仅仅把五个 Phase 串起来。

真正需要论证的是这些组件之间的“证据准入、主动未决、作用域绑定、观测门控和答案安全”是否形成了已有工作没有覆盖的统一问题与方法。

---



## 9. 当前实证账本：论文现在能证明到哪里



### 9.1 已有证据


| 对象                       | 当前结果                                                                                                           | 它真实证明什么                                                  | 它不证明什么                          |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------- |
| 大规模生成结构集                 | 1117/1117 的 structure、data、mutation、attribution、full-flow 指标通过相应门槛                                             | 在该生成分布和既定 oracle 下，主链能稳定运行                               | 对真实学生错误分布的外部有效性                 |
| 旧外部 mutation 集           | 77 个有效负变异中：50/77 产生数据反例、57/77 有隔离修复证据、59/77 完成归因；总门槛 FAIL                                                      | 暴露了 witness coverage 的真实缺口，系统没有把 27 个未区分样本当成成功           | Phase1 已对外部语料达到教学级 90%          |
| PostgreSQL 16 公开题对       | PGExercises 62/62；结构、数据、mutation、attribution、full-flow 均为 1.0                                                  | 对该批语料、catalog、mutation 和 PG16 backend 的闭环实证              | 所有 PostgreSQL 结构或所有学生提交         |
| PostgreSQL API 教学审计      | 6/6 identity、6/6 selected mutation、0 witness gap、learner-safe 1.0                                              | cd.* 限定名及 CASE、窗口、DISTINCT、UNION、递归 CTE 等选定场景可走原生链路或安全跳过 | 每个被识别结构都有原子 skill 映射            |
| MySQL 8.0.46 native gate | live tests 为 7 passed；教学审计 5/5 identity、5/5 mutation、0 witness gap、learner-safe 1.0；14 路由分支为 CORRECT=6、WRONG=8 | 指定版本与大小写门禁下，原生执行及 LEFT JOIN/CASE 等选定场景闭环可运行              | 其他 MySQL 版本、Windows 大小写语义或全方言等价 |
| Phase2                   | 7/7 acceptance groups、170/170 tests、20/20 rule matrix                                                          | 已实现规则、作用域和路由合同在测试集内一致                                    | 对真实学生首要错因的人工标注准确率               |
| Phase3–6                 | 观测准入、BKT、L1–L4、动作、反馈、幂等与审计测试存在                                                                                 | 工程状态更新和安全回退闭环存在                                          | 参数校准、因果学习收益或长期个性化有效性            |
| 在线 LLM gate              | CC Switch 的 jiji Responses provider 与 gpt-5.6-luna 完成最小请求及一条 Docker PostgreSQL Phase1→Phase5 集成 gate           | provider 协议、validator、fallback 和审计链路可以在线运行               | 模型规模化成功率、延迟成本、语言质量、漂移和教学效果      |




### 9.2 关键证据文件

- [系统能力评估 final v4](/home/roge/projects/sql-edu-main/data_construct_test/outputs/system_capability_evaluation_20260826_final_v4.md)
- [PostgreSQL 62 题 Phase1 收敛报告](/home/roge/projects/sql-edu-main/data_construct_test/outputs/real_teaching_phase1_pg_native_20260827_v5/phase1_cfg_convergence_report.md)
- [PostgreSQL Phase0–Phase6 教学审计](/home/roge/projects/sql-edu-main/data_construct_test/outputs/real_teaching_scenario_audit_pg_native_20260827_v3.md)
- [MySQL Phase0–Phase6 教学审计](/home/roge/projects/sql-edu-main/data_construct_test/outputs/real_teaching_scenario_audit_mysql_native_20260828_v4.md)
- [真实后端完整执行流程](/home/roge/projects/sql-edu-main/docs/41-真实后端完整执行流程.md)



### 9.3 当前最诚实的总评

> 当前系统已经是一个有明确支持边界、原生数据库物证、作用域诊断、状态更新、支架决策和安全 LLM 回退的研究原型；它足以支撑方法论文的系统设计与工程可行性部分，但还不足以支撑“普遍准确”“达到市面一流水准”或“显著提升学生学习效果”的结论。

---



## 10. Evaluation Plan：Full Paper 必须补齐的实验



### 10.1 RQ1：有界语义判断是否比常见基线更可靠

问题：

> obligation-driven multi-world native execution 是否提高反例发现率，并减少把系统缺口误判成学生错误的情况？

建议基线：

- 仅 AST difference；
- 单个固定测试数据库；
- 随机数据生成；
- SQLite-only compatibility；
- 去掉 mutation 的版本；
- 可复现时加入 XData、SQLRepair 或同类 test-generation/repair 方法。

指标：

- WRONG precision/recall；
- counterexample detection rate；
- no-counterexample acceptance precision；
- abstention coverage 与 selective risk；
- native/compat agreement；
- obligation coverage；
- witness worlds、行数、执行时间和超时率。

必须分别报告 PostgreSQL、MySQL 和 SQLite compatibility，不能把三者合并成“多方言平均正确率”而隐藏 backend 差异。

### 10.2 RQ2：作用域和证据图是否改善首要错因选择

构建真实学生 SQL 或专家植入的多错误数据集，由至少两名 SQL 教师独立标注：

- 所有可见错误；
- 首个应教学的偏离；
- 独立次要错误；
- 下游症状；
- 无法确定的案例。

报告：

- candidate precision/recall；
- primary/FDP accuracy；
- scope binding accuracy；
- independent secondary recall；
- erroneous suppression rate；
- unresolved calibration；
- Cohen's kappa 或 Krippendorff's alpha。

消融：

- 无 scope graph；
- 仅扁平阶段排序；
- 无 mutation/causal evidence；
- 把 CO_OCCURS 错当因果；
- 无 LLM reviewer；
- 仅 LLM reviewer。



### 10.3 RQ3：L1–L4 与受约束 LLM 是否安全且有用

对同一批诊断包生成：

1. 固定通用提示；
2. 确定性 L1–L4；
3. 不受约束 LLM；
4. 本文受约束 LLM。

由教师盲评：

- 事实忠实度；
- 教学可操作性；
- 提示深度是否适当；
- 是否泄露 reference SQL 或等价可复制答案；
- 语言自然性；
- 错误目标聚焦度。

自动红队集应覆盖 reference SQL 直接泄漏、谓词/常量泄漏、SQL-shaped text、Unicode/编码绕过、长文本、恶意 provider 输出、模型超时和 JSON 异常。同步报告 fallback 率、延迟和成本。

### 10.4 RQ4：系统是否改善真实学习结果

这是支撑教育效果 claim 的必要实验，而不是可选附录。建议至少比较：

- 固定 correctness-only 反馈；
- 固定通用 hint；
- 证据诊断但无历史适应；
- 完整 evidence-bounded adaptive feedback。

主要指标：

- 首次修正率；
- 修正所需提交次数；
- 完成时间；
- 提示使用率；
- 延迟后测；
- 新题迁移表现；
- 对系统信任和认知负担的问卷。

应按先验能力、题型、方言和错误类型分层，并预注册主要指标，避免只选择表现最好的子组。

### 10.5 RQ5：BKT 与 support policy 是否校准

使用匿名化真实学生序列，按时间划分训练、验证和测试集，比较：

- 当前原始 BKT；
- 题级频率基线；
- logistic/IRT 或其他简单学生模型；
- 去掉行为特征的 BKT；
- 去掉观测准入门控的版本。

报告 Brier score、log loss、校准曲线、ECE、下一题预测和支架决策收益。只有在该实验完成后，才考虑移除 UNCALIBRATED_MVP。

### 10.6 数据与可复现性要求

- 固定 corpus 版本、来源、许可、schema hash、方言和数据库版本；
- 训练/开发/测试按题目或 SQL family 隔离，避免同模板泄漏；
- 固定 mutation seed、world budget 和超时预算；
- 保存 diff→obligation→world→result→mutation→candidate→skill→action 的匿名化 lineage；
- 将 unsupported、input gap、engine gap 和 unexpected failure 分开报告；
- 为每个比例给出分母与置信区间，不能只报 passed 数。

---



## 11. Introduction 的完整故事线



### 11.1 七段式结构


| 段落                         | 要回答的问题             | 本文应写的内容                                                                                                            |
| -------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------ |
| P1 背景                      | 为什么 SQL 教学反馈重要？    | 定义具体教学场景：SQL 声明性和多种等价写法使得“是否语义正确、错在哪里、该提示多少”比普通样例判题更困难。                                                            |
| P2 Limitation 1            | 为什么现有判题证据不足？       | 固定数据漏边界，AST-only 会误报，compatibility execution 不能冒充目标原生方言。                                                           |
| P3 Limitation 2            | 为什么有判定还不等于有可教错因？   | 多错误传播和嵌套 query block 使扁平 diff 或单个 repair 无法可靠选择首要教学偏离。                                                             |
| P4 Limitation 3            | 为什么不能直接交给 BKT/LLM？ | 不可信诊断会污染学习状态；无约束 LLM 可能改变事实、捏造证据或泄露答案。                                                                             |
| P5 Goal + Key Ideas        | 本文重新定义了什么，为什么合理？   | 定义 evidence-bounded adaptive SQL tutoring；用 typed evidence admission 和 authority/language separation 支撑该设定。        |
| P6 Non-triviality + Method | 为什么不直接，如何实现？       | 用有界搜索、作用域依赖和个性化—安全 trade-off 说明挑战，再引出 Evidence Compiler、Scope-aware Diagnostic Planner 和 Adaptive Safe Controller。 |
| P7 Contributions           | 做出了什么贡献？           | 总结问题定义、证据编译、作用域诊断、自适应安全反馈及分层评测；教育效果只能在完成学生实验后写成实验结论。                                                               |




### 11.2 中文 Introduction 初稿

SQL 是数据库课程和数据能力训练中的核心技能，但自动反馈仍常停留在语法错误或固定测试数据上的结果比较。对于语法合法却语义错误的查询，教师不仅要判断答案是否满足题意，还要解释偏离发生在数据来源、连接、行过滤、聚合、投影还是结果整形，并根据学生已有尝试决定提示到什么程度。由于 SQL 具有声明性、多种等价写法以及 NULL、重复、排序和方言差异，“是否正确、为什么错误、应该提示多少”构成了一个相互依赖的教学决策问题。

现有 SQL 判题、测试生成和查询修复技术为语义检查提供了重要基础，但单一证据源很难安全支撑教学结论。固定数据库可能没有包含比较边界、NULL、无匹配连接行或 CASE 的不同分支；静态 AST 对比又可能把安全改写当成错误；在 SQLite 等兼容环境得到的结果也不能自动代表 PostgreSQL 或 MySQL 的原生语义。因此，表面结构不同不能单独证明学生错误，有限数据上结果相同也不能证明查询全局等价。

即使系统已经发现两条查询产生不同结果，也仍未必知道本轮最值得教学的位置。一个提交可能同时包含 JOIN、WHERE、GROUP BY、HAVING 和 SELECT 等多个差异，上游数据源或粒度错误还会制造下游投影与聚合症状。CTE、派生表、相关子查询和集合分支又分别拥有自己的查询作用域。因而，平铺 AST differences、按文本顺序选择第一个差异，或采用一个能够恢复结果的 repair，都不能自然区分首要教学偏离、独立次要错误和被上游解释的症状。

生成式模型和学生建模进一步扩大了反馈能力，也引入了新的权威与安全问题。直接把 reference SQL、student SQL 和历史交给 LLM，模型可能改变已有 verdict、捏造不存在的反例、选择没有证据的错因，或给出可复制答案。另一方面，BKT 等学习模型只有在输入观测可信时才有意义；若把 syntax error、engine failure、弱结构差异或尚未映射的复杂结构记录为技能失败，系统自身的能力缺口就会污染后续支架决策。

本文研究 evidence-bounded adaptive SQL tutoring。给定明确方言、权威 schema/catalog、查询任务、reference SQL、服务端题目技能映射、student SQL 和历史状态，系统生成有界判定、证据诊断、支持需求、L1–L4 教学动作和答案安全反馈。该设定基于两个核心洞见：第一，把证据视为跨阶段的 typed capability token，判题、归因、学习观测和反馈分别具有独立准入条件，证据不足时允许在对应层级主动 abstain；第二，分离 authority from language，由确定性证据链决定事实和动作，只允许 LLM 在既定边界内复核或表达。

实现这一设定并非简单拼接现有组件。结构差异到区分数据是受 schema 约束、NULL、bag semantics、方言和资源预算影响的有界搜索，多个证据目标还可能相互冲突；嵌套 query blocks 和错误传播形成有向依赖图，而不是一条扁平 SQL 顺序；历史个性化与答案泄漏之间又存在必须 fail-closed 的约束性 trade-off。为此，我们设计 Evidence Compiler，将 AST differences 编译为 obligations，并通过隔离 witness worlds、原生/兼容执行和 runtime mutation 形成物证；设计 Scope-aware Diagnostic Planner，将证据绑定到查询块和逻辑阶段，选择最早可证实的教学偏离；设计 Adaptive and Answer-safe Feedback Controller，只把可信原子技能观测转为 L1–L4 动作，再由确定性 renderer 或受约束 LLM 生成反馈。

本文的候选贡献包括：第一，定义带分级 abstention 和学习观测门控的 SQL 教学设定；第二，提出从结构差异、obligation、multi-world 数据到原生执行和 mutation 的 evidence compiler；第三，提出面向嵌套查询的 scope-aware 首要教学偏离选择；第四，实现由可信观测、历史支持需求、L1–L4 和受约束 LLM 组成的可审计闭环。当前原型已在选定 PostgreSQL/MySQL 教学场景和自动化合同测试中证明工程可行性，但外部 witness coverage、真实学生错因标注、BKT 校准、LLM 质量和学习效果仍需由 RQ1–RQ5 的完整实验验证。

---



## 12. Related Work 与新颖性核查



### 12.1 必须覆盖的五组工作


| 相关工作组                                          | 需要比较的核心问题                                | 本文可能的区别                                                 |
| ---------------------------------------------- | ---------------------------------------- | ------------------------------------------------------- |
| SQL equivalence、grading 与 test-data generation | 如何判断或区分 SQL pair；支持哪些 SQL fragment、约束和方言 | 本文强调 evidence lineage、native backend、主动未决及证据如何继续约束教学    |
| SQL repair 与 hint generation                   | 如何生成最小修复或逐步提示；是否保证 repair 正确/局部最优        | 本文不以输出修复 SQL 为目标，而以证据支持的教学偏离、学习观测和答案安全为目标               |
| 程序错误诊断与 misconception modeling                 | 如何从代码差异推断概念错误                            | 本文增加 query-scope binding、原生反例、mutation 和不可靠映射跳过机制       |
| Knowledge tracing 与 adaptive scaffolding       | 如何从历史更新掌握度和选择支架                          | 本文的重点是 SQL 诊断如何成为可信观测；当前 BKT 本身不是算法创新                   |
| LLM tutor 与安全反馈                                | 如何生成自然、个性化反馈并避免 hallucination/leakage    | 本文冻结 verdict、target、skill 和 level，只允许 LLM 引用已有证据并改写批准动作 |




### 12.2 与本地 Qr-Hint 论文的关系

[Actionable Hints Towards Correcting Wrong SQL](/home/roge/projects/sql-edu-main/Actionable Hints Towards Correcting Wrong SQL.pdf) 已经研究逐步、可操作、经过正确性保证并沿逻辑执行顺序组织的 SQL repair hints。因此，以下说法不能作为本文独占的新颖性：

- 不只给 syntactic diff；
- 按逻辑查询处理顺序提示；
- 分步骤帮助学生修正 SQL；
- 认识到一般 SQL 等价性不可判定。

本文需要重点对比的是：

- Qr-Hint 的目标是否包含多方言原生证据和显式 engine/input gap；
- 它是否允许判题、归因、学习观测和反馈分别 abstain；
- 它是否处理 ROOT/CTE/DERIVED/SUBQUERY/SET_BRANCH 的证据作用域谱系；
- 它是否把诊断门控为学生模型观测并选择历史相关的 L1–L4；
- 它是否约束 LLM 只改写批准动作并提供答案泄漏回退。

这些问题必须通过逐篇阅读和对照表回答，不能仅凭摘要推断。

### 12.3 当前最有希望的原创主轴

> 不是某个单独 SQL 算法，而是一种 typed evidence-to-teaching contract：系统把原生执行反例和 mutation 证据逐级绑定到 query scope、teachable deviation、atomic skill、scaffold action 与受限语言输出，并允许每一级独立 abstain。

这仍是“最有希望的论文主轴”，不是已完成的新颖性证明。

---



## 13. Claim Boundary：论文中的红线


| 不应写                         | 应改写为                                                |
| --------------------------- | --------------------------------------------------- |
| 系统证明两条 SQL 等价               | 在声明方言、权威 schema 和有界 witness worlds 下未发现反例，并满足当前接受门槛 |
| 系统支持所有 PostgreSQL/MySQL SQL | 在报告列出的版本、结构形状、catalog 和资源边界内具有原生运行证据                |
| 系统找到了学生真正的根因                | 系统找到了当前 evidence graph 中最早可证实、最适合教学的偏离点             |
| 所有差异都能映射到知识点                | 只有满足 rule→atomic skill 合同的候选进入学习观测，其他安全跳过           |
| BKT 已能准确估计掌握度               | BKT 工程链路已实现，当前为 UNCALIBRATED_MVP                    |
| LLM 提升了诊断和教学质量              | LLM 接入、约束和单场景在线 gate 已验证；质量与效果待系统实验                 |
| 系统已达到市面一流水准                 | 当前是能力边界清晰的研究原型；市场领先需要公开基准、竞品对比和真实学习效果               |
| 1641 passed 证明系统完全正确        | 自动化回归在覆盖范围内通过；skipped、warning、外部分布和真实学生效果仍需单独报告     |


系统的安全退出不是附带错误码，而是方法的一部分：

- INPUT_GAP：题目/reference/schema 不足，不能归咎学生；
- ENGINE_GAP：缺少声明方言的可信原生执行能力；
- UNDECIDED/KNOWN_GAP：结构差异存在，但没有足够可验证物证；
- PARTIAL/unscoped：查询块定位不足，不强行宣称具体根因；
- SKIP_*：不满足学习观测或自适应教学准入；
- deterministic fallback：LLM 不可用或违规时保留安全反馈。

---



## 14. Full Paper 推荐目录

1. Introduction
2. Related Work
  - SQL grading and semantic equivalence
  - Test-data generation and query repair
  - SQL hint generation
  - Knowledge tracing and adaptive scaffolding
  - LLM-based educational feedback
3. Problem Setting
  - Task input/output
  - Evidence and abstention semantics
  - Threat and answer-leakage model
4. System Overview
5. Evidence Compiler
  - Dialect/schema qualification
  - AST differences and obligations
  - Constraint-ledger witness worlds
  - Native execution and mutation evidence
6. Scope-aware Diagnostic Planner
  - Scoped query graph
  - Logical teaching path
  - Evidence grading and causal suppression
  - Constrained LLM reviewer
7. Adaptive and Answer-safe Feedback Controller
  - Observation admission
  - Support need and L1–L4
  - TeachingAction and constrained realization
8. Implementation
  - PostgreSQL/MySQL/SQLite backends
  - Security, resource isolation, idempotency and audit
9. Evaluation
  - RQ1–RQ5、baselines、ablations 与 error analysis
10. Discussion and Limitations
11. Ethics, Privacy and Deployment Considerations
12. Conclusion

---



## 15. 下一步工作与完成顺序



### 15.1 投稿前的最小必做集

1. 完成系统性文献矩阵，尤其逐项复核 Qr-Hint、SQLRepair、XData/ParSEval 类工作、SQL tutor、KT 和 LLM feedback，冻结可辩护的新颖性句子。
2. 冻结一套公开可复现 benchmark，将 1117 生成集、77 外部 mutation、PGExercises 和 MySQL 教学集按来源、family、方言和 schema 去重划分。
3. 优先处理旧外部 mutation 集中的 witness gaps，并把“新增覆盖”与“对原测试过拟合”通过隐藏测试分开。
4. 建立教师标注协议和多错误真实学生 SQL 数据，用于验证 scope binding、primary/FDP、secondary 和 suppression。
5. 进行 LLM 离线固定语料评测与泄漏红队，记录模型版本、provider、延迟、成本、fallback 和 validator 原因。
6. 收集真实学生轨迹并完成 BKT 校准；在此之前保留 UNCALIBRATED_MVP。
7. 进行有对照的学生实验，才能把“安全可运行”升级为“改善教学效果”。



### 15.2 推荐选题方向

如果投数据库或软件系统方向，论文主轴应放在 Phase1–2：

> 多方言、有界、可主动未决的 SQL 证据生成与作用域诊断，Phase3–5 作为教学落地案例。

如果投 AIED/EDM/学习技术方向，论文主轴应覆盖全链路：

> 可靠 SQL 语义证据如何门控学生建模、支架深度和答案安全 LLM 反馈，并通过学生实验验证学习收益。

以当前证据成熟度看，前者的系统/方法可行性更接近完成；后者必须等待真实学生数据、BKT 校准和学习效果实验。

### 15.3 备选标题

1. From Counterexamples to Scaffolds: Evidence-Bounded Adaptive Feedback for SQL Learning
2. Evidence-Bounded SQL Tutoring with Scope-Aware Diagnosis and Constrained LLM Feedback
3. When Not to Teach: Safe Abstention and Evidence-Grounded Scaffolding for SQL Tutors



### 15.4 最终的一句话故事

> 本文不是让 LLM 猜学生哪里错了，而是让每一条教学反馈都沿着可审计链路回答：哪一个结构差异被什么数据和引擎区分，它属于哪个查询块和教学阶段，为什么足以成为本轮技能观测，历史状态允许提示到什么程度，以及最终语言为什么没有越过答案边界。

