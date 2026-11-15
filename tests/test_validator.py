"""
Unit tests for validator.py post-download validation engine.
"""

from datetime import date, datetime, timedelta
import os
import sys
import unittest
import zipfile

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from validator import (
    parse_flexible_date,
    verify_file_integrity,
    validate_in_depth_sheet,
    validate_parking_sheet,
    validate_exports,
    REPORT_SPECS,
)


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(BASE_DIR, "tests", "temp_test_exports")
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            for f in os.listdir(self.test_dir):
                try:
                    os.remove(os.path.join(self.test_dir, f))
                except Exception:
                    pass
            try:
                os.rmdir(self.test_dir)
            except Exception:
                pass

    def test_parse_flexible_date(self):
        self.assertEqual(parse_flexible_date("11/14/2026"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("2026-11-14"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("14-Nov-26"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date("2026-11-14 23:59:59"), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date(datetime(2026, 11, 14, 10, 0, 0)), date(2026, 11, 14))
        self.assertEqual(parse_flexible_date(date(2026, 11, 14)), date(2026, 11, 14))
        # Summary row and invalid values
        self.assertIsNone(parse_flexible_date("Totals"))
        self.assertIsNone(parse_flexible_date("Total"))
        self.assertIsNone(parse_flexible_date(None))
        self.assertIsNone(parse_flexible_date(""))

    def test_verify_file_integrity(self):
        # 1. Non-existent file
        err = verify_file_integrity(os.path.join(self.test_dir, "missing.xlsx"))
        self.assertIn("does not exist", err)

        # 2. File too small (< 3KB)
        small_f = os.path.join(self.test_dir, "small.xlsx")
        with open(small_f, "wb") as f:
            f.write(b"tiny")
        err = verify_file_integrity(small_f)
        self.assertIn("too small", err)

        # 3. Not a zip file (>= 3KB)
        corrupt_f = os.path.join(self.test_dir, "corrupt.xlsx")
        with open(corrupt_f, "wb") as f:
            f.write(b"A" * 4000)
        err = verify_file_integrity(corrupt_f)
        self.assertIn("not a valid ZIP", err)

        # 4. Valid Excel file (created via openpyxl/pandas >= 5KB)
        valid_f = os.path.join(self.test_dir, "valid.xlsx")
        df = pd.DataFrame({"A": range(500), "B": ["sample text string"] * 500})
        df.to_excel(valid_f, index=False)
        self.assertIsNone(verify_file_integrity(valid_f))

    def test_validate_in_depth_sheet_revenue_header_1_and_totals(self):
        # Generate a dummy Revenue sheet with 2-line header + Totals row
        f = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        # Line 0 is super-header, Line 1 is column names
        data = [
            ["General Information", None, "Financials", None],
            ["Facility", "Date", "Theoretical Revenue", "Gross Revenue"],
            ["Mall 1", "11/14/2026", 100.0, 115.0],
            ["Mall 1", "11/14/2026", 200.0, 230.0],
            ["Totals", "Totals", 300.0, 345.0],
        ]
        df_raw = pd.DataFrame(data)
        df_raw.to_excel(f, header=False, index=False)

        res = validate_in_depth_sheet(f, REPORT_SPECS["revenue"], {date(2026, 11, 14)})
        self.assertTrue(res.is_valid, f"Errors: {res.errors}")
        self.assertEqual(res.row_count, 2)  # Totals excluded
        self.assertEqual(res.dates_found, {date(2026, 11, 14)})

    def test_validate_parking_transactions_detects_scientific_notation(self):
        f = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        # Ensure file size is >= 5KB with dummy rows
        df = pd.DataFrame({
            "Transaction Date/Time": ["2026-11-14 12:00:00"] * 200,
            "Entry Date/Time": ["2026-11-14 10:00:00"] * 200,
            "Exit Date/Time": ["2026-11-14 12:00:00"] * 200,
            "Serial Number": ["1234567890123456"] * 199 + ["2.02611E+22"],  # Corrupted barcode!
        })
        df.to_excel(f, index=False)

        res = validate_parking_sheet(f, REPORT_SPECS["parking"], {date(2026, 11, 14)})
        self.assertFalse(res.is_valid)
        self.assertTrue(any("scientific notation" in err for err in res.errors))

    def test_validate_exports_missing_file_fails(self):
        # Empty test directory missing all 4 files
        summary = validate_exports(exports_dir=self.test_dir, expected_date=date(2026, 11, 14), strict=False)
        self.assertFalse(summary.is_valid)
        self.assertGreaterEqual(len(summary.global_errors), 4)

    def test_validate_exports_all_four_files_valid(self):
        # Create all 4 valid files for date 2026-11-14
        exp_d = date(2026, 11, 14)
        date_str = "11/14/2026"

        # 1. Revenue
        f_rev = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        data_rev = [
            ["Info", None, "Fin", None],
            ["Facility", "Date", "Theoretical Revenue", "Gross Revenue"],
        ] + [["Mall", date_str, 10.0, 11.5]] * 250
        pd.DataFrame(data_rev).to_excel(f_rev, header=False, index=False)

        # 2. Access
        f_acc = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        df_acc = pd.DataFrame({
            "Facility": ["Mall"] * 250,
            "Date": [date_str] * 250,
            "Counting Category": ["Short Term"] * 250,
        })
        df_acc.to_excel(f_acc, index=False)

        # 3. System Event
        f_sys = os.path.join(self.test_dir, "14-Nov-26-System-Event-In-Depth.xlsx")
        df_sys = pd.DataFrame({
            "Facility": ["Mall"] * 250,
            "Date": [date_str] * 250,
            "System Event": ["Barrier Opened"] * 250,
        })
        df_sys.to_excel(f_sys, index=False)

        # 4. Parking
        f_prk = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        df_prk = pd.DataFrame({
            "Transaction Date/Time": ["2026-11-14 12:00:00"] * 250,
            "Entry Date/Time": ["2026-11-14 10:00:00"] * 250,
            "Exit Date/Time": ["2026-11-14 12:00:00"] * 250,
            "Serial Number": ["20261114000123456789"] * 250,
        })
        df_prk.to_excel(f_prk, index=False)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=exp_d, expected_days=1, strict=True)
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.sheet_results), 4)


if __name__ == "__main__":
    unittest.main()
