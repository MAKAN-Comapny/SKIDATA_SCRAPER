"""
tests/test_rolling_15days_advanced.py

Advanced, rigorous test suite for 15-Days Rolling Window pipeline.
Tests day-by-day parking isolation, 15-file XML streaming merge,
contiguous date coverage, granular gap detection, threshold limits,
barcode preservation across thousands of records, and multi-day in-depth reports.
"""

from datetime import date, datetime, timedelta
import os
import shutil
import tempfile
import unittest

import openpyxl
import pandas as pd

from excel_merge import merge_parking_xlsx
from validator import (
    REPORT_SPECS,
    get_expected_yesterday,
    validate_exports,
    validate_in_depth_sheet,
    validate_parking_sheet,
)


class TestRolling15DaysAdvanced(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_rolling_15d_")
        self.ref_date = date(2026, 11, 14)  # Fixed reference anchor
        self.window_15 = [self.ref_date - timedelta(days=i) for i in range(15)]
        self.window_15.sort()  # Ascending from 2026-10-31 to 2026-11-14

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_single_day_parking_file(
        self,
        filepath: str,
        target_date: date,
        num_rows: int = 5,
        start_serial: int = 1000,
        corrupt_barcode: bool = False,
    ):
        wb = openpyxl.Workbook()
        ws = wb.active
        headers = [
            "Transaction Date/Time",
            "Entry Date/Time",
            "Exit Date/Time",
            "Serial Number",
            "Receipt Number",
            "Payment",
            "Article",
            "Car Park",
            "Duration",
            "Access ID",
        ]
        ws.append(headers)
        d_str = target_date.strftime("%Y-%m-%d")
        for i in range(num_rows):
            ts_str = f"{d_str} {10 + (i % 10):02d}:{i % 60:02d}:00"
            entry_str = f"{d_str} 08:00:00"
            serial_val = (
                "2.02611E+22"
                if (corrupt_barcode and i == 0)
                else f"2026111400010000000{start_serial + i}"
            )
            ws.append([
                ts_str,
                entry_str,
                ts_str,
                serial_val,
                f"REC-{target_date.strftime('%m%d')}-{i:04d}",
                15.00 + i,
                "Short Term Parking",
                "View Mall",
                "02:00:00",
                101 + i,
            ])
        wb.save(filepath)

    def _create_multiday_in_depth(
        self,
        category: str,
        filepath: str,
        active_dates: list[date],
        rows_per_day: int = 2,
    ):
        wb = openpyxl.Workbook()
        ws = wb.active
        if category == "revenue":
            ws.append(["Category Summary", "Category Summary", "Category Summary", "Category Summary"])
            ws.append(["Date", "Theoretical Revenue", "Gross Revenue", "Facility"])
            for d in active_dates:
                d_str = d.strftime("%Y-%m-%d")
                for _ in range(rows_per_day):
                    ws.append([d_str, 500.00, 450.00, "View Mall"])
            ws.append(["Totals", len(active_dates) * rows_per_day * 500.00, len(active_dates) * rows_per_day * 450.00, "Totals"])
        elif category == "access":
            ws.append(["Date", "Counting Category", "Entries", "Exits"])
            for d in active_dates:
                d_str = d.strftime("%Y-%m-%d")
                for _ in range(rows_per_day):
                    ws.append([d_str, "Standard", 150, 140])
        elif category == "system_event":
            ws.append(["Date", "System Event", "Device", "Count"])
            for d in active_dates:
                d_str = d.strftime("%Y-%m-%d")
                for _ in range(rows_per_day):
                    ws.append([d_str, "Barrier Open", "Exit Gate 01", 120])
        wb.save(filepath)

    # 1. Rolling Window Date Generation: exactly 15 contiguous days
    def test_rolling_15_days_window_calendar_math(self):
        self.assertEqual(len(self.window_15), 15)
        self.assertEqual(self.window_15[0], date(2026, 10, 31))
        self.assertEqual(self.window_15[-1], date(2026, 11, 14))
        # Verify contiguous: difference between adjacent days is exactly 1
        for i in range(len(self.window_15) - 1):
            diff = (self.window_15[i + 1] - self.window_15[i]).days
            self.assertEqual(diff, 1)

    # 2. Month-crossing transition: Oct 31 to Nov 1 across 15-day boundary
    def test_rolling_month_crossing_boundary(self):
        oct_days = [d for d in self.window_15 if d.month == 10]
        nov_days = [d for d in self.window_15 if d.month == 11]
        self.assertEqual(len(oct_days), 1)  # Oct 31
        self.assertEqual(len(nov_days), 14)  # Nov 1 to Nov 14

    # 3. Year-crossing transition: Dec 20 to Jan 3 (15 days across New Year)
    def test_rolling_year_crossing_boundary(self):
        ny_anchor = date(2027, 1, 3)
        window = [ny_anchor - timedelta(days=i) for i in range(15)]
        window.sort()
        self.assertEqual(window[0], date(2026, 12, 20))
        self.assertEqual(window[-1], date(2027, 1, 3))
        self.assertEqual(len(window), 15)

    # 4. Day-by-Day Parking Scraping & 15-File Streaming Merge: Happy Path
    def test_day_by_day_parking_15_files_merge_happy_path(self):
        daily_files = []
        expected_total_rows = 0

        for idx, d in enumerate(self.window_15):
            fname = os.path.join(self.test_dir, f"temp_parking_day_{idx:02d}_{d.strftime('%Y%m%d')}.xlsx")
            rows = 4  # 4 rows per day
            self._create_single_day_parking_file(fname, d, num_rows=rows, start_serial=idx * 100)
            daily_files.append(fname)
            expected_total_rows += rows

        merged_output = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        total_merged = merge_parking_xlsx(daily_files, merged_output)
        self.assertEqual(total_merged, expected_total_rows)

        # Validate with validator.py
        res = validate_parking_sheet(merged_output, REPORT_SPECS["parking"], set(self.window_15))
        self.assertTrue(res.is_valid)
        self.assertEqual(res.row_count, expected_total_rows)
        self.assertEqual(len(res.dates_found), 15)
        self.assertEqual(len(res.missing_days), 0)

    # 5. Barcode Scientific Notation Immunity Across 15 Merged Days
    def test_barcode_scientific_notation_immunity_across_15_files(self):
        daily_files = []
        for idx, d in enumerate(self.window_15):
            fname = os.path.join(self.test_dir, f"temp_parking_{idx}.xlsx")
            # Write 23-digit string barcodes
            self._create_single_day_parking_file(fname, d, num_rows=2, start_serial=idx * 50)
            daily_files.append(fname)

        merged_output = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        merge_parking_xlsx(daily_files, merged_output)

        # Inspect resulting Excel directly
        df = pd.read_excel(merged_output, dtype={"Serial Number": str})
        for serial in df["Serial Number"].dropna():
            self.assertFalse("E+" in serial or "e+" in serial)
            self.assertTrue(len(serial) >= 20)

    # 6. Corrupt Barcode in Day 7 of 15 Caught by Validation
    def test_corrupt_barcode_in_day_7_caught_in_merged_result(self):
        daily_files = []
        for idx, d in enumerate(self.window_15):
            fname = os.path.join(self.test_dir, f"temp_parking_{idx}.xlsx")
            is_corrupt = (idx == 6)  # Day 7 has corrupted barcode
            self._create_single_day_parking_file(fname, d, num_rows=2, corrupt_barcode=is_corrupt)
            daily_files.append(fname)

        merged_output = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        merge_parking_xlsx(daily_files, merged_output)

        res = validate_parking_sheet(merged_output, REPORT_SPECS["parking"], set(self.window_15))
        self.assertFalse(res.is_valid)
        self.assertTrue(any("corrupted to scientific notation" in e for e in res.errors))

    # 7. Complete 15-Day Pipeline (All 4 Reports) Passes Validation
    def test_complete_15_day_pipeline_all_four_reports_passes(self):
        # 1. Merged Parking
        daily_files = []
        for idx, d in enumerate(self.window_15):
            fname = os.path.join(self.test_dir, f"temp_prk_{idx}.xlsx")
            self._create_single_day_parking_file(fname, d, num_rows=2)
            daily_files.append(fname)
        prk_path = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        merge_parking_xlsx(daily_files, prk_path)

        # 2, 3, 4. In-Depth Reports
        self._create_multiday_in_depth("revenue", os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx"), self.window_15)
        self._create_multiday_in_depth("access", os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx"), self.window_15)
        self._create_multiday_in_depth("system_event", os.path.join(self.test_dir, "14-Nov-26-System-Event-In-Depth.xlsx"), self.window_15)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=self.ref_date, expected_days=15, strict=True)
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.sheet_results), 4)
        for cat, r in summary.sheet_results.items():
            self.assertEqual(len(r.missing_days), 0)

    # 8. Gap Analysis: Oldest Day Missing (Day 15 missing = Oct 31)
    def test_gap_oldest_day_missing_warns_with_exact_date(self):
        dates_with_oldest_missing = self.window_15[1:]  # Nov 1 to Nov 14 (Oct 31 omitted)
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        self._create_multiday_in_depth("access", fpath, dates_with_oldest_missing)

        res = validate_in_depth_sheet(fpath, REPORT_SPECS["access"], set(self.window_15))
        self.assertTrue(res.is_valid)  # 1 gap < 50% is a warning
        self.assertEqual(res.missing_days, {date(2026, 10, 31)})
        self.assertTrue(any("Missing 1 day(s)" in w for w in res.warnings))

    # 9. Gap Analysis: Yesterday Missing (Day 1 missing = Nov 14)
    def test_gap_yesterday_missing_warns_with_exact_date(self):
        dates_with_yesterday_missing = self.window_15[:-1]  # Oct 31 to Nov 13 (Nov 14 omitted)
        fpath = os.path.join(self.test_dir, "14-Nov-26-System-Event-In-Depth.xlsx")
        self._create_multiday_in_depth("system_event", fpath, dates_with_yesterday_missing)

        res = validate_in_depth_sheet(fpath, REPORT_SPECS["system_event"], set(self.window_15))
        self.assertTrue(res.is_valid)
        self.assertEqual(res.missing_days, {date(2026, 11, 14)})

    # 10. Gap Analysis: Mid-Window Missing (Day 8 missing = Nov 7)
    def test_gap_mid_window_missing_warns(self):
        day_8 = self.window_15[7]
        dates_with_mid_missing = [d for d in self.window_15 if d != day_8]
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        self._create_multiday_in_depth("revenue", fpath, dates_with_mid_missing)

        res = validate_in_depth_sheet(fpath, REPORT_SPECS["revenue"], set(self.window_15))
        self.assertTrue(res.is_valid)
        self.assertEqual(res.missing_days, {day_8})

    # 11. Gap Analysis: Multiple scattered gaps (3 non-consecutive days)
    def test_multiple_scattered_gaps_tracked_accurately(self):
        omitted = {self.window_15[2], self.window_15[7], self.window_15[11]}
        active_dates = [d for d in self.window_15 if d not in omitted]
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        self._create_multiday_in_depth("access", fpath, active_dates)

        res = validate_in_depth_sheet(fpath, REPORT_SPECS["access"], set(self.window_15))
        self.assertTrue(res.is_valid)
        self.assertEqual(res.missing_days, omitted)

    # 12. Critical Threshold: More than 50% missing (> 7 days out of 15) FAILS
    def test_gap_over_50_percent_triggers_critical_failure(self):
        # Keep only 6 days (9 missing > 15//2 = 7)
        active_6_days = self.window_15[:6]

        self._create_multiday_in_depth("revenue", os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx"), active_6_days)
        self._create_multiday_in_depth("access", os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx"), active_6_days)
        self._create_multiday_in_depth("system_event", os.path.join(self.test_dir, "14-Nov-26-System-Event-In-Depth.xlsx"), active_6_days)

        daily_files = []
        for idx, d in enumerate(active_6_days):
            fname = os.path.join(self.test_dir, f"temp_prk_{idx}.xlsx")
            self._create_single_day_parking_file(fname, d, num_rows=1)
            daily_files.append(fname)
        prk_path = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        merge_parking_xlsx(daily_files, prk_path)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=self.ref_date, expected_days=15, strict=False)
        self.assertFalse(summary.is_valid)
        for cat, r in summary.sheet_results.items():
            self.assertFalse(r.is_valid)
            self.assertTrue(any("Critical data gap" in e for e in r.errors))

    # 13. Future Date Injection in Multi-Day Fails
    def test_future_date_injection_in_multiday_fails(self):
        future_date = date(2026, 11, 20)
        dates_with_future = list(self.window_15) + [future_date]
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        self._create_multiday_in_depth("access", fpath, dates_with_future)

        res = validate_in_depth_sheet(fpath, REPORT_SPECS["access"], set(self.window_15))
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Fatal: Found future dates" in e for e in res.errors))

    # 14. Merging with Varying Column Orders Aligns Schema
    def test_merging_varying_column_orders_aligns_schema(self):
        f1 = os.path.join(self.test_dir, "file1.xlsx")
        wb1 = openpyxl.Workbook()
        ws1 = wb1.active
        ws1.append(["Transaction Date/Time", "Serial Number", "Payment"])
        ws1.append(["2026-11-14 10:00:00", "202611140001", 10.0])
        wb1.save(f1)

        f2 = os.path.join(self.test_dir, "file2.xlsx")
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        # Swapped order: Payment before Serial Number
        ws2.append(["Transaction Date/Time", "Payment", "Serial Number"])
        ws2.append(["2026-11-13 11:00:00", 25.0, "202611130002"])
        wb2.save(f2)

        out = os.path.join(self.test_dir, "merged.xlsx")
        merge_parking_xlsx([f1, f2], out)

        df = pd.read_excel(out, dtype={"Serial Number": str})
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), ["Transaction Date/Time", "Serial Number", "Payment"])
        self.assertEqual(df.iloc[1]["Serial Number"], "202611130002")
        self.assertEqual(df.iloc[1]["Payment"], 25.0)

    # 15. Atomic Replacement: Merge Failure Leaves No Damaged File
    def test_atomic_replacement_failure_cleans_up_temp_file(self):
        good_file = os.path.join(self.test_dir, "good.xlsx")
        self._create_single_day_parking_file(good_file, self.ref_date, num_rows=2)

        corrupt_file = os.path.join(self.test_dir, "bad.xlsx")
        with open(corrupt_file, "wb") as f:
            f.write(b"NOT A REAL ZIP")

        out = os.path.join(self.test_dir, "atomic_test.xlsx")
        with self.assertRaises(Exception):
            merge_parking_xlsx([good_file, corrupt_file], out)

        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(f"{out}.tmp.xlsx"))

    # 16. Date Prefix Nomenclature: Multi-Day Range vs Single Day
    def test_get_report_file_prefix_nomenclature(self):
        from scraper import get_report_file_prefix
        target = date(2026, 11, 16)

        # Single day
        self.assertEqual(get_report_file_prefix(target, days_count=1), "16-Nov-26")
        self.assertEqual(get_report_file_prefix(target, days_count=0), "16-Nov-26")

        # 15 days rolling range ending on Nov 16
        self.assertEqual(get_report_file_prefix(target, days_count=15), "02-Nov-26-to-16-Nov-26")

        # 5 days rolling range ending on Nov 16
        self.assertEqual(get_report_file_prefix(target, days_count=5), "12-Nov-26-to-16-Nov-26")

    # 17. Filename Prefix Extraction & Consistency
    def test_extract_file_prefix_and_multi_day_validation(self):
        from validator import extract_file_prefix, validate_exports
        from scraper import get_report_file_prefix

        end_d = date(2026, 11, 16)
        prefix = get_report_file_prefix(end_d, days_count=15)
        self.assertEqual(prefix, "02-Nov-26-to-16-Nov-26")

        # Verify extract_file_prefix
        self.assertEqual(
            extract_file_prefix(f"{prefix}-Parking-Transactions.xlsx"),
            "02-Nov-26-to-16-Nov-26",
        )
        self.assertEqual(
            extract_file_prefix("16-Nov-26-Parking-Transactions.xlsx"),
            "16-Nov-26",
        )
        self.assertEqual(
            extract_file_prefix(f"{prefix}-Revenue-In-Depth.xlsx"),
            "02-Nov-26-to-16-Nov-26",
        )

        # Create 4 files with range nomenclature
        for sheet in ["Parking-Transactions", "Revenue-In-Depth", "Access-In-Depth", "System-Event-In-Depth"]:
            wb = openpyxl.Workbook()
            ws = wb.active
            if sheet == "Parking-Transactions":
                ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number", "Payment"])
                for d in [end_d - timedelta(days=i) for i in range(15)]:
                    ws.append([
                        f"{d.strftime('%Y-%m-%d')} 10:00:00",
                        f"{d.strftime('%Y-%m-%d')} 09:00:00",
                        f"{d.strftime('%Y-%m-%d')} 10:00:00",
                        f"TKT-{d.day}",
                        10.0,
                    ])
            elif sheet == "Revenue-In-Depth":
                ws.append(["Title", "Header"])
                ws.append(["Date", "Gross", "Net"])
                for d in [end_d - timedelta(days=i) for i in range(15)]:
                    ws.append([d.strftime("%Y-%m-%d"), 100.0, 85.0])
            elif sheet == "Access-In-Depth":
                ws.append(["Date", "Counting Category"])
                for d in [end_d - timedelta(days=i) for i in range(15)]:
                    ws.append([d.strftime("%Y-%m-%d"), "Short Stay"])
            else:
                ws.append(["Date", "System Event"])
                for d in [end_d - timedelta(days=i) for i in range(15)]:
                    ws.append([d.strftime("%Y-%m-%d"), "Door Open"])
            wb.save(os.path.join(self.test_dir, f"{prefix}-{sheet}.xlsx"))

        summary = validate_exports(
            exports_dir=self.test_dir,
            expected_date=end_d,
            expected_days=15,
            strict=False,
        )
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.global_errors), 0)
        # Verify no inconsistent prefix warning
        self.assertFalse(any("Inconsistent filename date prefixes" in w for w in summary.global_warnings))


if __name__ == "__main__":
    unittest.main()
