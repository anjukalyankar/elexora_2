import unittest

from main import family_for, is_official, field_evidence, _extract_mlfb_contexts, _extract_specs


class RelayPocTests(unittest.TestCase):
    def test_7sj66_is_delegated_family(self):
        self.assertEqual(family_for("7SJ6611-5DB90-1FG4+L0R"), "7SJ66")

    def test_other_relay_families(self):
        self.assertEqual(family_for("7SR1003-1JA20-2CA0"), "7SR")
        self.assertEqual(family_for("7SD610-5AB09-0AA0"), "7SD")

    def test_unknown_family_is_not_guessed(self):
        self.assertEqual(family_for("XYZ123"), "UNKNOWN")

    def test_official_domain_exact_or_subdomain(self):
        domains = ["siemens.com"]
        self.assertTrue(is_official("https://support.siemens.com/manual.pdf", domains))
        self.assertFalse(is_official("https://siemens.com.example.org/manual", domains))
        self.assertFalse(is_official("https://example.com/siemens.com", domains))

    def test_field_requires_value_and_label(self):
        self.assertEqual(field_evidence("auxiliary_supply", "110V DC", "Auxiliary supply 110V DC", [])["status"], "matched")
        self.assertEqual(field_evidence("auxiliary_supply", "110V DC", "110V DC", [])["status"], "value_seen_label_unconfirmed")
        self.assertEqual(field_evidence("auxiliary_supply", "110V DC", "Auxiliary supply 220V AC", [])["status"], "mismatch")

    def test_ct_mismatch_is_only_detected_in_field_context(self):
        result = field_evidence("ct_secondary", "1A", "CT secondary rated current 5A", [])
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["evidence_values"], ["5A"])

    def test_unrelated_different_voltage_does_not_create_mismatch(self):
        result = field_evidence("auxiliary_supply", "110V DC", "Auxiliary supply not stated; CT secondary 5A", [])
        self.assertEqual(result["status"], "not_confirmed")

    def test_protection_list_is_not_guessed_as_mismatch(self):
        result = field_evidence("protection_functions", "50, 51, 50N", "Protection functions 50, 51", ["ANSI"])
        self.assertEqual(result["status"], "not_confirmed")

    def test_list_value_is_normalized_for_field_confirmation(self):
        result = field_evidence("protection_functions", ["50", "51", "50N"], "Protection functions 50, 51, 50N", ["ANSI"])
        self.assertEqual(result["status"], "matched")

    def test_extracts_relay_mlfbs_and_tags_from_msl_drawing_text(self):
        text = "K51 NUM. PROT. RELAY 7SR5111-1AA11-1AA0 CT SEC: 1A AUX. SUPPLY: 110V DC"
        rows = _extract_mlfb_contexts(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mlfb"], "7SR5111-1AA11-1AA0")
        self.assertEqual(rows[0]["tag"], "K51")

    def test_extracts_explicit_msl_specs(self):
        text = "K51 7SR5111-1AA11-1AA0 CT SEC: 1A AUX. SUPPLY: 110V DC HARDWARE: 4CT, 4VT, 9BI+8BO [50,51,50N,51N]"
        specs = _extract_specs(text)
        self.assertEqual(specs["ct_secondary"], "1A")
        self.assertEqual(specs["auxiliary_supply"], "110V DC")
        self.assertEqual(specs["protection_functions"], ["50", "51", "50N", "51N"])


if __name__ == "__main__":
    unittest.main()
