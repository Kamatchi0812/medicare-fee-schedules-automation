import unittest
from pathlib import Path

import medicare_fee_schedules as schedules


class DMEPOSProcessingTests(unittest.TestCase):
    def test_selects_only_core_dmepos_text_files(self) -> None:
        files = [
            Path("DMEPOS_OCT.txt"),
            Path("DMEPEN26_JAN.txt"),
            Path("Former CBA Fee Schedule File.txt"),
            Path("DME Rural ZIP Code.txt"),
        ]

        self.assertEqual(
            [Path("DMEPOS_OCT.txt")],
            schedules.choose_dmepos_txt_files(files),
        )

    def test_quarterly_update_preserves_dme_records_by_state_and_modifier(self) -> None:
        existing = [{
            "YEAR": "2025",
            "HCPCS_CODE": "A4216",
            "MODIFIER": "",
            "JURISDICTION": "J",
            "CATEGORY": "OS",
            "REGION": "00",
            "STATE": "AK",
            "CEILING": "0.52",
        }]
        records = [
            {
                **existing[0],
                "YEAR": "2026",
                "CEILING": "0.63",
            },
            {
                **existing[0],
                "STATE": "CA",
            },
            {
                **existing[0],
                "MODIFIER": "NU",
            },
        ]

        merged, new_count, updated_count, _ = schedules.quarterly_update(
            existing,
            records,
            key_fields=schedules.DMEPOS_KEY_FIELDS,
        )

        self.assertEqual(3, len(merged))
        self.assertEqual((2, 1), (new_count, updated_count))
        self.assertIn(
            ("A4216", "", "J", "OS", "00", "AK"),
            {
                tuple(row[field] for field in schedules.DMEPOS_KEY_FIELDS)
                for row in merged
            },
        )


if __name__ == "__main__":
    unittest.main()
