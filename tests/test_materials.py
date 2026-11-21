"""检查交换资料的结构和记录间引用。"""
import json
import subprocess
import sys
from pathlib import Path
import unittest

from catalog import read_table

ROOT = Path(__file__).resolve().parents[1]
TABLE_KEYS = {'flow_rules': ['flow', 'stage'], 'journeys': ['journey'], 'time_proofs': ['journey', 'stage'], 'family_changes': ['journey'], 'joint_variants': ['variant'], 'workday_schedule': ['day', 'journey'], 'window_acceptance': ['variant']}


class MaterialsTest(unittest.TestCase):
    def rows(self, name):
        return list(read_table(name))

    def assert_subset(self, left, reference, right, key):
        self.assertLessEqual({r[reference] for r in self.rows(left)},
                             {r[key] for r in self.rows(right)})

    def test_structure(self):
        for name, columns in TABLE_KEYS.items():
            with self.subTest(name=name):
                rows = self.rows(name)
                self.assertTrue(rows)
                keys = []
                for row in rows:
                    self.assertNotIn(None, row)
                    self.assertNotIn(None, row.values())
                    value = tuple(row[field] for field in columns)
                    self.assertTrue(all(value))
                    keys.append(value)
                self.assertEqual(len(keys), len(set(keys)))

    def test_catalog_reports_input_counts(self):
        result = subprocess.check_output([sys.executable, "catalog.py"], cwd=ROOT, text=True)
        self.assertEqual(json.loads(result), {name: len(self.rows(name)) for name in TABLE_KEYS})

    def test_business_references(self):
        self.assert_subset("time_proofs", "journey", "journeys", "journey")
        self.assertTrue(any(int(row["latest"]) > 1440 for row in self.rows("time_proofs")))
        self.assertEqual({r["result"] for r in self.rows("journeys")}, {"served", "replacement", "no_replacement"})
        self.assertEqual({row["vehicle"] for row in self.rows("workday_schedule") if row["journey"] in {"E001", "E002"}}, {"C1"})
