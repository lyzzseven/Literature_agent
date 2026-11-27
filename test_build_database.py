"""Checks of the multidimensional PEO database and its scientific boundaries."""
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest

from build_database import build, normalize_observation


DATASET = Path(__file__).parent / "datasets" / "peo_database_v1"


class DatabaseTest(unittest.TestCase):
    def test_measurements_require_compatible_units_and_environment(self):
        wear = {"metric": "wear_volume", "value": "3.1", "unit": "10^-3 mm³"}
        with self.assertRaisesRegex(ValueError, "缺少测试环境"):
            normalize_observation(wear, {})
        with self.assertRaisesRegex(ValueError, "单位不匹配"):
            normalize_observation({**wear, "unit": "µm"}, {"medium": "dry_air"})
        self.assertAlmostEqual(normalize_observation(wear, {"medium": "dry_air"})[1], .0031)
        self.assertIsNone(normalize_observation({"metric": "morphology", "value": "微孔"}, {})[1])

    def test_real_database_rebuild_and_links(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "peo.sqlite"
            rejected = build(DATASET / "database_inputs.json", output)
            self.assertEqual(len(rejected), 1)
            with closing(sqlite3.connect(output)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone(), (113,))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone(), (60,))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone(), (251,))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM model_candidates").fetchone(), (244,))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_candidates").fetchone(), (23,))
                conditions = db.execute("""
                    SELECT duration_s,exposure_h,value,unit FROM model_candidates
                    WHERE doi='10.3390/coatings10100937' AND sample_key='Al' AND metric='wear_volume'
                    ORDER BY exposure_h
                """).fetchall()
                self.assertEqual([(r[0], r[1], r[3]) for r in conditions],
                                 [(900, .25, "mm³"), (900, .5, "mm³"), (900, 1., "mm³")])
                self.assertAlmostEqual(conditions[0][2], .0031)
                rp = db.execute("""
                    SELECT exposure_h,value,unit,medium,concentration FROM model_candidates
                    WHERE metric='polarization_resistance' AND sample_key='8 g/L phosphate hydrate; 0.025 A/cm²'
                    ORDER BY exposure_h
                """).fetchall()
                self.assertEqual(len(rp), 9)
                self.assertEqual(rp[0], (1., 76821., "Ω·cm²", "NaCl", .1))
                self.assertEqual(rp[-1], (168., 5878., "Ω·cm²", "NaCl", .1))
                self.assertEqual(db.execute("""
                    SELECT medium, count(*) FROM model_candidates WHERE metric='corrosion_current_density'
                    GROUP BY medium ORDER BY medium
                """).fetchall(), [("Hanks", 9), ("SBF", 4)])
                self.assertEqual(db.execute("""
                    SELECT count(*) FROM sample_assets l JOIN samples s ON s.id=l.sample_id
                    JOIN assets a ON a.id=l.asset_id WHERE s.doi != a.doi
                """).fetchone(), (0,))
                assets = db.execute("SELECT filename,sha256,content FROM assets").fetchall()
                self.assertEqual(len(assets), 31)
                for name, digest, data in assets:
                    self.assertEqual(hashlib.sha256(data).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()
