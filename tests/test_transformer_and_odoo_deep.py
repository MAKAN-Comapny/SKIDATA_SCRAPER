"""
tests/test_transformer_and_odoo_deep.py

Comprehensive deep test suite for Transformer.py and odoo_transformer.py.
Tests Qlik Sense export cleaning, header offsets, column normalization,
Odoo invoice mapping, rate calculations, analytic distributions, and Excel formatting.
"""

from datetime import date, datetime
import os
import shutil
import tempfile
import unittest

import openpyxl
import pandas as pd

import Transformer
import odoo_transformer


class TestTransformerAndOdooDeep(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_trans_odoo_")
        self.exports_dir = os.path.join(self.test_dir, "exports")
        self.cleaned_dir = os.path.join(self.test_dir, "Cleaned-Exports")
        os.makedirs(self.exports_dir, exist_ok=True)

        # Patch Transformer module paths for test isolation
        self._orig_trans_exp = Transformer.EXPORTS_DIR
        self._orig_trans_cln = Transformer.CLEANED_EXPORTS_DIR
        Transformer.EXPORTS_DIR = self.exports_dir
        Transformer.CLEANED_EXPORTS_DIR = self.cleaned_dir

        # Patch odoo_transformer module paths
        self._orig_odoo_exp = odoo_transformer.EXPORTS_DIR
        self._orig_odoo_cln = odoo_transformer.CLEANED_EXPORTS_DIR
        odoo_transformer.EXPORTS_DIR = self.exports_dir
        odoo_transformer.CLEANED_EXPORTS_DIR = self.cleaned_dir

    def tearDown(self):
        Transformer.EXPORTS_DIR = self._orig_trans_exp
        Transformer.CLEANED_EXPORTS_DIR = self._orig_trans_cln
        odoo_transformer.EXPORTS_DIR = self._orig_odoo_exp
        odoo_transformer.CLEANED_EXPORTS_DIR = self._orig_odoo_cln
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Transformer: Cleans Access-In-Depth (strips Totals and NaN dates)
    def test_transformer_cleans_access_in_depth(self):
        fpath = os.path.join(self.exports_dir, "14-Nov-26-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Counting Category", "Entries"])
        ws.append(["2026-11-14", "Short Term", 100])
        ws.append(["Totals", "Totals", 100])
        ws.append([None, "Unknown", 5])
        wb.save(fpath)

        Transformer.run()

        cleaned_file = os.path.join(self.cleaned_dir, "14-Nov-26-Access-In-Depth.xlsx")
        self.assertTrue(os.path.exists(cleaned_file))
        df = pd.read_excel(cleaned_file)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Date"], "2026-11-14")
        # Ensure exports/ was cleared
        self.assertFalse(os.path.exists(fpath))

    # 2. Transformer: Cleans Revenue-In-Depth with header=1
    def test_transformer_cleans_revenue_in_depth_header_1(self):
        fpath = os.path.join(self.exports_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Super Category", "Super Category", "Super Category"])
        ws.append(["Date", "Theoretical Revenue", "Gross Revenue"])
        ws.append(["2026-11-14", 500, 450])
        ws.append(["Totals", 500, 450])
        wb.save(fpath)

        Transformer.run()

        cleaned_file = os.path.join(self.cleaned_dir, "14-Nov-26-Revenue-In-Depth.xlsx")
        self.assertTrue(os.path.exists(cleaned_file))
        df = pd.read_excel(cleaned_file)
        self.assertEqual(len(df), 1)
        self.assertIn("Date", df.columns)
        self.assertEqual(df.iloc[0]["Date"], "2026-11-14")

    # 3. Transformer: Cleans System-Event-In-Depth
    def test_transformer_cleans_system_event_in_depth(self):
        fpath = os.path.join(self.exports_dir, "14-Nov-26-System-Event-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "System Event", "Count"])
        ws.append(["2026-11-14", "Barrier Open", 50])
        ws.append(["Totals", "Totals", 50])
        wb.save(fpath)

        Transformer.run()

        cleaned_file = os.path.join(self.cleaned_dir, "14-Nov-26-System-Event-In-Depth.xlsx")
        self.assertTrue(os.path.exists(cleaned_file))
        df = pd.read_excel(cleaned_file)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["System Event"], "Barrier Open")

    # 4. Transformer: Leaves Parking-Transactions unmodified during cleaning
    def test_transformer_preserves_parking_transactions(self):
        fpath = os.path.join(self.exports_dir, "14-Nov-26-Parking-Transactions.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Transaction Date/Time", "Serial Number"])
        ws.append(["2026-11-14 10:00:00", "20261114000100000000123"])
        wb.save(fpath)

        Transformer.run()

        cleaned_file = os.path.join(self.cleaned_dir, "14-Nov-26-Parking-Transactions.xlsx")
        self.assertTrue(os.path.exists(cleaned_file))
        df = pd.read_excel(cleaned_file, dtype={"Serial Number": str})
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["Serial Number"], "20261114000100000000123")

    # 5. Transformer: Raises SystemExit when exports folder is empty
    def test_transformer_empty_exports_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            Transformer.run()

    # 6. Transformer: Raises SystemExit when Date column is missing
    def test_transformer_missing_date_column_raises_system_exit(self):
        fpath = os.path.join(self.exports_dir, "14-Nov-26-Access-In-Depth.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["InvalidColumn", "Counting Category"])
        ws.append(["2026-11-14", "Short Term"])
        wb.save(fpath)

        with self.assertRaises(SystemExit):
            Transformer.run()

    # 7. Odoo: Header normalization matches case, spaces, slashes
    def test_odoo_normalize_header(self):
        self.assertEqual(odoo_transformer._normalize_header("Transaction Date/Time"), "transactiondatetime")
        self.assertEqual(odoo_transformer._normalize_header(" Transaction Date / Time "), "transactiondatetime")
        self.assertEqual(odoo_transformer._normalize_header("Serial_Number"), "serialnumber")
        self.assertEqual(odoo_transformer._normalize_header("SERIAL-NUMBER"), "serialnumber")

    # 8. Odoo: Column finding with fuzzy matching
    def test_odoo_find_column(self):
        cols = ["Car Park", "Transaction Date / Time", "Entry Plate Number"]
        col = odoo_transformer._find_column(cols, "Transaction Date/Time", "Transaction DateTime")
        self.assertEqual(col, "Transaction Date / Time")

    # 9. Odoo: Payment is nonzero checks
    def test_odoo_payment_is_nonzero(self):
        s = pd.Series([0, 0.0, "0", "0.0", 15.5, -10, None, float("nan")])
        res = odoo_transformer._payment_is_nonzero(s)
        self.assertListEqual(list(res), [False, False, False, False, True, True, False, False])

    # 10. Odoo: Serial as text preserves 23-digit barcode without exponential
    def test_odoo_serial_as_text_preserves_large_integers(self):
        self.assertEqual(odoo_transformer._serial_as_text("20261114000100000000123"), "20261114000100000000123")
        self.assertEqual(odoo_transformer._serial_as_text(202611140001), "202611140001")
        self.assertEqual(odoo_transformer._serial_as_text("12345.0"), "12345")
        self.assertEqual(odoo_transformer._serial_as_text(""), "")
        self.assertEqual(odoo_transformer._serial_as_text(None), "")

    # 11. Odoo: Car park mapping to Saudi locations
    def test_odoo_car_park_mapping(self):
        m1 = odoo_transformer._map_car_park("Westfield DAMMAM")
        self.assertEqual(m1["location"], "02 - Nakheel Mall Dammam")
        self.assertEqual(m1["region"], "Dammam Region")

        m2 = odoo_transformer._map_car_park("Mall of Arabia")
        self.assertEqual(m2["location"], "03 - Arab Mall Jeddah")
        self.assertEqual(m2["region"], "Jeddah Region")

        m_unknown = odoo_transformer._map_car_park("Non-Existent Mall")
        self.assertEqual(m_unknown["location"], "")

    # 12. Odoo: View Mall Premium calculation (exact hours * 15)
    def test_odoo_view_mall_premium_rule(self):
        # Case A: 35 minutes (1 hour) -> 15 SAR -> Premium
        is_prem_a = odoo_transformer._is_view_mall_premium(
            car_park="View Mall",
            entry_time="2026-11-14 10:00:00",
            exit_time="2026-11-14 10:35:00",
            payment=15.00,
        )
        self.assertTrue(is_prem_a)

        # Case B: 10 minutes (<= 15 minutes min duration threshold) -> Not Premium
        is_prem_b = odoo_transformer._is_view_mall_premium(
            car_park="View Mall",
            entry_time="2026-11-14 10:00:00",
            exit_time="2026-11-14 10:10:00",
            payment=15.00,
        )
        self.assertFalse(is_prem_b)

        # Case C: Other mall -> Not View Mall Premium
        is_prem_c = odoo_transformer._is_view_mall_premium(
            car_park="Alnoor Mall",
            entry_time="2026-11-14 10:00:00",
            exit_time="2026-11-14 11:35:00",
            payment=30.00,
        )
        self.assertFalse(is_prem_c)

    # 13. Odoo: Full Parking File Transformation
    def test_odoo_transform_parking_file_structure(self):
        src_path = os.path.join(self.exports_dir, "14-Nov-26-Parking-Transactions.xlsx")
        dest_path = os.path.join(self.cleaned_dir, "14-Nov-26-Parking-Transactions-Odoo.xlsx")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([
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
        ])
        # Row 1: Valid payment
        ws.append([
            "2026-11-14 15:30:00",
            "2026-11-14 13:00:00",
            "2026-11-14 15:30:00",
            "20261114000100000000123",
            "REC-1001",
            30.00,
            "Short Term Parking",
            "View Mall",
            "1234-KSA",
            "-",
        ])
        # Row 2: Zero payment (should be filtered out)
        ws.append([
            "2026-11-14 15:30:00",
            "2026-11-14 13:00:00",
            "2026-11-14 15:30:00",
            "20261114000100000000124",
            "REC-1002",
            0.00,
            "Free Pass",
            "View Mall",
            "-",
            "-",
        ])
        wb.save(src_path)

        before, after = odoo_transformer.transform_parking_file(src_path, dest_path)
        self.assertEqual(before, 2)
        self.assertEqual(after, 1)

        # Inspect resulting Odoo file
        df = pd.read_excel(dest_path, dtype={"Ticket Sequence": str, "Payment Reference": str})
        self.assertEqual(list(df.columns), odoo_transformer.OUTPUT_COLUMNS)
        row = df.iloc[0]
        self.assertEqual(row["Partner"], "SKIDATA")
        self.assertEqual(row["Invoice Lines/Unit Price"], 30.00)
        self.assertEqual(row["invoice Lines/Taxes"], "15%")
        self.assertEqual(row["Plate Number"], "1234-KSA")
        self.assertEqual(row["Ticket Sequence"], "20261114000100000000123")
        self.assertEqual(row["Payment Reference"], "REC-1001")
        self.assertEqual(row["Location"], "01 - The View Mall Riyadh")
        self.assertEqual(row["Region"], "Riyadh Region")


if __name__ == "__main__":
    unittest.main()
