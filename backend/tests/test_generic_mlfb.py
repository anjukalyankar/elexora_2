import unittest

from backend.app.generic_mlfb import build_from_chart_mapping, validate_mapping_config


class TestGenericChartMlfbBuilder(unittest.TestCase):
    def setUp(self):
        self.mapping = {
            "format_template": "ZX-{P1}-{P2}",
            "positions": [
                {"position": 1, "label": "Rated current", "required": True,
                 "options": [
                     {"match": r"\b630\s*A\b", "code": "A", "meaning": "630 A"},
                     {"match": r"\b1250\s*A\b", "code": "B", "meaning": "1250 A"},
                 ]},
                {"position": 2, "label": "Auxiliary supply", "required": True,
                 "options": [
                     {"match": r"\b110\s*V\s*DC\b", "code": "D", "meaning": "110 V DC"},
                     {"match": r"\b220\s*V\s*DC\b", "code": "E", "meaning": "220 V DC"},
                 ]},
            ],
        }

    def test_builds_from_reviewed_position_map(self):
        result = build_from_chart_mapping(self.mapping, "Rated current 1250 A; AUX 110V DC")
        self.assertEqual(result["mlfb"], "ZX-B-D")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(len(result["decoded_positions"]), 2)

    def test_missing_required_value_blocks_generation(self):
        result = build_from_chart_mapping(self.mapping, "Rated current 1250 A")
        self.assertEqual(result["mlfb"], "")
        self.assertIn("required position 2", result["warnings"][0])

    def test_conflicting_values_block_generation(self):
        result = build_from_chart_mapping(self.mapping, "630 A or 1250 A; AUX 110V DC")
        self.assertEqual(result["mlfb"], "")
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["errors"])

    def test_rejects_duplicate_position_and_template_mismatch(self):
        invalid = dict(self.mapping)
        invalid["positions"] = self.mapping["positions"] + [self.mapping["positions"][0]]
        with self.assertRaises(ValueError):
            validate_mapping_config(invalid)
        invalid = dict(self.mapping)
        invalid["format_template"] = "ZX-{P1}"
        with self.assertRaises(ValueError):
            validate_mapping_config(invalid)

    def test_rejects_invalid_regex(self):
        invalid = {
            "format_template": "ZX-{P1}",
            "positions": [{"position": 1, "options": [{"match": "(", "code": "A"}]}],
        }
        with self.assertRaises(ValueError):
            validate_mapping_config(invalid)


if __name__ == "__main__":
    unittest.main()
