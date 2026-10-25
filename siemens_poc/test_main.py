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


    def test_real_msld_sample_extracts_ct_and_aux_supply(self):
        result = extract_msld_specs(
            "7SJ66 SIEMENS MAKE NUM. PROT. RELAY; RATED INPUT AT 50Hz: "
            "CT SEC OF 1A; RATED AUX VOLTAGE: 110V DC; HARDWARE: "
            "3 O/C & 1 E/F, 4CT, 4VT, 16BI+7BO; SUITABLE FOR FLUSH MOUNTING; "
            "COMMUNICATION LANGUAGE: ENGLISH; WITH IEC 61850 PROTOCOL, RJ45 CONNECTOR; "
            "PROT: 50,50N,51,51N,67,67N,74TC,27,59,25; "
            "MLFB NO.: 7SJ6611-5DB90-1FG4+L0R"
        )
        self.assertEqual(result.get("ct_secondary"), "1A")
        self.assertIn("110", result.get("auxiliary_supply", ""))

    def test_chart_hardware_and_position16_ansi_capability_match(self):
        decoded = decode_7sj66_mlfb("7SJ6611-5DB90-1FG4+L0R")
        chart = {item["parameter"]: item["value"] for item in decoded["comparison_specs"]}
        chart["ansi_functions"] += ", 25"
        msld = {
            "hardware_io": "1/3 19-inch housing; 16 BI, 7 BO; 4xV, 4xI",
            "ansi_functions": "25,27,50,50N,51,51N,59,67,67N,74TC",
        }
        result = assess_technical_match(chart, msld, False)
        self.assertEqual(result["deviations"], [])
        self.assertNotIn("hardware_io", result["unverified_parameters"])

    def test_missing_specs_require_reconfirmation(self):
        result = assess_technical_match({"ct_secondary": "1A"}, None, False)
        self.assertEqual(result["status"], "MLFB_RECONFIRMATION_REQUIRED")

    def test_target_msld_has_no_false_hardware_or_ansi_deviation(self):
        decoded = decode_7sj66_mlfb("7SJ6611-5DB90-1FG4+L0R")
        chart = {item["parameter"]: item["value"] for item in decoded["comparison_specs"]}
        chart["ansi_functions"] += ", 25"
        msld = extract_msld_specs(
            "7SJ66 SIEMENS MAKE NUM. PROT. RELAY; RATED INPUT AT 50Hz: "
            "CT SEC OF 1A; RATED AUX VOLTAGE: 110V DC; HARDWARE: "
            "3 O/C & 1 E/F, 4CT, 4VT, 16BI+7BO; SUITABLE FOR FLUSH MOUNTING; "
            "COMMUNICATION LANGUAGE: ENGLISH; WITH IEC 61850 PROTOCOL, RJ45 CONNECTOR; "
            "PROT: 50,50N,51,51N,67,67N,74TC,27,59,25; "
            "MLFB NO.: 7SJ6611-5DB90-1FG4+L0R"
        )
        result = assess_technical_match(chart, msld, False)
        self.assertEqual(msld.get("region_language"), "English")
        self.assertEqual(result["deviations"], [])
        self.assertNotIn("hardware_io", result["unverified_parameters"])
        self.assertNotIn("ansi_functions", result["unverified_parameters"])
        self.assertEqual(result["status"], "MLFB_RECONFIRMATION_REQUIRED")


    def test_protection_variant_and_position16_are_compared_semantically(self):
        decoded = decode_7sj66_mlfb("7SJ6611-5DB90-1FG4+L0R")
        chart = {item["parameter"]: item["value"] for item in decoded["comparison_specs"]}
        chart["ansi_functions"] += ", 25"
        msld = {"ansi_functions": "25,27,50,50N,51,51N,59,67,67N,74TC"}
        result = assess_technical_match(chart, msld, False)
        self.assertEqual(result["deviations"], [])
        self.assertNotIn("ansi_functions", result["unverified_parameters"])
        # Missing Siemens exact product-page confirmation must still block approval.
        self.assertEqual(result["status"], "MLFB_RECONFIRMATION_REQUIRED")

    def test_chart_note_flags_incompatible_position15_sensitive_variant(self):
        result = decode_7sj66_mlfb("7SJ6611-5DB90-1FD0+L0R")
        self.assertTrue(any(
            item["positions"] == "7 and 14-15"
            for item in result["compatibility_issues"]
        ))

    def test_service_port_remains_unverified_if_mslD_does_not_specify_it(self):
        decoded = decode_7sj66_mlfb("7SJ6611-5DB90-1FG4+L0R")
        chart = {item["parameter"]: item["value"] for item in decoded["comparison_specs"]}
        result = assess_technical_match(
            chart, {"ansi_functions": "25,27,50,51,67,67N"}, False
        )
        self.assertNotIn("service_port", result["unverified_parameters"])

    def test_msld_language_is_only_extracted_when_explicit(self):
        result = extract_msld_specs("SIEMENS NUMERICAL PROTECTION RELAY; IEC 61850 RJ45")
        self.assertNotIn("region_language", result)


if __name__ == "__main__":
    unittest.main()
