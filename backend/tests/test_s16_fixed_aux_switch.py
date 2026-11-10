import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.main import build_rows, master_match


class TestS16FixedAuxSwitchSpecification(unittest.TestCase):
    def test_s16_is_always_mapped_to_fixed_specification(self):
        self.assertEqual(master_match("anything in MSLD", "other details", "S16"), "S16_AUX_SWITCH")

        rows, _, _ = build_rows(
            [{"designation": "F1", "quantity": "1"}],
            [{
                "designation": "S16",
                "description": "Different source wording or empty",
                "details": "Any additional details",
                "master_code": "S16_AUX_SWITCH",
                "quantity": 1,
            }],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["specification_lines"], [
            "B3NZ331",
            "SIEMENS MAKE",
            "AUX SWITCH FOR 3AH CIRCUIT",
            "BREAKER TEST/SERVICE POSITION",
            "4NO AT EACH TEST & SERVICE POSITION",
        ])
        self.assertEqual(rows[0]["specification"], "\n".join(rows[0]["specification_lines"]))


    def test_test_service_limit_switch_is_recognized_without_s16_designation(self):
        description = "LIMIT SWITCH (TEST & SERVICE)"
        details = "4NO TEST + 4NO SERVICE"
        master_code = master_match(description, details, "LS1")
        self.assertEqual(master_code, "S16_AUX_SWITCH")

        rows, _, _ = build_rows(
            [{"designation": "F1", "quantity": "1"}],
            [{
                "designation": "LS1",
                "description": description,
                "details": details,
                "master_code": master_code,
                "quantity": 1,
            }],
        )
        self.assertEqual(rows[0]["specification_lines"], [
            "B3NZ331",
            "SIEMENS MAKE",
            "AUX SWITCH FOR 3AH CIRCUIT",
            "BREAKER TEST/SERVICE POSITION",
            "4NO AT EACH TEST & SERVICE POSITION",
        ])

    def test_generic_limit_switch_is_not_misclassified(self):
        self.assertNotEqual(
            master_match("LIMIT SWITCH", "2NO + 2NC", "LS2"),
            "S16_AUX_SWITCH",
        )


if __name__ == "__main__":
    unittest.main()
