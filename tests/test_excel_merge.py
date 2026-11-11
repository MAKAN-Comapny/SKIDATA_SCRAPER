"""Unit tests for streaming Parking-Transactions Excel merger."""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import openpyxl
from openpyxl.cell import WriteOnlyCell

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from excel_merge import merge_parking_xlsx

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_HEADERS = [
    "Facility", "Car Park", "Entry Device", "Exit Device",
    "Entry Date/Time", "Exit Date/Time", "Duration", "Ticket Media Type",
    "Serial Number", "Article", "Article Category", "Receipt Number",
    "Payment Type", "Credit Card Number", "Payment", "Payment Device",
    "Transaction Date/Time", "Cashier", "Discount(Discount Number)",
    "Discount Amount", "Customer Name", "User Full Name",
    "Entry Plate Number", "Exit Plate Number", "Access ID",
]


def _make_sample_xlsx(path: str, rows: list[dict], reverse_cols: bool = False) -> None:
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    cols = list(reversed(_HEADERS)) if reverse_cols else list(_HEADERS)
    ws.append(cols)
    for row_data in rows:
        out = []
        for col in cols:
            val = row_data.get(col)
            if isinstance(val, tuple):
                cell = WriteOnlyCell(ws, value=val[0])
                cell.number_format = val[1]
                out.append(cell)
            else:
                out.append(val)
        ws.append(out)
    wb.save(path)


class TestExcelMerge(unittest.TestCase):
    def test_preserves_types_barcodes_durations_and_na(self):
        with tempfile.TemporaryDirectory() as tmp:
            f1 = os.path.join(tmp, "day1.xlsx")
            f2 = os.path.join(tmp, "day2.xlsx")
            out = os.path.join(tmp, "merged.xlsx")

            _make_sample_xlsx(f1, [{
                "Facility": "Makan Parking Co",
                "Car Park": "View Mall",
                "Entry Device": "E1",
                "Exit Device": "EX1",
                "Entry Date/Time": (46297.954375, "MM/dd/yyyy hh:mm:ss am/pm"),
                "Exit Date/Time": (46305.68039351852, "MM/dd/yyyy hh:mm:ss am/pm"),
                "Duration": (7.726018518518519, "dd hh:mm:ss"),
                "Ticket Media Type": "BARCODE",
                "Serial Number": "33741962105024400092681",
                "Article": "Short-term parking tkt",
                "Article Category": "Short-term parking tkt",
                "Receipt Number": "63972.0",
                "Payment Type": "SurePAy",
                "Credit Card Number": None,
                "Payment": 240,
                "Payment Device": "EX1",
                "Transaction Date/Time": (46305.68039351852, "MM/dd/yyyy hh:mm:ss am/pm"),
                "Cashier": "Unattended",
                "Discount(Discount Number)": "00123",
                "Discount Amount": 15,
                "Customer Name": "N/A",
                "User Full Name": "N/A",
                "Entry Plate Number": "1234ABC",
                "Exit Plate Number": "-",
                "Access ID": 24195508,
            }])

            _make_sample_xlsx(f2, [{
                "Facility": "Makan Parking Co",
                "Car Park": "Mall of Arabia",
                "Entry Device": "E2",
                "Exit Device": "EX2",
                "Entry Date/Time": (46316.99805555555, "MM/dd/yyyy hh:mm:ss am/pm"),
                "Exit Date/Time": (46316.99736111111, "MM/dd/yyyy hh:mm:ss am/pm"),
                "Duration": (-0.001273148148148162, "dd hh:mm:ss"),
                "Ticket Media Type": "KEY_CARD",
                "Serial Number": "14443\\2853586357",
                "Article": "Employees",
                "Article Category": "Contr. Parker",
                "Receipt Number": None,
                "Payment Type": None,
                "Credit Card Number": None,
                "Payment": 0,
                "Payment Device": None,
                "Transaction Date/Time": "-",
                "Cashier": None,
                "Discount(Discount Number)": None,
                "Discount Amount": 0,
                "Customer Name": "CENOMI MOA",
                "User Full Name": "Test User",
                "Entry Plate Number": "-",
                "Exit Plate Number": "-",
                "Access ID": 25143894,
            }], reverse_cols=True)

            total = merge_parking_xlsx([f1, f2], out)
            self.assertEqual(total, 2)

            # Inspect raw XML to verify exact Excel cell types & values
            with zipfile.ZipFile(out, "r") as z:
                with z.open("xl/worksheets/sheet1.xml") as handle:
                    rows = []
                    for _, elem in ET.iterparse(handle, events=("end",)):
                        if elem.tag == f"{_NS}row":
                            row_map = {}
                            for c in elem.findall(f"{_NS}c"):
                                ref = c.attrib["r"]
                                col_letters = "".join(ch for ch in ref if ch.isalpha())
                                c_type = c.attrib.get("t", "n")
                                v_el = c.find(f"{_NS}v")
                                is_el = c.find(f"{_NS}is")
                                val = "".join(is_el.itertext()) if is_el is not None else (v_el.text if v_el is not None else "")
                                row_map[col_letters] = (c_type, val)
                            rows.append(row_map)
                            elem.clear()

            self.assertEqual(len(rows), 3)  # header + 2 data rows
            r1, r2 = rows[1], rows[2]

            # Barcodes & identifiers: strict inlineStr, exact 23-digit string, .0 stripped on Receipt
            self.assertEqual(r1["I"], ("inlineStr", "33741962105024400092681"))
            self.assertEqual(r1["L"], ("inlineStr", "63972"))
            self.assertEqual(r1["S"], ("inlineStr", "00123"))
            self.assertEqual(r2["I"], ("inlineStr", "14443\\2853586357"))

            # Native Excel date serials and '-' preserved
            self.assertEqual(r1["E"][0], "n")
            self.assertAlmostEqual(float(r1["E"][1]), 46297.954375)
            self.assertEqual(r2["Q"], ("inlineStr", "-"))

            # Durations (>= 24h and < 0): raw numeric floats, never 1900-01-XX strings
            self.assertEqual(r1["G"][0], "n")
            self.assertAlmostEqual(float(r1["G"][1]), 7.726018518518519)
            self.assertEqual(r2["G"][0], "n")
            self.assertAlmostEqual(float(r2["G"][1]), -0.001273148148148162)

            # Native numeric Payment, Discount Amount, Access ID
            self.assertEqual(r1["O"], ("n", "240"))
            self.assertEqual(r1["T"], ("n", "15"))
            self.assertEqual(r1["Y"], ("n", "24195508"))
            self.assertEqual(r2["Y"], ("n", "25143894"))

            # 'N/A' preserved cleanly
            self.assertEqual(r1["U"], ("inlineStr", "N/A"))
            self.assertEqual(r1["V"], ("inlineStr", "N/A"))


if __name__ == "__main__":
    unittest.main()
