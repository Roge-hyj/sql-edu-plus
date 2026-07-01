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

### 2. 评测与完备性校验类 (2个)
* **`run_all_operator_tests.py`**
  - **作用**：运行 SQL 算子完备性测试。对 16 个经典 DQL 算子/子句与复杂场景用例，进行 AST 传感器、数据传感器与变分测试，生成诊断报告。
* **`run_generation_completeness_tests.py`**
  - **作用**：运行动态造数策略完备性专项检验。对 ParSEval 核心算法在 WHERE 三态、HAVING 聚合、连接漂移、去重等全部 23 种造数策略上的正确性进行断言和报告生成。

---

## 运行提示

所有的脚本均支持在项目根目录下通过 Python 直接运行，例如：
```bash
python data_construct_test/scripts/run_generation_completeness_tests.py
```
部分脚本（如作答模拟）运行前需要确保根目录下已正确配置 `.env` 环境变量中的 AI 密钥等参数。
