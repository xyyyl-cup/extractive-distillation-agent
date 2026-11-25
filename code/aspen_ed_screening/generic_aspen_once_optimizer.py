# -*- coding: utf-8 -*-
"""
Generic one-pass Aspen optimizer for Agent-generated binary-system specs.

This runner is intentionally conservative: it does not reuse the C3F8/C3F6
variable_map result paths. It mutates the zero-build spec, builds a fresh BKP,
runs Aspen Run2 through zero_build_aspen_agent's generic case validator, and
writes checkpoint rows that the dashboard can import as process evidence.
"""

import argparse
import copy
import datetime as dt
import json
import math
import multiprocessing as mp
import os
import queue as queue_mod
import random
import re
import subprocess
import time
import traceback
from pathlib import Path

import tac_model
import zero_build_aspen_agent as zero


VARIABLES = ("SF", "RR1", "D1", "RR2", "D2", "N1", "NF1", "NS1", "N2", "NF2")
CONTINUOUS_VARIABLES = ("SF", "RR1", "D1", "RR2", "D2")
INTEGER_VARIABLES = ("N1", "NF1", "NS1", "N2", "NF2")
SEARCH_MODES = ("adaptive_refinement", "nsga2_hybrid")
APPLIED_STAGE_FIELDS = ("N1", "NF1", "NS1", "N2", "NF2")
ASPEN_B1_NSTAGE_BOUNDS = (6, 180)
ASPEN_B2_NSTAGE_BOUNDS = (4, 100)
ASPEN_STAGE_APPLICATION_POLICY = "aspen_stage_limits_B1_6_180_B2_4_100"


def repair_invalid_json_escapes(text):
    """Repair odd backslash runs before non-JSON escape characters."""
    chars = []
    i = 0
    valid_escape = set('"\\/bfnrt')
    hex_digits = set("0123456789abcdefABCDEF")
    while i < len(text):
        if text[i] != "\\":
            chars.append(text[i])
            i += 1
            continue
        j = i
        while j < len(text) and text[j] == "\\":
            j += 1
        run = j - i
        next_char = text[j] if j < len(text) else ""
        valid_next = next_char in valid_escape
        if not valid_next and next_char == "u":
            valid_next = j + 4 < len(text) and all(ch in hex_digits for ch in text[j + 1:j + 5])
        if run % 2 == 1 and next_char and not valid_next:
            run += 1
        chars.append("\\" * run)
        i = j
    return "".join(chars)


def safe_json_dumps(payload, **kwargs):
    text = json.dumps(payload, **kwargs)
    text = repair_invalid_json_escapes(text)
    try:
        json.loads(text)
        return text
    except Exception:
        fixed = repair_invalid_json_escapes(text)
        try:
            json.loads(fixed)
            return fixed
        except Exception:
            return text


def safe_json_loads(text, default=None):
    try:
        return json.loads(text)
    except Exception:
        try:
            return json.loads(repair_invalid_json_escapes(text))
        except Exception:
            return default


CASE_RUNTIME_RESULT_KEYS = (
    "validation_mode",
    "fast_inplace",
    "converged",
    "pass_or_fail",
    "aspen_run_pass_or_fail",
    "aspen_status",
    "run_status",
    "error_message",
    "run_seconds",
    "run_method",
    "convergence_status",
    "Qreb_1",
    "Qreb_2",
    "Qreb_1_path",
    "Qreb_2_path",
    "total_reboiler_duty",
    "Qcond_1",
    "Qcond_2",
    "Qcond_1_path",
    "Qcond_2_path",
    "target_product",
    "target_component_id",
    "target_product_stream",
    "target_purity_target",
    "target_purity_basis",
    "target_purity_value",
    "target_purity_mole_frac",
    "target_purity_mole_frac_path",
    "target_purity_mass_frac",
    "target_purity_mass_frac_path",
    "target_purity_path",
    "target_recovery",
    "target_recovery_path",
    "target_recovery_loss",
    "purity_pass",
    "target_product_component_flow_kmol_hr",
    "target_product_component_flow_path",
    "target_feed_component_flow_kmol_hr",
    "target_feed_component_flow_path",
    "target_product_total_flow_kmol_hr",
    "target_product_total_flow_path",
    "target_metric_archive",
    "aspen_report_path",
    "aspen_report_component_metric_status",
    "solvent_loss_top1",
    "solvent_loss_top2",
    "solvent_loss_total",
    "solvent_loss_top1_path",
    "solvent_loss_top2_path",
    "required_outputs_complete",
    "duty_signal_available",
    "stream_signal_available",
    "duty_probe_values",
    "duty_probe_paths",
    "stream_probe_values",
    "stream_probe_paths",
    "validated_bkp",
    "archive_write_warning",
    "aspen_convergence_failure_signals",
    "from_cache",
    "feasible",
    "constraint_violation",
    "TAC",
    "tac_error",
    "total_capital_cost",
    "annualized_capital_cost",
    "operating_cost",
    "energy_cost",
    "B1_diameter_m",
    "B1_height_m",
    "B1_top_temp",
    "B1_bottom_temp",
    "B1_reboiler_outlet_temp",
    "B2_diameter_m",
    "B2_height_m",
    "B2_top_temp",
    "B2_bottom_temp",
    "B2_reboiler_outlet_temp",
    "feed_heater_duty",
    "tac_input_basis",
    "B1_column_shell_cost",
    "B2_column_shell_cost",
    "column_capital_cost",
    "B1_reboiler_area_m2",
    "B1_reboiler_capital_cost",
    "B1_reboiler_utility",
    "B1_reboiler_utility_cost_usd_per_gj",
    "B2_reboiler_area_m2",
    "B2_reboiler_capital_cost",
    "B2_reboiler_utility",
    "B2_reboiler_utility_cost_usd_per_gj",
    "B1_condenser_area_m2",
    "B1_condenser_capital_cost",
    "B1_condenser_utility",
    "B1_condenser_utility_cost_usd_per_gj",
    "B2_condenser_area_m2",
    "B2_condenser_capital_cost",
    "B2_condenser_utility",
    "B2_condenser_utility_cost_usd_per_gj",
    "heat_exchanger_area_m2",
    "heat_exchanger_capital_cost",
    "reboiler_energy_cost",
    "condenser_energy_cost",
    "feed_exchanger_energy_cost",
    "total_reboiler_duty_kW",
    "total_condenser_duty_kW",
    "solvent_molecular_weight_kg_per_kmol",
    "tac_recomputed_from_current_spec",
    "reboiler_steam_duty_gj_per_year",
    "carbon_emission_kg_per_year",
    "carbon_emission_ton_per_year",
    "carbon_emission_basis",
    "solvent_makeup_cost",
    "solvent_makeup_kg_per_year",
    "decision_score",
    "spec",
    "input_deck",
    "generated_bkp",
    "objective_energy",
    "objective_solvent_circulation",
    "objective_column_stages",
    "objective_makeup",
    "objective_carbon_emission",
    "objective_recovery_loss",
    "objective_TAC",
    "objective_energy_cost",
    "objective_solvent_loss",
    "multi_objective_feasible",
    "formal_multi_objective_pass",
    "trusted_as_optimization_pass",
    "final_closed_loop_pass",
    "diagnostic_only_pass",
    "pareto_front",
    "pareto_rank",
    "topsis_score",
    "topsis_rank",
    "timed_out",
    "timeout_seconds",
    "aspen_crash_dump",
    "dump_files",
    "fast_session_archive",
    "fast_template_bkp",
    "fast_session_id",
    "case_archive_policy",
)
SEARCH_PARENT_RECYCLE_CONTROL_MODES = (
    "calculator_inventory_control",
    "bounded_nonnegative_makeup_control",
    "fixed_fresh_makeup_diagnostic",
)
MULTI_OBJECTIVE_NAMES = (
    "objective_carbon_emission",
    "objective_TAC",
    "objective_recovery_loss",
)
MULTI_OBJECTIVE_WEIGHTS = {
    "objective_carbon_emission": 1.0 / 3.0,
    "objective_TAC": 1.0 / 3.0,
    "objective_recovery_loss": 1.0 / 3.0,
}
DEFAULT_TARGET_PURITY = 0.999
REQUIRED_OUTPUT_MISSING_STOP_STREAK = 5
ASPEN_MAX_PARALLEL_PROCESSES = int(os.environ.get("ED_AGENT_ASPEN_MAX_PARALLEL_PROCESSES", "1"))
BOUNDS_PROFILES = {
    "exploratory": {
        "SF": (0.2, 5.0),
        "RR1": (0.25, 10.0),
        "RR2": (0.10, 6.0),
        "D1": (0.65, 1.08),
        "D2": (0.50, 2.30),
        "N1": (6, 120),
        "N2": (4, 80),
        "NF1_BOTTOM_MARGIN": 1,
        "NF1_NS1_MIN_GAP": 1,
        "DISTILLATE_BOUNDS_MODE": "split_factor",
    },
    "publication_safe": {
        "SF": (0.25, 3.5),
        "RR1": (0.50, 8.0),
        "RR2": (0.20, 5.0),
        "D1": (0.80, 1.04),
        "D2": (0.65, 1.85),
        "N1": (20, 110),
        "N2": (4, 60),
        "NF1_BOTTOM_MARGIN": 2,
        "NF1_NS1_MIN_GAP": 3,
        "DISTILLATE_BOUNDS_MODE": "split_factor",
    },
    "purity_feasibility": {
        "SF": (0.15, 4.5),
        "RR1": (0.20, 12.0),
        "RR2": (0.10, 8.0),
        "D1": (0.70, 1.06),
        "D2": (0.50, 2.50),
        "N1": (12, 130),
        "N2": (4, 70),
        "NF1_BOTTOM_MARGIN": 1,
        "NF1_NS1_MIN_GAP": 1,
        "DISTILLATE_BOUNDS_MODE": "split_factor",
    },
    "aggressive_feasibility": {
        "SF": (0.10, 6.0),
        "RR1": (0.15, 18.0),
        "RR2": (0.08, 10.0),
        "D1": (0.65, 1.10),
        "D2": (0.40, 3.00),
        "N1": (10, 180),
        "N2": (4, 100),
        "NF1_BOTTOM_MARGIN": 1,
        "NF1_NS1_MIN_GAP": 1,
        "DISTILLATE_BOUNDS_MODE": "split_factor",
    },
}
BOUNDARY_TOLERANCE_FRACTION = 0.01

VOLATILITY_RANK = {
    "DMSO": 0,
    "SULFOLAN": 0,
    "GLYCEROL": 0,
    "ETHYL-01": 1,
    "ETHYLENEGLYCOL": 1,
    "WATER": 2,
    "METHANOL": 3,
    "CO2": 4,
    "METHANE": 5,
    "CH4": 5,
}


def feed_total(spec):
    stream = (spec.get("streams") or {}).get("F") or {}
    return sum(float(value or 0.0) for value in (stream.get("mole_flow") or {}).values()) or 10.0


def safe_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return default
        return parsed
    except Exception:
        return default


def safe_stage_int(value):
    parsed = safe_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def case_spec_stage_values(case_spec):
    case_spec = case_spec or {}
    validation_case = case_spec.get("validation_case") or {}
    values = {}
    for field in APPLIED_STAGE_FIELDS:
        value = safe_stage_int(validation_case.get("applied_{}".format(field)))
        if value is not None:
            values[field] = value
    radfrac = case_spec.get("radfrac") or {}
    b1 = radfrac.get("B1") or {}
    b2 = radfrac.get("B2") or {}
    b1_feeds = b1.get("feeds") or {}
    b2_feeds = b2.get("feeds") or {}
    direct_values = {
        "N1": b1.get("nstage"),
        "NS1": b1_feeds.get("C2", b1_feeds.get("C1")),
        "NF1": b1_feeds.get("S2"),
        "N2": b2.get("nstage"),
        "NF2": b2_feeds.get("W"),
    }
    for field, raw_value in direct_values.items():
        value = safe_stage_int(raw_value)
        if value is not None:
            values[field] = value
    return values


def annotate_applied_stage_values(row, case_spec):
    row = dict(row or {})
    values = case_spec_stage_values(case_spec)
    if not values:
        return row
    validation_case = (case_spec or {}).get("validation_case") or {}
    for field in APPLIED_STAGE_FIELDS:
        if field not in values:
            continue
        requested_key = "requested_{}".format(field)
        applied_key = "applied_{}".format(field)
        requested_value = row.get(requested_key)
        if requested_value in (None, ""):
            requested_value = validation_case.get(requested_key)
        if requested_value in (None, ""):
            requested_value = row.get(field)
        if requested_value not in (None, ""):
            row[requested_key] = requested_value
        row[field] = values[field]
        row[applied_key] = values[field]
    row["stage_application_policy"] = ASPEN_STAGE_APPLICATION_POLICY
    return row


def restore_row_applied_stage_values_from_spec(row):
    row = dict(row or {})
    spec_path = str(row.get("spec") or "").strip()
    if not spec_path:
        return row
    path = Path(spec_path)
    if not path.exists():
        return row
    try:
        case_spec = safe_json_loads(path.read_text(encoding="utf-8", errors="replace"), default={})
    except Exception as exc:
        row["resume_applied_stage_restore_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
        return row
    before = {field: row.get(field) for field in APPLIED_STAGE_FIELDS}
    restored = annotate_applied_stage_values(row, case_spec)
    after = {field: restored.get(field) for field in APPLIED_STAGE_FIELDS}
    if before != after:
        restored["resume_applied_stage_values_restored"] = True
        restored["resume_applied_stage_source"] = str(path.resolve())
    return restored


def ensure_carbon_emission_fields(row):
    row = dict(row or {})
    carbon = safe_float(row.get("carbon_emission_ton_per_year"))
    if carbon is not None:
        return row
    try:
        utility = tac_model.utility_costs(row)
    except Exception:
        return row
    for key in (
        "reboiler_steam_duty_gj_per_year",
        "carbon_emission_kg_per_year",
        "carbon_emission_ton_per_year",
        "carbon_emission_basis",
    ):
        if utility.get(key) not in (None, ""):
            row[key] = utility.get(key)
    return row


def attach_case_objectives(spec, row):
    row = dict(row)
    row = ensure_carbon_emission_fields(row)
    total = feed_total(spec)
    sf = safe_float(row.get("SF"), 3.0)
    n1 = safe_float(row.get("N1"), 0.0) or 0.0
    n2 = safe_float(row.get("N2"), 0.0) or 0.0
    rr1 = safe_float(row.get("RR1"), 0.0) or 0.0
    rr2 = safe_float(row.get("RR2"), 0.0) or 0.0
    makeup = safe_float(row.get("makeup_flow"), 0.0) or 0.0
    if truthy(row.get("converged")):
        try:
            recalculated_tac = tac_model.calculate_tac(row, solvent_economics_for_case(spec, row))
            if recalculated_tac.get("TAC") is not None:
                row.update(recalculated_tac)
                row["tac_recomputed_from_current_spec"] = True
        except Exception as exc:
            row["tac_recompute_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
    tac = safe_float(row.get("TAC"))
    carbon_emission = safe_float(row.get("carbon_emission_ton_per_year"))
    energy_cost = safe_float(row.get("energy_cost"))
    solvent_loss = safe_float(row.get("solvent_loss_total"))
    recovery = safe_float(row.get("target_recovery"))
    if recovery is None:
        for key, value in row.items():
            if str(key).startswith("recovery_"):
                recovery = safe_float(value)
                if recovery is not None:
                    break
    recovery_loss = None if recovery is None else max(0.0, 1.0 - recovery)
    solvent_circulation = max(0.0, sf * total)
    row["feed_total_kmol_hr"] = round(total, 6)
    row["solvent_flow_kmol_hr"] = round(solvent_circulation, 6)
    row["solvent_to_feed_ratio"] = round(sf, 6)
    row["solvent_makeup_kmol_hr"] = round(makeup, 6)
    row["column_stage_total"] = int(round(n1 + n2))
    row["reflux_ratio_sum"] = round(rr1 + rr2, 6)
    if solvent_loss is None:
        solvent_loss = 0.0 if safe_float(row.get("solvent_loss_top1")) is not None or safe_float(row.get("solvent_loss_top2")) is not None else None
        if solvent_loss is not None:
            solvent_loss += safe_float(row.get("solvent_loss_top1"), 0.0) or 0.0
            solvent_loss += safe_float(row.get("solvent_loss_top2"), 0.0) or 0.0
            row["solvent_loss_total"] = solvent_loss
    formal_feasible = (
        bool(row.get("feasible"))
        and row_is_closed_loop_pass(row)
        and safe_float(row.get("constraint_violation"), 1.0) <= 1.0e-12
    )
    row["obj_recovery_loss"] = recovery_loss
    row["obj_tac"] = tac
    row["obj_carbon_emission"] = carbon_emission
    row["obj_energy_cost"] = energy_cost
    row["obj_solvent_loss"] = solvent_loss
    if formal_feasible and None not in (carbon_emission, tac, recovery_loss):
        row["objective_carbon_emission"] = carbon_emission
        row["objective_recovery_loss"] = recovery_loss
        row["objective_TAC"] = tac
        row["objective_energy_cost"] = ""
        row["objective_solvent_loss"] = ""
        row["objective_energy"] = energy_cost
        row["objective_solvent_circulation"] = solvent_circulation
        row["objective_column_stages"] = n1 + n2
        row["objective_makeup"] = makeup
    else:
        row["objective_carbon_emission"] = ""
        row["objective_recovery_loss"] = ""
        row["objective_TAC"] = ""
        row["objective_energy_cost"] = ""
        row["objective_solvent_loss"] = ""
        row["objective_energy"] = ""
        row["objective_solvent_circulation"] = ""
        row["objective_column_stages"] = ""
        row["objective_makeup"] = ""
    return row


def aspen_automation_pids():
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$ErrorActionPreference='SilentlyContinue'; "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -eq 'AspenPlus.exe' -and "
                    "($_.CommandLine -like '*-Automation*' -or $_.CommandLine -like '*-Embedding*') } | "
                    "ForEach-Object { $_.ProcessId }"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
        )
    except Exception:
        return set()
    pids = set()
    for line in (completed.stdout or "").splitlines():
        try:
            pids.add(int(line.strip()))
        except Exception:
            pass
    return pids


def aspen_related_pids():
    """Return live Aspen-related PIDs. Used only for post-case maintenance."""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$ErrorActionPreference='SilentlyContinue'; "
                    "$names=@('AspenPlus.exe','AspenPlusGUI.exe','AspenProperties.exe','APMain.exe','Apwn.exe'); "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $names -contains $_.Name } | "
                    "ForEach-Object { $_.ProcessId }"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
        )
    except Exception:
        return set()
    pids = set()
    for line in (completed.stdout or "").splitlines():
        try:
            pids.add(int(line.strip()))
        except Exception:
            pass
    return pids


def aspen_automation_process_rows():
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$ErrorActionPreference='SilentlyContinue'; "
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -eq 'AspenPlus.exe' -and "
                    "($_.CommandLine -like '*-Automation*' -or $_.CommandLine -like '*-Embedding*') } | "
                    "ForEach-Object { $gp=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
                    "[pscustomobject]@{ProcessId=$_.ProcessId;Name=$_.Name;CommandLine=$_.CommandLine;"
                    "CreationDate=$_.CreationDate;MainWindowTitle=$gp.MainWindowTitle} } | ConvertTo-Json -Compress"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=20,
        )
    except Exception:
        return []
    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    now = time.time()
    for row in data or []:
        try:
            pid = int(row.get("ProcessId") or 0)
        except Exception:
            continue
        if pid <= 0:
            continue
        creation_date = str(row.get("CreationDate") or "")
        age_seconds = None
        match = re.match(r"^(\d{14})", creation_date)
        if match:
            try:
                age_seconds = max(0.0, now - dt.datetime.strptime(match.group(1), "%Y%m%d%H%M%S").timestamp())
            except Exception:
                age_seconds = None
        rows.append({
            "pid": pid,
            "name": str(row.get("Name") or ""),
            "command_line": str(row.get("CommandLine") or ""),
            "creation_date": creation_date,
            "main_window_title": str(row.get("MainWindowTitle") or ""),
            "age_seconds": age_seconds,
        })
    return rows


def cleanup_aspen_automation_over_limit(process_limit=1, min_age_seconds=90, reason="aspen_process_over_limit"):
    process_limit = max(0, int(process_limit or 0))
    rows = aspen_automation_process_rows()
    candidates = []
    for row in rows:
        if str(row.get("main_window_title") or "").strip():
            continue
        age = row.get("age_seconds")
        if age is not None and float(age) < float(min_age_seconds or 0):
            continue
        candidates.append(row)
    over_by = max(0, len(rows) - process_limit)
    to_stop = sorted(candidates, key=lambda row: (row.get("creation_date") or "", int(row.get("pid") or 0)))[:over_by]
    stopped_pids = [int(row.get("pid")) for row in to_stop if row.get("pid")]
    if stopped_pids:
        stop_processes(stopped_pids)
    remaining = aspen_automation_process_rows()
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "event": "aspen_over_limit_safe_cleanup",
        "reason": reason,
        "process_limit": process_limit,
        "min_age_seconds": min_age_seconds,
        "before_automation_count": len(rows),
        "candidate_count": len(candidates),
        "stopped_new_aspen_pids": sorted(stopped_pids),
        "after_automation_count": len(remaining),
        "remaining_new_aspen_pids": sorted(int(row.get("pid")) for row in remaining if row.get("pid")),
    }


def stop_processes(pids):
    pending = sorted(set(int(item) for item in pids if item))
    for pid in pending:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        except Exception:
            pass
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId={}\" "
                        "-ErrorAction SilentlyContinue; "
                        "if($p){{ Invoke-CimMethod -InputObject $p -MethodName Terminate "
                        "-Arguments @{{Reason=0}} | Out-Null }}"
                    ).format(pid),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception:
            pass
    deadline = time.time() + 20
    remaining = set(pending)
    while remaining and time.time() < deadline:
        live = aspen_related_pids()
        remaining &= live
        if not remaining:
            break
        time.sleep(1)
        for pid in sorted(remaining):
            try:
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId={}\" "
                            "-ErrorAction SilentlyContinue; "
                            "if($p){{ Invoke-CimMethod -InputObject $p -MethodName Terminate "
                            "-Arguments @{{Reason=0}} | Out-Null }}"
                        ).format(pid),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
            except Exception:
                pass


def cleanup_new_aspen_processes(baseline_pids, settle_seconds=6):
    baseline_pids = set(int(item) for item in (baseline_pids or set()) if item)
    deadline = time.time() + max(1, int(settle_seconds or 1))
    quiet_checks = 0
    while time.time() < deadline and quiet_checks < 2:
        new_pids = aspen_automation_pids() - baseline_pids
        if new_pids:
            quiet_checks = 0
            stop_processes(new_pids)
        else:
            quiet_checks += 1
        time.sleep(0.8)


def cleanup_new_aspen_related_processes(baseline_pids, settle_seconds=12):
    baseline_pids = set(int(item) for item in (baseline_pids or set()) if item)
    deadline = time.time() + max(1, int(settle_seconds or 1))
    quiet_checks = 0
    stopped = set()
    while time.time() < deadline and quiet_checks < 2:
        new_pids = aspen_related_pids() - baseline_pids
        if new_pids:
            quiet_checks = 0
            stopped.update(new_pids)
            stop_processes(new_pids)
        else:
            quiet_checks += 1
        time.sleep(0.8)
    remaining = aspen_related_pids() - baseline_pids
    remaining_primary = aspen_automation_pids() - baseline_pids
    total_after_cleanup = aspen_related_pids()
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "baseline_aspen_pid_count": len(baseline_pids),
        "total_aspen_pid_count_after_cleanup": len(total_after_cleanup),
        "total_aspen_pids_after_cleanup": sorted(total_after_cleanup),
        "stopped_new_aspen_pids": sorted(stopped),
        "remaining_new_aspen_pids": sorted(remaining),
        "remaining_new_aspen_primary_pids": sorted(remaining_primary),
        "remaining_new_aspen_primary_count": len(remaining_primary),
        "settle_seconds": settle_seconds,
    }


def cleanup_event_has_primary_aspen_residue(event):
    event = event or {}
    try:
        if int(event.get("remaining_new_aspen_primary_count") or 0) > 0:
            return True
    except Exception:
        pass
    return bool(event.get("remaining_new_aspen_primary_pids"))


def annotate_support_only_aspen_residue(event):
    if not event:
        return event
    if event.get("remaining_new_aspen_pids") and not cleanup_event_has_primary_aspen_residue(event):
        event["support_only_residue_warning"] = True
        event["cleanup_policy"] = "continue_without_primary_aspen_residue"
    return event


def aspen_dump_files(case_dir):
    case_dir = Path(case_dir)
    if not case_dir.exists():
        return []
    return sorted(case_dir.glob("aspenplus_ExceptionDump_*.dmp"))


def mark_aspen_crash(row, dump_files):
    dump_files = [str(Path(path).resolve()) for path in dump_files]
    row = dict(row)
    row["aspen_crash_dump"] = bool(dump_files)
    row["dump_files"] = ";".join(dump_files)
    row["converged"] = False
    row["feasible"] = False
    row["pass_or_fail"] = "FAIL"
    row["constraint_violation"] = 1
    message = "Aspen Plus generated a fatal-error dump during this case."
    if row.get("error_message"):
        message = "{} {}".format(message, row.get("error_message"))
    row["error_message"] = message
    return row


def solvent_id(spec):
    calc = spec.get("calculator") or {}
    return calc.get("solvent_component") or ((spec.get("validation_case") or {}).get("aspen_component_id") or "SOLVENT")


def topology_metadata(spec):
    blocks = {str(block.get("id") or ""): block for block in spec.get("blocks", []) if isinstance(block, dict)}
    calc = spec.get("calculator") or {}
    mode = str(spec.get("process_topology_mode") or "").strip()
    b3 = blocks.get("B3") or {}
    b3_inputs = {str(item).upper() for item in (b3.get("in") or [])}
    b3_outputs = {str(item).upper() for item in (b3.get("out") or [])}
    has_recycle_mixer = "B3" in blocks and "S3" in b3_inputs and "C2" in b3_outputs
    closed_loop = bool(
        "closed" in mode.lower()
        or (
            has_recycle_mixer
            and (not calc or str(calc.get("recycle_stream") or "").upper() == "S3")
        )
    )
    if not mode:
        mode = "closed_loop_solvent_recycle" if closed_loop else "open_loop_or_unclassified"
    recycle_control_mode = str(spec.get("recycle_control_mode") or "").strip()
    if not recycle_control_mode:
        if calc:
            recycle_control_mode = "bounded_nonnegative_makeup_control" if calc.get("formula_mode") == "bounded_nonnegative_makeup" else "calculator_inventory_control"
        elif "fixed_makeup" in mode.lower():
            recycle_control_mode = "fixed_fresh_makeup_diagnostic"
    return {
        "process_topology_mode": mode,
        "recycle_control_mode": recycle_control_mode,
        "closed_loop_recycle": closed_loop,
        "has_recycle_mixer": has_recycle_mixer,
        "has_recycle_calculator": bool(calc),
        "recycle_stream": calc.get("recycle_stream") or ("S3" if has_recycle_mixer else ""),
        "makeup_stream": calc.get("makeup_stream") or ("C1" if has_recycle_mixer else ""),
    }


def feed_mole_flows(spec):
    return dict(((spec.get("streams") or {}).get("F") or {}).get("mole_flow") or {})


def normalized_token(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def unique_tokens(values):
    tokens = []
    seen = set()
    for value in values or []:
        token = str(value or "").strip()
        if not token:
            continue
        key = token.upper()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def feed_component_aliases(spec):
    aliases = {}
    for row in spec.get("feed_component_mapping") or []:
        cid = row.get("aspen_component_id")
        if not cid:
            continue
        for key in (row.get("code"), row.get("name"), cid):
            token = normalized_token(key)
            if token:
                aliases[token] = cid
    return aliases


def target_component_id(spec):
    flows = feed_mole_flows(spec)
    validation_case = spec.get("validation_case") or {}
    for explicit in (
        spec.get("target_aspen_component_id"),
        validation_case.get("target_aspen_component_id"),
        spec.get("target_component_id"),
        validation_case.get("target_component_id"),
    ):
        if explicit and explicit in flows:
            return explicit
        explicit_token = normalized_token(explicit)
        if explicit_token:
            for key in flows:
                if normalized_token(key) == explicit_token:
                    return key
    target = normalized_token(spec.get("target_product") or (spec.get("validation_case") or {}).get("target_product"))
    aliases = feed_component_aliases(spec)
    if target in aliases:
        return aliases[target]
    if target in {normalized_token(key) for key in flows}:
        for key in flows:
            if normalized_token(key) == target:
                return key
    return next(iter(flows), "")


def component_volatility(component_id):
    token = normalized_token(component_id)
    return VOLATILITY_RANK.get(token, 2)


def split_targets(spec):
    flows = feed_mole_flows(spec)
    total = feed_total(spec)
    if not flows:
        return total * 0.5, total * 0.5
    target_id = target_component_id(spec)
    target_flow = float(flows.get(target_id) or 0.0)
    if target_flow <= 0:
        target_flow = total * 0.5
    other_flow = max(total - target_flow, total * 0.05)
    target_is_light = component_volatility(target_id) >= max(
        component_volatility(cid) for cid in flows
    )
    if target_is_light:
        return max(0.05, target_flow * 0.99), max(0.05, other_flow * 0.99)
    return max(0.05, other_flow * 0.99), max(0.05, target_flow * 0.99)


ATOMIC_WEIGHTS = {
    "H": 1.008,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "F": 18.998,
    "P": 30.974,
    "S": 32.06,
    "Cl": 35.45,
    "Br": 79.904,
    "I": 126.904,
}


def target_purity_value(spec):
    for value in (
        spec.get("target_purity"),
        (spec.get("validation_case") or {}).get("target_purity"),
    ):
        parsed = safe_float(value)
        if parsed is not None and 0.0 < parsed <= 1.0:
            return parsed
    return DEFAULT_TARGET_PURITY


def target_purity_basis(spec):
    for value in (
        (spec or {}).get("target_purity_basis"),
        ((spec or {}).get("validation_case") or {}).get("target_purity_basis"),
    ):
        token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if token in {"mass", "mass_frac", "mass_fraction", "weight", "weight_fraction", "wt"}:
            return "mass_fraction"
        if token in {"mole", "molar", "mole_frac", "mole_fraction", "mol"}:
            return "mole_fraction"
    return "mole_fraction"


def row_target_purity_value(row, spec=None):
    row = row or {}
    basis = str(row.get("target_purity_basis") or target_purity_basis(spec or {})).strip().lower()
    if basis == "mass_fraction":
        return safe_float(row.get("target_purity_mass_frac"))
    return safe_float(row.get("target_purity_mole_frac"))


def row_target_purity_path(row, spec=None):
    row = row or {}
    basis = str(row.get("target_purity_basis") or target_purity_basis(spec or {})).strip().lower()
    if basis == "mass_fraction":
        return row.get("target_purity_mass_frac_path") or ""
    return row.get("target_purity_mole_frac_path") or row.get("target_purity_path") or ""


def target_recovery_reference(spec):
    for value in (
        spec.get("target_recovery"),
        (spec.get("validation_case") or {}).get("target_recovery"),
    ):
        parsed = safe_float(value)
        if parsed is not None and 0.0 < parsed <= 1.0:
            return parsed
    return None


def target_split_for_recovery(spec):
    recovery = target_recovery_reference(spec)
    if recovery is None:
        stream = target_product_stream(spec)
        d1, d2 = split_targets(spec)
        return d1 if str(stream or "").upper() == "D" else d2
    target_feed = safe_float(feed_mole_flows(spec).get(target_component_id(spec)))
    purity = target_purity_value(spec) if target_purity_basis(spec) == "mole_fraction" else 1.0
    if target_feed is None or target_feed <= 0.0 or purity <= 0.0:
        stream = target_product_stream(spec)
        d1, d2 = split_targets(spec)
        return d1 if str(stream or "").upper() == "D" else d2
    return max(0.01, float(target_feed) * float(recovery) / max(float(purity), 1.0e-12))


def target_is_light_product(spec):
    flows = feed_mole_flows(spec)
    target_id = target_component_id(spec)
    if not flows or not target_id:
        return True
    target_rank = component_volatility(target_id)
    return target_rank >= max(component_volatility(cid) for cid in flows)


def target_product_stream(spec):
    explicit = (
        spec.get("target_product_stream")
        or (spec.get("validation_case") or {}).get("target_product_stream")
    )
    if explicit:
        return str(explicit).strip()
    return "D" if target_is_light_product(spec) else "S1"


STREAM_COMPONENT_TABLE_ALIASES = {
    "MOLEFLOW": ("MOLEFLOW", "MOLE-FLOW", "MOLEFLOW_OUT", "MOLE-FLOW-OUT", "MOLEFLMX", "MOLEFLOWMX"),
    "MOLEFRAC": ("MOLEFRAC", "MOLE-FRAC", "MOLEFRAC_OUT", "MOLE-FRAC-OUT", "MOLEFRMX", "MOLEFRACMX"),
    "MASSFLOW": ("MASSFLOW", "MASS-FLOW", "MASSFLOW_OUT", "MASS-FLOW-OUT", "MASSFLMX", "MASSFLOWMX"),
    "MASSFRAC": ("MASSFRAC", "MASS-FRAC", "MASSFRAC_OUT", "MASS-FRAC-OUT", "MASSFRMX", "MASSFRACMX"),
}

STREAM_SUBSTREAM_ALIASES = ("MIXED", "VAPOR", "LIQUID", "TOTAL")


def component_aliases_for_id(spec, component_id):
    target = normalized_token(component_id)
    values = [component_id]
    validation_case = spec.get("validation_case") or {}
    for key in (
        "target_aspen_component_id",
        "target_component_id",
        "target_product",
        "aspen_component_id",
    ):
        value = validation_case.get(key)
        if normalized_token(value) == target:
            values.append(value)
    for row in list(spec.get("components") or []) + list(spec.get("feed_component_mapping") or []):
        if not isinstance(row, dict):
            continue
        row_values = [
            row.get("id"),
            row.get("aspen_component_id"),
            row.get("code"),
            row.get("name"),
            row.get("formula"),
            row.get("db_name"),
        ]
        if target and target in {normalized_token(item) for item in row_values}:
            values.extend(row_values)
    for stream in (spec.get("streams") or {}).values():
        if not isinstance(stream, dict):
            continue
        for key in (stream.get("mole_flow") or {}).keys():
            if normalized_token(key) == target:
                values.append(key)
    return unique_tokens(values)


def stream_component_output_path(stream_id, table, component_id):
    return r"\Data\Streams\{}\Output\{}\MIXED\{}".format(stream_id, table, component_id)


def stream_component_output_paths(stream_id, table, component_id):
    paths = []
    for table_id in STREAM_COMPONENT_TABLE_ALIASES.get(str(table).upper(), (table,)):
        paths.append(r"\Data\Streams\{}\Output\{}\{}".format(stream_id, table_id, component_id))
        for substream in STREAM_SUBSTREAM_ALIASES:
            paths.extend([
                r"\Data\Streams\{}\Output\{}\{}\{}".format(stream_id, table_id, substream, component_id),
                r"\Data\Streams\{}\Output\{}\{}\{}".format(stream_id, table_id, component_id, substream),
                r"\Data\Streams\{}\Output\{}\SUBSTREAM\{}\{}".format(stream_id, table_id, substream, component_id),
                r"\Data\Streams\{}\Output\{}\{}\SUBSTREAM\{}".format(stream_id, table_id, component_id, substream),
                r"\Data\Streams\{}\Output\{}\{}\{}".format(stream_id, substream, table_id, component_id),
                r"\Data\Streams\{}\Output\{}\{}\{}".format(stream_id, substream, component_id, table_id),
                r"\Data\Streams\{}\Output\STR_MAIN\{}\{}\{}".format(stream_id, substream, table_id, component_id),
                r"\Data\Streams\{}\Output\STR_MAIN\{}\{}\{}".format(stream_id, table_id, substream, component_id),
                r"\Data\Streams\{}\Output\STR_MAIN\{}\{}\{}".format(stream_id, table_id, component_id, substream),
            ])
    return unique_tokens(paths)


def stream_total_output_paths(stream_id):
    return [
        r"\Data\Streams\{}\Output\TOT_FLOW".format(stream_id),
        r"\Data\Streams\{}\Output\TOT_FLOW\MIXED".format(stream_id),
        r"\Data\Streams\{}\Output\TOTAL_FLOW".format(stream_id),
        r"\Data\Streams\{}\Output\TOTAL_FLOW\MIXED".format(stream_id),
        r"\Data\Streams\{}\Output\MOLEFLOW".format(stream_id),
        r"\Data\Streams\{}\Output\MOLEFLOW\MIXED".format(stream_id),
        r"\Data\Streams\{}\Output\MOLE-FLOW".format(stream_id),
        r"\Data\Streams\{}\Output\MOLE-FLOW\MIXED".format(stream_id),
    ]


def first_numeric_node_prefer_positive(app, paths):
    first_value = None
    first_path = ""
    for path in unique_tokens(paths):
        value = zero._aspen_numeric_node(app, path)
        if value is None:
            continue
        if first_value is None:
            first_value = value
            first_path = path
        if float(value) > 0.0:
            return value, path
    return first_value, first_path


def aspen_child_nodes(node):
    for attr in ("Elements", "Children"):
        try:
            children = getattr(node, attr)
        except Exception:
            children = None
        if children is None:
            continue
        try:
            count = int(getattr(children, "Count"))
        except Exception:
            count = None
        if count is not None:
            for index in list(range(1, count + 1)) + list(range(0, count)):
                try:
                    child = children.Item(index)
                except Exception:
                    continue
                if child is not None:
                    yield child
            return
        try:
            for child in children:
                if child is not None:
                    yield child
            return
        except Exception:
            continue


def aspen_node_name(node):
    for attr in ("Name", "name"):
        try:
            value = getattr(node, attr)
        except Exception:
            value = None
        if value:
            return str(value)
    return ""


def aspen_node_path(node, fallback):
    for attr in ("Path", "path"):
        try:
            value = getattr(node, attr)
        except Exception:
            value = None
        if value:
            return str(value)
    return fallback


def aspen_node_float(node):
    try:
        value = node.Value
    except Exception:
        try:
            value = node.value
        except Exception:
            value = None
    return safe_float(value)


def stream_component_tree_search(app, stream_id, table, component_ids, prefer_positive=False, max_nodes=2500):
    root_path = r"\Data\Streams\{}\Output".format(stream_id)
    try:
        root = app.Tree.FindNode(root_path)
    except Exception:
        root = None
    if root is None:
        return None, ""
    table_tokens = {
        normalized_token(value)
        for value in STREAM_COMPONENT_TABLE_ALIASES.get(str(table).upper(), (table,))
        if normalized_token(value)
    }
    component_tokens = {
        normalized_token(value)
        for value in component_ids
        if normalized_token(value)
    }
    first_value = None
    first_path = ""
    stack = [(root, [root_path])]
    visited = 0
    while stack and visited < max_nodes:
        node, parts = stack.pop()
        visited += 1
        name = aspen_node_name(node)
        next_parts = parts + ([name] if name else [])
        part_tokens = {normalized_token(part) for part in next_parts if normalized_token(part)}
        value = aspen_node_float(node)
        if value is not None and table_tokens.intersection(part_tokens) and component_tokens.intersection(part_tokens):
            path = aspen_node_path(node, "\\".join(next_parts))
            if first_value is None:
                first_value = value
                first_path = path
            if (not prefer_positive) or float(value) > 0.0:
                return value, path
        children = list(aspen_child_nodes(node))
        for child in reversed(children):
            stack.append((child, next_parts))
    return first_value, first_path


def first_component_numeric_node(app, spec, stream_id, table, component_id, prefer_positive=False):
    paths = []
    component_aliases = component_aliases_for_id(spec, component_id)
    for component_alias in component_aliases:
        paths.extend(stream_component_output_paths(stream_id, table, component_alias))
    if prefer_positive:
        value, path = first_numeric_node_prefer_positive(app, paths)
    else:
        value, path = zero._first_numeric_node(app, paths)
    if value is not None:
        return value, path
    return stream_component_tree_search(
        app, stream_id, table, component_aliases, prefer_positive=prefer_positive
    )



def target_product_stream_candidates(spec, preferred_stream=None):
    candidates = []
    for stream_id in (preferred_stream, spec.get("target_product_stream"), (spec.get("validation_case") or {}).get("target_product_stream")):
        token = str(stream_id or "").strip()
        if token and token not in candidates:
            candidates.append(token)
    for block in spec.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        for stream_id in block.get("out") or []:
            token = str(stream_id or "").strip()
            if token and token not in candidates:
                candidates.append(token)
    for stream_id in ("D", "S1", "W", "S3"):
        if stream_id not in candidates:
            candidates.append(stream_id)
    return candidates


def read_stream_target_metrics(app, spec, stream_id, target_id):
    product_component_flow, product_component_path = first_component_numeric_node(
        app, spec, stream_id, "MOLEFLOW", target_id, prefer_positive=True
    )
    product_total_flow, product_total_path = first_numeric_node_prefer_positive(
        app, stream_total_output_paths(stream_id)
    )
    purity, purity_path = first_component_numeric_node(
        app, spec, stream_id, "MOLEFRAC", target_id, prefer_positive=True
    )
    mass_purity, mass_purity_path = first_component_numeric_node(
        app, spec, stream_id, "MASSFRAC", target_id, prefer_positive=True
    )
    if product_component_flow is None and purity is not None and product_total_flow not in (None, 0):
        product_component_flow = float(purity) * float(product_total_flow)
        product_component_path = "computed_from_product_purity/product_total_flow"
    if purity is None and product_component_flow is not None and product_total_flow not in (None, 0):
        purity = float(product_component_flow) / float(product_total_flow)
        purity_path = "computed_from_product_component_flow/product_total_flow"
    return {
        "stream_id": stream_id,
        "target_component_flow": product_component_flow,
        "target_component_flow_path": product_component_path,
        "total_flow": product_total_flow,
        "total_flow_path": product_total_path,
        "purity": purity,
        "purity_path": purity_path,
        "mass_purity": mass_purity,
        "mass_purity_path": mass_purity_path,
    }


def best_target_product_stream_metrics(app, spec, target_id, preferred_stream):
    rows = [
        read_stream_target_metrics(app, spec, stream_id, target_id)
        for stream_id in target_product_stream_candidates(spec, preferred_stream)
    ]
    usable = [
        row for row in rows
        if safe_float(row.get("target_component_flow")) is not None and safe_float(row.get("target_component_flow")) > 0.0
    ]
    if usable:
        preferred = [row for row in usable if row.get("stream_id") == preferred_stream]
        if preferred and safe_float(preferred[0].get("total_flow"), 0.0) > 0.0:
            return preferred[0], rows
        return max(usable, key=lambda row: safe_float(row.get("target_component_flow"), -math.inf)), rows
    preferred_rows = [row for row in rows if row.get("stream_id") == preferred_stream]
    return (preferred_rows[0] if preferred_rows else (rows[0] if rows else {})), rows


def report_stream_ids(spec):
    values = list((spec.get("streams") or {}).keys())
    for block in spec.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        values.extend(block.get("in") or [])
        values.extend(block.get("out") or [])
    values.extend(target_product_stream_candidates(spec, target_product_stream(spec)))
    return unique_tokens(values)


def report_component_token_map(spec):
    mapping = {}
    for row in list(spec.get("components") or []) + list(spec.get("feed_component_mapping") or []):
        if not isinstance(row, dict):
            continue
        canonical = row.get("id") or row.get("aspen_component_id") or row.get("code") or row.get("name")
        if not canonical:
            continue
        for value in (
            canonical,
            row.get("id"),
            row.get("aspen_component_id"),
            row.get("code"),
            row.get("name"),
            row.get("formula"),
            row.get("db_name"),
        ):
            token = normalized_token(value)
            if token:
                mapping[token] = str(canonical)
    for stream in (spec.get("streams") or {}).values():
        if not isinstance(stream, dict):
            continue
        for component_id in (stream.get("mole_flow") or {}).keys():
            token = normalized_token(component_id)
            if token:
                mapping.setdefault(token, str(component_id))
    return mapping


def report_number_token(value):
    text = str(value or "").strip().replace(",", "")
    if not text or set(text) <= {"-", "."}:
        return None
    return safe_float(text.replace("D", "E").replace("d", "E"))


def aspen_report_text(report_path):
    path = Path(report_path)
    if not path.exists():
        return ""
    data = path.read_bytes()
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return data.decode(encoding, errors="replace")
        except Exception:
            continue
    return data.decode(errors="replace")


def aspen_report_table_kind(line):
    upper = re.sub(r"[^A-Z0-9]+", " ", str(line or "").upper()).strip()
    if not upper:
        return ""
    if any(token in upper for token in ("MOLE FLOW", "MOLEFLOW", "MOLAR FLOW")):
        return "MOLEFLOW"
    if any(token in upper for token in ("MOLE FRAC", "MOLEFRAC", "MOLE FRACTION")):
        return "MOLEFRAC"
    return ""


def parse_aspen_report_stream_tables(report_path, spec):
    text = aspen_report_text(report_path)
    if not text:
        return {}
    stream_lookup = {normalized_token(item): item for item in report_stream_ids(spec) if normalized_token(item)}
    component_lookup = report_component_token_map(spec)
    tables = {}
    active_streams = []
    active_table = ""
    for raw_line in text.replace("\f", "\n").splitlines():
        line = raw_line.rstrip()
        upper = line.upper()
        tokens = line.split()
        if not tokens:
            continue
        if "STREAM ID" in upper:
            active_streams = []
            for token in tokens:
                stream_id = stream_lookup.get(normalized_token(token))
                if stream_id and stream_id not in active_streams:
                    active_streams.append(stream_id)
                    tables.setdefault(stream_id, {
                        "MOLEFLOW": {},
                        "MOLEFRAC": {},
                        "TOTAL_FLOW": None,
                        "paths": {},
                    })
            active_table = ""
            continue
        table_kind = aspen_report_table_kind(line)
        if active_streams and table_kind:
            active_table = table_kind
            continue
        if not active_streams:
            continue
        numeric_values = [report_number_token(token) for token in tokens]
        numeric_values = [value for value in numeric_values if value is not None]
        if not numeric_values:
            continue
        normalized_first = normalized_token(tokens[0])
        is_total_flow = normalized_first == "TOTAL" and "FLOW" in {normalized_token(token) for token in tokens[1:4]}
        if is_total_flow:
            for stream_id, value in zip(active_streams, numeric_values[-len(active_streams):]):
                tables.setdefault(stream_id, {
                    "MOLEFLOW": {},
                    "MOLEFRAC": {},
                    "TOTAL_FLOW": None,
                    "paths": {},
                })
                tables[stream_id]["TOTAL_FLOW"] = value
                tables[stream_id]["paths"]["TOTAL_FLOW"] = "aspen_report:{}:TOTAL_FLOW:{}".format(
                    Path(report_path).name, stream_id
                )
            continue
        if not active_table:
            continue
        component_id = component_lookup.get(normalized_first)
        if not component_id:
            continue
        values = numeric_values[-len(active_streams):]
        if len(values) != len(active_streams):
            continue
        for stream_id, value in zip(active_streams, values):
            tables.setdefault(stream_id, {
                "MOLEFLOW": {},
                "MOLEFRAC": {},
                "TOTAL_FLOW": None,
                "paths": {},
            })
            tables[stream_id][active_table][component_id] = value
            tables[stream_id]["paths"][(active_table, component_id)] = (
                "aspen_report:{}:{}:{}:{}".format(Path(report_path).name, stream_id, active_table, component_id)
            )
    return tables


def report_component_value(stream_table, spec, component_id, table):
    for alias in component_aliases_for_id(spec, component_id):
        token = normalized_token(alias)
        for key, value in (stream_table.get(table) or {}).items():
            if normalized_token(key) == token:
                path = (stream_table.get("paths") or {}).get((table, key), "")
                return value, path
    return None, ""


def read_report_stream_target_metrics(tables, spec, stream_id, target_id):
    stream_table = tables.get(stream_id) or {}
    product_component_flow, product_component_path = report_component_value(
        stream_table, spec, target_id, "MOLEFLOW"
    )
    purity, purity_path = report_component_value(stream_table, spec, target_id, "MOLEFRAC")
    mass_purity, mass_purity_path = report_component_value(stream_table, spec, target_id, "MASSFRAC")
    total_flow = safe_float(stream_table.get("TOTAL_FLOW"))
    total_path = (stream_table.get("paths") or {}).get("TOTAL_FLOW", "")
    if total_flow is None:
        component_values = [
            safe_float(value)
            for value in (stream_table.get("MOLEFLOW") or {}).values()
            if safe_float(value) is not None
        ]
        if component_values:
            total_flow = sum(component_values)
            total_path = "aspen_report:sum_component_moleflows:{}".format(stream_id)
    if product_component_flow is None and purity is not None and total_flow not in (None, 0):
        product_component_flow = float(purity) * float(total_flow)
        product_component_path = "computed_from_aspen_report_purity/product_total_flow"
    if purity is None and product_component_flow is not None and total_flow not in (None, 0):
        purity = float(product_component_flow) / float(total_flow)
        purity_path = "computed_from_aspen_report_component_flow/product_total_flow"
    return {
        "stream_id": stream_id,
        "target_component_flow": product_component_flow,
        "target_component_flow_path": product_component_path,
        "total_flow": total_flow,
        "total_flow_path": total_path,
        "purity": purity,
        "purity_path": purity_path,
        "mass_purity": mass_purity,
        "mass_purity_path": mass_purity_path,
    }


def best_report_target_product_stream_metrics(tables, spec, target_id, preferred_stream):
    rows = [
        read_report_stream_target_metrics(tables, spec, stream_id, target_id)
        for stream_id in target_product_stream_candidates(spec, preferred_stream)
        if stream_id in tables
    ]
    usable = [
        row for row in rows
        if safe_float(row.get("target_component_flow")) is not None and safe_float(row.get("target_component_flow")) > 0.0
    ]
    if usable:
        preferred = [row for row in usable if row.get("stream_id") == preferred_stream]
        if preferred and safe_float(preferred[0].get("total_flow"), 0.0) > 0.0:
            return preferred[0], rows
        return max(usable, key=lambda row: safe_float(row.get("target_component_flow"), -math.inf)), rows
    preferred_rows = [row for row in rows if row.get("stream_id") == preferred_stream]
    return (preferred_rows[0] if preferred_rows else (rows[0] if rows else {})), rows


def read_generic_metrics_from_aspen_report(report_path, spec):
    if not report_path:
        return {}
    tables = parse_aspen_report_stream_tables(report_path, spec)
    result = {
        "aspen_report_path": str(Path(report_path).resolve()),
        "aspen_report_component_metric_status": "no_stream_component_tables",
    }
    if not tables:
        return result
    target_id = target_component_id(spec)
    preferred_product_stream = target_product_stream(spec)
    product_metrics, product_metric_candidates = best_report_target_product_stream_metrics(
        tables, spec, target_id, preferred_product_stream
    )
    product_stream = product_metrics.get("stream_id") or preferred_product_stream
    product_component_flow = product_metrics.get("target_component_flow")
    product_total_flow = product_metrics.get("total_flow")
    purity = product_metrics.get("purity")
    mass_purity = product_metrics.get("mass_purity")
    purity_basis = target_purity_basis(spec)
    selected_purity = mass_purity if purity_basis == "mass_fraction" else purity
    selected_purity_path = (
        product_metrics.get("mass_purity_path")
        if purity_basis == "mass_fraction"
        else product_metrics.get("purity_path")
    ) or ""
    feed_stream = tables.get("F") or {}
    feed_component_flow, feed_component_path = report_component_value(feed_stream, spec, target_id, "MOLEFLOW")
    if feed_component_flow is None:
        feed_component_flow = safe_float(feed_mole_flows(spec).get(target_id))
        feed_component_path = "spec.streams.F.mole_flow"
    recovery = None
    if product_component_flow is not None and feed_component_flow not in (None, 0):
        recovery = float(product_component_flow) / float(feed_component_flow)
    result.update({
        "aspen_report_component_metric_status": "component_metrics_available"
        if product_component_flow is not None or purity is not None else "target_component_missing",
        "target_product_stream": product_stream,
        "target_product_stream_preferred": preferred_product_stream,
        "target_product_stream_selection": "aspen_report_max_target_component_flow"
        if product_stream != preferred_product_stream else "preferred_stream",
        "target_product_stream_candidates": product_metric_candidates,
        "target_purity_basis": purity_basis,
        "target_purity_target": target_purity_value(spec),
        "target_purity_value": selected_purity,
        "target_purity_mole_frac": purity,
        "target_purity_mole_frac_path": product_metrics.get("purity_path") or "",
        "target_purity_mass_frac": mass_purity,
        "target_purity_mass_frac_path": product_metrics.get("mass_purity_path") or "",
        "target_purity_path": selected_purity_path,
        "target_recovery": recovery,
        "target_recovery_reference": target_recovery_reference(spec),
        "target_recovery_loss": None if recovery is None else max(0.0, 1.0 - recovery),
        "purity_pass": bool(selected_purity is not None and selected_purity >= target_purity_value(spec)),
        "target_product_component_flow_kmol_hr": product_component_flow,
        "target_product_component_flow_path": product_metrics.get("target_component_flow_path") or "",
        "target_feed_component_flow_kmol_hr": feed_component_flow,
        "target_feed_component_flow_path": feed_component_path,
        "target_product_total_flow_kmol_hr": product_total_flow,
        "target_product_total_flow_path": product_metrics.get("total_flow_path") or "",
    })
    sid = solvent_id(spec)
    loss_d, loss_d_path = report_component_value(tables.get("D") or {}, spec, sid, "MOLEFLOW")
    loss_s1, loss_s1_path = report_component_value(tables.get("S1") or {}, spec, sid, "MOLEFLOW")
    result["solvent_loss_top1"] = loss_d
    result["solvent_loss_top2"] = loss_s1
    result["solvent_loss_top1_path"] = loss_d_path
    result["solvent_loss_top2_path"] = loss_s1_path
    result["solvent_loss_total"] = solvent_loss_total_from_row(result)
    return result


def merge_missing_aspen_metrics(row, fallback):
    if not fallback:
        return row
    merged = dict(row)
    always_copy = {
        "aspen_report_path",
        "aspen_report_component_metric_status",
        "aspen_report_export_type",
        "aspen_report_export_base",
        "aspen_report_export_warning",
        "aspen_report_size_bytes",
        "aspen_report_export_candidates",
        "target_product_stream",
        "target_product_stream_preferred",
        "target_product_stream_selection",
        "target_product_stream_candidates",
    }
    positive_override_keys = {
        "target_product_total_flow_kmol_hr",
    }
    for key, value in fallback.items():
        if value in (None, ""):
            continue
        if key in always_copy:
            merged[key] = value
            continue
        current = merged.get(key)
        if current in (None, ""):
            merged[key] = value
            continue
        if key in positive_override_keys and safe_float(current, 0.0) <= 0.0 and safe_float(value) is not None:
            merged[key] = value
    return merged


def clean_formula_token(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"[-_]", text, maxsplit=1)[0]


def molecular_weight_from_formula(formula):
    text = clean_formula_token(formula)
    if not text:
        return None
    tokens = list(re.finditer(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)", text))
    if not tokens or "".join(item.group(0) for item in tokens) != text:
        return None
    total = 0.0
    for item in tokens:
        element = item.group(1)
        weight = ATOMIC_WEIGHTS.get(element)
        if weight is None:
            return None
        count = float(item.group(2) or 1.0)
        total += weight * count
    return total if total > 0 else None


def component_record_for_id(spec, component_id):
    target = normalized_token(component_id)
    for group in (
        spec.get("components") or [],
        spec.get("feed_component_mapping") or [],
        [(spec.get("validation_case") or {})],
    ):
        for row in group if isinstance(group, list) else []:
            if not isinstance(row, dict):
                continue
            tokens = [
                row.get("id"),
                row.get("aspen_component_id"),
                row.get("code"),
                row.get("name"),
            ]
            if target and target in {normalized_token(item) for item in tokens if item}:
                return row
    return {}


def solvent_economics_for_case(spec, row):
    component_id = str((row or {}).get("aspen_component_id") or solvent_id(spec) or "").strip()
    component = component_record_for_id(spec, component_id)
    mw = (
        safe_float((row or {}).get("solvent_molecular_weight_kg_per_kmol"))
        or safe_float(component.get("molecular_weight_kg_per_kmol"))
        or safe_float(component.get("molecular_weight"))
        or molecular_weight_from_formula(component.get("formula"))
        or tac_model.DEFAULT_SOLVENT_MW.get(component_id)
    )
    explicit_price = (
        safe_float((row or {}).get("solvent_price_usd_per_kg"))
        or safe_float(component.get("solvent_price_usd_per_kg"))
        or safe_float(component.get("price_usd_per_kg"))
    )
    default_economics = tac_model.solvent_economics_for(component_id, {})
    price = explicit_price
    basis = (
        (row or {}).get("solvent_price_basis")
        or component.get("solvent_price_basis")
        or component.get("price_basis")
        or "component_formula_or_default"
    )
    source_url = (
        (row or {}).get("solvent_price_source_url")
        or component.get("solvent_price_source_url")
        or component.get("source_url")
        or default_economics.get("source_url", "")
    )
    source_note = (
        (row or {}).get("solvent_price_source_note")
        or component.get("solvent_price_source_note")
        or component.get("source_note")
        or default_economics.get("source_note", "")
    )
    price_range = (
        (row or {}).get("solvent_price_range_usd_per_kg")
        or component.get("solvent_price_range_usd_per_kg")
        or default_economics.get("price_range_usd_per_kg", "")
    )
    if price is None:
        price = safe_float(default_economics.get("solvent_price_usd_per_kg"))
        basis = default_economics.get("basis") or "default_solvent_price"
        source_url = default_economics.get("source_url", "")
        source_note = default_economics.get("source_note", "")
        price_range = default_economics.get("price_range_usd_per_kg", "")
    if price is None:
        price = 0.0
        basis = "molecular_weight_from_component_formula_price_assumed_zero_review_required"
    return {
        component_id: {
            "molecular_weight_kg_per_kmol": mw,
            "solvent_price_usd_per_kg": price,
            "basis": basis,
            "source_url": source_url,
            "source_note": source_note,
            "price_range_usd_per_kg": price_range,
        }
    }


def solvent_loss_total_from_row(row):
    loss_1 = safe_float(row.get("solvent_loss_top1"))
    loss_2 = safe_float(row.get("solvent_loss_top2"))
    if loss_1 is None and loss_2 is None:
        return None
    return (loss_1 or 0.0) + (loss_2 or 0.0)


def read_generic_aspen_metrics(app, spec, runner=None):
    import variable_map as vm

    result = {}
    qcond1, qcond1_path = zero._first_numeric_node(app, [
        getattr(vm, "QCOND1_PATH", ""),
        r"\Data\Blocks\B1\Output\COND_DUTY",
    ])
    qcond2, qcond2_path = zero._first_numeric_node(app, [
        getattr(vm, "QCOND2_PATH", ""),
        r"\Data\Blocks\B2\Output\COND_DUTY",
    ])
    result["Qcond_1"] = qcond1
    result["Qcond_2"] = qcond2
    result["Qcond_1_path"] = qcond1_path
    result["Qcond_2_path"] = qcond2_path

    target_id = target_component_id(spec)
    preferred_product_stream = target_product_stream(spec)
    target_purity = target_purity_value(spec)
    feed_flow_spec = safe_float(feed_mole_flows(spec).get(target_id))
    feed_component_flow, feed_component_path = first_component_numeric_node(
        app, spec, "F", "MOLEFLOW", target_id, prefer_positive=True
    )
    product_metrics, product_metric_candidates = best_target_product_stream_metrics(
        app, spec, target_id, preferred_product_stream
    )
    product_stream = product_metrics.get("stream_id") or preferred_product_stream
    product_component_flow = product_metrics.get("target_component_flow")
    product_component_path = product_metrics.get("target_component_flow_path") or ""
    product_total_flow = product_metrics.get("total_flow")
    product_total_path = product_metrics.get("total_flow_path") or ""
    purity = product_metrics.get("purity")
    purity_path = product_metrics.get("purity_path") or ""
    mass_purity = product_metrics.get("mass_purity")
    mass_purity_path = product_metrics.get("mass_purity_path") or ""
    purity_basis = target_purity_basis(spec)
    selected_purity = mass_purity if purity_basis == "mass_fraction" else purity
    selected_purity_path = mass_purity_path if purity_basis == "mass_fraction" else purity_path
    if feed_component_flow is None:
        feed_component_flow = feed_flow_spec
        feed_component_path = "spec.streams.F.mole_flow"
    recovery = None
    if product_component_flow is not None and feed_component_flow not in (None, 0):
        recovery = float(product_component_flow) / float(feed_component_flow)

    result.update({
        "target_product": spec.get("target_product") or (spec.get("validation_case") or {}).get("target_product") or target_id,
        "target_component_id": target_id,
        "target_product_stream": product_stream,
        "target_product_stream_preferred": preferred_product_stream,
        "target_product_stream_selection": "max_target_component_flow" if product_stream != preferred_product_stream else "preferred_stream",
        "target_product_stream_candidates": product_metric_candidates,
        "target_purity_target": target_purity,
        "target_purity_basis": purity_basis,
        "target_purity_value": selected_purity,
        "target_purity_mole_frac": purity,
        "target_purity_mole_frac_path": purity_path,
        "target_purity_mass_frac": mass_purity,
        "target_purity_mass_frac_path": mass_purity_path,
        "target_purity_path": selected_purity_path,
        "target_recovery": recovery,
        "target_recovery_reference": target_recovery_reference(spec),
        "target_recovery_loss": None if recovery is None else max(0.0, 1.0 - recovery),
        "purity_pass": bool(selected_purity is not None and selected_purity >= target_purity),
        "target_product_component_flow_kmol_hr": product_component_flow,
        "target_product_component_flow_path": product_component_path,
        "target_feed_component_flow_kmol_hr": feed_component_flow,
        "target_feed_component_flow_path": feed_component_path,
        "target_product_total_flow_kmol_hr": product_total_flow,
        "target_product_total_flow_path": product_total_path,
    })

    sid = solvent_id(spec)
    loss_d, loss_d_path = first_component_numeric_node(
        app, spec, "D", "MOLEFLOW", sid, prefer_positive=True
    )
    loss_s1, loss_s1_path = first_component_numeric_node(
        app, spec, "S1", "MOLEFLOW", sid, prefer_positive=True
    )
    result["solvent_loss_top1"] = loss_d
    result["solvent_loss_top2"] = loss_s1
    result["solvent_loss_top1_path"] = loss_d_path
    result["solvent_loss_top2_path"] = loss_s1_path
    result["solvent_loss_total"] = solvent_loss_total_from_row(result)

    if runner is not None and hasattr(runner, "_read_tac_inputs"):
        try:
            result.update(runner._read_tac_inputs(app))
        except Exception as exc:
            result["tac_input_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
    else:
        result.update(read_generic_tac_inputs(app, spec))
    return result


def read_optional_numeric_node(app, path):
    if not path:
        return None
    try:
        value = zero._aspen_numeric_node(app, path)
    except Exception:
        return None
    return safe_float(value)


def read_generic_column_diameter(app, block):
    import variable_map as vm

    values = []
    for path in getattr(vm, "TAC_COLUMN_DIAMETER_SECTION_PATHS", {}).get(block, []):
        value = read_optional_numeric_node(app, path)
        if value is not None and value > 0.0:
            values.append(value)
    if values:
        return max(values), "aspen_hydraulic_section_max"
    for path in getattr(vm, "TAC_COLUMN_DIAMETER_CANDIDATE_PATHS", {}).get(block, []):
        value = read_optional_numeric_node(app, path)
        if value is not None and value > 0.0:
            return value, "aspen_output_candidate"
    path = getattr(vm, "TAC_COLUMN_DIAMETER_PATHS", {}).get(block)
    value = read_optional_numeric_node(app, path)
    if value is not None and value > 0.0:
        return value, "aspen_legacy_path"
    fallback = safe_float(os.environ.get("ED_AGENT_FALLBACK_COLUMN_DIAMETER_M"), 1.0)
    return fallback, "fallback_engineering_estimate_review_required"


def read_generic_column_height(app, spec, block):
    import variable_map as vm

    sections = getattr(vm, "HYDRAULIC_SECTION_STAGE_PATHS", {}).get(block, [])
    spacing_paths = getattr(vm, "TAC_COLUMN_TRAY_SPACING_SECTION_PATHS", {}).get(block, [])
    height = 0.0
    any_section = False
    for index, section in enumerate(sections):
        stage1 = read_optional_numeric_node(app, section.get("stage1"))
        stage2 = read_optional_numeric_node(app, section.get("stage2"))
        spacing_path = spacing_paths[index] if index < len(spacing_paths) else None
        spacing = read_optional_numeric_node(app, spacing_path)
        if stage1 is None or stage2 is None or spacing is None or spacing <= 0.0:
            continue
        tray_count = max(0, int(round(stage2)) - int(round(stage1)) + 1)
        height += tray_count * spacing
        any_section = True
    if any_section:
        return height + float(getattr(vm, "TAC_COLUMN_HEIGHT_ALLOWANCE_M", 0.0) or 0.0), "aspen_hydraulic_section_spacing"

    block_spec = (spec.get("radfrac") or {}).get(block) or {}
    nstage = safe_float(block_spec.get("nstage"))
    spacing = safe_float(os.environ.get("ED_AGENT_FALLBACK_TRAY_SPACING_M"), 0.5)
    allowance = safe_float(os.environ.get("ED_AGENT_FALLBACK_COLUMN_HEIGHT_ALLOWANCE_M"), 0.0) or 0.0
    if nstage is not None and nstage > 1:
        return max(1.0, (nstage - 1.0) * spacing + allowance), "fallback_stage_count_estimate_review_required"
    return None, "missing"


def read_generic_tac_inputs(app, spec):
    import variable_map as vm

    result = {}
    basis = {}
    for block in ("B1", "B2"):
        diameter, diameter_basis = read_generic_column_diameter(app, block)
        height, height_basis = read_generic_column_height(app, spec, block)
        result["{}_diameter_m".format(block)] = diameter
        result["{}_height_m".format(block)] = height
        basis["{}_diameter_m".format(block)] = diameter_basis
        basis["{}_height_m".format(block)] = height_basis
        result["{}_top_temp".format(block)] = read_optional_numeric_node(
            app, getattr(vm, "TAC_COLUMN_TOP_TEMP_PATHS", {}).get(block)
        )
        result["{}_bottom_temp".format(block)] = read_optional_numeric_node(
            app, getattr(vm, "TAC_COLUMN_BOTTOM_TEMP_PATHS", {}).get(block)
        )
        result["{}_reboiler_outlet_temp".format(block)] = read_optional_numeric_node(
            app, getattr(vm, "TAC_REBOILER_OUTLET_TEMP_PATHS", {}).get(block)
        )
    result["feed_heater_duty"] = read_optional_numeric_node(app, getattr(vm, "TAC_FEED_HEATER_DUTY_PATH", ""))
    result["tac_input_basis"] = basis
    return result


def read_generic_metrics_from_archive(archive_path, spec):
    import variable_map as vm
    import win32com.client as win32

    archive_path = Path(archive_path)
    if not archive_path.exists():
        return {"target_metric_warning": "archive_missing: {}".format(archive_path)}
    pythoncom_module = zero.co_initialize_if_available()
    baseline_aspen = zero.aspen_automation_pids()
    app = None
    stage_dir = None
    try:
        stage_dir = zero.make_aspen_stage_dir(archive_path)
        staged_bkp = zero.stage_input_file_for_aspen(archive_path, stage_dir)
        app = zero.open_aspen_document_with_retry(
            win32,
            vm.ASPEN_PROG_ID,
            lambda active_app: active_app.InitFromArchive2(str(staged_bkp))
            if hasattr(active_app, "InitFromArchive2")
            else active_app.InitFromArchive(str(staged_bkp)),
            attempts=4,
            settle_seconds=8,
        )
        metrics = read_generic_aspen_metrics(app, spec)
        metrics["target_metric_archive"] = str(archive_path.resolve())
        return metrics
    except Exception as exc:
        return {"target_metric_warning": "{}: {}".format(exc.__class__.__name__, exc)}
    finally:
        zero.close_aspen_app(app)
        zero.cleanup_new_aspen_processes(baseline_aspen, settle_seconds=8)
        zero.cleanup_aspen_stage_dir(stage_dir)
        zero.co_uninitialize_if_needed(pythoncom_module)


def professional_constraint_violation(spec, row):
    physical_tolerance = 1.0e-6
    violation = 0.0
    if not truthy(row.get("converged")):
        violation += 1.0

    purity_target = target_purity_value(spec)
    purity = row_target_purity_value(row, spec)
    if purity is None:
        violation += 1.0
    elif purity < -physical_tolerance or purity > 1.0 + physical_tolerance:
        violation += 1.0
    elif purity < purity_target:
        violation += max(0.0, (purity_target - purity) * 1.0e5)

    recovery = safe_float(row.get("target_recovery"))
    recovery_target = target_recovery_reference(spec)
    if recovery is None:
        violation += 1.0
    elif recovery < -physical_tolerance or recovery > 1.0 + physical_tolerance:
        violation += 1.0
    elif recovery_target is not None and recovery < recovery_target:
        violation += max(0.0, (recovery_target - recovery) * 1.0e4)

    for name in ("Qreb_1", "Qreb_2"):
        reboiler_duty = safe_float(row.get(name))
        if reboiler_duty is None or reboiler_duty <= 0.0:
            violation += 1.0

    duty = safe_float(row.get("total_reboiler_duty"))
    if duty is None or duty <= 0.0:
        violation += 1.0

    tac = safe_float(row.get("TAC"))
    if tac is None or tac <= 0.0:
        violation += 1.0

    energy_cost = safe_float(row.get("energy_cost"))
    if energy_cost is None or energy_cost < 0.0:
        violation += 1.0

    for name in ("solvent_loss_top1", "solvent_loss_top2", "solvent_loss_total"):
        solvent_loss = safe_float(row.get(name))
        if solvent_loss is not None and solvent_loss < -physical_tolerance:
            violation += 1.0

    return violation


def strip_professional_constraint_diagnostics(message):
    text = str(message or "").strip()
    marker = "professional_constraints_failed:"
    if marker not in text:
        return text
    return text.split(marker, 1)[0].strip()


def finalize_case_row(spec, case_spec, row):
    case_spec = case_spec or spec
    row = annotate_applied_stage_values(row, case_spec)
    row.setdefault("target_product", spec.get("target_product") or target_component_id(spec))
    row.setdefault("target_component_id", target_component_id(spec))
    row.setdefault("target_product_stream", target_product_stream(spec))
    row["target_purity_target"] = target_purity_value(case_spec)
    row["target_purity_basis"] = target_purity_basis(case_spec)
    row["target_purity_value"] = row_target_purity_value(row, case_spec)
    row["target_purity_path"] = row_target_purity_path(row, case_spec)
    row["target_recovery_reference"] = target_recovery_reference(spec)
    if row.get("solvent_loss_total") in (None, ""):
        row["solvent_loss_total"] = solvent_loss_total_from_row(row)

    row["aspen_run_pass_or_fail"] = row.get("aspen_run_pass_or_fail") or row.get("pass_or_fail", "")
    if truthy(row.get("converged")):
        try:
            row.update(tac_model.calculate_tac(row, solvent_economics_for_case(case_spec, row)))
        except Exception as exc:
            row["TAC"] = None
            row["tac_error"] = "{}: {}".format(exc.__class__.__name__, exc)
    else:
        row["TAC"] = None
        row["tac_error"] = row.get("tac_error") or "Skipped TAC because Aspen did not converge."

    violation = professional_constraint_violation(case_spec, row)
    row["constraint_violation"] = violation
    row["feasible"] = bool(violation <= 1.0e-12)
    row["purity_pass"] = bool(
        row_target_purity_value(row, case_spec) is not None
        and row_target_purity_value(row, case_spec) >= target_purity_value(case_spec)
    )
    row["target_recovery_loss"] = (
        None if safe_float(row.get("target_recovery")) is None
        else max(0.0, 1.0 - safe_float(row.get("target_recovery")))
    )
    row["decision_score"] = row.get("TAC") if row["feasible"] else None
    if not row["feasible"]:
        original_message = strip_professional_constraint_diagnostics(row.get("error_message"))
        detail = (
            "professional_constraints_failed: purity_basis={} purity_target={} purity={} recovery_target={} recovery={} TAC={} energy_cost={}."
        ).format(
            target_purity_basis(case_spec),
            target_purity_value(case_spec),
            row_target_purity_value(row, case_spec),
            target_recovery_reference(case_spec),
            row.get("target_recovery"),
            row.get("TAC"),
            row.get("energy_cost"),
        )
        if detail not in original_message:
            row["error_message"] = "{} {}".format(original_message, detail).strip()
        row["pass_or_fail"] = "FAIL"
    elif str(row.get("aspen_run_pass_or_fail") or "").upper() == "PASS":
        row["pass_or_fail"] = "PASS"
        row["error_message"] = strip_professional_constraint_diagnostics(row.get("error_message"))
    row.update(topology_metadata(case_spec))
    return attach_case_objectives(case_spec, row)


def base_case(spec):
    case = dict(spec.get("validation_case") or {})
    d1_guess, d2_guess = split_targets(spec)
    b1 = (spec.get("radfrac") or {}).get("B1") or {}
    d_product = (b1.get("products") or {}).get("D") or []
    condenser_mode = b1.get("condenser") or "PARTIAL-V"
    distillate_phase = d_product[1] if len(d_product) > 1 else ("L" if condenser_mode == "TOTAL" else "V")
    defaults = {
        "case_id": "GENERIC_ASPEN_OPT_0001",
        "solvent_name": solvent_id(spec),
        "aspen_component_id": solvent_id(spec),
        "SF": 1.5,
        "makeup_flow": max(0.02, feed_total(spec) * 0.02),
        "recycle_control_mode": spec.get("recycle_control_mode") or "calculator_inventory_control",
        "RR1": 3.0,
        "D1": d1_guess,
        "RR2": 1.2,
        "D2": d2_guess,
        "N1": 60,
        "NF1": 40,
        "NS1": 20,
        "N2": 12,
        "NF2": 2,
        "condenser_mode": condenser_mode,
        "distillate_phase": distillate_phase,
        "max_iterations": int(b1.get("max_iterations") or 200),
    }
    for key, value in defaults.items():
        case.setdefault(key, value)
    return case


def control_family_coverage_first(templates):
    required_modes = (
        "calculator_inventory_control",
        "bounded_nonnegative_makeup_control",
    )
    selected = []
    selected_ids = set()
    for mode in required_modes:
        for index, template in enumerate(templates or []):
            if index in selected_ids:
                continue
            if str((template or {}).get("recycle_control_mode") or "").strip() == mode:
                selected.append(template)
                selected_ids.add(index)
                break
    for index, template in enumerate(templates or []):
        if index not in selected_ids:
            selected.append(template)
    return selected


def candidate_cases(spec, population_size, bounds=None, prioritize_control_family_coverage=False):
    base = base_case(spec)
    cases = []
    d1_guess, d2_guess = split_targets(spec)
    repair_templates = [
        {
            "source": "generic_seed_calculator_inventory",
            "recycle_control_mode": "calculator_inventory_control",
        },
        {"source": "generic_seed_high_purity_reflux", "recycle_control_mode": "calculator_inventory_control", "SF": 1.2, "RR1": 5.0, "RR2": 2.0, "D1": d1_guess * 0.96, "D2": d2_guess * 1.20, "N1": 70, "N2": 14},
        {"source": "generic_seed_balanced_reflux", "recycle_control_mode": "calculator_inventory_control", "SF": 1.0, "RR1": 3.0, "RR2": 1.2, "D1": d1_guess * 0.98, "D2": d2_guess * 1.35, "N1": 60, "N2": 12},
        {"source": "generic_seed_low_solvent_high_rr", "recycle_control_mode": "calculator_inventory_control", "SF": 0.7, "RR1": 4.5, "RR2": 1.5, "D1": d1_guess * 0.94, "D2": d2_guess * 1.45, "N1": 75, "N2": 14},
        {"source": "generic_seed_more_solvent_moderate_rr", "recycle_control_mode": "calculator_inventory_control", "SF": 2.0, "RR1": 2.5, "RR2": 1.0, "D1": d1_guess * 1.00, "D2": d2_guess * 1.25, "N1": 60, "N2": 12},
        {"source": "generic_repair_calculator_total_liquid", "recycle_control_mode": "calculator_inventory_control", "condenser_mode": "TOTAL", "distillate_phase": "L", "SF": 1.5, "RR1": 3.0, "RR2": 1.2, "N1": 55, "N2": 12},
        {"source": "generic_repair_calculator_recycle_bleed", "recycle_control_mode": "calculator_inventory_control", "SF": 1.5, "RR1": 3.0, "RR2": 1.2, "D2": d2_guess * 1.55, "N1": 60, "N2": 14},
        {"source": "generic_repair_calculator_strong_bleed", "recycle_control_mode": "calculator_inventory_control", "SF": 1.2, "RR1": 4.0, "RR2": 1.5, "D2": d2_guess * 1.85, "N1": 70, "N2": 16},
        {"source": "generic_repair_calculator_total_liquid_bleed", "recycle_control_mode": "calculator_inventory_control", "condenser_mode": "TOTAL", "distillate_phase": "L", "SF": 1.2, "RR1": 3.5, "RR2": 1.2, "D2": d2_guess * 1.65, "N1": 60, "N2": 12},
        {"source": "generic_repair_calculator_soft_split", "recycle_control_mode": "calculator_inventory_control", "SF": 1.0, "makeup_flow": feed_total(spec) * 0.01, "RR1": 4.0, "RR2": 1.5, "D1": d1_guess * 0.92, "D2": d2_guess * 1.35, "N1": 70, "N2": 14},
        {"source": "generic_repair_calculator_hard_split", "recycle_control_mode": "calculator_inventory_control", "SF": 2.5, "makeup_flow": feed_total(spec) * 0.05, "RR1": 6.0, "RR2": 2.5, "D1": d1_guess * 1.02, "D2": d2_guess * 1.60, "N1": 80, "N2": 16},
        {
            "source": "generic_repair_calculator_partial_vapor_compat",
            "recycle_control_mode": "calculator_inventory_control",
            "SF": 3.0,
            "RR1": 6.0,
            "RR2": 2.0,
            "N1": 50,
            "N2": 12,
        },
        {"source": "generic_repair_calculator_very_relaxed", "recycle_control_mode": "calculator_inventory_control", "SF": 2.5, "RR1": 1.0, "RR2": 0.5, "D1": d1_guess * 0.8, "D2": d2_guess * 0.8, "N1": 35, "N2": 8},
        {"source": "generic_diagnostic_bounded_relaxed_rr", "recycle_control_mode": "bounded_nonnegative_makeup_control", "SF": 3.0, "RR1": 2.0, "RR2": 0.8, "N1": 50, "N2": 12},
        {"source": "generic_diagnostic_bounded_low_solvent", "recycle_control_mode": "bounded_nonnegative_makeup_control", "SF": 1.5, "RR1": 2.0, "RR2": 0.8, "N1": 40, "N2": 10},
        {"source": "generic_diagnostic_bounded_high_solvent", "recycle_control_mode": "bounded_nonnegative_makeup_control", "SF": 5.0, "RR1": 3.0, "RR2": 1.2, "N1": 60, "N2": 14},
        {"source": "generic_diagnostic_bounded_total_liquid", "recycle_control_mode": "bounded_nonnegative_makeup_control", "condenser_mode": "TOTAL", "distillate_phase": "L", "SF": 3.0, "RR1": 2.0, "RR2": 0.8, "N1": 45, "N2": 10},
        {"source": "generic_diagnostic_bounded_recycle_bleed", "recycle_control_mode": "bounded_nonnegative_makeup_control", "SF": 3.0, "RR1": 2.0, "RR2": 0.8, "D2": max(d2_guess * 1.45, d2_guess + feed_total(spec) * 0.2), "N1": 45, "N2": 12},
        {"source": "generic_diagnostic_bounded_strong_bleed", "recycle_control_mode": "bounded_nonnegative_makeup_control", "SF": 3.0, "RR1": 1.5, "RR2": 0.6, "D2": max(d2_guess * 1.8, d2_guess + feed_total(spec) * 0.35), "N1": 40, "N2": 12},
        {"source": "generic_diagnostic_bounded_total_liquid_bleed", "recycle_control_mode": "bounded_nonnegative_makeup_control", "condenser_mode": "TOTAL", "distillate_phase": "L", "SF": 2.0, "RR1": 1.5, "RR2": 0.6, "D2": max(d2_guess * 1.6, d2_guess + feed_total(spec) * 0.25), "N1": 40, "N2": 10},
        {"source": "generic_diagnostic_fixed_makeup_low", "recycle_control_mode": "fixed_fresh_makeup_diagnostic", "SF": 3.0, "makeup_flow": feed_total(spec) * 0.005, "RR1": 2.0, "RR2": 0.8, "N1": 45, "N2": 10},
        {"source": "generic_diagnostic_fixed_makeup_wide_recovery", "recycle_control_mode": "fixed_fresh_makeup_diagnostic", "SF": 4.0, "makeup_flow": feed_total(spec) * 0.02, "RR1": 3.0, "RR2": 1.0, "D1": d1_guess * 0.7, "D2": d2_guess * 0.7, "N1": 45, "N2": 10},
        {"source": "generic_diagnostic_fixed_makeup_high_reflux", "recycle_control_mode": "fixed_fresh_makeup_diagnostic", "SF": 5.0, "RR1": 8.0, "RR2": 3.0, "N1": 70, "N2": 16},
    ]
    templates = repair_templates
    if prioritize_control_family_coverage:
        templates = control_family_coverage_first(repair_templates)
    for index, template in enumerate(templates[: max(1, int(population_size or 1))]):
        case = dict(base)
        case.update(template)
        case["case_id"] = "GENERIC_ASPEN_OPT_{:04d}".format(index + 1)
        case["SF"] = round(max(0.2, float(case["SF"])), 4)
        case["RR1"] = round(max(0.05, float(case["RR1"])), 4)
        case["RR2"] = round(max(0.05, float(case["RR2"])), 4)
        case["makeup_flow"] = round(max(0.0, float(case.get("makeup_flow") or 0.0)), 6)
        case["D1"] = round(max(0.01, min(feed_total(spec) * 0.995, float(case["D1"]))), 6)
        case["D2"] = round(max(0.01, min(feed_total(spec) * 0.995, float(case["D2"]))), 6)
        cases.append(normalized_refinement_case(spec, case, bounds=bounds))
    return cases


def clamp_int(value, low, high):
    return max(low, min(high, int(round(float(value)))))


def b1_sections(nstage, solvent_stage, feed_stage):
    solvent_stage = clamp_int(solvent_stage, 2, max(2, nstage - 2))
    feed_stage = clamp_int(feed_stage, solvent_stage + 1, max(solvent_stage + 1, nstage - 1))
    return [
        [1, 2, max(2, solvent_stage - 1)],
        [2, solvent_stage, max(solvent_stage, feed_stage - 1)],
        [3, feed_stage, nstage - 1],
    ]


def apply_case(spec, case):
    spec = copy.deepcopy(spec)
    total = feed_total(spec)
    sid = solvent_id(spec)
    solvent_flow = max(0.01, float(case.get("SF") or 3.0) * total)
    recycle_control_mode = str(case.get("recycle_control_mode") or spec.get("recycle_control_mode") or "").strip()
    if not recycle_control_mode:
        recycle_control_mode = "calculator_inventory_control" if spec.get("calculator") else "fixed_fresh_makeup_diagnostic"
    streams = spec.get("streams", {})
    has_recycle_mixer = any(
        block.get("id") == "B3" and "S3" in (block.get("in") or []) and "C2" in (block.get("out") or [])
        for block in spec.get("blocks", [])
    )
    if has_recycle_mixer and "C2" in streams:
        streams["C2"]["mole_flow"] = {sid: solvent_flow}
        if "C1" in streams:
            if recycle_control_mode == "calculator_inventory_control":
                streams["C1"]["mole_flow"] = {sid: solvent_flow}
            else:
                streams["C1"]["mole_flow"] = {sid: max(0.0, float(case.get("makeup_flow") or total * 0.02))}
    else:
        for stream_id in ("C1", "C2"):
            if stream_id in streams:
                streams[stream_id]["mole_flow"] = {sid: solvent_flow}
    if recycle_control_mode in {"calculator_inventory_control", "bounded_nonnegative_makeup_control"}:
        spec["calculator"] = {
            "id": (spec.get("calculator") or {}).get("id") or "C-1",
            "solvent_component": sid,
            "target_flow": solvent_flow,
            "recycle_stream": "S3",
            "makeup_stream": "C1",
        }
        if recycle_control_mode == "bounded_nonnegative_makeup_control":
            spec["calculator"]["formula_mode"] = "bounded_nonnegative_makeup"
        spec["process_topology_mode"] = "closed_loop_calculator_inventory_recycle"
    else:
        spec.pop("calculator", None)
        if has_recycle_mixer:
            spec["process_topology_mode"] = "closed_loop_fixed_makeup_recycle"
    spec["recycle_control_mode"] = recycle_control_mode
    b1 = spec["radfrac"]["B1"]
    b2 = spec["radfrac"]["B2"]
    for key, target in (("RR1", b1), ("D1", b1), ("RR2", b2), ("D2", b2)):
        if key.startswith("RR"):
            target["reflux_ratio"] = float(case[key])
        else:
            target["distillate_mole_flow"] = float(case[key])
    b1["nstage"] = clamp_int(case["N1"], ASPEN_B1_NSTAGE_BOUNDS[0], ASPEN_B1_NSTAGE_BOUNDS[1])
    ns1 = clamp_int(case.get("NS1", max(2, b1["nstage"] * 0.42)), 2, max(2, b1["nstage"] - 2))
    nf1 = clamp_int(case.get("NF1", max(ns1 + 1, b1["nstage"] * 0.70)), ns1 + 1, max(ns1 + 1, b1["nstage"] - 1))
    current_b1_feeds = b1.get("feeds") or {}
    solvent_feed_stream = "C2" if "C2" in current_b1_feeds else "C1"
    b1["feeds"] = {solvent_feed_stream: ns1, "S2": nf1}
    b1["products"]["W"][0] = b1["nstage"]
    b1["internal_sections"] = b1_sections(b1["nstage"], ns1, nf1)
    b2["nstage"] = clamp_int(case["N2"], ASPEN_B2_NSTAGE_BOUNDS[0], ASPEN_B2_NSTAGE_BOUNDS[1])
    nf2 = clamp_int(case.get("NF2", 2), 2, max(2, b2["nstage"] - 1))
    b2["feeds"] = {"W": nf2}
    b2["products"]["S3"][0] = b2["nstage"]
    b2["internal_sections"] = [[1, 2, b2["nstage"] - 1]]
    max_iterations = min(200, max(25, int(case.get("max_iterations") or 200)))
    spec["mixer_max_iterations"] = max(200, int(spec.get("mixer_max_iterations") or 200))
    b2_original_max = int(b2.get("max_iterations") or max_iterations)
    b1["max_iterations"] = max_iterations
    b2["max_iterations"] = min(max_iterations, b2_original_max)
    condenser = case.get("condenser_mode") or "PARTIAL-V"
    phase = case.get("distillate_phase") or ("L" if condenser == "TOTAL" else "V")
    b1["condenser"] = condenser
    b2["condenser"] = condenser
    b1["products"]["D"][1] = phase
    b2["products"]["S1"][1] = phase
    spec["validation_case"] = annotate_applied_stage_values(dict(case), spec)
    spec["validation_case"]["recycle_control_mode"] = recycle_control_mode
    return spec


def fast_solvent_component_ids(spec, case):
    """Solvent component ids that may need clearing in the persistent session."""
    ids = []
    for value in (
        case.get("aspen_component_id"),
        solvent_id(spec),
        ((spec.get("calculator") or {}).get("solvent_component")),
    ):
        token = str(value or "").strip()
        if token and token not in ids:
            ids.append(token)
    for component in spec.get("components") or []:
        if not isinstance(component, dict):
            continue
        role = str(component.get("role") or "").lower()
        token = str(component.get("id") or component.get("aspen_component_id") or "").strip()
        if token and ("solvent" in role or token in ids) and token not in ids:
            ids.append(token)
    for stream_id in ("C1", "C2"):
        stream = (spec.get("streams") or {}).get(stream_id) or {}
        for token in (stream.get("mole_flow") or {}).keys():
            token = str(token or "").strip()
            if token and token not in ids:
                ids.append(token)
    return ids


def fast_stream_component_flow_path(stream_id, component_id):
    return r"\Data\Streams\{}\Input\FLOW\MIXED\{}".format(stream_id, component_id)


def fast_find_node(app, path):
    if not path:
        return None
    try:
        return app.Tree.FindNode(str(path))
    except Exception:
        return None


def fast_set_node_value(app, path, value, label, required=True):
    node = fast_find_node(app, path)
    if node is None:
        if required:
            raise RuntimeError("Aspen Tree path for '{}' was not found: {}".format(label, path))
        return False
    node.Value = value
    return True


def fast_reinitialize_app(app):
    engine = None
    try:
        engine = app.Engine
    except Exception:
        engine = None
    attempts = []
    for target_label, target in (("Engine", engine), ("Application", app)):
        if target is None:
            continue
        for method_name, args in (
            ("Reinit", ()),
            ("Reinit", (0,)),
            ("Reset", ()),
        ):
            method = getattr(target, method_name, None)
            if method is None:
                continue
            try:
                method(*args)
                return "{}.{}{}".format(
                    target_label,
                    method_name,
                    "({})".format(",".join(str(arg) for arg in args)) if args else "()",
                )
            except Exception as exc:
                attempts.append("{}:{}".format(method_name, exc.__class__.__name__))
    return "not_available:{}".format(",".join(attempts[:3])) if attempts else "not_available"


def fast_run_aspen_simulation(app, timeout_seconds=0):
    timeout_seconds = max(0, int(timeout_seconds or 0))
    engine = None
    try:
        engine = app.Engine
    except Exception:
        engine = None
    if engine is not None:
        try:
            engine.Run2()
            method = "Engine.Run2"
        except Exception:
            app.Run2()
            method = "Application.Run2"
    else:
        app.Run2()
        method = "Application.Run2"
    start = time.time()
    while engine is not None:
        try:
            running = bool(engine.IsRunning)
        except Exception:
            break
        if not running:
            break
        if timeout_seconds and time.time() - start > timeout_seconds:
            raise TimeoutError("Aspen Run2 exceeded fast in-place timeout of {} seconds.".format(timeout_seconds))
        time.sleep(2)
    return method


def fast_write_hydraulic_sections(app, case_spec):
    try:
        import variable_map as vm
    except Exception:
        return
    if not getattr(vm, "ENABLE_COLUMN_HYDRAULICS", False):
        return
    for block, path in getattr(vm, "HYDRAULIC_SWITCH_PATHS", {}).items():
        fast_set_node_value(app, path, getattr(vm, "HYDRAULIC_SWITCH_ON_VALUE", "YES"), "{} hydraulic switch".format(block), required=False)
    for block, path in getattr(vm, "HYDRAULIC_REPORT_PATHS", {}).items():
        fast_set_node_value(app, path, getattr(vm, "HYDRAULIC_REPORT_ON_VALUE", "HYDRAULIC"), "{} hydraulic report".format(block), required=False)
    section_paths = getattr(vm, "HYDRAULIC_SECTION_STAGE_PATHS", {})
    radfrac = case_spec.get("radfrac") or {}
    for block, block_spec in radfrac.items():
        paths = section_paths.get(block) or []
        ranges = block_spec.get("internal_sections") or []
        for section_path, section in zip(paths, ranges):
            if len(section) < 3:
                continue
            stage1 = int(section[1])
            stage2 = int(section[2])
            if stage2 < stage1:
                continue
            # Aspen validates each hydraulic section immediately. When reusing
            # one session, writing a larger stage1 before stage2 can create a
            # transient start>end pair against the previous case and raise COM
            # error !CC. Move through a harmless low start first.
            safe_stage1 = min(stage1, 2)
            fast_set_node_value(app, section_path.get("stage1"), safe_stage1, "{} hydraulic safe stage1".format(block), required=False)
            fast_set_node_value(app, section_path.get("stage2"), stage2, "{} hydraulic stage2".format(block), required=False)
            fast_set_node_value(app, section_path.get("stage1"), stage1, "{} hydraulic stage1".format(block), required=False)


def fast_apply_case_to_app(app, spec, case):
    import variable_map as vm

    case_spec = apply_case(spec, case)
    sid = str(case.get("aspen_component_id") or solvent_id(case_spec)).strip()
    if not sid:
        raise RuntimeError("Missing aspen_component_id for fast in-place case.")
    total = zero._aspen_numeric_node(app, getattr(vm, "FEED_TOTAL_FLOW_PATH", "")) or feed_total(case_spec)
    solvent_flow = max(0.0, float(case.get("SF") or 0.0) * float(total))
    calc = case_spec.get("calculator") or {}
    target_flow = safe_float(calc.get("target_flow"), solvent_flow) or solvent_flow
    solvent_ids = fast_solvent_component_ids(case_spec, case)

    recycle_mode = str(case_spec.get("recycle_control_mode") or case.get("recycle_control_mode") or "").strip()
    if recycle_mode == "calculator_inventory_control" and calc:
        fast_set_node_value(app, getattr(vm, "CALCULATOR_RECYCLE_COMPONENT_PATH", ""), sid, "Calculator recycle component")
        fast_set_node_value(app, getattr(vm, "CALCULATOR_MAKEUP_COMPONENT_PATH", ""), sid, "Calculator makeup component")
        formula_template = getattr(vm, "CALCULATOR_FORTRAN_TEMPLATE", "      FLOW={target_flow:.10g}-MOLARCO")
        fast_set_node_value(
            app,
            getattr(vm, "CALCULATOR_FORTRAN_EXEC_PATH", ""),
            formula_template.format(target_flow=float(target_flow)),
            "Calculator target solvent flow formula",
        )
        for solvent in solvent_ids:
            fast_set_node_value(
                app,
                fast_stream_component_flow_path("C1", solvent),
                0.0,
                "C1 stale makeup component {}".format(solvent),
                required=False,
            )
            fast_set_node_value(
                app,
                fast_stream_component_flow_path("C2", solvent),
                0.0,
                "C2 stale recycle component {}".format(solvent),
                required=False,
            )
        fast_set_node_value(app, fast_stream_component_flow_path("C1", sid), 0.0, "C1 active makeup component")
        fast_set_node_value(app, fast_stream_component_flow_path("C2", sid), target_flow, "C2 active recycle component")
    else:
        for stream_id in ("C1", "C2"):
            for solvent in solvent_ids:
                value = solvent_flow if solvent == sid else 0.0
                fast_set_node_value(
                    app,
                    fast_stream_component_flow_path(stream_id, solvent),
                    value,
                    "{} solvent component {}".format(stream_id, solvent),
                    required=(solvent == sid),
                )

    fast_set_node_value(app, getattr(vm, "RR1_PATH", ""), float(case["RR1"]), "RR1")
    fast_set_node_value(app, getattr(vm, "D1_PATH", ""), float(case["D1"]), "D1")
    fast_set_node_value(app, getattr(vm, "RR2_PATH", ""), float(case["RR2"]), "RR2")
    fast_set_node_value(app, getattr(vm, "D2_PATH", ""), float(case["D2"]), "D2")
    optional_inputs = (
        ("N1", "N1_PATH"),
        ("NF1", "NF1_PATH"),
        ("NS1", "NS1_PATH"),
        ("N2", "N2_PATH"),
        ("NF2", "NF2_PATH"),
    )
    applied_case = case_spec.get("validation_case") or case
    for field, path_name in optional_inputs:
        path = getattr(vm, path_name, "")
        if field in applied_case and path:
            fast_set_node_value(app, path, int(round(float(applied_case[field]))), field, required=False)
    fast_write_hydraulic_sections(app, case_spec)
    return case_spec


def run_case_fast_inplace(
    spec,
    case,
    output_dir,
    runner,
    generation=0,
    session_id="generic_fast_inplace",
    run_timeout_seconds=0,
    save_validated_bkp=False,
):
    import variable_map as vm

    case_dir = Path(output_dir) / "case_files" / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    case_spec = apply_case(spec, case)
    spec_path = case_dir / "{}.spec.json".format(case["case_id"])
    spec_path.write_text(safe_json_dumps(case_spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    smoke = {
        "case_id": case.get("case_id"),
        "solvent_name": case.get("solvent_name") or case.get("aspen_component_id") or solvent_id(spec),
        "aspen_component_id": case.get("aspen_component_id") or solvent_id(spec),
        "validation_mode": "fast_inplace_generic_run2",
        "converged": False,
        "pass_or_fail": "FAIL",
        "error_message": "",
        "run_seconds": None,
        "fast_inplace": True,
        "fast_template_bkp": str(Path(runner.template_bkp).resolve()),
        "case_archive_policy": "per_case_validated_bkp" if save_validated_bkp else "lightweight_rows_only",
    }
    started = time.time()
    try:
        app = runner.start_session(session_id=session_id)
        case_spec = fast_apply_case_to_app(app, spec, case)
        smoke["reinitialize_method"] = fast_reinitialize_app(app)
        smoke["run_method"] = fast_run_aspen_simulation(app, timeout_seconds=run_timeout_seconds)
        smoke["run_seconds"] = round(time.time() - started, 3)
        status_value = zero._aspen_node_value(app, getattr(vm, "CONVERGENCE_STATUS_PATH", ""))
        smoke["convergence_status"] = status_value
        if status_value in (None, ""):
            status_converged = True
        else:
            success_values = {str(item).strip().upper() for item in getattr(vm, "CONVERGENCE_SUCCESS_VALUES", ())}
            status_converged = str(status_value).strip().upper() in success_values
        qreb1, qreb1_path = zero._first_numeric_node(app, [
            getattr(vm, "QREB1_PATH", ""),
            r"\Data\Blocks\B1\Output\DUTY",
            r"\Data\Blocks\B1\Output\HEAT_RATE",
        ])
        qreb2, qreb2_path = zero._first_numeric_node(app, [
            getattr(vm, "QREB2_PATH", ""),
            r"\Data\Blocks\B2\Output\DUTY",
            r"\Data\Blocks\B2\Output\HEAT_RATE",
        ])
        smoke["Qreb_1"] = qreb1
        smoke["Qreb_2"] = qreb2
        smoke["Qreb_1_path"] = qreb1_path
        smoke["Qreb_2_path"] = qreb2_path
        smoke["total_reboiler_duty"] = (
            (qreb1 or 0.0) + (qreb2 or 0.0)
            if qreb1 is not None or qreb2 is not None
            else None
        )
        smoke.update(read_generic_aspen_metrics(app, case_spec, runner=runner))
        smoke.update(zero._generic_required_output_probe(app, case_spec))
        if save_validated_bkp:
            report_export = zero.export_aspen_calculation_report(
                app,
                case_dir,
                "{}_aspen_report".format(case.get("case_id") or "generic_aspen_case"),
            )
            smoke.update(report_export)
            smoke = merge_missing_aspen_metrics(
                smoke,
                read_generic_metrics_from_aspen_report(report_export.get("aspen_report_path"), case_spec),
            )
        else:
            smoke["aspen_report_export_skipped"] = "fast_inplace_direct_tree_metrics"
        converged = bool(status_converged and smoke.get("required_outputs_complete"))
        smoke["converged"] = bool(converged)
        smoke["pass_or_fail"] = "PASS" if converged else "FAIL"
        if status_converged and not smoke.get("required_outputs_complete"):
            smoke["error_message"] = (
                "required_outputs_missing: Aspen Run2 returned no hard error, but required duty/stream outputs "
                "were missing or zero; this case is not trusted for optimization."
            )
        session_archive = getattr(runner, "_session_archive", None)
        smoke["fast_session_archive"] = str(Path(session_archive).resolve()) if session_archive else ""
        if save_validated_bkp:
            validated_bkp = case_dir / "{}.fast_inplace.validated.bkp".format(case["case_id"])
            try:
                app.WriteArchive2(str(validated_bkp.resolve()), True)
                smoke["validated_bkp"] = str(validated_bkp.resolve())
                failure_signals = zero.scan_bkp_convergence_failures(validated_bkp)
                if failure_signals:
                    smoke["aspen_convergence_failure_signals"] = failure_signals
                    smoke["converged"] = False
                    smoke["pass_or_fail"] = "FAIL"
                    smoke["error_message"] = "Aspen archive contains convergence failure signals: {}".format(
                        ", ".join(failure_signals)
                    )
            except Exception as exc:
                smoke["archive_write_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
        runner._session_case_count += 1
    except Exception as exc:
        smoke["run_seconds"] = round(time.time() - started, 3)
        smoke["converged"] = False
        smoke["pass_or_fail"] = "FAIL"
        smoke["error_message"] = "{}: {}\n{}".format(exc.__class__.__name__, exc, traceback.format_exc(limit=3))
        try:
            runner.close_session()
        except Exception:
            pass

    smoke["case_wall_seconds"] = round(time.time() - started, 3)
    row = dict(case)
    row.update(smoke)
    row["generation"] = generation
    row["from_cache"] = False
    row["converged"] = bool(smoke.get("converged"))
    row["spec"] = str(spec_path.resolve())
    row["input_deck"] = ""
    row["generated_bkp"] = smoke.get("fast_session_archive") or smoke.get("fast_template_bkp") or ""
    row.update(topology_metadata(case_spec))
    dump_files = aspen_dump_files(case_dir)
    fast_dir = Path(output_dir) / "fast_session_case_files"
    dump_files.extend(path for path in aspen_dump_files(fast_dir) if path not in dump_files)
    if dump_files:
        row = mark_aspen_crash(row, dump_files)
    return finalize_case_row(spec, case_spec, row)


def run_case(spec, case, output_dir, generation=0):
    case_dir = Path(output_dir) / "case_files" / case["case_id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    case_spec = apply_case(spec, case)
    spec_path = case_dir / "{}.spec.json".format(case["case_id"])
    inp_path = case_dir / "{}.inp".format(case["case_id"])
    bkp_path = case_dir / "{}.bkp".format(case["case_id"])
    spec_path.write_text(safe_json_dumps(case_spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    smoke = {}
    case_baseline_aspen = aspen_automation_pids()
    try:
        zero.write_input_deck(case_spec, inp_path)
        zero.build_archive_from_input(inp_path, bkp_path, run_seed_case=False)
        cleanup_new_aspen_processes(case_baseline_aspen, settle_seconds=8)
        smoke = zero.run_generic_smoke_validation(
            bkp_path,
            case_spec,
            case_dir,
            metric_reader=read_generic_aspen_metrics,
        )
        smoke = merge_missing_aspen_metrics(
            smoke,
            read_generic_metrics_from_aspen_report(smoke.get("aspen_report_path"), case_spec),
        )
        if truthy(smoke.get("converged")) and row_target_purity_value(smoke, case_spec) is None:
            metrics_archive = smoke.get("validated_bkp") or bkp_path
            smoke = merge_missing_aspen_metrics(smoke, read_generic_metrics_from_archive(metrics_archive, case_spec))
        cleanup_new_aspen_processes(case_baseline_aspen, settle_seconds=8)
    except Exception as exc:
        import traceback
        smoke = {
            "case_id": case.get("case_id"),
            "solvent_name": case.get("solvent_name") or solvent_id(spec),
            "aspen_component_id": case.get("aspen_component_id") or solvent_id(spec),
            "validation_mode": "generic_aspen_run2",
            "converged": False,
            "pass_or_fail": "FAIL",
            "error_message": "{}: {}\n{}".format(exc.__class__.__name__, exc, traceback.format_exc(limit=3)),
            "run_seconds": None,
        }
    finally:
        cleanup_new_aspen_processes(case_baseline_aspen, settle_seconds=8)
    row = dict(case)
    row.update(smoke)
    row["generation"] = generation
    row["from_cache"] = False
    row["converged"] = bool(smoke.get("converged"))
    row["spec"] = str(spec_path.resolve())
    row["input_deck"] = str(inp_path.resolve())
    row["generated_bkp"] = str(bkp_path.resolve())
    row.update(topology_metadata(case_spec))
    dumps = aspen_dump_files(case_dir)
    if dumps:
        row = mark_aspen_crash(row, dumps)
    return finalize_case_row(spec, case_spec, row)


def timeout_case_row(spec, case, output_dir, timeout_seconds, generation=0, reason="case timeout"):
    case_dir = Path(output_dir) / "case_files" / case["case_id"]
    row = dict(case)
    row.update({
        "generation": generation,
        "from_cache": False,
        "converged": False,
        "feasible": False,
        "pass_or_fail": "FAIL",
        "constraint_violation": 1,
        "TAC": None,
        "decision_score": None,
        "timed_out": True,
        "timeout_seconds": timeout_seconds,
        "error_message": reason,
        "spec": str((case_dir / "{}.spec.json".format(case["case_id"])).resolve()),
        "input_deck": str((case_dir / "{}.inp".format(case["case_id"])).resolve()),
        "generated_bkp": str((case_dir / "{}.bkp".format(case["case_id"])).resolve()),
    })
    row.update(topology_metadata(apply_case(spec, case)))
    return attach_case_objectives(spec, row)


def crash_case_row(spec, case, output_dir, dump_files, generation=0):
    row = timeout_case_row(
        spec,
        case,
        output_dir,
        timeout_seconds="",
        generation=generation,
        reason="Aspen Plus generated a fatal-error dump and the Agent stopped this case.",
    )
    row["timed_out"] = False
    return mark_aspen_crash(row, dump_files)


def run_case_worker(queue, spec, case, output_dir, generation):
    try:
        queue.put(run_case(spec, case, output_dir, generation=generation))
    except Exception as exc:
        import traceback
        row = timeout_case_row(
            spec,
            case,
            output_dir,
            timeout_seconds="",
            generation=generation,
            reason="{}: {}\n{}".format(exc.__class__.__name__, exc, traceback.format_exc(limit=3)),
        )
        row["timed_out"] = False
        queue.put(row)


def run_case_with_timeout(spec, case, output_dir, generation=0, timeout_seconds=0):
    timeout_seconds = int(timeout_seconds or 0)
    if timeout_seconds <= 0:
        return run_case(spec, case, output_dir, generation=generation)
    baseline_aspen = aspen_automation_pids()
    case_dir = Path(output_dir) / "case_files" / case["case_id"]
    baseline_dumps = set(aspen_dump_files(case_dir))
    queue = mp.Queue()
    process = mp.Process(target=run_case_worker, args=(queue, spec, case, str(output_dir), generation))
    process.start()
    started = time.time()
    dump_files = []
    result_row = None
    while process.is_alive() and time.time() - started < timeout_seconds:
        process.join(2)
        if result_row is None:
            try:
                result_row = queue.get_nowait()
                break
            except queue_mod.Empty:
                pass
            except Exception:
                pass
        current_dumps = [path for path in aspen_dump_files(case_dir) if path not in baseline_dumps]
        if current_dumps:
            dump_files = current_dumps
            break
    if result_row is not None:
        process.join(10)
        if process.is_alive():
            try:
                process.terminate()
            except Exception:
                pass
            process.join(5)
        stop_processes(aspen_automation_pids() - baseline_aspen)
        return result_row
    if dump_files and process.is_alive():
        try:
            process.terminate()
        except Exception:
            pass
        process.join(10)
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
            process.join(5)
        stop_processes(aspen_automation_pids() - baseline_aspen)
        return crash_case_row(spec, case, output_dir, dump_files, generation=generation)
    if process.is_alive():
        try:
            process.terminate()
        except Exception:
            pass
        process.join(10)
        if process.is_alive():
            try:
                process.kill()
            except Exception:
                pass
            process.join(5)
        stop_processes(aspen_automation_pids() - baseline_aspen)
        return timeout_case_row(
            spec,
            case,
            output_dir,
            timeout_seconds,
            generation=generation,
            reason="Aspen case exceeded per-case timeout and was stopped by the Agent.",
        )
    try:
        row = queue.get_nowait()
        stop_processes(aspen_automation_pids() - baseline_aspen)
        return row
    except queue_mod.Empty:
        pass
    except Exception:
        pass
    stop_processes(aspen_automation_pids() - baseline_aspen)
    return timeout_case_row(
        spec,
        case,
        output_dir,
        timeout_seconds,
        generation=generation,
        reason="Aspen case worker exited without returning a checkpoint row.",
    )


def seed_row_from_zero_report(seed_report_path, spec):
    if not seed_report_path:
        return None
    path = Path(seed_report_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not topology_metadata(spec).get("closed_loop_recycle"):
        return None
    seed_spec = {}
    if payload.get("spec"):
        seed_spec_path = Path(payload.get("spec"))
        if seed_spec_path.exists():
            seed_spec = zero.load_spec(seed_spec_path)
    if not topology_metadata(seed_spec).get("closed_loop_recycle"):
        return None
    smoke = payload.get("smoke_result") or {}
    if str(smoke.get("pass_or_fail") or "").upper() != "PASS":
        return None
    case = base_case(spec)
    case["case_id"] = "GENERIC_ASPEN_OPT_0001"
    case["source"] = "zero_build_pass_seed_checkpoint"
    row = dict(case)
    source_case_id = smoke.get("case_id")
    row.update(smoke)
    row["case_id"] = case["case_id"]
    row["source_aspen_case_id"] = source_case_id or ""
    row["generation"] = 0
    row["from_cache"] = True
    row["converged"] = bool(smoke.get("converged"))
    row["spec"] = str(Path(payload.get("spec") or "").resolve()) if payload.get("spec") else ""
    row["input_deck"] = str(Path(payload.get("input_deck") or "").resolve()) if payload.get("input_deck") else ""
    row["generated_bkp"] = str(Path(payload.get("generated_bkp") or "").resolve()) if payload.get("generated_bkp") else ""
    row["validated_bkp"] = smoke.get("validated_bkp") or row.get("validated_bkp", "")
    row.update(topology_metadata(seed_spec or spec))
    return finalize_case_row(spec, seed_spec or spec, row)


def truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "pass"}


def recycle_control_mode_from_row(row):
    mode = str(row.get("recycle_control_mode") or "").strip()
    if mode:
        return mode
    source = str(row.get("source") or "").lower()
    if "bounded" in source:
        return "bounded_nonnegative_makeup_control"
    if "fixed_makeup" in source:
        return "fixed_fresh_makeup_diagnostic"
    if truthy(row.get("has_recycle_calculator")):
        return "calculator_inventory_control"
    return "unknown_or_fixed_makeup"


def row_is_exact_inventory_control(row):
    return recycle_control_mode_from_row(row) == "calculator_inventory_control"


def row_is_search_parent_control_family(row):
    row = row or {}
    mode = recycle_control_mode_from_row(row)
    if mode not in SEARCH_PARENT_RECYCLE_CONTROL_MODES:
        return False
    return (
        truthy(row.get("closed_loop_recycle"))
        or truthy(row.get("has_recycle_mixer"))
        or mode in {"calculator_inventory_control", "bounded_nonnegative_makeup_control"}
    )


def row_is_closed_loop_control_family_pass(row):
    return row_is_search_parent_control_family(row) and row_aspen_run_pass(row)


def control_family_rank(row):
    mode = recycle_control_mode_from_row(row)
    try:
        return SEARCH_PARENT_RECYCLE_CONTROL_MODES.index(mode)
    except ValueError:
        return len(SEARCH_PARENT_RECYCLE_CONTROL_MODES)


def row_aspen_run_pass(row):
    row = row or {}
    for key in ("aspen_run_pass_or_fail", "aspen_status", "run_status", "pass_or_fail"):
        value = str(row.get(key) or "").strip().upper()
        if value:
            return value == "PASS"
    return False


def row_is_closed_loop_pass(row):
    return (
        truthy(row.get("closed_loop_recycle"))
        and row_is_exact_inventory_control(row)
        and row_aspen_run_pass(row)
    )


def row_has_required_outputs_missing(row):
    required_outputs_complete = str(row.get("required_outputs_complete")).strip().lower()
    return (
        required_outputs_complete in {"false", "0", "no"}
        and "required_outputs_missing" in str(row.get("error_message") or "")
    )


def multi_objective_candidate_rows(rows):
    candidates = []
    for row in rows or []:
        if not row_is_formal_multi_objective_feasible(row):
            continue
        if all(safe_float(row.get(name)) is not None for name in MULTI_OBJECTIVE_NAMES):
            candidates.append(row)
    return candidates


def objective_vector(row):
    return tuple(safe_float(row.get(name), math.inf) for name in MULTI_OBJECTIVE_NAMES)


def dominates(left, right):
    left_values = objective_vector(left)
    right_values = objective_vector(right)
    return all(a <= b for a, b in zip(left_values, right_values)) and any(a < b for a, b in zip(left_values, right_values))


def nondominated_front(rows):
    front = []
    for candidate in rows:
        if not any(dominates(other, candidate) for other in rows if other is not candidate):
            front.append(candidate)
    return front


def assign_pareto_fronts(rows):
    rows = list(rows or [])
    candidates = list(multi_objective_candidate_rows(rows))
    candidate_ids = {id(row) for row in candidates}
    for row in rows or []:
        row["multi_objective_feasible"] = id(row) in candidate_ids
        row["pareto_front"] = False
        row["pareto_rank"] = ""
        row["topsis_score"] = ""
        row["topsis_rank"] = ""
    if not candidates:
        return

    dominated_by = [0 for _ in candidates]
    dominates_list = [[] for _ in candidates]
    first_front = []
    for i, left in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            right = candidates[j]
            if dominates(left, right):
                dominates_list[i].append(j)
                dominated_by[j] += 1
            elif dominates(right, left):
                dominates_list[j].append(i)
                dominated_by[i] += 1
        if dominated_by[i] == 0:
            first_front.append(i)

    fronts = [first_front]
    rank = 1
    while fronts and fronts[-1]:
        next_front = []
        for i in fronts[-1]:
            row = candidates[i]
            row["pareto_rank"] = rank
            row["pareto_front"] = rank == 1
            for j in dominates_list[i]:
                dominated_by[j] -= 1
                if dominated_by[j] == 0:
                    next_front.append(j)
        if next_front:
            fronts.append(next_front)
        else:
            break
        rank += 1


def assign_topsis_scores(rows):
    candidates = multi_objective_candidate_rows(rows)
    if not candidates:
        return
    weighted = {}
    for name in MULTI_OBJECTIVE_NAMES:
        values = [safe_float(row.get(name), 0.0) for row in candidates]
        denom = math.sqrt(sum(value * value for value in values)) or 1.0
        weight = MULTI_OBJECTIVE_WEIGHTS.get(name, 1.0 / len(MULTI_OBJECTIVE_NAMES))
        for row, value in zip(candidates, values):
            weighted.setdefault(row.get("case_id"), {})[name] = weight * value / denom
    ideal = {
        name: min(weighted[row.get("case_id")][name] for row in candidates)
        for name in MULTI_OBJECTIVE_NAMES
    }
    nadir = {
        name: max(weighted[row.get("case_id")][name] for row in candidates)
        for name in MULTI_OBJECTIVE_NAMES
    }
    scored = []
    for row in candidates:
        cid = row.get("case_id")
        dist_ideal = math.sqrt(sum((weighted[cid][name] - ideal[name]) ** 2 for name in MULTI_OBJECTIVE_NAMES))
        dist_nadir = math.sqrt(sum((weighted[cid][name] - nadir[name]) ** 2 for name in MULTI_OBJECTIVE_NAMES))
        score = dist_nadir / (dist_ideal + dist_nadir) if (dist_ideal + dist_nadir) else 1.0
        row["topsis_score"] = round(score, 8)
        scored.append(row)
    for index, row in enumerate(
        sorted(
            scored,
            key=lambda item: (
                safe_float(item.get("topsis_score"), -math.inf),
                -safe_float(item.get("pareto_rank"), math.inf),
                -safe_float(item.get("objective_TAC"), math.inf),
            ),
            reverse=True,
        ),
        start=1,
    ):
        row["topsis_rank"] = index


def evaluate_multi_objective_rows(rows):
    rows = list(rows or [])
    assign_pareto_fronts(rows)
    assign_topsis_scores(rows)
    candidates = multi_objective_candidate_rows(rows)
    pareto_rows = [
        row for row in candidates
        if truthy(row.get("pareto_front"))
    ]
    ranked_rows = [
        row for row in candidates
        if safe_float(row.get("topsis_score")) is not None
    ]
    best_topsis = min(
        ranked_rows,
        key=lambda row: int(safe_float(row.get("topsis_rank"), 10**9)),
        default={},
    )
    max_generation = max([int(safe_float(row.get("generation"), 0) or 0) for row in rows] + [0])
    complete = bool(len(candidates) >= 2 and pareto_rows and best_topsis and max_generation >= 1)
    return {
        "status": "complete" if complete else ("needs_more_feasible_points" if candidates else "no_feasible_closed_loop_points"),
        "objective_names": list(MULTI_OBJECTIVE_NAMES),
        "objective_sense": {name: "minimize" for name in MULTI_OBJECTIVE_NAMES},
        "objective_weights": MULTI_OBJECTIVE_WEIGHTS,
        "case_count": len(rows),
        "feasible_case_count": len(candidates),
        "pareto_front_case_count": len(pareto_rows),
        "max_generation": max_generation,
        "topsis_best_case_id": best_topsis.get("case_id", ""),
        "topsis_best_score": best_topsis.get("topsis_score", ""),
        "topsis_best_carbon_emission": best_topsis.get("carbon_emission_ton_per_year", ""),
        "topsis_best_TAC": best_topsis.get("TAC", ""),
        "topsis_best_recovery_loss": best_topsis.get("objective_recovery_loss", ""),
        "topsis_best_recovery": best_topsis.get("target_recovery", ""),
        "topsis_best_solvent_flow_kmol_hr": best_topsis.get("solvent_flow_kmol_hr", ""),
        "topsis_best_column_stage_total": best_topsis.get("column_stage_total", ""),
    }


def multi_objective_convergence_snapshot(rows, generation=None):
    multi_objective = evaluate_multi_objective_rows(rows)
    candidates = multi_objective_candidate_rows(rows)
    best_purity_row = best_purity_metric_row(rows, trusted_only=True)
    best_purity = row_target_purity_value(best_purity_row) if best_purity_row else None
    target_purity = safe_float(best_purity_row.get("target_purity_target")) if best_purity_row else None
    best_objectives = {}
    for name in MULTI_OBJECTIVE_NAMES:
        values = [
            safe_float(row.get(name))
            for row in candidates
            if safe_float(row.get(name)) is not None
        ]
        best_objectives[name] = min(values) if values else ""
    pareto_rows = [
        row for row in candidates
        if truthy(row.get("pareto_front"))
    ]
    signature = []
    for row in pareto_rows:
        signature.append(tuple(
            round(safe_float(row.get(name), math.inf), 6)
            for name in MULTI_OBJECTIVE_NAMES
        ))
    return {
        "generation": int(generation if generation is not None else multi_objective.get("max_generation") or 0),
        "case_count": len(rows or []),
        "status": multi_objective.get("status", ""),
        "feasible_case_count": multi_objective.get("feasible_case_count", 0),
        "pareto_front_case_count": multi_objective.get("pareto_front_case_count", 0),
        "topsis_best_case_id": multi_objective.get("topsis_best_case_id", ""),
        "topsis_best_score": multi_objective.get("topsis_best_score", ""),
        "measured_case_count": len(professional_metric_rows(rows, trusted_only=True)),
        "best_purity_case_id": best_purity_row.get("case_id", "") if best_purity_row else "",
        "best_purity": best_purity if best_purity is not None else "",
        "target_purity": target_purity if target_purity is not None else "",
        "target_purity_basis": best_purity_row.get("target_purity_basis", "") if best_purity_row else "",
        "best_objectives": best_objectives,
        "pareto_signature": sorted(signature),
    }


def convergence_snapshot_improved(previous, current, score_epsilon, objective_rel_epsilon, objective_abs_epsilon):
    if not previous:
        return True
    if str(previous.get("status") or "") != "complete" and str(current.get("status") or "") == "complete":
        return True
    previous_feasible = int(previous.get("feasible_case_count") or 0)
    current_feasible = int(current.get("feasible_case_count") or 0)
    if previous_feasible <= 0 < current_feasible:
        return True
    previous_purity = safe_float(previous.get("best_purity"))
    current_purity = safe_float(current.get("best_purity"))
    if current_purity is not None and previous_purity is None:
        return True
    if previous_purity is not None and current_purity is not None:
        threshold = max(
            float(objective_abs_epsilon or 0.0),
            abs(previous_purity) * float(objective_rel_epsilon or 0.0),
        )
        if current_purity > previous_purity + threshold:
            return True
    previous_score = safe_float(previous.get("topsis_best_score"))
    current_score = safe_float(current.get("topsis_best_score"))
    if previous_score is not None and current_score is not None and current_score > previous_score + float(score_epsilon or 0.0):
        return True
    previous_objectives = previous.get("best_objectives") or {}
    current_objectives = current.get("best_objectives") or {}
    for name in MULTI_OBJECTIVE_NAMES:
        previous_value = safe_float(previous_objectives.get(name))
        current_value = safe_float(current_objectives.get(name))
        if previous_value is None or current_value is None:
            continue
        threshold = max(
            float(objective_abs_epsilon or 0.0),
            abs(previous_value) * float(objective_rel_epsilon or 0.0),
        )
        if current_value < previous_value - threshold:
            return True
    return False


def convergence_summary_payload(
    history,
    best_snapshot,
    stagnant_generations,
    stop_reason,
    min_generations,
    patience,
    score_epsilon,
    objective_rel_epsilon,
    objective_abs_epsilon,
):
    last_snapshot = history[-1] if history else {}
    converged = str(stop_reason or "") == "converged"
    if not converged and history:
        converged = (
            str(last_snapshot.get("status") or "") == "complete"
            and int(last_snapshot.get("generation") or 0) >= int(min_generations or 0)
            and int(stagnant_generations or 0) >= int(patience or 0)
        )
    return {
        "status": "converged" if converged else (stop_reason or "running_or_max_generation_reached"),
        "converged": bool(converged),
        "stop_reason": stop_reason or "",
        "min_generations": int(min_generations or 0),
        "patience": int(patience or 0),
        "stagnant_generations": int(stagnant_generations or 0),
        "score_epsilon": float(score_epsilon or 0.0),
        "objective_rel_epsilon": float(objective_rel_epsilon or 0.0),
        "objective_abs_epsilon": float(objective_abs_epsilon or 0.0),
        "last_snapshot": last_snapshot,
        "best_snapshot": best_snapshot or {},
        "history": history or [],
    }


def convergence_history_matches_current_objectives(history):
    expected = set(MULTI_OBJECTIVE_NAMES)
    expected_count = len(MULTI_OBJECTIVE_NAMES)
    for item in history or []:
        if not isinstance(item, dict):
            continue
        objectives = item.get("best_objectives") or {}
        if objectives and not expected.issubset(set(objectives)):
            return False
        signature = item.get("pareto_signature") or []
        for point in signature[:5]:
            if isinstance(point, (list, tuple)) and len(point) != expected_count:
                return False
    return True


def next_case_id(index):
    return "GENERIC_ASPEN_OPT_{:04d}".format(max(1, int(index or 1)))


def row_case_index(row):
    text = str((row or {}).get("case_id") or "")
    digits = "".join(ch for ch in text.rsplit("_", 1)[-1] if ch.isdigit())
    try:
        return int(digits)
    except Exception:
        return 0


def max_case_index(rows):
    return max([row_case_index(row) for row in rows or []] + [0])


def row_generation(row):
    try:
        return int(float((row or {}).get("generation") or 0))
    except Exception:
        return 0


def max_generation_index(rows):
    return max([row_generation(row) for row in rows or []] + [0])


def row_has_professional_metrics(row):
    return (
        row_target_purity_value(row) is not None
        or safe_float((row or {}).get("target_recovery")) is not None
        or safe_float((row or {}).get("objective_TAC")) is not None
    )


def row_has_physically_valid_purity_recovery(row, tolerance=1.0e-6):
    purity = row_target_purity_value(row)
    recovery = safe_float((row or {}).get("target_recovery"))
    return bool(
        purity is not None
        and recovery is not None
        and -tolerance <= purity <= 1.0 + tolerance
        and -tolerance <= recovery <= 1.0 + tolerance
    )


def row_has_trusted_professional_metrics(row):
    row = row or {}
    return (
        row_has_professional_metrics(row)
        and row_has_physically_valid_purity_recovery(row)
        and row_is_closed_loop_control_family_pass(row)
        and truthy(row.get("converged"))
        and truthy(row.get("required_outputs_complete"))
    )


def row_is_formal_multi_objective_feasible(row):
    return (
        row_has_trusted_professional_metrics(row)
        and row_is_closed_loop_pass(row)
        and safe_float((row or {}).get("constraint_violation"), 1.0) <= 1.0e-12
        and all(safe_float((row or {}).get(name)) is not None for name in MULTI_OBJECTIVE_NAMES)
    )


def optimizer_report_payload(path):
    if not path:
        return {}
    report_path = Path(path)
    if not report_path.exists():
        return {}
    return safe_json_loads(report_path.read_text(encoding="utf-8", errors="replace"), default={}) or {}


def optimizer_case_event_rows(path, payload=None):
    if not path:
        return []
    report_path = Path(path)
    payload = payload if isinstance(payload, dict) else optimizer_report_payload(path)
    candidates = []
    event_path = str((payload or {}).get("case_events_jsonl") or "").strip()
    if event_path:
        candidates.append(Path(event_path))
    if report_path.exists():
        candidates.extend((report_path.parent / "optimization").glob("*.case_events.jsonl"))
    seen = set()
    rows = []
    for candidate in candidates:
        try:
            candidate = Path(candidate)
        except Exception:
            continue
        key = str(candidate)
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"case_completed"' not in line and '"case_backfilled"' not in line:
                        continue
                    event = safe_json_loads(line, default=None)
                    if not isinstance(event, dict):
                        continue
                    row = event.get("row") if isinstance(event, dict) else None
                    if isinstance(row, dict):
                        rows.append(dict(row))
        except Exception:
            continue
    return rows


def optimizer_report_rows(path):
    data = optimizer_report_payload(path)
    if not data:
        return []
    rows = data.get("rows") or []
    if rows:
        return [dict(row) for row in rows if isinstance(row, dict)]
    return optimizer_case_event_rows(path, payload=data)


def optimizer_resume_rows(path):
    rows = optimizer_report_rows(path)
    deduped = {}
    order = []
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            case_id = "__row_{}".format(len(order) + 1)
        if case_id not in deduped:
            order.append(case_id)
        deduped[case_id] = row
    return [deduped[key] for key in order]


RESUME_CONSTRAINT_REFRESH_KEYS = (
    "constraint_violation",
    "feasible",
    "pass_or_fail",
    "purity_pass",
    "target_purity_target",
    "target_recovery_reference",
    "target_recovery_loss",
    "decision_score",
    "error_message",
)


def resume_constraint_state(row):
    return {key: (row or {}).get(key) for key in RESUME_CONSTRAINT_REFRESH_KEYS}


def refresh_resume_row_for_current_spec(spec, row):
    row = restore_row_applied_stage_values_from_spec(row)
    if not row_has_professional_metrics(row):
        return attach_case_objectives(spec, row)
    before = resume_constraint_state(row)
    try:
        case_spec = apply_case(spec, row)
        refreshed = finalize_case_row(spec, case_spec, row)
    except Exception as exc:
        refreshed = attach_case_objectives(spec, row)
        refreshed["resume_constraint_refresh_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
    after = resume_constraint_state(refreshed)
    refreshed["resume_constraints_refreshed"] = True
    refreshed["resume_constraint_refresh_changed"] = bool(before != after)
    return refreshed



def bounds_profile_config(profile):
    profile = str(profile or "publication_safe").strip().lower()
    if profile not in BOUNDS_PROFILES:
        profile = "publication_safe"
    config = dict(BOUNDS_PROFILES[profile])
    config["profile"] = profile
    return config


def distillate_bounds_from_profile(spec, name, profile_bounds):
    total = feed_total(spec)
    d1_guess, d2_guess = split_targets(spec)
    center = d1_guess if name == "D1" else d2_guess
    low, high = profile_bounds
    low = safe_float(low)
    high = safe_float(high)
    if low is None or high is None:
        low, high = 0.01, total * 0.995
    if low > high:
        low, high = high, low
    # Modern profiles express D1/D2 as factors around the material-balance split
    # target. Legacy profiles with high <= 1.0 used feed-total fractions.
    if low >= 0.1 and high <= 3.0:
        low, high = center * low, center * high
    elif high <= 1.0:
        low, high = total * low, total * high
    low = max(0.01, min(total * 0.995, float(low)))
    high = max(0.01, min(total * 0.995, float(high)))
    if low > high:
        low, high = high, low
    return (round(low, 6), round(high, 6))


def stage_bounds_within_aspen_limits(bounds, limits):
    low, high = bounds
    min_allowed, max_allowed = limits
    low = clamp_int(low, min_allowed, max_allowed)
    high = clamp_int(high, min_allowed, max_allowed)
    if low > high:
        low = high
    return (low, high)


def optimizer_bounds_from_args(args, spec):
    config = bounds_profile_config(getattr(args, "bounds_profile", "publication_safe"))
    overrides = {
        "SF": (getattr(args, "min_sf", None), getattr(args, "max_sf", None)),
        "RR1": (getattr(args, "min_rr", None), getattr(args, "max_rr", None)),
        "RR2": (getattr(args, "min_rr", None), getattr(args, "max_rr", None)),
        "N1": (getattr(args, "min_n1", None), getattr(args, "max_n1", None)),
        "N2": (getattr(args, "min_n2", None), getattr(args, "max_n2", None)),
    }
    for name, (low_override, high_override) in overrides.items():
        low, high = config[name]
        if low_override is not None:
            low = low_override
        if high_override is not None:
            high = high_override
        config[name] = (low, high)
    if getattr(args, "nf1_bottom_margin", None) is not None:
        config["NF1_BOTTOM_MARGIN"] = int(args.nf1_bottom_margin)
    if getattr(args, "feed_solvent_stage_gap", None) is not None:
        config["NF1_NS1_MIN_GAP"] = int(args.feed_solvent_stage_gap)
    total = feed_total(spec)
    for name in ("D1", "D2"):
        config[name] = distillate_bounds_from_profile(spec, name, config[name])
    config["N1"] = stage_bounds_within_aspen_limits(config["N1"], ASPEN_B1_NSTAGE_BOUNDS)
    config["N2"] = stage_bounds_within_aspen_limits(config["N2"], ASPEN_B2_NSTAGE_BOUNDS)
    return config


def serializable_bounds(bounds):
    payload = {}
    for key, value in (bounds or {}).items():
        if isinstance(value, tuple):
            payload[key] = list(value)
        else:
            payload[key] = value
    return payload


def professional_metric_rows(rows, trusted_only=False):
    return [
        row for row in rows or []
        if row_has_professional_metrics(row)
        and row_target_purity_value(row) is not None
        and (not trusted_only or row_has_trusted_professional_metrics(row))
    ]


def best_purity_metric_row(rows, trusted_only=False):
    measured = professional_metric_rows(rows, trusted_only=trusted_only)
    if not measured:
        return {}
    return max(measured, key=lambda row: safe_float(row_target_purity_value(row), -math.inf))


def optimizer_result_counts(rows, spec=None):
    rows = list(rows or [])
    target = target_purity_value(spec or {}) if spec else None
    measured = professional_metric_rows(rows)
    trusted_measured = professional_metric_rows(rows, trusted_only=True)
    aspen_pass_rows = [row for row in rows if row_is_closed_loop_pass(row)]
    formal_rows = [row for row in rows if row_is_formal_multi_objective_feasible(row)]
    purity_rows = []
    for row in trusted_measured:
        row_target = safe_float(row.get("target_purity_target"), target)
        purity = row_target_purity_value(row)
        if row_target is not None and purity is not None and purity >= row_target:
            purity_rows.append(row)
    return {
        "case_count": len(rows),
        "measured_case_count": len(measured),
        "trusted_measured_case_count": len(trusted_measured),
        "aspen_run_pass_case_count": len(aspen_pass_rows),
        "closed_loop_pass_case_count": len(aspen_pass_rows),
        "formal_pass_case_count": len(formal_rows),
        "purity_pass_case_count": len(purity_rows),
        "multi_objective_feasible_case_count": len(formal_rows),
    }


def next_feasibility_bounds_profile(profile):
    profile = str(profile or "publication_safe").strip()
    order = ("publication_safe", "purity_feasibility", "aggressive_feasibility")
    if profile not in order:
        return "purity_feasibility"
    index = order.index(profile)
    if index >= len(order) - 1:
        return profile
    return order[index + 1]


def profile_covering_row_bounds(spec, row, current_profile):
    current_profile = str(current_profile or "publication_safe").strip()
    order = ("publication_safe", "purity_feasibility", "aggressive_feasibility")
    if current_profile not in order:
        current_profile = "publication_safe"
    relevant = {"SF", "RR1", "RR2", "N1", "N2", "D1", "D2"}
    recommended = current_profile
    start = order.index(current_profile)
    for profile in order[start + 1:]:
        trial_bounds = optimizer_bounds_from_args(argparse.Namespace(bounds_profile=profile), spec)
        hits = [
            hit for hit in boundary_hits_for_row(row, trial_bounds)
            if hit.get("variable") in relevant
        ]
        recommended = profile
        if not hits:
            break
    return recommended


def best_constraint_metric_row(rows, trusted_only=False):
    measured = professional_metric_rows(rows, trusted_only=trusted_only)
    if not measured:
        return {}
    return min(
        measured,
        key=lambda row: (
            safe_float(row.get("constraint_violation"), math.inf),
            -safe_float(row_target_purity_value(row), -math.inf),
            -safe_float(row.get("target_recovery"), -math.inf),
            row_case_index(row),
        ),
    )


def constraint_feasibility_diagnosis(spec, rows, bounds):
    rows = list(rows or [])
    bounds = dict(bounds or bounds_profile_config("publication_safe"))
    candidates = multi_objective_candidate_rows(rows)
    measured = professional_metric_rows(rows, trusted_only=True)
    target = target_purity_value(spec)
    recovery_target = target_recovery_reference(spec)
    best_purity_row = best_purity_metric_row(rows, trusted_only=True)
    best_constraint_row = best_constraint_metric_row(rows, trusted_only=True)
    best = best_constraint_row or best_purity_row
    best_purity = row_target_purity_value(best_purity_row) if best_purity_row else None
    best_recovery = safe_float(best_purity_row.get("target_recovery")) if best_purity_row else None
    best_constraint_purity = row_target_purity_value(best) if best else None
    best_constraint_recovery = safe_float(best.get("target_recovery")) if best else None
    hits = boundary_hits_for_row(best, bounds) if best else []
    lower_vars = {hit.get("variable") for hit in hits if hit.get("side") == "lower"}
    upper_vars = {hit.get("variable") for hit in hits if hit.get("side") == "upper"}
    purity_met_rows = [
        row for row in measured
        if safe_float(row_target_purity_value(row), -math.inf) >= target
    ]
    hard_constraint_rows = [
        row for row in purity_met_rows
        if recovery_target is None
        or safe_float(row.get("target_recovery"), -math.inf) >= recovery_target
    ]
    should_adjust = False
    recommended_profile = bounds.get("profile", "exploratory")
    recommended_action = "continue_current_search"
    status = "formal_feasible_available" if candidates else "no_formal_feasible_points"
    reason = ""
    if candidates:
        reason = "At least one case satisfies hard purity constraints and all formal objectives."
    elif not measured:
        status = "waiting_for_professional_metrics"
        recommended_action = "collect_real_aspen_metric_cases"
        reason = "No rows with target-product purity/recovery metrics are available yet."
    elif hard_constraint_rows:
        status = "hard_constraints_met_objectives_missing"
        recommended_action = "inspect_missing_professional_objectives"
        reason = "Purity and recovery targets are met, but formal objective fields are still incomplete."
    elif best_purity is not None and best_purity >= target and recovery_target is not None:
        status = "recovery_constraint_not_met"
        reason = "At least one measured case meets purity, but no measured case meets purity and recovery together."
        if lower_vars & {"SF", "RR1", "RR2"} or upper_vars & {"SF", "RR1", "RR2", "N1", "N2", "D1", "D2"}:
            recommended_profile = profile_covering_row_bounds(spec, best, bounds.get("profile"))
            should_adjust = recommended_profile != bounds.get("profile")
            recommended_action = "auto_expand_to_recovery_feasibility_bounds" if should_adjust else "continue_current_search"
    else:
        status = "purity_constraint_not_met"
        reason = "Best measured purity is still below target."
        if bounds.get("profile") == "publication_safe" and (lower_vars & {"SF", "RR1", "RR2"}):
            recommended_profile = profile_covering_row_bounds(spec, best, bounds.get("profile"))
            should_adjust = recommended_profile != bounds.get("profile")
            recommended_action = "auto_expand_to_purity_feasibility_bounds"
            status = "purity_lower_bound_limited"
        elif upper_vars & {"SF", "RR1", "RR2", "N1", "N2", "D1", "D2"}:
            recommended_profile = profile_covering_row_bounds(spec, best, bounds.get("profile"))
            should_adjust = recommended_profile != bounds.get("profile")
            recommended_action = "auto_expand_to_purity_feasibility_bounds"
            status = "purity_upper_bound_limited"
    return {
        "status": status,
        "reason": reason,
        "target_purity": target,
        "target_purity_basis": target_purity_basis(spec),
        "target_recovery": recovery_target if recovery_target is not None else "",
        "measured_case_count": len(measured),
        "formal_feasible_case_count": len(candidates),
        "best_purity_case_id": best_purity_row.get("case_id", "") if best_purity_row else "",
        "best_purity": best_purity if best_purity is not None else "",
        "best_recovery": best_recovery if best_recovery is not None else "",
        "best_TAC": best_purity_row.get("TAC", "") if best_purity_row else "",
        "best_constraint_case_id": best.get("case_id", "") if best else "",
        "best_constraint_purity": best_constraint_purity if best_constraint_purity is not None else "",
        "best_constraint_recovery": best_constraint_recovery if best_constraint_recovery is not None else "",
        "best_constraint_violation": best.get("constraint_violation", "") if best else "",
        "best_variables": {
            key: best.get(key)
            for key in ("SF", "RR1", "D1", "RR2", "D2", "N1", "NF1", "NS1", "N2", "NF2")
        } if best else {},
        "best_boundary_hits": hits,
        "agent_should_adjust_bounds": bool(should_adjust),
        "recommended_bounds_profile": recommended_profile,
        "recommended_action": recommended_action,
    }


def adaptive_constraint_repair_bounds(spec, rows, bounds):
    bounds = dict(bounds or bounds_profile_config("publication_safe"))
    diagnosis = constraint_feasibility_diagnosis(spec, rows, bounds)
    if not diagnosis.get("agent_should_adjust_bounds"):
        return bounds
    recommended = str(diagnosis.get("recommended_bounds_profile") or "").strip()
    if not recommended or recommended == bounds.get("profile"):
        return bounds
    expanded = optimizer_bounds_from_args(argparse.Namespace(bounds_profile=recommended), spec)
    expanded["profile"] = recommended
    return expanded


def normalized_refinement_case(spec, case, bounds=None):
    total = feed_total(spec)
    bounds = dict(bounds or optimizer_bounds_from_args(argparse.Namespace(bounds_profile="publication_safe"), spec))
    normalized = dict(case)
    for key in CASE_RUNTIME_RESULT_KEYS:
        normalized.pop(key, None)
    normalized["bounds_profile"] = bounds.get("profile", "exploratory")
    normalized["SF"] = round(max(bounds["SF"][0], min(bounds["SF"][1], float(normalized.get("SF") or 3.0))), 4)
    normalized["RR1"] = round(max(bounds["RR1"][0], min(bounds["RR1"][1], float(normalized.get("RR1") or 2.0))), 4)
    normalized["RR2"] = round(max(bounds["RR2"][0], min(bounds["RR2"][1], float(normalized.get("RR2") or 0.8))), 4)
    normalized["D1"] = round(max(bounds["D1"][0], min(bounds["D1"][1], float(normalized.get("D1") or total * 0.5))), 6)
    normalized["D2"] = round(max(bounds["D2"][0], min(bounds["D2"][1], float(normalized.get("D2") or total * 0.5))), 6)
    normalized["N1"] = clamp_int(normalized.get("N1") or 45, int(bounds["N1"][0]), int(bounds["N1"][1]))
    normalized["N2"] = clamp_int(normalized.get("N2") or 10, int(bounds["N2"][0]), int(bounds["N2"][1]))
    feed_gap = max(1, int(bounds.get("NF1_NS1_MIN_GAP", 1)))
    bottom_margin = max(1, int(bounds.get("NF1_BOTTOM_MARGIN", 1)))
    normalized["NS1"] = clamp_int(
        normalized.get("NS1") or max(2, normalized["N1"] * 0.42),
        2,
        max(2, normalized["N1"] - feed_gap - bottom_margin),
    )
    normalized["NF1"] = clamp_int(
        normalized.get("NF1") or max(normalized["NS1"] + 1, normalized["N1"] * 0.70),
        normalized["NS1"] + feed_gap,
        max(normalized["NS1"] + feed_gap, normalized["N1"] - bottom_margin),
    )
    normalized["NF2"] = clamp_int(normalized.get("NF2") or 2, 2, max(2, normalized["N2"] - 1))
    normalized["makeup_flow"] = round(max(0.0, float(normalized.get("makeup_flow") or total * 0.02)), 6)
    recycle_control_mode = str(
        normalized.get("recycle_control_mode")
        or spec.get("recycle_control_mode")
        or ""
    ).strip()
    allowed_recycle_control_modes = {
        "calculator_inventory_control",
        "bounded_nonnegative_makeup_control",
        "fixed_fresh_makeup_diagnostic",
    }
    if recycle_control_mode not in allowed_recycle_control_modes:
        recycle_control_mode = "calculator_inventory_control" if spec.get("calculator") else "fixed_fresh_makeup_diagnostic"
    normalized["recycle_control_mode"] = recycle_control_mode
    normalized.setdefault("condenser_mode", "PARTIAL-V")
    normalized.setdefault("distillate_phase", "L" if normalized["condenser_mode"] == "TOTAL" else "V")
    normalized.setdefault("max_iterations", 200)
    return normalized


def case_signature(case):
    return tuple(
        (key, case.get(key))
        for key in (
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
            "condenser_mode",
            "distillate_phase",
            "recycle_control_mode",
        )
    )


def parent_sort_key(row):
    is_pass = row_is_closed_loop_pass(row)
    tac = row.get("TAC")
    try:
        tac_value = float(tac)
    except Exception:
        tac_value = float("inf")
    measured = row_has_trusted_professional_metrics(row)
    purity = safe_float(row_target_purity_value(row), -math.inf)
    recovery = safe_float(row.get("target_recovery"), -math.inf)
    violation = safe_float(row.get("constraint_violation"))
    if violation is None:
        violation = 1.0e12 if measured else 1.0e15
    pareto_rank = safe_float(row.get("pareto_rank"), math.inf)
    topsis_score = safe_float(row.get("topsis_score"), -math.inf)
    return (
        0 if row_is_formal_multi_objective_feasible(row) else 1,
        0 if measured else 1,
        violation,
        -purity,
        -recovery,
        0 if is_pass else 1,
        pareto_rank,
        -topsis_score,
        1 if truthy(row.get("timed_out")) else 0,
        tac_value,
        row_case_index(row),
    )


def search_parent_sort_key(row):
    return (
        0 if row_is_formal_multi_objective_feasible(row) else 1,
        0 if row_has_trusted_professional_metrics(row) else 1,
        0 if row_is_closed_loop_control_family_pass(row) else 1,
        control_family_rank(row),
        parent_sort_key(row),
    )


def selectable_parent_rows(rows, limit):
    eligible_rows = [
        row for row in rows or []
        if row_is_search_parent_control_family(row)
    ]
    if not eligible_rows:
        return []
    measured_rows = [row for row in eligible_rows if row_has_trusted_professional_metrics(row)]
    formal_rows = [row for row in measured_rows if row_is_formal_multi_objective_feasible(row)]
    pass_rows = [row for row in eligible_rows if row_is_closed_loop_control_family_pass(row)]
    source_rows = formal_rows or measured_rows or pass_rows or eligible_rows
    return sorted(source_rows, key=search_parent_sort_key)[: max(1, int(limit or 1))]


def near_feasible_polishing_variants(spec, rows):
    measured = [
        row for row in rows or []
        if row_is_search_parent_control_family(row)
        and row_has_trusted_professional_metrics(row)
    ]
    if not measured:
        return []
    best = sorted(measured, key=search_parent_sort_key)[0]
    purity = row_target_purity_value(best)
    recovery = safe_float(best.get("target_recovery"))
    violation = safe_float(best.get("constraint_violation"), math.inf)
    purity_target = target_purity_value(spec)
    recovery_target = target_recovery_reference(spec)
    purity_gap = max(0.0, float(purity_target) - float(purity)) if purity is not None else math.inf
    recovery_gap = 0.0
    if recovery_target is not None:
        recovery_gap = max(0.0, float(recovery_target) - float(recovery)) if recovery is not None else math.inf
    near_constraint = bool(
        violation <= 5.0
        or (
            purity_gap <= 5.0e-5
            and (recovery_target is None or recovery_gap <= 5.0e-3)
        )
    )
    if not near_constraint:
        return []

    target_split = target_split_for_recovery(spec)
    variants = [
        ("near_feasible_rr_polish", {"RR1": 1.025, "N1": 2}),
        ("near_feasible_rr_solvent_polish", {"SF": 1.015, "RR1": 1.035, "N1": 3}),
        ("near_feasible_rr_companion_polish", {"RR1": 1.040, "_companion_split_factor": 1.015, "N1": 4, "N2": 1}),
        ("near_feasible_purity_rr_deeper", {"RR1": 1.080, "N1": 8, "N2": 2}),
    ]
    if recovery_target is not None:
        variants.extend([
            ("near_feasible_target_split_0001_rr", {"_target_split_absolute": target_split * 1.0001, "RR1": 1.040, "N1": 4}),
            ("near_feasible_target_split_0003_rr_solvent", {"_target_split_absolute": target_split * 1.0003, "SF": 1.020, "RR1": 1.060, "N1": 6, "N2": 2}),
            ("near_feasible_target_split_0006_rr_companion", {"_target_split_absolute": target_split * 1.0006, "_companion_split_factor": 1.020, "RR1": 1.080, "N1": 8, "N2": 2}),
        ])
    if purity_gap > 0.0:
        variants.extend([
            ("near_feasible_purity_split_down_0002", {"_target_split_absolute": target_split * 0.9998, "RR1": 1.080, "N1": 8}),
            ("near_feasible_purity_split_down_0004", {"_target_split_absolute": target_split * 0.9996, "SF": 1.015, "RR1": 1.100, "N1": 10, "N2": 2}),
        ])
    return variants


def closed_loop_metric_probe_case(spec, rows, next_index, bounds=None):
    rows = list(rows or [])
    if professional_metric_rows(rows, trusted_only=True):
        return None
    metric_probe_rows = [
        row for row in rows
        if str(row.get("source") or "").startswith("generic_metric_probe_from_closed_loop_pass")
    ]
    if any(row_has_trusted_professional_metrics(row) for row in metric_probe_rows):
        return None
    if len(metric_probe_rows) >= 3:
        return None
    source_rows = [row for row in rows if row_is_closed_loop_pass(row)]
    if not source_rows:
        return None
    source = sorted(source_rows, key=search_parent_sort_key)[0]
    case = normalized_refinement_case(spec, source, bounds=bounds)
    source_case_id = str(source.get("case_id") or "").strip()
    case["case_id"] = next_case_id(next_index)
    case["source"] = "generic_metric_probe_from_closed_loop_pass_{}".format(source_case_id or "unknown")
    case["metric_probe_source_case_id"] = source_case_id
    case["search_mode"] = "nsga2_hybrid"
    case["optimizer_algorithm"] = "NSGA-II hybrid"
    return case


def refinement_cases(spec, rows, generation, population_size, next_index, bounds=None):
    parents = selectable_parent_rows(rows, max(2, int(population_size or 1)))
    if not parents:
        return []
    total = feed_total(spec)
    existing = {
        case_signature(normalized_refinement_case(spec, row, bounds=bounds))
        for row in rows or []
        if row_is_search_parent_control_family(row)
    }
    cases = []
    local_seen = set()
    metric_probe = closed_loop_metric_probe_case(spec, rows, next_index, bounds=bounds)
    if metric_probe:
        cases.append(metric_probe)
        local_seen.add(case_signature(metric_probe))
    pass_exists = any(row_is_formal_multi_objective_feasible(row) for row in rows or [])
    target_split_key = "D1" if str(target_product_stream(spec) or "").upper() == "D" else "D2"
    companion_split_key = "D2" if target_split_key == "D1" else "D1"
    recovery_target_split = target_split_for_recovery(spec)
    if pass_exists:
        variants = [
            ("lower_solvent", {"SF": 0.80}),
            ("slightly_lower_solvent", {"SF": 0.92}),
            ("higher_solvent_check", {"SF": 1.12}),
            ("lower_rr", {"RR1": 0.85, "RR2": 0.85}),
            ("lower_solvent_lower_rr", {"SF": 0.90, "RR1": 0.90, "RR2": 0.90}),
            ("more_stages_lower_solvent", {"SF": 0.88, "N1": 6, "N2": 2}),
            ("fewer_stages_same_split", {"N1": -4, "N2": -2}),
            ("fewer_stages_more_solvent", {"SF": 1.10, "N1": -6, "N2": -2}),
            ("lower_second_column_bleed", {"D2": max(0.01, split_targets(spec)[1] * 1.25)}),
            ("higher_second_column_bleed", {"D2": min(total * 0.995, max(0.01, split_targets(spec)[1] * 1.65))}),
            ("lower_rr_more_stages", {"RR1": 0.75, "RR2": 0.75, "N1": 8, "N2": 2}),
            ("lower_solvent_higher_rr", {"SF": 0.75, "RR1": 1.20, "RR2": 1.20}),
            ("low_solvent_deeper_recovery", {"SF": 0.70, "D2": min(total * 0.995, max(0.01, split_targets(spec)[1] * 1.65)), "N1": 6}),
            ("high_solvent_low_rr_compact", {"SF": 1.35, "RR1": 0.65, "RR2": 0.65, "N1": -4, "N2": -2}),
        ]
    else:
        variants = near_feasible_polishing_variants(spec, rows) + [
            ("purity_cut_97_high_rr", {"_target_split_factor": 0.97, "RR1": 1.40, "N1": 10}),
            ("purity_cut_94_high_rr", {"_target_split_factor": 0.94, "RR1": 1.70, "N1": 16}),
            ("purity_cut_90_deeper", {"_target_split_factor": 0.90, "RR1": 2.10, "N1": 24}),
            ("purity_cut_85_deepest", {"_target_split_factor": 0.85, "RR1": 2.80, "N1": 32}),
            ("purity_more_stages", {"RR1": 1.55, "N1": 28, "N2": 4}),
            ("purity_low_solvent_more_rr", {"SF": 0.75, "RR1": 2.00, "N1": 20}),
            ("purity_min_solvent_cut", {"SF": 0.60, "_target_split_factor": 0.92, "RR1": 2.40, "N1": 28}),
            ("purity_high_solvent_check", {"SF": 1.30, "RR1": 1.80, "N1": 24}),
            ("purity_companion_up", {"_companion_split_factor": 1.25, "N2": 6}),
            ("purity_low_sf_companion_up", {"SF": 0.75, "_companion_split_factor": 1.40, "N2": 8}),
            ("purity_target_up_companion_up", {"_target_split_factor": 1.02, "_companion_split_factor": 1.30, "SF": 0.85, "N2": 8}),
            ("purity_target_down_companion_up", {"_target_split_factor": 0.98, "_companion_split_factor": 1.45, "SF": 0.85, "N2": 8}),
            ("recovery_target_high_rr", {"_target_split_absolute": recovery_target_split, "_companion_split_factor": 1.35, "RR1": 1.50, "N1": 18, "N2": 4}),
            ("recovery_target_max_rr_deeper", {"_target_split_absolute": recovery_target_split, "_companion_split_factor": 1.45, "RR1": 2.00, "N1": 30, "N2": 6}),
            ("recovery_target_slight_cut_high_rr", {"_target_split_absolute": recovery_target_split * 0.995, "_companion_split_factor": 1.45, "RR1": 2.20, "N1": 34, "N2": 6}),
            ("recovery_target_more_solvent_high_rr", {"_target_split_absolute": recovery_target_split, "_companion_split_factor": 1.55, "SF": 1.25, "RR1": 1.80, "N1": 26, "N2": 8}),
            ("purity_compact_first_deeper_second", {"N1": -6, "N2": 10, "_companion_split_factor": 1.35}),
            ("purity_low_rr_companion_up", {"RR1": 0.85, "RR2": 0.85, "_companion_split_factor": 1.35}),
            ("purity_feasibility_lowest_sf_low_rr", {"SF": 0.55, "RR1": 0.65, "RR2": 0.65, "N1": 16, "N2": 2}),
            ("purity_feasibility_min_sf_deeper", {"SF": 0.40, "RR1": 0.80, "RR2": 0.80, "N1": 28, "N2": 4}),
            ("purity_feasibility_high_cut_low_sf", {"SF": 0.60, "RR1": 0.75, "RR2": 0.75, "_target_split_factor": 1.01, "N1": 18}),
            ("purity_feasibility_low_cut_low_sf", {"SF": 0.60, "RR1": 0.75, "RR2": 0.75, "_target_split_factor": 0.97, "N1": 18}),
            ("more_solvent", {"SF": 1.25}),
            ("less_solvent", {"SF": 0.75}),
            ("higher_rr", {"RR1": 1.70, "RR2": 1.20}),
            ("more_stages", {"N1": 16, "N2": 4}),
            ("stronger_bleed", {"D2": min(total * 0.995, max(0.01, split_targets(spec)[1] * 1.8))}),
            ("softer_split", {"D1": 0.9, "D2": 0.9, "_multiply_distillates": True}),
        ]
    for parent_index, parent in enumerate(parents):
        base = normalized_refinement_case(spec, parent, bounds=bounds)
        for label, delta in variants:
            candidate = dict(base)
            delta_values = dict(delta)
            multiply_distillates = bool(delta_values.pop("_multiply_distillates", False))
            target_split_absolute = delta_values.pop("_target_split_absolute", None)
            if target_split_absolute is not None:
                candidate[target_split_key] = float(target_split_absolute)
            target_split_factor = delta_values.pop("_target_split_factor", None)
            if target_split_factor is not None:
                candidate[target_split_key] = float(candidate.get(target_split_key) or 0.0) * float(target_split_factor)
            companion_split_factor = delta_values.pop("_companion_split_factor", None)
            if companion_split_factor is not None:
                candidate[companion_split_key] = float(candidate.get(companion_split_key) or 0.0) * float(companion_split_factor)
            for key, value in delta_values.items():
                if key in {"SF", "RR1", "RR2"}:
                    candidate[key] = float(candidate.get(key) or 0.0) * float(value)
                elif key in {"N1", "N2"}:
                    candidate[key] = int(candidate.get(key) or 0) + int(value)
                elif key in {"D1", "D2"} and multiply_distillates:
                    candidate[key] = float(candidate.get(key) or 0.0) * float(value)
                else:
                    candidate[key] = value
            candidate["source"] = "generic_refine_g{}_p{}_{}".format(generation, parent_index + 1, label)
            candidate["case_id"] = next_case_id(next_index + len(cases))
            candidate = normalized_refinement_case(spec, candidate, bounds=bounds)
            signature = case_signature(candidate)
            if signature in existing or signature in local_seen:
                continue
            local_seen.add(signature)
            cases.append(candidate)
            if len(cases) >= max(1, int(population_size or 1)):
                return cases
    return cases


def random_stage_layout(bounds, n1, n2, rng):
    feed_gap = max(1, int((bounds or {}).get("NF1_NS1_MIN_GAP", 1)))
    bottom_margin = max(1, int((bounds or {}).get("NF1_BOTTOM_MARGIN", 1)))
    ns1_high = max(2, int(n1) - feed_gap - bottom_margin)
    ns1 = rng.randint(2, ns1_high) if ns1_high >= 2 else 2
    nf1_low = max(2, ns1 + feed_gap)
    nf1_high = max(nf1_low, int(n1) - bottom_margin)
    nf1 = rng.randint(nf1_low, nf1_high) if nf1_high >= nf1_low else nf1_low
    nf2_high = max(2, int(n2) - 1)
    nf2 = rng.randint(2, nf2_high) if nf2_high >= 2 else 2
    return ns1, nf1, nf2


def genetic_initial_cases(spec, population_size, bounds=None, rng=None):
    rng = rng or random.Random(20260630)
    population_size = max(1, int(population_size or 1))
    bounds = dict(bounds or optimizer_bounds_from_args(argparse.Namespace(bounds_profile="publication_safe"), spec))
    cases = []
    existing = set()
    for seed in candidate_cases(spec, population_size, bounds=bounds):
        seed = dict(seed)
        seed["case_id"] = next_case_id(len(cases) + 1)
        signature = case_signature(seed)
        if signature in existing:
            continue
        existing.add(signature)
        cases.append(seed)
    if len(cases) >= population_size:
        return cases[:population_size]
    remaining = population_size - len(cases)
    lhs_keys = ("SF", "RR1", "RR2", "D1", "D2", "N1", "N2")
    lhs_values = {}
    for key in lhs_keys:
        low, high = bounds[key]
        values = []
        for index in range(remaining):
            fraction = (index + rng.random()) / max(1, remaining)
            value = float(low) + fraction * (float(high) - float(low))
            if key in {"N1", "N2"}:
                value = int(round(value))
            values.append(value)
        rng.shuffle(values)
        lhs_values[key] = values
    attempts = 0
    total = feed_total(spec)
    while len(cases) < population_size and attempts < population_size * 40:
        template = dict(base_case(spec))
        if attempts < remaining:
            lhs_index = attempts
            for key in lhs_keys:
                template[key] = lhs_values[key][lhs_index]
        else:
            template["SF"] = rng.uniform(float(bounds["SF"][0]), float(bounds["SF"][1]))
            template["RR1"] = rng.uniform(float(bounds["RR1"][0]), float(bounds["RR1"][1]))
            template["RR2"] = rng.uniform(float(bounds["RR2"][0]), float(bounds["RR2"][1]))
            template["D1"] = rng.uniform(float(bounds["D1"][0]), float(bounds["D1"][1]))
            template["D2"] = rng.uniform(float(bounds["D2"][0]), float(bounds["D2"][1]))
            template["N1"] = rng.randint(int(bounds["N1"][0]), int(bounds["N1"][1]))
            template["N2"] = rng.randint(int(bounds["N2"][0]), int(bounds["N2"][1]))
        ns1, nf1, nf2 = random_stage_layout(bounds, int(template["N1"]), int(template["N2"]), rng)
        template["NS1"] = ns1
        template["NF1"] = nf1
        template["NF2"] = nf2
        template["makeup_flow"] = round(max(0.0, total * 0.02), 6)
        template["source"] = "generic_nsga2_lhs_seed_{:02d}".format(len(cases) + 1)
        template["case_id"] = next_case_id(len(cases) + 1)
        candidate = normalized_refinement_case(spec, template, bounds=bounds)
        signature = case_signature(candidate)
        attempts += 1
        if signature in existing:
            continue
        existing.add(signature)
        cases.append(candidate)
    return cases[:population_size]


def nsga2_crowding_distances(rows):
    distances = {str(row.get("case_id") or ""): 0.0 for row in rows or []}
    fronts = {}
    for row in rows or []:
        rank = int(safe_float(row.get("pareto_rank"), 10**9) or 10**9)
        fronts.setdefault(rank, []).append(row)
    for front_rows in fronts.values():
        if len(front_rows) <= 2:
            for row in front_rows:
                distances[str(row.get("case_id") or "")] = float("inf")
            continue
        for name in MULTI_OBJECTIVE_NAMES:
            ordered = sorted(front_rows, key=lambda row: safe_float(row.get(name), math.inf))
            low = safe_float(ordered[0].get(name), 0.0)
            high = safe_float(ordered[-1].get(name), low)
            span = abs(high - low) or 1.0
            distances[str(ordered[0].get("case_id") or "")] = float("inf")
            distances[str(ordered[-1].get("case_id") or "")] = float("inf")
            for index in range(1, len(ordered) - 1):
                cid = str(ordered[index].get("case_id") or "")
                if math.isinf(distances.get(cid, 0.0)):
                    continue
                previous_value = safe_float(ordered[index - 1].get(name), low)
                next_value = safe_float(ordered[index + 1].get(name), high)
                distances[cid] = distances.get(cid, 0.0) + abs(next_value - previous_value) / span
    return distances


def nsga2_parent_pool(rows, population_size):
    eligible_rows = [
        row for row in rows or []
        if row_is_search_parent_control_family(row)
    ]
    if not eligible_rows:
        return []
    assign_pareto_fronts(eligible_rows)
    assign_topsis_scores(eligible_rows)
    feasible = multi_objective_candidate_rows(eligible_rows)
    if len(feasible) < 2:
        return selectable_parent_rows(rows, max(2, int(population_size or 1)))
    crowding = nsga2_crowding_distances(feasible)
    ordered = sorted(
        feasible,
        key=lambda row: (
            int(safe_float(row.get("pareto_rank"), 10**9) or 10**9),
            -crowding.get(str(row.get("case_id") or ""), 0.0),
            -safe_float(row.get("topsis_score"), -math.inf),
            parent_sort_key(row),
        ),
    )
    return ordered[: max(2, int(population_size or 1))]


def mutated_continuous_value(key, parent_a, parent_b, bounds, rng, crossover_rate, mutation_rate):
    low, high = bounds[key]
    span = max(1e-12, float(high) - float(low))
    value_a = safe_float(parent_a.get(key), (float(low) + float(high)) / 2.0)
    value_b = safe_float(parent_b.get(key), value_a)
    if rng.random() < float(crossover_rate or 0.0):
        alpha = rng.random()
        value = alpha * value_a + (1.0 - alpha) * value_b
    else:
        value = value_a if rng.random() < 0.5 else value_b
    if rng.random() < float(mutation_rate or 0.0):
        value += rng.gauss(0.0, 0.08 * span)
    return round(max(float(low), min(float(high), value)), 6 if key in {"D1", "D2"} else 4)


def mutated_integer_value(key, parent_a, parent_b, low, high, rng, crossover_rate, mutation_rate):
    low = int(low)
    high = int(high)
    value_a = int(round(safe_float(parent_a.get(key), low)))
    value_b = int(round(safe_float(parent_b.get(key), value_a)))
    if rng.random() < float(crossover_rate or 0.0):
        alpha = rng.random()
        value = int(round(alpha * value_a + (1.0 - alpha) * value_b))
    else:
        value = value_a if rng.random() < 0.5 else value_b
    if rng.random() < float(mutation_rate or 0.0):
        step_max = max(1, int(round(0.08 * max(1, high - low))))
        value += rng.choice((-1, 1)) * rng.randint(1, step_max)
    return clamp_int(value, low, high)


def nsga2_child_case(spec, parent_a, parent_b, generation, case_index, bounds, rng, crossover_rate, mutation_rate):
    parent_a = normalized_refinement_case(spec, parent_a, bounds=bounds)
    parent_b = normalized_refinement_case(spec, parent_b, bounds=bounds)
    child = dict(parent_a)
    for key in CONTINUOUS_VARIABLES:
        child[key] = mutated_continuous_value(key, parent_a, parent_b, bounds, rng, crossover_rate, mutation_rate)
    child["N1"] = mutated_integer_value("N1", parent_a, parent_b, bounds["N1"][0], bounds["N1"][1], rng, crossover_rate, mutation_rate)
    child["N2"] = mutated_integer_value("N2", parent_a, parent_b, bounds["N2"][0], bounds["N2"][1], rng, crossover_rate, mutation_rate)
    feed_gap = max(1, int(bounds.get("NF1_NS1_MIN_GAP", 1)))
    bottom_margin = max(1, int(bounds.get("NF1_BOTTOM_MARGIN", 1)))
    child["NS1"] = mutated_integer_value(
        "NS1",
        parent_a,
        parent_b,
        2,
        max(2, int(child["N1"]) - feed_gap - bottom_margin),
        rng,
        crossover_rate,
        mutation_rate,
    )
    child["NF1"] = mutated_integer_value(
        "NF1",
        parent_a,
        parent_b,
        int(child["NS1"]) + feed_gap,
        max(int(child["NS1"]) + feed_gap, int(child["N1"]) - bottom_margin),
        rng,
        crossover_rate,
        mutation_rate,
    )
    child["NF2"] = mutated_integer_value(
        "NF2",
        parent_a,
        parent_b,
        2,
        max(2, int(child["N2"]) - 1),
        rng,
        crossover_rate,
        mutation_rate,
    )
    child["source"] = "generic_nsga2_hybrid_g{}_p{}_x{}_child".format(
        generation,
        row_case_index(parent_a),
        row_case_index(parent_b),
    )
    child["case_id"] = next_case_id(case_index)
    child["search_mode"] = "nsga2_hybrid"
    child["optimizer_algorithm"] = "NSGA-II hybrid"
    return normalized_refinement_case(spec, child, bounds=bounds)


def nsga2_hybrid_cases(
    spec,
    rows,
    generation,
    population_size,
    next_index,
    bounds=None,
    rng=None,
    crossover_rate=0.85,
    mutation_rate=0.18,
):
    rng = rng or random.Random(20260630 + int(generation or 0))
    population_size = max(1, int(population_size or 1))
    bounds = dict(bounds or optimizer_bounds_from_args(argparse.Namespace(bounds_profile="publication_safe"), spec))
    effective_bounds = adaptive_constraint_repair_bounds(spec, rows, bounds)
    parent_pool = nsga2_parent_pool(rows, max(4, population_size))
    if not parent_pool:
        return refinement_cases(spec, rows, generation, population_size, next_index, bounds=effective_bounds)
    existing = {
        case_signature(normalized_refinement_case(spec, row, bounds=effective_bounds))
        for row in rows or []
        if row_is_search_parent_control_family(row)
    }
    cases = []
    local_seen = set()
    metric_probe = closed_loop_metric_probe_case(spec, rows, next_index, bounds=effective_bounds)
    if metric_probe:
        cases.append(metric_probe)
        local_seen.add(case_signature(metric_probe))
    if not multi_objective_candidate_rows(rows):
        repair_target = min(population_size, max(12, population_size // 2))
        repair_cases = refinement_cases(spec, rows, generation, repair_target, next_index, bounds=effective_bounds)
        for repair in repair_cases:
            repair = dict(repair)
            repair["source"] = "generic_nsga2_purity_constraint_repair_{}".format(repair.get("source") or "adaptive")
            repair["search_mode"] = "nsga2_hybrid"
            repair["optimizer_algorithm"] = "NSGA-II hybrid"
            signature = case_signature(repair)
            if signature in existing or signature in local_seen:
                continue
            local_seen.add(signature)
            cases.append(repair)
            if len(cases) >= repair_target:
                break
    attempts = 0
    while len(cases) < population_size and attempts < population_size * 80:
        attempts += 1
        if len(parent_pool) >= 2:
            parent_a, parent_b = rng.sample(parent_pool, 2)
        else:
            parent_a = parent_b = parent_pool[0]
        candidate = nsga2_child_case(
            spec,
            parent_a,
            parent_b,
            generation,
            next_index + len(cases),
            effective_bounds,
            rng,
            crossover_rate,
            mutation_rate,
        )
        signature = case_signature(candidate)
        if signature in existing or signature in local_seen:
            continue
        local_seen.add(signature)
        cases.append(candidate)
    if len(cases) < population_size:
        fallback_cases = refinement_cases(
            spec,
            rows,
            generation,
            population_size - len(cases),
            next_index + len(cases),
            bounds=effective_bounds,
        )
        for fallback in fallback_cases:
            fallback = dict(fallback)
            fallback["source"] = "generic_nsga2_hybrid_fallback_{}".format(fallback.get("source") or "adaptive")
            fallback["search_mode"] = "nsga2_hybrid"
            fallback["optimizer_algorithm"] = "NSGA-II hybrid"
            signature = case_signature(fallback)
            if signature in existing or signature in local_seen:
                continue
            local_seen.add(signature)
            cases.append(fallback)
            if len(cases) >= population_size:
                break
    return cases


def optimizer_search_metadata(args):
    search_mode = str(getattr(args, "search_mode", "adaptive_refinement") or "adaptive_refinement").strip()
    if search_mode not in SEARCH_MODES:
        search_mode = "adaptive_refinement"
    if search_mode == "nsga2_hybrid":
        algorithm = "NSGA-II hybrid"
        description = (
            "Initial engineering seeds plus LHS sampling; later generations use "
            "non-dominated sorting, crowding-distance parent selection, bounded "
            "crossover/mutation, and adaptive-refinement fallback."
        )
    else:
        algorithm = "adaptive_refinement"
        description = "Engineering parent ranking with deterministic local perturbation templates."
    return {
        "search_mode": search_mode,
        "algorithm": algorithm,
        "description": description,
        "crossover_rate": float(getattr(args, "crossover_rate", 0.0) or 0.0),
        "mutation_rate": float(getattr(args, "mutation_rate", 0.0) or 0.0),
        "random_seed": int(getattr(args, "random_seed", 0) or 0),
        "case_archive_policy": (
            "per_case_validated_bkp"
            if bool(getattr(args, "fast_save_validated_bkp", False))
            else "lightweight_rows_only"
        ),
        "validated_bkp_saved_per_case": bool(getattr(args, "fast_save_validated_bkp", False)),
        "reset_resume_convergence": bool(getattr(args, "reset_resume_convergence", False)),
        "repair_probe_cover_control_families": bool(getattr(args, "repair_probe_cover_control_families", False)),
    }


def optimizer_closed_loop_summary(rows):
    rows = rows or []
    closed_loop_rows = [row for row in rows if truthy(row.get("closed_loop_recycle"))]
    closed_loop_pass_rows = [
        row for row in closed_loop_rows
        if row_is_closed_loop_pass(row)
    ]
    diagnostic_closed_loop_pass_rows = [
        row for row in closed_loop_rows
        if str(row.get("pass_or_fail") or "").upper() == "PASS" and not row_is_closed_loop_pass(row)
    ]
    open_loop_pass_rows = [
        row for row in rows
        if not truthy(row.get("closed_loop_recycle"))
        and str(row.get("pass_or_fail") or "").upper() == "PASS"
    ]
    pass_like_rows = closed_loop_pass_rows + diagnostic_closed_loop_pass_rows
    failed_rows = [row for row in closed_loop_rows if row not in pass_like_rows]
    control_mode_counts = {}
    for row in closed_loop_rows:
        mode = recycle_control_mode_from_row(row)
        control_mode_counts[mode] = control_mode_counts.get(mode, 0) + 1
    calculator_count = control_mode_counts.get("calculator_inventory_control", 0)
    bounded_count = control_mode_counts.get("bounded_nonnegative_makeup_control", 0)
    fixed_makeup_count = control_mode_counts.get("fixed_fresh_makeup_diagnostic", 0)
    untested_repair_strategies = []
    if closed_loop_rows and not calculator_count:
        untested_repair_strategies.append("calculator_inventory_control")
    if closed_loop_rows and calculator_count and not bounded_count:
        untested_repair_strategies.append("bounded_nonnegative_makeup_control")
    def error_text(row):
        return " ".join(str(row.get(key) or "") for key in (
            "error_message",
            "convergence_status",
            "aspen_convergence_failure_signals",
            "status",
            "pass_or_fail",
            "constraint_violation",
            "stdout_tail",
            "stderr_tail",
        )).upper()
    max_iter_count = sum(
        1 for row in failed_rows
        if (
            "EXCEEDED MAXIMUM NUMBER OF ITERATIONS" in error_text(row)
            or "EXCEEDED_MAXIMUM_ITERATIONS" in error_text(row)
            or "MAXIMUM NUMBER OF ITERATIONS" in error_text(row)
        )
    )
    output_missing_count = sum(1 for row in failed_rows if "REQUIRED_OUTPUTS_MISSING" in error_text(row))
    rpc_failure_count = sum(
        1 for row in failed_rows
        if (
            "-2147023170" in error_text(row)
            or "REMOTE PROCEDURE CALL" in error_text(row)
            or "远程过程调用失败" in str(row.get("error_message") or "")
            or "PYWINTYPES.COM_ERROR" in error_text(row)
        )
    )
    crash_count = sum(1 for row in failed_rows if truthy(row.get("aspen_crash_dump")) or str(row.get("dump_files") or "").strip())
    timeout_count = sum(1 for row in failed_rows if truthy(row.get("timed_out")))
    if closed_loop_pass_rows:
        status = "closed_loop_pass_available"
        dominant_failure = ""
        stop_before_more = False
    elif not closed_loop_rows:
        status = "closed_loop_not_attempted"
        dominant_failure = ""
        stop_before_more = False
    else:
        if output_missing_count >= max(crash_count, max_iter_count, timeout_count, 1):
            dominant_failure = "required_outputs_missing"
        elif rpc_failure_count >= max(crash_count, max_iter_count, timeout_count, 1):
            dominant_failure = "aspen_com_rpc_failure"
        elif crash_count >= max(max_iter_count, timeout_count, 1):
            dominant_failure = "aspen_fatal_dump"
        elif timeout_count >= max(max_iter_count, 1):
            dominant_failure = "case_timeout"
        elif max_iter_count:
            dominant_failure = "recycle_convergence_max_iterations"
        else:
            dominant_failure = "closed_loop_not_converged"
        if dominant_failure == "required_outputs_missing":
            status = "closed_loop_required_outputs_missing"
            stop_before_more = True
        elif dominant_failure == "aspen_com_rpc_failure":
            status = "closed_loop_aspen_rpc_failure"
            stop_before_more = True
        else:
            status = "closed_loop_recycle_repair_required" if len(closed_loop_rows) >= 6 else "closed_loop_trials_no_pass"
            stop_before_more = status == "closed_loop_recycle_repair_required"
    return {
        "status": status,
        "closed_loop_case_count": len(closed_loop_rows),
        "closed_loop_pass_case_count": len(closed_loop_pass_rows),
        "diagnostic_closed_loop_pass_case_count": len(diagnostic_closed_loop_pass_rows),
        "open_loop_pass_case_count": len(open_loop_pass_rows),
        "max_iteration_failure_count": max_iter_count,
        "required_outputs_missing_count": output_missing_count,
        "aspen_rpc_failure_count": rpc_failure_count,
        "aspen_crash_dump_count": crash_count,
        "timeout_case_count": timeout_count,
        "recycle_control_mode_counts": control_mode_counts,
        "calculator_inventory_case_count": calculator_count,
        "bounded_nonnegative_makeup_case_count": bounded_count,
        "fixed_fresh_makeup_diagnostic_case_count": fixed_makeup_count,
        "untested_repair_strategies": untested_repair_strategies,
        "dominant_failure": dominant_failure,
        "full_flow_completion_allowed": bool(closed_loop_pass_rows),
        "agent_should_stop_before_more_optimization": stop_before_more,
    }


def dynamic_variable_bounds_for_row(row, bounds):
    n1 = safe_float(row.get("N1"))
    n2 = safe_float(row.get("N2"))
    ns1 = safe_float(row.get("NS1"))
    feed_gap = max(1, int((bounds or {}).get("NF1_NS1_MIN_GAP", 1)))
    bottom_margin = max(1, int((bounds or {}).get("NF1_BOTTOM_MARGIN", 1)))
    result = {
        "SF": bounds.get("SF"),
        "RR1": bounds.get("RR1"),
        "RR2": bounds.get("RR2"),
        "D1": bounds.get("D1"),
        "D2": bounds.get("D2"),
        "N1": bounds.get("N1"),
        "N2": bounds.get("N2"),
    }
    if n1 is not None:
        result["NS1"] = (2, max(2, int(round(n1)) - feed_gap - bottom_margin))
        if ns1 is not None:
            result["NF1"] = (
                int(round(ns1)) + feed_gap,
                max(int(round(ns1)) + feed_gap, int(round(n1)) - bottom_margin),
            )
    if n2 is not None:
        result["NF2"] = (2, max(2, int(round(n2)) - 1))
    return {key: value for key, value in result.items() if value}


def boundary_hits_for_row(row, bounds, tolerance_fraction=BOUNDARY_TOLERANCE_FRACTION):
    hits = []
    row_bounds = dynamic_variable_bounds_for_row(row, bounds or {})
    integer_vars = {"N1", "NF1", "NS1", "N2", "NF2"}
    for name in VARIABLES:
        if name not in row_bounds:
            continue
        value = safe_float(row.get(name))
        if value is None:
            continue
        low, high = row_bounds[name]
        low = float(low)
        high = float(high)
        span = max(0.0, high - low)
        tolerance = max(span * float(tolerance_fraction or 0.0), 0.5 if name in integer_vars else 1.0e-9)
        if value <= low + tolerance:
            hits.append({"variable": name, "side": "lower", "value": value, "bound": low})
        elif value >= high - tolerance:
            hits.append({"variable": name, "side": "upper", "value": value, "bound": high})
    return hits


def optimization_boundary_diagnosis(rows, bounds):
    bounds = dict(bounds or bounds_profile_config("publication_safe"))
    candidates = multi_objective_candidate_rows(rows)
    topsis_row = min(
        (row for row in candidates if safe_float(row.get("topsis_rank")) is not None),
        key=lambda row: safe_float(row.get("topsis_rank"), math.inf),
        default={},
    )
    best_tac_row = min(
        (row for row in candidates if safe_float(row.get("TAC")) is not None),
        key=lambda row: safe_float(row.get("TAC"), math.inf),
        default={},
    )
    pareto_rows = [row for row in candidates if truthy(row.get("pareto_front"))]
    selected = [
        ("topsis_best", topsis_row),
        ("best_tac", best_tac_row),
    ]
    selected_hits = []
    for label, row in selected:
        if not row:
            continue
        for hit in boundary_hits_for_row(row, bounds):
            item = dict(hit)
            item["selection"] = label
            item["case_id"] = row.get("case_id", "")
            selected_hits.append(item)
    pareto_hit_counts = {}
    for row in pareto_rows:
        for hit in boundary_hits_for_row(row, bounds):
            key = "{}_{}".format(hit.get("variable"), hit.get("side"))
            pareto_hit_counts[key] = pareto_hit_counts.get(key, 0) + 1
    status = "no_multi_objective_feasible_cases"
    recommendation = "collect_feasible_cases_before_adjusting_bounds"
    should_adjust = False
    recommended_profile = bounds.get("profile", "exploratory")
    if candidates:
        status = "interior_or_tolerance_ok"
        recommendation = "keep_current_bounds"
        if selected_hits:
            status = "boundary_limited"
            lower_vars = {hit.get("variable") for hit in selected_hits if hit.get("side") == "lower"}
            upper_vars = {hit.get("variable") for hit in selected_hits if hit.get("side") == "upper"}
            if bounds.get("profile") == "exploratory" and lower_vars & {"SF", "RR1", "RR2", "N1", "N2"}:
                should_adjust = True
                recommended_profile = "publication_safe"
                recommendation = "rerun_with_publication_safe_bounds"
            elif bounds.get("profile") == "publication_safe" and lower_vars:
                should_adjust = False
                recommendation = "publication_safe_floor_active_review_constraints_or_raise_floor"
            elif upper_vars:
                should_adjust = True
                recommendation = "expand_or_recenter_upper_bounds_after_feasibility_review"
            else:
                should_adjust = True
                recommendation = "review_bounds_before_accepting_boundary_solution"
    return {
        "status": status,
        "bounds_profile": bounds.get("profile", "exploratory"),
        "bounds": serializable_bounds(bounds),
        "boundary_tolerance_fraction": BOUNDARY_TOLERANCE_FRACTION,
        "agent_should_adjust_bounds": bool(should_adjust),
        "recommended_bounds_profile": recommended_profile,
        "recommended_action": recommendation,
        "topsis_best_case_id": topsis_row.get("case_id", "") if topsis_row else "",
        "best_tac_case_id": best_tac_row.get("case_id", "") if best_tac_row else "",
        "selected_boundary_hits": selected_hits,
        "pareto_boundary_hit_counts": pareto_hit_counts,
    }


def write_outputs(rows, output_dir, stem, spec=None, convergence=None, maintenance_events=None, bounds=None, search_metadata=None):
    if spec is not None:
        rows = [attach_case_objectives(spec, row) for row in rows]
    multi_objective = evaluate_multi_objective_rows(rows)
    bounds = dict(bounds or bounds_profile_config("publication_safe"))
    boundary_diagnosis = optimization_boundary_diagnosis(rows, bounds)
    feasibility_diagnosis = constraint_feasibility_diagnosis(spec or {}, rows, bounds) if spec is not None else {}
    if feasibility_diagnosis:
        boundary_diagnosis["constraint_feasibility"] = feasibility_diagnosis
        if feasibility_diagnosis.get("agent_should_adjust_bounds"):
            boundary_diagnosis["status"] = feasibility_diagnosis.get("status") or boundary_diagnosis.get("status")
            boundary_diagnosis["agent_should_adjust_bounds"] = True
            boundary_diagnosis["recommended_bounds_profile"] = feasibility_diagnosis.get("recommended_bounds_profile", "")
            boundary_diagnosis["recommended_action"] = feasibility_diagnosis.get("recommended_action", "")
            boundary_diagnosis["selected_boundary_hits"] = feasibility_diagnosis.get("best_boundary_hits", [])
    result_counts = optimizer_result_counts(rows, spec)
    convergence = convergence or {}
    if convergence:
        multi_objective["convergence_status"] = convergence.get("status", "")
        multi_objective["converged"] = bool(convergence.get("converged"))
        multi_objective["convergence_stop_reason"] = convergence.get("stop_reason", "")
    search_metadata = dict(search_metadata or {})
    if search_metadata:
        multi_objective["search_mode"] = search_metadata.get("search_mode", "")
        multi_objective["algorithm"] = search_metadata.get("algorithm", "")
    multi_objective["boundary_status"] = boundary_diagnosis.get("status", "")
    multi_objective["boundary_recommended_action"] = boundary_diagnosis.get("recommended_action", "")
    output_dir = Path(output_dir)
    opt_dir = output_dir / "optimization"
    opt_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = opt_dir / "{}.checkpoint.jsonl".format(stem)
    case_events = opt_dir / "{}.case_events.jsonl".format(stem)
    checkpoint.write_text(
        "".join(safe_json_dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )
    workbook = opt_dir / "{}.xlsx".format(stem)
    try:
        import pandas as pd
        all_cases = pd.DataFrame(rows)
        pareto_rows = [row for row in rows if truthy(row.get("pareto_front"))]
        topsis_rows = sorted(
            [row for row in rows if safe_float(row.get("topsis_rank")) is not None],
            key=lambda row: int(safe_float(row.get("topsis_rank"), 10**9)),
        )
        with pd.ExcelWriter(str(workbook)) as writer:
            all_cases.to_excel(writer, index=False, sheet_name="all_cases")
            pd.DataFrame(pareto_rows).to_excel(writer, index=False, sheet_name="pareto_front")
            pd.DataFrame(topsis_rows).to_excel(writer, index=False, sheet_name="topsis_ranking")
            pd.DataFrame([multi_objective]).to_excel(writer, index=False, sheet_name="multi_objective_summary")
    except Exception:
        workbook = ""
    pareto_csv = opt_dir / "{}.pareto_front.csv".format(stem)
    topsis_csv = opt_dir / "{}.topsis_ranking.csv".format(stem)
    try:
        import pandas as pd
        pd.DataFrame([row for row in rows if truthy(row.get("pareto_front"))]).to_csv(pareto_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(
            sorted(
                [row for row in rows if safe_float(row.get("topsis_rank")) is not None],
                key=lambda row: int(safe_float(row.get("topsis_rank"), 10**9)),
            )
        ).to_csv(topsis_csv, index=False, encoding="utf-8-sig")
    except Exception:
        pareto_csv = ""
        topsis_csv = ""
    report = {
        "agent": "generic_aspen_once_optimizer",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "case_count": len(rows),
        "pass_case_count": result_counts.get("formal_pass_case_count", 0),
        "formal_pass_case_count": result_counts.get("formal_pass_case_count", 0),
        "aspen_run_pass_case_count": result_counts.get("aspen_run_pass_case_count", 0),
        "closed_loop_pass_case_count": result_counts.get("closed_loop_pass_case_count", 0),
        "purity_pass_case_count": result_counts.get("purity_pass_case_count", 0),
        "measured_case_count": result_counts.get("measured_case_count", 0),
        "closed_loop_recycle_diagnosis": optimizer_closed_loop_summary(rows),
        "constraint_feasibility_diagnosis": feasibility_diagnosis,
        "multi_objective_optimization": multi_objective,
        "convergence": convergence,
        "search_strategy": search_metadata,
        "optimizer_algorithm": search_metadata.get("algorithm", "") if search_metadata else "",
        "search_mode": search_metadata.get("search_mode", "") if search_metadata else "",
        "output_refresh_mode": "full_batch_refresh",
        "range_policy": {
            "bounds_profile": bounds.get("profile", "exploratory"),
            "bounds": serializable_bounds(bounds),
            "boundary_tolerance_fraction": BOUNDARY_TOLERANCE_FRACTION,
        },
        "boundary_diagnosis": boundary_diagnosis,
        "maintenance_events": maintenance_events or [],
        "latest_maintenance_event": (maintenance_events or [])[-1] if maintenance_events else {},
        "best_pass_case_id": min(
            (row for row in rows if row_is_formal_multi_objective_feasible(row) and row.get("TAC") is not None),
            key=lambda row: float(row.get("TAC") or 0.0),
            default={},
        ).get("case_id", ""),
        "best_closed_loop_case_id": min(
            (row for row in rows if row_is_closed_loop_pass(row) and row.get("TAC") is not None),
            key=lambda row: float(row.get("TAC") or 0.0),
            default={},
        ).get("case_id", ""),
        "topsis_best_case_id": multi_objective.get("topsis_best_case_id", ""),
        "checkpoint": str(checkpoint.resolve()),
        "case_events_jsonl": str(case_events.resolve()),
        "workbook": str(Path(workbook).resolve()) if workbook else "",
        "pareto_front_csv": str(Path(pareto_csv).resolve()) if pareto_csv else "",
        "topsis_ranking_csv": str(Path(topsis_csv).resolve()) if topsis_csv else "",
        "rows": rows,
    }
    report_path = output_dir / "generic_aspen_once_optimizer.report.json"
    report_path.write_text(safe_json_dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def optimizer_opt_dir(output_dir):
    opt_dir = Path(output_dir) / "optimization"
    opt_dir.mkdir(parents=True, exist_ok=True)
    return opt_dir


def compact_json_dumps(payload):
    return safe_json_dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def append_case_event(row, output_dir, stem):
    opt_dir = optimizer_opt_dir(output_dir)
    event_path = opt_dir / "{}.case_events.jsonl".format(stem)
    event = {
        "event": "case_completed",
        "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
        "row": row,
    }
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(compact_json_dumps(event) + "\n")
    return event_path


def backfill_case_events(rows, output_dir, stem):
    rows = [row for row in rows or [] if isinstance(row, dict)]
    if not rows:
        return None
    opt_dir = optimizer_opt_dir(output_dir)
    event_path = opt_dir / "{}.case_events.jsonl".format(stem)
    existing = set()
    if event_path.exists():
        try:
            with event_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"case_completed"' not in line and '"case_backfilled"' not in line:
                        continue
                    event = safe_json_loads(line, default=None)
                    if not isinstance(event, dict):
                        continue
                    row = event.get("row") if isinstance(event, dict) else None
                    case_id = str((row or {}).get("case_id") or "").strip()
                    if case_id:
                        existing.add(case_id)
        except Exception:
            existing = set()
    wrote = 0
    with event_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            case_id = str(row.get("case_id") or "").strip()
            if not case_id or case_id in existing:
                continue
            event = {
                "event": "case_backfilled",
                "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
                "row": row,
            }
            handle.write(compact_json_dumps(event) + "\n")
            existing.add(case_id)
            wrote += 1
    return {"path": str(event_path.resolve()), "backfilled_rows": wrote, "existing_rows": len(existing)}


def existing_ranked_multi_objective_summary(rows):
    rows = list(rows or [])
    candidates = multi_objective_candidate_rows(rows)
    pareto_rows = [row for row in candidates if truthy(row.get("pareto_front"))]
    ranked_rows = [
        row for row in candidates
        if safe_float(row.get("topsis_score")) is not None
    ]
    best_topsis = min(
        ranked_rows,
        key=lambda row: int(safe_float(row.get("topsis_rank"), 10**9) or 10**9),
        default={},
    )
    if not best_topsis:
        best_topsis = max(
            ranked_rows,
            key=lambda row: safe_float(row.get("topsis_score"), -math.inf),
            default={},
        )
    max_generation = max([int(safe_float(row.get("generation"), 0) or 0) for row in rows] + [0])
    unranked_feasible = [
        row for row in candidates
        if safe_float(row.get("topsis_score")) is None or safe_float(row.get("pareto_rank")) is None
    ]
    status = "lightweight_refresh_pending" if unranked_feasible else (
        "complete" if candidates and pareto_rows and best_topsis else
        ("needs_more_feasible_points" if candidates else "no_feasible_closed_loop_points")
    )
    return {
        "status": status,
        "objective_names": list(MULTI_OBJECTIVE_NAMES),
        "objective_sense": {name: "minimize" for name in MULTI_OBJECTIVE_NAMES},
        "objective_weights": MULTI_OBJECTIVE_WEIGHTS,
        "case_count": len(rows),
        "feasible_case_count": len(candidates),
        "pareto_front_case_count": len(pareto_rows),
        "max_generation": max_generation,
        "topsis_best_case_id": best_topsis.get("case_id", ""),
        "topsis_best_score": best_topsis.get("topsis_score", ""),
        "topsis_best_carbon_emission": best_topsis.get("carbon_emission_ton_per_year", ""),
        "topsis_best_TAC": best_topsis.get("TAC", ""),
        "topsis_best_recovery_loss": best_topsis.get("objective_recovery_loss", ""),
        "topsis_best_recovery": best_topsis.get("target_recovery", ""),
        "topsis_best_solvent_flow_kmol_hr": best_topsis.get("solvent_flow_kmol_hr", ""),
        "topsis_best_column_stage_total": best_topsis.get("column_stage_total", ""),
        "unranked_feasible_case_count": len(unranked_feasible),
        "ranking_refresh_pending": bool(unranked_feasible),
    }


def needs_full_ranking_refresh(rows):
    candidates = multi_objective_candidate_rows(rows)
    return any(
        safe_float(row.get("topsis_score")) is None
        or safe_float(row.get("pareto_rank")) is None
        for row in candidates
    )


def write_lightweight_outputs(rows, output_dir, stem, spec=None, convergence=None, maintenance_events=None, bounds=None, search_metadata=None, latest_row=None, refresh_reason="case_completed"):
    if spec is not None and latest_row is not None:
        latest_row = attach_case_objectives(spec, latest_row)
    rows = list(rows or [])
    if spec is not None:
        rows = [attach_case_objectives(spec, row) for row in rows]
    multi_objective = existing_ranked_multi_objective_summary(rows)
    bounds = dict(bounds or bounds_profile_config("publication_safe"))
    feasibility_diagnosis = constraint_feasibility_diagnosis(spec or {}, rows, bounds) if spec is not None else {}
    convergence = convergence or {}
    if convergence:
        multi_objective["convergence_status"] = convergence.get("status", "")
        multi_objective["converged"] = bool(convergence.get("converged"))
        multi_objective["convergence_stop_reason"] = convergence.get("stop_reason", "")
    search_metadata = dict(search_metadata or {})
    if search_metadata:
        multi_objective["search_mode"] = search_metadata.get("search_mode", "")
        multi_objective["algorithm"] = search_metadata.get("algorithm", "")
    output_dir = Path(output_dir)
    opt_dir = optimizer_opt_dir(output_dir)
    checkpoint = opt_dir / "{}.checkpoint.jsonl".format(stem)
    case_events = opt_dir / "{}.case_events.jsonl".format(stem)
    workbook = opt_dir / "{}.xlsx".format(stem)
    pareto_csv = opt_dir / "{}.pareto_front.csv".format(stem)
    topsis_csv = opt_dir / "{}.topsis_ranking.csv".format(stem)
    status_path = opt_dir / "{}.latest_status.json".format(stem)
    pass_rows = [
        row for row in rows
        if row_is_formal_multi_objective_feasible(row) and row.get("TAC") is not None
    ]
    best_pass = min(pass_rows, key=lambda row: float(row.get("TAC") or 0.0), default={})
    best_closed_loop = min(
        (row for row in rows if row_is_closed_loop_pass(row) and row.get("TAC") is not None),
        key=lambda row: float(row.get("TAC") or 0.0),
        default={},
    )
    result_counts = optimizer_result_counts(rows, spec)
    boundary_diagnosis = {}
    if feasibility_diagnosis:
        boundary_diagnosis["constraint_feasibility"] = feasibility_diagnosis
        boundary_diagnosis["status"] = feasibility_diagnosis.get("status", "")
        boundary_diagnosis["bounds_profile"] = bounds.get("profile", "exploratory")
        boundary_diagnosis["bounds"] = serializable_bounds(bounds)
        boundary_diagnosis["boundary_tolerance_fraction"] = BOUNDARY_TOLERANCE_FRACTION
        boundary_diagnosis["agent_should_adjust_bounds"] = bool(feasibility_diagnosis.get("agent_should_adjust_bounds"))
        boundary_diagnosis["recommended_bounds_profile"] = feasibility_diagnosis.get("recommended_bounds_profile", "")
        boundary_diagnosis["recommended_action"] = feasibility_diagnosis.get("recommended_action", "")
        boundary_diagnosis["selected_boundary_hits"] = feasibility_diagnosis.get("best_boundary_hits", [])
        boundary_diagnosis["pareto_boundary_hit_counts"] = {}
    report = {
        "agent": "generic_aspen_once_optimizer",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "case_count": len(rows),
        "pass_case_count": result_counts.get("formal_pass_case_count", 0),
        "formal_pass_case_count": result_counts.get("formal_pass_case_count", 0),
        "aspen_run_pass_case_count": result_counts.get("aspen_run_pass_case_count", 0),
        "closed_loop_pass_case_count": result_counts.get("closed_loop_pass_case_count", 0),
        "purity_pass_case_count": result_counts.get("purity_pass_case_count", 0),
        "measured_case_count": result_counts.get("measured_case_count", 0),
        "closed_loop_recycle_diagnosis": optimizer_closed_loop_summary(rows),
        "constraint_feasibility_diagnosis": feasibility_diagnosis,
        "multi_objective_optimization": multi_objective,
        "convergence": convergence,
        "search_strategy": search_metadata,
        "optimizer_algorithm": search_metadata.get("algorithm", "") if search_metadata else "",
        "search_mode": search_metadata.get("search_mode", "") if search_metadata else "",
        "output_refresh_mode": "lightweight_case_update",
        "output_refresh_reason": refresh_reason,
        "full_ranking_refresh_pending": bool(multi_objective.get("ranking_refresh_pending")),
        "range_policy": {
            "bounds_profile": bounds.get("profile", "exploratory"),
            "bounds": serializable_bounds(bounds),
            "boundary_tolerance_fraction": BOUNDARY_TOLERANCE_FRACTION,
        },
        "boundary_diagnosis": boundary_diagnosis,
        "maintenance_events": maintenance_events or [],
        "latest_maintenance_event": (maintenance_events or [])[-1] if maintenance_events else {},
        "best_pass_case_id": best_pass.get("case_id", ""),
        "best_closed_loop_case_id": best_closed_loop.get("case_id", ""),
        "topsis_best_case_id": multi_objective.get("topsis_best_case_id", ""),
        "checkpoint": str(checkpoint.resolve()),
        "case_events_jsonl": str(case_events.resolve()),
        "workbook": str(workbook.resolve()) if workbook.exists() else "",
        "pareto_front_csv": str(pareto_csv.resolve()) if pareto_csv.exists() else "",
        "topsis_ranking_csv": str(topsis_csv.resolve()) if topsis_csv.exists() else "",
        "latest_case": latest_row or (rows[-1] if rows else {}),
        "row_storage": {
            "mode": "append_only_case_events",
            "rows_embedded": False,
            "case_events_jsonl": str(case_events.resolve()),
            "note": "Use case_events_jsonl for resume/reconstruction; full rows are embedded only in full refresh/final reports.",
        },
    }
    latest_status = {
        key: report.get(key)
        for key in (
            "agent",
            "generated_at",
            "case_count",
            "pass_case_count",
            "multi_objective_optimization",
            "convergence",
            "search_strategy",
            "output_refresh_mode",
            "output_refresh_reason",
            "full_ranking_refresh_pending",
            "best_pass_case_id",
            "topsis_best_case_id",
            "latest_case",
        )
    }
    status_path.write_text(safe_json_dumps(latest_status, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    report_path = output_dir / "generic_aspen_once_optimizer.report.json"
    report_path.write_text(compact_json_dumps(report) + "\n", encoding="utf-8")
    return report


def parse_args():
    parser = argparse.ArgumentParser(description="Run one generic Aspen optimization pass from a zero-build spec.")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--population-size", type=int, default=1)
    parser.add_argument("--generations", type=int, default=0, help="Maximum refinement generation. With --run-until-converged this is only a safety cap; 0 means no generation cap.")
    parser.add_argument("--stem", default="generic_agent_nsga2")
    parser.add_argument("--search-mode", choices=SEARCH_MODES, default="adaptive_refinement", help="Candidate-generation strategy for refinement generations.")
    parser.add_argument("--random-seed", type=int, default=20260630, help="Deterministic seed for NSGA-II hybrid sampling/crossover/mutation.")
    parser.add_argument("--crossover-rate", type=float, default=0.85, help="NSGA-II hybrid bounded crossover probability.")
    parser.add_argument("--mutation-rate", type=float, default=0.18, help="NSGA-II hybrid bounded mutation probability.")
    parser.add_argument("--stop-after-pass", action="store_true")
    parser.add_argument("--seed-report", default="")
    parser.add_argument("--resume-report", default="", help="Existing generic_aspen_once_optimizer.report.json whose rows should seed this continuation run.")
    parser.add_argument("--reset-resume-convergence", action="store_true", help="Use resume rows as the starting population but restart convergence/patience accounting.")
    parser.add_argument("--case-timeout-seconds", type=int, default=240)
    parser.add_argument("--fast-inplace", action="store_true", help="Reuse one Aspen archive session; write Tree variables, reinitialize, Run2, read generic outputs.")
    parser.add_argument("--fast-template-bkp", default="", help="Stable .bkp archive to copy once per fast in-place Aspen session.")
    parser.add_argument("--fast-session-id", default="generic_fast_inplace", help="Archive/session id used for the reusable fast in-place Aspen copy.")
    parser.add_argument("--fast-session-restart-interval", type=int, default=20, help="Close and reopen the fast in-place Aspen session after this many cases; 0 disables.")
    parser.add_argument("--fast-run-timeout-seconds", type=int, default=0, help="Run2 timeout inside fast in-place mode; 0 uses --case-timeout-seconds.")
    parser.add_argument("--fast-save-validated-bkp", action="store_true", help="Write a per-case validated BKP in fast mode for traceability; slower.")
    parser.add_argument("--bounds-profile", choices=sorted(BOUNDS_PROFILES), default="publication_safe", help="Decision-variable bound policy used by the optimizer.")
    parser.add_argument("--min-sf", type=float, default=None)
    parser.add_argument("--max-sf", type=float, default=None)
    parser.add_argument("--min-rr", type=float, default=None)
    parser.add_argument("--max-rr", type=float, default=None)
    parser.add_argument("--min-n1", type=int, default=None)
    parser.add_argument("--max-n1", type=int, default=None)
    parser.add_argument("--min-n2", type=int, default=None)
    parser.add_argument("--max-n2", type=int, default=None)
    parser.add_argument("--nf1-bottom-margin", type=int, default=None, help="Minimum number of trays below the main feed stage in B1.")
    parser.add_argument("--feed-solvent-stage-gap", type=int, default=None, help="Minimum stage gap between solvent feed and main feed in B1.")
    parser.add_argument("--aspen-cleanup-batch-size", type=int, default=1, help="Run post-case Aspen maintenance after this many launched cases; 0 disables batch cleanup.")
    parser.add_argument("--aspen-cleanup-settle-seconds", type=int, default=18, help="Seconds to wait while clearing Aspen processes created after optimizer start.")
    parser.add_argument("--aspen-cleanup-cooldown-seconds", type=int, default=10, help="Short pause after batch cleanup so Aspen COM state can settle.")
    parser.add_argument("--aspen-process-limit", type=int, default=ASPEN_MAX_PARALLEL_PROCESSES, help="Maximum live Aspen-related automation processes tolerated before pre-case cleanup.")
    parser.add_argument("--start-index", type=int, default=1, help="1-based candidate index to start from.")
    parser.add_argument("--max-cases", type=int, default=0, help="Maximum candidate cases to run after start-index; 0 means no extra limit.")
    parser.add_argument(
        "--required-output-missing-stop-streak",
        type=int,
        default=REQUIRED_OUTPUT_MISSING_STOP_STREAK,
        help="Stop after this many consecutive required-output-missing cases without a closed-loop pass; 0 disables the streak stop for bounded diagnostic probes.",
    )
    parser.add_argument(
        "--repair-probe-cover-control-families",
        action="store_true",
        help="Prioritize one representative case from each required recycle-control family before same-family refinements.",
    )
    parser.add_argument("--run-until-converged", action="store_true", help="Continue refinement until the convergence patience rule is met; generation/case limits become safety caps.")
    parser.add_argument("--min-generations", type=int, default=2, help="Minimum refinement generations before convergence can stop the optimizer.")
    parser.add_argument("--convergence-patience", type=int, default=3, help="Stop after this many consecutive generations without meaningful Pareto/TOPSIS improvement.")
    parser.add_argument("--convergence-score-epsilon", type=float, default=0.002, help="Minimum TOPSIS-score improvement counted as meaningful.")
    parser.add_argument("--convergence-objective-rel-epsilon", type=float, default=0.002, help="Minimum relative improvement in any objective counted as meaningful.")
    parser.add_argument("--convergence-objective-abs-epsilon", type=float, default=1e-9, help="Minimum absolute improvement in any objective counted as meaningful.")
    parser.add_argument("--full-refresh-interval", type=int, default=0, help="Optional case interval for full Pareto/TOPSIS/Excel refresh during a run; 0 disables periodic full refresh and writes the full workbook/report only at final stop.")
    return parser.parse_args()


def main():
    args = parse_args()
    spec = zero.load_spec(args.spec)
    bounds = optimizer_bounds_from_args(args, spec)
    search_metadata = optimizer_search_metadata(args)
    search_mode = search_metadata.get("search_mode") or "adaptive_refinement"
    resume_payload = optimizer_report_payload(args.resume_report)
    resume_objective_names = tuple(
        (resume_payload.get("multi_objective_optimization") or {}).get("objective_names") or []
    )
    objective_schema_changed = bool(
        resume_objective_names and resume_objective_names != tuple(MULTI_OBJECTIVE_NAMES)
    )
    rows = [
        refresh_resume_row_for_current_spec(spec, row)
        for row in optimizer_resume_rows(args.resume_report)
        if isinstance(row, dict)
    ]
    resume_constraint_refresh_changed = any(
        truthy(row.get("resume_constraint_refresh_changed"))
        for row in rows
    )
    backfill_case_events(rows, args.output_dir, args.stem)
    profile_order = {"publication_safe": 0, "purity_feasibility": 1, "aggressive_feasibility": 2}
    resume_profile = str(
        (resume_payload.get("range_policy") or {}).get("bounds_profile")
        or (resume_payload.get("boundary_diagnosis") or {}).get("bounds_profile")
        or ""
    ).strip()
    current_profile = str(bounds.get("profile") or getattr(args, "bounds_profile", "") or "").strip()
    if (
        resume_profile in profile_order
        and current_profile in profile_order
        and profile_order[resume_profile] > profile_order[current_profile]
    ):
        resume_args = argparse.Namespace(**vars(args))
        resume_args.bounds_profile = resume_profile
        bounds = optimizer_bounds_from_args(resume_args, spec)
        search_metadata["resume_bounds_profile_adopted"] = resume_profile
        search_metadata["requested_bounds_profile"] = current_profile
    previous_convergence = resume_payload.get("convergence") or {}
    convergence_history = [
        dict(item) for item in (previous_convergence.get("history") or [])
        if isinstance(item, dict)
    ]
    maintenance_events = []
    optimizer_baseline_aspen_pids = aspen_related_pids()
    optimizer_baseline_aspen_primary_pids = aspen_automation_pids()
    best_convergence_snapshot = dict(previous_convergence.get("best_snapshot") or {})
    stagnant_generations = int(previous_convergence.get("stagnant_generations") or 0)
    stop_reason = "converged" if truthy(previous_convergence.get("converged")) else ""
    if args.reset_resume_convergence or resume_constraint_refresh_changed:
        convergence_history = []
        best_convergence_snapshot = {}
        stagnant_generations = 0
        stop_reason = ""
    if objective_schema_changed or not convergence_history_matches_current_objectives(convergence_history):
        convergence_history = []
        best_convergence_snapshot = {}
        stagnant_generations = 0
        stop_reason = ""
    fast_runner = None
    fast_run_timeout_seconds = 0
    fast_baseline_aspen_pids = set()
    if args.fast_inplace:
        if not args.fast_template_bkp:
            raise SystemExit("--fast-inplace requires --fast-template-bkp pointing to a stable validated .bkp archive.")
        fast_template_bkp = Path(args.fast_template_bkp)
        if not fast_template_bkp.exists():
            raise SystemExit("Fast in-place template BKP not found: {}".format(fast_template_bkp))
        from aspen_runner import AspenCaseRunner
        fast_baseline_aspen_pids = set(optimizer_baseline_aspen_pids)
        fast_runner = AspenCaseRunner(
            template_bkp=fast_template_bkp,
            case_work_dir=Path(args.output_dir) / "fast_session_case_files",
            visible=False,
        )
        fast_run_timeout_seconds = max(0, int(args.fast_run_timeout_seconds or args.case_timeout_seconds or 0))

    def refresh_adaptive_bounds(reason=""):
        nonlocal bounds, search_metadata
        current_profile = bounds.get("profile", "")
        diagnosis = constraint_feasibility_diagnosis(spec, rows, bounds)
        adjusted = adaptive_constraint_repair_bounds(spec, rows, bounds)
        if adjusted.get("profile") != bounds.get("profile"):
            search_metadata["requested_bounds_profile"] = bounds.get("profile", "")
            search_metadata["adaptive_bounds_profile"] = adjusted.get("profile", "")
            search_metadata["adaptive_bounds_reason"] = reason or "constraint_feasibility_diagnosis"
            maintenance_events.append({
                "event": "adaptive_bounds_profile_changed",
                "reason": reason or "constraint_feasibility_diagnosis",
                "from_profile": current_profile,
                "to_profile": adjusted.get("profile", ""),
                "diagnosis_status": diagnosis.get("status", ""),
                "recommended_action": diagnosis.get("recommended_action", ""),
                "best_purity_case_id": diagnosis.get("best_purity_case_id", ""),
                "best_purity": diagnosis.get("best_purity", ""),
                "best_recovery": diagnosis.get("best_recovery", ""),
                "best_boundary_hits": diagnosis.get("best_boundary_hits", []),
                "at": dt.datetime.now().isoformat(timespec="seconds"),
            })
            bounds = adjusted
        return bounds

    refresh_adaptive_bounds("initial_or_resume_constraint_repair")

    def write_current_outputs():
        return write_outputs(
            rows,
            args.output_dir,
            args.stem,
            spec=spec,
            convergence=convergence,
            maintenance_events=maintenance_events,
            bounds=bounds,
            search_metadata=search_metadata,
        )

    def write_lightweight_current(latest_row=None, reason="case_completed"):
        return write_lightweight_outputs(
            rows,
            args.output_dir,
            args.stem,
            spec=spec,
            convergence=convergence,
            maintenance_events=maintenance_events,
            bounds=bounds,
            search_metadata=search_metadata,
            latest_row=latest_row,
            refresh_reason=reason,
        )

    def record_case_row(row, reason="case_completed"):
        append_case_event(row, args.output_dir, args.stem)
        return write_lightweight_current(row, reason=reason)

    def should_full_refresh_after_case(launched_cases):
        interval = max(0, int(args.full_refresh_interval or 0))
        return bool(interval and launched_cases > 0 and launched_cases % interval == 0)

    def maintenance_after_case(case_id, generation, launched_cases):
        nonlocal stop_reason
        if fast_runner is not None:
            restart_interval = max(0, int(args.fast_session_restart_interval or 0))
            if (
                restart_interval > 0
                and fast_runner.has_open_session
                and fast_runner.session_case_count >= restart_interval
            ):
                event = {
                    "event": "fast_inplace_session_restart",
                    "after_launched_cases": launched_cases,
                    "restart_interval": restart_interval,
                    "case_id": case_id,
                    "generation": generation,
                    "session_case_count": fast_runner.session_case_count,
                }
                fast_runner.close_session()
                cleanup_event = cleanup_new_aspen_related_processes(
                    fast_baseline_aspen_pids,
                    settle_seconds=args.aspen_cleanup_settle_seconds,
                )
                event["cleanup"] = cleanup_event
                maintenance_events.append(event)
                return event
            return None
        batch_size = max(0, int(args.aspen_cleanup_batch_size or 0))
        if not batch_size or launched_cases <= 0 or launched_cases % batch_size:
            return None
        event = cleanup_new_aspen_related_processes(
            optimizer_baseline_aspen_pids,
            settle_seconds=args.aspen_cleanup_settle_seconds,
        )
        event.update({
            "event": "aspen_batch_cleanup",
            "after_launched_cases": launched_cases,
            "batch_size": batch_size,
            "case_id": case_id,
            "generation": generation,
        })
        maintenance_events.append(event)
        cooldown = max(0, int(args.aspen_cleanup_cooldown_seconds or 0))
        if cooldown:
            time.sleep(cooldown)
        annotate_support_only_aspen_residue(event)
        if cleanup_event_has_primary_aspen_residue(event):
            event["unsafe_after_cleanup"] = True
            stop_reason = stop_reason or "aspen_process_cleanup_failed"
        return event

    def maintenance_before_case(case_id, generation, launched_cases):
        nonlocal stop_reason
        limit = max(1, int(args.aspen_process_limit or 1))
        total_primary_before = aspen_automation_pids()
        if fast_runner is not None:
            keep_limit = limit if fast_runner.has_open_session else max(0, limit - 1)
            cleanup_needed = len(total_primary_before) > keep_limit
            cleanup_target = keep_limit
        else:
            cleanup_needed = len(total_primary_before) >= limit
            cleanup_target = max(0, limit - 1)
        if cleanup_needed:
            event = cleanup_aspen_automation_over_limit(
                process_limit=cleanup_target,
                min_age_seconds=90,
                reason="pre_case_process_limit",
            )
            event.update({
                "before_case_id": case_id,
                "before_generation": generation,
                "after_launched_cases": launched_cases,
                "fast_inplace": fast_runner is not None,
                "fast_session_open": bool(fast_runner is not None and fast_runner.has_open_session),
            })
            maintenance_events.append(event)
            remaining_count = len(aspen_automation_pids())
            if (fast_runner is not None and remaining_count > keep_limit) or (
                fast_runner is None and remaining_count >= limit
            ):
                event["unsafe_before_case"] = True
                stop_reason = stop_reason or "aspen_process_limit_cleanup_failed"
                return event
        if fast_runner is not None:
            return None
        live_primary = aspen_automation_pids() - optimizer_baseline_aspen_primary_pids
        live_related = aspen_related_pids() - optimizer_baseline_aspen_pids
        if not live_primary and not live_related:
            return None
        event = cleanup_new_aspen_related_processes(
            optimizer_baseline_aspen_pids,
            settle_seconds=args.aspen_cleanup_settle_seconds,
        )
        event.update({
            "event": "aspen_pre_case_limit_cleanup",
            "before_case_id": case_id,
            "before_generation": generation,
            "after_launched_cases": launched_cases,
            "batch_size": max(0, int(args.aspen_cleanup_batch_size or 0)),
            "process_limit": limit,
            "live_aspen_pid_count_before_cleanup": len(live_related),
            "live_aspen_primary_pid_count_before_cleanup": len(live_primary),
        })
        maintenance_events.append(event)
        cooldown = max(0, int(args.aspen_cleanup_cooldown_seconds or 0))
        if cooldown:
            time.sleep(cooldown)
        annotate_support_only_aspen_residue(event)
        if cleanup_event_has_primary_aspen_residue(event):
            event["unsafe_before_case"] = True
            stop_reason = stop_reason or "aspen_process_limit_cleanup_failed"
        return event

    def record_convergence(generation):
        nonlocal best_convergence_snapshot, stagnant_generations, stop_reason
        snapshot = multi_objective_convergence_snapshot(rows, generation=generation)
        improved = convergence_snapshot_improved(
            best_convergence_snapshot,
            snapshot,
            args.convergence_score_epsilon,
            args.convergence_objective_rel_epsilon,
            args.convergence_objective_abs_epsilon,
        )
        snapshot["improved"] = bool(improved)
        if improved:
            best_convergence_snapshot = dict(snapshot)
            stagnant_generations = 0
        else:
            stagnant_generations += 1
        snapshot["stagnant_generations"] = stagnant_generations
        convergence_history.append(snapshot)
        if int(generation or 0) >= int(args.min_generations or 0) and stagnant_generations >= int(args.convergence_patience or 0):
            if str(snapshot.get("status") or "") == "complete":
                stop_reason = "converged"
            elif int(snapshot.get("measured_case_count") or 0) > 0 and int(snapshot.get("feasible_case_count") or 0) <= 0:
                adjusted = adaptive_constraint_repair_bounds(spec, rows, bounds)
                if adjusted.get("profile") != bounds.get("profile"):
                    snapshot["stop_deferred_for_bounds_adjustment"] = True
                    snapshot["deferred_bounds_profile"] = adjusted.get("profile", "")
                else:
                    stop_reason = "purity_constraint_not_met"
        return convergence_summary_payload(
            convergence_history,
            best_convergence_snapshot,
            stagnant_generations,
            stop_reason,
            args.min_generations,
            args.convergence_patience,
            args.convergence_score_epsilon,
            args.convergence_objective_rel_epsilon,
            args.convergence_objective_abs_epsilon,
        )

    def evaluate_case(case, generation):
        if fast_runner is not None:
            return run_case_fast_inplace(
                spec,
                case,
                args.output_dir,
                fast_runner,
                generation=generation,
                session_id=args.fast_session_id,
                run_timeout_seconds=fast_run_timeout_seconds,
                save_validated_bkp=args.fast_save_validated_bkp,
            )
        return run_case_with_timeout(
            spec,
            case,
            args.output_dir,
            generation=generation,
            timeout_seconds=args.case_timeout_seconds,
        )

    convergence = convergence_summary_payload(
        convergence_history,
        best_convergence_snapshot,
        stagnant_generations,
        stop_reason,
        args.min_generations,
        args.convergence_patience,
        args.convergence_score_epsilon,
        args.convergence_objective_rel_epsilon,
        args.convergence_objective_abs_epsilon,
    )
    required_output_missing_streak = 0

    def should_stop_for_required_outputs_missing(row):
        nonlocal required_output_missing_streak, stop_reason
        if not row_has_required_outputs_missing(row):
            required_output_missing_streak = 0
            return False
        required_output_missing_streak += 1
        stop_streak = int(args.required_output_missing_stop_streak or 0)
        if stop_streak <= 0:
            return False
        has_any_closed_loop_pass = any(row_is_closed_loop_pass(item) for item in rows)
        if (
            required_output_missing_streak >= stop_streak
            and not has_any_closed_loop_pass
        ):
            stop_reason = "aspen_required_outputs_missing_streak"
            return True
        return False

    seed_row = seed_row_from_zero_report(args.seed_report, spec) if (not rows and int(args.start_index or 1) <= 1) else None
    if seed_row:
        rows.append(seed_row)
    if not stop_reason and not (args.stop_after_pass and rows and row_is_closed_loop_pass(rows[-1])):
        start_index = max(1, int(args.start_index or 1))
        case_budget = max(0, int(args.max_cases or 0))
        launched_cases = 0
        resume_active = bool(args.resume_report and rows)
        resume_generation = max_generation_index(rows) if resume_active else 0
        if not resume_active:
            initial_rng = random.Random(int(args.random_seed or 0))
            if search_mode == "nsga2_hybrid":
                cases = genetic_initial_cases(spec, args.population_size, bounds=bounds, rng=initial_rng)[start_index - 1:]
            else:
                cases = candidate_cases(
                    spec,
                    args.population_size,
                    bounds=bounds,
                    prioritize_control_family_coverage=args.repair_probe_cover_control_families,
                )[start_index - 1:]
            if case_budget > 0:
                cases = cases[:case_budget]
            if rows and start_index <= 1:
                cases = cases[len(rows):]
            for case in cases:
                if case_budget > 0 and launched_cases >= case_budget:
                    break
                maintenance_before_case(case.get("case_id"), 0, launched_cases)
                if stop_reason:
                    break
                row = evaluate_case(case, 0)
                launched_cases += 1
                rows.append(row)
                record_case_row(row)
                if should_full_refresh_after_case(launched_cases):
                    report = write_current_outputs()
                    rows = report.get("rows") or rows
                if maintenance_after_case(case.get("case_id"), 0, launched_cases):
                    write_lightweight_current(row, reason="maintenance_after_case")
                previous_bounds_profile = bounds.get("profile", "")
                refresh_adaptive_bounds("after_case_{}_constraint_repair".format(row.get("case_id") or case.get("case_id") or "unknown"))
                if bounds.get("profile", "") != previous_bounds_profile:
                    write_lightweight_current(row, reason="adaptive_bounds_profile_changed")
                    break
                if stop_reason:
                    break
                if should_stop_for_required_outputs_missing(row):
                    break
                if args.stop_after_pass and row_is_closed_loop_pass(row):
                    break
        if resume_active and convergence_history:
            convergence = convergence_summary_payload(
                convergence_history,
                best_convergence_snapshot,
                stagnant_generations,
                stop_reason,
                args.min_generations,
                args.convergence_patience,
                args.convergence_score_epsilon,
                args.convergence_objective_rel_epsilon,
                args.convergence_objective_abs_epsilon,
            )
        else:
            convergence = record_convergence(resume_generation)
        write_lightweight_current(rows[-1] if rows else None, reason="resume_state")
        if needs_full_ranking_refresh(rows):
            report = write_current_outputs()
            rows = report.get("rows") or rows
        first_generation = max(1, resume_generation + 1) if resume_active else 1
        generation_limit = max(0, int(args.generations or 0))
        generation = first_generation
        while True:
            if not args.run_until_converged and generation > generation_limit:
                break
            if args.run_until_converged and generation_limit > 0 and generation > generation_limit:
                stop_reason = stop_reason or "safety_generation_cap_reached"
                break
            if stop_reason:
                break
            if args.stop_after_pass and rows and row_is_closed_loop_pass(rows[-1]):
                break
            refresh_adaptive_bounds("generation_{}_constraint_repair".format(generation))
            if case_budget > 0 and launched_cases >= case_budget:
                stop_reason = stop_reason or ("safety_case_cap_reached" if args.run_until_converged else "case_budget_reached")
                break
            next_index = max_case_index(rows) + 1
            generation_size = int(args.population_size or 1)
            if case_budget > 0:
                generation_size = min(generation_size, case_budget - launched_cases)
            if search_mode == "nsga2_hybrid":
                generation_rng = random.Random(int(args.random_seed or 0) + int(generation or 0) * 100003 + int(next_index or 0))
                cases = nsga2_hybrid_cases(
                    spec,
                    rows,
                    generation,
                    generation_size,
                    next_index,
                    bounds=bounds,
                    rng=generation_rng,
                    crossover_rate=args.crossover_rate,
                    mutation_rate=args.mutation_rate,
                )
            else:
                cases = refinement_cases(spec, rows, generation, generation_size, next_index, bounds=bounds)
            if not cases:
                stop_reason = stop_reason or "no_refinement_cases"
                convergence = record_convergence(generation)
                break
            for case in cases:
                if case_budget > 0 and launched_cases >= case_budget:
                    stop_reason = stop_reason or ("safety_case_cap_reached" if args.run_until_converged else "case_budget_reached")
                    break
                maintenance_before_case(case.get("case_id"), generation, launched_cases)
                if stop_reason:
                    break
                row = evaluate_case(case, generation)
                launched_cases += 1
                rows.append(row)
                record_case_row(row)
                if should_full_refresh_after_case(launched_cases):
                    report = write_current_outputs()
                    rows = report.get("rows") or rows
                if maintenance_after_case(case.get("case_id"), generation, launched_cases):
                    write_lightweight_current(row, reason="maintenance_after_case")
                previous_bounds_profile = bounds.get("profile", "")
                refresh_adaptive_bounds("after_case_{}_constraint_repair".format(row.get("case_id") or case.get("case_id") or "unknown"))
                if bounds.get("profile", "") != previous_bounds_profile:
                    write_lightweight_current(row, reason="adaptive_bounds_profile_changed")
                    break
                if stop_reason:
                    break
                if should_stop_for_required_outputs_missing(row):
                    break
                if args.stop_after_pass and row_is_closed_loop_pass(row):
                    break
            convergence = record_convergence(generation)
            if should_full_refresh_after_case(launched_cases):
                report = write_current_outputs()
                rows = report.get("rows") or rows
            else:
                write_lightweight_current(rows[-1] if rows else None, reason="generation_{}_end".format(generation))
            if stop_reason == "converged":
                break
            if args.stop_after_pass and rows and row_is_closed_loop_pass(rows[-1]):
                break
            generation += 1
    if stop_reason and str((convergence or {}).get("stop_reason") or "") != str(stop_reason):
        convergence = convergence_summary_payload(
            convergence_history,
            best_convergence_snapshot,
            stagnant_generations,
            stop_reason,
            args.min_generations,
            args.convergence_patience,
            args.convergence_score_epsilon,
            args.convergence_objective_rel_epsilon,
            args.convergence_objective_abs_epsilon,
        )
    if not stop_reason:
        stop_reason = "max_generation_reached"
        convergence = convergence_summary_payload(
            convergence_history,
            best_convergence_snapshot,
            stagnant_generations,
            stop_reason,
            args.min_generations,
            args.convergence_patience,
            args.convergence_score_epsilon,
            args.convergence_objective_rel_epsilon,
            args.convergence_objective_abs_epsilon,
        )
    if fast_runner is not None:
        fast_runner.close_session()
        event = cleanup_new_aspen_related_processes(
            fast_baseline_aspen_pids,
            settle_seconds=args.aspen_cleanup_settle_seconds,
        )
        event.update({
            "event": "fast_inplace_final_cleanup",
            "after_launched_cases": locals().get("launched_cases", 0),
        })
        maintenance_events.append(event)
    report = write_current_outputs()
    print(safe_json_dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
