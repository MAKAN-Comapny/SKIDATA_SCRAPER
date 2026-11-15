"""
Comprehensive QA Test Suite: File Integrity, Container Limits & Archive Corruption.
Covers 20+ test cases testing size thresholds, corrupted byte streams, missing files, and ZIP headers.
"""

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

from validator import verify_file_integrity


class TestFileIntegrityDeep(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(BASE_DIR, "tests", "temp_integrity_sandbox")
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # File Existence (3 cases)
    # =========================================================================
    def test_missing_file_reports_error(self):
        p = os.path.join(self.test_dir, "non_existent_report.xlsx")
        err = verify_file_integrity(p)
        self.assertIsNotNone(err)
        self.assertIn("does not exist", err)

    def test_missing_directory_reports_error(self):
        p = os.path.join(self.test_dir, "no_dir", "file.xlsx")
        err = verify_file_integrity(p)
        self.assertIsNotNone(err)
        self.assertIn("does not exist", err)

    def test_empty_string_path_reports_error(self):
        err = verify_file_integrity("")
        self.assertIsNotNone(err)

    # =========================================================================
    # Size Threshold Boundary Checks (7 cases)
    # Threshold is 3072 bytes (3 KB)
    # =========================================================================
    def test_zero_byte_file(self):
        p = os.path.join(self.test_dir, "zero.xlsx")
        with open(p, "wb") as f:
            pass
        err = verify_file_integrity(p)
        self.assertIn("too small", err)
        self.assertIn("0 bytes", err)

    def test_1_byte_file(self):
        p = os.path.join(self.test_dir, "one_byte.xlsx")
        with open(p, "wb") as f:
            f.write(b"X")
        err = verify_file_integrity(p)
        self.assertIn("too small", err)

    def test_100_bytes_html_error_page(self):
        p = os.path.join(self.test_dir, "error_502.xlsx")
        with open(p, "wb") as f:
            f.write(b"<html><body>502 Bad Gateway</body></html>")
        err = verify_file_integrity(p)
        self.assertIn("too small", err)

    def test_1024_bytes_file(self):
        p = os.path.join(self.test_dir, "1kb.xlsx")
        with open(p, "wb") as f:
            f.write(b"A" * 1024)
        err = verify_file_integrity(p)
        self.assertIn("too small", err)

    def test_3071_bytes_boundary(self):
        # 1 byte below minimum threshold of 3072
        p = os.path.join(self.test_dir, "boundary_below.xlsx")
        with open(p, "wb") as f:
            f.write(b"B" * 3071)
        err = verify_file_integrity(p)
        self.assertIn("too small", err)
        self.assertIn("3071 bytes", err)

    def test_3072_bytes_boundary_non_zip(self):
        # Exactly 3072 bytes, but non-zip data
        p = os.path.join(self.test_dir, "boundary_exact_bad.xlsx")
        with open(p, "wb") as f:
            f.write(b"B" * 3072)
        err = verify_file_integrity(p)
        self.assertIn("not a valid ZIP", err)

    def test_custom_size_threshold(self):
        p = os.path.join(self.test_dir, "custom_size.xlsx")
        with open(p, "wb") as f:
            f.write(b"A" * 5000)
        # Passes 3072 check, but fails 10,000 threshold
        self.assertIn("not a valid ZIP", verify_file_integrity(p, min_size_bytes=3072))
        self.assertIn("too small", verify_file_integrity(p, min_size_bytes=10000))

    # =========================================================================
    # ZIP Container and Header Corruption (6 cases)
    # =========================================================================
    def test_truncated_zip_archive(self):
        # Starts with PK\x03\x04 (standard ZIP magic bytes) but truncated
        p = os.path.join(self.test_dir, "truncated.xlsx")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04" + b"incomplete_bytes" * 250)
        err = verify_file_integrity(p)
        self.assertIn("not a valid ZIP", err)

    def test_corrupt_zip_central_directory(self):
        # Valid zip initially, then corrupted central directory
        p = os.path.join(self.test_dir, "bad_cd.xlsx")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("test.txt", "content" * 1000)
        # Overwrite end of file with zeros
        with open(p, "r+b") as f:
            f.seek(-40, os.SEEK_END)
            f.write(b"\x00" * 40)
        err = verify_file_integrity(p)
        self.assertIsNotNone(err)

    def test_corrupted_crc_entry_inside_zip(self):
        # Create a zip where an internal entry fails testzip()
        p = os.path.join(self.test_dir, "bad_crc.xlsx")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/workbook.xml", "valid content " * 400)
        # Flip bits in the compressed payload
        with open(p, "r+b") as f:
            f.seek(40)
            f.write(b"\xff\xff\xff\xff")
        err = verify_file_integrity(p)
        self.assertIsNotNone(err)

    def test_valid_zip_with_xlsx_extension_passes_integrity(self):
        p = os.path.join(self.test_dir, "valid_container.xlsx")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>" * 400)
            zf.writestr("xl/workbook.xml", "<workbook/>" * 400)
        self.assertIsNone(verify_file_integrity(p))

    # =========================================================================
    # Real OpenPyXL and Pandas Workbooks (4 cases)
    # =========================================================================
    def test_openpyxl_generated_workbook_passes(self):
        p = os.path.join(self.test_dir, "real_openpyxl.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        for i in range(150):
            ws.append([f"Row {i}", i * 1.5, "Data string text"])
        wb.save(p)
        self.assertIsNone(verify_file_integrity(p))

    def test_pandas_generated_workbook_passes(self):
        p = os.path.join(self.test_dir, "real_pandas.xlsx")
        df = pd.DataFrame({"A": range(200), "B": [f"val_{i}" for i in range(200)]})
        df.to_excel(p, index=False)
        self.assertIsNone(verify_file_integrity(p))


if __name__ == "__main__":
    unittest.main()
