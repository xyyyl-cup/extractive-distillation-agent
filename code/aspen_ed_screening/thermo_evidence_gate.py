# -*- coding: utf-8 -*-
"""Evidence gate for thermodynamic-method selection and Aspen execution.

The gate is intentionally independent from Aspen COM.  It accepts structured
records extracted from original articles, NIST ThermoML, or an audited Aspen/
DDB export.  A ternary entrainer calculation is authorized only when all three
binary pairs have model-compatible, traceable parameter evidence.
"""

from __future__ import annotations

import json
import math
import re
from itertools import combinations
from pathlib import Path


PUBLICATION_STATUSES = {"verified_exact_parameter", "regressed_and_validated"}
PREDICTIVE_SCREENING_STATUS = "predictive_consensus_validated"
COSMORS_SURROGATE_STATUS = "predictive_cosmors_surrogate_validated"
PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS = "predictive_property_model_surrogate_validated"
SCREENING_STATUSES = PUBLICATION_STATUSES | {
    PREDICTIVE_SCREENING_STATUS,
    COSMORS_SURROGATE_STATUS,
    PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS,
}
PREDICTIVE_MAX_RMSE_LN_GAMMA = 0.20
PREDICTIVE_MAX_ABS_LN_GAMMA_SPREAD = 0.70
COSMORS_MAX_HOLDOUT_RMSE_LN_GAMMA = 0.10
COSMORS_MAX_ABS_LN_GAMMA_ERROR = 0.30
COSMORS_MIN_ANALOG_BENCHMARK_SYSTEMS = 2
TRACEABLE_SOURCE_TYPES = {
    "primary_article",
    "nist_thermoml",
    "nist_source",
    "aspen_databank_export",
    "ddb_export",
    "predictive_model_consensus",
    "cosmors_surrogate_regression",
    "predictive_property_model_surrogate",
}
RAW_EQUILIBRIUM_TYPES = {"VLE", "LLE", "VLLE", "SLE", "HENRY", "IDAC", "SOLUBILITY"}

COMPONENT_ALIASES = {
    "WATER": "H2O",
    "H2O": "H2O",
    "ETOH": "ETHANOL",
    "ETHYLALCOHOL": "ETHANOL",
    "C2H5OH": "ETHANOL",
    "C2H6O": "ETHANOL",
    "ETHANOL": "ETHANOL",
    "MEOH": "METHANOL",
    "METHYLALCOHOL": "METHANOL",
    "CH3OH": "METHANOL",
    "CH4O": "METHANOL",
    "METHANOL": "METHANOL",
    "ETHYLENECARBONATE": "EC",
    "C3H4O3": "EC",
    "EC": "EC",
}

METHOD_ALIASES = {
    "NRTL-RK": "NRTL",
    "NRTL-HOC": "NRTL",
    "NRTL": "NRTL",
    "UNIQUAC": "UNIQUAC",
    "WILSON": "WILSON",
    "UNIFAC": "UNIFAC",
    "UNIF-DMD": "UNIFAC",
    "UNIF-LBY": "UNIFAC",
    "PENG-ROB": "PR",
    "PR-BM": "PR",
    "PR": "PR",
    "PRWS": "PRWS",
    "PC-SAFT": "PC-SAFT",
}

COMPONENT_NAMES_ZH = {
    "H2O": "水",
    "ETHANOL": "乙醇",
    "METHANOL": "甲醇",
    "EC": "碳酸乙烯酯（EC）",
}

PAIR_STATUS_ZH = {
    "verified_exact_parameter": "已找到并核验可直接使用的文献参数",
    "regressed_and_validated": "参数已由实验数据回归并完成独立验证",
    PREDICTIVE_SCREENING_STATUS: "预测参数已通过多方法与实验约束验证，仅可用于筛选",
    COSMORS_SURROGATE_STATUS: "COSMO-RS 替代参数已通过验证，仅可用于筛选",
    PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS: "按文献选定模型拟合的 COSMO-RS 参数已通过验证，仅可用于筛选",
    "regression_required": "已找到实验相平衡数据，但还需要回归模型参数",
    "parameter_requires_verification": "已有参数，但尚未完成来源和误差验证",
    "parameter_adapter_required": "参数证据可用，但缺少对应 Aspen 参数写入模块",
    "parameter_model_mismatch": "参数所属模型与当前选择的物性方法不一致",
    "predictive_only": "只有未经验证的单一预测结果",
    "predictive_consensus_unqualified": "多模型预测未达到验证要求",
    "predictive_cosmors_surrogate_unqualified": "COSMO-RS 替代参数未达到验证要求",
    "predictive_property_model_surrogate_unqualified": "按文献模型拟合的 COSMO-RS 参数未达到验证要求",
    "invalid_evidence": "证据记录不完整或格式无效",
    "unresolved_parameter_gap": "尚未找到可用实验数据或参数",
}

ERROR_MESSAGES_ZH = {
    "predictive_surrogate_model_does_not_match_parameter_model": "COSMO-RS 拟合所用模型与待写入参数模型不一致",
    "predictive_surrogate_requires_literature_model_selection_record": "缺少能够证明当前物性方法选择合理的原始文献记录",
    "cosmors_surrogate_holdout_rmse_exceeds_limit": "独立验证集误差超过允许上限",
    "cosmors_surrogate_max_error_exceeds_limit": "最大活度系数误差超过允许上限",
    "cosmors_requires_two_traceable_analog_experimental_benchmarks": "不足两个采用相同 COSMO 方案的相似体系实验基准",
    "cosmors_analog_chemical_similarity_not_justified": "没有说明实验基准体系与目标体系为何相似",
    "cosmors_benchmark_uncertainty_not_propagated": "尚未把 COSMO 误差传播到萃取剂排序和流程结果",
    "cosmors_decision_margin_does_not_exceed_uncertainty": "候选物之间的差异小于模型不确定度，排序不可靠",
    "cosmors_surrogate_phase_behavior_not_consistent": "预测的相态与实验或文献事实不一致",
    "predictive_consensus_requires_traceable_experimental_constraint": "预测参数没有目标体系实验数据约束",
    "predictive_consensus_requires_two_independent_method_families": "不足两类相互独立的预测方法",
    "predictive_consensus_intermodel_spread_exceeds_limit": "不同预测方法之间的差异过大",
    "predictive_consensus_ranking_not_stable": "考虑模型误差后，萃取剂排序不稳定",
}


class ThermoEvidenceError(ValueError):
    pass


def finite_or_none(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def component_id(value):
    token = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    if not token:
        raise ThermoEvidenceError("component identity is empty")
    return COMPONENT_ALIASES.get(token, token)


def method_family(value):
    token = re.sub(r"\s+", " ", str(value or "").strip().upper()).replace("_", "-")
    return METHOD_ALIASES.get(token, token)


def pair_key(values):
    normalized = [component_id(value) for value in values]
    if len(normalized) != 2 or normalized[0] == normalized[1]:
        raise ThermoEvidenceError("a binary pair needs two distinct components")
    return "::".join(sorted(normalized))


def literature_queries_for_pair(pair, selected_method=""):
    first, second = [component_id(value) for value in pair]
    model = method_family(selected_method)
    model_terms = "{} binary interaction parameters".format(model) if model else "NRTL UNIQUAC Wilson binary interaction parameters"
    return [
        {
            "pair": [first, second],
            "pair_key": pair_key([first, second]),
            "query": '"{}" "{}" vapor liquid equilibrium VLE T-x-y P-x-y'.format(first, second),
            "purpose": "locate finite-composition experimental VLE data",
        },
        {
            "pair": [first, second],
            "pair_key": pair_key([first, second]),
            "query": '"{}" "{}" {}'.format(first, second, model_terms),
            "purpose": "locate the original article's model-specific parameter table",
        },
        {
            "pair": [first, second],
            "pair_key": pair_key([first, second]),
            "query": '"{}" "{}" NIST ThermoML phase equilibrium'.format(first, second),
            "purpose": "locate machine-readable experimental data and original-source metadata",
        },
        {
            "pair": [first, second],
            "pair_key": pair_key([first, second]),
            "query": '"{}" "{}" Aspen Plus DECHEMA DDB parameter'.format(first, second),
            "purpose": "locate an audited databank record when the public article omits a parameter table",
        },
    ]


def task_components(report, candidate=None):
    task = (report or {}).get("separation_task") or {}
    feed = [component_id(task.get("component_a")), component_id(task.get("component_b"))]
    if not candidate:
        return feed
    solvent = (
        candidate.get("code")
        or candidate.get("solvent_code")
        or candidate.get("name")
        or candidate.get("aspen_component_id")
    )
    return feed + [component_id(solvent)]


def required_pairs(report, candidate=None):
    components = task_components(report, candidate=candidate)
    return [
        {
            "pair": [first, second],
            "pair_key": pair_key([first, second]),
            "pair_role": "feed_binary" if index == 0 else "entrainer_binary",
        }
        for index, (first, second) in enumerate(combinations(components, 2))
    ]


def _read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ThermoEvidenceError("thermodynamic evidence JSON root must be an object")
    return value


def evidence_path_candidates(report):
    report = report or {}
    task = report.get("separation_task") or {}
    paths = report.get("research_task_package_paths") or {}
    candidates = [
        report.get("thermodynamic_evidence_path"),
        report.get("thermo_evidence_path"),
        task.get("thermodynamic_evidence_path"),
        task.get("thermo_evidence_path"),
        paths.get("thermodynamic_evidence_json"),
        paths.get("thermo_evidence_json"),
    ]
    output_dir = report.get("output_dir")
    if output_dir:
        package_dir = Path(output_dir) / "research_task_package"
        candidates.extend(
            [
                package_dir / "thermodynamic_evidence.json",
                package_dir / "thermo_evidence.json",
            ]
        )
    result = []
    seen = set()
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def load_evidence_payload(report):
    report = report or {}
    for key in ("thermodynamic_evidence", "thermo_evidence"):
        value = report.get(key)
        if isinstance(value, dict):
            return value, "inline:{}".format(key), "loaded"
    for path in evidence_path_candidates(report):
        if not path.exists():
            continue
        try:
            return _read_json(path), str(path.resolve()), "loaded"
        except Exception as exc:
            return {"records": []}, str(path.resolve()), "invalid: {}: {}".format(exc.__class__.__name__, exc)
    return {"records": []}, "", "not_provided"


def source_is_traceable(source):
    source = source or {}
    source_type = str(source.get("type") or "").strip().lower()
    errors = []
    if source_type not in TRACEABLE_SOURCE_TYPES:
        errors.append("unsupported_or_missing_source_type")
    if not (source.get("doi") or source.get("url") or source.get("record_id")):
        errors.append("missing_doi_url_or_record_id")
    if source_type == "primary_article" and not source.get("doi"):
        errors.append("primary_article_requires_doi")
    if not source.get("title"):
        errors.append("missing_source_title")
    return not errors, errors


def _validate_range(value, label):
    if not isinstance(value, list) or len(value) != 2:
        return ["invalid_or_missing_{}".format(label)]
    low = finite_or_none(value[0])
    high = finite_or_none(value[1])
    if low is None or high is None or low > high:
        return ["invalid_or_missing_{}".format(label)]
    return []


def _validate_nrtl_parameter(parameter, expected_pair):
    errors = []
    if str(parameter.get("form") or "").strip().lower() != "aspen_bpval_8":
        errors.append("unsupported_nrtl_parameter_form")
    rows = parameter.get("rows") or []
    if not isinstance(rows, list) or len(rows) != 2:
        return errors + ["nrtl_requires_two_directional_rows"]
    expected = {component_id(value) for value in expected_pair}
    directions = set()
    for row in rows:
        try:
            source = component_id(row.get("from"))
            target = component_id(row.get("to"))
        except ThermoEvidenceError:
            errors.append("nrtl_component_order_missing")
            continue
        directions.add((source, target))
        if {source, target} != expected or source == target:
            errors.append("nrtl_component_order_mismatch")
        values = row.get("values") or []
        if not isinstance(values, list) or len(values) != 8 or any(finite_or_none(item) is None for item in values):
            errors.append("nrtl_bpval_requires_eight_finite_values")
    if len(directions) != 2 or not all((target, source) in directions for source, target in directions):
        errors.append("nrtl_reverse_direction_missing")
    return errors


def _prediction_method_family(value):
    if isinstance(value, dict):
        explicit = str(value.get("independence_group") or value.get("family") or "").strip().upper()
        if explicit:
            return re.sub(r"[^A-Z0-9]+", "_", explicit).strip("_")
        value = value.get("name") or value.get("method") or ""
    token = re.sub(r"[^A-Z0-9]+", "-", str(value or "").strip().upper()).strip("-")
    if token.startswith("UNIFAC"):
        return "GROUP_CONTRIBUTION_UNIFAC"
    if token.startswith("COSMO"):
        return "QUANTUM_CHEMISTRY_COSMO"
    return token


def _predictive_consensus_errors(record, expected_pair, all_records):
    verification = record.get("verification") or {}
    errors = []
    methods = verification.get("prediction_methods") or []
    if not isinstance(methods, list):
        methods = []
    qualified_methods = []
    for method in methods:
        family = _prediction_method_family(method)
        if not family:
            continue
        if not isinstance(method, dict):
            errors.append("predictive_method_requires_structured_audit_record")
            continue
        if method.get("applicability_verified") is not True:
            errors.append("predictive_method_applicability_not_verified")
            continue
        if not (method.get("artifact_path") or method.get("artifact_sha256") or method.get("output_record_id")):
            errors.append("predictive_method_output_provenance_missing")
            continue
        if "GROUP_CONTRIBUTION" in family and method.get("group_assignment_complete") is not True:
            errors.append("group_contribution_assignment_incomplete")
            continue
        qualified_methods.append((family, method))
    independent_families = {family for family, _method in qualified_methods}
    if len(independent_families) < 2:
        errors.append("predictive_consensus_requires_two_independent_method_families")

    constraint_ids = verification.get("experimental_constraint_record_ids") or []
    if not isinstance(constraint_ids, list):
        constraint_ids = []
    indexed_records = {
        str(item.get("record_id") or "").strip(): item
        for item in (all_records or [])
        if str(item.get("record_id") or "").strip()
    }
    valid_constraint_count = 0
    expected_key = pair_key(expected_pair)
    own_id = str(record.get("record_id") or "").strip()
    for constraint_id in dict.fromkeys(str(item or "").strip() for item in constraint_ids):
        constraint = indexed_records.get(constraint_id)
        if not constraint or constraint_id == own_id:
            continue
        try:
            constraint_key = pair_key(constraint.get("pair") or [])
        except ThermoEvidenceError:
            continue
        source_ok, _source_errors = source_is_traceable(constraint.get("source") or {})
        data_type = str(constraint.get("data_type") or "").strip().upper()
        if constraint_key == expected_key and source_ok and data_type in RAW_EQUILIBRIUM_TYPES:
            valid_constraint_count += 1
    if valid_constraint_count < 1:
        errors.append("predictive_consensus_requires_traceable_experimental_constraint")

    rmse = finite_or_none(verification.get("fit_rmse_ln_gamma"))
    if rmse is None or rmse < 0 or rmse > PREDICTIVE_MAX_RMSE_LN_GAMMA:
        errors.append("predictive_consensus_rmse_ln_gamma_exceeds_limit")
    spread = finite_or_none(verification.get("max_abs_ln_gamma_spread"))
    if spread is None or spread < 0 or spread > PREDICTIVE_MAX_ABS_LN_GAMMA_SPREAD:
        errors.append("predictive_consensus_intermodel_spread_exceeds_limit")
    if verification.get("ranking_stable") is not True:
        errors.append("predictive_consensus_ranking_not_stable")
    if verification.get("regression_converged") is not True:
        errors.append("predictive_consensus_regression_not_converged")
    if verification.get("temperature_range_covered") is not True:
        errors.append("predictive_consensus_temperature_range_not_covered")
    return errors, {
        "independent_method_family_count": len(independent_families),
        "independent_method_families": sorted(independent_families),
        "qualified_prediction_method_count": len(qualified_methods),
        "valid_experimental_constraint_count": valid_constraint_count,
        "fit_rmse_ln_gamma": rmse,
        "max_abs_ln_gamma_spread": spread,
    }


def _cosmors_surrogate_errors(record, expected_pair, all_records):
    """Audit a COSMO-RS-to-literature-selected-model fallback.

    Three different questions are checked separately:
    1. Does the selected Aspen property model reproduce the COSMO-RS activity-
       coefficient surface on held-out compositions and temperatures?
    2. Was the same COSMO-RS protocol benchmarked against experiments for
       chemically analogous systems?
    3. Is the entrainer decision stable after propagating the benchmark/model
       uncertainty into the screening calculation?
    """
    verification = record.get("verification") or {}
    errors = []
    parameter_model = method_family((record.get("parameter") or {}).get("model"))
    surrogate_model = method_family(verification.get("surrogate_property_model"))
    if not parameter_model:
        errors.append("predictive_surrogate_parameter_model_missing")
    if surrogate_model != parameter_model:
        errors.append("predictive_surrogate_model_does_not_match_parameter_model")

    indexed_records = {
        str(item.get("record_id") or "").strip(): item
        for item in (all_records or [])
        if str(item.get("record_id") or "").strip()
    }
    selection_ids = verification.get("model_selection_record_ids") or []
    if not isinstance(selection_ids, list):
        selection_ids = []
    valid_selection_records = []
    own_id = str(record.get("record_id") or "").strip()
    for selection_id in dict.fromkeys(str(item or "").strip() for item in selection_ids):
        selection_record = indexed_records.get(selection_id)
        if not selection_record or selection_id == own_id:
            continue
        source_ok, _source_errors = source_is_traceable(selection_record.get("source") or {})
        selected_record_model = method_family(
            ((selection_record.get("parameter") or {}).get("model"))
            or ((selection_record.get("verification") or {}).get("selected_method"))
        )
        if source_ok and selected_record_model == parameter_model:
            valid_selection_records.append(selection_record)
    if not valid_selection_records:
        errors.append("predictive_surrogate_requires_literature_model_selection_record")

    methods = verification.get("prediction_methods") or []
    if not isinstance(methods, list):
        methods = []
    qualified_cosmo = []
    for method in methods:
        if not isinstance(method, dict):
            errors.append("cosmors_method_requires_structured_audit_record")
            continue
        family = _prediction_method_family(method)
        if family != "QUANTUM_CHEMISTRY_COSMO":
            continue
        if method.get("applicability_verified") is not True:
            errors.append("cosmors_method_applicability_not_verified")
            continue
        if not (method.get("artifact_path") or method.get("artifact_sha256") or method.get("output_record_id")):
            errors.append("cosmors_output_provenance_missing")
            continue
        if not (method.get("protocol") or method.get("protocol_record_id")):
            errors.append("cosmors_protocol_missing")
            continue
        qualified_cosmo.append(method)
    if not qualified_cosmo:
        errors.append("qualified_cosmors_prediction_required")

    fit_rmse = finite_or_none(verification.get("fit_rmse_ln_gamma"))
    holdout_rmse = finite_or_none(verification.get("holdout_rmse_ln_gamma"))
    max_abs_error = finite_or_none(verification.get("max_abs_ln_gamma_error"))
    if fit_rmse is None or fit_rmse < 0 or fit_rmse > PREDICTIVE_MAX_RMSE_LN_GAMMA:
        errors.append("cosmors_surrogate_fit_rmse_exceeds_limit")
    if holdout_rmse is None or holdout_rmse < 0 or holdout_rmse > COSMORS_MAX_HOLDOUT_RMSE_LN_GAMMA:
        errors.append("cosmors_surrogate_holdout_rmse_exceeds_limit")
    if max_abs_error is None or max_abs_error < 0 or max_abs_error > COSMORS_MAX_ABS_LN_GAMMA_ERROR:
        errors.append("cosmors_surrogate_max_error_exceeds_limit")
    for field, error in (
        ("regression_converged", "cosmors_surrogate_regression_not_converged"),
        ("temperature_range_covered", "cosmors_surrogate_temperature_range_not_covered"),
        ("composition_range_covered", "cosmors_surrogate_composition_range_not_covered"),
        ("infinite_dilution_endpoints_covered", "cosmors_surrogate_endpoints_not_covered"),
        ("phase_behavior_consistent", "cosmors_surrogate_phase_behavior_not_consistent"),
    ):
        if verification.get(field) is not True:
            errors.append(error)

    benchmark = verification.get("cosmors_reality_benchmark") or {}
    benchmark_rows = benchmark.get("systems") or []
    if not isinstance(benchmark_rows, list):
        benchmark_rows = []
    valid_benchmarks = []
    for row in benchmark_rows:
        if not isinstance(row, dict) or row.get("same_cosmors_protocol") is not True:
            continue
        source_ok, _source_errors = source_is_traceable(row.get("source") or {})
        value = finite_or_none(row.get("metric_value"))
        limit = finite_or_none(row.get("acceptance_limit"))
        if (
            source_ok
            and row.get("metric_name")
            and value is not None
            and limit is not None
            and value >= 0
            and limit >= 0
            and value <= limit
        ):
            valid_benchmarks.append(row)
    if len(valid_benchmarks) < COSMORS_MIN_ANALOG_BENCHMARK_SYSTEMS:
        errors.append("cosmors_requires_two_traceable_analog_experimental_benchmarks")
    if benchmark.get("chemical_similarity_justified") is not True:
        errors.append("cosmors_analog_chemical_similarity_not_justified")

    if verification.get("ranking_stable") is not True:
        errors.append("cosmors_surrogate_ranking_not_stable")
    if verification.get("uncertainty_propagated") is not True:
        errors.append("cosmors_benchmark_uncertainty_not_propagated")
    if verification.get("decision_margin_exceeds_uncertainty") is not True:
        errors.append("cosmors_decision_margin_does_not_exceed_uncertainty")

    return errors, {
        "qualified_cosmors_method_count": len(qualified_cosmo),
        "surrogate_property_model": parameter_model,
        "valid_literature_model_selection_record_count": len(valid_selection_records),
        "valid_analog_experimental_benchmark_count": len(valid_benchmarks),
        "fit_rmse_ln_gamma": fit_rmse,
        "holdout_rmse_ln_gamma": holdout_rmse,
        "max_abs_ln_gamma_error": max_abs_error,
        "target_pair": pair_key(expected_pair),
        "validation_layers": {
            "selected_property_model_reproduces_cosmors": not any(
                item in errors
                for item in (
                    "cosmors_surrogate_fit_rmse_exceeds_limit",
                    "cosmors_surrogate_holdout_rmse_exceeds_limit",
                    "cosmors_surrogate_max_error_exceeds_limit",
                    "cosmors_surrogate_regression_not_converged",
                    "cosmors_surrogate_temperature_range_not_covered",
                    "cosmors_surrogate_composition_range_not_covered",
                    "cosmors_surrogate_endpoints_not_covered",
                    "cosmors_surrogate_phase_behavior_not_consistent",
                )
            ),
            "cosmors_benchmarked_against_reality": len(valid_benchmarks) >= COSMORS_MIN_ANALOG_BENCHMARK_SYSTEMS,
            "screening_decision_robust": not any(
                item in errors
                for item in (
                    "cosmors_surrogate_ranking_not_stable",
                    "cosmors_benchmark_uncertainty_not_propagated",
                    "cosmors_decision_margin_does_not_exceed_uncertainty",
                )
            ),
        },
    }


def assess_record(record, expected_pair, selected_method="", all_records=None):
    errors = []
    warnings = []
    try:
        record_pair_key = pair_key(record.get("pair") or [])
    except ThermoEvidenceError:
        record_pair_key = ""
        errors.append("invalid_evidence_pair")
    if record_pair_key and record_pair_key != pair_key(expected_pair):
        errors.append("evidence_pair_mismatch")
    source = record.get("source") or {}
    source_ok, source_errors = source_is_traceable(source)
    errors.extend(source_errors)
    parameter = record.get("parameter") or {}
    parameter_model = method_family(parameter.get("model")) if parameter.get("model") else ""
    required_model = method_family(selected_method)
    if parameter:
        if not parameter_model:
            errors.append("missing_parameter_model")
        errors.extend(_validate_range(parameter.get("temperature_range_c"), "temperature_range_c"))
        errors.extend(_validate_range(parameter.get("pressure_range_mpa"), "pressure_range_mpa"))
        if not parameter.get("units"):
            errors.append("missing_parameter_units")
        if parameter_model == "NRTL":
            errors.extend(_validate_nrtl_parameter(parameter, expected_pair))
        elif not parameter.get("form"):
            warnings.append("non_nrtl_parameter_form_requires_aspen_adapter")
    data_type = str(record.get("data_type") or "").strip().upper()
    if data_type not in RAW_EQUILIBRIUM_TYPES and not parameter:
        errors.append("missing_equilibrium_data_type")
    declared = str((record.get("verification") or {}).get("status") or "").strip().lower()
    predictive_metrics = {}
    if declared == PREDICTIVE_SCREENING_STATUS:
        if str(source.get("type") or "").strip().lower() != "predictive_model_consensus":
            errors.append("predictive_consensus_requires_predictive_source_type")
        consensus_errors, predictive_metrics = _predictive_consensus_errors(
            record,
            expected_pair,
            all_records if all_records is not None else [record],
        )
        errors.extend(consensus_errors)
    elif declared in {COSMORS_SURROGATE_STATUS, PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS}:
        if str(source.get("type") or "").strip().lower() not in {
            "cosmors_surrogate_regression",
            "predictive_property_model_surrogate",
        }:
            errors.append("cosmors_surrogate_requires_cosmors_source_type")
        surrogate_errors, predictive_metrics = _cosmors_surrogate_errors(
            record,
            expected_pair,
            all_records if all_records is not None else [record],
        )
        errors.extend(surrogate_errors)
    model_matches = bool(not required_model or not parameter_model or required_model == parameter_model)
    if declared == PREDICTIVE_SCREENING_STATUS and errors:
        status = "predictive_consensus_unqualified"
    elif declared == COSMORS_SURROGATE_STATUS and errors:
        status = "predictive_cosmors_surrogate_unqualified"
    elif declared == PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS and errors:
        status = "predictive_property_model_surrogate_unqualified"
    elif errors:
        status = "invalid_evidence"
    elif parameter and not model_matches:
        status = "parameter_model_mismatch"
    elif declared in PUBLICATION_STATUSES and parameter and source_ok:
        native_databank_ready = (
            str(source.get("type") or "").strip().lower() == "aspen_databank_export"
            and str(parameter.get("form") or "").strip().lower() == "aspen_native_pair"
        )
        if parameter_model == "NRTL" or native_databank_ready:
            status = declared
        else:
            status = "parameter_adapter_required"
            warnings.append("verified parameters exist but this Aspen parameter renderer is not implemented")
    elif declared in {
        PREDICTIVE_SCREENING_STATUS,
        COSMORS_SURROGATE_STATUS,
        PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS,
    } and parameter and source_ok:
        if parameter_model == "NRTL":
            status = declared
            warnings.append("screening_only_not_publication_validated")
        else:
            status = "parameter_adapter_required"
            warnings.append("predictive parameter validation passed but this selected model's Aspen parameter renderer is not implemented")
    elif declared == "predictive_only":
        status = "predictive_only"
        warnings.append("single_or_unvalidated_prediction_cannot_authorize_aspen")
    elif parameter and source_ok:
        status = "parameter_requires_verification"
    elif data_type in RAW_EQUILIBRIUM_TYPES and source_ok:
        status = "regression_required"
    else:
        status = "unresolved_parameter_gap"
    return {
        "record_id": record.get("record_id") or "",
        "pair_key": record_pair_key,
        "status": status,
        "parameter_model": parameter_model,
        "source_type": source.get("type") or "",
        "source_title": source.get("title") or "",
        "source_doi": source.get("doi") or "",
        "source_url": source.get("url") or "",
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "predictive_consensus_metrics": predictive_metrics,
    }


def pair_status(assessments):
    statuses = {row.get("status") for row in assessments}
    for status in (
        "verified_exact_parameter",
        "regressed_and_validated",
        PREDICTIVE_SCREENING_STATUS,
        COSMORS_SURROGATE_STATUS,
        PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS,
        "predictive_consensus_unqualified",
        "predictive_cosmors_surrogate_unqualified",
        "predictive_property_model_surrogate_unqualified",
        "regression_required",
        "parameter_requires_verification",
        "parameter_adapter_required",
        "parameter_model_mismatch",
        "predictive_only",
        "invalid_evidence",
    ):
        if status in statuses:
            return status
    return "unresolved_parameter_gap"


def _model_coverage(records, allowed_statuses):
    coverage = {}
    for record in records:
        parameter = record.get("parameter") or {}
        model = method_family(parameter.get("model")) if parameter.get("model") else ""
        pair = record.get("pair") or []
        if not model or assess_record(record, pair, model, all_records=records).get("status") not in allowed_statuses:
            continue
        try:
            key = pair_key(record.get("pair") or [])
        except ThermoEvidenceError:
            continue
        coverage.setdefault(model, set()).add(key)
    return {key: len(value) for key, value in coverage.items()}


def _verified_model_coverage(records):
    return _model_coverage(records, PUBLICATION_STATUSES)


def build_system_evidence_summary(report, selected_method=""):
    payload, source, load_status = load_evidence_payload(report)
    records = payload.get("records") or []
    if not isinstance(records, list):
        records = []
        load_status = "invalid: records must be a list"
    coverage = _verified_model_coverage(records)
    screening_coverage = _model_coverage(records, SCREENING_STATUSES)
    recommended_method = ""
    if coverage:
        recommended_method = sorted(coverage, key=lambda item: (-coverage[item], item))[0]
    elif screening_coverage:
        recommended_method = sorted(screening_coverage, key=lambda item: (-screening_coverage[item], item))[0]
    requested = method_family(selected_method)
    feed_gate = evaluate_candidate(
        report,
        candidate=None,
        selected_method=requested or recommended_method,
        evidence_payload=payload,
        evidence_source=source,
        load_status=load_status,
    )
    return {
        "status": load_status,
        "evidence_source": source,
        "record_count": len(records),
        "verified_model_pair_coverage": coverage,
        "screening_model_pair_coverage": screening_coverage,
        "recommended_method": recommended_method,
        "requested_method": requested,
        "feed_binary_gate": feed_gate,
        "decision_policy": "select the property model from traceable literature and system applicability first; missing entrainer interactions may use a COSMO-RS surrogate fitted to that selected model only after holdout fidelity, analogous-system experimental benchmarking, and uncertainty-aware ranking stability all pass",
    }


def evaluate_candidate(
    report,
    candidate=None,
    selected_method="",
    *,
    evidence_payload=None,
    evidence_source="",
    load_status="",
):
    if evidence_payload is None:
        evidence_payload, evidence_source, load_status = load_evidence_payload(report)
    records = evidence_payload.get("records") or []
    if not isinstance(records, list):
        records = []
        load_status = "invalid: records must be a list"
    selected_family = method_family(selected_method)
    pair_requirements = required_pairs(report, candidate=candidate)
    if not selected_family:
        required_keys = {row["pair_key"] for row in pair_requirements}
        model_pairs = {}
        for record in records:
            parameter = record.get("parameter") or {}
            model = method_family(parameter.get("model")) if parameter.get("model") else ""
            pair = record.get("pair") or []
            if not model or assess_record(record, pair, model, all_records=records).get("status") not in SCREENING_STATUSES:
                continue
            try:
                key = pair_key(record.get("pair") or [])
            except ThermoEvidenceError:
                continue
            if key in required_keys:
                model_pairs.setdefault(model, set()).add(key)
        if model_pairs:
            selected_family = sorted(model_pairs, key=lambda item: (-len(model_pairs[item]), item))[0]
    # The evidence gate may identify a literature model before its Aspen
    # writer is verified.  Do not authorize execution merely because records
    # exist; the model adapter must be explicitly implemented.
    adapter_status = "unavailable"
    try:
        from thermo_regression_agent import VERIFIED_ADAPTER_MODELS
        adapter_status = "verified" if selected_family in VERIFIED_ADAPTER_MODELS else "unverified"
    except Exception:
        adapter_status = "unavailable"
    matrix = []
    for pair in pair_requirements:
        matched = []
        for record in records:
            try:
                if pair_key(record.get("pair") or []) == pair["pair_key"]:
                    matched.append(record)
            except ThermoEvidenceError:
                continue
        assessments = [
            assess_record(record, pair["pair"], selected_family, all_records=records)
            for record in matched
        ]
        matrix.append(
            {
                "pair": pair["pair"],
                "pair_key": pair["pair_key"],
                "pair_role": pair["pair_role"],
                "status": pair_status(assessments),
                "assessments": assessments,
            }
        )
    statuses = [row["status"] for row in matrix]
    publication_ready = bool(statuses) and all(status in PUBLICATION_STATUSES for status in statuses)
    screening_ready = bool(statuses) and all(status in SCREENING_STATUSES for status in statuses)
    publication_missing = [row for row in matrix if row["status"] not in PUBLICATION_STATUSES]
    screening_missing = [row for row in matrix if row["status"] not in SCREENING_STATUSES]
    if publication_ready and adapter_status == "verified":
        status = "publication_ready"
    elif screening_ready and adapter_status == "verified":
        status = "screening_ready_predictive"
    elif adapter_status != "verified":
        status = "blocked_parameter_adapter_unverified"
    elif load_status == "not_provided":
        status = "blocked_thermo_evidence_not_provided"
    elif str(load_status).startswith("invalid"):
        status = "blocked_thermo_evidence_invalid"
    else:
        status = "blocked_thermo_evidence_incomplete"
    candidate_code = ""
    if candidate:
        candidate_code = str(
            candidate.get("code")
            or candidate.get("solvent_code")
            or candidate.get("name")
            or candidate.get("aspen_component_id")
            or ""
        ).strip()
    return {
        "status": status,
        "candidate_code": candidate_code,
        "selected_method": selected_family,
        "parameter_adapter_status": adapter_status,
        "evidence_source": evidence_source,
        "evidence_load_status": load_status,
        "publication_ready": publication_ready,
        "screening_ready": screening_ready,
        "aspen_run_authorized": screening_ready and adapter_status == "verified",
        "aspen_execution_mode": (
            "publication_validated"
            if publication_ready
            else ("predictive_screening" if screening_ready else "blocked")
        ),
        "claim_level": (
            "publication_validated"
            if publication_ready
            else ("screening_only_predictive" if screening_ready else "blocked")
        ),
        "requires_uncertainty_campaign": bool(screening_ready and not publication_ready),
        "required_pair_count": len(matrix),
        "ready_pair_count": sum(1 for row in matrix if row["status"] in SCREENING_STATUSES),
        "publication_ready_pair_count": sum(1 for row in matrix if row["status"] in PUBLICATION_STATUSES),
        "screening_ready_pair_count": sum(1 for row in matrix if row["status"] in SCREENING_STATUSES),
        "missing_pairs": [
            {
                "pair": row["pair"],
                "pair_key": row["pair_key"],
                "status": row["status"],
            }
            for row in screening_missing
        ],
        "publication_missing_pairs": [
            {
                "pair": row["pair"],
                "pair_key": row["pair_key"],
                "status": row["status"],
            }
            for row in publication_missing
        ],
        "screening_missing_pairs": [
            {
                "pair": row["pair"],
                "pair_key": row["pair_key"],
                "status": row["status"],
            }
            for row in screening_missing
        ],
        "next_literature_queries": [
            query
            for row in publication_missing
            for query in literature_queries_for_pair(row["pair"], selected_family)
        ],
        "matrix": matrix,
        "reason": (
            "All required binary pairs have traceable, model-compatible parameters."
            if publication_ready
            else (
                "Aspen screening is authorized with qualified predictive parameters; results remain screening-only and require uncertainty analysis and experimental validation."
                if screening_ready
                else "Aspen is blocked until every required binary pair has publication-grade parameters or a qualified predictive surrogate with reality and decision-robustness validation."
            )
        ),
        "screening_policy": {
            "minimum_independent_prediction_method_families": 2,
            "minimum_traceable_experimental_constraints": 1,
            "maximum_fit_rmse_ln_gamma": PREDICTIVE_MAX_RMSE_LN_GAMMA,
            "maximum_abs_ln_gamma_intermodel_spread": PREDICTIVE_MAX_ABS_LN_GAMMA_SPREAD,
            "cosmors_surrogate_route": {
                "minimum_prediction_method_families": 1,
                "required_family": "QUANTUM_CHEMISTRY_COSMO",
                "maximum_holdout_rmse_ln_gamma": COSMORS_MAX_HOLDOUT_RMSE_LN_GAMMA,
                "maximum_abs_ln_gamma_error": COSMORS_MAX_ABS_LN_GAMMA_ERROR,
                "minimum_traceable_analog_experimental_benchmarks": COSMORS_MIN_ANALOG_BENCHMARK_SYSTEMS,
                "requires_same_cosmors_protocol_for_benchmarks": True,
                "requires_chemical_similarity_justification": True,
                "requires_full_composition_and_operating_temperature_coverage": True,
                "requires_phase_behavior_consistency": True,
                "requires_uncertainty_propagation": True,
                "requires_decision_margin_to_exceed_uncertainty": True,
            },
            "requires_stable_ranking": True,
            "requires_converged_regression": True,
            "requires_temperature_range_coverage": True,
        },
    }


def _component_name_zh(value):
    normalized = component_id(value)
    return COMPONENT_NAMES_ZH.get(normalized, normalized)


def _pair_label_zh(pair):
    return "–".join(_component_name_zh(value) for value in pair or [])


def _pair_action_zh(status, pair_label, selected_method):
    if status in PUBLICATION_STATUSES:
        return "保留该参数及其来源记录，并检查实际运行温压是否位于文献适用范围内。"
    if status in {
        PREDICTIVE_SCREENING_STATUS,
        COSMORS_SURROGATE_STATUS,
        PREDICTIVE_PROPERTY_MODEL_SURROGATE_STATUS,
    }:
        return "只允许用于候选筛选；保留不确定度分析，并在形成论文结论前补充目标体系实验验证。"
    if status == "regression_required":
        return "优先使用已找到的实验数据回归 {} 参数，并用未参与拟合的数据进行验证。".format(selected_method or "所选模型")
    if status == "parameter_requires_verification":
        return "核对参数原始出处、单位、组分方向和温度形式，再用实验数据计算误差。"
    if status == "parameter_adapter_required":
        return "实现所选模型的 Aspen 参数写入模块后，再进行等效性测试。"
    if status == "parameter_model_mismatch":
        return "不要把其他模型的参数直接用于 {}；应回到文献重新选择模型或获取匹配参数。".format(selected_method or "当前方法")
    if status in {
        "predictive_consensus_unqualified",
        "predictive_cosmors_surrogate_unqualified",
        "predictive_property_model_surrogate_unqualified",
    }:
        return "补齐独立实验基准、验证集误差和排序稳定性检查；未通过前不得启动 Aspen。"
    if status == "invalid_evidence":
        return "修正证据记录，并回到论文原文核对 DOI、数据表、模型和参数单位。"
    if status == "predictive_only":
        return "补充实验数据或第二类独立预测方法，不能用单一预测结果证明物性方法可靠。"
    return "继续检索 {} 的实验 VLE/LLE、模型参数及原始论文。".format(pair_label)


def build_chinese_validation_report(report, candidate=None, selected_method="", gate_result=None):
    """Build a Chinese, human-readable audit without replacing machine status codes."""
    task = (report or {}).get("separation_task") or {}
    requested_method = selected_method or task.get("property_method") or ""
    result = gate_result or evaluate_candidate(
        report,
        candidate=candidate,
        selected_method=requested_method,
    )
    chosen_method = result.get("selected_method") or method_family(requested_method) or "尚未确定"
    matrix = result.get("matrix") or []
    feed_rows = [row for row in matrix if row.get("pair_role") == "feed_binary"]
    feed_supported = bool(feed_rows) and all(
        row.get("status") in SCREENING_STATUSES for row in feed_rows
    )
    hard_failure_statuses = {
        "parameter_model_mismatch",
        "invalid_evidence",
        "predictive_consensus_unqualified",
        "predictive_cosmors_surrogate_unqualified",
        "predictive_property_model_surrogate_unqualified",
    }
    has_hard_failure = any(row.get("status") in hard_failure_statuses for row in matrix)

    if result.get("publication_ready"):
        verdict = "验证通过"
        confidence = "高"
        conclusion = "当前物性方法及全部必要二元参数均有可追溯证据，并已达到正式结果使用要求。"
    elif result.get("screening_ready"):
        verdict = "有条件通过（仅限筛选）"
        confidence = "中"
        conclusion = "当前物性方法有文献依据，预测补充参数也通过了筛选级验证，但尚不能作为最终发表级物性依据。"
    elif candidate and feed_supported and not has_hard_failure:
        verdict = "有条件通过（参数未闭合）"
        confidence = "中"
        conclusion = "物性方法本身获得了基础体系证据支持；当前问题是萃取剂相关参数不完整，并不等于已经证明该模型不合适。"
    elif has_hard_failure:
        verdict = "验证不通过"
        confidence = "低"
        conclusion = "现有证据与当前物性方法不匹配，或验证误差未达标；在修正前不能用于 Aspen 计算。"
    else:
        verdict = "证据不足"
        confidence = "低"
        conclusion = "目前没有足够的目标体系文献和实验数据，无法确认当前物性方法是否合适。"

    pair_rows = []
    references = []
    seen_references = set()
    next_actions = []
    for row in matrix:
        pair_label = _pair_label_zh(row.get("pair") or [])
        status = row.get("status") or "unresolved_parameter_gap"
        sources = []
        errors_zh = []
        for assessment in row.get("assessments") or []:
            title = str(assessment.get("source_title") or "").strip()
            doi = str(assessment.get("source_doi") or "").strip()
            url = str(assessment.get("source_url") or "").strip()
            if title:
                source_item = {"题名": title, "DOI": doi, "链接": url}
                source_key = (title, doi, url)
                if source_key not in seen_references:
                    seen_references.add(source_key)
                    references.append(source_item)
                sources.append(source_item)
            errors_zh.extend(
                ERROR_MESSAGES_ZH.get(error, error)
                for error in assessment.get("errors") or []
            )
        action = _pair_action_zh(status, pair_label, chosen_method)
        if status not in PUBLICATION_STATUSES:
            next_actions.append("{}：{}".format(pair_label, action))
        pair_rows.append(
            {
                "组分对": pair_label,
                "作用": "原料二元体系" if row.get("pair_role") == "feed_binary" else "萃取剂相关二元体系",
                "结论": PAIR_STATUS_ZH.get(status, status),
                "机器状态码": status,
                "证据来源": sources,
                "未通过原因": sorted(set(errors_zh)),
                "下一步": action,
            }
        )

    task_components_zh = "–".join(
        _component_name_zh(value)
        for value in (task.get("component_a"), task.get("component_b"))
        if value
    )
    if candidate:
        solvent = candidate.get("code") or candidate.get("name") or candidate.get("aspen_component_id")
        if solvent:
            task_components_zh += "–" + _component_name_zh(solvent)
    return {
        "报告名称": "物性方法文献审查与验证报告",
        "任务编号": task.get("task_id") or "",
        "体系": task_components_zh,
        "待验证物性方法": method_family(requested_method) or "尚未指定",
        "建议采用的物性方法": chosen_method,
        "审查结论": verdict,
        "置信度": confidence,
        "Aspen运行决定": "允许" if result.get("aspen_run_authorized") else "暂不允许",
        "主要判断": conclusion,
        "验证范围说明": (
            "验证通过只适用于证据覆盖的组分、温度、压力、组成和相态范围；超出范围时必须重新审查。"
        ),
        "二元体系证据矩阵": pair_rows,
        "参考文献与数据来源": references,
        "后续任务": list(dict.fromkeys(next_actions)),
        "机器判定": {
            "状态码": result.get("status") or "",
            "正式结果可用": bool(result.get("publication_ready")),
            "筛选计算可用": bool(result.get("screening_ready")),
            "必要组分对数量": result.get("required_pair_count") or 0,
            "筛选级已闭合数量": result.get("screening_ready_pair_count") or 0,
            "正式级已闭合数量": result.get("publication_ready_pair_count") or 0,
        },
    }


def render_chinese_validation_report(report_zh):
    """Render the structured Chinese audit as Markdown for export and review."""
    rows = [
        "# {}".format(report_zh.get("报告名称") or "物性方法审查报告"),
        "",
        "- 任务编号：{}".format(report_zh.get("任务编号") or "未提供"),
        "- 体系：{}".format(report_zh.get("体系") or "未提供"),
        "- 待验证物性方法：{}".format(report_zh.get("待验证物性方法") or "未指定"),
        "- 建议采用的物性方法：{}".format(report_zh.get("建议采用的物性方法") or "未确定"),
        "- 审查结论：{}".format(report_zh.get("审查结论") or "证据不足"),
        "- 置信度：{}".format(report_zh.get("置信度") or "低"),
        "- Aspen运行决定：{}".format(report_zh.get("Aspen运行决定") or "暂不允许"),
        "",
        "## 主要判断",
        "",
        str(report_zh.get("主要判断") or ""),
        "",
        "## 二元体系证据矩阵",
        "",
        "| 组分对 | 作用 | 结论 | 下一步 |",
        "|---|---|---|---|",
    ]
    for item in report_zh.get("二元体系证据矩阵") or []:
        rows.append(
            "| {} | {} | {} | {} |".format(
                item.get("组分对") or "",
                item.get("作用") or "",
                item.get("结论") or "",
                str(item.get("下一步") or "").replace("|", "；"),
            )
        )
        if item.get("未通过原因"):
            rows.append("")
            rows.append("{} 未通过原因：{}".format(item.get("组分对"), "；".join(item.get("未通过原因") or [])))
    rows.extend(["", "## 参考文献与数据来源", ""])
    references = report_zh.get("参考文献与数据来源") or []
    if references:
        for index, item in enumerate(references, 1):
            suffix = "；DOI：{}".format(item.get("DOI")) if item.get("DOI") else ""
            rows.append("{}. {}{}".format(index, item.get("题名") or "未命名来源", suffix))
    else:
        rows.append("尚未找到可追溯的原始文献或数据库记录。")
    rows.extend(["", "## 后续任务", ""])
    actions = report_zh.get("后续任务") or []
    if actions:
        rows.extend("- {}".format(item) for item in actions)
    else:
        rows.append("- 当前证据已经闭合；仅需确认实际运行条件没有超出文献适用范围。")
    rows.extend(["", "## 适用范围", "", str(report_zh.get("验证范围说明") or "")])
    return "\n".join(rows).rstrip() + "\n"


def evidence_template(report, candidates=None):
    task = (report or {}).get("separation_task") or {}
    pair_rows = {}
    for candidate in candidates or []:
        for row in required_pairs(report, candidate=candidate):
            pair_rows[row["pair_key"]] = row
    if not pair_rows:
        for row in required_pairs(report, candidate=None):
            pair_rows[row["pair_key"]] = row
    return {
        "schema_version": 1,
        "task_id": task.get("task_id") or "",
        "policy": "Select the property model from traceable literature and system applicability first. Publication records require original or audited sources. Missing entrainer interactions may use either experiment-constrained multi-model prediction or a COSMO-RS surrogate fitted to the literature-selected model, with reality benchmarking and uncertainty-aware decision validation.",
        "required_pairs": list(pair_rows.values()),
        "literature_search_queue": [
            query
            for row in pair_rows.values()
            for query in literature_queries_for_pair(row["pair"])
        ],
        "records": [],
    }


def build_search_queue(report, candidates=None, selected_method=""):
    candidates = candidates or []
    gates = [
        evaluate_candidate(report, candidate=candidate, selected_method=selected_method)
        for candidate in candidates
    ]
    if not gates:
        gates = [evaluate_candidate(report, candidate=None, selected_method=selected_method)]
    rows = []
    seen = set()
    for gate in gates:
        for query in gate.get("next_literature_queries") or []:
            key = (query.get("pair_key"), query.get("query"))
            if key in seen:
                continue
            seen.add(key)
            item = dict(query)
            item["candidate_code"] = gate.get("candidate_code") or "feed_binary"
            item["selected_method"] = gate.get("selected_method") or method_family(selected_method)
            rows.append(item)
    return {
        "status": "complete" if not rows else "search_required",
        "selected_method": method_family(selected_method),
        "query_count": len(rows),
        "queries": rows,
    }


def nrtl_pairs_for_candidate(report, candidate=None, selected_method="NRTL", component_id_map=None):
    """Return Aspen BPVAL-ready NRTL rows after publication or screening gate passes."""
    gate = evaluate_candidate(report, candidate=candidate, selected_method=selected_method)
    if not gate.get("aspen_run_authorized") or method_family(gate.get("selected_method")) != "NRTL":
        return []
    payload, _source, _status = load_evidence_payload(report)
    records = payload.get("records") or []
    required_keys = {row["pair_key"] for row in required_pairs(report, candidate=candidate)}
    component_id_map = {
        component_id(key): str(value)
        for key, value in (component_id_map or {}).items()
        if key and value
    }
    result = []
    eligible_records = []
    for record in records:
        try:
            key = pair_key(record.get("pair") or [])
        except ThermoEvidenceError:
            continue
        if key not in required_keys:
            continue
        assessment = assess_record(record, record.get("pair") or [], "NRTL", all_records=records)
        if assessment.get("status") not in SCREENING_STATUSES:
            continue
        parameter = record.get("parameter") or {}
        if method_family(parameter.get("model")) != "NRTL":
            continue
        priority = 0 if assessment.get("status") in PUBLICATION_STATUSES else 1
        eligible_records.append((priority, key, record))
    completed_pairs = set()
    for _priority, key, record in sorted(eligible_records, key=lambda item: (item[0], item[1])):
        if key in completed_pairs:
            continue
        parameter = record.get("parameter") or {}
        for row in parameter.get("rows") or []:
            source = component_id(row.get("from"))
            target = component_id(row.get("to"))
            result.append(
                [
                    component_id_map.get(source, source),
                    component_id_map.get(target, target),
                    [float(value) for value in row.get("values") or []],
                ]
            )
        completed_pairs.add(key)
    return result
