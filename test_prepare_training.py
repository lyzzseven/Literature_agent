"""Regression tests for the PEO training-data admission rules."""
from copy import deepcopy
import unittest

from prepare_training import evaluate, prepare, quantity


class TrainingPreparationTest(unittest.TestCase):
    def setUp(self):
        self.study = {
            "doi": "10.1234/fixture",
            "source_url": "https://example.org/paper",
            "defaults": {
                "substrate_grade": " az31 ", "electrolyte_family": "Phosphate",
                "measurement_method": "cross_section_sem",
                "duration": {"value": 5, "unit": "min"},
                "current_density": {"value": 12, "unit": "A/dm²", "basis": "peak"},
                "electrolyte_components": {"KF": {"value": 0, "unit": "g/L"}},
            },
            "samples": [{
                "sample_id": "A", "reviewed": True, "coating_scope": "as_prepared_peo",
                "target_origin": "reported_table", "process_locator": "Methods and Table 1, row A",
                "thickness_locator": "Table 2, row A", "thickness": {"value": 8000, "unit": "nm"},
            }],
        }

    def test_preserves_units_and_missingness(self):
        [row], rejected = prepare({"studies": [self.study]})
        self.assertFalse(rejected)
        self.assertEqual(row["substrate_grade"], "AZ31")
        self.assertEqual(row["duration_s"], 300)
        self.assertEqual(row["thickness_um"], 8)
        self.assertEqual(row["current_peak_a_cm2"], .12)
        self.assertNotIn("current_setpoint_a_cm2", row)
        self.assertNotIn("frequency_hz", row)
        self.assertEqual(row["electrolyte_components"], {"KF": 0})
        self.assertAlmostEqual(quantity("current_density", {"value": 50, "unit": "mA·cm−2"}), .05)

    def test_pending_evidence_and_unit_mismatch_are_rejected(self):
        sample = self.study["samples"][0]
        self.study["samples"] = [
            {**sample, "sample_id": "unreviewed", "reviewed": False},
            {**sample, "sample_id": "wrong-unit", "thickness": {"value": 5, "unit": "Hz"}},
            {**sample, "sample_id": "graph", "target_origin": "digitized_figure"},
        ]
        clean, rejected = prepare({"studies": [self.study]})
        self.assertFalse(clean)
        self.assertEqual(len(rejected), 3)

    def test_duplicates_and_conflicts_are_distinct(self):
        sample = self.study["samples"][0]
        self.study["samples"].append(deepcopy(sample))
        clean, rejected = prepare({"studies": [self.study]})
        self.assertEqual(len(clean), 1)
        self.assertEqual(rejected[0]["reason"], "重复记录")

        self.study["samples"][1]["thickness"]["value"] = 9000
        clean, rejected = prepare({"studies": [self.study]})
        self.assertFalse(clean)
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all("冲突" in row["reason"] for row in rejected))

    def test_document_versions_share_one_validation_group(self):
        related = deepcopy(self.study)
        related["doi"] = "10.1234/related-version"
        for study in (self.study, related):
            study["study_group"] = "same-experiment"
        related["samples"][0]["sample_id"] = "B"
        unrelated = deepcopy(self.study)
        unrelated["doi"] = "10.1234/distinct"
        unrelated["study_group"] = "separate-experiment"
        clean, rejected = prepare({"studies": [self.study, related, unrelated]})
        self.assertFalse(rejected)
        folds = evaluate(clean)["folds"]
        self.assertEqual([fold["test_rows"] for fold in folds], [2, 1])
        self.assertTrue(all(fold["held_out_study"] not in fold["train_groups"] for fold in folds))


if __name__ == "__main__":
    unittest.main()
