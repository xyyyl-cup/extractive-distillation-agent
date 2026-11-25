# TAC assumptions

本项目中的 TAC 目前只用于 Aspen 闭环流程的候选方案筛选和 NSGA-II 多目标优化排序，不能直接等同于最终工程报价。正式写入论文前，应再用你最终采用的文献公式、设备年限、折旧方式、公用工程价格和供应商报价统一校正。

## Cost model currently used

代码位置：`tac_model.py`

- Annual operating hours: `8000 h/year`
- Payback period: `3 years`
- Column shell cost: `17640 * D^1.066 * L^0.802`
- Heat exchanger cost: `7296 * A^0.65`
- Heat exchanger area: `A = Q / (U * deltaT)`
- TAC: `total_capital_cost / payback_years + operating_cost`

Current operating cost terms:

- Reboiler utility cost
- Condenser cooling cost
- Feed heater/cooler utility cost
- Fresh solvent makeup cost from solvent losses

## Solvent economics in `solvents.xlsx`

`solvents.xlsx` now contains the columns used by `tac_model.load_solvent_economics()`:

- `molecular_weight_kg_per_kmol`
- `solvent_price_usd_per_kg`
- `price_basis`
- `price_source`
- `price_source_url`
- `price_accessed_date`
- `price_confidence`
- `price_note`

The values are screening assumptions collected on 2026-05-13:

| Aspen ID | Solvent | MW (kg/kmol) | Price (USD/kg) | Basis |
|---|---:|---:|---:|---|
| ETHYL-01 | ETHYLENE-GLYCOL | 62.068 | 0.684 | China FOB ethylene glycol, March 2026, 683.64 USD/MT |
| NMP | N-METHYL-2-PYRROLIDONE | 99.133 | 1.608 | China NMP 10933 CNY/TON, converted with USD/CNY = 6.80 |
| DMSO | DIMETHYL-SULFOXIDE | 78.133 | 2.661 | China DMSO, Q4 2025 / December, 2661 USD/MT |
| SULFOLAN | SULFOLANE | 120.170 | 2.537 | China sulfolane 17250 CNY/TON, converted with USD/CNY = 6.80 |
| PC | PROPYLENE-CARBONATE | 102.089 | 0.737 | China propylene carbonate market reference, 737 USD/MT |
| GBL | GAMMA-BUTYROLACTONE | 86.089 | 1.368 | APAC gamma-butyrolactone market reference, 1368 USD/MT |
| ADPN | ADIPONITRILE | 108.144 | 5.250 | China adiponitrile 35700 CNY/TON, converted with USD/CNY = 6.80 |

## Source links

Molecular weights were cross-checked against PubChem pages for the corresponding compounds:

- Ethylene glycol: https://pubchem.ncbi.nlm.nih.gov/compound/Ethylene-Glycol
- NMP: https://pubchem.ncbi.nlm.nih.gov/compound/1-Methyl-2-pyrrolidinone
- DMSO: https://pubchem.ncbi.nlm.nih.gov/compound/Dimethyl-Sulfoxide
- Sulfolane: https://pubchem.ncbi.nlm.nih.gov/compound/Sulfolane
- Propylene carbonate: https://pubchem.ncbi.nlm.nih.gov/compound/Propylene-carbonate
- Gamma-butyrolactone: https://pubchem.ncbi.nlm.nih.gov/compound/gamma-Butyrolactone
- Adiponitrile: https://pubchem.ncbi.nlm.nih.gov/compound/Adiponitrile

Price sources:

- Ethylene glycol: https://www.procurementresource.com/resource-center/ethylene-glycol-price-trends
- NMP: https://www.guidechem.com/price/en/872-50-4.html
- DMSO: https://www.imarcgroup.com/dimethyl-sulfoxide-pricing-report
- Sulfolane: https://www.guidechem.com/price/en/126-33-0.html
- Propylene carbonate: https://www.price-watch.ai/propylene-carbonate-prices/
- Gamma-butyrolactone: https://www.chemanalyst.com/Pricing-data/gamma-butyrolactone-2326
- Adiponitrile: https://www.guidechem.com/price/en/111-69-3.html

## How to treat these prices

For optimization, keeping all solvent prices in one table is enough. For the final thesis comparison, the important part is sensitivity:

- If solvent loss is very small, solvent unit price will have little influence on TAC.
- If solvent loss is not negligible, rerun the TOPSIS ranking with low/base/high solvent prices.
- If a candidate solvent wins only because its public price assumption is favorable, mark that result as price-sensitive.
