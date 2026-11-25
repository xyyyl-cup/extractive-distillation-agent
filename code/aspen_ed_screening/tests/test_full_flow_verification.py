import unittest
from unittest import mock

import agent_full_flow_runner as runner
import full_agent_dashboard as dashboard


def complete_report():
    return {
        "separation_task": {
            "component_a": "C3F8",
            "component_b": "C3F6",
            "target_product": "C3F8",
            "compatible_verified_case": True,
        },
        "counts": {
            "imported_molecular_evidence_records": 9,
            "imported_strict_gromacs_records": 2,
            "imported_process_result_records": 12000,
            "process_evidence_records": 13,
        },
        "process_result_import": {
            "closed_loop_optimization_pass_case_count": 100,
            "closed_loop_completion_allowed": True,
            "multi_objective_optimization_complete": True,
            "multi_objective_converged": True,
            "publication_optimization_budget_complete": True,
            "optimization_solvent_alignment_status": "matched",
            "mainline_aspen_optimization_required": False,
            "mainline_multi_objective_converged": True,
            "optimization_case_count": 100000,
            "multi_objective_feasible_case_count": 100000,
            "max_optimization_generation": 1000,
        },
    }


class FullFlowVerificationTests(unittest.TestCase):
    def test_compatible_case_does_not_bypass_missing_evidence(self):
        report = complete_report()
        report["counts"]["imported_molecular_evidence_records"] = 0
        report["counts"]["imported_strict_gromacs_records"] = 0

        audit = dashboard.task_full_flow_verification(report)

        self.assertFalse(audit["verified"])
        self.assertIn("molecular_evidence_imported", audit["blockers"])
        self.assertIn("strict_gromacs_pair_imported", audit["blockers"])

    def test_full_evidence_passes_uniform_gate(self):
        audit = dashboard.task_full_flow_verification(complete_report())

        self.assertTrue(audit["verified"])
        self.assertEqual([], audit["blockers"])

    def test_budget_and_alignment_are_mandatory(self):
        report = complete_report()
        report["process_result_import"]["publication_optimization_budget_complete"] = False
        report["process_result_import"]["optimization_solvent_alignment_status"] = "mismatch"

        audit = dashboard.task_full_flow_verification(report)

        self.assertFalse(audit["verified"])
        self.assertIn("publication_budget_complete", audit["blockers"])
        self.assertIn("solvent_alignment_matched", audit["blockers"])

    def test_legacy_process_files_alone_are_not_completion(self):
        report = complete_report()
        report["counts"] = {
            "process_evidence_records": 13,
            "imported_process_result_records": 12000,
        }
        report["process_result_import"] = {}

        audit = dashboard.task_full_flow_verification(report)

        self.assertFalse(audit["verified"])
        self.assertTrue(audit["compatible_verified_case_is_evidence_only"])
        self.assertIn("closed_loop_pass_present", audit["blockers"])

    def test_changed_purity_basis_requires_mainline_refresh(self):
        report = {
            "separation_task": {
                "target_purity": 0.9995,
                "target_purity_basis": "mass_fraction",
            }
        }
        old_progress = {
            "optimization_case_count": 427,
            "constraint_target_purity": 0.99999,
            "constraint_target_purity_basis": "mole_fraction",
            "optimization_solvent_alignment_status": "matched",
            "mainline_aspen_optimization_required": False,
        }

        with mock.patch.object(runner.agent, "aspen_progress_summary", return_value=old_progress):
            summary = runner.attach_mainline_aspen_progress(report, {})

        self.assertEqual("mismatch", summary["optimization_constraint_alignment_status"])
        self.assertTrue(runner.is_mainline_aspen_required(summary))


if __name__ == "__main__":
    unittest.main()
