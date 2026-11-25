import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import full_agent_dashboard as dashboard
import full_screening_agent as screening
import agent_full_flow_runner as full_flow
import generic_aspen_once_optimizer as generic
import optimize_nmp_nsga2 as legacy


def legacy_row():
    return {
        "converged": True,
        "x_C3F8_top1": 0.99999,
        "recovery_C3F8": 0.999,
        "Qreb_1": 100.0,
        "Qreb_2": 50.0,
        "total_reboiler_duty": 150.0,
        "TAC": 1000.0,
        "solvent_loss_top1": 0.0,
        "solvent_loss_top2": 0.0,
        "solvent_loss_total": 0.0,
    }


def generic_row():
    return {
        "case_id": "CASE_1",
        "converged": True,
        "required_outputs_complete": True,
        "pass_or_fail": "PASS",
        "closed_loop_recycle": True,
        "recycle_control_mode": "calculator_inventory_control",
        "target_purity_mole_frac": 0.99999,
        "target_recovery": 0.999,
        "Qreb_1": 100.0,
        "Qreb_2": 50.0,
        "total_reboiler_duty": 150.0,
        "TAC": 1000.0,
        "energy_cost": 100.0,
        "constraint_violation": 0.0,
        "objective_carbon_emission": 1.0,
        "objective_TAC": 1000.0,
        "objective_recovery_loss": 0.001,
    }


def generic_spec():
    return {
        "target_purity": 0.9995,
        "target_purity_basis": "mass_fraction",
        "target_recovery": None,
        "target_product": "THF",
        "target_component_id": "THF",
        "target_product_stream": "S1",
        "streams": {
            "F": {"mole_flow": {"THF": 5.0, "GBL": 1.0}},
            "C1": {"mole_flow": {"GBL": 1.0}},
            "C2": {"mole_flow": {"GBL": 1.0}},
        },
        "blocks": [
            {"id": "B3", "in": ["S3", "C1"], "out": ["C2"]},
        ],
        "calculator": {"solvent_component": "GBL"},
        "radfrac": {
            "B1": {
                "nstage": 60,
                "feeds": {"C2": 20, "S2": 42},
                "products": {"D": [1, "V"], "W": [60, "L"]},
            },
            "B2": {
                "nstage": 12,
                "feeds": {"W": 2},
                "products": {"S1": [1, "V"], "S3": [12, "L"]},
            },
        },
    }


def generic_case(**overrides):
    case = {
        "case_id": "CASE_STAGE",
        "SF": 1.0,
        "RR1": 3.0,
        "D1": 4.8,
        "RR2": 0.8,
        "D2": 4.6,
        "N1": 120,
        "NF1": 84,
        "NS1": 50,
        "N2": 30,
        "NF2": 2,
    }
    case.update(overrides)
    return case


class PhysicalFeasibilityGateTests(unittest.TestCase):
    def test_ethylene_carbonate_uses_verified_aspen_v11_databank_id(self):
        identity = dashboard.aspen_identity_for_item({}, "EC", "Ethylene carbonate")

        self.assertEqual("C3H4O3", identity["aspen_component_id"])
        self.assertEqual("aspen_v11_com_verified_alias", identity["mapping_confidence"])
        self.assertEqual("mapped_by_agent", identity["status"])

    def test_legacy_valid_row_passes(self):
        self.assertEqual(0.0, legacy.evaluate_constraints(legacy_row(), 0.999, None))

    def test_legacy_negative_reboiler_is_rejected(self):
        row = legacy_row()
        row["Qreb_2"] = -50.0
        row["total_reboiler_duty"] = 50.0

        self.assertGreater(legacy.evaluate_constraints(row, 0.999, None), 0.0)

    def test_legacy_out_of_range_recovery_is_rejected(self):
        row = legacy_row()
        row["recovery_C3F8"] = 1.001

        self.assertGreater(legacy.evaluate_constraints(row, 0.999, None), 0.0)

    def test_generic_negative_reboiler_is_rejected(self):
        row = generic_row()
        row["Qreb_2"] = -50.0
        row["total_reboiler_duty"] = 50.0

        self.assertGreater(generic.professional_constraint_violation({}, row), 0.0)

    def test_generic_out_of_range_recovery_is_rejected(self):
        row = generic_row()
        row["target_recovery"] = 1.001

        self.assertGreater(generic.professional_constraint_violation({}, row), 0.0)

    def test_unspecified_recovery_is_not_a_hard_constraint(self):
        spec = {
            "target_purity": 0.999,
            "target_purity_basis": "mole_fraction",
            "target_recovery": None,
        }
        row = generic_row()
        row["target_recovery"] = 0.50

        self.assertIsNone(generic.target_recovery_reference(spec))
        self.assertEqual(0.0, generic.professional_constraint_violation(spec, row))

    def test_explicit_recovery_remains_a_hard_constraint(self):
        spec = {
            "target_purity": 0.999,
            "target_purity_basis": "mole_fraction",
            "target_recovery": 0.99,
        }
        row = generic_row()
        row["target_recovery"] = 0.95

        self.assertGreater(generic.professional_constraint_violation(spec, row), 0.0)

    def test_prompt_recovery_is_optional_and_explicit_only(self):
        self.assertIsNone(dashboard.parse_prompt_recovery("purity 99.95%"))
        self.assertAlmostEqual(0.95, dashboard.parse_prompt_recovery("recovery 95%"))
        self.assertAlmostEqual(0.975, dashboard.parse_prompt_recovery("\u56de\u6536\u7387 97.5%"))

    def test_screening_agent_default_recovery_is_unspecified(self):
        with mock.patch.object(sys, "argv", ["full_screening_agent.py"]):
            args = screening.parse_args()
        task = screening.build_separation_task(args)

        self.assertIsNone(args.target_recovery)
        self.assertIsNone(task["target_recovery"])
        self.assertEqual("maximize_only", task["target_recovery_mode"])

    def test_recovery_constraint_change_invalidates_stale_optimizer_evidence(self):
        report = {
            "separation_task": {
                "target_purity": 0.9995,
                "target_purity_basis": "mass_fraction",
                "target_recovery": None,
            }
        }
        progress = {
            "optimization_case_count": 10,
            "constraint_target_purity": 0.9995,
            "constraint_target_purity_basis": "mass_fraction",
            "constraint_target_recovery": 0.99,
        }
        with mock.patch.object(full_flow.agent, "aspen_progress_summary", return_value=progress):
            summary = full_flow.attach_mainline_aspen_progress(report, {})

        self.assertEqual("mismatch", summary["optimization_constraint_alignment_status"])
        self.assertTrue(summary["mainline_aspen_optimization_required"])

    def test_unspecified_recovery_matches_refreshed_optimizer_evidence(self):
        report = {
            "separation_task": {
                "target_purity": 0.9995,
                "target_purity_basis": "mass_fraction",
                "target_recovery": None,
            }
        }
        progress = {
            "optimization_case_count": 10,
            "constraint_target_purity": 0.9995,
            "constraint_target_purity_basis": "mass_fraction",
            "constraint_target_recovery": "",
        }
        with mock.patch.object(full_flow.agent, "aspen_progress_summary", return_value=progress):
            summary = full_flow.attach_mainline_aspen_progress(report, {})

        self.assertEqual("matched", summary["optimization_constraint_alignment_status"])
        self.assertEqual("maximize_only", summary["current_target_recovery_mode"])

    def test_mass_fraction_target_uses_mass_purity(self):
        spec = {
            "target_purity": 0.9995,
            "target_purity_basis": "mass_fraction",
            "target_recovery": 0.99,
        }
        row = generic_row()
        row.update({
            "target_purity_basis": "mass_fraction",
            "target_purity_mole_frac": 1.0,
            "target_purity_mass_frac": 0.9994,
        })

        self.assertGreater(generic.professional_constraint_violation(spec, row), 0.0)
        row["target_purity_mass_frac"] = 0.9995
        self.assertEqual(0.0, generic.professional_constraint_violation(spec, row))

    def test_multi_objective_candidates_require_formal_feasibility(self):
        valid = generic_row()
        invalid = generic_row()
        invalid["case_id"] = "CASE_2"
        invalid["constraint_violation"] = 1.0

        candidates = generic.multi_objective_candidate_rows([valid, invalid])

        self.assertEqual(["CASE_1"], [row["case_id"] for row in candidates])

    def test_untrusted_high_purity_metrics_do_not_guide_search(self):
        trusted = generic_row()
        trusted["target_purity_mole_frac"] = 0.9984
        trusted["target_recovery"] = 0.9418
        trusted["constraint_violation"] = 1.0
        untrusted = generic_row()
        untrusted.update({
            "case_id": "CASE_UNTRUSTED",
            "target_purity_mole_frac": 1.0,
            "target_recovery": 1.0494,
            "converged": False,
            "required_outputs_complete": False,
            "constraint_violation": 0.0,
        })

        self.assertEqual(
            ["CASE_1"],
            [row["case_id"] for row in generic.professional_metric_rows(
                [untrusted, trusted], trusted_only=True
            )],
        )
        self.assertEqual(
            "CASE_1",
            generic.best_purity_metric_row(
                [untrusted, trusted], trusted_only=True
            )["case_id"],
        )
        self.assertEqual(
            ["CASE_1"],
            [row["case_id"] for row in generic.selectable_parent_rows(
                [untrusted, trusted], 2
            )],
        )

    def test_formal_feasibility_requires_trusted_outputs(self):
        row = generic_row()
        row["converged"] = False
        row["required_outputs_complete"] = False

        self.assertFalse(generic.row_is_formal_multi_objective_feasible(row))

    def test_converged_recovery_over_one_is_not_a_trusted_parent(self):
        valid = generic_row()
        invalid = generic_row()
        invalid.update({
            "case_id": "CASE_RECOVERY_OVER_ONE",
            "target_purity_mole_frac": 1.0,
            "target_recovery": 1.06,
            "constraint_violation": 1.0,
        })

        self.assertFalse(generic.row_has_trusted_professional_metrics(invalid))
        self.assertEqual(
            ["CASE_1"],
            [row["case_id"] for row in generic.selectable_parent_rows([invalid, valid], 2)],
        )

    def test_aggressive_stage_bounds_match_apply_case_limits(self):
        bounds = generic.bounds_profile_config("aggressive_feasibility")
        case_spec = generic.apply_case(generic_spec(), generic_case(N1=180, N2=100, NF1=126, NS1=76))

        self.assertEqual((10, 180), bounds["N1"])
        self.assertEqual((4, 100), bounds["N2"])
        self.assertEqual(180, case_spec["radfrac"]["B1"]["nstage"])
        self.assertEqual(100, case_spec["radfrac"]["B2"]["nstage"])
        self.assertEqual(180, case_spec["validation_case"]["N1"])
        self.assertEqual(100, case_spec["validation_case"]["N2"])
        self.assertEqual(180, case_spec["validation_case"]["applied_N1"])
        self.assertEqual(100, case_spec["validation_case"]["applied_N2"])

    def test_apply_case_records_requested_and_applied_stage_values(self):
        case_spec = generic.apply_case(
            generic_spec(),
            generic_case(N1=220, N2=140, NF1=210, NS1=200, NF2=120),
        )
        validation_case = case_spec["validation_case"]

        self.assertEqual(180, validation_case["N1"])
        self.assertEqual(100, validation_case["N2"])
        self.assertEqual(220, validation_case["requested_N1"])
        self.assertEqual(140, validation_case["requested_N2"])
        self.assertEqual(180, validation_case["applied_N1"])
        self.assertEqual(100, validation_case["applied_N2"])
        self.assertEqual(generic.ASPEN_STAGE_APPLICATION_POLICY, validation_case["stage_application_policy"])

    def test_resume_refresh_restores_actual_stage_values_from_spec_json(self):
        stored_spec = generic.apply_case(generic_spec(), generic_case(N1=120, N2=30, NF1=84, NS1=50, NF2=2))
        stored_spec["validation_case"].update({"N1": 180, "N2": 30})
        stored_spec["validation_case"].pop("applied_N1", None)
        stored_spec["validation_case"].pop("requested_N1", None)
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / "old_case.spec.json"
            spec_path.write_text(generic.safe_json_dumps(stored_spec, ensure_ascii=False), encoding="utf-8")
            row = generic_row()
            row.update(generic_case(N1=180, N2=30, NF1=84, NS1=50, NF2=2))
            row.update({
                "spec": str(spec_path),
                "target_purity_basis": "mass_fraction",
                "target_purity_mass_frac": 0.9996,
            })

            refreshed = generic.refresh_resume_row_for_current_spec(generic_spec(), row)

        self.assertEqual(120, refreshed["N1"])
        self.assertEqual(180, refreshed["requested_N1"])
        self.assertEqual(120, refreshed["applied_N1"])
        self.assertTrue(refreshed["resume_applied_stage_values_restored"])


if __name__ == "__main__":
    unittest.main()
