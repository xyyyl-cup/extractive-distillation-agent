# 热力学物性证据门禁

主智能体现在把“推荐物性方法”“允许用于候选筛选”和“允许作为论文验证证据”分成三个决定，并采用双门制。方法选择与参数补全按二元对执行，不把某一个体系的模型硬编码成所有体系的默认答案。

1. 启发式分类只能生成检索候选，不再直接授权 Aspen。
2. 优先读取原论文、NIST ThermoML、经审计的 Aspen 数据库导出或 DDB 导出。
3. 物性方法由证据记录里的模型决定；例如原文给出 NRTL 参数时，优先选择 NRTL。
4. 对含萃取剂的三元计算，必须检查三个二元对：原料 A/B、A/萃取剂、B/萃取剂。
5. 发表门：每一对都必须具有同一模型下、方向和单位明确、温压范围明确的完整参数，或具有已经验证的实验数据回归结果。
6. 筛选门有两条补参路线：其一是两个独立方法族的预测共识并由同一二元对实验数据约束；其二是单一 COSMO-RS 预测后回归到**文献为目标体系选定的 Aspen 物性模型**，但必须分别通过代理拟合、相似体系实验基准和决策不确定性三层验证。NRTL 只是可能的目标模型之一。
7. 单一、未经现实基准验证的 UNIFAC 或 COSMO 预测，IDAC/SLE 题录本身、单方向参数或只有文献题录，都不能解除 Aspen 门禁。不同 UNIFAC 数据库的组分覆盖范围必须分别审计，不能以开源数据库缺失推断 Aspen 内部数据库也缺失。
8. 通过筛选门但未通过发表门时，Aspen 可以运行候选筛选；所有结果永久标记为 `screening_only_predictive`，不能声称已经完成实验或文献验证，并须执行参数不确定性分析。
9. 三个二元对全部通过相应门禁后，双向 NRTL 参数会自动写入快速 VLE/α Aspen 输入文件及从零建模规格，不再仅依赖 Aspen 内置数据库。

## 证据文件位置

每个研究任务导出后会生成：

```text
research_task_package/thermodynamic_evidence.json
research_task_package/thermodynamic_evidence_audit.json
research_task_package/thermodynamic_evidence_search_queue.json
```

也可以在任务 JSON 中显式设置：

```json
{
  "thermodynamic_evidence_path": "D:/path/to/thermodynamic_evidence.json"
}
```

显式路径的优先级高于自动生成的任务包文件。

## NRTL 参数记录最低要求

```json
{
  "record_id": "source_pair_record",
  "pair": ["COMP-A", "COMP-B"],
  "data_type": "VLE",
  "source": {
    "type": "primary_article",
    "doi": "10.xxxx/xxxxx",
    "url": "https://doi.org/10.xxxx/xxxxx",
    "title": "Original article title"
  },
  "parameter": {
    "model": "NRTL",
    "form": "aspen_bpval_8",
    "units": {
      "a": "dimensionless",
      "b": "K",
      "alpha": "dimensionless"
    },
    "temperature_range_c": [25.0, 100.0],
    "pressure_range_mpa": [0.01, 0.101325],
    "rows": [
      {"from": "COMP-A", "to": "COMP-B", "values": [0, 0, 0.3, 0, 0, 0, 25, 100]},
      {"from": "COMP-B", "to": "COMP-A", "values": [0, 0, 0.3, 0, 0, 0, 25, 100]}
    ]
  },
  "verification": {
    "status": "verified_exact_parameter"
  }
}
```

允许通过发表门的状态只有：

- `verified_exact_parameter`：直接从可追溯来源提取并核验的完整参数；
- `regressed_and_validated`：利用实验数据回归，并通过独立数据或留出数据验证的参数。

## 筛选级预测共识记录

筛选门额外接受 `predictive_consensus_validated`。该状态不是人工填写一个标签就能通过，程序会同时检查以下条件：

- 至少两个独立预测方法族，例如 UNIFAC 类群贡献法与 COSMO 类量化预测法；两个 UNIFAC 变体仍只算一个方法族；
- 每个预测方法必须保留实际输出文件、哈希或输出记录编号，并明确通过适用性检查；仅填写方法名称不计入共识；
- 群贡献法还必须证明所有组分均有完整且受支持的基团分配；若 EC 等组分缺少可靠基团，UNIFAC 不得计入共识；
- 至少一个同一二元对、来源可追溯的实验记录，如 VLE、IDAC、SLE 或溶解度数据；
- `fit_rmse_ln_gamma <= 0.20`；
- `max_abs_ln_gamma_spread <= 0.70`；
- 回归收敛、目标温度范围被覆盖、候选排序在预测扰动下保持稳定。

示例结构如下。参数值必须来自实际预测和受约束回归，不能直接复制示例占位值：

```json
{
  "record_id": "comp-a_comp-b_predictive_consensus",
  "pair": ["COMP-A", "COMP-B"],
  "source": {
    "type": "predictive_model_consensus",
    "record_id": "local-audit-id",
    "title": "Audited UNIFAC/COSMO constrained NRTL regression"
  },
  "parameter": {
    "model": "NRTL",
    "form": "aspen_bpval_8",
    "units": {"a": "dimensionless", "b": "K", "alpha": "dimensionless"},
    "temperature_range_c": [25.0, 100.0],
    "pressure_range_mpa": [0.01, 0.101325],
    "rows": [
      {"from": "COMP-A", "to": "COMP-B", "values": ["actual values required"]},
      {"from": "COMP-B", "to": "COMP-A", "values": ["actual values required"]}
    ]
  },
  "verification": {
    "status": "predictive_consensus_validated",
    "prediction_methods": [
      {
        "name": "UNIFAC-DMD",
        "independence_group": "group_contribution",
        "applicability_verified": true,
        "group_assignment_complete": true,
        "artifact_path": "path/to/unifac-output.json"
      },
      {
        "name": "COSMO-RS",
        "independence_group": "quantum_chemistry",
        "applicability_verified": true,
        "artifact_path": "path/to/cosmo-output.json"
      }
    ],
    "experimental_constraint_record_ids": ["same-pair-idac-or-vle-record"],
    "fit_rmse_ln_gamma": 0.12,
    "max_abs_ln_gamma_spread": 0.35,
    "ranking_stable": true,
    "regression_converged": true,
    "temperature_range_covered": true
  }
}
```

## 单一 COSMO-RS 补参路线

当目标二元对没有足够实验数据、但可以可靠建立 COSMO-RS 分子与构象模型时，门禁接受 `predictive_property_model_surrogate_validated`。目标模型必须先由原始文献、实际相态和温压范围确定，补参记录还必须引用对应的 `model_selection_record_ids`。该路线不要求第二个预测方法，但必须回答三个不同问题：

1. **代理拟合**：文献选定的模型是否在未参与回归的组成和温度点上复现 COSMO-RS 的两个活度系数、无限稀释端点和相行为；
2. **现实基准**：完全相同的 COSMO-RS 协议是否在至少两个化学相似、具有可追溯实验相平衡数据的体系上达到预先声明的误差标准；
3. **决策稳定性**：将基准误差传播到 NRTL 参数和 Aspen 筛选后，萃取剂排名、通过/淘汰结论和关键流程指标是否仍稳定。

当前默认代理阈值为留出点 `RMSE(ln gamma) <= 0.10`、最大 `|Delta ln gamma| <= 0.30`。相似体系的接受阈值不使用一个跨体系固定数字，而是由每个基准记录的实验类型、实验不确定度和筛选所需精度明确给出。

示例审计字段：

```json
{
  "verification": {
    "status": "predictive_property_model_surrogate_validated",
    "surrogate_property_model": "MODEL-SELECTED-FROM-LITERATURE",
    "model_selection_record_ids": ["traceable-literature-method-record"],
    "prediction_methods": [{
      "name": "COSMO-RS",
      "independence_group": "quantum_chemistry_cosmo",
      "applicability_verified": true,
      "artifact_path": "path/to/cosmo-surface.json",
      "protocol": "level of theory, parameterization, conformer and sigma-profile audit"
    }],
    "fit_rmse_ln_gamma": 0.04,
    "holdout_rmse_ln_gamma": 0.06,
    "max_abs_ln_gamma_error": 0.18,
    "regression_converged": true,
    "temperature_range_covered": true,
    "composition_range_covered": true,
    "infinite_dilution_endpoints_covered": true,
    "phase_behavior_consistent": true,
    "cosmors_reality_benchmark": {
      "chemical_similarity_justified": true,
      "systems": [{
        "system": ["COMP-A", "ANALOG-SOLVENT-1"],
        "same_cosmors_protocol": true,
        "metric_name": "RMSE_ln_gamma",
        "metric_value": 0.12,
        "acceptance_limit": 0.20,
        "source": {
          "type": "primary_article",
          "doi": "10.xxxx/xxxxx",
          "title": "Experimental benchmark source"
        }
      }]
    },
    "ranking_stable": true,
    "uncertainty_propagated": true,
    "decision_margin_exceeds_uncertainty": true
  }
}
```

示例中只展示一个相似体系，正式放行至少需要两个合格基准记录。该状态始终属于 `screening_only_predictive`，不能升级为发表级实验验证。若文献选定的是状态方程、电解质模型或其他不能由活度系数曲面直接补参的模型，智能体必须切换到该模型专用的数据与参数回归路线，不能套用 COSMO-RS 活度系数代理路线。

门禁输出会明确区分：

- `publication_ready` / `publication_validated`：可用于正式物性验证结论；
- `screening_ready_predictive` / `screening_only_predictive`：只允许探索性 Aspen 筛选；
- `blocked_thermo_evidence_*`：仍然禁止 Aspen 启动。

## Aspen 阻塞结果

若任一必要二元对连筛选门都不满足，α 结果表会写入以下状态之一，并且不会调用 Aspen：

- `blocked_thermo_evidence_not_provided`
- `blocked_thermo_evidence_incomplete`
- `blocked_thermo_evidence_invalid`

候选级审计文件为：

```text
research_task_package/thermodynamic_evidence_candidate_audit.json
```

其中会列出通过数量、缺失二元对、已有来源和下一步需要检索或回归的内容。

检索队列会为每个尚未闭合的二元对分别生成有限组成 VLE、模型参数、NIST ThermoML 和 Aspen/DECHEMA/DDB 四类定向检索式。检索结果仍须提取成上述结构化记录并通过单位、方向、温压范围和模型一致性检查，单纯搜索到题录不会解除门禁。
