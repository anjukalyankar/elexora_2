import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.main import (
    build_rows,
    emergency_push_button_contact_config,
    master_match,
)


class TestEmergencyPushButton(unittest.TestCase):
    def test_emergency_push_button_uses_nc_compatible_mslD_contact_configuration(self):
        cases = [
            ("1NO+1NC", "1NO+1NC", "3SB54-00-0A"),
            ("1NC + 1NO", "1NO+1NC", "3SB54-00-0A"),
            ("1NC", "1NC", "3SB54-00-0C"),
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
                self.assertTrue(config["compatible"])
                self.assertEqual(config["label"], expected_label)
                self.assertEqual(config["type"], expected_type)

    def test_no_only_contact_blocks_are_rejected_for_trip_actuator(self):
        for source_contacts in ("1NO", "2NO"):
            with self.subTest(source_contacts=source_contacts):
                config = emergency_push_button_contact_config({
                    "designation": "S1",
                    "description": "EMERGENCY TRIP PUSH BUTTON",
                    "details": source_contacts,
                })
                self.assertFalse(config["compatible"])
                self.assertTrue(config["confirmed"])

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

    def test_unknown_contact_configuration_is_not_guessed(self):
        config = emergency_push_button_contact_config({
            "designation": "S1",
            "description": "EMERGENCY TRIP PUSH BUTTON",
            "details": "CONTACTS NOT SPECIFIED",
        })
        self.assertFalse(config["confirmed"])
        self.assertIsNone(config["type"])


if __name__ == "__main__":
    unittest.main()
