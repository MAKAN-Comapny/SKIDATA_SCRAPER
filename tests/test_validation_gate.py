"""Comprehensive tests for the 4-tier SKIDATA Backfill Validation Gate & pipeline hardening."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import openpyxl
from fastapi.testclient import TestClient
from openpyxl.cell import WriteOnlyCell

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from Transformer_backfill import clean_backfill_directory
import main_backfill
import portal_server
from scraper_backfill import PortalZeroDataError
from validator_backfill import (
    inspect_xlsx_summary,
    merge_validation_reports,
    validate_backfill_run,
)

PARKING_HEADERS = [
    "Facility", "Car Park", "Entry Device", "Exit Device",
    "Entry Date/Time", "Exit Date/Time", "Duration", "Ticket Media Type",
    "Serial Number", "Article", "Article Category", "Receipt Number",
    "Payment Type", "Credit Card Number", "Payment", "Payment Device",
    "Transaction Date/Time", "Cashier", "Discount(Discount Number)",
    "Discount Amount", "Customer Name", "User Full Name",
    "Entry Plate Number", "Exit Plate Number", "Access ID",
]


def _to_excel_serial(d: date, hour: int = 10, minute: int = 0) -> float:
    dt = datetime(d.year, d.month, d.day, hour, minute, 0)
    delta = dt - datetime(1899, 12, 30)
    return delta.days + delta.seconds / 86400.0


def _write_parking_xlsx(path: str, rows: list[dict]) -> None:
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    ws.append(PARKING_HEADERS)
    for r in rows:
        out = []
        for col in PARKING_HEADERS:
            val = r.get(col)
            if isinstance(val, tuple):
                c = WriteOnlyCell(ws, value=val[0])
                c.number_format = val[1]
                out.append(c)
            else:
                out.append(val)
        ws.append(out)
    wb.save(path)


def _write_indepth_xlsx(
    path: str,
    headers: list[str],
    data_rows: list[list],
    raw_with_title: bool = False,
) -> None:
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    if raw_with_title:
        ws.append(["SKIDATA Qlik Export Title"])
    ws.append(headers)
    for r in data_rows:
        ws.append(r)
    wb.save(path)


def _make_clean_parking_row(d: date, access_id: int, payment: float = 20.0, serial: str = "6401234567890123") -> dict:
    entry_s = _to_excel_serial(d, hour=9, minute=0)
    exit_s = _to_excel_serial(d, hour=11, minute=30)
    return {
        "Facility": "Makan Parking Co",
        "Car Park": "View Mall",
        "Entry Device": "E1",
        "Exit Device": "EX1",
        "Entry Date/Time": (entry_s, "MM/dd/yyyy hh:mm:ss am/pm"),
        "Exit Date/Time": (exit_s, "MM/dd/yyyy hh:mm:ss am/pm"),
        "Duration": (exit_s - entry_s, "dd hh:mm:ss"),
        "Ticket Media Type": "Barcode",
        "Serial Number": serial,
        "Article": "Short Term",
        "Article Category": "Visitor",
        "Receipt Number": "100200300400",
        "Payment Type": "Card",
        "Credit Card Number": "41111100001111",
        "Payment": payment,
        "Payment Device": "APM 1",
        "Transaction Date/Time": (exit_s, "MM/dd/yyyy hh:mm:ss am/pm"),
        "Cashier": "-",
        "Discount(Discount Number)": "-",
        "Discount Amount": 0.0,
        "Customer Name": "N/A",
        "User Full Name": "N/A",
        "Entry Plate Number": "ABC1234",
        "Exit Plate Number": "ABC1234",
        "Access ID": access_id,
    }


def _build_complete_clean_fixture(
    base_dir: str,
    dates: list[date],
    prefix: str = "01-Oct-26_to_03-Oct-26",
    payment_per_row: float = 20.0,
    rev_gross_override: float | None = None,
    rev_qty_override: int | None = None,
    manifest: dict | None = None,
    clean_meta: dict | None = None,
) -> tuple[dict, str, str]:
    exports_dir = os.path.join(base_dir, "exports")
    cleaned_dir = os.path.join(base_dir, "cleaned")
    os.makedirs(exports_dir, exist_ok=True)
    os.makedirs(cleaned_dir, exist_ok=True)

    pt_rows = []
    rev_rows = []
    aid = 500000
    for idx, d in enumerate(dates, start=1):
        pt_rows.append(_make_clean_parking_row(d, aid + idx * 10 + 1, payment=payment_per_row, serial=f"64000000000010{idx}"))
        pt_rows.append(_make_clean_parking_row(d, aid + idx * 10 + 2, payment=payment_per_row, serial=f"64000000000020{idx}"))
        rev_rows.append([
            "Makan", "View Mall",
            d.strftime("%d-%b-%Y"),
            2 if rev_qty_override is None else rev_qty_override,
            payment_per_row * 2,
            (payment_per_row * 2) if rev_gross_override is None else rev_gross_override,
        ])

    _write_parking_xlsx(os.path.join(cleaned_dir, f"{prefix}-Parking-Transactions.xlsx"), pt_rows)
    _write_indepth_xlsx(
        os.path.join(cleaned_dir, f"{prefix}-Revenue-In-Depth.xlsx"),
        ["Facility", "Car Park", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
        rev_rows,
    )
    _write_indepth_xlsx(
        os.path.join(cleaned_dir, f"{prefix}-Access-In-Depth.xlsx"),
        ["Facility", "Car Park", "Date", "Access Count"],
        [[
            "Makan", "View Mall", dates[0].strftime("%d-%b-%Y"), len(pt_rows),
        ]],
    )
    _write_indepth_xlsx(
        os.path.join(cleaned_dir, f"{prefix}-System-Event-In-Depth.xlsx"),
        ["Facility", "Car Park", "Date", "Events"],
        [[
            "Makan", "View Mall", dates[0].strftime("%d-%b-%Y"), 4,
        ]],
    )

    if manifest is not None:
        with open(os.path.join(exports_dir, "_scrape_manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
    if clean_meta is not None:
        with open(os.path.join(cleaned_dir, "_clean_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(clean_meta, fh)

    spec = {
        "name": "October 2026 Test",
        "start_date": dates[0],
        "end_date": dates[-1],
        "file_prefix": prefix,
        "sharepoint_folder": "Validated_Docs/old/Oct",
    }
    return spec, exports_dir, cleaned_dir


class TestValidationGateTiers(unittest.TestCase):
    def test_clean_4_tables_pass_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]
            spec, exp, cln = _build_complete_clean_fixture(tmp, days)
            report = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["upload_blocked"])
            self.assertEqual(report["hard_failures"], [])
            self.assertEqual(report["soft_warnings"], [])
            self.assertEqual(report["checks"]["completeness"]["covered_days"], 3)
            self.assertEqual(report["checks"]["completeness"]["expected_days"], 3)
            self.assertTrue(report["checks"]["structure_and_types"]["barcodes_text_ok"])
            self.assertTrue(report["checks"]["reconciliation"]["pre_clean_totals_match"])

    def test_missing_day_blocks_upload_as_partial_data_while_empty_day_is_soft_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]
            # Missing Oct 2 without portal empty manifest -> HARD FAIL + partial badge
            spec, exp, cln = _build_complete_clean_fixture(tmp, [days[0], days[2]])
            spec["end_date"] = days[2]
            report_fail = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            self.assertEqual(report_fail["status"], "failed")
            self.assertTrue(report_fail["upload_blocked"])
            codes = {f["code"] for f in report_fail["hard_failures"]}
            self.assertIn("PARTIAL_DATA_MISSING_DAYS", codes)
            self.assertEqual(report_fail["tables"]["Parking Transactions"]["status"], "partial")

            # Now record Oct 2 in manifest portal_empty_dates -> SOFT WARNING, upload allowed
            manifest = {
                "parking": {
                    "mode": "daily_chunks",
                    "portal_empty_dates": ["2026-10-02"],
                    "failed_dates": {},
                    "scraped_dates": ["2026-10-01", "2026-10-03"],
                    "chunk_row_counts": {"2026-10-01": 2, "2026-10-03": 2},
                    "duplicate_chunks_detected": [],
                }
            }
            with open(os.path.join(exp, "_scrape_manifest.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            # Remove Oct 2 from Rev as well so missing-day reconciliation does not also fire
            _write_indepth_xlsx(
                os.path.join(cln, f"{spec['file_prefix']}-Revenue-In-Depth.xlsx"),
                ["Facility", "Car Park", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
                [
                    ["Makan", "View Mall", "01-Oct-2026", 2, 40.0, 40.0],
                    ["Makan", "View Mall", "03-Oct-2026", 2, 40.0, 40.0],
                ],
            )
            report_warn = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            self.assertEqual(report_warn["status"], "warning")
            self.assertFalse(report_warn["upload_blocked"])
            warn_codes = {w["code"] for w in report_warn["soft_warnings"]}
            self.assertIn("PORTAL_ZERO_DATA_DAYS", warn_codes)

    def test_qlik_100k_cap_risk_blocks_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = [date(2026, 10, 1)]
            manifest = {
                "parking": {
                    "mode": "full_range",
                    "portal_empty_dates": [],
                    "failed_dates": {},
                    "scraped_dates": [],
                    "full_export_row_count": 99500,
                    "chunk_row_counts": {},
                    "duplicate_chunks_detected": [],
                }
            }
            spec, exp, cln = _build_complete_clean_fixture(tmp, days, prefix="01-Oct-26", manifest=manifest)
            report = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            self.assertTrue(report["upload_blocked"])
            self.assertIn("QLIK_100K_CAP_RISK", {f["code"] for f in report["hard_failures"]})

    def test_tier2_catches_barcodes_dates_durations_na_strip_and_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = date(2026, 10, 5)
            spec, exp, cln = _build_complete_clean_fixture(tmp, [d], prefix="05-Oct-26")

            # Corrupt Parking-Transactions (>=20 rows so NA_VALUES_STRIPPED heuristic triggers)
            bad_rows = []
            for idx in range(25):
                bad_row = _make_clean_parking_row(d, 9001 + idx, payment=15.0)
                bad_row["Serial Number"] = "6.40123E+15"
                bad_row["Entry Date/Time"] = "10/05/2026 09:00:00 AM"
                bad_row["Duration"] = "02:30:00"
                bad_row["Customer Name"] = ""
                bad_row["User Full Name"] = None
                bad_rows.append(bad_row)
            _write_parking_xlsx(os.path.join(cln, "05-Oct-26-Parking-Transactions.xlsx"), bad_rows)

            # Inject unstripped Totals row into Revenue-In-Depth (both col 0 and Date say Totals)
            _write_indepth_xlsx(
                os.path.join(cln, "05-Oct-26-Revenue-In-Depth.xlsx"),
                ["Facility", "Car Park", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
                [
                    ["Makan", "View Mall", "05-Oct-2026", 1, 15.0, 15.0],
                    ["Totals", "", "Totals", 1, 15.0, 15.0],
                ],
            )

            report = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            self.assertTrue(report["upload_blocked"])
            codes = {f["code"] for f in report["hard_failures"]}
            self.assertTrue({
                "BARCODE_TEXT_CORRUPTION",
                "DATE_NOT_NATIVE_EXCEL",
                "DURATION_CORRUPTED_STRING",
                "NA_VALUES_STRIPPED",
                "TOTALS_ROW_PRESENT",
            }.issubset(codes))
            # Verify double-"Totals" row was counted once, not twice
            self.assertEqual(report["checks"]["structure_and_types"]["violation_counts"]["totals_left"], 1)

    def test_clean_backfill_directory_strips_totals_captures_meta_and_blocks_raw_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "exports")
            dst = os.path.join(tmp, "cleaned")
            os.makedirs(src, exist_ok=True)

            # Valid raw Revenue-In-Depth (header=1, Totals row included)
            _write_indepth_xlsx(
                os.path.join(src, "01-Oct-26-Revenue-In-Depth.xlsx"),
                ["Facility", "Car Park", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
                [
                    ["Makan", "View Mall", "01-Oct-2026", 5, 100.0, 100.0],
                    ["Makan", "View Mall", "Totals", 5, 100.0, 100.0],
                ],
                raw_with_title=True,
            )
            # Corrupt System-Event-In-Depth (not a zip) -> strict=True must NOT copy raw file
            corrupt_src = os.path.join(src, "01-Oct-26-System-Event-In-Depth.xlsx")
            with open(corrupt_src, "wb") as fh:
                fh.write(b"NOT_A_VALID_XLSX_ZIP")

            meta = clean_backfill_directory(src, dst, strict=True)
            self.assertIn("01-Oct-26-Revenue-In-Depth.xlsx", meta["revenue_pre_clean_totals"])
            self.assertEqual(meta["revenue_pre_clean_totals"]["01-Oct-26-Revenue-In-Depth.xlsx"]["Gross Revenue"], 100.0)
            self.assertTrue(any("01-Oct-26-System-Event-In-Depth.xlsx" in e for e in meta["clean_errors"]))
            self.assertFalse(os.path.exists(os.path.join(dst, "01-Oct-26-System-Event-In-Depth.xlsx")))

            summary = inspect_xlsx_summary(os.path.join(dst, "01-Oct-26-Revenue-In-Depth.xlsx"))
            self.assertTrue(summary["readable"])
            self.assertEqual(summary["row_count"], 1)

    def test_tier3_reconciliation_variance_vs_hard_fail_and_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            d1, d2 = date(2026, 10, 10), date(2026, 10, 11)
            # Each day has 2 rows x 100 = 200 per day, total 400.
            # Set rev_gross per day to 212 => total 424 vs 400 (5.66% diff -> SOFT WARN)
            spec, exp, cln = _build_complete_clean_fixture(
                tmp, [d1, d2], prefix="10-Oct-26_to_11-Oct-26", payment_per_row=100.0, rev_gross_override=212.0
            )
            warn_report = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            self.assertEqual(warn_report["status"], "warning")
            self.assertFalse(warn_report["upload_blocked"])
            self.assertIn("REVENUE_TXN_VARIANCE", {w["code"] for w in warn_report["soft_warnings"]})

            # Severe mismatch (500 vs 200/day -> 60% diff -> HARD FAIL)
            spec2, exp2, cln2 = _build_complete_clean_fixture(
                os.path.join(tmp, "bad"), [d1, d2], prefix="10-Oct-26_to_11-Oct-26", payment_per_row=100.0, rev_gross_override=500.0
            )
            fail_report = validate_backfill_run(spec2, cln2, exp2, sheet_filter="all")
            self.assertTrue(fail_report["upload_blocked"])
            self.assertIn("REVENUE_TXN_MISMATCH", {f["code"] for f in fail_report["hard_failures"]})

    def test_tier4_duplicates_and_exit_before_entry_soft_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = date(2026, 10, 12)
            spec, exp, cln = _build_complete_clean_fixture(tmp, [d], prefix="12-Oct-26", payment_per_row=20.0)
            # Override Parking-Transactions with 2 identical composite keys + 1 Exit < Entry row
            r1 = _make_clean_parking_row(d, 7001, payment=20.0, serial="640999888777")
            r2 = _make_clean_parking_row(d, 7001, payment=20.0, serial="640999888777")  # duplicate composite
            r3 = _make_clean_parking_row(d, 7002, payment=20.0, serial="640111222333")
            r3["Entry Date/Time"] = (_to_excel_serial(d, hour=14), "MM/dd/yyyy hh:mm:ss am/pm")
            r3["Exit Date/Time"] = (_to_excel_serial(d, hour=12), "MM/dd/yyyy hh:mm:ss am/pm")  # inverted clock skew
            _write_parking_xlsx(os.path.join(cln, "12-Oct-26-Parking-Transactions.xlsx"), [r1, r2, r3])
            _write_indepth_xlsx(
                os.path.join(cln, "12-Oct-26-Revenue-In-Depth.xlsx"),
                ["Facility", "Car Park", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
                [["Makan", "View Mall", "12-Oct-2026", 3, 60.0, 60.0]],
            )

            report = validate_backfill_run(spec, cln, exp, sheet_filter="all")
            # <= 100 dups + clock skew => soft warnings ONLY, upload remains allowed!
            self.assertEqual(report["status"], "warning")
            self.assertFalse(report["upload_blocked"])
            warn_codes = {w["code"] for w in report["soft_warnings"]}
            self.assertIn("DUPLICATE_COMPOSITE_ROWS", warn_codes)
            self.assertIn("INVERTED_EXIT_LT_ENTRY", warn_codes)

    def test_execute_month_pipeline_blocks_upload_on_fail_and_allows_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = [date(2026, 10, 1)]
            spec_raw, exp, cln = _build_complete_clean_fixture(tmp, days, prefix="01-Oct-26")
            spec_raw["local_exports"] = exp
            spec_raw["local_cleaned"] = cln

            # 1) Simulate partial missing day failure => upload_backfill MUST NOT be called
            spec_raw["end_date"] = date(2026, 10, 2)
            with patch.object(main_backfill, "scrape_month", new=AsyncMock()), \
                 patch.object(main_backfill, "clean_and_stage", return_value={"clean_errors": [], "revenue_pre_clean_totals": {}}), \
                 patch.object(main_backfill, "upload_backfill") as mock_upload:
                rep_blocked = asyncio.run(
                    main_backfill.execute_month_pipeline(spec_raw, sheet_filter="all", headless=True, no_upload=False, validate=True)
                )
            self.assertTrue(rep_blocked["upload_blocked"])
            mock_upload.assert_not_called()
            self.assertTrue(os.path.exists(os.path.join(cln, "_validation_report.json")))

            # 2) Same broken state with validate=False => bypassed & upload runs!
            with patch.object(main_backfill, "scrape_month", new=AsyncMock()), \
                 patch.object(main_backfill, "clean_and_stage", return_value={"clean_errors": [], "revenue_pre_clean_totals": {}}), \
                 patch.object(main_backfill, "upload_backfill") as mock_upload:
                rep_bypass = asyncio.run(
                    main_backfill.execute_month_pipeline(spec_raw, sheet_filter="all", headless=True, no_upload=False, validate=False)
                )
            self.assertEqual(rep_bypass["status"], "bypassed")
            self.assertFalse(rep_bypass["upload_blocked"])
            mock_upload.assert_called_once()

    def test_merge_validation_reports_and_portal_execute_validate_flag(self):
        # Multi-batch merge rolls up hard failure safely
        r1 = {"batch_name": "Oct 2026", "file_prefix": "Oct", "sharepoint_folder": "Validated_Docs/old/Oct",
              "validation_enabled": True, "status": "passed", "upload_blocked": False, "upload_performed": True,
              "summary": "ok", "tables": {"Revenue in Depth": {"status": "done", "rows": 10}},
              "checks": {"completeness": {"expected_days": 10, "covered_days": 10}, "reconciliation": {"revenue_gross": 100.0, "revenue_quantity": 10, "parking_payment": 100.0, "parking_rows": 10}},
              "hard_failures": [], "soft_warnings": []}
        r2 = {"batch_name": "Nov 2026", "file_prefix": "Nov", "sharepoint_folder": "Validated_Docs/old/Nov",
              "validation_enabled": True, "status": "failed", "upload_blocked": True, "upload_performed": False,
              "summary": "missing day", "tables": {"Revenue in Depth": {"status": "done", "rows": 5}},
              "checks": {"completeness": {"expected_days": 10, "covered_days": 9}, "reconciliation": {"revenue_gross": 50.0, "revenue_quantity": 5, "parking_payment": 50.0, "parking_rows": 5}},
              "hard_failures": [{"code": "PARTIAL_DATA_MISSING_DAYS", "severity": "hard", "category": "completeness", "message": "Missing 1 day", "details": {}}],
              "soft_warnings": []}
        merged = merge_validation_reports([r1, r2])
        self.assertEqual(merged["status"], "failed")
        self.assertTrue(merged["upload_blocked"])
        self.assertEqual(len(merged["batches"]), 2)

        # Verify FastAPI accepts `validate: false` and forwards it to job_runner
        client = TestClient(portal_server.app)
        headers = {"Authorization": f"Bearer {portal_server.AUTH_TOKEN_SECRET}"}
        with patch.object(portal_server.job_runner, "start_job", return_value=(True, "Started", {"id": 99})) as mock_start:
            res = client.post(
                "/api/execute",
                json={
                    "mode": "single_date",
                    "target_date": "2026-10-01",
                    "target_folder": "Validated_Docs/old",
                    "validate": False,
                },
                headers=headers,
            )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(mock_start.call_args.args[0]["validate"])
        self.assertEqual(PortalZeroDataError.__name__, "PortalZeroDataError")

    def test_portal_serves_mangekyou_sharingan_favicon(self):
        client = TestClient(portal_server.app)
        res_html = client.get("/")
        self.assertEqual(res_html.status_code, 200)
        self.assertIn("./favicon.webp?v=2", res_html.text)
        self.assertIn("./favicon.png?v=2", res_html.text)

        res_webp = client.get("/favicon.webp")
        self.assertEqual(res_webp.status_code, 200)
        self.assertTrue(res_webp.content.startswith(b"RIFF"))
        self.assertEqual(res_webp.content[8:12], b"WEBP")

        res_png = client.get("/favicon.png")
        self.assertEqual(res_png.status_code, 200)
        self.assertTrue(res_png.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_pre_clean_revenue_totals_ignores_qlik_subgroup_subtotals(self):
        with tempfile.TemporaryDirectory() as tmp:
            # (a) Table-wide Grand Total row (Facility=='Totals') wins over earlier 0-rev subgroup Totals
            src_a = os.path.join(tmp, "exp_a")
            dst_a = os.path.join(tmp, "cln_a")
            os.makedirs(src_a, exist_ok=True)
            _write_indepth_xlsx(
                os.path.join(src_a, "07-Oct-26-Revenue-In-Depth.xlsx"),
                ["Facility", "Car Park", "Article", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
                [
                    ["Makan", "View Mall", "VIP Free", "07-Oct-2026", 4, 0.0, 0.0],
                    ["Makan", "View Mall", "VIP Free", "Totals", 4, 0.0, 0.0],
                    ["Makan", "View Mall", "Short Term", "07-Oct-2026", 20040, 26115.0, 26115.0],
                    ["Makan", "View Mall", "Short Term", "Totals", 20040, 26115.0, 26115.0],
                    ["Totals", "", "", "", 20044, 26115.0, 26115.0],
                ],
                raw_with_title=True,
            )
            meta_a = clean_backfill_directory(src_a, dst_a, strict=True)
            totals_a = meta_a["revenue_pre_clean_totals"]["07-Oct-26-Revenue-In-Depth.xlsx"]
            self.assertEqual(totals_a["Quantity"], 20044.0)
            self.assertEqual(totals_a["Gross Revenue"], 26115.0)
            summary_a = inspect_xlsx_summary(os.path.join(dst_a, "07-Oct-26-Revenue-In-Depth.xlsx"))
            self.assertEqual(summary_a["row_count"], 2)

            # (b) Multiple per-subgroup Date=='Totals' rows without a Grand Total row -> returns None (not Qty=4 / Gross=0)
            src_b = os.path.join(tmp, "exp_b")
            dst_b = os.path.join(tmp, "cln_b")
            os.makedirs(src_b, exist_ok=True)
            _write_indepth_xlsx(
                os.path.join(src_b, "07-Oct-26-Revenue-In-Depth.xlsx"),
                ["Facility", "Car Park", "Article", "Date", "Quantity", "Theoretical Revenue", "Gross Revenue"],
                [
                    ["Makan", "View Mall", "VIP Free", "07-Oct-2026", 4, 0.0, 0.0],
                    ["Makan", "View Mall", "VIP Free", "Totals", 4, 0.0, 0.0],
                    ["Makan", "Mall of Arabia", "Short Term", "07-Oct-2026", 10, 500.0, 500.0],
                    ["Makan", "Mall of Arabia", "Short Term", "Totals", 10, 500.0, 500.0],
                ],
                raw_with_title=True,
            )
            meta_b = clean_backfill_directory(src_b, dst_b, strict=True)
            self.assertNotIn("07-Oct-26-Revenue-In-Depth.xlsx", meta_b["revenue_pre_clean_totals"])


if __name__ == "__main__":
    unittest.main()
