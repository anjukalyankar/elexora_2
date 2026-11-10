import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.main import build_rows, limit_switch_contact_configuration, master_match


class TestElmexLimitSwitch(unittest.TestCase):
    def test_limit_switch_is_classified_separately_from_test_service_switch(self):
        self.assertEqual(
            master_match("ELMEX LIMIT SWITCH", "1NO+1NC", "LS1"),
            "LIMIT_SWITCH",
        )
        self.assertEqual(
            master_match("LIMIT SWITCH (TEST & SERVICE)", "4NO TEST + 4NO SERVICE", "LS2"),
            "S16_AUX_SWITCH",
        )

    def test_no_nc_contact_configuration_is_extracted_from_msld_row(self):
        cases = [
            ("1NO+1NC", "1NO+1NC"),
            ("1NC + 1NO", "1NO+1NC"),
            ("2NO", "2NO"),
            ("2NC", "2NC"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                record = {"description": "LIMIT SWITCH", "details": source}
                self.assertEqual(limit_switch_contact_configuration(record), expected)

    def test_missing_contact_configuration_is_not_guessed(self):
        record = {"description": "LIMIT SWITCH", "details": "OPEN EXECUTION WITH PUSH ROD"}
        self.assertEqual(
            limit_switch_contact_configuration(record),
            "[CONTACT CONFIGURATION NOT SPECIFIED]",
        )

    def test_bom_description_is_rendered_as_separate_lines(self):
        record = {
            "designation": "LS1",
            "description": "ELMEX MAKE LIMIT SWITCH",
            "details": "1NO+1NC",
            "master_code": "LIMIT_SWITCH",
            "quantity": 1,
        }
        rows, _, _ = build_rows(
            [{"designation": "F1", "quantity": "1"}],
            [record],
        )
        expected = [
            "ELMEX MAKE LIMIT SWITCH",
            "IN OPEN EXECUTION WITH PUSH ROD",
            "CONTACT ACTION: SNAP",
            "AUXILIARY CONTACTS: 1NO+1NC",
        ]
        self.assertEqual(rows[0]["specification_lines"], expected)
        self.assertEqual(rows[0]["specification"], "\\n".join(expected))


if __name__ == "__main__":
    unittest.main()
