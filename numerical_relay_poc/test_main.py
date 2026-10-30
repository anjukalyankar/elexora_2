import unittest

from main import family_for, is_official, field_evidence, _extract_mlfb_contexts, _extract_specs, _verify_extracted_relay, _contains_exact_mlfb


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

    def test_extracts_tag_from_adjacent_pdf_text_line(self):
        text = "K51\\nNUMERICAL PROTECTION RELAY\\n7SR5111-1AA11-1AA0\\nCT SEC: 1A\\nAUX. SUPPLY: 110V DC"
        rows = _extract_mlfb_contexts(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "K51")

    def test_extracts_multiple_relay_tags_independently(self):
        text = "K51\\n7SR5111-1AA11-1AA0\\n\\nK87L\\n7SD8011-5EB90-1FA0+LOA\\n\\nK50G\\nCTUM15"
        rows = _extract_mlfb_contexts(text)
        self.assertEqual([row["tag"] for row in rows], ["K51", "K87L", "K50G"])
        self.assertEqual([row["mlfb"] for row in rows], [
            "7SR5111-1AA11-1AA0", "7SD8011-5EB90-1FA0+LOA", "CTUM15"
        ])

    def test_ctum_is_classified_outside_numerical_verification(self):
        result = _verify_extracted_relay({"mlfb": "CTUM15", "msld_specs": {"auxiliary_supply": "110V DC"}})
        self.assertEqual(result["status"], "separate_scope")
        self.assertEqual(result["equipment_type"], "AUXILIARY_RELAY")
        self.assertFalse(result["production_bom_modified"])

    def test_exact_mlfb_matching_rejects_other_variants(self):
        self.assertTrue(_contains_exact_mlfb("7SR5111-1AA11-1AA0", "Product 7SR5111-1AA11-1AA0"))
        self.assertFalse(_contains_exact_mlfb("7SR5111-1AA11-1AA0", "Product 7SR5111-1AA11-2AA0"))
        self.assertTrue(_contains_exact_mlfb("7SD8011-5EB90-1FA0+LOA", "Article 7SD8011-5EB90-1FA0+LOA"))

    def test_same_mlfb_at_two_tags_is_not_merged(self):
        text = "K51\\n7SR5111-1AA11-1AA0\\nK52\\n7SR5111-1AA11-1AA0"
        rows = _extract_mlfb_contexts(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["tag"] for row in rows], ["K51", "K52"])


if __name__ == "__main__":
    unittest.main()
