import unittest

from backend.app.admin_mlfb import extract_candidate_mlfbs, normalize_mlfb
from backend.app.vcb_chart import decode_3ah3_mlfb


class TestAdminMlfbHelpers(unittest.TestCase):
    def test_extracts_common_siemens_candidate_formats(self):
        text = "MLFB: 7SJ6611-5DB90-1FG4+L0R and 3AH3-3152-EFFW-AFF2"
        found = extract_candidate_mlfbs(text)
        self.assertIn("7SJ6611-5DB90-1FG4+L0R", found)
        self.assertIn("3AH3-3152-EFFW-AFF2", found)

    def test_normalization_ignores_separators(self):
        self.assertEqual(
            normalize_mlfb("7SJ6611-5DB90-1FG4+L0R"),
            normalize_mlfb("7sj6611 5db90 1fg4 l0r"),
        )

    def test_duplicate_candidate_is_returned_once(self):
        found = extract_candidate_mlfbs("MLFB 3AH3-3152-EFFW-AFF2 / 3AH3-3152-EFFW-AFF2")
        self.assertEqual(len(found), 1)

    def test_3ah3_decoder_splits_all_selection_chart_positions(self):
        result = decode_3ah3_mlfb("3AH3-3152-EFFW-AFF2")
        self.assertEqual(result["status"], "decoded")
        self.assertEqual(len(result["decoded_positions"]), 12)
        self.assertEqual(result["positions"][5], "3")
        self.assertEqual(result["positions"][8], "2")
        self.assertEqual(result["positions"][9], "E")
        self.assertEqual(result["positions"][14], "F")
        self.assertEqual(result["positions"][15], "F")
        self.assertEqual(result["positions"][16], "2")

    def test_3ah3_decoder_reports_msld_mismatches(self):
        result = decode_3ah3_mlfb(
            "3AH3-3152-EFFW-AFF2",
            "33kV, 1250A, 31.5kA/3Sec, MOTOR: 240V AC, C.C: 110V DC, T.C: 110V DC, 12NO + 12NC, 64 WAY PLUG",
        )
        self.assertIn(result["status"], {"mismatch", "decoded_with_warnings"})
        self.assertTrue(result["checks"])

    def test_3ah3_decoder_rejects_invalid_structure(self):
        result = decode_3ah3_mlfb("3AH3-3152-EFFW")
        self.assertEqual(result["status"], "format_mismatch")


if __name__ == "__main__":
    unittest.main()
