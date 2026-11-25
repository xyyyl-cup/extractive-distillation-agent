# Extractive Distillation Agent

Research code and selected supporting records for the Chinese manuscript V4. This private submission snapshot connects task interpretation, solvent screening, molecular analysis, Aspen Plus simulation, and multiobjective optimization.

## Start here

1. Use Windows and a licensed, COM-registered Aspen Plus installation (the supplied records identify V11). Install Python and the dependencies below. The exact Python environment used for the historical calculations is not locked; no fresh end-to-end execution is claimed for this package.
2. Run `python -m pip install -r requirements.txt` from the repository root.
3. Configure credentials locally using the variable names in `.env.example`. The application reads operating-system environment variables; the example file is documentation and is not automatically loaded.
4. Run `cd code/aspen_ed_screening`, then `python full_agent_dashboard.py --host 127.0.0.1 --port 8788`. Open http://127.0.0.1:8788.
5. Read `docs/REPRODUCIBILITY.md` before running scientific calculations. Aspen, Gaussian, GROMACS, Multiwfn and VMD must be installed separately as applicable.

## Repository contents

- `code/aspen_ed_screening`: original core source, local modules, tests, template and solvent library; generated historical task directories are excluded.
- `cases/final_selection`: later selection records matching V4 and the verified EC 3214 records. Original calculation values are preserved.
- `data/historical_optimization`: selected historical figure inputs; historical candidate markers are not independent validation results.
- `molecular`: selected small input, analysis and tabular files. Large trajectories, checkpoint binaries and many original outputs are held in the author's source archive.
- `docs`: version mapping, reproducibility limitations and upload instructions.
- `SOURCE_MANIFEST.csv`: source-to-package mapping and SHA-256 checksums.

## V4 case mapping

| Case | Selected record | V4 TAC (10^3 USD/year) | Recovery (%) |
|---|---|---:|---:|
| C3F8/C3F6–PC | PC_2891_B2_nonideal | 867 | 99.97 |
| Ethanol/water–EC | 3214 | 152 | 99.99 |
| Methanol/THF–GBL | GBL_recovery_99p3_RR2_1p8 | 252 | 99.24 |

The older root README and validation-status table in the source ZIP predate the later selection records and must not be used as the final V4 case index. See `docs/V4_MAPPING.md` for the remaining economic-basis distinction.

## Scope and permissions

Prepared for a private repository. No public software license has been selected; authors must confirm redistribution permissions and a license before public release. Proprietary software, credentials and software licenses are not included. A DOI, public repository address and paper acceptance are not claimed.
