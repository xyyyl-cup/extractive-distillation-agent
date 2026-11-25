# Verified Aspen Paths

Verified by reading `ED_template.bkp` through Aspen COM on 2026-05-11.

The first-version scripts save raw Aspen values. No unit conversion is applied.

| Result field | Aspen Tree path | Raw unit |
|---|---|---|
| `x_C3F8_top1` | `\Data\Streams\D\Output\MOLEFRAC\MIXED\C3F8` | dimensionless |
| `x_C3F6_top2` | `\Data\Streams\S1\Output\MOLEFRAC\MIXED\C3F6` | dimensionless |
| `Qreb_1` | `\Data\Blocks\B1\Output\REB_DUTY` | `cal/sec` |
| `Qreb_2` | `\Data\Blocks\B2\Output\REB_DUTY` | `cal/sec` |
| `Qcond_1` | `\Data\Blocks\B1\Output\COND_DUTY` | `cal/sec` |
| `Qcond_2` | `\Data\Blocks\B2\Output\COND_DUTY` | `cal/sec` |
| `solvent_loss_top1` | `\Data\Streams\D\Output\MOLEFLOW\MIXED\<solvent_id>` | `kmol/hr` |
| `solvent_loss_top2` | `\Data\Streams\S1\Output\MOLEFLOW\MIXED\<solvent_id>` | `kmol/hr` |
| `recovery_C3F8` numerator | `\Data\Streams\D\Output\MOLEFLOW\MIXED\C3F8` | `kmol/hr` |
| `recovery_C3F8` denominator | `\Data\Streams\F\Output\MOLEFLOW\MIXED\C3F8` | `kmol/hr` |
| `recovery_C3F6` numerator | `\Data\Streams\S1\Output\MOLEFLOW\MIXED\C3F6` | `kmol/hr` |
| `recovery_C3F6` denominator | `\Data\Streams\F\Output\MOLEFLOW\MIXED\C3F6` | `kmol/hr` |

Solvent IDs currently configured:

```text
ETHYL-01
NMP
DMSO
SULFOLAN
PC
GBL
ADPN
```

Useful conversion:

```text
1 cal/sec = 0.004184 kW
```

## Verified Input Paths

Verified by reading `ED_template.bkp` through Aspen COM on 2026-05-11.

| Input variable | Aspen Tree path | Raw unit / note |
|---|---|---|
| Feed total flow | `\Data\Streams\F\Input\TOTAL\MIXED` | `kmol/hr` basis, COM `UnitString` blank |
| Solvent total flow | `\Data\Streams\C1\Input\TOTAL\MIXED` | `kmol/hr` basis, COM `UnitString` blank |
| Solvent component flow | `\Data\Streams\C1\Input\FLOW\MIXED\<solvent_id>` | `kmol/hr` |
| `RR1` | `\Data\Blocks\B1\Input\BASIS_RR` | dimensionless |
| `D1` | `\Data\Blocks\B1\Input\BASIS_D` | `kmol/hr` |
| `RR2` | `\Data\Blocks\B2\Input\BASIS_RR` | dimensionless |
| `D2` | `\Data\Blocks\B2\Input\BASIS_D` | `kmol/hr` |
| `N1` | `\Data\Blocks\B1\Input\NSTAGE` | dimensionless |
| `NF1` feed stream `S2` to B1 | `\Data\Blocks\B1\Input\FEED_STAGE\S2` | dimensionless; current value `52` |
| `NS1` solvent stream `C2` to B1 | `\Data\Blocks\B1\Input\FEED_STAGE\C2` | dimensionless; current value `34` |
| `N2` | `\Data\Blocks\B2\Input\NSTAGE` | dimensionless |
| `NF2` feed stream `W` to B2 | `\Data\Blocks\B2\Input\FEED_STAGE\W` | dimensionless; current value `2` |

## Verified Closed-Loop Paths

Verified against the recycle version of `ED_template.bkp` on 2026-05-11.

Current closed-loop topology:

```text
C1 fresh makeup + S3 recycle -> B3 -> C2 -> B1
B2 bottoms S3 recycles to B3
```

Calculator `C-1` implements:

```fortran
FLOW=target-MOLARCO
```

where `MOLARCO` is recycle solvent flow in `S3`, and `FLOW` is fresh makeup
solvent flow in `C1`.

| Variable | Aspen Tree path | Raw unit / note |
|---|---|---|
| Calculator recycle component | `\Data\Flowsheeting Options\Calculator\C-1\Input\FVN_COMPONEN\MOLARCO` | active solvent ID |
| Calculator makeup component | `\Data\Flowsheeting Options\Calculator\C-1\Input\FVN_COMPONEN\FLOW` | active solvent ID |
| Calculator Fortran line | `\Data\Flowsheeting Options\Calculator\C-1\Input\FORTRAN_EXEC\#0` | string |
| Recycle status | `\Data\Convergence\Convergence\$OLVER01\Output\ROWSTAT` | `OK` means converged |
| Recycle iteration count | `\Data\Convergence\Convergence\$OLVER01\Output\TOT_ITER` | dimensionless |
| C2 tear total guess | `\Data\Streams\C2\Input\TOTAL\MIXED` | `kmol/hr` basis, COM `UnitString` blank |
| C2 tear component guess | `\Data\Streams\C2\Input\FLOW\MIXED\<solvent_id>` | `kmol/hr` |
| C1 makeup solvent flow | `\Data\Streams\C1\Output\MOLEFLOW\MIXED\<solvent_id>` | `kmol/hr` |
| S3 recycle solvent flow | `\Data\Streams\S3\Output\MOLEFLOW\MIXED\<solvent_id>` | `kmol/hr` |
| C2 actual solvent feed to B1 | `\Data\Streams\C2\Output\MOLEFLOW\MIXED\<solvent_id>` | `kmol/hr` |

Important operating note:

When switching solvent IDs, reset all configured `C1\Input\FLOW` and
`C2\Input\FLOW` solvent component paths to zero, then set only the active
solvent in `C2\Input\FLOW` to the target solvent flow. Without resetting the
`C2` tear guess, old solvent material can remain in the recycle loop and make
convergence unreliable.

## Verified TAC Paths

Verified through Aspen COM on 2026-05-13. A second check was run at the
`NMPNS000158` operating point to confirm the values change correctly after the
Python runner switches the template to NMP.

| TAC variable | Aspen Tree path | Raw unit | `NMPNS000158` value |
|---|---|---|---:|
| B1 top temperature | `\Data\Blocks\B1\Output\TOP_TEMP` | `C` | `-36.7884027` |
| B1 bottom temperature | `\Data\Blocks\B1\Output\BOTTOM_TEMP` | `C` | `108.475515` |
| B1 top pressure | `\Data\Blocks\B1\Output\PRES1` | `bar` | `1.0` |
| B1 bottom pressure | `\Data\Blocks\B1\Output\PRES2` | `bar` | `1.0` |
| B1 reboiler outlet temperature | `\Data\Blocks\B1\Output\REB_TOUT` | `C` | `108.475515` |
| B1 reboiler outlet pressure | `\Data\Blocks\B1\Output\REB_POUT` | `bar` | `1.0` |
| B1 top vapor flow | `\Data\Blocks\B1\Output\TOP_VFLOW` | `kmol/hr` | `8.983977` |
| B1 bottom vapor flow | `\Data\Blocks\B1\Output\BOT_VFLOW` | `kmol/hr` | `33.1101685` |
| B2 top temperature | `\Data\Blocks\B2\Output\TOP_TEMP` | `C` | `116.942926` |
| B2 bottom temperature | `\Data\Blocks\B2\Output\BOTTOM_TEMP` | `C` | `203.158792` |
| B2 top pressure | `\Data\Blocks\B2\Output\PRES1` | `bar` | `1.0` |
| B2 bottom pressure | `\Data\Blocks\B2\Output\PRES2` | `bar` | `1.0` |
| B2 reboiler outlet temperature | `\Data\Blocks\B2\Output\REB_TOUT` | `C` | `203.158792` |
| B2 reboiler outlet pressure | `\Data\Blocks\B2\Output\REB_POUT` | `bar` | `1.0` |
| B2 top vapor flow | `\Data\Blocks\B2\Output\TOP_VFLOW` | `kmol/hr` | `1.097219` |
| B2 bottom vapor flow | `\Data\Blocks\B2\Output\BOT_VFLOW` | `kmol/hr` | `20.0424007` |
| B4 feed heater/cooler duty | `\Data\Blocks\B4\Output\QCALC` | `cal/sec` | `-21400.5504` |
| C1 fresh NMP makeup | `\Data\Streams\C1\Output\MOLEFLOW\MIXED\NMP` | `kmol/hr` | `0.0811959996` |
| C2 NMP feed to B1 | `\Data\Streams\C2\Output\MOLEFLOW\MIXED\NMP` | `kmol/hr` | `31.886` |
| S3 NMP recycle | `\Data\Streams\S3\Output\MOLEFLOW\MIXED\NMP` | `kmol/hr` | `31.804804` |

Current hydraulic diameter status:

The output paths `\Data\Blocks\<block>\Output\DIAM`,
`\Data\Blocks\<block>\Output\CA_DIAM1`, and
`\Data\Blocks\<block>\Output\TRINP_DIAM` still return `0` in this template.
The usable values are Aspen's indexed hydraulic internal-section inputs after
the run. The automation therefore reads all section diameters and uses the
maximum section diameter for each column.

| Hydraulic variable | Aspen Tree path | Raw unit | `NMPNS000158` value |
|---|---|---|---:|
| B1 section CS-1 diameter | `\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-1` | `meter` | `0.9100532826` |
| B1 section CS-2 diameter | `\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-2` | `meter` | `0.6601705539` |
| B1 section CS-3 diameter | `\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-3` | `meter` | `0.6577273467` |
| B2 section CS-1 diameter | `\Data\Blocks\B2\Input\CA_DIAM\INT-1\CS-1` | `meter` | `0.4348033617` |
| B1 tray spacing, CS-1/CS-2/CS-3 | `\Data\Blocks\B1\Input\CA_TRAY_SPC\INT-1\CS-*` | `meter` | `0.6096` |
| B2 tray spacing, CS-1 | `\Data\Blocks\B2\Input\CA_TRAY_SPC\INT-1\CS-1` | `meter` | `0.6096` |

For B1, the hydraulic sections are updated by the runner as
`2..NS1-1`, `NS1..NF1-1`, and `NF1..N1-1`. For B2, the hydraulic section is
updated as `2..N2-1`.
