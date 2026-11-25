import unittest
from unittest import mock

from tools import thermo_literature_auditor as auditor


class ThermoLiteratureAuditorTests(unittest.TestCase):
    def test_queries_include_exact_entrainer_and_chemical_family(self):
        queries = auditor.build_queries("ETHANOL", "H2O", "EC", "NRTL")

        self.assertTrue(any("ethylene carbonate" in query for query in queries))
        self.assertTrue(any("cyclic carbonate" in query for query in queries))
        self.assertTrue(any("NRTL" in query for query in queries))

    def test_supplementary_records_are_not_treated_as_primary_articles(self):
        self.assertFalse(
            auditor._is_primary_article(
                {
                    "title": "Supporting Information",
                    "doi": "10.1021/example.s001",
                    "type": "component",
                }
            )
        )
        self.assertTrue(
            auditor._is_primary_article(
                {
                    "title": "Experimental VLE and NRTL correlation",
                    "doi": "10.1021/example",
                    "type": "journal-article",
                }
            )
        )

    def test_full_text_extraction_requires_method_and_experimental_validation_context(self):
        text = """
        Experimental vapor-liquid equilibrium measurements were carried out for
        ethanol, water, and ethylene carbonate over the required composition range.
        The NRTL model was selected as a good model to represent the experimental
        data. The holdout RMSE was 0.05 and the predicted azeotropic behavior agreed
        with the measurements. Binary interaction parameters were regressed from the
        training observations and evaluated against an independent test set.
        """

        evidence = auditor.extract_method_evidence(text, "ETHANOL", "H2O", "EC")
        nrtl = next(row for row in evidence["物性方法证据"] if row["方法"] == "NRTL")

        self.assertEqual(evidence["体系匹配范围"], "目标体系实验范围")
        self.assertTrue(nrtl["与实验验证同时出现"])
        self.assertTrue(nrtl["包含数值误差指标"])
        self.assertTrue(nrtl["涉及参数回归"])

    def test_method_scope_does_not_promote_binary_nrtl_to_ternary_validation(self):
        text = """
        Experimental VLE data were measured for ethanol, water, and propylene
        carbonate. NRTL parameters available for the binary system alcohol + water
        describe properly the binary phase equilibria. COSMO-SAC was selected as a
        good model to represent the experimental data for the ternary alcohol +
        water + cyclic carbonate mixtures.
        """

        evidence = auditor.extract_method_evidence(text, "ETHANOL", "H2O", "EC")
        nrtl = next(row for row in evidence["物性方法证据"] if row["方法"] == "NRTL")
        cosmo_sac = next(row for row in evidence["物性方法证据"] if row["方法"] == "COSMO-SAC")

        self.assertEqual(nrtl["验证范围"], "仅基础二元体系")
        self.assertEqual(cosmo_sac["验证范围"], "基础体系相同、同类萃取剂实验范围")

    def test_azeotrope_table_is_converted_to_numerical_error_metrics(self):
        text = """
        Table 4 Validation of the water-azeotrope point in mixtures with three
        different light alcohols: experimental results and predictions using the
        COSMOSAC model in Aspen Plus v14. Water in azeotrope wt% Alcohol Exp
        COSMOSAC Ethanol 4.0 4.0 2-Propanol 12.6 13.7 t-Butanol 11.8 15.1.
        """

        metrics = auditor.extract_tabular_validation_metrics(text)

        self.assertEqual(len(metrics), 1)
        self.assertEqual(len(metrics[0]["数据行"]), 3)
        self.assertAlmostEqual(metrics[0]["平均绝对误差"], 1.466667, places=6)
        self.assertAlmostEqual(metrics[0]["平均相对误差百分比"], 12.232087, places=5)
        self.assertEqual(metrics[0]["验证范围"], "仅基础二元体系")

    def test_audit_validates_selected_method_only_from_full_text_evidence(self):
        search_payload = {
            "queries": ["test query"],
            "failures": [],
            "records": [
                {
                    "title": "Experimental target-system VLE",
                    "doi": "10.0000/test",
                    "url": "https://doi.org/10.0000/test",
                    "year": 2026,
                    "type": "journal-article",
                    "relevance_score": 50,
                    "status": "仅元数据线索",
                }
            ],
        }
        full_text = """
        Experimental VLE measurements for ethanol, water, and ethylene carbonate
        were obtained. The NRTL model was validated and selected as a good model
        to represent the experimental data. The holdout RMSE was 0.04.
        """
        with mock.patch.object(auditor, "search_literature", return_value=search_payload), mock.patch.object(
            auditor,
            "obtain_full_text",
            return_value={
                "status": "已取得全文",
                "text": full_text,
                "source_url": "https://example.test/fulltext",
                "source_route": "test",
                "attempts": [],
            },
        ):
            report = auditor.audit_property_method("ETHANOL", "H2O", "EC", "NRTL")

        self.assertEqual(report["审查结论"], "验证通过")
        self.assertEqual(report["已取得并分析正文数量"], 1)
        self.assertEqual(report["当前方法证据"][0]["方法"], "NRTL")

    def test_audit_does_not_validate_selected_method_when_only_an_alternative_is_supported(self):
        search_payload = {
            "queries": ["test query"],
            "failures": [],
            "records": [
                {
                    "title": "Experimental target-system VLE",
                    "doi": "10.0000/test",
                    "url": "https://doi.org/10.0000/test",
                    "year": 2026,
                    "type": "journal-article",
                    "relevance_score": 50,
                    "status": "仅元数据线索",
                }
            ],
        }
        full_text = """
        Experimental VLE measurements for ethanol, water, and ethylene carbonate
        were obtained. COSMO-SAC was validated and selected as a good model to
        represent the experimental data. The average absolute deviation was 3.2%.
        """
        with mock.patch.object(auditor, "search_literature", return_value=search_payload), mock.patch.object(
            auditor,
            "obtain_full_text",
            return_value={
                "status": "已取得全文",
                "text": full_text,
                "source_url": "https://example.test/fulltext",
                "source_route": "test",
                "attempts": [],
            },
        ):
            report = auditor.audit_property_method("ETHANOL", "H2O", "EC", "NRTL")

        self.assertEqual(report["审查结论"], "当前选择未获验证")
        self.assertEqual(report["当前方法证据"], [])
        self.assertEqual(report["其他方法证据"][0]["方法"], "COSMO-SAC")


if __name__ == "__main__":
    unittest.main()
