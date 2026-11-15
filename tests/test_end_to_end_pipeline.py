"""
End-to-End Integration and Stress Test Suite for the SKIDATA Scraper Pipeline.

Tests the full lifecycle across both Daily and 15-Day Rolling pipelines:
  - Demo dataset generation
  - Validator checks across 10 critical scenarios
  - Excel merger for 15 isolated daily files
  - Transformer cleanup and Totals removal
  - Odoo invoice transformation
  - AST Timezone calculations
"""

from datetime import date, datetime, timedelta, timezone
import glob
import os
import shutil
import sys
import unittest
import zipfile

import openpyxl
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from validator import (
    validate_exports,
    get_expected_yesterday,
    get_ast_now,
    AST_TIMEZONE,
)
from excel_merge import merge_parking_xlsx
import Transformer
from odoo_transformer import transform_parking_file


class TestEndToEndPipeline(unittest.TestCase):
    def setUp(self):
        self.test_exports_dir = os.path.join(BASE_DIR, "tests", "e2e_temp_exports")
        self.test_cleaned_dir = os.path.join(BASE_DIR, "tests", "e2e_temp_cleaned")
        self.test_odoo_dir = os.path.join(BASE_DIR, "tests", "e2e_temp_odoo")

        # Clean any old temp directories
        for d in (self.test_exports_dir, self.test_cleaned_dir, self.test_odoo_dir):
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)

        # Patch Transformer module paths for test sandbox
        self._orig_exp = Transformer.EXPORTS_DIR
        self._orig_cln = Transformer.CLEANED_EXPORTS_DIR
        Transformer.EXPORTS_DIR = self.test_exports_dir
        Transformer.CLEANED_EXPORTS_DIR = self.test_cleaned_dir

    def tearDown(self):
        Transformer.EXPORTS_DIR = self._orig_exp
        Transformer.CLEANED_EXPORTS_DIR = self._orig_cln
        for d in (self.test_exports_dir, self.test_cleaned_dir, self.test_odoo_dir):
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)

    def _generate_demo_dataset(self, target_dates: list[date], corrupt_barcode: bool = False, include_totals: bool = True):
        """Generates realistic demo files matching SKIDATA Qlik Sense export schemas."""
        date_prefix = target_dates[-1].strftime("%d-%b-%y")

        # 1. Revenue In Depth (2-line header + Totals)
        rev_path = os.path.join(self.test_exports_dir, f"{date_prefix}-Revenue-In-Depth.xlsx")
        rev_rows = [
            ["General Information", None, None, "Financial Summary", None],
            ["Facility", "Car Park", "Date", "Theoretical Revenue", "Gross Revenue"],
        ]
        for d in target_dates:
            d_str = d.strftime("%m/%d/%Y")
            for i in range(20):
                rev_rows.append([f"Facility {i%3}", f"CarPark {i%2}", d_str, 50.0 + i, 57.5 + i])
        if include_totals:
            rev_rows.append(["Totals", "Totals", "Totals", 1000.0, 1150.0])

        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rev_rows:
            ws.append(row)
        wb.save(rev_path)

        # 2. Access In Depth (header=0 + Totals)
        acc_path = os.path.join(self.test_exports_dir, f"{date_prefix}-Access-In-Depth.xlsx")
        acc_rows = [["Facility", "Car Park", "Counting Category", "Date", "Quantity"]]
        for d in target_dates:
            d_str = d.strftime("%m/%d/%Y")
            for i in range(20):
                acc_rows.append([f"Facility {i%3}", f"CarPark {i%2}", "Short Term", d_str, 1])
        if include_totals:
            acc_rows.append(["Totals", "Totals", "Totals", "Totals", 400])

        wb = openpyxl.Workbook()
        ws = wb.active
        for row in acc_rows:
            ws.append(row)
        wb.save(acc_path)

        # 3. System Event In Depth (header=0 + Totals)
        sys_path = os.path.join(self.test_exports_dir, f"{date_prefix}-System-Event-In-Depth.xlsx")
        sys_rows = [["Facility", "Car Park", "Device", "System Event", "Date", "Information"]]
        for d in target_dates:
            d_str = d.strftime("%m/%d/%Y")
            for i in range(20):
                sys_rows.append([f"Facility {i%3}", f"CarPark {i%2}", "Lane 1", "Barrier Open", d_str, "Normal pass"])
        if include_totals:
            sys_rows.append(["Totals", "Totals", "Totals", "Totals", "Totals", "Totals"])

        wb = openpyxl.Workbook()
        ws = wb.active
        for row in sys_rows:
            ws.append(row)
        wb.save(sys_path)

        # 4. Parking Transactions (header=0)
        prk_path = os.path.join(self.test_exports_dir, f"{date_prefix}-Parking-Transactions.xlsx")
        prk_rows = [[
            "Facility", "Car Park", "Entry Device", "Exit Device",
            "Entry Date/Time", "Exit Date/Time", "Transaction Date/Time",
            "Duration", "Ticket Media Type", "Serial Number", "Article",
            "Payment", "Discount Amount", "Receipt Number",
            "Entry Plate Number", "Exit Plate Number"
        ]]
        for d in target_dates:
            for i in range(25):
                entry_dt = f"{d} 08:{i:02d}:00"
                exit_dt = f"{d} 10:{i:02d}:00"
                txn_dt = f"{d} 10:{i:02d}:15"
                barcode = f"202611140001234567890{i:02d}" if not (corrupt_barcode and i == 0) else "2.02611E+22"
                payment = 15.0 if i % 2 == 0 else 0.0  # Mix of paid and free parking
                prk_rows.append([
                    "View Mall", "Main Car Park", "Entry Gate 1", "Exit Gate 1",
                    entry_dt, exit_dt, txn_dt,
                    "02:00:15", "Barcode Ticket", barcode, "Short Term Parking",
                    payment, 0.0, f"REC-{i:04d}",
                    f"ABC-{i:03d}", f"ABC-{i:03d}"
                ])

        wb = openpyxl.Workbook()
        ws = wb.active
        for row in prk_rows:
            ws.append(row)
        wb.save(prk_path)

        return rev_path, acc_path, sys_path, prk_path

    # =========================================================================
    # Test Case 1: Daily Scraper Happy Path (End-to-End Pipeline)
    # =========================================================================
    def test_daily_pipeline_happy_path(self):
        yesterday = date(2026, 11, 14)
        self._generate_demo_dataset([yesterday])

        # Step 1.5: Validate downloaded exports
        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=yesterday,
            expected_days=1,
            strict=True,
        )
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.sheet_results), 4)
        for cat, res in summary.sheet_results.items():
            self.assertTrue(res.is_valid, f"{cat} failed: {res.errors}")
            self.assertGreater(res.row_count, 0)

        # Step 2: Transform exports (clean Totals, move to Cleaned-Exports)
        Transformer.run()

        # Verify exports/ is now empty and Cleaned-Exports/ contains all 4 files
        self.assertEqual(len(glob.glob(os.path.join(self.test_exports_dir, "*.xlsx"))), 0)
        cleaned_files = glob.glob(os.path.join(self.test_cleaned_dir, "*.xlsx"))
        self.assertEqual(len(cleaned_files), 4)

        # Verify Totals row was completely stripped by Transformer
        for cf in cleaned_files:
            if "Parking" not in cf:
                df = pd.read_excel(cf)
                self.assertNotIn("Totals", df["Date"].astype(str).values)

        # Step 3: Odoo transformation
        parking_cleaned = [f for f in cleaned_files if "Parking-Transactions" in f][0]
        odoo_out = os.path.join(self.test_odoo_dir, "14-Nov-26-Parking-Transactions-Odoo.xlsx")
        before_rows, after_rows = transform_parking_file(parking_cleaned, odoo_out)

        self.assertGreater(before_rows, after_rows)
        self.assertGreater(after_rows, 0)
        self.assertTrue(os.path.exists(odoo_out))

        # Check Odoo file schema
        df_odoo = pd.read_excel(odoo_out)
        self.assertIn("Invoice Lines/Unit Price", df_odoo.columns)
        self.assertIn("Ticket Sequence", df_odoo.columns)
        self.assertTrue((df_odoo["Invoice Lines/Unit Price"] > 0).all())

    # =========================================================================
    # Test Case 2: 15-Day Rolling Scraper Happy Path (Merge + Validation + Transform)
    # =========================================================================
    def test_rolling_15_days_pipeline_happy_path(self):
        end_date = date(2026, 11, 14)
        rolling_15_days = [end_date - timedelta(days=i) for i in reversed(range(15))]
        self.assertEqual(len(rolling_15_days), 15)

        # 1. Simulate day-by-day Parking Transactions export (15 individual files)
        temp_parking_files = []
        for idx, d in enumerate(rolling_15_days):
            day_file = os.path.join(self.test_exports_dir, f"temp_parking_day_{idx}.xlsx")
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append([
                "Facility", "Car Park", "Entry Device", "Exit Device",
                "Entry Date/Time", "Exit Date/Time", "Transaction Date/Time",
                "Duration", "Ticket Media Type", "Serial Number",
                "Payment", "Discount Amount", "Receipt Number",
                "Entry Plate Number", "Exit Plate Number"
            ])
            for i in range(10):
                ws.append([
                    "View Mall", "Main Car Park", "Entry Gate 1", "Exit Gate 1",
                    f"{d} 09:00:00", f"{d} 11:00:00", f"{d} 11:05:00",
                    "02:05:00", "Barcode Ticket", f"202611000000{idx:02d}{i:04d}",
                    20.0, 0.0, f"R-{idx}-{i}", "ABC-123", "ABC-123"
                ])
            wb.save(day_file)
            temp_parking_files.append(day_file)

        # Merge the 15 daily files
        date_prefix = end_date.strftime("%d-%b-%y")
        merged_parking_path = os.path.join(self.test_exports_dir, f"{date_prefix}-Parking-Transactions.xlsx")
        total_rows = merge_parking_xlsx(temp_parking_files, merged_parking_path)
        self.assertEqual(total_rows, 150)
        for tf in temp_parking_files:
            os.remove(tf)

        # 2. Generate 15-day multi-day Revenue, Access, System Event
        self._generate_demo_dataset(rolling_15_days)

        # 3. Validate the 15-day exports
        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=end_date,
            expected_days=15,
            strict=True,
        )
        self.assertTrue(summary.is_valid)
        for cat, res in summary.sheet_results.items():
            self.assertEqual(len(res.missing_days), 0, f"{cat} has missing days: {res.missing_days}")
            self.assertEqual(len(res.dates_found), 15)

    # =========================================================================
    # Test Case 3: Stale / Wrong Date in Export (ETL Sync Lag)
    # =========================================================================
    def test_stale_wrong_date_fails_validation(self):
        # Scraper exported Nov 13, but pipeline expected yesterday (Nov 14)
        wrong_date = date(2026, 11, 13)
        self._generate_demo_dataset([wrong_date])

        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=date(2026, 11, 14),
            expected_days=1,
            strict=False,
        )
        self.assertFalse(summary.is_valid)
        self.assertGreater(len(summary.sheet_results["revenue"].errors), 0)
        self.assertIn("does not contain expected date 2026-11-14", summary.sheet_results["revenue"].errors[0])

    # =========================================================================
    # Test Case 4: Missing Day Gap in 15-Day Rolling Range
    # =========================================================================
    def test_missing_day_gap_detected_in_rolling_window(self):
        end_date = date(2026, 11, 14)
        rolling_days = [end_date - timedelta(days=i) for i in reversed(range(15))]
        # Intentionally remove day index 7 (2026-11-07)
        missing_target = rolling_days[7]
        gapped_days = [d for d in rolling_days if d != missing_target]
        self.assertEqual(len(gapped_days), 14)

        self._generate_demo_dataset(gapped_days)

        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=end_date,
            expected_days=15,
            strict=False,
        )
        for cat, res in summary.sheet_results.items():
            self.assertIn(missing_target, res.missing_days)
            self.assertTrue(any(f"Missing 1 day(s)" in w for w in res.warnings))

    # =========================================================================
    # Test Case 5: Barcode Scientific Notation Detection
    # =========================================================================
    def test_barcode_scientific_notation_corruption_caught(self):
        yesterday = date(2026, 11, 14)
        self._generate_demo_dataset([yesterday], corrupt_barcode=True)

        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=yesterday,
            expected_days=1,
            strict=False,
        )
        self.assertFalse(summary.is_valid)
        prk_res = summary.sheet_results["parking"]
        self.assertFalse(prk_res.is_valid)
        self.assertTrue(any("scientific notation" in err for err in prk_res.errors))

    # =========================================================================
    # Test Case 6: Long-Stay Parkers Handled Correctly
    # =========================================================================
    def test_long_stay_parker_entry_date_does_not_trigger_error(self):
        yesterday = date(2026, 11, 14)
        date_prefix = yesterday.strftime("%d-%b-%y")
        self._generate_demo_dataset([yesterday])

        # Overwrite Parking-Transactions to include long-stay parkers (Entry 20 days ago)
        prk_path = os.path.join(self.test_exports_dir, f"{date_prefix}-Parking-Transactions.xlsx")
        df = pd.read_excel(prk_path)
        # Modify first 10 rows: entry date was 20 days ago, transaction date was yesterday
        df.loc[:9, "Entry Date/Time"] = "2026-10-25 10:00:00"
        df.loc[:9, "Transaction Date/Time"] = f"{yesterday} 12:00:00"
        df.to_excel(prk_path, index=False)

        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=yesterday,
            expected_days=1,
            strict=True,
        )
        # Must pass because Transaction Date/Time is yesterday!
        self.assertTrue(summary.is_valid)

    # =========================================================================
    # Test Case 7: Missing Required File Detected
    # =========================================================================
    def test_missing_report_file_fails_validation(self):
        yesterday = date(2026, 11, 14)
        rev, acc, sys_path, prk = self._generate_demo_dataset([yesterday])
        # Delete Parking Transactions
        os.remove(prk)

        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=yesterday,
            expected_days=1,
            strict=False,
        )
        self.assertFalse(summary.is_valid)
        self.assertTrue(any("Parking-Transactions" in err for err in summary.global_errors))

    # =========================================================================
    # Test Case 8: Truncated / Corrupt File Detected
    # =========================================================================
    def test_corrupt_file_fails_validation(self):
        yesterday = date(2026, 11, 14)
        rev, acc, sys_path, prk = self._generate_demo_dataset([yesterday])
        # Corrupt Revenue In-Depth by writing truncated bytes (> 3KB so it passes size check)
        with open(rev, "wb") as f:
            f.write(b"PK\x03\x04" + b"corrupted_non_zip_data" * 300)

        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=yesterday,
            expected_days=1,
            strict=False,
        )
        self.assertFalse(summary.is_valid)
        rev_res = summary.sheet_results["revenue"]
        self.assertFalse(rev_res.is_valid)
        self.assertTrue(any("archive" in err.lower() or "zip" in err.lower() for err in rev_res.errors))

    # =========================================================================
    # Test Case 9: Zero-Payment Facility
    # =========================================================================
    def test_zero_payment_parking_handled_gracefully(self):
        yesterday = date(2026, 11, 14)
        date_prefix = yesterday.strftime("%d-%b-%y")
        self._generate_demo_dataset([yesterday])

        # Set all payments to 0.0
        prk_path = os.path.join(self.test_exports_dir, f"{date_prefix}-Parking-Transactions.xlsx")
        df = pd.read_excel(prk_path)
        df["Payment"] = 0.0
        df.to_excel(prk_path, index=False)

        # Raw file validation must pass
        summary = validate_exports(
            exports_dir=self.test_exports_dir,
            expected_date=yesterday,
            expected_days=1,
            strict=True,
        )
        self.assertTrue(summary.is_valid)

        # Odoo transformation should produce 0 rows without crashing
        odoo_out = os.path.join(self.test_odoo_dir, "odoo_zero.xlsx")
        before, after = transform_parking_file(prk_path, odoo_out)
        self.assertEqual(after, 0)
        self.assertTrue(os.path.exists(odoo_out))

    # =========================================================================
    # Test Case 10: Saudi Arabia AST Timezone Accuracy
    # =========================================================================
    def test_saudi_arabia_ast_timezone(self):
        now_ast = get_ast_now()
        yesterday_ast = get_expected_yesterday()
        self.assertEqual(now_ast.tzinfo, AST_TIMEZONE)
        self.assertEqual(yesterday_ast, (now_ast - timedelta(days=1)).date())
        # UTC offset must be +3 hours
        self.assertEqual(now_ast.utcoffset(), timedelta(hours=3))


if __name__ == "__main__":
    unittest.main()
