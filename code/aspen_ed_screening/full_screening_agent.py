# -*- coding: utf-8 -*-
"""
System-input screening agent for constrained autonomous solvent screening.

The default validation case is the C3F8/C3F6 extractive-distillation workflow,
but the report is built around a binary-separation task profile rather than a
hard-coded solvent list. The agent connects the existing project pieces:

1. target-system input and constrained task instantiation;
2. molecular evidence from the frontend/API snapshot;
3. Aspen-ready solvent mapping;
4. zero-built Aspen BKP generation evidence;
5. process validation and NSGA-II/TOPSIS-style decision evidence;
6. repair/search queues for candidates that still need Aspen confirmation.

By default it runs in audit mode and does not launch Aspen. Use
--build-zero-bkp, --run-aspen-validation, or --run-optimization when a real
simulation campaign is intended.
"""

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import pandas as pd

import variable_map as vm
from aspen_runner import AspenCaseRunner
from run_screening import read_solvent_ids


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = BASE_DIR.parent / "screening_api_snapshot.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "generated" / "full_agent"
DEFAULT_ZERO_BKP = BASE_DIR / "generated" / "zero_agent_generated_ed_template.bkp"
DEFAULT_ZERO_REPORT = BASE_DIR / "generated" / "zero_agent_generated_ed_template.autoverify.json"

DEFAULT_COMPONENT_A = "C3F8"
DEFAULT_COMPONENT_B = "C3F6"
DEFAULT_TARGET_PRODUCT = "C3F8"
DEFAULT_TARGET_PURITY = 0.999
DEFAULT_TARGET_RECOVERY = None
DEFAULT_SEPARATION_CLASS = "near-boiling fluorocarbon binary"
DEFAULT_PROCESS_BOUNDARY = "closed-loop extractive distillation"
DEFAULT_PROPERTY_METHOD = "PRWS"
DEFAULT_VALIDATION_SCOPE = (
    "Framework is system-input driven; current verified case is C3F8/C3F6 "
    "within the extractive-distillation process boundary."
)

CODE_TO_ASPEN_ID = {
    "EG": "ETHYL-01",
    "ETHYLENE-GLYCOL": "ETHYL-01",
    "NMP": "NMP",
    "DMSO": "DMSO",
    "SULF": "SULFOLAN",
    "SULFOLANE": "SULFOLAN",
    "SULFOLAN": "SULFOLAN",
    "PC": "PC",
    "GBL": "GBL",
    "ADPN": "ADPN",
}

DEFAULT_CASE_VALUES = OrderedDict(
    [
        ("SF", 1.5),
        ("RR1", 4.0),
        ("D1", 8.974),
        ("RR2", 1.5),
        ("D2", 1.02501),
        ("N1", 60),
        ("NF1", 40),
        ("NS1", 20),
        ("N2", 12),
        ("NF2", 2),
    ]
)

REPAIR_GRID = [
    {"RR1": 6.0, "RR2": 2.0, "D1": 8.8},
    {"RR1": 3.0, "RR2": 1.2, "D1": 8.980945},
    {"RR1": 4.5, "RR2": 1.5, "D1": 8.6},
    {"SF": 2.0, "RR1": 5.0, "RR2": 2.0, "D1": 8.8},
]

WORKBOOK_SKIP_PATTERNS = (".tmp.", "~$", "_trial", "_smoke", "_paused", "_stalled")

PREFERRED_PROCESS_EVIDENCE_FILES = {
    ("SULFOLAN", "sulfolan_nsga2_tac_formal_100x70_expanded.xlsx"): 100,
    ("NMP", "nmp_nsga2_tac_formal_100x110_protected.xlsx"): 100,
}


def run_command_capture(command, cwd=None):
    return subprocess.run(
        command,
        cwd=str(cwd or BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def safe_token(value):
    raw = str(value or "").strip()
    token = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_").lower()
    return token or "x"


def condition_signature(args):
    return "feed_{}_purity_{}_t_{}_p_{}".format(
        safe_token(args.feed_molar_ratio),
        safe_token(args.target_purity),
        safe_token(args.feed_temperature_c),
        safe_token(args.feed_pressure_mpa),
    )


def build_separation_task(args):
    component_a = str(args.component_a or "").strip()
    component_b = str(args.component_b or "").strip()
    target_product = str(args.target_product or "").strip()
    task_id = "{}_{}_{}".format(component_a, component_b, target_product).replace("/", "_")
    output_dir = Path(args.output_dir)
    run_id = output_dir.name
    scenario_id = "{}__{}".format(task_id, condition_signature(args))
    default_case = (
        {component_a.upper(), component_b.upper()} == {DEFAULT_COMPONENT_A, DEFAULT_COMPONENT_B}
        and target_product.upper() == DEFAULT_TARGET_PRODUCT
        and str(args.process_boundary or "").strip().lower() == DEFAULT_PROCESS_BOUNDARY.lower()
        and math.isclose(finite_float(args.target_purity), DEFAULT_TARGET_PURITY, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(finite_float(args.feed_temperature_c), 50.0, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(finite_float(args.feed_pressure_mpa), 0.1, rel_tol=0.0, abs_tol=1e-9)
        and str(args.feed_molar_ratio or "").strip().lower() == "90/10 mol%"
    )
    validation_scope = (
        DEFAULT_VALIDATION_SCOPE
        if default_case
        else "Task input can be instantiated, but no compatible molecular snapshot or Aspen process template has been verified for this binary system yet."
    )
    validation_role = "regression_only_verified_case" if default_case else "publication_validation_candidate"
    return OrderedDict(
        [
            ("task_id", task_id),
            ("system_id", task_id),
            ("scenario_id", scenario_id),
            ("run_id", run_id),
            ("created_at", dt.datetime.now().isoformat(timespec="seconds")),
            ("condition_signature", condition_signature(args)),
            ("condition_summary", "feed {}; purity {}; T {} C; P {} MPa".format(
                args.feed_molar_ratio,
                finite_float(args.target_purity),
                finite_float(args.feed_temperature_c),
                finite_float(args.feed_pressure_mpa),
            )),
            ("component_a", component_a),
            ("component_b", component_b),
            ("component_pair", [component_a, component_b]),
            ("target_product", target_product),
            ("target_purity", finite_float(args.target_purity)),
            ("target_recovery", finite_float(args.target_recovery)),
            ("target_recovery_mode", "hard_constraint" if finite_float(args.target_recovery) is not None else "maximize_only"),
            ("separation_class", args.separation_class),
            ("process_boundary", args.process_boundary),
            ("property_method", args.property_method),
            ("feed_temperature_c", finite_float(args.feed_temperature_c)),
            ("feed_pressure_mpa", finite_float(args.feed_pressure_mpa)),
            ("feed_molar_ratio", args.feed_molar_ratio),
            ("flow_validation_count", int(args.flow_validation_count)),
            ("run_aspen_validation", bool(args.run_aspen_validation)),
            ("run_optimization", bool(args.run_optimization)),
            ("optimization_population_size", int(args.optimization_population_size)),
            ("optimization_generations", int(args.optimization_generations)),
            ("validation_bkp", str(Path(args.zero_bkp if args.use_zero_bkp_for_validation else vm.TEMPLATE_BKP).resolve())),
            ("instantiation_mode", "system_input_default_case" if default_case else "system_input_override"),
            ("compatible_verified_case", default_case),
            ("validation_role", validation_role),
            ("publication_validation_eligible", not default_case),
            ("validation_scope", validation_scope),
        ]
    )


def agent_capability_profile(task):
    return OrderedDict(
        [
            ("agent_type", "constrained_autonomous_solvent_screening_agent"),
            ("not_plain_workflow_reason", "maintains candidate/evidence state and generates validation queues from the current task profile"),
            ("human_boundary", "Aspen execution remains an explicit controlled action; audit refresh is automatic and safe"),
            ("portable_input_contract", ["component_pair", "target_product", "purity/recovery constraints", "process_boundary", "available_tools"]),
            ("current_tools", ["molecular_snapshot_frontend", "Gaussian/BSSE evidence", "GROMACS/MD evidence", "Aspen Plus template boundary", "NSGA-II", "TOPSIS"]),
            ("current_case", "{} / {}".format(task.get("component_a"), task.get("component_b"))),
            ("transfer_claim", "can be re-instantiated for another binary separation after providing task inputs and compatible property/process templates"),
        ]
    )


def evidence_state_policy():
    return [
        OrderedDict(
            [
                ("state", "system_input"),
                ("gate", "component pair, target product and process boundary are defined"),
                ("next_action", "instantiate candidate pool and solvent-screening rules"),
            ]
        ),
        OrderedDict(
            [
                ("state", "candidate_pool"),
                ("gate", "seed solvents plus feedback/active-learning suggestions are available"),
                ("next_action", "schedule molecular evidence checks"),
            ]
        ),
        OrderedDict(
            [
                ("state", "molecular_evidence"),
                ("gate", "Gaussian/BSSE and MD interaction evidence are complete and directionally consistent"),
                ("next_action", "score distillation suitability and decide validation priority"),
            ]
        ),
        OrderedDict(
            [
                ("state", "process_validation_queue"),
                ("gate", "candidate is Aspen-configured and passes molecular/process-boundary checks"),
                ("next_action", "queue explicit Aspen validation or optimization dry-run"),
            ]
        ),
        OrderedDict(
            [
                ("state", "process_decision"),
                ("gate", "process evidence or queued validation cases exist"),
                ("next_action", "rank by molecular/process evidence and TOPSIS-style trade-off"),
            ]
        ),
    ]


def transfer_gap_report(task, compatible_verified_case):
    if compatible_verified_case:
        return [
            OrderedDict([("item", "molecular_snapshot"), ("status", "available"), ("detail", "C3F8/C3F6 molecular screening snapshot is available")]),
            OrderedDict([("item", "aspen_template"), ("status", "available"), ("detail", "C3F8/C3F6 closed-loop ED template boundary is available")]),
            OrderedDict([("item", "process_evidence"), ("status", "available"), ("detail", "NMP and sulfolane optimization workbooks are available")]),
        ]
    pair = "/".join(task.get("component_pair") or [])
    return [
        OrderedDict([("item", "molecular_snapshot"), ("status", "missing"), ("detail", "Need a {} molecular-screening snapshot before evidence gating".format(pair))]),
        OrderedDict([("item", "candidate_seed_rules"), ("status", "missing"), ("detail", "Need solvent seed/rule set appropriate for {} and target {}".format(pair, task.get("target_product")))]),
        OrderedDict([("item", "thermo_basis"), ("status", "missing"), ("detail", "Need property-method/VLE basis for {} under the selected process boundary".format(pair))]),
        OrderedDict([("item", "aspen_template"), ("status", "missing"), ("detail", "Need compatible Aspen process template or generated BKP for {}".format(pair))]),
        OrderedDict([("item", "process_evidence"), ("status", "missing"), ("detail", "No NSGA-II/TOPSIS process evidence should be reused from another binary system")]),
    ]


def finite_float(value):
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value):
        return None
    return value


def as_bool(value):
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().upper() in ("TRUE", "1", "YES", "Y", "PASS", "OK")


def clean_scalar(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def normalize_code(value):
    return str(value or "").strip().upper().replace("-", "_")


def clamp01(value):
    value = finite_float(value)
    if value is None:
        return 0.0
    return max(0.0, min(1.0, value))


def normalize_cost_values(rows, field):
    values = [finite_float(row.get(field)) for row in rows]
    finite = [value for value in values if value is not None]
    if not finite:
        return [0.0 for _ in rows]
    low, high = min(finite), max(finite)
    if high <= low:
        return [1.0 if value is not None else 0.0 for value in values]
    return [0.0 if value is None else (high - value) / (high - low) for value in values]


def normalize_benefit_values(rows, field):
    values = [finite_float(row.get(field)) for row in rows]
    finite = [value for value in values if value is not None]
    if not finite:
        return [0.0 for _ in rows]
    low, high = min(finite), max(finite)
    if high <= low:
        return [1.0 if value is not None else 0.0 for value in values]
    return [0.0 if value is None else (value - low) / (high - low) for value in values]


def load_solvents(path):
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_excel(str(path))


def solvent_lookup(solvents_df):
    by_component = {}
    by_code = {}
    if solvents_df.empty:
        return by_component, by_code
    for _, row in solvents_df.iterrows():
        component_id = str(row.get("aspen_component_id") or "").strip()
        if not component_id:
            continue
        by_component[component_id] = {key: clean_scalar(row.get(key)) for key in solvents_df.columns}
        by_code[normalize_code(component_id)] = component_id
        by_code[normalize_code(row.get("solvent_name"))] = component_id
    for code, component_id in CODE_TO_ASPEN_ID.items():
        if component_id in by_component:
            by_code[normalize_code(code)] = component_id
    return by_component, by_code


def objective_from_row(row):
    assessment = row.get("assessment") or {}
    return row.get("objective") or assessment.get("objective") or {}


def molecular_record(row, by_code):
    objective = objective_from_row(row)
    metrics = objective.get("metrics") or row.get("rdf") or {}
    distillation = objective.get("distillation") or {}
    code = str(row.get("code") or "").strip()
    aspen_id = by_code.get(normalize_code(code))
    if aspen_id is None:
        aspen_id = by_code.get(normalize_code(row.get("name")))

    objective_score = finite_float(objective.get("score"))
    molecule_score = finite_float(objective.get("molecule_score"))
    distillation_score = finite_float(objective.get("distillation_score"))
    category = objective.get("category")
    metrics_complete = (
        finite_float(metrics.get("gaussian_delta_kcal_mol")) is not None
        and finite_float(metrics.get("md_delta_kj_mol")) is not None
    )
    score_ready = objective_score is not None
    source = row.get("source")

    return OrderedDict(
        [
            ("code", code),
            ("name", row.get("name")),
            ("source", source),
            ("status", row.get("status")),
            ("category", category),
            ("objective_score", objective_score),
            ("molecule_score", molecule_score),
            ("distillation_score", distillation_score),
            ("gaussian_delta_kcal_mol", finite_float(metrics.get("gaussian_delta_kcal_mol"))),
            ("md_delta_kj_mol", finite_float(metrics.get("md_delta_kj_mol"))),
            ("rdf_peak_c3f6", finite_float(metrics.get("rdf_peak_c3f6"))),
            ("rdf_peak_c3f8", finite_float(metrics.get("rdf_peak_c3f8"))),
            ("coordination_c3f6_0p80nm", finite_float(metrics.get("coordination_c3f6_0p80nm"))),
            ("coordination_c3f8_0p80nm", finite_float(metrics.get("coordination_c3f8_0p80nm"))),
            ("diffusion_c3f6", finite_float(metrics.get("diffusion_c3f6"))),
            ("diffusion_c3f8", finite_float(metrics.get("diffusion_c3f8"))),
            ("bp_c", finite_float(metrics.get("bp_c") or distillation.get("bp_c"))),
            ("stage_decision", objective.get("stage_decision")),
            ("risk", objective.get("risk")),
            ("issue_count", len(objective.get("issues") or [])),
            ("molecular_evidence_complete", bool(metrics_complete and score_ready)),
            ("aspen_component_id", aspen_id),
            ("aspen_configured", bool(aspen_id)),
            ("recommendation_reason", objective.get("reason")),
        ]
    )


def load_molecular_snapshot(path, by_code):
    snapshot = read_json(path)
    rows = [molecular_record(row, by_code) for row in snapshot.get("rows", [])]
    rows.sort(
        key=lambda row: (
            not row.get("molecular_evidence_complete"),
            -(finite_float(row.get("objective_score")) or -999.0),
            -(finite_float(row.get("molecule_score")) or -999.0),
            str(row.get("code") or ""),
        )
    )
    return snapshot, rows


def choose_flow_candidates(molecular_rows, count):
    eligible = []
    for row in molecular_rows:
        score = finite_float(row.get("objective_score"))
        if not row.get("aspen_configured") or not row.get("molecular_evidence_complete"):
            continue
        if score is None or score < 50.0:
            continue
        eligible.append(dict(row))
    eligible.sort(
        key=lambda row: (
            -(finite_float(row.get("objective_score")) or -999.0),
            -(finite_float(row.get("molecule_score")) or -999.0),
            -(finite_float(row.get("distillation_score")) or -999.0),
            -(finite_float(row.get("md_delta_kj_mol")) or -999.0),
            -(finite_float(row.get("gaussian_delta_kcal_mol")) or -999.0),
            str(row.get("aspen_component_id") or ""),
        )
    )
    selected = []
    seen_components = set()
    for row in eligible:
        component_id = row.get("aspen_component_id")
        if component_id in seen_components:
            continue
        seen_components.add(component_id)
        selected.append(row)
        if len(selected) >= count:
            break
    return selected


def workbook_is_candidate(path):
    name = path.name.lower()
    if not name.endswith(".xlsx"):
        return False
    return not any(pattern in name for pattern in WORKBOOK_SKIP_PATTERNS)


def process_evidence_source_priority(row):
    component_id = str(row.get("aspen_component_id") or "").upper()
    source_file = str(row.get("source_file") or "").lower()
    return PREFERRED_PROCESS_EVIDENCE_FILES.get((component_id, source_file), 0)


def first_feasible_row(df):
    if df is None or df.empty:
        return None
    rows = df.to_dict(orient="records")
    feasible = [
        row for row in rows
        if as_bool(row.get("feasible")) or str(row.get("pass_or_fail") or "").strip().upper() == "PASS"
    ]
    if feasible:
        feasible.sort(
            key=lambda row: (
                -(finite_float(row.get("decision_score")) or -1.0),
                finite_float(row.get("TAC")) or float("inf"),
                finite_float(row.get("total_reboiler_duty")) or float("inf"),
            )
        )
        return feasible[0]
    rows.sort(
        key=lambda row: (
            finite_float(row.get("constraint_violation")) or float("inf"),
            finite_float(row.get("TAC")) or float("inf"),
            finite_float(row.get("total_reboiler_duty")) or float("inf"),
        )
    )
    return rows[0] if rows else None


def process_summary_from_workbook(path):
    path = Path(path)
    try:
        excel = pd.ExcelFile(path)
    except Exception:
        return None
    if "decision_rank" not in excel.sheet_names and "all_evaluations" not in excel.sheet_names:
        return None

    decision_df = None
    all_df = None
    pareto_count = 0
    try:
        if "decision_rank" in excel.sheet_names:
            decision_df = excel.parse("decision_rank")
        if "all_evaluations" in excel.sheet_names:
            all_df = excel.parse("all_evaluations")
        if "pareto" in excel.sheet_names:
            pareto_count = len(excel.parse("pareto"))
    except Exception:
        return None

    best = first_feasible_row(decision_df)
    if best is None:
        best = first_feasible_row(all_df)
    if not best:
        return None

    component_id = str(best.get("aspen_component_id") or "").strip()
    if not component_id:
        return None

    all_records = [] if all_df is None else all_df.to_dict(orient="records")
    feasible_count = sum(1 for row in all_records if as_bool(row.get("feasible")) or str(row.get("pass_or_fail") or "").upper() == "PASS")
    summary = OrderedDict()
    summary["aspen_component_id"] = component_id
    summary["solvent_name"] = clean_scalar(best.get("solvent_name"))
    summary["source_type"] = "optimization_workbook"
    summary["source_path"] = str(path.resolve())
    summary["source_file"] = path.name
    summary["source_mtime"] = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    summary["evaluation_count"] = len(all_records)
    summary["feasible_count"] = feasible_count
    summary["pareto_count"] = pareto_count
    for key in [
        "case_id",
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
        "feasible",
        "pass_or_fail",
        "x_C3F8_top1",
        "recovery_C3F8",
        "x_C3F6_top2",
        "recovery_C3F6",
        "Qreb_1",
        "Qreb_2",
        "total_reboiler_duty",
        "TAC",
        "energy_cost",
        "decision_score",
    ]:
        summary[key] = clean_scalar(best.get(key))
    return summary


def zero_report_summary(path):
    report = read_json(path)
    smoke = report.get("smoke_result") or {}
    if not smoke:
        return None
    component_id = str(smoke.get("aspen_component_id") or "NMP")
    summary = OrderedDict()
    summary["aspen_component_id"] = component_id
    summary["solvent_name"] = smoke.get("solvent_name")
    summary["source_type"] = "zero_bkp_smoke"
    summary["source_path"] = str(Path(path).resolve())
    summary["source_file"] = Path(path).name
    summary["source_mtime"] = dt.datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat(timespec="seconds")
    summary["evaluation_count"] = 1
    summary["feasible_count"] = 1 if str(smoke.get("pass_or_fail") or "").upper() == "PASS" else 0
    summary["pareto_count"] = 0
    for key in [
        "case_id",
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
        "converged",
        "pass_or_fail",
        "x_C3F8_top1",
        "recovery_C3F8",
        "x_C3F6_top2",
        "recovery_C3F6",
        "Qreb_1",
        "Qreb_2",
        "total_reboiler_duty",
        "TAC",
        "energy_cost",
        "decision_score",
    ]:
        summary[key] = clean_scalar(smoke.get(key))
    summary["validation_role"] = report.get("validation_role")
    return summary


def merge_baseline_screening(base_results, cases_path):
    results_path = Path(base_results)
    cases_path = Path(cases_path)
    if not results_path.exists() or not cases_path.exists():
        return []
    try:
        results = pd.read_excel(str(results_path))
        cases = pd.read_excel(str(cases_path))
    except Exception:
        return []
    if "case_id" not in results.columns or "case_id" not in cases.columns:
        return []
    merged = results.merge(cases, on="case_id", how="left", suffixes=("", "_case"))
    summaries = []
    for _, row in merged.iterrows():
        component_id = str(row.get("aspen_component_id") or "").strip()
        if not component_id:
            continue
        summary = OrderedDict()
        summary["aspen_component_id"] = component_id
        summary["solvent_name"] = clean_scalar(row.get("solvent_name"))
        summary["source_type"] = "baseline_screening"
        summary["source_path"] = str(results_path.resolve())
        summary["source_file"] = results_path.name
        summary["source_mtime"] = dt.datetime.fromtimestamp(results_path.stat().st_mtime).isoformat(timespec="seconds")
        summary["evaluation_count"] = 1
        summary["feasible_count"] = 1 if str(row.get("pass_or_fail") or "").upper() == "PASS" else 0
        summary["pareto_count"] = 0
        for key in [
            "case_id",
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
            "converged",
            "pass_or_fail",
            "x_C3F8_top1",
            "recovery_C3F8",
            "x_C3F6_top2",
            "recovery_C3F6",
            "Qreb_1",
            "Qreb_2",
            "total_reboiler_duty",
            "TAC",
            "energy_cost",
            "decision_score",
        ]:
            summary[key] = clean_scalar(row.get(key))
        summaries.append(summary)
    return summaries


def load_process_evidence(output_dir, zero_report, results_path, cases_path):
    evidence = []
    optimization_dir = Path(output_dir)
    if optimization_dir.exists():
        for path in sorted(optimization_dir.glob("*.xlsx")):
            if not workbook_is_candidate(path):
                continue
            summary = process_summary_from_workbook(path)
            if summary:
                evidence.append(summary)
    zero_summary = zero_report_summary(zero_report)
    if zero_summary:
        evidence.append(zero_summary)
    evidence.extend(merge_baseline_screening(results_path, cases_path))
    return evidence


def best_process_by_component(evidence):
    best = {}
    for row in evidence:
        component_id = row.get("aspen_component_id")
        if not component_id:
            continue
        current = best.get(component_id)
        key = (
            1 if row.get("source_type") == "optimization_workbook" else 0,
            process_evidence_source_priority(row),
            finite_float(row.get("decision_score")) or -1.0,
            finite_float(row.get("recovery_C3F8")) or -1.0,
            -(finite_float(row.get("TAC")) or float("inf")),
            str(row.get("source_mtime") or ""),
        )
        if current is None:
            best[component_id] = dict(row)
            best[component_id]["_selection_key"] = key
            continue
        if key > current.get("_selection_key"):
            best[component_id] = dict(row)
            best[component_id]["_selection_key"] = key
    for row in best.values():
        row.pop("_selection_key", None)
    return best


def molecular_score_01(row):
    score = finite_float(row.get("objective_score"))
    if score is None:
        return 0.0
    return clamp01((score + 50.0) / 150.0)


def process_scores(process_rows):
    pass_rows = [
        row for row in process_rows
        if str(row.get("pass_or_fail") or "").upper() == "PASS" or as_bool(row.get("feasible"))
    ]
    if not pass_rows:
        return {}
    recovery_norm = normalize_benefit_values(pass_rows, "recovery_C3F8")
    tac_norm = normalize_cost_values(pass_rows, "TAC")
    duty_norm = normalize_cost_values(pass_rows, "total_reboiler_duty")
    scores = {}
    for idx, row in enumerate(pass_rows):
        decision = finite_float(row.get("decision_score"))
        if decision is None:
            decision = 0.0
        score = (
            0.35 * clamp01(decision)
            + 0.25 * recovery_norm[idx]
            + 0.25 * tac_norm[idx]
            + 0.15 * duty_norm[idx]
        )
        scores[row.get("aspen_component_id")] = max(scores.get(row.get("aspen_component_id"), 0.0), clamp01(score))
    return scores


def final_decision_rank(molecular_rows, best_process):
    process_rows = list(best_process.values())
    proc_scores = process_scores(process_rows)
    ranked = []
    for mol in molecular_rows:
        component_id = mol.get("aspen_component_id")
        proc = best_process.get(component_id) if component_id else None
        m_score = molecular_score_01(mol)
        p_score = proc_scores.get(component_id, 0.0)
        readiness = 1.0 if mol.get("aspen_configured") else 0.0
        if proc:
            readiness = max(readiness, 1.0)
        final = 0.45 * m_score + 0.45 * p_score + 0.10 * readiness
        if not mol.get("molecular_evidence_complete"):
            final *= 0.5
        row = OrderedDict()
        for key in [
            "code",
            "name",
            "source",
            "status",
            "category",
            "objective_score",
            "molecule_score",
            "distillation_score",
            "gaussian_delta_kcal_mol",
            "md_delta_kj_mol",
            "bp_c",
            "aspen_component_id",
            "aspen_configured",
        ]:
            row[key] = mol.get(key)
        row["process_evidence_source"] = proc.get("source_file") if proc else None
        row["process_case_id"] = proc.get("case_id") if proc else None
        row["process_pass_or_fail"] = proc.get("pass_or_fail") if proc else None
        row["process_x_C3F8_top1"] = proc.get("x_C3F8_top1") if proc else None
        row["process_recovery_C3F8"] = proc.get("recovery_C3F8") if proc else None
        row["process_TAC"] = proc.get("TAC") if proc else None
        row["process_total_reboiler_duty"] = proc.get("total_reboiler_duty") if proc else None
        row["process_decision_score"] = proc.get("decision_score") if proc else None
        row["agent_molecular_score"] = round(m_score, 6)
        row["agent_process_score"] = round(p_score, 6)
        row["agent_readiness_score"] = round(readiness, 6)
        row["agent_final_score"] = round(final, 6)
        if proc and str(proc.get("source_type")) == "optimization_workbook":
            row["agent_state"] = "optimized_process_evidence"
        elif proc:
            row["agent_state"] = "process_validation_evidence"
        elif mol.get("aspen_configured"):
            row["agent_state"] = "queued_for_aspen_validation"
        elif mol.get("molecular_evidence_complete"):
            row["agent_state"] = "molecular_only"
        else:
            row["agent_state"] = "missing_molecular_steps"
        ranked.append(row)
    ranked.sort(key=lambda row: (-(finite_float(row.get("agent_final_score")) or 0.0), str(row.get("code") or "")))
    return ranked


def values_from_process_or_default(component_id, process_row):
    values = OrderedDict(DEFAULT_CASE_VALUES)
    if process_row:
        for key in DEFAULT_CASE_VALUES:
            value = finite_float(process_row.get(key))
            if value is not None:
                if key in ("N1", "NF1", "NS1", "N2", "NF2"):
                    value = int(round(value))
                values[key] = value
    return values


def candidate_case_row(candidate, process_row, index, prefix):
    component_id = candidate.get("aspen_component_id")
    values = values_from_process_or_default(component_id, process_row)
    row = OrderedDict()
    row["case_id"] = "{}{:03d}_{}".format(prefix, index, component_id)
    row["solvent_name"] = candidate.get("name") or component_id
    row["aspen_component_id"] = component_id
    for key, value in values.items():
        row[key] = value
    return row


def build_validation_cases(selected, best_process, prefix="AGTVAL"):
    rows = []
    for index, candidate in enumerate(selected, start=1):
        component_id = candidate.get("aspen_component_id")
        rows.append(candidate_case_row(candidate, best_process.get(component_id), index, prefix))
    return rows


def build_repair_cases(selected, best_process, max_per_candidate=4):
    rows = []
    for candidate in selected:
        component_id = candidate.get("aspen_component_id")
        proc = best_process.get(component_id)
        if proc and (str(proc.get("pass_or_fail") or "").upper() == "PASS" or as_bool(proc.get("feasible"))):
            continue
        base = values_from_process_or_default(component_id, proc)
        for idx, patch in enumerate(REPAIR_GRID[:max_per_candidate], start=1):
            values = OrderedDict(base)
            values.update(patch)
            row = OrderedDict()
            row["case_id"] = "AGTRPR{:03d}_{}".format(idx, component_id)
            row["solvent_name"] = candidate.get("name") or component_id
            row["aspen_component_id"] = component_id
            for key, value in values.items():
                row[key] = value
            row["repair_reason"] = "cold-start fallback search"
            rows.append(row)
    return rows


def write_excel_report(path, sheets):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.stem + ".tmp" + path.suffix)
    with pd.ExcelWriter(str(tmp_path), engine="openpyxl") as writer:
        for sheet_name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=sheet_name[:31], index=False)
    tmp_path.replace(path)
    return path


def write_markdown(path, report):
    lines = []
    task = report.get("separation_task") or {}
    capability = report.get("agent_capability") or {}
    lines.append("# System-Input Autonomous Screening Agent Report")
    lines.append("")
    lines.append("- Generated at: {}".format(report["generated_at"]))
    lines.append("- Separation task: {} / {} -> {}".format(task.get("component_a"), task.get("component_b"), task.get("target_product")))
    lines.append("- Process boundary: {}".format(task.get("process_boundary")))
    lines.append("- Target purity: {}".format(task.get("target_purity")))
    lines.append("- Agent type: {}".format(capability.get("agent_type")))
    lines.append("- Workflow distinction: {}".format(capability.get("not_plain_workflow_reason")))
    lines.append("- Molecular candidates: {}".format(report["counts"]["molecular_candidates"]))
    lines.append("- Aspen-configured candidates: {}".format(report["counts"]["aspen_configured_candidates"]))
    lines.append("- Selected for flow validation: {}".format(report["counts"]["selected_flow_candidates"]))
    lines.append("- Process evidence records: {}".format(report["counts"]["process_evidence_records"]))
    lines.append("")
    lines.append("## Evidence-State Policy")
    lines.append("")
    for item in report.get("evidence_state_policy") or []:
        lines.append("- {}: {} -> {}".format(item.get("state"), item.get("gate"), item.get("next_action")))
    lines.append("")
    lines.append("## Agent Chain")
    for step in report["agent_steps"]:
        lines.append("- {}: {}".format(step["step"], step["status"]))
    lines.append("")
    lines.append("## Top Decision Rank")
    lines.append("")
    lines.append("| rank | code | component | molecular | process | final | state | evidence |")
    lines.append("|---:|---|---|---:|---:|---:|---|---|")
    for idx, row in enumerate(report["top_final_rank"], start=1):
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                idx,
                row.get("code") or "",
                row.get("aspen_component_id") or "",
                row.get("objective_score") if row.get("objective_score") is not None else "",
                row.get("agent_process_score") if row.get("agent_process_score") is not None else "",
                row.get("agent_final_score") if row.get("agent_final_score") is not None else "",
                row.get("agent_state") or "",
                row.get("process_evidence_source") or "",
            )
        )
    lines.append("")
    lines.append("## Wording Guardrail")
    lines.append("")
    lines.append("Use the zero-built BKP smoke result as automation evidence, not as the optimized process result.")
    lines.append("Use optimization workbooks as process-result evidence for NMP and sulfolane.")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def build_zero_bkp(smoke_run=False):
    command = [sys.executable, str(BASE_DIR / "zero_build_aspen_agent.py"), "--write-default-spec"]
    if smoke_run:
        command.append("--smoke-run")
    result = run_command_capture(command, cwd=BASE_DIR)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def run_aspen_validation_cases(rows, bkp_path, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = AspenCaseRunner(template_bkp=bkp_path, case_work_dir=output_dir / "case_files", visible=False)
    solvent_ids = read_solvent_ids(vm.SOLVENTS_XLSX, None)
    results = []
    try:
        for row in rows:
            results.append(runner.run_case(row, solvent_ids))
    finally:
        runner.close_session()
    return results


def optimization_command(candidate, args):
    component_id = candidate.get("aspen_component_id")
    safe_component = str(component_id).lower().replace("-", "_")
    output = Path(args.output_dir) / "optimization" / "{}_agent_nsga2.xlsx".format(safe_component)
    case_work_dir = Path(args.output_dir) / "optimization_cases" / safe_component
    command = [
        sys.executable,
        str(BASE_DIR / "optimize_nmp_nsga2.py"),
        "--template",
        str(args.zero_bkp if args.use_zero_bkp_for_optimization else vm.TEMPLATE_BKP),
        "--output",
        str(output),
        "--case-work-dir",
        str(case_work_dir),
        "--case-prefix",
        "{}AG".format(str(component_id).replace("-", "")[:6].upper()),
        "--solvent-name",
        str(candidate.get("name") or component_id),
        "--aspen-component-id",
        str(component_id),
        "--population-size",
        str(args.optimization_population_size),
        "--generations",
        str(args.optimization_generations),
        "--workbook-write-interval",
        str(args.workbook_write_interval),
    ]
    if args.optimization_dry_run:
        command.append("--dry-run")
    return command


def run_optimization_commands(selected, args):
    records = []
    for candidate in selected:
        command = optimization_command(candidate, args)
        if "--output" in command:
            output_path = Path(command[command.index("--output") + 1])
            checkpoint_path = output_path.with_suffix(".checkpoint.jsonl")
            for stale_path in (output_path, checkpoint_path):
                try:
                    if stale_path.exists():
                        stale_path.unlink()
                except OSError:
                    pass
        try:
            result = run_command_capture(command, cwd=BASE_DIR)
            record = {
                "aspen_component_id": candidate.get("aspen_component_id"),
                "command": command,
                "returncode": result.returncode,
                "stdout_tail": (result.stdout or "")[-2000:],
                "stderr_tail": (result.stderr or "")[-2000:],
            }
        except Exception as exc:
            record = {
                "aspen_component_id": candidate.get("aspen_component_id"),
                "command": command,
                "returncode": -1,
                "stdout_tail": "",
                "stderr_tail": str(exc)[-2000:],
                "error": str(exc),
            }
        records.append(record)
    return records


def agent_step(step, status, detail=None):
    return {"step": step, "status": status, "detail": detail or ""}


def parse_args():
    parser = argparse.ArgumentParser(description="Run the system-input constrained autonomous solvent-screening agent.")
    parser.add_argument("--component-a", default=DEFAULT_COMPONENT_A)
    parser.add_argument("--component-b", default=DEFAULT_COMPONENT_B)
    parser.add_argument("--target-product", default=DEFAULT_TARGET_PRODUCT)
    parser.add_argument("--target-purity", type=float, default=DEFAULT_TARGET_PURITY)
    parser.add_argument(
        "--target-recovery",
        type=float,
        default=DEFAULT_TARGET_RECOVERY,
        help="Optional minimum recovery. When omitted, recovery remains a maximization objective rather than a hard constraint.",
    )
    parser.add_argument("--separation-class", default=DEFAULT_SEPARATION_CLASS)
    parser.add_argument("--process-boundary", default=DEFAULT_PROCESS_BOUNDARY)
    parser.add_argument("--property-method", default=DEFAULT_PROPERTY_METHOD)
    parser.add_argument("--feed-temperature-c", type=float, default=50.0)
    parser.add_argument("--feed-pressure-mpa", type=float, default=0.1)
    parser.add_argument("--feed-molar-ratio", default="90/10 mol%")
    parser.add_argument(
        "--allow-cross-system-evidence",
        action="store_true",
        help="Allow using the current molecular/process evidence even when the requested binary system differs from the verified C3F8/C3F6 case.",
    )
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--solvents", default=str(vm.SOLVENTS_XLSX))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--zero-bkp", default=str(DEFAULT_ZERO_BKP))
    parser.add_argument("--zero-report", default=str(DEFAULT_ZERO_REPORT))
    parser.add_argument("--flow-validation-count", type=int, default=2)
    parser.add_argument("--build-zero-bkp", action="store_true")
    parser.add_argument("--zero-smoke", action="store_true")
    parser.add_argument("--run-aspen-validation", action="store_true")
    parser.add_argument("--run-optimization", action="store_true")
    parser.add_argument("--optimization-dry-run", action="store_true")
    parser.add_argument("--use-zero-bkp-for-validation", action="store_true")
    parser.add_argument("--optimization-population-size", type=int, default=4)
    parser.add_argument("--optimization-generations", type=int, default=0)
    parser.add_argument("--workbook-write-interval", type=int, default=4)
    parser.add_argument("--use-zero-bkp-for-optimization", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    steps = []
    separation_task = build_separation_task(args)
    compatible_verified_case = bool(separation_task.get("compatible_verified_case"))
    evidence_allowed = compatible_verified_case or args.allow_cross_system_evidence
    capability = agent_capability_profile(separation_task)
    policy = evidence_state_policy()
    transfer_gaps = transfer_gap_report(separation_task, compatible_verified_case)
    steps.append(
        agent_step(
            "system_input_instantiation",
            "done",
            "{} -> {} target, boundary={}".format(
                "/".join(separation_task["component_pair"]),
                separation_task["target_product"],
                separation_task["process_boundary"],
            ),
        )
    )
    if compatible_verified_case:
        steps.append(agent_step("task_template_compatibility", "done", "verified C3F8/C3F6 evidence/template boundary"))
    elif args.allow_cross_system_evidence:
        steps.append(agent_step("task_template_compatibility", "forced", "cross-system evidence reuse explicitly allowed"))
    else:
        steps.append(agent_step("task_template_compatibility", "blocked", "no compatible molecular snapshot/Aspen template for this binary system"))

    build_result = None
    if not evidence_allowed:
        steps.append(agent_step("zero_bkp_generation", "blocked", "no compatible Aspen BKP generator/template for this binary system"))
    elif args.build_zero_bkp:
        build_result = build_zero_bkp(smoke_run=args.zero_smoke)
        steps.append(agent_step("zero_bkp_generation", "done" if build_result["returncode"] == 0 else "failed"))
    else:
        steps.append(agent_step("zero_bkp_generation", "reused_existing", str(args.zero_bkp)))

    solvents_df = load_solvents(args.solvents)
    by_component, by_code = solvent_lookup(solvents_df)

    if evidence_allowed:
        snapshot, molecular_rows = load_molecular_snapshot(args.snapshot, by_code)
        steps.append(agent_step("molecular_evidence_ingestion", "done", "{} rows".format(len(molecular_rows))))
        selected = choose_flow_candidates(molecular_rows, args.flow_validation_count)
        steps.append(agent_step("candidate_selection", "done", "{} selected".format(len(selected))))
        process_evidence = load_process_evidence(BASE_DIR / "outputs" / "optimization", args.zero_report, vm.RESULTS_XLSX, vm.CASES_XLSX)
        best_process = best_process_by_component(process_evidence)
        steps.append(agent_step("process_evidence_ingestion", "done", "{} records".format(len(process_evidence))))
        validation_cases = build_validation_cases(selected, best_process)
        repair_cases = build_repair_cases(selected, best_process)
    else:
        snapshot = {"rows": [], "active_learning": {}, "global_autopilot": {}}
        molecular_rows = []
        selected = []
        process_evidence = []
        best_process = {}
        validation_cases = []
        repair_cases = []
        steps.append(agent_step("molecular_evidence_ingestion", "blocked", "missing compatible {} snapshot".format("/".join(separation_task["component_pair"]))))
        steps.append(agent_step("candidate_selection", "blocked", "candidate pool must be generated for this binary system"))
        steps.append(agent_step("process_evidence_ingestion", "blocked", "no compatible Aspen/process evidence available"))

    validation_results = []
    if not evidence_allowed:
        steps.append(agent_step("aspen_validation", "blocked", "no compatible Aspen template for this binary system"))
    elif args.run_aspen_validation:
        validation_bkp = args.zero_bkp if args.use_zero_bkp_for_validation else vm.TEMPLATE_BKP
        validation_results = run_aspen_validation_cases(validation_cases, validation_bkp, output_dir / "aspen_validation")
        steps.append(agent_step("aspen_validation", "done", "{} cases".format(len(validation_results))))
        process_evidence.extend(validation_results)
        best_process = best_process_by_component(process_evidence)
    else:
        steps.append(agent_step("aspen_validation", "queued", "{} cases".format(len(validation_cases))))

    optimization_runs = []
    if not evidence_allowed:
        steps.append(agent_step("nsga2_optimization", "blocked", "no compatible validation candidates for this binary system"))
    elif args.run_optimization:
        optimization_runs = run_optimization_commands(selected, args)
        status = "done" if all(row["returncode"] == 0 for row in optimization_runs) else "partial"
        steps.append(agent_step("nsga2_optimization", status, "{} commands".format(len(optimization_runs))))
    else:
        steps.append(agent_step("nsga2_optimization", "queued", "{} candidates".format(len(selected))))

    final_rank = final_decision_rank(molecular_rows, best_process)

    sheets = OrderedDict(
        [
            ("separation_task", [separation_task]),
            ("agent_capability", [capability]),
            ("evidence_state_policy", policy),
            ("transfer_gap_report", transfer_gaps),
            ("molecular_candidates", molecular_rows),
            ("selected_flow_candidates", selected),
            ("process_evidence", process_evidence),
            ("final_decision_rank", final_rank),
            ("validation_cases", validation_cases),
            ("repair_cases", repair_cases),
            ("agent_steps", steps),
        ]
    )
    if validation_results:
        sheets["validation_results"] = validation_results
    if optimization_runs:
        sheets["optimization_runs"] = optimization_runs

    xlsx_path = write_excel_report(output_dir / "full_agent_decision_report.xlsx", sheets)
    counts = {
        "molecular_candidates": len(molecular_rows),
        "aspen_configured_candidates": sum(1 for row in molecular_rows if row.get("aspen_configured")),
        "selected_flow_candidates": len(selected),
        "process_evidence_records": len(process_evidence),
        "validation_cases": len(validation_cases),
        "validation_results": len(validation_results),
        "repair_cases": len(repair_cases),
        "optimization_runs": len(optimization_runs),
        "compatible_verified_case": 1 if compatible_verified_case else 0,
        "transfer_gap_items": len([row for row in transfer_gaps if row.get("status") == "missing"]),
    }
    report = OrderedDict(
        [
            ("agent", "system_input_autonomous_screening_agent"),
            ("generated_at", dt.datetime.now().isoformat(timespec="seconds")),
            ("separation_task", separation_task),
            ("agent_capability", capability),
            ("evidence_state_policy", policy),
            ("transfer_gap_report", transfer_gaps),
            ("snapshot", str(Path(args.snapshot).resolve())),
            ("solvents", str(Path(args.solvents).resolve())),
            ("output_dir", str(output_dir.resolve())),
            ("zero_bkp", str(Path(args.zero_bkp).resolve())),
            ("zero_report", str(Path(args.zero_report).resolve())),
            ("counts", counts),
            ("active_learning", snapshot.get("active_learning", {})),
            ("global_autopilot", snapshot.get("global_autopilot", {})),
            ("agent_steps", steps),
            ("selected_flow_candidates", selected),
            ("top_final_rank", final_rank[:10]),
            ("queued_validation_cases", validation_cases),
            ("queued_repair_cases", repair_cases),
            ("validation_results", validation_results),
            ("build_zero_bkp_result", build_result),
            ("optimization_runs", optimization_runs),
            ("excel_report", str(xlsx_path.resolve())),
        ]
    )
    json_path = write_json(output_dir / "full_agent_decision_report.json", report)
    md_path = write_markdown(output_dir / "full_agent_decision_report.md", report)
    report["json_report"] = str(json_path.resolve())
    report["markdown_report"] = str(md_path.resolve())
    write_json(json_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
