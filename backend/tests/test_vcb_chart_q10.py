import unittest

from app.vcb_chart import build_3ah3_q10_spec


class TestQ10ThreeAH3ChartMapping(unittest.TestCase):
    def test_q10_mlfb_and_plain_text_output(self):
        row = {
            "designation": "Q10",
            "description": (
                "11KV, 3AH3, 1250A, 31.5KA/3Sec, 12NO + 12NC, 64 WAY PLUG "
                "MOTOR: 110V DC, C.C: 110V DC, T.C: 110V DC"
            ),
        }
        lines, validation = build_3ah3_q10_spec(row)

        self.assertEqual(validation["mlfb"], "3AH3-1152-AEEA-AEF2")
        self.assertEqual(validation["validation_status"], "matched")
        self.assertEqual(
            lines,
            [
                "CB1250A",
                "SIEMENS MAKE VACUUM CB, TYPE 3AH3, 1250AMPS",
                "12kV, IMPULSE 75kV, BREAKING CAPACITY : 31.5kA FOR 3 SEC",
                "SPRING CHARGING MOTOR VOLTAGE : 110V DC",
                "CLOSING COIL VOLTAGE : 110V DC",
                "TRIP COIL VOLTAGE : 110V DC",
                "ELECT SPRING CHARGE INDICATION LIMIT SW",
                "WITH ANTI PUMPING FEATURE, AUX SWITCH 12NO + 12NC",
                "SUITABLE FOR D/O ARRANGEMENT, PLUG & SOCKET 64 PIN",
                "MECH (ON/OFF(IND+PB),(CONT+SPR CHRG IND))",
                "MLFB NO. : 3AH3-1152-AEEA-AEF2",
                "VCB TO BE SUITABLE TO INSERTION IN 8BK80(OD)-800MM PANEL",
            ],
        )
        self.assertEqual(validation["chart_positions"], {
            "5-8": "1152", "9": "A", "10": "E", "11": "E",
            "12": "A", "13": "A", "14": "E", "15": "F", "16": "2",
        })

    def test_unknown_supply_does_not_guess_mlfb(self):
        row = {
            "designation": "Q10",
            "description": (
                "11KV, 3AH3, 1250A, 31.5KA/3Sec, 12NO + 12NC, 64 WAY PLUG "
                "MOTOR: 230V AC, C.C: 110V DC, T.C: 110V DC"
            ),
        }
        lines, validation = build_3ah3_q10_spec(row)
        self.assertEqual(validation["mlfb"], "")
        self.assertEqual(validation["validation_status"], "incomplete_or_mismatch")
        self.assertIn("Motor supply is not mapped in the supplied chart.", validation["warnings"])
        self.assertIn("NOT GENERATED", lines[10])


if __name__ == "__main__":
    unittest.main()
