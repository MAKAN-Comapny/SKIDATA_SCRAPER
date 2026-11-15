"""
tests/test_daily_pipeline_advanced.py

Advanced, rigorous test suite for Day-Wise (Single-Day / Yesterday AST) pipeline.
Tests strict single-date enforcement, boundary conditions, long-stay parkers,
Qlik Sense totals stripping, barcode integrity, and Odoo invoice conversion.
"""

from datetime import date, datetime, timedelta, timezone
import os
import shutil
import tempfile
import unittest

import openpyxl
import pandas as pd

from validator import (
    AST_TIMEZONE,
    REPORT_SPECS,
    get_ast_now,
    get_expected_yesterday,
    parse_flexible_date,
    validate_exports,
    validate_in_depth_sheet,
    validate_parking_sheet,
)
from odoo_transformer import (
    transform_parking_file,
    _pick_plate,
    _serial_as_text,
    _map_car_park,
    _resolve_sub_service,
    _build_analytics_distribution,
)


class TestDailyPipelineAdvanced(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_daily_adv_")
        self.yesterday = get_expected_yesterday()
        self.y_str = self.yesterday.strftime("%Y-%m-%d")
        self.prefix = self.yesterday.strftime("%d-%b-%y")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_daily_revenue(self, target_date: date, row_dates: list[str], include_totals: bool = True):
        fpath = os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        # Qlik Sense 2-line header
        ws.append(["Category Summary", "Category Summary", "Category Summary", "Category Summary"])
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue", "Facility"])
        for d in row_dates:
            ws.append([d, 1250.50, 1100.00, "View Mall"])
        if include_totals:
            ws.append(["Totals", len(row_dates) * 1250.50, len(row_dates) * 1100.00, "Totals"])
        wb.save(fpath)
        return fpath

    def _create_daily_access(self, target_date: date, row_dates: list[str]):
        fpath = os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category", "Entries", "Exits"])
        for d in row_dates:
            ws.append([d, "Short Term Parking", 420, 395])
        wb.save(fpath)
        return fpath

    def _create_daily_system_event(self, target_date: date, row_dates: list[str]):
        fpath = os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-System-Event-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "System Event", "Device", "Count"])
        for d in row_dates:
            ws.append([d, "Barrier Gate Passed", "Exit Gate 01", 380])
        wb.save(fpath)
        return fpath

    def _create_daily_parking(
        self,
        target_date: date,
        records: list[dict],
    ):
        fpath = os.path.join(self.test_dir, f"{target_date.strftime('%d-%b-%y')}-Parking-Transactions.xlsx")
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
            "Entry Plate Number",
            "Exit Plate Number",
        ]
        ws.append(headers)
        for r in records:
            ws.append([
                r.get("txn", f"{target_date.strftime('%Y-%m-%d')} 14:30:00"),
                r.get("entry", f"{target_date.strftime('%Y-%m-%d')} 10:15:00"),
                r.get("exit", f"{target_date.strftime('%Y-%m-%d')} 14:30:00"),
                r.get("serial", "20261114000100000000123"),
                r.get("receipt", "REC-99887766"),
                r.get("payment", 25.00),
                r.get("article", "Short Term Parking"),
                r.get("car_park", "View Mall"),
                r.get("entry_plate", "1234-KSA"),
                r.get("exit_plate", "1234-KSA"),
            ])
        wb.save(fpath)
        return fpath

    # 1. AST Timezone Yesterday Precision
    def test_ast_yesterday_math_at_utc_midnight_boundaries(self):
        # At 21:00 UTC on Nov 14, AST is 00:00 on Nov 15 -> Yesterday AST is Nov 14
        utc_21_00 = datetime(2026, 11, 14, 21, 0, 0, tzinfo=timezone.utc)
        ast_dt = utc_21_00.astimezone(AST_TIMEZONE)
        self.assertEqual(ast_dt.date(), date(2026, 11, 15))
        yesterday_ast = (ast_dt - timedelta(days=1)).date()
        self.assertEqual(yesterday_ast, date(2026, 11, 14))

        # At 20:59 UTC on Nov 14, AST is 23:59 on Nov 14 -> Yesterday AST is Nov 13
        utc_20_59 = datetime(2026, 11, 14, 20, 59, 59, tzinfo=timezone.utc)
        ast_dt_prev = utc_20_59.astimezone(AST_TIMEZONE)
        self.assertEqual(ast_dt_prev.date(), date(2026, 11, 14))
        yesterday_ast_prev = (ast_dt_prev - timedelta(days=1)).date()
        self.assertEqual(yesterday_ast_prev, date(2026, 11, 13))

    # 2. Strict Date Matching: All rows match yesterday AST -> PASSED
    def test_daily_all_rows_match_yesterday_passes(self):
        self._create_daily_revenue(self.yesterday, [self.y_str] * 10)
        self._create_daily_access(self.yesterday, [self.y_str] * 10)
        self._create_daily_system_event(self.yesterday, [self.y_str] * 10)
        self._create_daily_parking(self.yesterday, [{}] * 10)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=self.yesterday, expected_days=1, strict=True)
        self.assertTrue(summary.is_valid)
        self.assertEqual(len(summary.sheet_results), 4)
        for cat, res in summary.sheet_results.items():
            self.assertEqual(res.dates_found, {self.yesterday})
            self.assertEqual(len(res.errors), 0)

    # 3. Date Bleed: Even 1 row from day before yesterday triggers rejection
    def test_daily_revenue_with_stale_prior_day_row_rejected(self):
        two_days_ago = (self.yesterday - timedelta(days=1)).strftime("%Y-%m-%d")
        # 9 rows from yesterday, 1 rogue row from two days ago
        self._create_daily_revenue(self.yesterday, [self.y_str] * 9 + [two_days_ago])
        self._create_daily_access(self.yesterday, [self.y_str] * 10)
        self._create_daily_system_event(self.yesterday, [self.y_str] * 10)
        self._create_daily_parking(self.yesterday, [{}] * 10)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=self.yesterday, expected_days=1, strict=False)
        self.assertFalse(summary.is_valid)
        rev_res = summary.sheet_results["revenue"]
        self.assertFalse(rev_res.is_valid)
        self.assertTrue(any("unexpected dates" in e for e in rev_res.errors))

    # 4. Future Date Bleed: 1 row from today/future triggers rejection
    def test_daily_access_with_future_date_row_rejected(self):
        tomorrow = (self.yesterday + timedelta(days=2)).strftime("%Y-%m-%d")
        self._create_daily_revenue(self.yesterday, [self.y_str] * 10)
        self._create_daily_access(self.yesterday, [self.y_str] * 9 + [tomorrow])
        self._create_daily_system_event(self.yesterday, [self.y_str] * 10)
        self._create_daily_parking(self.yesterday, [{}] * 10)

        summary = validate_exports(exports_dir=self.test_dir, expected_date=self.yesterday, expected_days=1, strict=False)
        self.assertFalse(summary.is_valid)
        acc_res = summary.sheet_results["access"]
        self.assertFalse(acc_res.is_valid)

    # 5. Long-Stay Parkers Semantics: Entry 15 days ago, Exit yesterday -> MUST PASS
    def test_daily_parking_long_stay_vehicle_passes(self):
        fifteen_days_ago = (self.yesterday - timedelta(days=15)).strftime("%Y-%m-%d 08:00:00")
        records = [
            # Standard daily visitor
            {"txn": f"{self.y_str} 11:00:00", "entry": f"{self.y_str} 09:00:00", "exit": f"{self.y_str} 11:00:00"},
            # Long-stay vehicle: entered 15 days ago, paid and exited yesterday
            {"txn": f"{self.y_str} 16:30:00", "entry": fifteen_days_ago, "exit": f"{self.y_str} 16:30:00", "payment": 450.00},
        ]
        fpath = self._create_daily_parking(self.yesterday, records)
        res = validate_parking_sheet(fpath, REPORT_SPECS["parking"], {self.yesterday})
        self.assertTrue(res.is_valid)
        self.assertEqual(res.dates_found, {self.yesterday})

    # 6. Long-Stay Parkers Error: Transaction Date is old date -> MUST FAIL
    def test_daily_parking_wrong_transaction_date_fails(self):
        old_date = (self.yesterday - timedelta(days=2)).strftime("%Y-%m-%d 16:30:00")
        records = [
            {"txn": old_date, "entry": old_date, "exit": old_date},
        ]
        fpath = self._create_daily_parking(self.yesterday, records)
        res = validate_parking_sheet(fpath, REPORT_SPECS["parking"], {self.yesterday})
        self.assertFalse(res.is_valid)
        self.assertTrue(any("does not contain expected date" in e for e in res.errors))

    # 7. Scientific Notation Barcode Corruption in Daily File caught
    def test_daily_parking_scientific_notation_barcode_rejected(self):
        records = [
            {"serial": "2.02611E+22"},  # Corrupted barcode
        ]
        fpath = self._create_daily_parking(self.yesterday, records)
        res = validate_parking_sheet(fpath, REPORT_SPECS["parking"], {self.yesterday})
        self.assertFalse(res.is_valid)
        self.assertTrue(any("corrupted to scientific notation" in e for e in res.errors))

    # 8. Qlik Totals Row Stripping: Only totals row present -> reports 0 data rows
    def test_daily_revenue_only_totals_reports_empty_data_rows(self):
        fpath = self._create_daily_revenue(self.yesterday, [], include_totals=True)
        res = validate_in_depth_sheet(fpath, REPORT_SPECS["revenue"], {self.yesterday})
        self.assertFalse(res.is_valid)
        self.assertTrue(any("0 data rows after filtering" in e for e in res.errors))

    # 9. Daily Odoo Transformation: Zero payment filtering & structure
    def test_daily_odoo_transformation_filters_zero_payments(self):
        records = [
            {"serial": "20261114000100000000111", "payment": 0.00, "article": "Free 15-min Exit"},
            {"serial": "20261114000100000000222", "payment": 15.00, "article": "Standard Hourly"},
            {"serial": "20261114000100000000333", "payment": 45.00, "article": "Lost Ticket Fee"},
            {"serial": "20261114000100000000444", "payment": "0", "article": "Staff Pass"},
        ]
        src_path = self._create_daily_parking(self.yesterday, records)
        dest_path = os.path.join(self.test_dir, f"{self.prefix}-Parking-Transactions-Odoo.xlsx")

        before, after = transform_parking_file(src_path, dest_path)
        self.assertEqual(before, 4)
        self.assertEqual(after, 2)  # Only 15.00 and 45.00 kept

        # Inspect transformed Excel
        df_odoo = pd.read_excel(dest_path, dtype={"Ticket Sequence": str, "Payment Reference": str})
        self.assertEqual(len(df_odoo), 2)
        self.assertTrue(all(df_odoo["Invoice Lines/Unit Price"] > 0))
        self.assertTrue(all(df_odoo["Partner"] == "SKIDATA"))
        self.assertTrue(all(df_odoo["invoice Lines/Taxes"] == "15%"))
        self.assertTrue(all(df_odoo["Invoice Lines/Quantity"] == 1))
        # Barcodes preserved exactly without scientific notation
        self.assertIn("20261114000100000000222", df_odoo["Ticket Sequence"].values)
        self.assertIn("20261114000100000000333", df_odoo["Ticket Sequence"].values)

    # 10. Daily Plate Number Edge Cases: '-' and '#' stripped
    def test_daily_plate_number_placeholders_stripped(self):
        self.assertEqual(_pick_plate("-", "5678-ABC"), "5678-ABC")
        self.assertEqual(_pick_plate("#", "-"), "")
        self.assertEqual(_pick_plate(None, "1234-XYZ"), "1234-XYZ")
        self.assertEqual(_pick_plate("9999-KSA", "#"), "9999-KSA")
        self.assertEqual(_pick_plate("nan", None), "")

    # 11. Daily View Mall Premium Logic: Duration > 15 and payment == hours * 15
    def test_daily_view_mall_premium_service_resolution(self):
        # 70 minutes = 2 hours -> Payment 30 -> Premium
        sub_service_premium = _resolve_sub_service(
            car_park="View Mall",
            entry_time="2026-11-14 10:00:00",
            exit_time="2026-11-14 11:10:00",
            payment=30.00,
            mapped_sub_service="Standard",
        )
        self.assertEqual(sub_service_premium, "Premium")

        # 70 minutes = 2 hours -> Payment 20 (not 30) -> Standard
        sub_service_standard = _resolve_sub_service(
            car_park="View Mall",
            entry_time="2026-11-14 10:00:00",
            exit_time="2026-11-14 11:10:00",
            payment=20.00,
            mapped_sub_service="Standard",
        )
        self.assertEqual(sub_service_standard, "Standard")

    # 12. Daily Analytic IDs Generation
    def test_daily_analytic_ids_distribution_json(self):
        # View Mall Riyadh: location=42, region=110, service=135, sub_service=131 (Premium)
        json_str = _build_analytics_distribution(
            location="01 - The View Mall Riyadh",
            region="Riyadh Region",
            service="Paid Parking",
            sub_service="Premium",
        )
        self.assertIn('"42":100', json_str)
        self.assertIn('"110":100', json_str)
        self.assertIn('"135":100', json_str)
        self.assertIn('"131":100', json_str)


if __name__ == "__main__":
    unittest.main()
