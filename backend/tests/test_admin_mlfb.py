import unittest

from backend.app.admin_mlfb import extract_candidate_mlfbs, normalize_mlfb


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


if __name__ == "__main__":
    unittest.main()
