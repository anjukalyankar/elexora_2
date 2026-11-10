import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.main import (
    build_rows,
    emergency_push_button_contact_config,
    master_match,
)


class TestEmergencyPushButton(unittest.TestCase):
    def test_abbreviated_msld_emergency_trip_push_button_is_recognized(self):
        self.assertEqual(
            master_match("PUSH BUTTON RED (MUSHROOM HEAD)", "1NO+1NC EM. TRIP", "S1"),
            "EMERGENCY_PUSH_BUTTON",
        )

    def test_emergency_push_button_maps_all_no_nc_configurations(self):
        cases = [
            ("1NO+1NC", "1NO+1NC", "3SB54-00-0A"),
            ("1NC + 1NO", "1NO+1NC", "3SB54-00-0A"),
            ("1NO", "1NO", "3SB54-00-0B"),
            ("1NC", "1NC", "3SB54-00-0C"),
            ("2NO", "2NO", "3SB54-00-0E"),
            ("2NC", "2NC", "3SB54-00-0F"),
        ]
        for source_contacts, expected_label, expected_type in cases:
            with self.subTest(source_contacts=source_contacts):
                record = {
                    "designation": "S1",
                    "description": "EMERGENCY TRIP PUSH BUTTON",
                    "details": source_contacts,
                }
                config = emergency_push_button_contact_config(record)
                self.assertTrue(config["confirmed"])
                self.assertEqual(config["label"], expected_label)
                self.assertEqual(config["type"], expected_type)

    def test_emergency_push_button_builds_fixed_text_and_dynamic_type(self):
        record = {
            "designation": "S1",
            "description": "EMERGENCY TRIP PUSH BUTTON",
            "details": "1NO+1NC",
        }
        master_code = master_match(record["description"], record["details"], record["designation"])
        self.assertEqual(master_code, "EMERGENCY_PUSH_BUTTON")
        record["master_code"] = master_code
        rows, _, _ = build_rows(
            [{"designation": "F1", "quantity": "1"}],
            [record],
        )
        expected = [
            "3SB50",
            "SIEMENS MAKE",
            "EMERGENCY TRIP PUSH BUTTON",
            "SICONT PUSH BUTTON ACTUATOR",
            "MUSHROOM WITH P.F.R",
            "WITH HOLDER FOR REAR CONNECTION",
            "COLOUR : RED",
            "WITH 1NO+1NC CONTACT BLOCK",
            "TYPE :3SB50-00-0UC01+3SB54-00-0A",
            "WITH PROTECTIVE SHROUDS",
        ]
        self.assertEqual(rows[0]["specification_lines"], expected)
        self.assertEqual(rows[0]["specification"], "\n".join(expected))




if __name__ == "__main__":
    unittest.main()
