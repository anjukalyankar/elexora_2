import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.main import build_rows, master_match, space_heater_validation


class TestE11E12SpaceHeater(unittest.TestCase):
    def test_e11_and_e12_use_approved_line_by_line_specification(self):
        expected = [
            "APPROVED MAKE",
            "PANEL SPACE HEATER",
            "IN PERFORATED ENCLOSURE",
            "WATTAGE: 200 WATTS",
            "VOLTAGE: 240V AC",
        ]
        for designation in ("E11", "E12"):
            with self.subTest(designation=designation):
                description = "SPACE HEATER"
                details = "200W, 240V AC"
                master_code = master_match(description, details, designation)
                self.assertEqual(master_code, "SPACE_HEATER")
                rows, _, _ = build_rows(
                    [{"designation": "F1", "quantity": "1"}],
                    [{
                        "designation": designation,
                        "description": description,
                        "details": details,
                        "master_code": master_code,
                        "quantity": 1,
                    }],
                )
                self.assertEqual(rows[0]["specification_lines"], expected)
                self.assertEqual(rows[0]["specification"], "\n".join(expected))

    def test_space_heater_rating_mismatch_is_reported(self):
        warnings = space_heater_validation({
            "designation": "E11",
            "description": "SPACE HEATER",
            "details": "150W, 230V AC",
        })
        self.assertTrue(any("150 Watts" in warning for warning in warnings))
        self.assertTrue(any("230.0V AC" in warning or "230V AC" in warning for warning in warnings))

    def test_space_heater_missing_rating_is_reported(self):
        warnings = space_heater_validation({
            "designation": "E12",
            "description": "SPACE HEATER",
            "details": "",
        })
        self.assertTrue(any("wattage is missing" in warning for warning in warnings))
        self.assertTrue(any("voltage is missing" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
