import unittest

from backend.app.relay_nlp import analyze_7sj66, extract_7sj66_fields


MSLD_SAMPLE = """
NUM. PROT. RELAY 7SJ6611-5EB90-1FE0-L0R
CT SEC: 1A, AUX. SUPPLY: 110V DC, 3 O/C + 1 E/F
WITH RJ45 IEC61850
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

    def test_flags_current_input_mismatch(self):
        result = analyze_7sj66(MSLD_SAMPLE)
        self.assertEqual(result["validation"]["status"], "mismatch")
        self.assertTrue(any("5 A current-input" in message for message in result["validation"]["errors"]))
        self.assertIn("MLFB NO.: 7SJ6611-5EB90-1FE0-L0R", result["description"])

    def test_missing_mlfb_does_not_claim_verified(self):
        result = analyze_7sj66("CT SEC: 1A, AUX SUPPLY: 110V DC")
        self.assertIn(result["validation"]["status"], ("mismatch", "partial"))
        self.assertTrue(any("MLFB was not found" in message for message in result["validation"]["errors"]))


if __name__ == "__main__":
    unittest.main()
