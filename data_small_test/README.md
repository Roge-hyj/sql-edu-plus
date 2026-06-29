# 小规模验证数据归档

本目录保存之前已经跑通的小规模验证数据和脚本，用于回溯格式、测试模拟学生流程和感知层判分逻辑。

## 文件说明

- `data_std.json`: 20 道 SQL DQL 标准题目，字段结构为 `id / difficulty / l1 / l2 / schema / q / ans_sql / source`。
- `data_student.json`: 基于 `data_std.json` 生成的模拟学生回答数据，按学生画像聚合，包含 `kp1_matrix / kp2_matrix / records`。
- `data.py`: 调用本地模型模拟学生 SQL 作答的脚本。
- `perception_audit_log.json`: 感知层/判分流程的审计日志。
- `test_perception_layer_audit.py`, `test_perception_v3.py`: 小规模验证测试脚本。

这些文件不作为新大规模数据集的最终产物，只作为格式参考和流程样例。
