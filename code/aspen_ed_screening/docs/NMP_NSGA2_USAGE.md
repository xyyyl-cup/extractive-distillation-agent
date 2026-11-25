# NMP 全变量 NSGA-II 优化说明

本脚本用于闭环 NMP 萃取精馏 Aspen 模板的多目标优化。

## 优化变量

连续变量：

- `SF`
- `RR1`
- `D1`
- `RR2`
- `D2`

整数变量：

- `N1`
- `NF1`
- `NS1`
- `N2`
- `NF2`

默认会约束：

- `NF1`、`NS1` 不超过 `N1`
- `NF2` 不超过 `N2`
- `NS1 < NF1`，即萃取剂进料板在原料进料板上方

## 目标函数

当前版本：

- 最大化 `recovery_C3F8`
- 最小化 `TAC`
- 最小化 `energy_cost`

硬约束：

- Aspen 收敛
- `x_C3F8_top1 >= 0.99999`
- `total_reboiler_duty > 0`
- `total_reboiler_duty <= 1.0e7 cal/sec`
- `TAC > 0`

TAC 由 `tac_model.py` 计算，使用 Aspen 读取到的塔径、塔高、换热负荷、塔顶/塔釜温度以及 `solvents.xlsx` 中的分子量和参考单价。当前 TAC 是筛选模型，不是最终工程报价；假设来源见 `docs/TAC_ASSUMPTIONS.md`。

## 干跑检查

只生成初始种群，不运行 Aspen：

```powershell
& 'C:\Users\熊\AppData\Local\Python\bin\python.exe' optimize_nmp_nsga2.py --dry-run --population-size 24 --generations 1 --output outputs\optimization\nmp_nsga2_dry_run.xlsx
```

## TAC smoke test

只运行初始种群，不进入下一代优化，用于检查 Aspen 收敛、水力学塔径/塔高读取和 TAC 计算：

```powershell
& 'C:\Users\熊\AppData\Local\Python\bin\python.exe' optimize_nmp_nsga2.py --population-size 4 --generations 0 --output outputs\optimization\nmp_tac_smoke.xlsx
```

## 正式运行

建议先跑一个中等规模：

```powershell
& 'C:\Users\熊\AppData\Local\Python\bin\python.exe' optimize_nmp_nsga2.py --population-size 24 --generations 6 --output outputs\optimization\nmp_nsga2_optimization.xlsx
```

每个 case 完成后会追加写入同名 checkpoint：

```text
outputs\optimization\nmp_nsga2_optimization.checkpoint.jsonl
```

`xlsx` 采用安全写入：先写临时文件，再替换正式文件。这样即使电脑在写 Excel 时死机，checkpoint 仍然能用于恢复。

`--resume` 只建议用于同一版 TAC/水力学脚本中断后的续跑。脚本会自动跳过缺少塔径、塔高等 TAC 原始列的旧缓存，避免旧结果阻止新 case 重跑。

断点续跑示例：

```powershell
& 'C:\Users\熊\AppData\Local\Python\bin\python.exe' optimize_nmp_nsga2.py `
  --population-size 100 `
  --generations 49 `
  --seed 20260513 `
  --seed-from outputs\optimization\nmp_nsga2_tac_trial.xlsx `
  --seed-sheet decision_rank `
  --seed-count 16 `
  --case-work-dir case_files\nmp_formal_100x50 `
  --output outputs\optimization\nmp_nsga2_tac_formal_100x50.xlsx `
  --resume
```

续跑时要保持 `population-size`、`generations`、`seed`、变量范围和 `seed-from` 与原运行一致。脚本会重放 checkpoint 中已经完成的个体，不重新调用 Aspen，然后从断点继续。

总 Aspen 计算次数约为：

```text
population_size * (generations + 1)
```

例如 `24 * (6 + 1) = 168` 次。

## 调整变量范围

示例：

```powershell
& 'C:\Users\熊\AppData\Local\Python\bin\python.exe' optimize_nmp_nsga2.py `
  --population-size 32 `
  --generations 10 `
  --sf-range 3.0,4.1 `
  --rr1-range 11,17 `
  --d1-range 8.972,8.990 `
  --rr2-range 2.2,4.8 `
  --d2-range 1.08,1.30 `
  --n1-range 55,90 `
  --nf1-range 35,75 `
  --ns1-range 20,55 `
  --n2-range 4,12 `
  --nf2-range 2,8 `
  --output outputs\optimization\nmp_nsga2_optimization.xlsx
```

## 输出文件

`outputs\optimization\nmp_nsga2_optimization.xlsx` 包含：

- `all_evaluations`：所有已评价点
- `pareto`：可行解中的非支配解
- `final_population`：最后一代种群
- `history`：每一代的可行点数、Pareto 点数和最好指标
- `settings`：本次运行的变量范围和约束

每跑完一个 Aspen case 都会立即写入 Excel，避免中途崩溃丢失结果。
