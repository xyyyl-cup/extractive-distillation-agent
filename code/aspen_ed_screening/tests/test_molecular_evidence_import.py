import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import full_agent_dashboard as dashboard


class MolecularEvidenceImportTests(unittest.TestCase):
    @staticmethod
    def write_summary(path, delta, threshold_status):
        fields = [
            "ExtractantCode",
            "ExtractantName",
            "C3F6_Total_kJ_mol",
            "C3F8_Total_kJ_mol",
            "Delta_C3F8_minus_C3F6_kJ_mol",
            "GROMACS_Judgement",
            "Advice",
            "Meaning",
            "AnalysisWindow_ps",
            "BlockIntervalLow_kJ_mol",
            "BlockIntervalHigh_kJ_mol",
            "ThresholdStatus",
            "UncertaintyScope",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(
                {
                    "ExtractantCode": "NMP",
                    "ExtractantName": "NMP",
                    "C3F6_Total_kJ_mol": -593.9954,
                    "C3F8_Total_kJ_mol": -582.3274,
                    "Delta_C3F8_minus_C3F6_kJ_mol": delta,
                    "GROMACS_Judgement": "C3F6 stronger",
                    "Advice": "replicate",
                    "Meaning": "1000-2000 ps short-range interaction energy",
                    "AnalysisWindow_ps": "1000-2000",
                    "BlockIntervalLow_kJ_mol": -31.6362,
                    "BlockIntervalHigh_kJ_mol": 54.9722,
                    "ThresholdStatus": threshold_status,
                    "UncertaintyScope": "five contiguous time blocks; not independent trajectory replicas",
                }
            )

    def test_equilibrated_summary_is_preferred_over_legacy_full_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_summary(root / "GROMACS_equilibrated_interaction_summary.csv", 11.668, "threshold_uncertain")
            self.write_summary(root / "GROMACS_auto_interaction_summary.csv", 44.1641, "legacy_window_no_uncertainty")
            with mock.patch.object(dashboard, "GROMACS_MASTER_DIR", root), mock.patch.object(
                dashboard, "LEGACY_MOLECULAR_EVIDENCE_CACHE", None
            ):
                preferred = dashboard.preferred_gromacs_interaction_summary_path()
                evidence = dashboard.load_legacy_molecular_evidence()

        row = evidence["gromacs_interaction"]["NMP"]
        self.assertEqual(preferred.name, "GROMACS_equilibrated_interaction_summary.csv")
        self.assertAlmostEqual(row["delta_c3f8_minus_c3f6_kj_mol"], 11.668)
        self.assertEqual(row["analysis_window_ps"], "1000-2000")
        self.assertEqual(row["threshold_status"], "threshold_uncertain")

    def test_uncertainty_unqualified_memory_cannot_change_candidate_score(self):
        task = {
            "component_a": "C3F8",
            "component_b": "C3F6",
            "target_product": "C3F8",
        }
        signature = dashboard.task_learning_signature(task)
        memory = {
            "records": [
                {
                    "task_signature": signature,
                    "solvent_code": "PC",
                    "family": "carbonate",
                    "molecular_selectivity_score": 32.8562,
                    "effect_label": "good",
                },
                {
                    "task_signature": "OTHER_SYSTEM",
                    "solvent_code": "EC",
                    "family": "carbonate",
                    "molecular_selectivity_score": 100.0,
                    "effect_label": "good",
                    "evidence_confidence": "independent_replicates_supported",
                },
            ]
        }
        with mock.patch.object(dashboard, "load_solvent_learning_memory", return_value=memory):
            context = dashboard.solvent_learning_context(task)

        self.assertEqual(context["exact"]["PC"]["effective_label"], "")
        self.assertEqual(context["exact"]["PC"]["learning_use_status"], "trace_only_unqualified")
        self.assertEqual(context["family"], {})

    def test_block_uncertain_evidence_is_reported_without_learning_uplift(self):
        note = dashboard.candidate_learning_note(
            {"code": "PC"},
            {"exact": {"PC": {"effect_label": "good", "molecular_selectivity_score": 32.8562}}},
            legacy_evidence={
                "status": "legacy_c3f6_c3f8_evidence_available",
                "gromacs_delta_c3f8_minus_c3f6_kj_mol": 32.8562,
                "gromacs_threshold_status": "threshold_uncertain",
                "gromacs_uncertainty_scope": "five contiguous time blocks; not independent trajectory replicas",
            },
        )

        self.assertEqual(note["learning_status"], "molecular_threshold_uncertain")
        self.assertEqual(note["learning_adjustment"], "none_pending_independent_replicates")
        self.assertNotIn("tested_good", note["learning_status"])


if __name__ == "__main__":
    unittest.main()
