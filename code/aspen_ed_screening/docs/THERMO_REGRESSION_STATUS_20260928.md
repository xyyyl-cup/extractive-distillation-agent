# 从零 Aspen 回归实现状态（2026-09-28）

## 已实现

- `thermo_regression_agent.py` 使用体系描述、文献模型和 VLE 数据生成独立 Aspen Properties 输入文件。
- 当前生成的是 `.inp` 输入文件，并通过 `InitFromFile2` 导入；不把 `.bkp/.aprbkp` 归档格式冒充为输入文件。
- NRTL 适配器已生成 C3F8/C3F6 的训练数据输入，训练集和留出集分开。
- UNIQUAC、Wilson、PR、PRWS 尚未通过 Aspen 语法和引擎验证，会阻塞而不会自动改用 NRTL。
- 结果审计会阻止仅有物性表、无回归完成、无参数回读或无独立验证的参数进入流程模拟。

## 实测阻塞

- 2026-09-28，Aspen Properties GUI/COM 能处理生成输入的输入规范，但引擎加载阶段失败。
- 同时运行 Aspen Properties 命令行对官方 `drex4.aprbkp` 和自生成 `.inp` 均返回：`Properties license is not available`。
- 因此目前不能据此判断自生成 NRTL 参数语法已经通过，也不能声称完成 Aspen 回归。

## 许可证恢复后的验收顺序

1. 先用官方 `drex4.aprbkp` 做许可证和引擎健康检查。
2. 再导入自生成 NRTL `.inp`，确认出现 `Data Regression begins`。
3. 确认出现 `Data regression completed`，读取 `ESTVALUE2` 参数。
4. 用预留的留出点计算误差，输出参数包；若无实验基准，状态只能是 `screening_only_predictive`。
