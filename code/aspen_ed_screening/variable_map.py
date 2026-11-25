# -*- coding: utf-8 -*-
"""
Central Aspen Variable Explorer mapping for the C3F8/C3F6 extractive
distillation screening workflow.

Replace the placeholder strings below with exact paths copied from Aspen Plus
Variable Explorer. Do not put Aspen Tree paths in other Python modules.
"""

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

TEMPLATE_BKP = BASE_DIR / "ED_template.bkp"
SOLVENTS_XLSX = BASE_DIR / "solvents.xlsx"
CASES_XLSX = BASE_DIR / "cases.xlsx"
RESULTS_XLSX = BASE_DIR / "results.xlsx"
CASE_WORK_DIR = BASE_DIR / "case_files"


PLACEHOLDER = "待我从 Aspen Variable Explorer 复制"
OPTIONAL_PATH = ""


def is_configured_path(path):
    """Return True only when a path was really supplied by the user."""
    if path is None:
        return False
    text = str(path).strip()
    if not text:
        return False
    upper_text = text.upper()
    return "待我" not in text and upper_text not in ("TODO", "TBD", "NONE", "NULL")


# Aspen COM settings. Change ASPEN_PROG_ID if your Aspen installation uses a
# version-specific ProgID.
ASPEN_PROG_ID = "Apwn.Document"
ASPEN_VISIBLE = False
ASPEN_SUPPRESS_DIALOGS = True
RUN_TIMEOUT_SECONDS = 3600
RUN_POLL_INTERVAL_SECONDS = 2
SAVE_MODIFIED_CASE_FILES = False
TERMINATE_RUNNER_ASPEN_PROCESSES = True


# Component IDs used only for labels and optional recovery paths.
C3F8_COMPONENT_ID = "C3F8"
C3F6_COMPONENT_ID = "C3F6"


# Closed-loop recycle mode.
#
# Current ED_template.bkp uses:
#   C1 = fresh solvent makeup
#   S3 = recovered solvent recycle from B2 bottoms
#   B3 = mixer
#   C2 = actual solvent feed to B1 and recycle tear stream
#
# Python controls target solvent-to-feed ratio by:
#   target_solvent_flow = SF * feed_total_flow
#   Calculator C-1 Fortran: FLOW = target_solvent_flow - MOLARCO
# where MOLARCO is S3 solvent recycle and FLOW is C1 fresh makeup.
CLOSED_LOOP_RECYCLE_MODE = True


# ---------------------------------------------------------------------------
# Input variables to write before each run
# ---------------------------------------------------------------------------

# Open-loop total solvent stream flow. In closed-loop mode C1 is makeup, not
# total solvent feed; keep this path for backward compatibility only.
SOLVENT_TOTAL_FLOW_PATH = r"\Data\Streams\C1\Input\TOTAL\MIXED"

# Feed flow used to convert SF into solvent flow. Either set a Tree path or set
# FEED_TOTAL_FLOW_FIXED to a numeric value in the same flow units as the solvent
# stream variable above.
FEED_TOTAL_FLOW_PATH = r"\Data\Streams\F\Input\TOTAL\MIXED"
FEED_TOTAL_FLOW_FIXED = None

# Keys must exactly match solvents.xlsx/aspen_component_id. Use
# SOLVENT_COMPONENT_FLOW_PATHS for the current template because C1 is specified
# by component mole flows. Keep SOLVENT_COMPOSITION_PATHS empty unless you later
# switch C1 to mole-fraction inputs.
SOLVENT_COMPOSITION_PATHS = {}
SOLVENT_COMPONENT_FLOW_PATHS = {
    "ETHYL-01": r"\Data\Streams\C1\Input\FLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\C1\Input\FLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\C1\Input\FLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\C1\Input\FLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\C1\Input\FLOW\MIXED\PC",
    "GBL": r"\Data\Streams\C1\Input\FLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\C1\Input\FLOW\MIXED\ADPN",
}

# Closed-loop Calculator C-1 paths. These paths were verified against the
# recycle template on 2026-05-11. Python switches both Calculator variables to
# the active solvent component and writes the target flow into FORTRAN_EXEC.
CALCULATOR_RECYCLE_COMPONENT_PATH = (
    r"\Data\Flowsheeting Options\Calculator\C-1\Input\FVN_COMPONEN\MOLARCO"
)
CALCULATOR_MAKEUP_COMPONENT_PATH = (
    r"\Data\Flowsheeting Options\Calculator\C-1\Input\FVN_COMPONEN\FLOW"
)
CALCULATOR_FORTRAN_EXEC_PATH = (
    r"\Data\Flowsheeting Options\Calculator\C-1\Input\FORTRAN_EXEC\#0"
)
CALCULATOR_FORTRAN_TEMPLATE = "      FLOW={target_flow:.10g}-MOLARCO"

# Closed-loop tear stream initial guesses. These are important when switching
# solvent IDs, otherwise old solvent material may remain in the C2 tear guess
# and make recycle convergence unreliable.
RECYCLE_TEAR_TOTAL_FLOW_PATH = r"\Data\Streams\C2\Input\TOTAL\MIXED"
RECYCLE_TEAR_COMPONENT_FLOW_PATHS = {
    "ETHYL-01": r"\Data\Streams\C2\Input\FLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\C2\Input\FLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\C2\Input\FLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\C2\Input\FLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\C2\Input\FLOW\MIXED\PC",
    "GBL": r"\Data\Streams\C2\Input\FLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\C2\Input\FLOW\MIXED\ADPN",
}

# Closed-loop diagnostic output paths. The current results table does not use
# these fields directly, but they are useful for verification and future
# reporting:
#   C1 = fresh makeup, S3 = recycle, C2 = actual solvent feed to B1.
MAKEUP_SOLVENT_FLOW_PATHS = {
    "ETHYL-01": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\PC",
    "GBL": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\C1\Output\MOLEFLOW\MIXED\ADPN",
}
RECYCLE_SOLVENT_FLOW_PATHS = {
    "ETHYL-01": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\PC",
    "GBL": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\S3\Output\MOLEFLOW\MIXED\ADPN",
}
ACTUAL_SOLVENT_FEED_FLOW_PATHS = {
    "ETHYL-01": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\PC",
    "GBL": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\C2\Output\MOLEFLOW\MIXED\ADPN",
}

RR1_PATH = r"\Data\Blocks\B1\Input\BASIS_RR"
D1_PATH = r"\Data\Blocks\B1\Input\BASIS_D"
RR2_PATH = r"\Data\Blocks\B2\Input\BASIS_RR"
D2_PATH = r"\Data\Blocks\B2\Input\BASIS_D"

# Optional first-version variables. Leave blank if the Aspen template keeps
# stage numbers and feed stages fixed.
N1_PATH = r"\Data\Blocks\B1\Input\NSTAGE"
NF1_PATH = r"\Data\Blocks\B1\Input\FEED_STAGE\S2"
NS1_PATH = r"\Data\Blocks\B1\Input\FEED_STAGE\C2"
N2_PATH = r"\Data\Blocks\B2\Input\NSTAGE"
NF2_PATH = r"\Data\Blocks\B2\Input\FEED_STAGE\W"


# ---------------------------------------------------------------------------
# Convergence/status variables
# ---------------------------------------------------------------------------

# Optional. If left blank, a Run2 call without COM exception is treated as a
# completed run. For stricter screening, point this to a status variable or a
# user Calculator/Design Spec result and edit CONVERGENCE_SUCCESS_VALUES.
CONVERGENCE_STATUS_PATH = r"\Data\Convergence\Convergence\$OLVER01\Output\ROWSTAT"
CONVERGENCE_SUCCESS_VALUES = ("OK", "SUCCESS", "CONVERGED", "0", 0, True)


# ---------------------------------------------------------------------------
# Result variables to read after each run
# ---------------------------------------------------------------------------

# Verified against ED_template.bkp through Aspen COM on 2026-05-11.
# Values are read in Aspen's raw display units; no conversion is applied in the
# first-version results table.
QREB1_PATH = r"\Data\Blocks\B1\Output\REB_DUTY"
QREB2_PATH = r"\Data\Blocks\B2\Output\REB_DUTY"
QCOND1_PATH = r"\Data\Blocks\B1\Output\COND_DUTY"
QCOND2_PATH = r"\Data\Blocks\B2\Output\COND_DUTY"

C3F8_TOP1_MOLEFRAC_PATH = r"\Data\Streams\D\Output\MOLEFRAC\MIXED\C3F8"
C3F6_TOP2_MOLEFRAC_PATH = r"\Data\Streams\S1\Output\MOLEFRAC\MIXED\C3F6"

# Recovery can be read directly if you have Aspen Calculator variables, or
# calculated from component-flow numerator/denominator paths.
C3F8_RECOVERY_PATH = OPTIONAL_PATH
C3F8_TOP1_COMPONENT_FLOW_PATH = r"\Data\Streams\D\Output\MOLEFLOW\MIXED\C3F8"
C3F8_FEED_COMPONENT_FLOW_PATH = r"\Data\Streams\F\Output\MOLEFLOW\MIXED\C3F8"

C3F6_RECOVERY_PATH = OPTIONAL_PATH
C3F6_TOP2_COMPONENT_FLOW_PATH = r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\C3F6"
C3F6_FEED_COMPONENT_FLOW_PATH = r"\Data\Streams\F\Output\MOLEFLOW\MIXED\C3F6"

# Solvent loss can be a direct Calculator result, or component-flow paths keyed
# by the active solvent component ID.
SOLVENT_LOSS_TOP1_PATH = OPTIONAL_PATH
SOLVENT_LOSS_TOP2_PATH = OPTIONAL_PATH
SOLVENT_LOSS_TOP1_PATHS = {
    "ETHYL-01": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\PC",
    "GBL": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\D\Output\MOLEFLOW\MIXED\ADPN",
}
SOLVENT_LOSS_TOP2_PATHS = {
    "ETHYL-01": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\ETHYL-01",
    "NMP": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\NMP",
    "DMSO": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\DMSO",
    "SULFOLAN": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\SULFOLAN",
    "PC": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\PC",
    "GBL": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\GBL",
    "ADPN": r"\Data\Streams\S1\Output\MOLEFLOW\MIXED\ADPN",
}


# Aspen COM UnitString values verified for the paths above. These are metadata
# for interpretation/conversion; the script writes raw Aspen values unless a
# later version explicitly enables unit conversion.
RESULT_RAW_UNITS = {
    "x_C3F8_top1": "",
    "x_C3F6_top2": "",
    "solvent_loss_top1": "kmol/hr",
    "solvent_loss_top2": "kmol/hr",
    "Qreb_1": "cal/sec",
    "Qreb_2": "cal/sec",
    "Qcond_1": "cal/sec",
    "Qcond_2": "cal/sec",
    "total_reboiler_duty": "cal/sec",
    "B1_diameter_m": "meter",
    "B2_diameter_m": "meter",
    "B1_height_m": "meter",
    "B2_height_m": "meter",
    "B1_top_temp": "C",
    "B1_bottom_temp": "C",
    "B1_reboiler_outlet_temp": "C",
    "B2_top_temp": "C",
    "B2_bottom_temp": "C",
    "B2_reboiler_outlet_temp": "C",
    "feed_heater_duty": "cal/sec",
    "total_capital_cost": "USD",
    "annualized_capital_cost": "USD/year",
    "operating_cost": "USD/year",
    "energy_cost": "USD/year",
    "solvent_makeup_cost": "USD/year",
    "solvent_makeup_kg_per_year": "kg/year",
    "TAC": "USD/year",
    "score": "cal/sec",
}

# Common conversion factor, kept here for traceability. Not applied by default.
CAL_PER_SEC_TO_KW = 4.184e-3


# Input-path units verified against ED_template.bkp through Aspen COM on
# 2026-05-11. TOTAL paths return a blank UnitString in COM, but the matching
# stream component-flow basis is kmol/hr in this template.
INPUT_RAW_UNITS = {
    "FEED_TOTAL_FLOW_PATH": "kmol/hr",
    "SOLVENT_TOTAL_FLOW_PATH": "kmol/hr",
    "SOLVENT_COMPONENT_FLOW_PATHS": "kmol/hr",
    "RECYCLE_TEAR_TOTAL_FLOW_PATH": "kmol/hr",
    "RECYCLE_TEAR_COMPONENT_FLOW_PATHS": "kmol/hr",
    "MAKEUP_SOLVENT_FLOW_PATHS": "kmol/hr",
    "RECYCLE_SOLVENT_FLOW_PATHS": "kmol/hr",
    "ACTUAL_SOLVENT_FEED_FLOW_PATHS": "kmol/hr",
    "RR1_PATH": "",
    "D1_PATH": "kmol/hr",
    "RR2_PATH": "",
    "D2_PATH": "kmol/hr",
    "N1_PATH": "",
    "NF1_PATH": "",
    "NS1_PATH": "",
    "N2_PATH": "",
    "NF2_PATH": "",
}


# ---------------------------------------------------------------------------
# TAC-related Aspen outputs
# ---------------------------------------------------------------------------

# These paths were verified through Aspen COM on 2026-05-13 using the closed-loop
# template and the NMPNS000158 operating point. The values are raw Aspen display
# values and should be converted inside the TAC model.
TAC_COLUMN_TOP_TEMP_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\TOP_TEMP",
    "B2": r"\Data\Blocks\B2\Output\TOP_TEMP",
}
TAC_COLUMN_BOTTOM_TEMP_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\BOTTOM_TEMP",
    "B2": r"\Data\Blocks\B2\Output\BOTTOM_TEMP",
}
TAC_COLUMN_TOP_PRESSURE_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\PRES1",
    "B2": r"\Data\Blocks\B2\Output\PRES1",
}
TAC_COLUMN_BOTTOM_PRESSURE_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\PRES2",
    "B2": r"\Data\Blocks\B2\Output\PRES2",
}
TAC_REBOILER_OUTLET_TEMP_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\REB_TOUT",
    "B2": r"\Data\Blocks\B2\Output\REB_TOUT",
}
TAC_REBOILER_OUTLET_PRESSURE_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\REB_POUT",
    "B2": r"\Data\Blocks\B2\Output\REB_POUT",
}

# Aspen hydraulic/tray-sizing controls. These are written on per-case copies of
# the template only. The hydraulic section stage ranges are updated from
# N1/NF1/NS1/N2 so that column sizing follows the structural variables.
ENABLE_COLUMN_HYDRAULICS = True
HYDRAULIC_SWITCH_PATHS = {
    "B1": r"\Data\Blocks\B1\Input\HYDRAUL",
    "B2": r"\Data\Blocks\B2\Input\HYDRAUL",
}
HYDRAULIC_REPORT_PATHS = {
    "B1": r"\Data\Blocks\B1\Input\HYDRAULIC",
    "B2": r"\Data\Blocks\B2\Input\HYDRAULIC",
}
HYDRAULIC_SWITCH_ON_VALUE = "YES"
HYDRAULIC_REPORT_ON_VALUE = "HYDRAULIC"

HYDRAULIC_SECTION_STAGE_PATHS = {
    "B1": [
        {
            "section": "CS-1",
            "stage1": r"\Data\Blocks\B1\Input\CA_STAGE1\INT-1\CS-1",
            "stage2": r"\Data\Blocks\B1\Input\CA_STAGE2\INT-1\CS-1",
        },
        {
            "section": "CS-2",
            "stage1": r"\Data\Blocks\B1\Input\CA_STAGE1\INT-1\CS-2",
            "stage2": r"\Data\Blocks\B1\Input\CA_STAGE2\INT-1\CS-2",
        },
        {
            "section": "CS-3",
            "stage1": r"\Data\Blocks\B1\Input\CA_STAGE1\INT-1\CS-3",
            "stage2": r"\Data\Blocks\B1\Input\CA_STAGE2\INT-1\CS-3",
        },
    ],
    "B2": [
        {
            "section": "CS-1",
            "stage1": r"\Data\Blocks\B2\Input\CA_STAGE1\INT-1\CS-1",
            "stage2": r"\Data\Blocks\B2\Input\CA_STAGE2\INT-1\CS-1",
        },
    ],
}

# The output DIAM/CA_DIAM nodes still return 0 in this template. The usable
# diameters are Aspen's indexed hydraulic internal-section inputs after the run;
# for equipment cost, use the maximum section diameter for each column.
TAC_COLUMN_DIAMETER_SECTION_PATHS = {
    "B1": [
        r"\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-1",
        r"\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-2",
        r"\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-3",
    ],
    "B2": [
        r"\Data\Blocks\B2\Input\CA_DIAM\INT-1\CS-1",
    ],
}
TAC_COLUMN_TRAY_SPACING_SECTION_PATHS = {
    "B1": [
        r"\Data\Blocks\B1\Input\CA_TRAY_SPC\INT-1\CS-1",
        r"\Data\Blocks\B1\Input\CA_TRAY_SPC\INT-1\CS-2",
        r"\Data\Blocks\B1\Input\CA_TRAY_SPC\INT-1\CS-3",
    ],
    "B2": [
        r"\Data\Blocks\B2\Input\CA_TRAY_SPC\INT-1\CS-1",
    ],
}
TAC_COLUMN_HEIGHT_ALLOWANCE_M = 0.0

# Legacy single-path map retained for simple probes. Use
# TAC_COLUMN_DIAMETER_SECTION_PATHS for TAC calculations.
TAC_COLUMN_DIAMETER_PATHS = {
    "B1": r"\Data\Blocks\B1\Input\CA_DIAM\INT-1\CS-1",
    "B2": r"\Data\Blocks\B2\Input\CA_DIAM\INT-1\CS-1",
}
TAC_COLUMN_DIAMETER_CANDIDATE_PATHS = {
    "B1": [
        r"\Data\Blocks\B1\Output\DIAM",
        r"\Data\Blocks\B1\Output\CA_DIAM1",
        r"\Data\Blocks\B1\Output\TRINP_DIAM",
    ],
    "B2": [
        r"\Data\Blocks\B2\Output\DIAM",
        r"\Data\Blocks\B2\Output\CA_DIAM1",
        r"\Data\Blocks\B2\Output\TRINP_DIAM",
    ],
}

# Vapor-flow paths can be used only for a separate Python diameter estimate if
# tray-sizing diameters remain unavailable.
TAC_COLUMN_TOP_VAPOR_FLOW_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\TOP_VFLOW",
    "B2": r"\Data\Blocks\B2\Output\TOP_VFLOW",
}
TAC_COLUMN_BOTTOM_VAPOR_FLOW_PATHS = {
    "B1": r"\Data\Blocks\B1\Output\BOT_VFLOW",
    "B2": r"\Data\Blocks\B2\Output\BOT_VFLOW",
}

# Feed pre-cooler/pre-heater B4 duty. Negative means cooling in the current
# template.
TAC_FEED_HEATER_DUTY_PATH = r"\Data\Blocks\B4\Output\QCALC"

TAC_RAW_UNITS = {
    "TAC_COLUMN_TOP_TEMP_PATHS": "C",
    "TAC_COLUMN_BOTTOM_TEMP_PATHS": "C",
    "TAC_COLUMN_TOP_PRESSURE_PATHS": "bar",
    "TAC_COLUMN_BOTTOM_PRESSURE_PATHS": "bar",
    "TAC_REBOILER_OUTLET_TEMP_PATHS": "C",
    "TAC_REBOILER_OUTLET_PRESSURE_PATHS": "bar",
    "TAC_COLUMN_TOP_VAPOR_FLOW_PATHS": "kmol/hr",
    "TAC_COLUMN_BOTTOM_VAPOR_FLOW_PATHS": "kmol/hr",
    "TAC_COLUMN_DIAMETER_SECTION_PATHS": "meter",
    "TAC_COLUMN_TRAY_SPACING_SECTION_PATHS": "meter",
    "TAC_COLUMN_HEIGHT_ALLOWANCE_M": "meter",
    "TAC_FEED_HEATER_DUTY_PATH": "cal/sec",
    "MAKEUP_SOLVENT_FLOW_PATHS": "kmol/hr",
}


# ---------------------------------------------------------------------------
# Pass/fail constraints and scoring
# ---------------------------------------------------------------------------

C3F8_PURITY_TARGET = 0.999
C3F6_PURITY_TARGET = None
C3F8_RECOVERY_TARGET = None
C3F6_RECOVERY_TARGET = None
MAX_SOLVENT_LOSS = None


SOLVENT_COLUMNS = [
    "solvent_name",
    "aspen_component_id",
    "SF_min",
    "SF_max",
    "note",
]

CASE_COLUMNS = [
    "case_id",
    "solvent_name",
    "aspen_component_id",
    "SF",
    "RR1",
    "D1",
    "RR2",
    "D2",
    "N1",
    "NF1",
    "NS1",
    "N2",
    "NF2",
]

RESULT_COLUMNS = [
    "case_id",
    "solvent_name",
    "converged",
    "error_message",
    "x_C3F8_top1",
    "recovery_C3F8",
    "x_C3F6_top2",
    "recovery_C3F6",
    "solvent_loss_top1",
    "solvent_loss_top2",
    "Qreb_1",
    "Qreb_2",
    "Qcond_1",
    "Qcond_2",
    "total_reboiler_duty",
    "B1_diameter_m",
    "B2_diameter_m",
    "B1_height_m",
    "B2_height_m",
    "B1_top_temp",
    "B1_bottom_temp",
    "B1_reboiler_outlet_temp",
    "B2_top_temp",
    "B2_bottom_temp",
    "B2_reboiler_outlet_temp",
    "feed_heater_duty",
    "total_capital_cost",
    "annualized_capital_cost",
    "operating_cost",
    "energy_cost",
    "solvent_makeup_cost",
    "solvent_makeup_kg_per_year",
    "TAC",
    "tac_error",
    "pass_or_fail",
    "score",
]
