import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aspen_relative_volatility_flash as flash
import full_agent_dashboard as dashboard
import thermo_evidence_gate as gate
import zero_build_aspen_agent as zero_build


def nrtl_record(record_id, pair, rows):
    return {
        "record_id": record_id,
        "pair": pair,
        "data_type": "VLE",
        "source": {
            "type": "primary_article",
            "doi": "10.0000/{}".format(record_id),
            "url": "https://doi.org/10.0000/{}".format(record_id),
            "title": "Original VLE and NRTL parameter source",
        },
        "parameter": {
            "model": "NRTL",
            "form": "aspen_bpval_8",
            "units": {"a": "dimensionless", "b": "K", "alpha": "dimensionless"},
            "temperature_range_c": [25.0, 110.0],
            "pressure_range_mpa": [0.006, 0.102],
            "rows": rows,
        },
        "verification": {"status": "verified_exact_parameter"},
    }


def real_partial_evidence():
    return {
        "records": [
            nrtl_record(
                "ethanol-water",
                ["ETHANOL", "H2O"],
                [
                    {"from": "ETHANOL", "to": "H2O", "values": [-0.8009, 246.18, 0.3, 0, 0, 0, 25, 100]},
                    {"from": "H2O", "to": "ETHANOL", "values": [3.4578, -586.081, 0.3, 0, 0, 0, 25, 100]},
                ],
            ),
            nrtl_record(
                "water-ec",
                ["H2O", "EC"],
                [
                    {"from": "H2O", "to": "EC", "values": [1.6017, 158.52, 0.47, 0, 0, 0, 39.76, 109.98]},
                    {"from": "EC", "to": "H2O", "values": [-4.0518, 1602.5, 0.47, 0, 0, 0, 39.76, 109.98]},
                ],
            ),
            {
                "record_id": "ethanol-in-ec-idac",
                "pair": ["ETHANOL", "EC"],
                "data_type": "IDAC",
                "source": {
                    "type": "nist_thermoml",
                    "doi": "10.1021/je0102107",
                    "url": "https://trc.nist.gov/ThermoML/10.1021/je0102107.html",
                    "title": "Infinite Dilution Activity Coefficients in Ethylene Glycol and Ethylene Carbonate",
                },
                "verification": {"status": "raw_data_located"},
            },
        ]
    }


def report_payload(output_dir=""):
    return {
        "output_dir": output_dir,
        "separation_task": {
            "task_id": "ETHANOL_H2O_EC_TEST",
            "component_a": "ETHANOL",
            "component_b": "H2O",
            "target_product": "ETHANOL",
            "property_method": "NRTL",
            "compatible_verified_case": False,
        },
        "thermodynamic_evidence": real_partial_evidence(),
    }


def add_complete_ethanol_ec_parameter(report):
    report["thermodynamic_evidence"]["records"].append(
        nrtl_record(
            "ethanol-ec",
            ["ETHANOL", "EC"],
            [
                {"from": "ETHANOL", "to": "EC", "values": [0.1, 100.0, 0.3, 0, 0, 0, 25, 110]},
                {"from": "EC", "to": "ETHANOL", "values": [0.2, -50.0, 0.3, 0, 0, 0, 25, 110]},
            ],
        )
    )
    return report


def add_predictive_ethanol_ec_consensus(
    report,
    *,
    prediction_methods=None,
    constraint_ids=None,
    rmse=0.12,
    spread=0.35,
    ranking_stable=True,
):
    report["thermodynamic_evidence"]["records"].append(
        {
            "record_id": "ethanol-ec-predictive-consensus-test-only",
            "pair": ["ETHANOL", "EC"],
            "source": {
                "type": "predictive_model_consensus",
                "record_id": "synthetic-test-consensus",
                "title": "Synthetic consensus record used only by unit tests",
            },
            "parameter": {
                "model": "NRTL",
                "form": "aspen_bpval_8",
                "units": {"a": "dimensionless", "b": "K", "alpha": "dimensionless"},
                "temperature_range_c": [25.0, 110.0],
                "pressure_range_mpa": [0.006, 0.102],
                "rows": [
                    {"from": "ETHANOL", "to": "EC", "values": [0.1, 100.0, 0.3, 0, 0, 0, 25, 110]},
                    {"from": "EC", "to": "ETHANOL", "values": [0.2, -50.0, 0.3, 0, 0, 0, 25, 110]},
                ],
            },
            "verification": {
                "status": "predictive_consensus_validated",
                "prediction_methods": prediction_methods
                if prediction_methods is not None
                else [
                    {
                        "name": "UNIFAC-DMD",
                        "independence_group": "group_contribution",
                        "applicability_verified": True,
                        "group_assignment_complete": True,
                        "output_record_id": "synthetic-unifac-test-output",
                    },
                    {
                        "name": "COSMO-RS",
                        "independence_group": "quantum_chemistry",
                        "applicability_verified": True,
                        "output_record_id": "synthetic-cosmo-test-output",
                    },
                ],
                "experimental_constraint_record_ids": constraint_ids
                if constraint_ids is not None
                else ["ethanol-in-ec-idac"],
                "fit_rmse_ln_gamma": rmse,
                "max_abs_ln_gamma_spread": spread,
                "ranking_stable": ranking_stable,
                "regression_converged": True,
                "temperature_range_covered": True,
            },
        }
    )
    return report


def add_cosmors_ethanol_ec_surrogate(
    report,
    *,
    holdout_rmse=0.05,
    benchmark_count=2,
    uncertainty_propagated=True,
):
    benchmark_systems = []
    for index in range(benchmark_count):
        benchmark_systems.append(
            {
                "system": ["ETHANOL", "ANALOG-{}".format(index + 1)],
                "same_cosmors_protocol": True,
                "metric_name": "RMSE_ln_gamma",
                "metric_value": 0.12 + index * 0.01,
                "acceptance_limit": 0.20,
                "source": {
                    "type": "primary_article",
                    "doi": "10.0000/cosmo-analog-{}".format(index + 1),
                    "url": "https://doi.org/10.0000/cosmo-analog-{}".format(index + 1),
                    "title": "Analog experimental phase-equilibrium benchmark {}".format(index + 1),
                },
            }
        )
    report["thermodynamic_evidence"]["records"].append(
        {
            "record_id": "ethanol-ec-cosmors-surrogate-test-only",
            "pair": ["ETHANOL", "EC"],
            "source": {
                "type": "predictive_property_model_surrogate",
                "record_id": "synthetic-cosmors-surrogate",
                "title": "Synthetic COSMO-RS-to-literature-model audit used only by unit tests",
            },
            "parameter": {
                "model": "NRTL",
                "form": "aspen_bpval_8",
                "units": {"a": "dimensionless", "b": "K", "alpha": "dimensionless"},
                "temperature_range_c": [25.0, 110.0],
                "pressure_range_mpa": [0.006, 0.102],
                "rows": [
                    {"from": "ETHANOL", "to": "EC", "values": [0.1, 100.0, 0.3, 0, 0, 0, 25, 110]},
                    {"from": "EC", "to": "ETHANOL", "values": [0.2, -50.0, 0.3, 0, 0, 0, 25, 110]},
                ],
            },
            "verification": {
                "status": "predictive_property_model_surrogate_validated",
                "surrogate_property_model": "NRTL",
                "model_selection_record_ids": ["ethanol-water"],
                "prediction_methods": [
                    {
                        "name": "COSMO-RS",
                        "independence_group": "quantum_chemistry_cosmo",
                        "applicability_verified": True,
                        "output_record_id": "synthetic-cosmo-surface",
                        "protocol": "BP-TZVP-compatible test protocol with conformer audit",
                    }
                ],
                "fit_rmse_ln_gamma": 0.04,
                "holdout_rmse_ln_gamma": holdout_rmse,
                "max_abs_ln_gamma_error": 0.18,
                "regression_converged": True,
                "temperature_range_covered": True,
                "composition_range_covered": True,
                "infinite_dilution_endpoints_covered": True,
                "phase_behavior_consistent": True,
                "cosmors_reality_benchmark": {
                    "chemical_similarity_justified": True,
                    "systems": benchmark_systems,
                },
                "ranking_stable": True,
                "uncertainty_propagated": uncertainty_propagated,
                "decision_margin_exceeds_uncertainty": True,
            },
        }
    )
    return report


class ThermodynamicEvidenceGateTests(unittest.TestCase):
    def test_single_cosmors_route_opens_screening_only_after_three_validation_layers(self):
        report = add_cosmors_ethanol_ec_surrogate(report_payload())

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertTrue(candidate_gate["screening_ready"])
        self.assertFalse(candidate_gate["publication_ready"])
        assessment = next(
            row for row in candidate_gate["matrix"]
            if row["pair_key"] == "EC::ETHANOL"
        )["assessments"][-1]
        self.assertEqual(assessment["status"], "predictive_property_model_surrogate_validated")
        self.assertEqual(
            assessment["predictive_consensus_metrics"]["valid_analog_experimental_benchmark_count"],
            2,
        )
        self.assertTrue(
            assessment["predictive_consensus_metrics"]["validation_layers"]["screening_decision_robust"]
        )

    def test_cosmors_surrogate_is_rejected_when_reality_or_holdout_validation_is_weak(self):
        report = add_cosmors_ethanol_ec_surrogate(
            report_payload(),
            holdout_rmse=0.11,
            benchmark_count=1,
            uncertainty_propagated=False,
        )

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertFalse(candidate_gate["aspen_run_authorized"])
        assessment = next(
            row for row in candidate_gate["matrix"]
            if row["pair_key"] == "EC::ETHANOL"
        )["assessments"][-1]
        self.assertEqual(assessment["status"], "predictive_property_model_surrogate_unqualified")
        self.assertIn("cosmors_surrogate_holdout_rmse_exceeds_limit", assessment["errors"])
        self.assertIn(
            "cosmors_requires_two_traceable_analog_experimental_benchmarks",
            assessment["errors"],
        )
        self.assertIn("cosmors_benchmark_uncertainty_not_propagated", assessment["errors"])

    def test_cosmors_surrogate_follows_literature_selected_model_instead_of_forcing_nrtl(self):
        report = add_cosmors_ethanol_ec_surrogate(report_payload())
        selection_record = report["thermodynamic_evidence"]["records"][0]
        surrogate_record = report["thermodynamic_evidence"]["records"][-1]
        selection_record["parameter"]["model"] = "UNIQUAC"
        surrogate_record["parameter"]["model"] = "UNIQUAC"
        surrogate_record["parameter"]["form"] = "aspen_uniquac_pair"
        surrogate_record["verification"]["surrogate_property_model"] = "UNIQUAC"

        assessment = gate.assess_record(
            surrogate_record,
            ["ETHANOL", "EC"],
            "UNIQUAC",
            all_records=report["thermodynamic_evidence"]["records"],
        )

        self.assertEqual(assessment["parameter_model"], "UNIQUAC")
        self.assertNotIn("predictive_surrogate_model_does_not_match_parameter_model", assessment["errors"])
        self.assertEqual(assessment["status"], "parameter_adapter_required")

    def test_single_predictive_method_cannot_open_screening_gate(self):
        report = add_predictive_ethanol_ec_consensus(
            report_payload(),
            prediction_methods=[{
                "name": "UNIFAC-DMD",
                "independence_group": "group_contribution",
                "applicability_verified": True,
                "group_assignment_complete": True,
                "output_record_id": "synthetic-unifac-test-output",
            }],
        )

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertFalse(candidate_gate["screening_ready"])
        self.assertFalse(candidate_gate["aspen_run_authorized"])
        missing = candidate_gate["screening_missing_pairs"][0]
        self.assertEqual(missing["status"], "predictive_consensus_unqualified")
        assessment = next(
            row for row in candidate_gate["matrix"]
            if row["pair_key"] == "EC::ETHANOL"
        )["assessments"][-1]
        self.assertIn(
            "predictive_consensus_requires_two_independent_method_families",
            assessment["errors"],
        )

    def test_predictive_consensus_without_experimental_constraint_is_rejected(self):
        report = add_predictive_ethanol_ec_consensus(report_payload(), constraint_ids=[])

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertFalse(candidate_gate["screening_ready"])
        assessment = next(
            row for row in candidate_gate["matrix"]
            if row["pair_key"] == "EC::ETHANOL"
        )["assessments"][-1]
        self.assertIn(
            "predictive_consensus_requires_traceable_experimental_constraint",
            assessment["errors"],
        )

    def test_unifac_without_complete_group_assignment_does_not_count(self):
        report = add_predictive_ethanol_ec_consensus(
            report_payload(),
            prediction_methods=[
                {
                    "name": "UNIFAC-DMD",
                    "independence_group": "group_contribution",
                    "applicability_verified": True,
                    "group_assignment_complete": False,
                    "output_record_id": "synthetic-incomplete-unifac-output",
                },
                {
                    "name": "COSMO-RS",
                    "independence_group": "quantum_chemistry",
                    "applicability_verified": True,
                    "output_record_id": "synthetic-cosmo-test-output",
                },
            ],
        )

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertFalse(candidate_gate["aspen_run_authorized"])
        assessment = next(
            row for row in candidate_gate["matrix"]
            if row["pair_key"] == "EC::ETHANOL"
        )["assessments"][-1]
        self.assertIn("group_contribution_assignment_incomplete", assessment["errors"])

    def test_two_family_consensus_opens_screening_gate_but_not_publication_gate(self):
        report = add_predictive_ethanol_ec_consensus(report_payload())

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertTrue(candidate_gate["screening_ready"])
        self.assertTrue(candidate_gate["aspen_run_authorized"])
        self.assertFalse(candidate_gate["publication_ready"])
        self.assertEqual(candidate_gate["status"], "screening_ready_predictive")
        self.assertEqual(candidate_gate["claim_level"], "screening_only_predictive")
        self.assertTrue(candidate_gate["requires_uncertainty_campaign"])
        self.assertEqual(candidate_gate["missing_pairs"], [])
        self.assertEqual(candidate_gate["publication_missing_pairs"][0]["pair_key"], "EC::ETHANOL")

    def test_unstable_or_high_spread_consensus_is_rejected(self):
        report = add_predictive_ethanol_ec_consensus(
            report_payload(),
            spread=0.71,
            ranking_stable=False,
        )

        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertFalse(candidate_gate["aspen_run_authorized"])
        assessment = next(
            row for row in candidate_gate["matrix"]
            if row["pair_key"] == "EC::ETHANOL"
        )["assessments"][-1]
        self.assertIn("predictive_consensus_intermodel_spread_exceeds_limit", assessment["errors"])
        self.assertIn("predictive_consensus_ranking_not_stable", assessment["errors"])

    def test_predictive_consensus_rows_are_injected_but_labeled_screening_only(self):
        report = add_predictive_ethanol_ec_consensus(report_payload())
        candidate = {"code": "EC", "name": "Ethylene carbonate", "aspen_component_id": "EC"}
        request = {"solvent_code": "EC", "sf_ratio": 1.0, "property_method": "NRTL"}

        case = flash.build_case(report, request, solvent=candidate, base=False)
        deck = "\n".join(flash.deck_lines(case))

        self.assertEqual(len(case["nrtl_pairs"]), 6)
        self.assertEqual(
            case["thermodynamic_evidence_gate"]["aspen_execution_mode"],
            "predictive_screening",
        )
        self.assertFalse(case["thermodynamic_evidence_gate"]["publication_ready"])
        self.assertIn("BPVAL ETHANOL EC", deck)

    def test_zero_build_spec_keeps_predictive_screening_label(self):
        report = add_predictive_ethanol_ec_consensus(report_payload())
        pool = {
            "items": [
                {
                    "code": "EC",
                    "name": "Ethylene carbonate",
                    "aspen_component_id": "EC",
                    "relative_volatility_gate": "pass_alpha_gate",
                    "distillation_gate": "pass_high_boiling",
                    "process_gate_rank": 1,
                    "rank": 1,
                }
            ]
        }
        thermo = dashboard.build_thermo_method_confirmation(report, {})
        plan = dashboard.build_aspen_from_zero_plan(report, thermo, pool)

        self.assertTrue(plan["thermodynamic_evidence_ready"])
        self.assertTrue(plan["spec_ready"])
        injection = plan["zero_build_spec"]["thermodynamic_parameter_injection"]
        self.assertEqual(injection["directed_nrtl_row_count"], 6)
        self.assertEqual(injection["claim_level"], "screening_only_predictive")
        self.assertEqual(injection["aspen_execution_mode"], "predictive_screening")
        self.assertTrue(injection["requires_uncertainty_campaign"])
        self.assertFalse(injection["publication_ready"])

    def test_verified_non_nrtl_parameter_waits_for_an_aspen_adapter(self):
        record = nrtl_record(
            "ethanol-water-uniquac",
            ["ETHANOL", "H2O"],
            [
                {"from": "ETHANOL", "to": "H2O", "values": [0, 1, 0.3, 0, 0, 0, 25, 100]},
                {"from": "H2O", "to": "ETHANOL", "values": [0, 2, 0.3, 0, 0, 0, 25, 100]},
            ],
        )
        record["parameter"]["model"] = "UNIQUAC"

        assessment = gate.assess_record(record, ["ETHANOL", "H2O"], "UNIQUAC")

        self.assertEqual(assessment["status"], "parameter_adapter_required")

    def test_real_partial_case_selects_source_model_but_blocks_candidate(self):
        report = report_payload()
        summary = gate.build_system_evidence_summary(report)
        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertEqual(summary["recommended_method"], "NRTL")
        self.assertTrue(summary["feed_binary_gate"]["publication_ready"])
        self.assertEqual(candidate_gate["ready_pair_count"], 2)
        self.assertEqual(candidate_gate["required_pair_count"], 3)
        self.assertFalse(candidate_gate["aspen_run_authorized"])
        self.assertEqual(candidate_gate["missing_pairs"][0]["pair_key"], "EC::ETHANOL")
        self.assertEqual(candidate_gate["missing_pairs"][0]["status"], "regression_required")
        self.assertEqual(len(candidate_gate["next_literature_queries"]), 4)
        self.assertTrue(all(row["pair_key"] == "EC::ETHANOL" for row in candidate_gate["next_literature_queries"]))

    def test_complete_three_pair_package_authorizes_aspen(self):
        report = add_complete_ethanol_ec_parameter(report_payload())
        candidate_gate = gate.evaluate_candidate(
            report,
            candidate={"code": "EC", "name": "Ethylene carbonate"},
            selected_method="NRTL",
        )

        self.assertTrue(candidate_gate["aspen_run_authorized"])
        self.assertEqual(candidate_gate["ready_pair_count"], 3)
        self.assertEqual(candidate_gate["missing_pairs"], [])

    def test_verified_nrtl_rows_are_injected_into_flash_deck(self):
        report = add_complete_ethanol_ec_parameter(report_payload())
        candidate = {"code": "EC", "name": "Ethylene carbonate", "aspen_component_id": "EC"}
        request = {"solvent_code": "EC", "sf_ratio": 1.0, "property_method": "NRTL"}

        case = flash.build_case(report, request, solvent=candidate, base=False)
        deck = "\n".join(flash.deck_lines(case))

        self.assertEqual(len(case["nrtl_pairs"]), 6)
        self.assertIn("PROP-DATA NRTL-1", deck)
        self.assertIn("BPVAL ETHANOL WATER", deck)
        self.assertIn("BPVAL WATER EC", deck)
        self.assertIn("BPVAL ETHANOL EC", deck)

    def test_complete_nrtl_evidence_cannot_be_reused_for_pr(self):
        report = add_complete_ethanol_ec_parameter(report_payload())
        candidate = {"code": "EC", "name": "Ethylene carbonate", "aspen_component_id": "EC"}
        request = {"solvent_code": "EC", "sf_ratio": 1.0, "property_method": "PR"}

        with self.assertRaisesRegex(ValueError, "blocked_thermo_evidence_incomplete"):
            flash.build_case(report, request, solvent=candidate, base=False)

    def test_dashboard_prefers_original_source_method(self):
        thermo = dashboard.build_thermo_method_confirmation(report_payload(), {})

        self.assertEqual(thermo["selected_method"], "NRTL")
        self.assertEqual(thermo["execution_method_candidate"], "NRTL")
        self.assertEqual(thermo["status"], "feed_binary_evidence_verified_candidate_pairs_pending")

    def test_zero_build_plan_is_blocked_by_partial_candidate_evidence(self):
        report = report_payload()
        pool = {
            "items": [
                {
                    "code": "EC",
                    "name": "Ethylene carbonate",
                    "aspen_component_id": "EC",
                    "relative_volatility_gate": "pass_alpha_gate",
                    "distillation_gate": "pass_high_boiling",
                    "process_gate_rank": 1,
                    "rank": 1,
                }
            ]
        }
        thermo = dashboard.build_thermo_method_confirmation(report, {})
        plan = dashboard.build_aspen_from_zero_plan(report, thermo, pool)

        self.assertFalse(plan["thermodynamic_evidence_ready"])
        self.assertFalse(plan["spec_ready"])
        self.assertFalse(plan["can_generate_bkp_now"])
        self.assertEqual(plan["thermodynamic_evidence_gate"]["ready_pair_count"], 2)
        self.assertEqual(plan["thermodynamic_evidence_gate"]["required_pair_count"], 3)

    def test_zero_build_spec_receives_verified_nrtl_rows(self):
        report = add_complete_ethanol_ec_parameter(report_payload())
        pool = {
            "items": [
                {
                    "code": "EC",
                    "name": "Ethylene carbonate",
                    "aspen_component_id": "EC",
                    "relative_volatility_gate": "pass_alpha_gate",
                    "distillation_gate": "pass_high_boiling",
                    "process_gate_rank": 1,
                    "rank": 1,
                }
            ]
        }
        thermo = dashboard.build_thermo_method_confirmation(report, {})
        plan = dashboard.build_aspen_from_zero_plan(report, thermo, pool)

        self.assertTrue(plan["thermodynamic_evidence_ready"])
        self.assertTrue(plan["spec_ready"])
        self.assertEqual(len(plan["zero_build_spec"]["nrtl_pairs"]), 6)
        self.assertEqual(
            plan["zero_build_spec"]["thermodynamic_parameter_injection"]["directed_nrtl_row_count"],
            6,
        )
        deck = zero_build.render_input_deck(plan["zero_build_spec"])
        self.assertIn("BPVAL ETHANOL WATER", deck)
        self.assertIn("BPVAL ETHANOL EC", deck)

    def test_alpha_prescreen_does_not_start_aspen_when_one_pair_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = report_payload(temp_dir)
            report_path = Path(temp_dir) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            candidate = {"code": "EC", "name": "Ethylene carbonate", "aspen_component_id": "EC"}
            request = {
                "solvent_code": "EC",
                "solvent_name": "Ethylene carbonate",
                "aspen_component_id": "EC",
                "sf_ratio": 1.0,
                "x_overhead_liquid_spec": 0.5,
                "case_id": "EC_TEST",
            }
            with mock.patch.object(flash, "prepare_requests", return_value=(report, [candidate], [request])), mock.patch.object(
                flash.agent, "enrich_trial_report_payload", side_effect=lambda value: value
            ), mock.patch.object(flash, "run_aspen_case") as run_aspen:
                result = flash.run_prescreen(report_path)

            self.assertEqual(result["status"], "blocked_thermo_evidence_incomplete")
            self.assertEqual(result["base_status"], "not_run_thermo_evidence_gate")
            run_aspen.assert_not_called()

    def test_chinese_report_distinguishes_supported_method_from_missing_parameter(self):
        report = report_payload()
        candidate = {"code": "EC", "name": "Ethylene carbonate"}

        report_zh = gate.build_chinese_validation_report(
            report,
            candidate=candidate,
            selected_method="NRTL",
        )

        self.assertEqual(report_zh["审查结论"], "有条件通过（参数未闭合）")
        self.assertEqual(report_zh["Aspen运行决定"], "暂不允许")
        self.assertIn("物性方法本身获得了基础体系证据支持", report_zh["主要判断"])
        ethanol_ec = next(
            row for row in report_zh["二元体系证据矩阵"]
            if row["组分对"] == "乙醇–碳酸乙烯酯（EC）"
        )
        self.assertEqual(ethanol_ec["结论"], "已找到实验相平衡数据，但还需要回归模型参数")
        self.assertIn("未参与拟合", ethanol_ec["下一步"])

    def test_chinese_report_marks_complete_parameter_package_as_validated(self):
        report = add_complete_ethanol_ec_parameter(report_payload())
        candidate = {"code": "EC", "name": "Ethylene carbonate"}

        report_zh = gate.build_chinese_validation_report(
            report,
            candidate=candidate,
            selected_method="NRTL",
        )
        markdown = gate.render_chinese_validation_report(report_zh)

        self.assertEqual(report_zh["审查结论"], "验证通过")
        self.assertEqual(report_zh["Aspen运行决定"], "允许")
        self.assertIn("# 物性方法文献审查与验证报告", markdown)
        self.assertIn("乙醇–水–碳酸乙烯酯（EC）", markdown)

    def test_dashboard_embeds_chinese_property_method_report(self):
        thermo = dashboard.build_thermo_method_confirmation(report_payload(), {})

        report_zh = thermo["物性方法中文审查报告"]
        self.assertEqual(report_zh["体系"], "乙醇–水")
        self.assertEqual(report_zh["待验证物性方法"], "NRTL")
        self.assertEqual(report_zh["审查结论"], "验证通过")


if __name__ == "__main__":
    unittest.main()
