# 数据集构建与评测验证脚本 (Scripts)

本目录包含全量数据集构建、模拟作答生成以及评测系统完备性校验的核心 Python 脚本。

## 脚本清单及说明

### 1. 数据集构建与模拟类 (4个)
* **`build_data_std_full.py`**
  - **作用**：解析抽取的 DQL 题目源信息，按教材知识点出现顺序排重与组织，生成全量标准题库 `outputs/data_std_full.json`。
* **`simulate_student_answers.py`**
  - **作用**：调用 LLM 大模型（如 Gemini 接口）按不同的学生画像（如 Newbie, Struggles with JOINs, Logic Master 等）生成模拟 SQL 作答，输出原始的 `outputs/data_student_raw_full.json`。
* **`build_data_student_full.py`**
  - **作用**：解析模拟作答并与标准答案合并，自动通过沙盒计算正确率与知识点偏好，生成最终的 `outputs/data_student_full.json`。
* **`select_initial_diagnostic_20.py`**
  - **作用**：基于知识图谱覆盖率最大化与复杂度均衡策略，从标准题库中精选出 20 道最具诊断代表性的题目，输出为 `outputs/initial_diagnostic_20.json`。

### 2. 外部语料采集类
* **`collect_web_sql_corpus.py`**
  - **作用**：根据 `data_construct_test/sources/web_sql_corpus_manifest.json` 抓取或读取外部 SQL 语料，缓存原始下载，抽取/归一化 `SELECT` / `WITH` 查询，去重后输出 `outputs/web_sql_corpus.jsonl`。当前支持裸 JSON/JSONL/SQL/TXT 和 `.tar/.tar.gz/.tar.bz2` 压缩包；对 WikiSQL 结构化 SQL 会转换成可执行 SQL 和单表 schema。
  - **运行**：`python data_construct_test/scripts/collect_web_sql_corpus.py --max-per-source 5000`
  - **本地/授权数据**：Spider、BIRD 等需要按上游条款下载的数据，可通过 `--local-file /path/to/train.json` 或在 manifest 中增加 `local_path` 接入。
  - **输出**：`outputs/web_sql_corpus.jsonl` 和 `outputs/web_sql_corpus_report.json`，包含来源、成员路径、schema、SQL、CFG 标签、来源哈希和来源分布统计。

### 3. 评测与完备性校验类
* **`run_all_operator_tests.py`**
  - **作用**：运行 SQL 算子完备性测试。对 16 个经典 DQL 算子/子句与复杂场景用例，进行 AST 传感器、数据传感器与变分测试，生成诊断报告。
* **`run_generation_completeness_tests.py`**
  - **作用**：运行动态造数策略完备性专项检验。对 ParSEval 核心算法在 WHERE 三态、HAVING 聚合、连接漂移、去重等全部 23 种造数策略上的正确性进行断言和报告生成。
* **`run_phase1_capability_samples.py`**
  - **作用**：运行覆盖全部 31 个 Phase 1 CFG 标签的攻击性能力基准。逐例记录严格解析、`SQLStructureIR`、AST Diff、动态造数、沙盒判等、变异证据和最终归因，并输出当前可通过与不能通过的 JSON/Markdown 样例。
  - **输出**：`outputs/phase1_capability_samples.json`（完整 SQL、造数数据库和证据）及 `outputs/phase1_capability_samples.md`（能力矩阵和已知盲区）。
* **`run_phase1_cfg_fragment_benchmark.py`**
  - **作用**：按 SQL CFG 产生式及其备选项运行细粒度攻击，不以知识点标签代替语法分支。当前基准包含 150 个解析、等价改写和语义变异样例，逐例记录 Schema、标准/学生 SQL、严格解析、IR、Diff Graph、造数、两侧结果、变异证据、归因和失败阶段。
  - **输出**：`outputs/phase1_cfg_fragment_capability.json`（完整证据）、`outputs/phase1_cfg_fragment_capability.md`（产生式矩阵）、`outputs/phase1_cfg_supported_samples.jsonl`（通过样例）和 `outputs/phase1_cfg_known_gaps.jsonl`（不支持样例）。
  - **判读**：`supported` 只表示当前有界反例数据上的该条具体攻击通过，不是对任意数据库的形式化语义等价证明；`known_gap` 按 parser、IR、执行、造数、归因阶段分类。
* **`run_phase1_cfg_convergence_benchmark.py`**
  - **作用**：将 150 个 CFG 备选项放大到 4 个行数尺度，并按 20 个参数化语义族持续生成攻击；同时可接入 `outputs/web_sql_corpus.jsonl`，把真实外部 SQL 语料派生成身份等价正例和边界/NULL/集合/排序/LIMIT 等语义变异负例。默认规模为 100,000 个参数化攻击 + 最多 50,000 个外部语料派生样本，每 1,000 例统计新增失败签名、反例检出、等价保持、归因命中和 Wilson 置信区间。
  - **大规模运行**：`python data_construct_test/scripts/collect_web_sql_corpus.py --max-per-source 50000`，然后运行 `python data_construct_test/scripts/run_phase1_cfg_convergence_benchmark.py --generated-cases 100000 --web-cases 50000 --batch-size 1000`
  - **收敛式运行**：可加 `--early-stop-after-saturated-batches 20`，当连续 20 个 batch 没有新增 unexpected failure signature 时提前停止并保留 checkpoint。该条件只表示经验反例搜索趋于饱和，不是形式化完备证明。
  - **输出**：`outputs/phase1_cfg_convergence_report.json/.md`、`outputs/phase1_cfg_convergence_all.jsonl`、`outputs/phase1_cfg_convergence_supported.jsonl`、`outputs/phase1_cfg_convergence_failures.jsonl`、`outputs/phase1_cfg_convergence_detailed_evidence.jsonl` 和断点状态文件。`detailed_evidence` 对每个唯一 SQL/数据库尺度组合保留完整 IR、Diff Graph、执行、变异和归因对象。
* **`run_phase1_cfg_database_profiles.py`**
  - **作用**：把每个可执行 CFG 样例交叉运行在定向、空表、单行、均匀、NULL 密集、重复值密集、分组倾斜和连接键对齐数据库上，检查声明等价对是否出现反例，以及非等价对是否能被区分。
  - **运行**：`python data_construct_test/scripts/run_phase1_cfg_database_profiles.py --seeds 8`
  - **输出**：`outputs/phase1_cfg_database_profiles_report.json/.md`、`outputs/phase1_cfg_database_profiles_all.jsonl` 和 `outputs/phase1_cfg_database_profiles_counterexamples.jsonl`。
  - **判读**：该脚本扩大数据库实例覆盖，不构成任意有限数据库上的等价性证明。

---

## 运行提示

所有的脚本均支持在项目根目录下通过 Python 直接运行，例如：
```bash
python data_construct_test/scripts/run_generation_completeness_tests.py
```
部分脚本（如作答模拟）运行前需要确保根目录下已正确配置 `.env` 环境变量中的 AI 密钥等参数。
