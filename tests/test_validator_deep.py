"""
Deep unit and integration test suite for validator.py.
Covers 32+ granular scenarios including sheet-by-sheet permutations,
schema variations, corruption modes, rolling window coverage, and gap thresholds.
"""

from datetime import date, datetime, timedelta
import os
import shutil
import tempfile
import unittest
import zipfile

import openpyxl
import pandas as pd

from validator import (
    REPORT_SPECS,
    SheetValidationResult,
    ValidationSummary,
    get_ast_now,
    get_expected_yesterday,
    parse_flexible_date,
    validate_exports,
    validate_in_depth_sheet,
    validate_parking_sheet,
    verify_file_integrity,
)


class TestValidatorDeep(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_val_deep_")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_excel(self, filepath: str, sheet_data: list[list], header_offset: int = 0):
        wb = openpyxl.Workbook()
        ws = wb.active
        for _ in range(header_offset):
            ws.append(["SUPER HEADER CATEGORY"] * len(sheet_data[0]))
        for row in sheet_data:
            ws.append(row)
        wb.save(filepath)

    # 1. In-Depth: Revenue with header=1 (normal Qlik Sense structure)
    def test_in_depth_sheet_valid_revenue_with_header_1(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Top Category Header", "Top Category Header", "Top Category Header"])
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
        ws.append(["2026-11-14", 500, 450])
        ws.append(["2026-11-14", 300, 300])
        ws.append(["Totals", 800, 750])  # Totals row should be stripped
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["revenue"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.row_count, 2)
        self.assertEqual(res.dates_found, {date(2026, 11, 14)})
        self.assertEqual(len(res.errors), 0)

    # 2. In-Depth: Revenue with header=0 fallback
    def test_in_depth_sheet_revenue_header_0_fallback(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
        ws.append(["2026-11-14", 500, 450])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["revenue"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.row_count, 1)

    # 3. In-Depth: Missing date column
    def test_in_depth_sheet_missing_date_column_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Col1", "Theoretical Revenue", "Gross Revenue"])
        ws.append(["2026-11-14", 500, 450])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["revenue"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Required date column 'Date' not found" in e for e in res.errors))

    # 4. In-Depth: Missing business column logs warning
    def test_in_depth_sheet_missing_min_cols_warns(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Gross Revenue"])  # Missing 'Theoretical Revenue'
        ws.append(["2026-11-14", 450])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["revenue"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)  # Warnings do not invalidate file
        self.assertTrue(any("Theoretical Revenue" in w for w in res.warnings))

    # 5. In-Depth: Zero data rows after totals stripped
    def test_in_depth_sheet_zero_rows_after_totals_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
        ws.append(["Totals", 0, 0])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["revenue"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("0 data rows" in e for e in res.errors))

    # 6. In-Depth: Future date fatal in multi-day
    def test_in_depth_sheet_future_date_fatal_in_multiday(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category"])
        ws.append(["2026-11-14", "Normal Entry"])
        ws.append(["2026-11-20", "Future Entry"])  # Future date relative to target window
        wb.save(fpath)

        expected_window = {date(2026, 11, 10), date(2026, 11, 14)}
        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["access"], expected_window
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Fatal: Found future dates" in e for e in res.errors))

    # 7. In-Depth: Single day wrong date fails
    def test_in_depth_sheet_single_day_wrong_date_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category"])
        ws.append(["2026-11-13", "Normal Entry"])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["access"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("does not contain expected date" in e for e in res.errors))

    # 8. In-Depth: Single day unexpected extra dates fails
    def test_in_depth_sheet_single_day_unexpected_dates_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category"])
        ws.append(["2026-11-14", "Entry A"])
        ws.append(["2026-11-12", "Entry B"])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["access"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("unexpected dates" in e for e in res.errors))

    # 9. In-Depth: Access In Depth validation
    def test_in_depth_sheet_access_in_depth_validation(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category", "Entries", "Exits"])
        ws.append(["2026-11-14", "Short Term", 150, 140])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["access"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)

    # 10. In-Depth: System Event validation
    def test_in_depth_sheet_system_event_validation(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-System-Event-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "System Event", "Count"])
        ws.append(["2026-11-14", "Barrier Open", 80])
        wb.save(fpath)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["system_event"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)

    # 11. In-Depth: Corrupt file reports integrity error
    def test_in_depth_sheet_corrupt_file_reports_error(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        with open(fpath, "wb") as f:
            f.write(b"NOT A ZIP FILE" + b"X" * 4000)

        res = validate_in_depth_sheet(
            fpath, REPORT_SPECS["revenue"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("not a valid ZIP/Excel archive" in e for e in res.errors))

    # 12. Parking: Valid data
    def test_parking_sheet_valid_data(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        ws.append(["2026-11-14 10:00:00", "2026-11-14 08:00:00", "2026-11-14 10:00:00", "202611140001"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.row_count, 1)

    # 13. Parking: Missing required column fails
    def test_parking_sheet_missing_required_column_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Entry Date/Time", "Exit Date/Time", "Serial Number"])  # Missing Transaction Date/Time
        ws.append(["2026-11-14 08:00:00", "2026-11-14 10:00:00", "202611140001"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Required parking column" in e for e in res.errors))

    # 14. Parking: Zero rows fails
    def test_parking_sheet_zero_rows_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("contains 0 rows" in e for e in res.errors))

    # 15. Parking: Single day wrong date fails
    def test_parking_sheet_single_day_wrong_date_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        ws.append(["2026-11-13 10:00:00", "2026-11-13 08:00:00", "2026-11-13 10:00:00", "202611140001"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("does not contain expected date" in e for e in res.errors))

    # 16. Parking: Future date fails
    def test_parking_sheet_future_date_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        ws.append(["2026-11-20 10:00:00", "2026-11-20 08:00:00", "2026-11-20 10:00:00", "202611140001"])
        wb.save(fpath)

        expected_window = {date(2026, 11, 13), date(2026, 11, 14)}
        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], expected_window
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("Fatal: Found future dates" in e for e in res.errors))

    # 17. Parking: Scientific notation barcode detected (uppercase E+)
    def test_parking_sheet_scientific_notation_barcode_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        ws.append(["2026-11-14 10:00:00", "2026-11-14 08:00:00", "2026-11-14 10:00:00", "2.02611E+22"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("corrupted to scientific notation" in e for e in res.errors))

    # 18. Parking: Scientific notation barcode detected (lowercase e+)
    def test_parking_sheet_lowercase_scientific_notation_fails(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        ws.append(["2026-11-14 10:00:00", "2026-11-14 08:00:00", "2026-11-14 10:00:00", "2.02611e+22"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("corrupted to scientific notation" in e for e in res.errors))

    # 19. Parking: Clean text 23-digit barcode passes
    def test_parking_sheet_clean_text_barcode_passes(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        ws.append(["2026-11-14 10:00:00", "2026-11-14 08:00:00", "2026-11-14 10:00:00", "20261114000100000000123"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)

    # 20. Parking: Long-stay parkers entry date ignored
    def test_parking_sheet_long_stay_parker_entry_date_ignored(self):
        fpath = os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        # Entered 30 days ago, exited yesterday
        ws.append(["2026-11-14 10:00:00", "2026-10-15 08:00:00", "2026-11-14 10:00:00", "202611140001"])
        wb.save(fpath)

        res = validate_parking_sheet(
            fpath, REPORT_SPECS["parking"], {date(2026, 11, 14)}
        )
        self.assertTrue(res.is_valid)

    # 21. Validate Exports: Non-existent exports dir fails
    def test_validate_exports_directory_not_found_strict(self):
        non_existent = os.path.join(self.test_dir, "no_such_folder")
        with self.assertRaises(RuntimeError):
            validate_exports(exports_dir=non_existent, strict=True)

    # 22. Validate Exports: Non-existent exports dir non-strict returns invalid
    def test_validate_exports_directory_not_found_non_strict(self):
        non_existent = os.path.join(self.test_dir, "no_such_folder")
        summary = validate_exports(exports_dir=non_existent, strict=False)
        self.assertFalse(summary.is_valid)
        self.assertTrue(any("does not exist" in e for e in summary.global_errors))

    # Helper to generate a full set of 4 valid daily files
    def _create_full_valid_suite(self, target_date: date):
        d_str = target_date.strftime("%Y-%m-%d")
        ts_str = f"{d_str} 12:00:00"

        # 1. Revenue
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Top Category", "Top Category", "Top Category"])
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
        for _ in range(5):
            ws.append([d_str, 500, 450])
        wb.save(os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-Revenue-In-Depth.xlsx"))

        # 2. Access
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category", "Entries"])
        for _ in range(5):
            ws.append([d_str, "Standard", 100])
        wb.save(os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-Access-In-Depth.xlsx"))

        # 3. System Event
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "System Event", "Count"])
        for _ in range(5):
            ws.append([d_str, "Gate Pulse", 50])
        wb.save(os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-System-Event-In-Depth.xlsx"))

        # 4. Parking
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])
        for i in range(5):
            ws.append([ts_str, ts_str, ts_str, f"202611140000000000000{i}"])
        wb.save(os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-Parking-Transactions.xlsx"))

    # 23. Validate Exports: All 4 files present and valid passes
    def test_validate_exports_all_four_files_present_passes(self):
        target = date(2026, 11, 14)
        self._create_full_valid_suite(target)
        summary = validate_exports(exports_dir=self.test_dir, expected_date=target, expected_days=1)
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.sheet_results), 4)

    # 24. Validate Exports: One file missing fails
    def test_validate_exports_one_file_missing_fails(self):
        target = date(2026, 11, 14)
        self._create_full_valid_suite(target)
        # Remove Parking-Transactions
        for f in os.listdir(self.test_dir):
            if "Parking-Transactions" in f:
                os.remove(os.path.join(self.test_dir, f))

        summary = validate_exports(exports_dir=self.test_dir, expected_date=target, expected_days=1, strict=False)
        self.assertFalse(summary.is_valid)
        self.assertTrue(any("Parking-Transactions" in e for e in summary.global_errors))

    # 25. Validate Exports: Multiple matching files selects newest
    def test_validate_exports_multiple_matches_newest_selected(self):
        target = date(2026, 11, 14)
        self._create_full_valid_suite(target)
        # Create an older duplicate
        older = os.path.join(self.test_dir, "01-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
        ws.append(["2026-11-01", 100, 100])
        wb.save(older)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=target, expected_days=1, strict=False)
        self.assertTrue(any("Multiple files matching '*Revenue-In-Depth.xlsx'" in w for w in summary.global_warnings))

    # 26. Validate Exports: Inconsistent date prefixes warns
    def test_validate_exports_inconsistent_date_prefixes_warns(self):
        target = date(2026, 11, 14)
        self._create_full_valid_suite(target)
        # Rename access to 13-Nov-26
        for f in os.listdir(self.test_dir):
            if "Access-In-Depth" in f:
                old = os.path.join(self.test_dir, f)
                new = os.path.join(self.test_dir, "13-Nov-26-Access-In-Depth.xlsx")
                os.rename(old, new)
                break

        summary = validate_exports(exports_dir=self.test_dir, expected_date=target, expected_days=1, strict=False)
        self.assertTrue(any("Inconsistent filename date prefixes" in w for w in summary.global_warnings))

    # 27. Rolling: 15 days complete passes
    def test_validate_exports_rolling_15_days_complete_passes(self):
        end_d = date(2026, 11, 14)
        days_15 = [end_d - timedelta(days=i) for i in range(15)]

        # Create multi-day files
        wb_rev = openpyxl.Workbook()
        ws_rev = wb_rev.active
        ws_rev.append(["Top", "Top", "Top"])
        ws_rev.append(["Date", "Theoretical Revenue", "Gross Revenue"])

        wb_acc = openpyxl.Workbook()
        ws_acc = wb_acc.active
        ws_acc.append(["Date", "Counting Category"])

        wb_sys = openpyxl.Workbook()
        ws_sys = wb_sys.active
        ws_sys.append(["Date", "System Event"])

        wb_prk = openpyxl.Workbook()
        ws_prk = wb_prk.active
        ws_prk.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time", "Serial Number"])

        for d in days_15:
            d_str = d.strftime("%Y-%m-%d")
            ts_str = f"{d_str} 12:00:00"
            ws_rev.append([d_str, 500, 450])
            ws_acc.append([d_str, "Standard"])
            ws_sys.append([d_str, "Alarm"])
            ws_prk.append([ts_str, ts_str, ts_str, "202611140001"])

        wb_rev.save(os.path.join(self.test_dir, "14-Nov-26-Revenue-In-Depth.xlsx"))
        wb_acc.save(os.path.join(self.test_dir, "14-Nov-26-Access-In-Depth.xlsx"))
        wb_sys.save(os.path.join(self.test_dir, "14-Nov-26-System-Event-In-Depth.xlsx"))
        wb_prk.save(os.path.join(self.test_dir, "14-Nov-26-Parking-Transactions.xlsx"))

        summary = validate_exports(exports_dir=self.test_dir, expected_date=end_d, expected_days=15, strict=True)
        self.assertTrue(summary.is_valid)

    # 28. Rolling: Gap under 50% logs warning but remains valid
    def test_validate_exports_rolling_gap_under_50_percent_warns(self):
        end_d = date(2026, 11, 14)
        days_15 = [end_d - timedelta(days=i) for i in range(15)]
        # Skip 2 days: Nov 5 and Nov 8
        days_with_gap = [d for d in days_15 if d not in (date(2026, 11, 5), date(2026, 11, 8))]

        for name, spec in REPORT_SPECS.items():
            wb = openpyxl.Workbook()
            ws = wb.active
            if name == "revenue":
                ws.append(["Top", "Top", "Top"])
                ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
                for d in days_with_gap:
                    ws.append([d.strftime("%Y-%m-%d"), 100, 100])
            elif name == "parking":
                ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time"])
                for d in days_with_gap:
                    ws.append([f"{d.strftime('%Y-%m-%d')} 12:00:00", f"{d.strftime('%Y-%m-%d')} 12:00:00", f"{d.strftime('%Y-%m-%d')} 12:00:00"])
            else:
                ws.append(["Date", spec["min_cols"][-1]])
                for d in days_with_gap:
                    ws.append([d.strftime("%Y-%m-%d"), "Val"])
            wb.save(os.path.join(self.test_dir, f"14-Nov-26-{spec['pattern'].replace('*', '')}"))

        summary = validate_exports(exports_dir=self.test_dir, expected_date=end_d, expected_days=15, strict=True)
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.sheet_results["revenue"].missing_days), 2)

    # 29. Rolling: Critical data gap (> 50% missing) fails
    def test_validate_exports_rolling_gap_over_50_percent_fails(self):
        end_d = date(2026, 11, 14)
        # Only provide 5 days out of 15 (10 days missing > 15//2 = 7)
        days_5 = [end_d - timedelta(days=i) for i in range(5)]

        for name, spec in REPORT_SPECS.items():
            wb = openpyxl.Workbook()
            ws = wb.active
            if name == "revenue":
                ws.append(["Top", "Top", "Top"])
                ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
                for d in days_5:
                    ws.append([d.strftime("%Y-%m-%d"), 100, 100])
            elif name == "parking":
                ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time"])
                for d in days_5:
                    ws.append([f"{d.strftime('%Y-%m-%d')} 12:00:00", f"{d.strftime('%Y-%m-%d')} 12:00:00", f"{d.strftime('%Y-%m-%d')} 12:00:00"])
            else:
                ws.append(["Date", spec["min_cols"][-1]])
                for d in days_5:
                    ws.append([d.strftime("%Y-%m-%d"), "Val"])
            wb.save(os.path.join(self.test_dir, f"14-Nov-26-{spec['pattern'].replace('*', '')}"))

        summary = validate_exports(exports_dir=self.test_dir, expected_date=end_d, expected_days=15, strict=False)
        self.assertFalse(summary.is_valid)
        self.assertTrue(any("Critical data gap" in e for e in summary.sheet_results["revenue"].errors))

    # 30. File integrity: Plain text file fails
    def test_verify_file_integrity_plain_text(self):
        fpath = os.path.join(self.test_dir, "fake.xlsx")
        with open(fpath, "w") as f:
            f.write("Hello world" * 500)
        err = verify_file_integrity(fpath)
        self.assertIsNotNone(err)
        self.assertIn("not a valid ZIP/Excel archive", err)

    # 31. File integrity: Small file fails
    def test_verify_file_integrity_too_small(self):
        fpath = os.path.join(self.test_dir, "tiny.xlsx")
        with open(fpath, "wb") as f:
            f.write(b"PK\x03\x04")
        err = verify_file_integrity(fpath)
        self.assertIsNotNone(err)
        self.assertIn("File size too small", err)

    # 32. Custom window: 7-day validation
    def test_validate_exports_custom_7_day_window(self):
        target = date(2026, 11, 10)
        days_7 = [target - timedelta(days=i) for i in range(7)]

        for name, spec in REPORT_SPECS.items():
            wb = openpyxl.Workbook()
            ws = wb.active
            if name == "revenue":
                ws.append(["Top", "Top", "Top"])
                ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
                for d in days_7:
                    ws.append([d.strftime("%Y-%m-%d"), 100, 100])
            elif name == "parking":
                ws.append(["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time"])
                for d in days_7:
                    ws.append([f"{d.strftime('%Y-%m-%d')} 12:00:00", f"{d.strftime('%Y-%m-%d')} 12:00:00", f"{d.strftime('%Y-%m-%d')} 12:00:00"])
            else:
                ws.append(["Date", spec["min_cols"][-1]])
                for d in days_7:
                    ws.append([d.strftime("%Y-%m-%d"), "Val"])
            wb.save(os.path.join(self.test_dir, f"10-Nov-26-{spec['pattern'].replace('*', '')}"))

        summary = validate_exports(exports_dir=self.test_dir, expected_date=target, expected_days=7, strict=True)
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.expected_window), 7)


if __name__ == "__main__":
    unittest.main()
