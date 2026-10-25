import unittest

from siemens_poc.main import decode_7sj66_mlfb, extract_msld_specs, assess_technical_match


class TestSevenSJ66POC(unittest.TestCase):
    def test_decode_target_format_and_suffix(self):
        result = decode_7sj66_mlfb("7SJ6611-5EB90-1FE0-L0R")
        self.assertEqual(result["mlfb"], "7SJ6611-5EB90-1FE0-L0R")
        self.assertEqual(result["position_count"], 16)
        self.assertEqual(result["protection_variant"]["code"], "FE")
        self.assertTrue(any(item["position"] == 13 for item in result["unknown_codes"]))

    def test_decode_plus_suffix(self):
        result = decode_7sj66_mlfb("7SJ6611-5DB90-1FG4+L0R")
        self.assertEqual(result["suffix"], "L0R")
        self.assertEqual(result["protection_variant"]["code"], "FG")

    def test_invalid_format_is_not_approved(self):
        result = decode_7sj66_mlfb("7SJ66-not-an-mlfb")
        self.assertEqual(result["status"], "INVALID_FORMAT")

    def test_msld_extraction(self):
        result = extract_msld_specs(
            "K55 7SJ6611-5EB90-1FE0-L0R CT SEC: 1A AUX 110V DC "
            "4CT 4VT 16BI+7BO FLUSH MOUNTING IEC 61850 RJ45 "
            "50 51 50N 51N 27 59 74TC"
        )
        self.assertEqual(result.get("ct_secondary"), "1A")
        self.assertIn("110", result.get("auxiliary_supply", ""))
        self.assertEqual(result.get("hardware_io"), "1/3 19-inch housing; 16 BI, 7 BO; 4xV, 4xI")
        self.assertEqual(result.get("system_port"), "IEC 61850 electrical RJ45")
        self.assertIn("ansi_functions", result)

    def test_deviation_is_not_verified(self):
        result = assess_technical_match(
            {"ct_secondary": "1A"}, {"ct_secondary": "5A"}, True
        )
        self.assertEqual(result["status"], "DEVIATION_FOUND")
        self.assertEqual(result["deviations"][0]["parameter"], "ct_secondary")

    def test_missing_specs_require_reconfirmation(self):
        result = assess_technical_match({"ct_secondary": "1A"}, None, False)
        self.assertEqual(result["status"], "MLFB_RECONFIRMATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
