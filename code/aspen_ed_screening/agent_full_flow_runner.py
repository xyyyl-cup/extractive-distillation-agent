# -*- coding: utf-8 -*-
"""
Sequential full-flow runner for the binary-separation agent.

This file is intentionally launched by full_agent_dashboard.py as an agent
subprocess. It performs guarded stage transitions; it does not bypass the
report gates that the dashboard shows to the user.
"""

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import full_agent_dashboard as agent


MAINLINE_ASPEN_PROGRESS_KEYS = (
    "optimization_solvent_alignment_status",
    "optimization_solvent_alignment_message",
    "mainline_multi_objective_converged",
    "mainline_aspen_optimization_required",
    "optimization_evidence_scope",
    "constraint_target_purity",
    "constraint_target_purity_basis",
    "constraint_target_recovery",
    "selected_molecular_solvent_code",
    "selected_molecular_solvent_label",
    "topsis_best_solvent_name",
    "topsis_best_aspen_component_id",
    "topsis_best_case_id",
    "optimization_case_count",
    "optimization_pass_case_count",
    "closed_loop_optimization_case_count",
    "closed_loop_optimization_pass_case_count",
    "multi_objective_feasible_case_count",
    "pareto_front_case_count",
    "multi_objective_converged",
    "multi_objective_optimization_complete",
    "publication_optimization_budget_complete",
)

REPAIR_PROBE_DEFAULT_CASE_COUNT = 16
REPAIR_PROBE_MAX_CASE_COUNT = 20
REPAIR_PROBE_REQUIRED_MODE_FAMILIES = (
    "calculator_inventory_control",
    "bounded_nonnegative_makeup_control",
)

# Mainline campaigns must retain the persistent Aspen session. Isolated case
# archives remain available only when the optimizer is invoked explicitly for
# diagnostics; the full-flow agent never selects that slower mode automatically.
ASPEN_MAINLINE_FAST_ONLY = True


def required_output_repair_missing_families(closed_loop_diag):
    diag = closed_loop_diag or {}
    if not isinstance(diag, dict):
        return []
    required_output_failure = (
        diag.get("status") == "closed_loop_required_outputs_missing"
        or diag.get("dominant_failure") == "required_outputs_missing"
    )
    if not required_output_failure:
        return []
    counts = diag.get("recycle_control_mode_counts") or {}
    if not isinstance(counts, dict):
        counts = {}

    def mode_count(mode):
        try:
            return int(counts.get(mode) or 0)
        except Exception:
            return 0

    missing = []
    explicit = diag.get("untested_repair_strategies") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    for strategy in explicit:
        strategy = str(strategy or "").strip()
        if strategy in REPAIR_PROBE_REQUIRED_MODE_FAMILIES and strategy not in missing:
            missing.append(strategy)

    closed_loop_case_count = int(diag.get("closed_loop_case_count") or 0)
    if closed_loop_case_count > 0:
        calculator_count = mode_count("calculator_inventory_control")
        bounded_count = mode_count("bounded_nonnegative_makeup_control")
        if calculator_count <= 0 and "calculator_inventory_control" not in missing:
            missing.append("calculator_inventory_control")
        if calculator_count > 0 and bounded_count <= 0 and "bounded_nonnegative_makeup_control" not in missing:
            missing.append("bounded_nonnegative_makeup_control")
    return missing


def required_output_repair_can_continue(closed_loop_diag):
    return bool(required_output_repair_missing_families(closed_loop_diag))


def closed_loop_aspen_rpc_failure(closed_loop_diag):
    diag = closed_loop_diag or {}
    if not isinstance(diag, dict):
        return False
    status_rpc_failure = diag.get("status") == "closed_loop_aspen_rpc_failure"
    dominant_rpc_failure = diag.get("dominant_failure") == "aspen_com_rpc_failure"
    try:
        rpc_failure_count = int(diag.get("aspen_rpc_failure_count") or 0)
    except Exception:
        rpc_failure_count = 0
    if not (status_rpc_failure or dominant_rpc_failure or rpc_failure_count > 0):
        return False
    trusted_pass_count = 0
    for key in (
        "closed_loop_pass_case_count",
        "diagnostic_closed_loop_pass_case_count",
        "formal_feasible_case_count",
    ):
        try:
            trusted_pass_count += int(diag.get(key) or 0)
        except Exception:
            pass
    if trusted_pass_count > 0 and not diag.get("agent_should_stop_before_more_optimization"):
        return False
    return bool(
        status_rpc_failure
        or dominant_rpc_failure
        or (
            rpc_failure_count > 0
            and not diag.get("full_flow_completion_allowed")
        )
    )


def closed_loop_aspen_rpc_history(closed_loop_diag):
    diag = closed_loop_diag or {}
    if not isinstance(diag, dict):
        return {}
    status_rpc_failure = diag.get("status") == "closed_loop_aspen_rpc_failure"
    dominant_rpc_failure = diag.get("dominant_failure") == "aspen_com_rpc_failure"
    try:
        rpc_failure_count = int(diag.get("aspen_rpc_failure_count") or 0)
    except Exception:
        rpc_failure_count = 0
    try:
        pass_count = int(diag.get("closed_loop_pass_case_count") or 0)
    except Exception:
        pass_count = 0
    return {
        "status": diag.get("status") or "",
        "dominant_failure": diag.get("dominant_failure") or "",
        "aspen_rpc_failure_count": rpc_failure_count,
        "closed_loop_pass_case_count": pass_count,
        "blocks_fast_inplace": closed_loop_aspen_rpc_failure(diag),
        "history_present": bool(status_rpc_failure or dominant_rpc_failure or rpc_failure_count > 0),
    }


def attach_mainline_aspen_progress(report, process_summary):
    summary = dict(process_summary or {})
    try:
        progress = agent.aspen_progress_summary(report)
    except Exception:
        progress = {}
    for key in MAINLINE_ASPEN_PROGRESS_KEYS:
        if key in progress:
            summary[key] = progress.get(key)
    task = (report or {}).get("separation_task") or {}
    current_target = float(task.get("target_purity") or 0.0)
    current_basis = str(task.get("target_purity_basis") or "mole_fraction").strip().lower()
    current_recovery = agent.safe_float(task.get("target_recovery"))
    evidence_target = summary.get("constraint_target_purity")
    try:
        evidence_target = float(evidence_target)
    except Exception:
        evidence_target = None
    evidence_basis = str(summary.get("constraint_target_purity_basis") or "mole_fraction").strip().lower()
    evidence_recovery = agent.safe_float(summary.get("constraint_target_recovery"))
    has_evidence = bool(int(summary.get("optimization_case_count") or 0) > 0)
    recovery_matches = (
        current_recovery is None and evidence_recovery is None
    ) or (
        current_recovery is not None
        and evidence_recovery is not None
        and abs(evidence_recovery - current_recovery) <= 1.0e-12
    )
    if not has_evidence:
        constraint_alignment = "no_evidence"
    elif evidence_target is None or (current_recovery is not None and evidence_recovery is None):
        constraint_alignment = "unknown"
    elif (
        abs(evidence_target - current_target) <= 1.0e-12
        and evidence_basis == current_basis
        and recovery_matches
    ):
        constraint_alignment = "matched"
    else:
        constraint_alignment = "mismatch"
    summary["optimization_constraint_alignment_status"] = constraint_alignment
    summary["optimization_constraint_alignment_message"] = (
        "Current purity target/basis and optional recovery constraint match the optimizer evidence."
        if constraint_alignment == "matched"
        else "Current purity target/basis or optional recovery constraint differs from optimizer evidence; mainline Aspen must refresh the campaign."
        if constraint_alignment == "mismatch"
        else "Optimizer purity/recovery constraint evidence is not available yet."
    )
    summary["current_target_purity"] = current_target
    summary["current_target_purity_basis"] = current_basis
    summary["current_target_recovery"] = current_recovery
    summary["current_target_recovery_mode"] = "hard_constraint" if current_recovery is not None else "maximize_only"
    if constraint_alignment in {"mismatch", "unknown"} and has_evidence:
        summary["mainline_aspen_optimization_required"] = True
    return summary


def import_process_summary_with_mainline(report, report_path):
    paths = report.get("research_task_package_paths") or {}
    if not paths.get("aspen_build_plan_json"):
        return report, {"status": "no_aspen_plan"}
    report, process_summary = agent.import_process_results(report, report_path)
    return report, attach_mainline_aspen_progress(report, process_summary)


def is_mainline_aspen_required(process_summary):
    alignment = str((process_summary or {}).get("optimization_solvent_alignment_status") or "").strip().lower()
    constraint_alignment = str((process_summary or {}).get("optimization_constraint_alignment_status") or "").strip().lower()
    return bool(
        alignment in {"mismatch", "unknown"}
        or constraint_alignment in {"mismatch", "unknown"}
        or (process_summary or {}).get("mainline_aspen_optimization_required")
    )


def is_mainline_aspen_closed(process_summary):
    alignment = str((process_summary or {}).get("optimization_solvent_alignment_status") or "").strip().lower()
    return bool(alignment == "matched" and (process_summary or {}).get("mainline_multi_objective_converged"))


def aspen_optimization_run_plan(task, trial_mode=False, spec_path=None):
    campaign = agent.optimization_campaign_defaults(task or {}, trial_mode=trial_mode)
    bounds_profile = str(campaign.get("bounds_profile") or "publication_safe")
    search_mode = str(campaign.get("search_mode") or "nsga2_hybrid")
    random_seed = int(
        campaign.get("random_seed")
        or (20260630 + sum((index + 1) * ord(ch) for index, ch in enumerate(str((task or {}).get("task_id") or ""))) % 100000)
    )
    variable_ranges = agent.optimizer_bounds_for_dashboard_profile(bounds_profile)
    variable_range_basis = "dashboard_profile_factor_bounds"
    if spec_path:
        try:
            import generic_aspen_once_optimizer as optimizer
            spec = optimizer.zero.load_spec(spec_path)
            args = type("Args", (), {"bounds_profile": bounds_profile})()
            variable_ranges = optimizer.serializable_bounds(optimizer.optimizer_bounds_from_args(args, spec))
            variable_range_basis = "spec_material_balance_split_kmol_per_hr"
        except Exception as exc:
            variable_ranges = dict(variable_ranges)
            variable_ranges["range_resolution_warning"] = "{}: {}".format(exc.__class__.__name__, exc)
    variable_notes = {
        "SF": "solvent/feed molar ratio",
        "RR1": "extractive column reflux ratio",
        "RR2": "solvent-recovery column reflux ratio",
        "D1": "extractive column distillate flow; optimizer converts the profile factors around the material-balance split target to kmol/h",
        "D2": "solvent-recovery column distillate flow; optimizer converts the profile factors around the material-balance split target to kmol/h",
        "N1": "extractive column stage count",
        "N2": "solvent-recovery column stage count",
        "NF1_BOTTOM_MARGIN": "minimum distance from feed stage to column bottom",
        "NF1_NS1_MIN_GAP": "minimum feed/solvent stage separation",
    }
    return {
        "campaign": campaign,
        "bounds_profile": bounds_profile,
        "search_mode": search_mode,
        "algorithm": campaign.get("algorithm") or ("NSGA-II hybrid" if search_mode == "nsga2_hybrid" else "adaptive refinement"),
        "random_seed": random_seed,
        "crossover_rate": float(campaign.get("crossover_rate") or 0.85),
        "mutation_rate": float(campaign.get("mutation_rate") or 0.18),
        "variable_ranges": variable_ranges,
        "variable_range_basis": variable_range_basis,
        "variable_notes": variable_notes,
    }


def read_json_tolerant(path):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r'(?<!\\)\\(?![\\/\"bfnrtu])', r'\\\\', text)
        return json.loads(text)


def truthy_value(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "pass"}


def optimizer_row_aspen_run_pass(row):
    for key in ("aspen_run_pass_or_fail", "aspen_status", "run_status", "pass_or_fail"):
        value = str((row or {}).get(key) or "").strip().upper()
        if value:
            return value == "PASS"
    return truthy_value((row or {}).get("aspen_run_pass"))


def optimizer_row_solvent_tokens(row):
    tokens = {
        agent.normalize_agent_token((row or {}).get("solvent_code")),
        agent.normalize_agent_token((row or {}).get("solvent_name")),
        agent.normalize_agent_token((row or {}).get("aspen_component_id")),
    }
    tokens.discard("")
    return tokens


def optimizer_row_is_probe_seed_pass(row):
    row = row or {}
    return bool(
        truthy_value(row.get("closed_loop_recycle"))
        and (
            truthy_value(row.get("final_closed_loop_pass"))
            or truthy_value(row.get("trusted_as_optimization_pass"))
            or optimizer_row_aspen_run_pass(row)
        )
    )


def optimizer_payload_rows(payload, report_path=None):
    payload = payload if isinstance(payload, dict) else {}
    rows = payload.get("rows") or []
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]
    candidates = []
    event_path = str(payload.get("case_events_jsonl") or "").strip()
    if event_path:
        candidates.append(Path(event_path))
    if report_path:
        try:
            candidates.extend((Path(report_path).parent / "optimization").glob("*.case_events.jsonl"))
        except Exception:
            pass
    seen = set()
    restored = []
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
                    event = json.loads(line)
                    row = event.get("row") if isinstance(event, dict) else None
                    if isinstance(row, dict):
                        restored.append(row)
        except Exception:
            continue
    return restored


def current_solvent_probe_pass_summary(output_dir, selected_solvent):
    report_path = Path(output_dir) / "generic_aspen_once_optimizer.report.json"
    if not report_path.exists():
        return {"pass_count": 0, "case_ids": [], "report_json": str(report_path)}
    try:
        payload = read_json_tolerant(report_path)
    except Exception as exc:
        return {"pass_count": 0, "case_ids": [], "report_json": str(report_path), "read_error": str(exc)}
    selected_tokens = {
        agent.normalize_agent_token((selected_solvent or {}).get("code")),
        agent.normalize_agent_token((selected_solvent or {}).get("aspen_component_id")),
        agent.normalize_agent_token((selected_solvent or {}).get("name")),
        agent.normalize_agent_token((selected_solvent or {}).get("solvent_name")),
    }
    selected_tokens.discard("")
    pass_rows = []
    formal_pass_rows = []
    for row in optimizer_payload_rows(payload, report_path):
        row_tokens = optimizer_row_solvent_tokens(row)
        if selected_tokens and not (selected_tokens & row_tokens):
            continue
        if str(row.get("pass_or_fail") or "").strip().upper() == "PASS":
            formal_pass_rows.append(row)
        if optimizer_row_is_probe_seed_pass(row):
            pass_rows.append(row)
    closed_loop_diag = (payload.get("closed_loop_recycle_diagnosis") or {})
    return {
        "pass_count": len(pass_rows),
        "case_ids": [row.get("case_id") for row in pass_rows if row.get("case_id")],
        "formal_pass_count": len(formal_pass_rows),
        "formal_case_ids": [row.get("case_id") for row in formal_pass_rows if row.get("case_id")],
        "report_json": str(report_path),
        "output_dir": str(Path(output_dir)),
        "case_count": payload.get("case_count"),
        "raw_pass_case_count": payload.get("pass_case_count"),
        "dominant_failure": (closed_loop_diag.get("dominant_failure") or ""),
        "closed_loop_recycle_diagnosis": closed_loop_diag,
        "missing_repair_mode_families": required_output_repair_missing_families(closed_loop_diag),
    }


def latest_current_solvent_repair_probe_summary(base_dir, selected_solvent):
    base = Path(base_dir)
    candidates = sorted(
        [path for path in base.glob("aspen_repair_probe_autopilot_*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    fallback = {}
    for candidate in candidates:
        summary = current_solvent_probe_pass_summary(candidate, selected_solvent)
        if not fallback:
            fallback = summary
        if int(summary.get("pass_count") or 0) > 0:
            return summary
    return fallback or {"pass_count": 0, "case_ids": []}


def current_solvent_optimization_resume_summary(base_dir, selected_solvent):
    base = Path(base_dir)
    selected_tokens = {
        agent.normalize_agent_token((selected_solvent or {}).get("code")),
        agent.normalize_agent_token((selected_solvent or {}).get("aspen_component_id")),
        agent.normalize_agent_token((selected_solvent or {}).get("name")),
        agent.normalize_agent_token((selected_solvent or {}).get("solvent_name")),
    }
    selected_tokens.discard("")
    candidates = sorted(
        [path for path in base.glob("aspen_optimization_once_autopilot_*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        report_json = candidate / "generic_aspen_once_optimizer.report.json"
        if not report_json.exists():
            continue
        try:
            payload = read_json_tolerant(report_json)
        except Exception:
            continue
        rows = optimizer_payload_rows(payload, report_json)
        if not rows:
            continue
        matching_rows = []
        for row in rows:
            row_tokens = optimizer_row_solvent_tokens(row)
            if not selected_tokens or selected_tokens & row_tokens:
                matching_rows.append(row)
        if not matching_rows:
            continue
        mo = payload.get("multi_objective_optimization") or {}
        closed_loop_diag = payload.get("closed_loop_recycle_diagnosis") or {}
        diagnostic_pass_count = int(closed_loop_diag.get("closed_loop_pass_case_count") or 0)
        return {
            "resume_report": str(report_json.resolve()),
            "output_dir": str(candidate.resolve()),
            "case_count": payload.get("case_count") or len(rows),
            "matching_case_count": len(matching_rows),
            "pass_case_count": payload.get("pass_case_count"),
            "closed_loop_pass_case_count": diagnostic_pass_count,
            "feasible_case_count": mo.get("feasible_case_count"),
            "pareto_front_case_count": mo.get("pareto_front_case_count"),
            "max_generation": mo.get("max_generation"),
            "converged": bool(mo.get("converged")),
            "topsis_best_case_id": mo.get("topsis_best_case_id") or "",
            "closed_loop_recycle_diagnosis": closed_loop_diag,
            "dominant_failure": closed_loop_diag.get("dominant_failure") or "",
        }
    return {}


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


def stop_processes(pids):
    for pid in sorted(set(int(item) for item in pids if item)):
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


def tail(path, limit=4000):
    path = Path(path)
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > limit:
        data = data[-limit:]
    return agent.decode_subprocess_output(data)


def status_path_for(report, report_path):
    output_dir = Path(report.get("output_dir") or Path(report_path).parent)
    package_dir = output_dir / "research_task_package"
    package_dir.mkdir(parents=True, exist_ok=True)
    return package_dir / "full_flow_autopilot_status.json"


def closed_loop_diag_from_status_payload(payload, report=None, _seen=None, _depth=0):
    if not isinstance(payload, dict) or _depth > 20:
        return {}
    _seen = _seen or set()
    payload_id = id(payload)
    if payload_id in _seen:
        return {}
    _seen.add(payload_id)
    payload = payload or {}
    for key in (
        "closed_loop_recycle_diagnosis",
        "process_import_after_repair_probe",
        "process_import_after_optimization",
        "process_import_before_optimization",
        "process_import",
    ):
        value = payload.get(key) or {}
        if key == "closed_loop_recycle_diagnosis" and isinstance(value, dict) and value:
            return value
        if isinstance(value, dict):
            diag = value.get("closed_loop_recycle_diagnosis") or {}
            if diag:
                return diag
    delegated = payload.get("delegated_full_flow_status") or {}
    if isinstance(delegated, dict):
        diag = closed_loop_diag_from_status_payload(delegated, None, _seen, _depth + 1)
        if diag:
            return diag
    report_process = ((report or {}).get("process_result_import") or {})
    if isinstance(report_process, dict):
        return report_process.get("closed_loop_recycle_diagnosis") or {}
    return {}


def normalize_required_output_repair_status(payload, report=None):
    status = dict(payload or {})
    status_text = str(status.get("status") or "").strip().lower()
    stage_status = str(status.get("stage_status") or "").strip().lower()
    active = status_text == "running" or stage_status in {"running", "planning", "started", "delegating"}
    if active:
        return status
    diag = closed_loop_diag_from_status_payload(status, report)
    missing = required_output_repair_missing_families(diag)
    if not missing:
        return status
    status["status"] = "blocked_required_outputs_missing_retryable"
    status["current_stage"] = "aspen_required_output_repair_probe"
    status["stage_status"] = "blocked"
    status["current_action"] = "continue_required_output_repair_probe"
    status["missing_repair_mode_families"] = missing
    status["closed_loop_recycle_diagnosis"] = diag
    status["message"] = (
        "Aspen required outputs are missing, but the repair probe has not covered all generic "
        "recycle-control families yet: {}. Continue the full-flow agent repair probe before "
        "declaring a final Aspen blocker."
    ).format(", ".join(missing))
    return status


def write_status(report, report_path, payload):
    payload = normalize_required_output_repair_status(payload, report)
    payload["updated_at"] = agent.iso_from_timestamp(time.time())
    path = status_path_for(report, report_path)
    agent.write_json_file(path, payload)
    report = dict(report)
    report["full_flow_autopilot_status"] = payload
    report.setdefault("research_task_package_paths", {})["full_flow_autopilot_status_json"] = str(path.resolve())
    agent.write_json_file(report_path, report)
    return path


def optimizer_progress_snapshot(optimizer_report_path):
    path = Path(optimizer_report_path or "")
    if not path.exists():
        return {}
    payload = agent.read_json_default(path, {})
    if not isinstance(payload, dict) or not payload:
        return {}
    latest = payload.get("latest_case") or {}
    if not isinstance(latest, dict):
        latest = {}
    multi_objective = payload.get("multi_objective_optimization") or {}
    if not isinstance(multi_objective, dict):
        multi_objective = {}
    convergence = payload.get("convergence") or {}
    if not isinstance(convergence, dict):
        convergence = {}

    case_count = payload.get("case_count")
    if case_count in (None, ""):
        case_count = multi_objective.get("case_count")
    feasible_count = multi_objective.get("feasible_case_count")
    if feasible_count in (None, ""):
        feasible_count = payload.get("formal_pass_case_count") or payload.get("pass_case_count")
    convergence_status = (
        convergence.get("status")
        or multi_objective.get("convergence_status")
        or ""
    )
    converged = convergence.get("converged")
    if converged is None:
        converged = multi_objective.get("converged")

    snapshot = {
        "optimizer_report_json": str(path.resolve()),
        "optimizer_report_mtime": agent.iso_from_timestamp(path.stat().st_mtime),
        "case_count": case_count,
        "latest_case_id": latest.get("case_id") or "",
        "latest_generation": latest.get("generation") or multi_objective.get("max_generation") or "",
        "latest_purity": latest.get("target_purity_value") or latest.get("target_purity_mass_frac") or latest.get("target_purity_mole_frac") or latest.get("purity") or "",
        "latest_purity_basis": latest.get("target_purity_basis") or "mole_fraction",
        "latest_recovery": latest.get("target_recovery") or latest.get("recovery") or "",
        "formal_feasible_case_count": feasible_count,
        "pareto_front_case_count": multi_objective.get("pareto_front_case_count") or "",
        "topsis_best_case_id": (
            multi_objective.get("topsis_best_case_id")
            or payload.get("topsis_best_case_id")
            or ""
        ),
        "topsis_best_score": multi_objective.get("topsis_best_score") or "",
        "convergence_status": convergence_status,
        "converged": bool(converged),
        "stagnant_generations": convergence.get("stagnant_generations") or "",
        "stop_reason": convergence.get("stop_reason") or multi_objective.get("convergence_stop_reason") or "",
    }
    parts = ["Aspen optimization is running"]
    if snapshot["case_count"] not in (None, ""):
        parts.append("case {}".format(snapshot["case_count"]))
    if snapshot["latest_case_id"]:
        parts.append("latest {}".format(snapshot["latest_case_id"]))
    if snapshot["latest_generation"] not in (None, ""):
        parts.append("generation {}".format(snapshot["latest_generation"]))
    if snapshot["formal_feasible_case_count"] not in (None, ""):
        parts.append("formal feasible {}".format(snapshot["formal_feasible_case_count"]))
    if snapshot["pareto_front_case_count"] not in (None, ""):
        parts.append("Pareto {}".format(snapshot["pareto_front_case_count"]))
    if snapshot["convergence_status"]:
        parts.append("convergence {}".format(snapshot["convergence_status"]))
    snapshot["message"] = "; ".join(parts) + "."
    return snapshot


def clear_active_command(status):
    for key in (
        "command",
        "stdout_log",
        "stderr_log",
        "stdout_tail",
        "stderr_tail",
        "returncode",
        "timeout_seconds",
    ):
        status.pop(key, None)


def run_command(
    stage,
    command,
    cwd,
    log_dir,
    status,
    report,
    report_path,
    timeout_seconds=None,
    cleanup_new_aspen=False,
    progress_report_path=None,
    progress_poll_seconds=30,
):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = log_dir / "{}.out.log".format(stage)
    stderr_log = log_dir / "{}.err.log".format(stage)
    status["current_stage"] = stage
    status["stage_status"] = "running"
    status["command"] = command
    status["stdout_log"] = str(stdout_log.resolve())
    status["stderr_log"] = str(stderr_log.resolve())
    write_status(report, report_path, status)
    baseline_aspen = aspen_automation_pids() if cleanup_new_aspen else set()
    timed_out = False
    with stdout_log.open("wb") as out, stderr_log.open("wb") as err:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=out, stderr=err)
        deadline = time.time() + float(timeout_seconds) if timeout_seconds else None
        next_progress_sync = time.time() + max(5, int(progress_poll_seconds or 30))
        while True:
            returncode = process.poll()
            if returncode is not None:
                break
            if deadline is not None and time.time() >= deadline:
                timed_out = True
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=15)
                except Exception:
                    pass
                returncode = -9
                if cleanup_new_aspen:
                    cleanup_new_aspen_processes(baseline_aspen, settle_seconds=8)
                break
            if time.time() >= next_progress_sync:
                status["stdout_tail"] = tail(stdout_log, limit=2000)
                status["stderr_tail"] = tail(stderr_log, limit=2000)
                if progress_report_path:
                    progress = optimizer_progress_snapshot(progress_report_path)
                    if progress:
                        status["aspen_optimization_progress"] = progress
                        status["optimizer_report_json"] = progress.get("optimizer_report_json") or ""
                        status["message"] = progress.get("message") or status.get("message") or ""
                write_status(report, report_path, status)
                next_progress_sync = time.time() + max(5, int(progress_poll_seconds or 30))
            time.sleep(1)
    if cleanup_new_aspen:
        cleanup_new_aspen_processes(baseline_aspen, settle_seconds=8)
    status["stage_status"] = "timeout" if timed_out else ("done" if returncode == 0 else "failed")
    status["returncode"] = returncode
    if timed_out:
        status["timeout_seconds"] = timeout_seconds
        status["message"] = "{} exceeded timeout and was stopped by the agent.".format(stage)
    status["stdout_tail"] = tail(stdout_log)
    status["stderr_tail"] = tail(stderr_log)
    write_status(report, report_path, status)
    return returncode


def sha256_file(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recover_zero_build_report_from_archive(spec_path, input_deck, output_bkp, report_path, reason="", command_status=None):
    output_bkp = Path(output_bkp)
    report_path = Path(report_path)
    if not output_bkp.exists() or output_bkp.stat().st_size <= 0:
        return None
    spec_path = Path(spec_path)
    input_deck = Path(input_deck)
    try:
        spec = agent.read_json(spec_path) if spec_path.exists() else {}
    except Exception:
        spec = {}
    report = {
        "agent": "zero_build_aspen_agent",
        "generated_at": agent.iso_from_timestamp(time.time()),
        "generation_mode": "recovered_existing_archive_after_zero_build_timeout",
        "template_bkp_used_as_output_carrier": False,
        "input_deck": str(input_deck.resolve()) if input_deck.exists() else str(input_deck),
        "generated_bkp": str(output_bkp.resolve()),
        "spec": str(spec_path.resolve()) if spec_path.exists() else str(spec_path),
        "task_id": spec.get("task_id", ""),
        "validation_role": spec.get("validation_role", ""),
        "validation_source": spec.get("validation_source", ""),
        "recovery": {
            "status": "archive_generated_report_recovered",
            "reason": reason or "zero-build command did not finish cleanly, but the Aspen BKP archive exists",
            "command_status": command_status or {},
            "smoke_result_available": False,
            "policy": "Reuse only as a fast Aspen template; do not count as a completed optimization or smoke PASS.",
        },
        "static_verification": {
            "status": "not_rerun_after_timeout",
            "reason": "A COM static verification would reopen Aspen; the optimizer will validate real cases from this BKP.",
        },
    }
    if input_deck.exists():
        report["input_deck_sha256"] = sha256_file(input_deck)
    report["generated_bkp_sha256"] = sha256_file(output_bkp)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    agent.write_json_file(report_path, report)
    return report


def read_rows(path):
    if not path:
        return []
    path = Path(path)
    if not path.exists() or path.is_dir():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


GAUSSIAN_STAGE_ORDER = {
    "component_monomer_opt": 0,
    "solvent_monomer_opt": 1,
    "counterpoise_dimer_optimization": 2,
    "counterpoise_dimer_single_point": 2,
}


def gaussian_job_complete(row):
    if row.get("status") != "input_generated":
        return False
    parsed = agent.gaussian_log_summary(
        row.get("expected_log") or "",
        input_file=row.get("input_file"),
        enforce_input_mtime=row.get("job_type") in {"counterpoise_dimer_single_point", "counterpoise_dimer_optimization"},
    )
    return bool(parsed.get("normal_termination"))


def gaussian_row_in_scope(row, primary_solvent):
    if not primary_solvent:
        return True
    if row.get("job_type") == "component_monomer_opt":
        return True
    return agent.row_matches_primary_validation_solvent(row, primary_solvent)


def gaussian_queue_status(paths, primary_solvent=None):
    rows = [
        row for row in read_rows(paths.get("gaussian_jobs_csv", ""))
        if row.get("status") == "input_generated"
        and gaussian_row_in_scope(row, primary_solvent or {})
    ]
    rows = sorted(
        rows,
        key=lambda row: (
            GAUSSIAN_STAGE_ORDER.get(row.get("job_type"), 9),
            row.get("component") or "",
            row.get("solvent_code") or "",
            row.get("job_id") or "",
        ),
    )
    completed = []
    pending = []
    for row in rows:
        if gaussian_job_complete(row):
            completed.append(row)
        else:
            pending.append(row)
    monomer_types = {"component_monomer_opt", "solvent_monomer_opt"}
    pending_monomers = [row for row in pending if row.get("job_type") in monomer_types]
    pending_dimers = [row for row in pending if row.get("job_type") in {"counterpoise_dimer_single_point", "counterpoise_dimer_optimization"}]
    next_job = pending[0] if pending else {}
    return {
        "total": len(rows),
        "completed_count": len(completed),
        "pending_count": len(pending),
        "pending_monomer_count": len(pending_monomers),
        "pending_dimer_count": len(pending_dimers),
        "next_job_id": next_job.get("job_id", ""),
        "next_job_type": next_job.get("job_type", ""),
        "next_input_file": next_job.get("input_file", ""),
    }


def has_completed_gaussian(paths, primary_solvent=None):
    summary = gaussian_queue_status(paths, primary_solvent=primary_solvent)
    return bool(summary.get("total")) and summary.get("pending_count", 0) == 0


def nci_vmd_queue_status(paths, primary_solvent=None):
    rows = read_rows(paths.get("nci_vmd_jobs_csv", ""))
    if primary_solvent:
        rows = [row for row in rows if agent.row_matches_primary_validation_solvent(row, primary_solvent or {})]
    evidence_csv = paths.get("molecular_evidence_import_csv", "")
    if not evidence_csv and paths.get("molecular_inputs_dir"):
        evidence_csv = str(Path(paths.get("molecular_inputs_dir")) / "molecular_evidence_import.csv")
    evidence = [
        row for row in read_rows(evidence_csv)
        if row.get("evidence_type") == "nci_vmd"
        and row.get("status") in {"imported", "rendered_tga"}
    ]
    if primary_solvent:
        evidence = [row for row in evidence if agent.row_matches_primary_validation_solvent(row, primary_solvent or {})]
    completed_job_ids = {row.get("job_id") for row in evidence if row.get("job_id")}
    ready_rows = [row for row in rows if row.get("source_fch") and Path(row.get("source_fch")).exists()]
    return {
        "total": len(rows),
        "ready_fch_count": len(ready_rows),
        "completed_count": len(completed_job_ids),
        "pending_count": max(0, len(rows) - len(completed_job_ids)),
        "completed_job_ids": sorted(completed_job_ids),
        "next_job_id": next((row.get("job_id") for row in rows if row.get("job_id") not in completed_job_ids), ""),
    }


def primary_molecular_gate_passed(paths, primary_solvent):
    candidate_csv = paths.get("candidate_molecular_evidence_import_csv")
    if not candidate_csv:
        return False
    strict_rows = strict_gromacs_evidence_rows(paths)
    strict_for_primary = [
        row for row in strict_rows
        if agent.row_matches_primary_validation_solvent(row, primary_solvent or {})
    ]
    if len(strict_for_primary) < 2:
        return False
    for row in read_rows(candidate_csv):
        if row.get("molecular_evidence_status") not in {
            "gaussian_bsse_nci_and_gromacs_pair_imported",
            "gaussian_bsse_and_gromacs_pair_imported",
            "gaussian_and_gromacs_pair_imported",
        }:
            continue
        if agent.row_matches_primary_validation_solvent(row, primary_solvent or {}):
            return True
    return False


def strict_gromacs_evidence_rows(paths):
    evidence_csv = paths.get("molecular_evidence_import_csv", "")
    if not evidence_csv and paths.get("molecular_inputs_dir"):
        evidence_csv = str(Path(paths.get("molecular_inputs_dir")) / "molecular_evidence_import.csv")
    strict_rows = []
    for row in read_rows(evidence_csv):
        if row.get("evidence_type") != "gromacs":
            continue
        if row.get("status") == "historical_result_available":
            strict_rows.append(row)
            continue
        if row.get("status") != "imported":
            continue
        strict_flag = str(row.get("gromacs_strict_md_complete") or "").strip().lower()
        if strict_flag in {"true", "1", "yes"}:
            strict_rows.append(row)
    return strict_rows


def full_gromacs_gate_passed(paths):
    jobs = read_rows(paths.get("gromacs_jobs_csv", ""))
    if not jobs:
        return False
    completed_job_ids = {row.get("job_id") for row in strict_gromacs_evidence_rows(paths) if row.get("job_id")}
    return bool(completed_job_ids) and all(row.get("job_id") in completed_job_ids for row in jobs if row.get("job_id"))


def strict_molecular_gate_status(paths, primary_solvent, molecular_scope_solvent=None, run_full_molecular_scope=False):
    """Current-system molecular evidence must finish before Aspen optimization."""
    gaussian_status = gaussian_queue_status(paths, primary_solvent=molecular_scope_solvent or primary_solvent)
    nci_status = nci_vmd_queue_status(paths, primary_solvent=molecular_scope_solvent or primary_solvent)
    if run_full_molecular_scope:
        evidence_ready = full_gromacs_gate_passed(paths)
    else:
        evidence_ready = bool(primary_solvent and primary_molecular_gate_passed(paths, primary_solvent))
    blockers = []
    if gaussian_status.get("total") and gaussian_status.get("pending_count", 0) > 0:
        blockers.append("gaussian_queue_pending")
    if nci_status.get("total") and nci_status.get("pending_count", 0) > 0:
        blockers.append("nci_vmd_queue_pending")
    if not evidence_ready:
        blockers.append("candidate_molecular_evidence_not_imported")
    return {
        "ready": bool(evidence_ready and not blockers),
        "evidence_ready": bool(evidence_ready),
        "blockers": blockers,
        "gaussian_queue": gaussian_status,
        "nci_vmd_queue": nci_status,
        "run_full_molecular_scope": bool(run_full_molecular_scope),
    }


def relative_volatility_summary(report):
    brief = (report or {}).get("system_research_brief") or {}
    pool = brief.get("candidate_solvent_pool") or {}
    return brief.get("relative_volatility_prescreen") or pool.get("relative_volatility_prescreen") or {}


def alpha_prescreen_has_pass(summary):
    status = str((summary or {}).get("status") or "").strip().lower()
    backend = str((summary or {}).get("backend") or "").strip().lower()
    pass_count = int((summary or {}).get("pass_count") or 0)
    full_required = bool((summary or {}).get("alpha_full_screen_required", True))
    full_complete = bool((summary or {}).get("alpha_full_screen_complete", False))
    if full_required and not full_complete:
        return False
    return status == "pass_candidates_available" and pass_count > 0 and backend == "aspen_kvalue_result_import"


def ensure_relative_volatility_prescreen(report, report_path, status, stage):
    task = report.get("separation_task") or {}
    report = agent.enrich_trial_report_payload(report)
    output_dir = Path(report.get("output_dir") or Path(report_path).parent)
    old_paths = dict(report.get("research_task_package_paths") or {})
    paths = agent.export_research_task_package(report, output_dir)
    if old_paths.get("full_flow_autopilot_status_json"):
        paths["full_flow_autopilot_status_json"] = old_paths["full_flow_autopilot_status_json"]
    report["research_task_package_paths"] = paths
    report = agent.enrich_trial_report_payload(report)
    agent.write_json_file(report_path, report)
    summary = relative_volatility_summary(report)
    if alpha_prescreen_has_pass(summary):
        return report, summary, 0
    script = agent.BASE_DIR / "aspen_relative_volatility_flash.py"
    if not script.exists():
        return report, summary, 3
    request_rows = agent.read_csv_rows(paths.get("relative_volatility_kvalue_request_csv"))
    request_count = len(request_rows)
    request_candidate_count = len({
        agent.component_token(row.get("solvent_code") or row.get("code") or row.get("aspen_component_id"))
        for row in request_rows
        if agent.component_token(row.get("solvent_code") or row.get("code") or row.get("aspen_component_id"))
    })
    timeout_seconds = max(1800, min(14400, (request_count + 1) * 180))
    stage(
        "relative_volatility_flash",
        "Running Aspen property flash/VLE K-value prescreen before any Gaussian/GROMACS stage.",
        {
            "relative_volatility_before": summary,
            "request_count": request_count,
            "timeout_seconds": timeout_seconds,
        },
    )
    rc = run_command(
        "relative_volatility_flash",
        [
            sys.executable,
            str(script),
            "--report",
            str(Path(report_path).resolve()),
            "--max-candidates",
            str(max(1, request_candidate_count)),
        ],
        agent.BASE_DIR,
        output_dir / "relative_volatility_flash_logs",
        status,
        report,
        report_path,
        timeout_seconds=timeout_seconds,
        cleanup_new_aspen=True,
    )
    report = agent.read_json(report_path)
    report = agent.enrich_trial_report_payload(report)
    agent.write_json_file(report_path, report)
    summary = relative_volatility_summary(report)
    if rc != 0 and not alpha_prescreen_has_pass(summary):
        status["relative_volatility_prescreen"] = summary
        return report, summary, rc
    return report, summary, 0


def run_full_flow(report_path, max_gaussian_jobs=None, trial_mode=False, solvent_code=""):
    report_path = Path(report_path).resolve()
    report = agent.read_json(report_path)
    task = report.get("separation_task") or {}
    task_id = task.get("task_id") or ""
    solvent_code = (solvent_code or "").strip()
    status = {
        "mode": "agent_publication_trial_autopilot" if trial_mode else "agent_full_flow_autopilot",
        "status": "running",
        "task_id": task_id,
        "trial_mode": bool(trial_mode),
        "max_gaussian_jobs": int(max_gaussian_jobs if max_gaussian_jobs is not None else agent.GAUSSIAN_DEFAULT_MAX_JOBS),
        "solvent_lock": solvent_code,
        "started_at": agent.iso_from_timestamp(time.time()),
        "stages": [],
    }
    max_gaussian_jobs = int(status["max_gaussian_jobs"] or 0)
    write_status(report, report_path, status)

    def stage(name, message, extra=None):
        item = {
            "stage": name,
            "message": message,
            "at": agent.iso_from_timestamp(time.time()),
        }
        if extra:
            item.update(extra)
        status["stages"].append(item)
        status["current_stage"] = name
        status["message"] = message
        write_status(report, report_path, status)

    try:
        report = agent.enrich_trial_report_payload(report)
        agent.write_json_file(report_path, report)
        report, molecular_summary = agent.import_molecular_results(report, report_path)
        report, process_summary = import_process_summary_with_mainline(report, report_path)
        report, topology_summary = agent.import_generated_gromacs_assets(report, report_path)
        task = report.get("separation_task") or {}
        paths = report.get("research_task_package_paths") or {}
        stage("self_check", "self check finished", {
            "molecular": molecular_summary,
            "process": process_summary,
            "topology": topology_summary,
        })
        report, alpha_summary, alpha_rc = ensure_relative_volatility_prescreen(report, report_path, status, stage)
        task = report.get("separation_task") or {}
        if not alpha_prescreen_has_pass(alpha_summary):
            clear_active_command(status)
            status["status"] = "blocked_relative_volatility_prescreen"
            status["current_stage"] = "relative_volatility_flash"
            status["stage_status"] = "blocked"
            status["message"] = (
                "Aspen relative-volatility prescreen did not produce any pass_alpha_gate candidate; "
                "Gaussian/GROMACS is blocked until Ialpha = alpha_S/alpha_0 improves enough."
            )
            status["relative_volatility_prescreen"] = alpha_summary
            write_status(report, report_path, status)
            return 3
        primary_solvent = {} if task.get("compatible_verified_case") else agent.primary_validation_solvent_identity(report, solvent_code)
        primary_solvent_code = primary_solvent.get("code") or primary_solvent.get("aspen_component_id") or ""
        blocker = agent.selected_solvent_blocker("agent_full_flow_autopilot", task_id, primary_solvent)
        if blocker:
            clear_active_command(status)
            status["status"] = "blocked"
            status["current_stage"] = "solvent_lock"
            status["stage_status"] = "blocked"
            status["message"] = blocker.get("message") or "Selected solvent is not available in the current candidate pool."
            status["selected_solvent_error"] = primary_solvent
            write_status(report, report_path, status)
            return 3
        if primary_solvent_code:
            status["primary_validation_solvent"] = primary_solvent
            report, paths = agent.ensure_selected_solvent_execution_package(report, report_path, primary_solvent_code)
            task = report.get("separation_task") or {}
            process_summary = attach_mainline_aspen_progress(report, process_summary)
            primary_solvent = agent.primary_validation_solvent_identity(report, primary_solvent_code)
            primary_solvent_code = primary_solvent.get("code") or primary_solvent.get("aspen_component_id") or primary_solvent_code
            blocker = agent.selected_solvent_blocker("agent_full_flow_autopilot", task_id, primary_solvent)
            if blocker:
                clear_active_command(status)
                status["status"] = "blocked"
                status["current_stage"] = "solvent_lock"
                status["stage_status"] = "blocked"
                status["message"] = blocker.get("message") or "Selected solvent is not available in the current candidate pool."
                status["selected_solvent_error"] = primary_solvent
                write_status(report, report_path, status)
                return 3
            status["primary_validation_solvent"] = primary_solvent
        molecular_solvent_count = max(1, int(
            task.get("molecular_approved_solvent_count")
            or task.get("approved_molecular_solvent_count")
            or task.get("molecular_batch_size")
            or 1
        ))
        run_full_molecular_scope = molecular_solvent_count > 1 and not task.get("compatible_verified_case")
        molecular_scope_solvent = {} if run_full_molecular_scope else primary_solvent
        molecular_scope_solvent_code = molecular_scope_solvent.get("code") or molecular_scope_solvent.get("aspen_component_id") or ""
        status["molecular_scope"] = {
            "mode": "top_candidate_pool" if run_full_molecular_scope else "primary_validation_solvent",
            "requested_solvent_count": molecular_solvent_count,
            "solvent_filter": molecular_scope_solvent_code,
        }
        paths = report.get("research_task_package_paths") or {}
        stage("relative_volatility_gate", "Aspen Ialpha gate passed; molecular stages may proceed.", {
            "relative_volatility": relative_volatility_summary(report),
            "primary_validation_solvent": primary_solvent,
            "molecular_scope": status["molecular_scope"],
        })
        current_mainline_aspen_required = is_mainline_aspen_required(process_summary)
        early_closed_loop_diag = (process_summary or {}).get("closed_loop_recycle_diagnosis") or {}
        early_required_output_retry = (
            early_closed_loop_diag.get("status") == "closed_loop_required_outputs_missing"
            or early_closed_loop_diag.get("dominant_failure") == "required_outputs_missing"
        )
        early_aspen_rpc_retry = closed_loop_aspen_rpc_failure(early_closed_loop_diag)
        early_required_output_missing_families = required_output_repair_missing_families(early_closed_loop_diag)
        if (
            early_closed_loop_diag.get("agent_should_stop_before_more_optimization")
            and not early_required_output_retry
            and not early_aspen_rpc_retry
            and not current_mainline_aspen_required
        ):
            clear_active_command(status)
            status["status"] = "blocked_aspen_recycle_repair_required"
            status["current_stage"] = "aspen_recycle_repair_gate"
            status["stage_status"] = "blocked"
            status["message"] = (
                "Agent stopped after self-check: closed-loop recycle repair is required ({})"
            ).format(early_closed_loop_diag.get("dominant_failure") or "no closed-loop pass")
            status["closed_loop_recycle_diagnosis"] = early_closed_loop_diag
            status["process_import_before_optimization"] = process_summary
            write_status(report, report_path, status)
            return 3
        if early_required_output_retry:
            stage(
                "aspen_required_output_repair_retry",
                "Cached Aspen rows lacked required duty/stream outputs; continuing to Aspen rebuild/repair probe instead of stopping at self-check.",
                {
                    "previous_closed_loop_recycle_diagnosis": early_closed_loop_diag,
                    "missing_repair_mode_families": early_required_output_missing_families,
                },
            )
        if early_aspen_rpc_retry:
            stage(
                "aspen_rpc_repair_retry",
                "Cached Aspen rows show COM/RPC automation failure; continuing with a bounded fast-session repair probe.",
                {
                    "previous_closed_loop_recycle_diagnosis": early_closed_loop_diag,
                    "retry_mode": "fast_inplace_session_restart",
                },
            )
        strict_closed_loop_pass_count = 0 if current_mainline_aspen_required else int((process_summary or {}).get("closed_loop_optimization_pass_case_count") or 0)
        strict_closed_loop_allowed = bool(
            (process_summary or {}).get("closed_loop_completion_allowed")
            or early_closed_loop_diag.get("full_flow_completion_allowed")
        )
        multi_objective_complete = bool((process_summary or {}).get("multi_objective_optimization_complete"))
        multi_objective_converged = bool((process_summary or {}).get("multi_objective_converged"))
        publication_budget_complete = bool((process_summary or {}).get("publication_optimization_budget_complete"))
        optimization_alignment = str((process_summary or {}).get("optimization_solvent_alignment_status") or "").strip().lower()
        mainline_aspen_required = current_mainline_aspen_required
        if (
            strict_closed_loop_pass_count > 0
            and strict_closed_loop_allowed
            and multi_objective_complete
            and multi_objective_converged
            and publication_budget_complete
            and optimization_alignment == "matched"
            and not mainline_aspen_required
            and agent.task_is_full_flow_verified(report)
        ):
            clear_active_command(status)
            status["status"] = "complete"
            status["current_stage"] = "full_flow_verified"
            status["stage_status"] = "done"
            status["message"] = (
                "Full-flow runner found existing strict closed-loop Aspen pass plus completed multi-objective Pareto/TOPSIS optimization."
            )
            status["process_import"] = process_summary
            status["closed_loop_recycle_diagnosis"] = early_closed_loop_diag
            write_status(report, report_path, status)
            return 0
        if strict_closed_loop_pass_count > 0 and strict_closed_loop_allowed and (optimization_alignment in {"mismatch", "unknown"} or mainline_aspen_required):
            stage(
                "aspen_mainline_solvent_gate",
                "Existing Aspen Pareto/TOPSIS evidence is side or unverified; current locked solvent still needs mainline Aspen optimization.",
                {"process": process_summary},
            )
        if strict_closed_loop_pass_count > 0 and strict_closed_loop_allowed and (not multi_objective_complete or not multi_objective_converged):
            stage(
                "aspen_multi_objective_gate",
                "Existing closed-loop Aspen pass found; continuing until multi-objective Pareto/TOPSIS convergence is reached.",
                {"process": process_summary},
            )

        molecular_gate_status = strict_molecular_gate_status(
            paths,
            primary_solvent,
            molecular_scope_solvent=molecular_scope_solvent,
            run_full_molecular_scope=run_full_molecular_scope,
        )
        molecular_gate_already_imported = bool(molecular_gate_status.get("ready"))
        status["molecular_evidence_gate"] = molecular_gate_status
        if molecular_gate_already_imported:
            stage(
                "molecular_evidence_gate",
                "Selected-solvent Gaussian/NCI/GROMACS molecular evidence is already imported; skipping Sobtop and GROMACS rerun.",
                {
                    "molecular": molecular_summary,
                    "primary_validation_solvent": primary_solvent,
                    "strict_gromacs_record_count": len(strict_gromacs_evidence_rows(paths)),
                    "molecular_evidence_gate": molecular_gate_status,
                },
            )

        gaussian_status = gaussian_queue_status(paths, primary_solvent=molecular_scope_solvent)
        if (not molecular_gate_already_imported) and paths.get("run_gaussian_jobs_ps1"):
            if gaussian_status.get("pending_count", 0) <= 0 and gaussian_status.get("total"):
                stage("gaussian", "Gaussian logs are already complete", gaussian_status)
                if trial_mode:
                    status["status"] = "trial_gaussian_queue_complete"
                    status["message"] = "Publication trial found the Gaussian queue complete; use full autopilot to continue Sobtop/GROMACS/Aspen."
                    status["gaussian_queue"] = gaussian_status
                    write_status(report, report_path, status)
                    return 0
            elif gaussian_status.get("pending_count", 0) > 0:
                ps = agent.powershell_path()
                gaussian = agent.discover_gaussian_executable()
                if not ps or not gaussian:
                    status["status"] = "blocked"
                    status["message"] = "Gaussian stage blocked: powershell or Gaussian executable not found."
                    write_status(report, report_path, status)
                    return 3
                command = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", paths["run_gaussian_jobs_ps1"], "-GaussianExe", gaussian]
                formchk = agent.discover_formchk_executable(gaussian)
                if formchk:
                    command.extend(["-FormchkExe", formchk])
                if max_gaussian_jobs:
                    command.extend(["-MaxJobs", str(int(max_gaussian_jobs))])
                if molecular_scope_solvent_code:
                    command.extend(["-SolventCode", molecular_scope_solvent_code])
                rc = run_command("gaussian", command, Path(paths["molecular_inputs_dir"]), Path(paths["molecular_inputs_dir"]) / "autopilot_logs", status, report, report_path)
                if rc != 0:
                    status["status"] = "failed"
                    status["message"] = "Gaussian stage failed."
                    write_status(report, report_path, status)
                    return rc
                report = agent.read_json(report_path)
                report, molecular_summary = agent.import_molecular_results(report, report_path)
                gaussian_status = gaussian_queue_status(report.get("research_task_package_paths") or paths, primary_solvent=molecular_scope_solvent)
                if trial_mode:
                    status["status"] = "trial_stopped_after_gaussian"
                    status["message"] = (
                        "Publication trial stopped after the budgeted Gaussian stage. "
                        "This proves the Agent can launch the new-system Gaussian queue; complete molecular evidence still requires the remaining queue."
                    )
                    status["gaussian_queue"] = gaussian_status
                    status["molecular_import_after_trial"] = molecular_summary
                    write_status(report, report_path, status)
                    return 0
                if gaussian_status.get("pending_count", 0) > 0:
                    clear_active_command(status)
                    status["status"] = "blocked_gaussian_queue_pending"
                    status["current_stage"] = "gaussian"
                    status["stage_status"] = "batch_complete"
                    status["message"] = (
                        "Gaussian batch completed under the one-job safety budget; "
                        "pending Gaussian jobs remain, so NCI/VMD, GROMACS and Aspen are held."
                    )
                    status["gaussian_queue"] = gaussian_status
                    status["molecular_import_after_gaussian_batch"] = molecular_summary
                    write_status(report, report_path, status)
                    return 3

        paths = report.get("research_task_package_paths") or {}
        gaussian_status = gaussian_queue_status(paths, primary_solvent=molecular_scope_solvent)
        if (not molecular_gate_already_imported) and paths.get("run_gaussian_jobs_ps1") and gaussian_status.get("pending_count", 0) > 0:
            clear_active_command(status)
            status["status"] = "blocked_gaussian_queue_pending"
            status["current_stage"] = "gaussian"
            status["stage_status"] = "blocked"
            status["message"] = (
                "Gaussian queue is not complete; NCI/VMD, GROMACS and Aspen are blocked until every scoped Gaussian job terminates normally."
            )
            status["gaussian_queue"] = gaussian_status
            write_status(report, report_path, status)
            return 3

        paths = report.get("research_task_package_paths") or {}
        nci_status = nci_vmd_queue_status(paths, primary_solvent=molecular_scope_solvent)
        if (not molecular_gate_already_imported) and paths.get("run_nci_vmd_queue_ps1") and nci_status.get("total"):
            if nci_status.get("pending_count", 0) <= 0:
                stage("nci_vmd", "Multiwfn/VMD NCI image evidence is already imported", nci_status)
            elif nci_status.get("ready_fch_count", 0) <= 0:
                status["status"] = "blocked_before_nci_vmd"
                status["message"] = "NCI/VMD stage blocked: Gaussian dimer .fch files are not available yet."
                status["nci_vmd_queue"] = nci_status
                write_status(report, report_path, status)
                return 3
            else:
                ps = agent.powershell_path()
                multiwfn = agent.discover_multiwfn_executable()
                vmd = agent.discover_vmd_executable()
                if not ps or not multiwfn or not vmd:
                    status["status"] = "blocked_before_nci_vmd"
                    status["message"] = "NCI/VMD stage blocked: powershell, Multiwfn, or VMD executable not found."
                    status["nci_vmd_queue"] = nci_status
                    write_status(report, report_path, status)
                    return 3
                command = [
                    ps,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    paths["run_nci_vmd_queue_ps1"],
                    "-Multiwfn",
                    multiwfn,
                    "-Vmd",
                    vmd,
                    "-GridQuality",
                    "low",
                ]
                if molecular_scope_solvent_code:
                    command.extend(["-SolventCode", molecular_scope_solvent_code])
                rc = run_command(
                    "nci_vmd",
                    command,
                    Path(paths["molecular_inputs_dir"]),
                    Path(paths["molecular_inputs_dir"]) / "autopilot_logs",
                    status,
                    report,
                    report_path,
                )
                if rc != 0:
                    status["status"] = "failed"
                    status["message"] = "Multiwfn/VMD NCI image stage failed."
                    write_status(report, report_path, status)
                    return rc
                report = agent.read_json(report_path)
                report, molecular_summary = agent.import_molecular_results(report, report_path)
                paths = report.get("research_task_package_paths") or {}
                nci_status = nci_vmd_queue_status(paths, primary_solvent=molecular_scope_solvent)
                stage("nci_vmd_import", "Multiwfn/VMD NCI image evidence imported", molecular_summary)
                if nci_status.get("pending_count", 0) > 0:
                    clear_active_command(status)
                    status["status"] = "blocked_nci_vmd_queue_pending"
                    status["current_stage"] = "nci_vmd"
                    status["stage_status"] = "blocked"
                    status["message"] = (
                        "NCI/VMD evidence is still incomplete; Sobtop, GROMACS and Aspen are held until every scoped NCI/VMD job is imported."
                    )
                    status["nci_vmd_queue"] = nci_status
                    status["molecular_import_after_nci_batch"] = molecular_summary
                    write_status(report, report_path, status)
                    return 3

        paths = report.get("research_task_package_paths") or {}
        nci_status = nci_vmd_queue_status(paths, primary_solvent=molecular_scope_solvent)
        if (not molecular_gate_already_imported) and paths.get("run_nci_vmd_queue_ps1") and nci_status.get("pending_count", 0) > 0:
            clear_active_command(status)
            status["status"] = "blocked_nci_vmd_queue_pending"
            status["current_stage"] = "nci_vmd"
            status["stage_status"] = "blocked"
            status["message"] = (
                "NCI/VMD evidence is incomplete; Sobtop, GROMACS and Aspen are blocked until every scoped NCI/VMD job is imported."
            )
            status["nci_vmd_queue"] = nci_status
            write_status(report, report_path, status)
            return 3

        paths = report.get("research_task_package_paths") or {}
        if (not molecular_gate_already_imported) and paths.get("run_sobtop_parameterization_ps1"):
            ps = agent.powershell_path()
            if ps:
                command = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", paths["run_sobtop_parameterization_ps1"]]
                if molecular_scope_solvent_code:
                    command.extend(["-SolventCode", molecular_scope_solvent_code])
                rc = run_command(
                    "sobtop_multiwfn",
                    command,
                    Path(paths["molecular_inputs_dir"]),
                    Path(paths["molecular_inputs_dir"]) / "autopilot_logs",
                    status,
                    report,
                    report_path,
                )
                if rc != 0:
                    status["status"] = "failed"
                    status["message"] = "Sobtop/Multiwfn stage failed."
                    write_status(report, report_path, status)
                    return rc
                report = agent.read_json(report_path)
                report, topology_summary = agent.import_generated_gromacs_assets(report, report_path)
                stage("topology_import", "Sobtop generated assets imported", topology_summary)

        paths = report.get("research_task_package_paths") or {}
        readiness = agent.inspect_gromacs_queue_readiness(paths, primary_solvent=molecular_scope_solvent) if ((not molecular_gate_already_imported) and paths.get("run_gromacs_queue_ps1")) else {}
        if not molecular_gate_already_imported:
            stage("gromacs_preflight", "GROMACS preflight finished", readiness)
        if (not molecular_gate_already_imported) and primary_solvent_code and readiness.get("primary_expected_job_count") and readiness.get("primary_ready_job_count", 0) < readiness.get("primary_expected_job_count", 0):
            status["status"] = "blocked_before_gromacs"
            status["message"] = (
                "GROMACS stage blocked: primary validation solvent does not yet have all required topology-ready MD cases."
            )
            status["gromacs_readiness"] = readiness
            write_status(report, report_path, status)
            return 3
        if (not molecular_gate_already_imported) and run_full_molecular_scope and full_gromacs_gate_passed(paths):
            stage("gromacs", "Full candidate-pool Gaussian/GROMACS molecular gate is already imported", readiness)
        elif (not molecular_gate_already_imported) and (not run_full_molecular_scope) and primary_solvent_code and primary_molecular_gate_passed(paths, primary_solvent):
            stage("gromacs", "Primary Gaussian/GROMACS molecular gate is already imported", readiness)
        elif (not molecular_gate_already_imported) and readiness.get("ready_dir_count"):
            ps = agent.powershell_path()
            gmx = readiness.get("gmx")
            if ps and gmx:
                command = [
                    ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", readiness["script"],
                    "-Gmx", gmx,
                    "-NSolvent", str(agent.GROMACS_LEGACY_DEFAULT_NSOLVENT),
                    "-NSolute", str(agent.GROMACS_LEGACY_DEFAULT_NSOLUTE),
                    "-EmSteps", str(agent.GROMACS_LEGACY_DEFAULT_EM_STEPS),
                    "-NvtSteps", str(agent.GROMACS_LEGACY_DEFAULT_NVT_STEPS),
                    "-NptSteps", str(agent.GROMACS_LEGACY_DEFAULT_NPT_STEPS),
                    "-MdSteps", str(agent.GROMACS_LEGACY_DEFAULT_MD_STEPS),
                ]
                if molecular_scope_solvent_code:
                    command.extend(["-SolventCode", molecular_scope_solvent_code])
                rc = run_command(
                    "gromacs",
                    command,
                    Path(paths["molecular_inputs_dir"]),
                    Path(paths["molecular_inputs_dir"]) / "autopilot_logs",
                    status,
                    report,
                    report_path,
                )
                if rc != 0:
                    status["status"] = "failed"
                    status["message"] = "GROMACS stage failed."
                    write_status(report, report_path, status)
                    return rc
                report = agent.read_json(report_path)
                report, molecular_summary = agent.import_molecular_results(report, report_path)

        report = agent.read_json(report_path)
        paths = report.get("research_task_package_paths") or {}
        molecular_gate_status = strict_molecular_gate_status(
            paths,
            primary_solvent,
            molecular_scope_solvent=molecular_scope_solvent,
            run_full_molecular_scope=run_full_molecular_scope,
        )
        status["molecular_evidence_gate"] = molecular_gate_status
        if not molecular_gate_status.get("ready"):
            clear_active_command(status)
            status["status"] = "blocked_molecular_evidence_pending"
            status["current_stage"] = "molecular_evidence_gate"
            status["stage_status"] = "blocked"
            status["message"] = (
                "Current-system molecular evidence is not complete; Aspen optimization is blocked until "
                "the scoped Gaussian/NCI/GROMACS evidence for the selected solvent is imported."
            )
            write_status(report, report_path, status)
            return 3
        plan_path = paths.get("aspen_build_plan_json")
        if plan_path and Path(plan_path).exists() and not task.get("compatible_verified_case"):
            plan = agent.read_json(plan_path)
            if plan.get("can_generate_bkp_now"):
                spec_path = paths.get("aspen_zero_build_spec_json") or str(Path(plan_path).parent / "aspen_zero_build_spec.json")
                if not Path(spec_path).exists():
                    status["status"] = "blocked_before_aspen_optimization"
                    status["message"] = "Aspen zero-build spec is missing for this new binary system."
                    write_status(report, report_path, status)
                    return 3
                report, paths, price_evidence = agent.ensure_current_solvent_price_evidence(
                    report,
                    report_path,
                    primary_solvent_code,
                )
                price_summary = agent.solvent_price_evidence_summary(price_evidence)
                status["solvent_price_evidence"] = price_summary
                stage(
                    "solvent_price_research",
                    "Online solvent price evidence is locked for this system/solvent before Aspen economics.",
                    price_summary,
                )
                try:
                    report, existing_process_summary = import_process_summary_with_mainline(report, report_path)
                except Exception:
                    existing_process_summary = attach_mainline_aspen_progress(report, report.get("process_result_import") or {})
                existing_mainline_required = is_mainline_aspen_required(existing_process_summary)
                if existing_mainline_required:
                    stage(
                        "aspen_mainline_optimization_prepare",
                        "Cached Aspen evidence is side/unverified for the current locked solvent; the agent will run Aspen for the selected solvent.",
                        {"process": existing_process_summary},
                    )
                closed_loop_diag = (existing_process_summary or {}).get("closed_loop_recycle_diagnosis") or {}
                allow_required_output_repair_retry = (
                    closed_loop_diag.get("status") == "closed_loop_required_outputs_missing"
                    or closed_loop_diag.get("dominant_failure") == "required_outputs_missing"
                )
                allow_aspen_rpc_repair_retry = closed_loop_aspen_rpc_failure(closed_loop_diag)
                missing_repair_mode_families = required_output_repair_missing_families(closed_loop_diag)
                if (
                    closed_loop_diag.get("agent_should_stop_before_more_optimization")
                    and not allow_required_output_repair_retry
                    and not allow_aspen_rpc_repair_retry
                    and not existing_mainline_required
                ):
                    clear_active_command(status)
                    status["status"] = "blocked_aspen_recycle_repair_required"
                    status["current_stage"] = "aspen_recycle_repair_gate"
                    status["stage_status"] = "blocked"
                    status["message"] = (
                        "Agent stopped before launching another Aspen campaign: "
                        "closed-loop recycle repair is required ({})"
                    ).format(closed_loop_diag.get("dominant_failure") or "no closed-loop pass")
                    status["closed_loop_recycle_diagnosis"] = closed_loop_diag
                    status["process_import_before_optimization"] = existing_process_summary
                    write_status(report, report_path, status)
                    return 3
                if allow_required_output_repair_retry:
                    stage(
                        "aspen_required_output_repair_retry",
                        "Cached Aspen rows lacked required duty/stream outputs; retrying zero-build with repaired component databank identities.",
                        {
                            "previous_closed_loop_recycle_diagnosis": closed_loop_diag,
                            "missing_repair_mode_families": missing_repair_mode_families,
                        },
                    )
                if allow_aspen_rpc_repair_retry:
                    stage(
                        "aspen_rpc_repair_retry",
                        "Cached Aspen rows show COM/RPC automation failure; retrying with a bounded fast Aspen session.",
                        {
                            "previous_closed_loop_recycle_diagnosis": closed_loop_diag,
                            "retry_mode": "fast_inplace_session_restart",
                        },
                    )
                aspen_dir = Path(plan_path).parent / "aspen_generated"
                task_token = agent.safe_file_token(task_id or "binary_task")
                zero_inp = aspen_dir / "{}_zero_agent.inp".format(task_token)
                zero_bkp = aspen_dir / "{}_zero_agent.bkp".format(task_token)
                zero_report = aspen_dir / "{}_zero_agent.autoverify.json".format(task_token)
                if zero_bkp.exists() and not zero_report.exists():
                    recovered = recover_zero_build_report_from_archive(
                        spec_path,
                        zero_inp,
                        zero_bkp,
                        zero_report,
                        reason="existing Aspen zero-build archive was found without its autoverify report",
                        command_status={"source": "pre_existing_archive"},
                    )
                    if recovered:
                        stage(
                            "aspen_zero_build_recovered",
                            "Existing Aspen zero-build archive was recovered as a reusable optimization template.",
                            {"zero_build_report": str(zero_report.resolve()), "zero_build_bkp": str(zero_bkp.resolve())},
                        )
                if zero_bkp.exists() and zero_report.exists():
                    stage(
                        "aspen_zero_build",
                        "Existing Aspen zero-build archive is available; reusing it instead of opening Aspen again.",
                        {"zero_build_report": str(zero_report.resolve()), "zero_build_bkp": str(zero_bkp.resolve())},
                    )
                else:
                    rc = run_command(
                        "aspen_zero_build",
                        [
                            sys.executable,
                            str(agent.BASE_DIR / "zero_build_aspen_agent.py"),
                            "--spec",
                            str(spec_path),
                            "--output-inp",
                            str(zero_inp),
                            "--output-bkp",
                            str(zero_bkp),
                            "--report",
                            str(zero_report),
                            "--no-seed-run",
                        ],
                        agent.BASE_DIR,
                        aspen_dir / "logs",
                        status,
                        report,
                        report_path,
                        timeout_seconds=180,
                        cleanup_new_aspen=True,
                    )
                    if rc != 0:
                        recovered = recover_zero_build_report_from_archive(
                            spec_path,
                            zero_inp,
                            zero_bkp,
                            zero_report,
                            reason="zero-build command ended with return code {}, but the Aspen BKP archive exists".format(rc),
                            command_status={
                                "returncode": rc,
                                "stage_status": status.get("stage_status", ""),
                                "timeout_seconds": status.get("timeout_seconds", ""),
                                "stdout_log": status.get("stdout_log", ""),
                                "stderr_log": status.get("stderr_log", ""),
                            },
                        )
                        if not recovered:
                            status["status"] = "failed"
                            status["message"] = "Generic Aspen zero-build archive generation failed."
                            write_status(report, report_path, status)
                            return rc
                        stage(
                            "aspen_zero_build_recovered",
                            "Aspen zero-build timed out after writing a BKP; the agent recovered it as a reusable optimization template.",
                            {"zero_build_report": str(zero_report.resolve()), "zero_build_bkp": str(zero_bkp.resolve())},
                        )
                    else:
                        stage(
                            "aspen_zero_build",
                            "Aspen zero-build archive generated without shortcut validation; proceeding to real optimization cases.",
                            {"zero_build_report": str(zero_report.resolve())},
                        )
                report = agent.read_json(report_path)
                report, process_summary = import_process_summary_with_mainline(report, report_path)
                optimization_dir = Path(report.get("output_dir") or report_path.parent) / "aspen_optimization_once_autopilot_{}".format(
                    time.strftime("%Y%m%d_%H%M%S")
                )
                optimization_plan = aspen_optimization_run_plan(task, trial_mode=trial_mode, spec_path=spec_path)
                campaign = optimization_plan["campaign"]
                population_size = int(campaign.get("population_size") or 12)
                generations = int(campaign.get("max_generations") or campaign.get("generations") or 2)
                min_generations = int(campaign.get("minimum_generations") or 2)
                convergence_patience = int(campaign.get("convergence_patience") or 2)
                run_until_converged = bool(campaign.get("run_until_converged"))
                generation_limit = int(campaign.get("safety_max_generations") or (0 if run_until_converged else generations))
                timeout_generations = generation_limit if generation_limit else max(generations + convergence_patience + 5, generations + 1)
                bounds_profile = str(optimization_plan.get("bounds_profile") or campaign.get("bounds_profile") or "exploratory")
                search_mode = str(optimization_plan.get("search_mode") or campaign.get("search_mode") or "nsga2_hybrid")
                random_seed = int(optimization_plan.get("random_seed") or campaign.get("random_seed") or 20260630)
                crossover_rate = float(optimization_plan.get("crossover_rate") or campaign.get("crossover_rate") or 0.85)
                mutation_rate = float(optimization_plan.get("mutation_rate") or campaign.get("mutation_rate") or 0.18)
                fast_template_bkp = zero_bkp if zero_bkp.exists() else None
                initial_rpc_history = closed_loop_aspen_rpc_history(
                    (process_summary or {}).get("closed_loop_recycle_diagnosis") or {}
                )
                if initial_rpc_history.get("history_present"):
                    status["aspen_optimization_fast_inplace_rpc_history"] = initial_rpc_history
                status["aspen_mainline_execution_policy"] = "fast_only"
                if ASPEN_MAINLINE_FAST_ONLY and not fast_template_bkp:
                    status["status"] = "blocked_aspen_fast_template_missing"
                    status["current_stage"] = "aspen_fast_template_preflight"
                    status["current_action"] = "repair_fast_template_before_mainline_optimization"
                    status["stage_status"] = "blocked"
                    status["message"] = (
                        "Fast-only Aspen policy requires a stable zero-build BKP template; "
                        "isolated case fallback is disabled."
                    )
                    write_status(report, report_path, status)
                    return 3
                if initial_rpc_history.get("blocks_fast_inplace"):
                    status["aspen_optimization_fast_inplace_rpc_policy"] = (
                        "Previous COM/RPC failures were detected, but fast-only policy keeps the persistent "
                        "Aspen session and relies on bounded session restarts instead of isolated case fallback."
                    )
                status["aspen_optimization_campaign"] = campaign
                status["aspen_optimization_bounds_profile"] = bounds_profile
                status["aspen_optimization_search_mode"] = search_mode
                status["aspen_optimization_algorithm"] = optimization_plan.get("algorithm") or campaign.get("algorithm") or search_mode
                status["aspen_optimization_random_seed"] = random_seed
                status["aspen_optimization_crossover_rate"] = crossover_rate
                status["aspen_optimization_mutation_rate"] = mutation_rate
                status["aspen_optimization_variable_ranges"] = optimization_plan.get("variable_ranges") or {}
                status["aspen_optimization_variable_notes"] = optimization_plan.get("variable_notes") or {}
                status["aspen_optimization_execution_mode"] = "fast_inplace_single_aspen_session"
                status.pop("aspen_optimization_fast_inplace_disabled_reason", None)
                status["aspen_optimization_fast_template_bkp"] = (
                    str(fast_template_bkp.resolve()) if fast_template_bkp else ""
                )
                stage(
                    "aspen_optimization_plan",
                    "Aspen multi-objective optimization plan and variable ranges are locked.",
                    optimization_plan,
                )

                def add_fast_inplace_args(command, session_suffix, restart_interval=10, save_validated_bkp=False):
                    if not fast_template_bkp:
                        return False
                    command.extend([
                        "--fast-inplace",
                        "--fast-template-bkp",
                        str(fast_template_bkp),
                        "--fast-session-id",
                        "{}_{}".format(task_token, session_suffix),
                        "--fast-session-restart-interval",
                        str(restart_interval),
                        "--fast-run-timeout-seconds",
                        "240",
                    ])
                    if save_validated_bkp:
                        command.append("--fast-save-validated-bkp")
                    return True

                current_mainline_required = is_mainline_aspen_required(process_summary)
                probe_pass_count = 0 if current_mainline_required else int((process_summary or {}).get("closed_loop_optimization_pass_case_count") or 0)
                existing_repair_probe_summary = {}
                if probe_pass_count <= 0:
                    existing_repair_probe_summary = latest_current_solvent_repair_probe_summary(
                        Path(report.get("output_dir") or report_path.parent),
                        primary_solvent,
                    )
                    probe_pass_count = int(existing_repair_probe_summary.get("pass_count") or 0)
                if probe_pass_count > 0:
                    stage(
                        "aspen_repair_probe",
                        "Existing trusted current-solvent repair PASS is available; skipping repair probe and proceeding to Pareto/TOPSIS optimization.",
                        {
                            "closed_loop_pass_count": probe_pass_count,
                            "repair_probe_summary": existing_repair_probe_summary,
                        },
                    )
                else:
                    repair_probe_case_count = min(
                        REPAIR_PROBE_MAX_CASE_COUNT,
                        max(REPAIR_PROBE_DEFAULT_CASE_COUNT, int(population_size or 1)),
                    )
                    repair_probe_dir = Path(report.get("output_dir") or report_path.parent) / "aspen_repair_probe_autopilot_{}".format(
                        time.strftime("%Y%m%d_%H%M%S")
                    )
                    status["aspen_repair_probe_case_count"] = repair_probe_case_count
                    status["aspen_repair_probe_policy"] = (
                        "Cover both calculator-inventory and bounded-nonnegative makeup repair families in fast-only mode."
                    )
                    repair_probe_use_fast_inplace = bool(fast_template_bkp)
                    status["aspen_repair_probe_fast_inplace"] = repair_probe_use_fast_inplace
                    repair_probe_command = [
                        sys.executable,
                        str(agent.BASE_DIR / "generic_aspen_once_optimizer.py"),
                        "--spec",
                        str(spec_path),
                        "--output-dir",
                        str(repair_probe_dir),
                        "--population-size",
                        str(repair_probe_case_count),
                        "--search-mode",
                        "adaptive_refinement",
                        "--generations",
                        "0",
                        "--max-cases",
                        str(repair_probe_case_count),
                        "--required-output-missing-stop-streak",
                        "0",
                        "--repair-probe-cover-control-families",
                        "--case-timeout-seconds",
                        "240",
                        "--aspen-cleanup-batch-size",
                        "1",
                        "--aspen-cleanup-settle-seconds",
                        "18",
                        "--aspen-cleanup-cooldown-seconds",
                        "10",
                        "--aspen-process-limit",
                        str(agent.ASPEN_MAX_PARALLEL_PROCESSES),
                        "--min-generations",
                        "0",
                        "--convergence-patience",
                        "1",
                        "--stop-after-pass",
                        "--seed-report",
                        str(aspen_dir / "{}_zero_agent.autoverify.json".format(task_token)),
                    ]
                    repair_probe_fast_inplace_enabled = False
                    if repair_probe_use_fast_inplace:
                        repair_probe_fast_inplace_enabled = add_fast_inplace_args(
                            repair_probe_command,
                            "repair_probe",
                            restart_interval=6,
                        )
                    rc = run_command(
                        "aspen_repair_probe",
                        repair_probe_command,
                        agent.BASE_DIR,
                        repair_probe_dir / "logs",
                        status,
                        report,
                        report_path,
                        timeout_seconds=max(900, repair_probe_case_count * 300),
                        cleanup_new_aspen=True,
                        progress_report_path=repair_probe_dir / "generic_aspen_once_optimizer.report.json",
                    )
                    if rc != 0:
                        status["status"] = "blocked_aspen_fast_inplace_failed"
                        status["current_stage"] = "aspen_fast_repair_probe"
                        status["current_action"] = "restart_fast_inplace_repair_probe"
                        status["stage_status"] = "blocked"
                        status["message"] = (
                            "Fast-only Aspen repair probe failed; automatic isolated-case fallback is disabled."
                        )
                        write_status(report, report_path, status)
                        return 3
                    report = agent.read_json(report_path)
                    report, process_summary = import_process_summary_with_mainline(report, report_path)
                    repair_probe_summary = current_solvent_probe_pass_summary(repair_probe_dir, primary_solvent)
                    probe_pass_count = int(repair_probe_summary.get("pass_count") or 0)
                    if probe_pass_count <= 0 and not is_mainline_aspen_required(process_summary):
                        probe_pass_count = int((process_summary or {}).get("closed_loop_optimization_pass_case_count") or 0)
                    status["aspen_repair_probe_summary"] = repair_probe_summary
                    if probe_pass_count <= 0:
                        closed_loop_diag = (process_summary or {}).get("closed_loop_recycle_diagnosis") or {}
                        missing_repair_mode_families = required_output_repair_missing_families(closed_loop_diag)
                        if missing_repair_mode_families:
                            status["status"] = "blocked_required_outputs_missing_retryable"
                            status["current_stage"] = "aspen_required_output_repair_probe"
                            status["current_action"] = "continue_fast_inplace_repair_probe"
                            status["message"] = (
                                "Fast-only Aspen repair probe still has untested required-output repair families: {}."
                            ).format(", ".join(missing_repair_mode_families))
                        elif closed_loop_aspen_rpc_failure(closed_loop_diag):
                            status["status"] = "blocked_aspen_fast_inplace_rpc_failure"
                            status["current_stage"] = "aspen_fast_rpc_repair_probe"
                            status["current_action"] = "restart_fast_inplace_repair_probe"
                            status["message"] = (
                                "Aspen COM/RPC failed in fast-only repair mode. Automatic isolated-case fallback "
                                "is disabled; restart the bounded fast session and preserve existing evidence."
                            )
                        else:
                            status["status"] = "blocked_aspen_recycle_repair_required"
                            status["current_stage"] = "aspen_recycle_repair_probe"
                            status["current_action"] = "repair_closed_loop_recycle_solver_before_more_optimization"
                            status["message"] = (
                                "Aspen fast-only repair probe ran {} serial cases with real duty/stream checks, "
                                "but no trusted closed-loop PASS was imported. Dominant failure: {}"
                            ).format(repair_probe_case_count, closed_loop_diag.get("dominant_failure") or "unknown")
                        status["stage_status"] = "blocked"
                        status["missing_repair_mode_families"] = missing_repair_mode_families
                        status["aspen_repair_probe_output_dir"] = str(repair_probe_dir.resolve())
                        status["process_import_after_repair_probe"] = process_summary
                        status["closed_loop_recycle_diagnosis"] = closed_loop_diag
                        status["aspen_repair_probe_summary"] = repair_probe_summary
                        write_status(report, report_path, status)
                        return 3
                    stage(
                        "aspen_repair_probe",
                        "Aspen repair probe found at least one trusted closed-loop PASS; proceeding to Pareto/TOPSIS optimization.",
                        {
                            "repair_probe_output_dir": str(repair_probe_dir.resolve()),
                            "closed_loop_pass_count": probe_pass_count,
                            "repair_probe_summary": repair_probe_summary,
                        },
                    )
                optimization_command = [
                        sys.executable,
                        str(agent.BASE_DIR / "generic_aspen_once_optimizer.py"),
                        "--spec",
                        str(spec_path),
                        "--output-dir",
                        str(optimization_dir),
                        "--population-size",
                        str(population_size),
                        "--search-mode",
                        search_mode,
                        "--random-seed",
                        str(random_seed),
                        "--crossover-rate",
                        str(crossover_rate),
                        "--mutation-rate",
                        str(mutation_rate),
                        "--generations",
                        str(generation_limit),
                        "--case-timeout-seconds",
                        "240",
                        "--aspen-cleanup-batch-size",
                        "1",
                        "--aspen-cleanup-settle-seconds",
                        "18",
                        "--aspen-cleanup-cooldown-seconds",
                        "10",
                        "--aspen-process-limit",
                        str(agent.ASPEN_MAX_PARALLEL_PROCESSES),
                        "--min-generations",
                        str(min_generations),
                        "--convergence-patience",
                        str(convergence_patience),
                        "--bounds-profile",
                        bounds_profile,
                        "--seed-report",
                        str(aspen_dir / "{}_zero_agent.autoverify.json".format(task_token)),
                    ]
                resume_base_dir = Path(report.get("output_dir") or report_path.parent)
                resume_summary = current_solvent_optimization_resume_summary(
                    resume_base_dir,
                    primary_solvent,
                )
                repair_probe_resume_summary = latest_current_solvent_repair_probe_summary(
                    resume_base_dir,
                    primary_solvent,
                )
                repair_probe_pass_count = int(repair_probe_resume_summary.get("pass_count") or 0)
                optimization_pass_count = int(
                    resume_summary.get("closed_loop_pass_case_count")
                    or resume_summary.get("pass_case_count")
                    or 0
                )
                optimization_should_stop = bool(
                    (resume_summary.get("closed_loop_recycle_diagnosis") or {}).get(
                        "agent_should_stop_before_more_optimization"
                    )
                )
                if (
                    repair_probe_pass_count > 0
                    and (
                        not resume_summary
                        or optimization_pass_count <= 0
                        or optimization_should_stop
                    )
                ):
                    probe_report = Path(repair_probe_resume_summary.get("report_json") or "")
                    resume_summary = dict(repair_probe_resume_summary)
                    resume_summary.update(
                        {
                            "resume_report": str(probe_report.resolve()),
                            "source_type": "repair_probe_seed",
                            "converged": False,
                            "selected_reason": (
                                "Latest current-solvent repair probe has closed-loop Aspen seed rows, "
                                "while the latest mainline optimization has no usable closed-loop pass."
                            ),
                        }
                    )
                    status["aspen_optimization_repair_probe_resume"] = resume_summary
                resume_rpc_history = closed_loop_aspen_rpc_history(
                    resume_summary.get("closed_loop_recycle_diagnosis") or {}
                )
                if resume_rpc_history.get("history_present"):
                    status["aspen_optimization_resume_rpc_history"] = resume_rpc_history
                if resume_rpc_history.get("blocks_fast_inplace"):
                    status["aspen_optimization_resume_rpc_policy"] = (
                        "Resumable evidence contains COM/RPC failures; fast-only policy preserves the persistent "
                        "Aspen session and its bounded restart interval."
                    )
                if resume_summary.get("resume_report") and not resume_summary.get("converged"):
                    optimization_command.extend(["--resume-report", str(resume_summary.get("resume_report"))])
                    status["aspen_optimization_resume"] = resume_summary
                    resume_message = (
                        "Latest current-solvent repair probe rows found; seeding the mainline Pareto/TOPSIS campaign from that Aspen evidence."
                        if resume_summary.get("source_type") == "repair_probe_seed"
                        else "Existing current-solvent optimization rows found; continuing from the latest EC campaign instead of restarting."
                    )
                    stage(
                        "aspen_optimization_resume",
                        resume_message,
                        resume_summary,
                    )
                if run_until_converged:
                    optimization_command.append("--run-until-converged")
                optimization_fast_inplace_enabled = add_fast_inplace_args(
                    optimization_command,
                    "optimization",
                    restart_interval=10,
                )
                status["aspen_optimization_fast_inplace_enabled"] = bool(optimization_fast_inplace_enabled)
                if ASPEN_MAINLINE_FAST_ONLY and not optimization_fast_inplace_enabled:
                    status["status"] = "blocked_aspen_fast_template_missing"
                    status["current_stage"] = "aspen_fast_template_preflight"
                    status["current_action"] = "repair_fast_template_before_mainline_optimization"
                    status["stage_status"] = "blocked"
                    status["message"] = (
                        "Fast-only Aspen policy could not enable --fast-inplace; isolated fallback is disabled."
                    )
                    write_status(report, report_path, status)
                    return 3
                rc = run_command(
                    "aspen_generic_optimization",
                    optimization_command,
                    agent.BASE_DIR,
                    optimization_dir / "logs",
                    status,
                    report,
                    report_path,
                    timeout_seconds=max(1800, population_size * max(1, timeout_generations + 1) * 90),
                    cleanup_new_aspen=True,
                    progress_report_path=optimization_dir / "generic_aspen_once_optimizer.report.json",
                )
                if rc != 0:
                    status["status"] = "blocked_aspen_fast_inplace_failed"
                    status["current_stage"] = "aspen_fast_mainline_optimization"
                    status["current_action"] = "restart_fast_inplace_mainline_optimization"
                    status["stage_status"] = "blocked"
                    status["message"] = (
                        "Fast-only Aspen mainline optimization failed; automatic isolated-case fallback is disabled."
                    )
                    write_status(report, report_path, status)
                    return 3
                report = agent.read_json(report_path)
                report, process_summary = import_process_summary_with_mainline(report, report_path)
                pass_count = int((process_summary or {}).get("closed_loop_optimization_pass_case_count") or 0)
                multi_objective_complete = bool((process_summary or {}).get("multi_objective_optimization_complete"))
                multi_objective_converged = bool((process_summary or {}).get("multi_objective_converged"))
                publication_budget_complete = bool((process_summary or {}).get("publication_optimization_budget_complete"))
                optimization_alignment = str((process_summary or {}).get("optimization_solvent_alignment_status") or "").strip().lower()
                mainline_aspen_converged = is_mainline_aspen_closed(process_summary)
                mainline_aspen_required = is_mainline_aspen_required(process_summary)
                if pass_count <= 0:
                    closed_loop_diag = (process_summary or {}).get("closed_loop_recycle_diagnosis") or {}
                    if closed_loop_aspen_rpc_failure(closed_loop_diag):
                        status["status"] = "blocked_aspen_fast_inplace_rpc_failure"
                        status["current_stage"] = "aspen_fast_mainline_optimization"
                        status["current_action"] = "restart_fast_inplace_mainline_optimization"
                        status["stage_status"] = "blocked"
                        status["message"] = (
                            "Aspen COM/RPC failed without a trusted pass in fast-only mode. Automatic isolated-case "
                            "fallback is disabled; preserve the campaign and restart the bounded fast session."
                        )
                        status["aspen_optimization_output_dir"] = str(optimization_dir.resolve())
                        status["process_import_after_optimization"] = process_summary
                        status["closed_loop_recycle_diagnosis"] = closed_loop_diag
                        write_status(report, report_path, status)
                        return 3
                    if closed_loop_diag.get("status") == "closed_loop_required_outputs_missing":
                        missing_repair_mode_families = required_output_repair_missing_families(closed_loop_diag)
                        if missing_repair_mode_families:
                            status["status"] = "blocked_required_outputs_missing_retryable"
                            status["current_stage"] = "aspen_required_output_repair_probe"
                            status["current_action"] = "continue_required_output_repair_probe"
                            status["message"] = (
                                "Aspen required outputs are still missing, and repair families remain untested: {}. "
                                "The full-flow agent should continue a repair probe before final blocking."
                            ).format(", ".join(missing_repair_mode_families))
                        else:
                            status["status"] = "blocked_required_outputs_missing"
                            status["current_stage"] = "aspen_required_outputs_missing"
                            status["current_action"] = "repair_aspen_model_outputs_before_more_optimization"
                            status["message"] = (
                                closed_loop_diag.get("blocker")
                                or "Aspen Run2 returned no hard error, but required duty/stream outputs were missing or zero."
                            )
                        status["stage_status"] = "blocked"
                        status["missing_repair_mode_families"] = missing_repair_mode_families
                    else:
                        status["status"] = "blocked_aspen_optimization_not_converged"
                        status["current_action"] = "repair_closed_loop_recycle_solver_before_more_optimization"
                        status["message"] = (
                            "Generic Aspen optimization ran, but no converged/pass closed-loop solvent-recycle case was imported yet. "
                            "Dominant failure: {}"
                        ).format(closed_loop_diag.get("dominant_failure") or "unknown")
                    status["aspen_optimization_output_dir"] = str(optimization_dir.resolve())
                    status["process_import_after_optimization"] = process_summary
                    status["closed_loop_recycle_diagnosis"] = closed_loop_diag
                    write_status(report, report_path, status)
                    return 3
                if not multi_objective_complete or not multi_objective_converged:
                    status["status"] = "blocked_aspen_multi_objective_not_converged"
                    status["message"] = (
                        "Generic Aspen optimization found closed-loop passes, but Pareto/TOPSIS has not converged yet. "
                        "Continue the optimization campaign or raise the max generation budget."
                    )
                    status["aspen_optimization_output_dir"] = str(optimization_dir.resolve())
                    status["process_import_after_optimization"] = process_summary
                    write_status(report, report_path, status)
                    return 3
                if not publication_budget_complete:
                    status["status"] = "blocked_aspen_publication_evidence_floor_incomplete"
                    status["message"] = (
                        "Generic Aspen optimization converged, but the publication evidence floor "
                        "(minimum generations/feasible points) has not been met."
                    )
                    status["aspen_optimization_output_dir"] = str(optimization_dir.resolve())
                    status["process_import_after_optimization"] = process_summary
                    write_status(report, report_path, status)
                    return 3
                if optimization_alignment in {"mismatch", "unknown"} or mainline_aspen_required or not mainline_aspen_converged:
                    status["status"] = "blocked_aspen_mainline_not_optimized"
                    status["current_stage"] = "aspen_mainline_optimization"
                    status["stage_status"] = "blocked"
                    status["message"] = (
                        "Aspen optimization evidence does not close the current locked solvent; "
                        "the agent must run or import mainline Aspen optimization for the selected solvent."
                    )
                    status["aspen_optimization_output_dir"] = str(optimization_dir.resolve())
                    status["process_import_after_optimization"] = process_summary
                    write_status(report, report_path, status)
                    return 3
                status["status"] = "complete"
                status["message"] = "Full-flow runner completed through closed-loop Aspen validation and multi-objective Pareto/TOPSIS optimization."
                status["aspen_optimization_output_dir"] = str(optimization_dir.resolve())
                status["process_import_after_optimization"] = process_summary
                write_status(report, report_path, status)
                return 0

        if task.get("compatible_verified_case"):
            output_dir = Path(report.get("output_dir") or report_path.parent) / "aspen_optimization_once_autopilot_{}".format(
                time.strftime("%Y%m%d_%H%M%S")
            )
            component_a = str(task.get("component_a") or "").strip()
            component_b = str(task.get("component_b") or "").strip()
            target_product = str(task.get("target_product") or "").strip()
            if not (component_a and component_b and target_product):
                status["status"] = "blocked_task_identity_missing"
                status["current_stage"] = "task_identity_validation"
                status["stage_status"] = "blocked"
                status["message"] = (
                    "Task identity is incomplete; component_a, component_b, and target_product "
                    "must be present before launching a process optimization branch."
                )
                status["task_identity_fields"] = {
                    "component_a": component_a,
                    "component_b": component_b,
                    "target_product": target_product,
                }
                write_status(report, report_path, status)
                return 3
            command = [
                sys.executable,
                str(agent.BASE_DIR / "full_screening_agent.py"),
                "--component-a", component_a,
                "--component-b", component_b,
                "--target-product", target_product,
                "--target-purity", str(task.get("target_purity") or 0.999),
                "--separation-class", str(task.get("separation_class") or "binary extractive distillation"),
                "--process-boundary", str(task.get("process_boundary") or "closed-loop extractive distillation"),
                "--property-method", str(task.get("property_method") or "PRWS"),
                "--feed-temperature-c", str(task.get("feed_temperature_c") or 50.0),
                "--feed-pressure-mpa", str(task.get("feed_pressure_mpa") or 0.1),
                "--feed-molar-ratio", str(task.get("feed_molar_ratio") or "90/10 mol%"),
                "--flow-validation-count", "1",
                "--run-optimization",
                "--optimization-population-size", "4",
                "--optimization-generations", "2",
                "--output-dir", str(output_dir),
            ]
            target_recovery = agent.finite_float(task.get("target_recovery"))
            if target_recovery is not None and 0.0 < target_recovery <= 1.0:
                command.extend(["--target-recovery", str(target_recovery)])
            rc = run_command(
                "aspen_optimization",
                command,
                agent.BASE_DIR,
                output_dir / "logs",
                status,
                report,
                report_path,
                timeout_seconds=420,
                cleanup_new_aspen=True,
            )
            if rc != 0:
                status["status"] = "failed"
                status["message"] = "Aspen optimization stage failed."
                write_status(report, report_path, status)
                return rc
            status["status"] = "complete"
            status["message"] = "Full-flow runner completed through one Aspen optimization campaign."
            status["aspen_optimization_output_dir"] = str(output_dir.resolve())
            write_status(report, report_path, status)
            return 0

        status["status"] = "blocked_before_aspen_optimization"
        status["message"] = "Molecular stages are prepared/imported, but Aspen plan has not yet passed its component/property/molecular gate."
        write_status(report, report_path, status)
        return 3
    except Exception as exc:
        status["status"] = "failed"
        status["message"] = "{}: {}".format(exc.__class__.__name__, exc)
        write_status(report, report_path, status)
        return 2


def main():
    parser = argparse.ArgumentParser(description="Run the binary-separation agent full-flow autopilot.")
    parser.add_argument("--report", required=True)
    parser.add_argument("--max-gaussian-jobs", type=int, default=None)
    parser.add_argument("--trial-mode", action="store_true")
    parser.add_argument("--solvent-code", default="", help="Lock molecular stages to one candidate solvent code/aspen id/name.")
    args = parser.parse_args()
    raise SystemExit(run_full_flow(args.report, max_gaussian_jobs=args.max_gaussian_jobs, trial_mode=args.trial_mode, solvent_code=args.solvent_code))


if __name__ == "__main__":
    main()
