import unittest

from backend.app.relay_nlp import analyze_7sj66, extract_7sj66_fields


MSLD_SAMPLE = """
NUM. PROT. RELAY 7SJ6611-5EB90-1FE0-L0R
CT SEC: 1A, AUX. SUPPLY: 110V DC, 3 O/C + 1 E/F
WITH RJ45 IEC61850
HARDWARE: 4CT, 4VT, 16BI+7BO [50,51,50N,51N,27,59,74TC]
"""



K55_SAMPLE = """
NUM. PROT. RELAY
WITH RJ45 IEC61850
7SJ6612-5EB90-1FE0-L0R K55 CT SEC: 1A, AUX. SUPPLY: 110V DC, 3 O/C + 1 E/F
HARDWARE: 4CT, 4VT, 16BI+7BO [50,51,50N,51N,27,59,74TC]
"""

class TestRelayNLP(unittest.TestCase):
    def test_extracts_7sj66_fields(self):
        fields = extract_7sj66_fields(MSLD_SAMPLE)
        self.assertEqual(fields["mlfb"]["value"], "7SJ6611-5EB90-1FE0-L0R")
        self.assertEqual(fields["ct_secondary"]["value"], "1A")
        self.assertEqual(fields["aux_voltage"]["value"], "110V DC")
        self.assertEqual(fields["io_hardware"]["value"], "4CT, 4VT, 16BI+7BO")
        self.assertEqual(fields["protection"]["value"], ["50", "51", "50N", "51N", "74TC", "27", "59"])

    def test_extracts_7sj6612_mlfb_for_k55(self):
        fields = extract_7sj66_fields(K55_SAMPLE)
        self.assertEqual(fields["mlfb"]["value"], "7SJ6612-5EB90-1FE0-L0R")
        result = analyze_7sj66(K55_SAMPLE)
        self.assertNotIn("MLFB was not found", result["validation"]["errors"])

    def test_chart_consistent_mlfb_is_verified_without_generic_warning(self):
        result = analyze_7sj66(MSLD_SAMPLE)
        self.assertEqual(result["validation"]["status"], "verified")
        self.assertEqual(result["validation"]["errors"], [])
        self.assertEqual(result["validation"]["warnings"], [])
        self.assertTrue(any(check["item"] == "Current input" and check["status"] == "matched"
                            for check in result["validation"]["checks"]))
        self.assertTrue(any(check["item"] == "Position 9" and check["status"] == "matched"
                            for check in result["validation"]["checks"]))
        self.assertIn("MLFB NO.: 7SJ6611-5EB90-1FE0-L0R", result["description"])
        self.assertIn("SUITABLE FOR FLUSH MOUNTING", result["description"])
        self.assertIn("81U/O", result["description"])
        self.assertIn("32/55/81R", result["description"])

    def test_flags_actual_current_input_mismatch(self):
        source = MSLD_SAMPLE.replace("CT SEC: 1A", "CT SEC: 5A")
        result = analyze_7sj66(source)
        self.assertEqual(result["validation"]["status"], "mismatch")
        self.assertTrue(any("position 7 indicates 1A" in message
                            for message in result["validation"]["errors"]))

    def test_missing_mlfb_does_not_claim_verified(self):
        result = analyze_7sj66("CT SEC: 1A, AUX SUPPLY: 110V DC")
        self.assertIn(result["validation"]["status"], ("mismatch", "partial"))
        self.assertTrue(any("MLFB was not found" in message for message in result["validation"]["errors"]))


if __name__ == "__main__":
    unittest.main()
