import unittest

import full_agent_dashboard as dashboard


class EvidenceTrustBoundaryTests(unittest.TestCase):
    @staticmethod
    def compatible_report():
        return {
            "separation_task": {
                "task_id": "C3F8_C3F6_C3F8",
                "component_a": "C3F8",
                "component_b": "C3F6",
                "target_product": "C3F8",
                "property_method": "PENG-ROB",
                "compatible_verified_case": True,
                "validation_scope": "execution template only",
            },
            "counts": {
                "process_evidence_records": 1,
                "molecular_candidates": 1,
                "imported_molecular_evidence_records": 2,
                "imported_process_result_records": 1,
            },
            "online_component_info": [],
        }

    def test_template_compatibility_does_not_confirm_property_method(self):
        thermo = dashboard.build_thermo_method_confirmation(self.compatible_report(), {})

        self.assertEqual(thermo["selected_method"], "PENG-ROB")
        self.assertEqual(thermo["status"], "current_template_method_recorded_needs_validation")
        self.assertNotIn("verified", thermo["status"])
        final_check = next(row for row in thermo["evidence_checks"] if row["check"] == "final property-method decision")
        self.assertEqual(final_check["status"], "not_scientifically_confirmed")

    def test_compatible_execution_evidence_retains_publication_blockers(self):
        report = self.compatible_report()
        thermo = dashboard.build_thermo_method_confirmation(report, {})
        audit = dashboard.build_result_trust_audit(
            report,
            thermo,
            {
                "status": "available",
                "relative_volatility_prescreen": {"status": "pass", "pass_count": 1},
            },
            {"can_generate_bkp_now": True, "bkp_generation_boundary": "template execution only"},
        )

        rows = {row["gate"]: row for row in audit["audit_rows"]}
        self.assertEqual(rows["VLE/property method"]["status"], "execution_method_recorded_validation_pending")
        self.assertEqual(
            rows["final result claim"]["status"],
            "execution_evidence_available_publication_validation_pending",
        )
        self.assertEqual(audit["overall_status"], "execution_evidence_available_needs_publication_audit")
        self.assertEqual(audit["decision"], "allow_execution_level_claims_only")
        self.assertGreaterEqual(audit["blocked_or_partial_gates"], 3)
        self.assertNotIn("validated_current_case", str(audit))


if __name__ == "__main__":
    unittest.main()
