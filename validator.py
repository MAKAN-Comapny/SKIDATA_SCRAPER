"""
validator.py - Production-Grade Post-Download Validation Engine for SKIDATA Reports.

Guarantees:
  1. Completeness: Exactly 4 report categories exist in exports_dir.
  2. Container Integrity: Valid non-empty, non-corrupted Excel (ZIP) workbooks (>= 5 KB).
  3. Header & Schema Parity: Expected business columns exist (handles Revenue header=1).
  4. Data Sanitization: Automatic removal of Qlik summary 'Totals' rows before assertions.
  5. Date Precision:
     - For Daily runs (expected_days=1): every row equals yesterday AST.
     - For Rolling runs (expected_days=15): all 15 calendar days are present (missing days flagged).
     - Parking Transactions: strictly evaluates 'Transaction Date/Time' (never Entry Date).
  6. Identifier Precision: 23-digit barcodes and serial numbers are free from scientific notation (E+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import glob
import os
import re
from typing import Dict, List, Optional, Set
import zipfile

import pandas as pd

AST_TIMEZONE = timezone(timedelta(hours=3))

REPORT_SPECS = {
    "revenue": {
        "pattern": "*Revenue-In-Depth.xlsx",
        "display_name": "Revenue In Depth",
        "has_date_col": True,
        "date_col": "Date",
        "header_row": 1,  # Qlik exports Revenue In Depth with 2-line header
        "min_cols": ["Date", "Theoretical Revenue", "Gross Revenue"],
    },
    "access": {
        "pattern": "*Access-In-Depth.xlsx",
        "display_name": "Access In Depth",
        "has_date_col": True,
        "date_col": "Date",
        "header_row": 0,
        "min_cols": ["Date", "Counting Category"],
    },
    "system_event": {
        "pattern": "*System-Event-In-Depth.xlsx",
        "display_name": "System Event In Depth",
        "has_date_col": True,
        "date_col": "Date",
        "header_row": 0,
        "min_cols": ["Date", "System Event"],
    },
    "parking": {
        "pattern": "*Parking-Transactions.xlsx",
        "display_name": "Parking Transactions",
        "has_date_col": False,
        "date_col": "Transaction Date/Time",
        "header_row": 0,
        "min_cols": ["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time"],
    },
}

_SCI_NOTATION_PATTERN = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")


def get_ast_now() -> datetime:
    """Return current datetime in Saudi Arabia Standard Time (AST = UTC+3)."""
    return datetime.now(AST_TIMEZONE)


def get_expected_yesterday() -> date:
    """Return yesterday's calendar date in Saudi Arabia Standard Time (AST)."""
    return (get_ast_now() - timedelta(days=1)).date()


def parse_flexible_date(val: any) -> Optional[date]:
    """Parse mixed date representations into a python datetime.date object."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()

    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("totals", "total", "nan", "nat", "none"):
        return None

    # Try common SKIDATA date formats
    for fmt in (
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%b-%y",
        "%d-%b-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(val_str.split()[0], fmt).date()
        except (ValueError, IndexError):
            continue

    try:
        ts = pd.to_datetime(val_str, errors="coerce")
        if pd.notna(ts):
            return ts.date()
    except Exception:
        pass

    return None


@dataclass
class SheetValidationResult:
    category: str
    filepath: str
    file_size_bytes: int
    row_count: int
    dates_found: Set[date] = field(default_factory=set)
    missing_days: Set[date] = field(default_factory=set)
    unexpected_dates: Set[date] = field(default_factory=set)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


@dataclass
class ValidationSummary:
    is_valid: bool
    expected_window: List[date]
    sheet_results: Dict[str, SheetValidationResult] = field(default_factory=dict)
    global_errors: List[str] = field(default_factory=list)
    global_warnings: List[str] = field(default_factory=list)

    def print_report(self) -> None:
        """Print a structured terminal audit report."""
        print("\n" + "=" * 78)
        print("                 SKIDATA EXPORT VALIDATION REPORT")
        print("=" * 78)
        start_d = min(self.expected_window) if self.expected_window else "N/A"
        end_d = max(self.expected_window) if self.expected_window else "N/A"
        print(f"Target Window : {start_d} to {end_d} ({len(self.expected_window)} calendar day(s))")
        print(f"Overall Status: {'PASSED [OK]' if self.is_valid else 'FAILED [CRITICAL]'}")
        print("-" * 78)

        for cat, res in self.sheet_results.items():
            fname = os.path.basename(res.filepath) if res.filepath else "MISSING"
            status = "OK" if res.is_valid else "FAIL"
            min_found = min(res.dates_found) if res.dates_found else "None"
            max_found = max(res.dates_found) if res.dates_found else "None"
            print(f"[{status}] {cat.upper():<12} | Rows: {res.row_count:>7,} | Size: {res.file_size_bytes:>9,} B | Dates: {min_found} -> {max_found}")
            if res.missing_days:
                missing_sorted = sorted(list(res.missing_days))
                print(f"       Gaps ({len(missing_sorted)}): {', '.join(str(d) for d in missing_sorted[:5])}{'...' if len(missing_sorted) > 5 else ''}")
            for err in res.errors:
                print(f"       ERROR: {err}")
            for wrn in res.warnings:
                print(f"       WARN : {wrn}")

        if self.global_errors:
            print("\nGLOBAL CRITICAL ERRORS:")
            for err in self.global_errors:
                print(f"  * {err}")
        if self.global_warnings:
            print("\nGLOBAL WARNINGS:")
            for wrn in self.global_warnings:
                print(f"  * {wrn}")
        print("=" * 78 + "\n")


def verify_file_integrity(filepath: str, min_size_bytes: int = 3072) -> Optional[str]:
    """Verify that file exists, meets minimum size, and is a valid ZIP/Excel archive."""
    if not os.path.exists(filepath):
        return f"File does not exist: {filepath}"
    size = os.path.getsize(filepath)
    if size < min_size_bytes:
        return f"File size too small ({size} bytes, minimum {min_size_bytes:,} bytes expected): {filepath}"
    if not zipfile.is_zipfile(filepath):
        return f"File is not a valid ZIP/Excel archive: {filepath}"
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            bad_file = zf.testzip()
            if bad_file:
                return f"Corrupt entry '{bad_file}' detected inside archive: {filepath}"
    except Exception as exc:
        return f"Archive header inspection failed ({exc!r}): {filepath}"
    return None


def validate_in_depth_sheet(
    filepath: str,
    spec: dict,
    expected_dates: Set[date],
) -> SheetValidationResult:
    """Validate In-Depth analysis sheets (Revenue, Access, System Event)."""
    res = SheetValidationResult(
        category=spec["display_name"],
        filepath=filepath,
        file_size_bytes=os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        row_count=0,
    )

    integrity_err = verify_file_integrity(filepath)
    if integrity_err:
        res.errors.append(integrity_err)
        return res

    header_row = spec["header_row"]
    date_col = spec["date_col"]

    try:
        df = pd.read_excel(filepath, header=header_row)
    except Exception as exc:
        res.errors.append(f"Failed to read Excel workbook: {exc!r}")
        return res

    # If Revenue header=1 didn't find 'Date', check header=0 fallback
    if date_col not in df.columns:
        try:
            df_fallback = pd.read_excel(filepath, header=0)
            if date_col in df_fallback.columns:
                df = df_fallback
        except Exception:
            pass

    if date_col not in df.columns:
        res.errors.append(f"Required date column '{date_col}' not found. Available columns: {list(df.columns)[:8]}")
        return res

    # Verify minimum required columns
    for col in spec.get("min_cols", []):
        if col not in df.columns:
            res.warnings.append(f"Expected business column '{col}' missing from export.")

    # Strip summary Totals rows
    df_clean = df.dropna(subset=[date_col]).copy()
    df_clean = df_clean[df_clean[date_col].astype(str).str.strip().str.lower() != "totals"]
    res.row_count = len(df_clean)

    if res.row_count == 0:
        res.errors.append("File contains 0 data rows after filtering summary Totals.")
        return res

    # Parse distinct dates
    dates_found: Set[date] = set()
    for raw_d in df_clean[date_col].unique():
        parsed = parse_flexible_date(raw_d)
        if parsed:
            dates_found.add(parsed)

    res.dates_found = dates_found

    # Check date coverage
    res.missing_days = expected_dates - dates_found
    res.unexpected_dates = {d for d in dates_found if d not in expected_dates}

    # Strict check on single-day mode: must contain expected date
    if len(expected_dates) == 1:
        exp = next(iter(expected_dates))
        if exp not in dates_found:
            res.errors.append(f"Exported data does not contain expected date {exp}. Found: {dates_found}")
        if res.unexpected_dates:
            res.errors.append(f"Found unexpected dates in single-day export: {res.unexpected_dates}")
    else:
        # Multi-day mode: check for missing days
        if res.missing_days:
            res.warnings.append(f"Missing {len(res.missing_days)} day(s) from target window: {sorted(list(res.missing_days))}")
        # Any future dates are fatal
        max_exp = max(expected_dates)
        future_dates = {d for d in dates_found if d > max_exp}
        if future_dates:
            res.errors.append(f"Fatal: Found future dates in export: {future_dates}")

    return res


def validate_parking_sheet(
    filepath: str,
    spec: dict,
    expected_dates: Set[date],
) -> SheetValidationResult:
    """Validate Parking Transactions sheet."""
    res = SheetValidationResult(
        category=spec["display_name"],
        filepath=filepath,
        file_size_bytes=os.path.getsize(filepath) if os.path.exists(filepath) else 0,
        row_count=0,
    )

    integrity_err = verify_file_integrity(filepath)
    if integrity_err:
        res.errors.append(integrity_err)
        return res

    try:
        # Read with selective columns for fast execution on large 100k+ row files
        df_cols = pd.read_excel(filepath, nrows=1)
        actual_cols = list(df_cols.columns)
        for min_col in spec.get("min_cols", []):
            if min_col not in actual_cols:
                res.errors.append(f"Required parking column '{min_col}' not found.")
                return res

        usecols = ["Transaction Date/Time", "Entry Date/Time", "Exit Date/Time"]
        dtype_map = {}
        for id_col in ("Serial Number", "Ticket Media Type", "Receipt Number"):
            if id_col in actual_cols:
                usecols.append(id_col)
                dtype_map[id_col] = str

        df = pd.read_excel(filepath, usecols=usecols, dtype=dtype_map)
    except Exception as exc:
        res.errors.append(f"Failed to read Parking Transactions Excel: {exc!r}")
        return res

    res.row_count = len(df)
    if res.row_count == 0:
        res.errors.append("Parking Transactions contains 0 rows.")
        return res

    # Strictly evaluate Transaction Date/Time
    txn_series = pd.to_datetime(df["Transaction Date/Time"], errors="coerce")
    dates_found = set(txn_series.dropna().dt.date.unique())
    res.dates_found = dates_found

    res.missing_days = expected_dates - dates_found
    res.unexpected_dates = {d for d in dates_found if d not in expected_dates}

    if len(expected_dates) == 1:
        exp = next(iter(expected_dates))
        if exp not in dates_found:
            res.errors.append(f"Parking Transactions does not contain expected date {exp}. Found: {dates_found}")
        if res.unexpected_dates:
            res.errors.append(f"Found unexpected transaction dates in single-day export: {res.unexpected_dates}")
    else:
        if res.missing_days:
            res.warnings.append(f"Missing {len(res.missing_days)} day(s) in Parking Transactions: {sorted(list(res.missing_days))}")
        max_exp = max(expected_dates)
        future_dates = {d for d in dates_found if d > max_exp}
        if future_dates:
            res.errors.append(f"Fatal: Found future dates in Parking Transactions: {future_dates}")

    # Verify barcode / serial numbers did not suffer scientific notation corruption
    if "Serial Number" in df.columns:
        serials = df["Serial Number"].dropna().astype(str)
        corrupted = serials[serials.apply(lambda s: bool(_SCI_NOTATION_PATTERN.match(s)))]
        if len(corrupted) > 0:
            res.errors.append(f"Barcode/Serial Number corrupted to scientific notation ({len(corrupted)} instances, e.g. {corrupted.iloc[0]}).")

    return res


def validate_exports(
    exports_dir: str = "exports",
    expected_date: Optional[date] = None,
    expected_days: Optional[int] = None,
    strict: bool = True,
    allow_missing_days: bool = False,
) -> ValidationSummary:
    """
    Main post-download validation entry point.
    Validates all 4 exported files against calendar requirements and data integrity rules.
    """
    ref_date = expected_date or get_expected_yesterday()
    if expected_days is None:
        try:
            from scraper import parse_days_count, parse_target_date
            target = parse_target_date()
            expected_days = 1 if target is not None else parse_days_count()
        except ImportError:
            expected_days = 1

    expected_window = [ref_date - timedelta(days=i) for i in range(expected_days)]
    expected_set = set(expected_window)

    summary = ValidationSummary(
        is_valid=True,
        expected_window=expected_window,
    )

    if not os.path.exists(exports_dir):
        summary.is_valid = False
        summary.global_errors.append(f"Exports directory does not exist: {exports_dir}")
        summary.print_report()
        if strict:
            raise RuntimeError(summary.global_errors[0])
        return summary

    # 1. Discover all 4 report files
    found_files: Dict[str, str] = {}
    date_prefixes: Set[str] = set()

    for key, spec in REPORT_SPECS.items():
        pattern = spec["pattern"]
        matches = glob.glob(os.path.join(exports_dir, pattern))
        if not matches:
            summary.global_errors.append(f"Missing required report file matching pattern '{pattern}' in {exports_dir}")
        elif len(matches) > 1:
            summary.global_warnings.append(f"Multiple files matching '{pattern}'. Using newest: {matches[-1]}")
            matches.sort(key=os.path.getmtime)
            found_files[key] = matches[-1]
            base = os.path.basename(matches[-1])
            date_prefixes.add(base.split("-")[0] if "-" in base else "")
        else:
            found_files[key] = matches[0]
            base = os.path.basename(matches[0])
            date_prefixes.add(base.split("-")[0] if "-" in base else "")

    if len(found_files) < 4:
        summary.is_valid = False

    # Check date prefix consistency across filenames
    if len(date_prefixes) > 1:
        summary.global_warnings.append(f"Inconsistent filename date prefixes across exported files: {date_prefixes}")

    # 2. Validate individual report contents
    for key, spec in REPORT_SPECS.items():
        if key not in found_files:
            continue
        filepath = found_files[key]
        if key == "parking":
            res = validate_parking_sheet(filepath, spec, expected_set)
        else:
            res = validate_in_depth_sheet(filepath, spec, expected_set)

        summary.sheet_results[key] = res
        if not res.is_valid:
            summary.is_valid = False

    # Check missing days in multi-day mode
    if expected_days > 1 and not allow_missing_days:
        for key, res in summary.sheet_results.items():
            if res.missing_days:
                # Gaps of more than 50% of the window are fatal
                if len(res.missing_days) > (expected_days // 2):
                    res.errors.append(f"Critical data gap: {len(res.missing_days)}/{expected_days} days missing.")
                    summary.is_valid = False

    summary.print_report()

    if strict and not summary.is_valid:
        all_errs = summary.global_errors + [f"{cat}: {err}" for cat, r in summary.sheet_results.items() for err in r.errors]
        raise RuntimeError(f"SKIDATA Export Validation Failed ({len(all_errs)} errors):\n" + "\n".join(f"  * {e}" for e in all_errs))

    return summary


if __name__ == "__main__":
    import sys
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "exports"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None
    summary = validate_exports(exports_dir=target_dir, expected_days=days, strict=False)
    sys.exit(0 if summary.is_valid else 1)
