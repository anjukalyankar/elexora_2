import unittest
from unittest.mock import patch

from app.main import build_rows


class TestPTBuiltInPrimaryFuseRow(unittest.TestCase):
    @patch("app.main.pt_spec", return_value=[
        "PT 11kV",
        "MAKE",
        "POTENTIAL TRANSFORMERS IN CAST RESIN EPOXY CASE (DRAWOUT TYPE)",
        "- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A",
    ])
    def test_fuse_designation_and_range_quantity_are_shown_on_pt_row(self, _pt_spec):
        records = [
            {
                "designation": "T1-T3",
                "description": "POTENTIAL TRANSFORMER",
                "details": "",
                "master_code": "PT",
                "quantity": 3,
            },
            {
                "designation": "F1-F3",
                "description": "PRIMARY FUSE 3.15A",
                "details": "",
                "master_code": "F1-F3",
                "quantity": 3,
            },
        ]

        rows = build_rows([], records)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["designation"], "F1-F3")
        self.assertEqual(rows[0]["eqpt_qty"], 3)
        self.assertEqual(rows[0]["total"], 3)
        self.assertIn(
            "- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A",
            rows[0]["specification_lines"],
        )

    @patch("app.main.pt_spec", return_value=["PT", "- SINGLE PHASE WITH IN BUILT PRIMARY FUSE OF 3.15A"])
    def test_non_fuse_f_designation_is_not_absorbed(self, _pt_spec):
        records = [
            {
                "designation": "T1-T3",
                "description": "POTENTIAL TRANSFORMER",
                "details": "",
                "master_code": "PT",
                "quantity": 3,
            },
            {
                "designation": "F1-F3",
                "description": "INDICATION CONTACTS",
                "details": "",
                "master_code": "F1-F3",
                "quantity": 3,
            },
        ]

        rows = build_rows([], records)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["designation"], "T1-T3")
        self.assertEqual(rows[0]["eqpt_qty"], 3)
        self.assertEqual(rows[1]["designation"], "F1-F3")


if __name__ == "__main__":
    unittest.main()
