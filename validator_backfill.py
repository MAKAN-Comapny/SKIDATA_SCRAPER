"""
Data Validation Gate for SKIDATA Historical & Range Backfill Pipeline.

Inspects staged backfill outputs (and extract/clean manifests) right before
SharePoint upload to enforce extract completeness, Excel cell-type integrity
(text barcodes/IDs, native Excel dates, numeric durations, numbers, and 'N/A'
preservation), Revenue-vs-Transactions financial reconciliation, and duplicate
or timestamp anomaly checks.

Severity policy:
  - hard_failures -> Block SharePoint upload (when validation is enabled) and
                     fail the workflow with detailed diagnostic context.
  - soft_warnings -> Allow SharePoint upload while recording advisories and
                     sample Access IDs / dates into `_validation_report.json`.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

from excel_merge import (
    _DATE_HEADERS,
    _DATE_PARSE_FORMATS,
    _DURATION_HEADERS,
    _NUMERIC_HEADERS,
    _SCI_NOTATION_RE,
    _SPREADSHEET_NS,
    _TRAILING_DOT_ZERO_RE,
    _extract_row_cells,
    _is_text_identifier,
    _load_shared_strings,
    _resolve_first_sheet_path,
)

QLIK_ROW_LIMIT_WARNING = 99_000
EXCEL_EPOCH = datetime(1899, 12, 30)
DUPLICATE_HARD_FAIL_THRESHOLD = 100
RECON_SOFT_WARN_PCT = 3.0
RECON_HARD_FAIL_PCT = 10.0

TABLE_CONFIG = {
    "revenue": ("Revenue in Depth", "Revenue-In-Depth"),
    "access": ("Access in Depth", "Access-In-Depth"),
    "events": ("System Event in Depth", "System-Event-In-Depth"),
    "parking": ("Parking Transactions", "Parking-Transactions"),
}

REQUIRED_PARKING_HEADERS = (
    "Entry Date/Time",
    "Exit Date/Time",
    "Duration",
    "Serial Number",
    "Payment",
    "Transaction Date/Time",
    "Discount Amount",
    "Access ID",
)


def expected_table_keys(sheet_filter: str) -> list[str]:
    """Return canonical table keys expected for a given `--sheet` filter."""
    flt = (sheet_filter or "all").strip().lower()
    if flt == "all":
        return ["revenue", "access", "events", "parking"]
    if flt in ("revenue",):
        return ["revenue"]
    if flt in ("access",):
        return ["access"]
    if flt in ("events", "system_events"):
        return ["events"]
    if flt in ("parking", "transactions"):
        return ["parking"]
    return ["revenue", "access", "events", "parking"]


def _row_dict_to_list(cells: dict[int, tuple[str, str]], width: Optional[int] = None) -> list[tuple[str, str] | None]:
    max_idx = max(cells.keys(), default=-1)
    size = max(max_idx + 1, width or 0)
    out: list[tuple[str, str] | None] = [None] * size
    for idx, val in cells.items():
        if 0 <= idx < size:
            out[idx] = val
    return out


def inspect_xlsx_summary(file_path: str) -> dict[str, Any]:
    """Verify an `.xlsx` file is readable Zip/XML and count rows quickly."""
    if not os.path.exists(file_path):
        return {"readable": False, "error": "File does not exist", "headers": [], "row_count": 0, "file_size": 0}

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return {"readable": False, "error": "File is 0 bytes", "headers": [], "row_count": 0, "file_size": 0}

    try:
        with zipfile.ZipFile(file_path, "r") as archive:
            shared_strings = _load_shared_strings(archive)
            sheet_path = _resolve_first_sheet_path(archive)
            row_tag = f"{_SPREADSHEET_NS}row"
            total_rows = 0
            headers: list[str] = []
            with archive.open(sheet_path) as handle:
                for _, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag == row_tag:
                        if total_rows == 0:
                            raw_cells = _extract_row_cells(elem, shared_strings)
                            max_col = max(raw_cells.keys(), default=-1)
                            headers = [
                                (raw_cells[i][1].strip() if i in raw_cells else "")
                                for i in range(max_col + 1)
                            ]
                        total_rows += 1
                        elem.clear()
        data_rows = max(0, total_rows - 1) if headers else 0
        return {
            "readable": total_rows > 0 and any(h for h in headers),
            "error": None if total_rows > 0 else "Worksheet has 0 rows",
            "headers": headers,
            "row_count": data_rows,
            "file_size": file_size,
        }
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError, FileNotFoundError) as exc:
        return {
            "readable": False,
            "error": f"Corrupt/unreadable xlsx: {exc}",
            "headers": [],
            "row_count": 0,
            "file_size": file_size,
        }


def _parse_excel_serial_datetime(raw_value: str) -> Optional[datetime]:
    try:
        serial = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not (1.0 <= serial <= 120_000.0):
        return None
    try:
        return EXCEL_EPOCH + timedelta(seconds=round(serial * 86_400))
    except OverflowError:
        return None


def _parse_date_only(val: str) -> Optional[date]:
    text = (val or "").strip()
    if not text or text == "-":
        return None
    # Excel numeric serial?
    dt = _parse_excel_serial_datetime(text)
    if dt is not None:
        return dt.date()
    date_part = text.split()[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(date_part, fmt).date()
        except ValueError:
            continue
    return None


def _try_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    text = str(val).strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_iso_date(val: Any) -> Optional[date]:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if not val:
        return None
    text = str(val).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_range_list(start_d: Optional[date], end_d: Optional[date]) -> list[str]:
    if not start_d or not end_d or end_d < start_d:
        return []
    days = (end_d - start_d).days + 1
    return [(start_d + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _parse_hour_bucket(val: Any) -> Optional[str]:
    """Normalize Qlik Hour values (`9`, `'14'`, `'14:00'`, `'02:00 PM'`) into `'00'`..`'23'`."""
    if val is None:
        return None
    text = str(val).strip()
    if not text or text == "-":
        return None
    num = _try_float(text)
    if num is not None and 0 <= int(num) <= 23 and ":" not in text:
        return f"{int(num):02d}"
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I %p"):
        try:
            return f"{datetime.strptime(text.upper(), fmt).hour:02d}"
        except ValueError:
            continue
    return None


def _accumulate_bucket(store: dict[str, dict[str, float]], key: Optional[str], **deltas: float) -> None:
    """Increment numeric fields inside a string-keyed aggregation bucket."""
    if not key:
        return
    bucket = store.setdefault(key, {})
    for field, val in deltas.items():
        if val:
            bucket[field] = bucket.get(field, 0.0) + float(val)


def _merge_bucket_map(dest: dict[str, dict[str, float]], src: Optional[dict[str, dict[str, float]]]) -> None:
    """Merge all numeric buckets from `src` into `dest`."""
    if not src:
        return
    for key, vals in src.items():
        _accumulate_bucket(dest, key, **vals)


_PAYMENT_METHOD_CANONICAL = {
    "surepay": "SurePay",
    "cash": "Cash",
    "other credit card": "Other Credit Card",
}


def _normalize_payment_method(raw_method: str) -> str:
    """Normalize Qlik tender capitalization glitches (e.g. `SurePAy` vs `SurePay`)."""
    cleaned = (raw_method or "").strip()
    if not cleaned or cleaned in ("-", "N/A"):
        return "Other / Unspecified"
    return _PAYMENT_METHOD_CANONICAL.get(cleaned.lower(), cleaned)


def _validate_indepth_workbook(file_path: str, file_type: str) -> dict[str, Any]:
    """Validate Access-In-Depth, Revenue-In-Depth, or System-Event-In-Depth workbook and collect insight aggregates."""
    result: dict[str, Any] = {
        "readable": False,
        "error": None,
        "rows": 0,
        "headers": [],
        "has_date_col": False,
        "totals_rows_remaining": 0,
        "dates": set(),
        "active_dates": set(),
        "revenue_totals": {"quantity": 0.0, "theoretical": 0.0, "gross": 0.0},
        "by_date": {},
        "by_hour": {},
        "by_car_park": {},
    }
    if not os.path.exists(file_path):
        result["error"] = "File missing"
        return result

    try:
        with zipfile.ZipFile(file_path, "r") as archive:
            shared_strings = _load_shared_strings(archive)
            sheet_path = _resolve_first_sheet_path(archive)
            row_tag = f"{_SPREADSHEET_NS}row"

            headers: list[str] = []
            idx_map: dict[str, int] = {}
            is_rev = file_type == "Revenue-In-Depth"
            is_access = file_type == "Access-In-Depth"

            with archive.open(sheet_path) as handle:
                row_num = 0
                for _, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag != row_tag:
                        continue
                    cells = _extract_row_cells(elem, shared_strings)
                    elem.clear()
                    row_num += 1

                    if row_num == 1:
                        max_c = max(cells.keys(), default=-1)
                        headers = [(cells[i][1].strip() if i in cells else "") for i in range(max_c + 1)]
                        idx_map = {h: i for i, h in enumerate(headers) if h}
                        result["headers"] = headers
                        result["has_date_col"] = "Date" in idx_map
                        continue

                    result["rows"] += 1
                    col0 = cells.get(0, ("", ""))[1].strip() if 0 in cells else ""
                    dt_idx = idx_map.get("Date")
                    raw_dt = cells[dt_idx][1].strip() if (dt_idx is not None and dt_idx in cells) else ""
                    if col0.lower() == "totals" or raw_dt.lower() == "totals":
                        result["totals_rows_remaining"] += 1
                        continue

                    row_date_iso: Optional[str] = None
                    if raw_dt:
                        parsed_d = _parse_date_only(raw_dt)
                        if parsed_d is not None:
                            row_date_iso = parsed_d.strftime("%Y-%m-%d")
                            result["dates"].add(row_date_iso)

                    cp_idx = idx_map.get("Car Park")
                    cp_name = cells[cp_idx][1].strip() if (cp_idx is not None and cp_idx in cells) else ""
                    if cp_name in ("-", "N/A"):
                        cp_name = ""
                    hr_idx = idx_map.get("Hour")
                    hr_key = _parse_hour_bucket(cells[hr_idx][1]) if (hr_idx is not None and hr_idx in cells) else None

                    if is_rev:
                        iq = idx_map.get("Quantity")
                        it = idx_map.get("Theoretical Revenue")
                        ig = idx_map.get("Gross Revenue")
                        q_val = _try_float(cells[iq][1]) if (iq is not None and iq in cells) else 0.0
                        t_val = _try_float(cells[it][1]) if (it is not None and it in cells) else 0.0
                        g_val = _try_float(cells[ig][1]) if (ig is not None and ig in cells) else 0.0
                        q_num = q_val or 0.0
                        t_num = t_val or 0.0
                        g_num = g_val or 0.0
                        result["revenue_totals"]["quantity"] += q_num
                        result["revenue_totals"]["theoretical"] += t_num
                        result["revenue_totals"]["gross"] += g_num
                        if row_date_iso and q_num > 0:
                            result["active_dates"].add(row_date_iso)
                        _accumulate_bucket(result["by_date"], row_date_iso, revenue_gross=g_num, revenue_theoretical=t_num, revenue_qty=q_num)
                        _accumulate_bucket(result["by_hour"], hr_key, revenue_gross=g_num)
                        _accumulate_bucket(result["by_car_park"], cp_name, revenue_gross=g_num, revenue_theoretical=t_num, revenue_qty=q_num)
                    elif is_access:
                        if row_date_iso:
                            result["active_dates"].add(row_date_iso)
                        ie = idx_map.get("Entry Count")
                        ix = idx_map.get("Exit Count")
                        en_num = (_try_float(cells[ie][1]) if (ie is not None and ie in cells) else 0.0) or 0.0
                        ex_num = (_try_float(cells[ix][1]) if (ix is not None and ix in cells) else 0.0) or 0.0
                        _accumulate_bucket(result["by_date"], row_date_iso, access_entries=en_num, access_exits=ex_num)
                        _accumulate_bucket(result["by_hour"], hr_key, access_entries=en_num)
                        _accumulate_bucket(result["by_car_park"], cp_name, access_entries=en_num, access_exits=ex_num)
                    elif row_date_iso:
                        result["active_dates"].add(row_date_iso)

        result["readable"] = True
        return result
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError, FileNotFoundError) as exc:
        result["error"] = f"Corrupt/unreadable xlsx: {exc}"
        return result


def _validate_parking_workbook(
    file_path: str,
    start_date: Optional[date],
    end_date: Optional[date],
) -> dict[str, Any]:
    """Stream-validate every row & XML cell type in `Parking-Transactions.xlsx`."""
    res: dict[str, Any] = {
        "readable": False,
        "error": None,
        "rows": 0,
        "headers": [],
        "missing_required_headers": [],
        "payment_sum": 0.0,
        "discount_sum": 0.0,
        "dates_seen": set(),
        "violation_counts": {
            "barcodes": 0,
            "dates": 0,
            "durations": 0,
            "numbers": 0,
            "na_stripped": 0,
        },
        "violation_samples": [],
        "na_count": 0,
        "na_col_stats": {},
        "inverted_exit_lt_entry": [],
        "out_of_range_txn_dates": [],
        "duplicate_access_ids": [],
        "duplicate_count": 0,
        "by_date": {},
        "by_hour": {},
        "by_car_park": {},
        "payment_methods": {},
        "paid_rows": 0,
        "free_rows": 0,
    }
    if not os.path.exists(file_path):
        res["error"] = "File missing"
        return res

    tol_start = (start_date - timedelta(days=1)) if start_date else None
    tol_end = (end_date + timedelta(days=1)) if end_date else None
    composite_seen: Counter[tuple[str, str, str]] = Counter()
    max_samples = 15

    try:
        with zipfile.ZipFile(file_path, "r") as archive:
            shared_strings = _load_shared_strings(archive)
            sheet_path = _resolve_first_sheet_path(archive)
            row_tag = f"{_SPREADSHEET_NS}row"

            headers: list[str] = []
            idx_map: dict[str, int] = {}
            text_id_cols: list[tuple[int, str]] = []
            date_cols: list[tuple[int, str]] = []
            dur_cols: list[tuple[int, str]] = []
            num_cols: list[tuple[int, str]] = []
            na_cols: dict[str, dict[str, int]] = {}

            with archive.open(sheet_path) as handle:
                row_num = 0
                for _, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag != row_tag:
                        continue
                    cells = _extract_row_cells(elem, shared_strings)
                    elem.clear()
                    row_num += 1

                    if row_num == 1:
                        max_c = max(cells.keys(), default=-1)
                        headers = [(cells[i][1].strip() if i in cells else "") for i in range(max_c + 1)]
                        idx_map = {h: i for i, h in enumerate(headers) if h}
                        res["headers"] = headers
                        res["missing_required_headers"] = [h for h in REQUIRED_PARKING_HEADERS if h not in idx_map]
                        for col_i, h in enumerate(headers):
                            if not h:
                                continue
                            if _is_text_identifier(h):
                                text_id_cols.append((col_i, h))
                            elif h in _DATE_HEADERS:
                                date_cols.append((col_i, h))
                            elif h in _DURATION_HEADERS:
                                dur_cols.append((col_i, h))
                            elif h in _NUMERIC_HEADERS:
                                num_cols.append((col_i, h))
                            if h in ("Customer Name", "User Full Name"):
                                na_cols[h] = {"na": 0, "null": 0, "other": 0}
                        continue

                    res["rows"] += 1
                    excel_row = row_num

                    # Access ID for diagnostic context
                    acc_idx = idx_map.get("Access ID")
                    acc_display = cells[acc_idx][1].strip() if (acc_idx is not None and acc_idx in cells) else f"row#{excel_row}"

                    # 1. Text barcode / ID integrity
                    for col_i, col_name in text_id_cols:
                        cell = cells.get(col_i)
                        if cell is None:
                            continue
                        raw_type, raw_val = cell
                        val = raw_val.strip()
                        if not val or val == "-":
                            continue
                        bad_reason = None
                        if raw_type in ("n", ""):
                            bad_reason = f"numeric type instead of text ({val})"
                        elif _SCI_NOTATION_RE.match(val):
                            bad_reason = f"scientific notation ({val})"
                        elif _TRAILING_DOT_ZERO_RE.match(val):
                            bad_reason = f"trailing .0 artifact ({val})"
                        if bad_reason:
                            res["violation_counts"]["barcodes"] += 1
                            if len(res["violation_samples"]) < max_samples:
                                res["violation_samples"].append(
                                    f"Row {excel_row} [{col_name}]: {bad_reason}"
                                )

                    # 2. Native dates
                    parsed_dates: dict[str, Optional[datetime]] = {}
                    for col_i, col_name in date_cols:
                        cell = cells.get(col_i)
                        if cell is None:
                            parsed_dates[col_name] = None
                            continue
                        raw_type, raw_val = cell
                        val = raw_val.strip()
                        if not val or val == "-":
                            parsed_dates[col_name] = None
                            continue
                        dt_obj: Optional[datetime] = None
                        if raw_type in ("n", ""):
                            dt_obj = _parse_excel_serial_datetime(val)
                        elif raw_type == "d":
                            for fmt in _DATE_PARSE_FORMATS:
                                try:
                                    dt_obj = datetime.strptime(val.replace("T", " "), fmt)
                                    break
                                except ValueError:
                                    pass
                        else:
                            # Stringified timestamp (broken pandas output)
                            res["violation_counts"]["dates"] += 1
                            if len(res["violation_samples"]) < max_samples:
                                res["violation_samples"].append(
                                    f"Row {excel_row} [{col_name}]: stored as text '{val}' instead of Excel date"
                                )
                            parsed_dates[col_name] = None
                            continue

                        if dt_obj is None:
                            res["violation_counts"]["dates"] += 1
                            if len(res["violation_samples"]) < max_samples:
                                res["violation_samples"].append(
                                    f"Row {excel_row} [{col_name}]: invalid Excel date serial '{val}'"
                                )
                        parsed_dates[col_name] = dt_obj

                    # 3. Numeric Duration (must not be string / 1900-1899 text)
                    for col_i, col_name in dur_cols:
                        cell = cells.get(col_i)
                        if cell is None:
                            continue
                        raw_type, raw_val = cell
                        val = raw_val.strip()
                        if not val or val == "-":
                            continue
                        if raw_type not in ("n", "") or _try_float(val) is None:
                            res["violation_counts"]["durations"] += 1
                            if len(res["violation_samples"]) < max_samples:
                                res["violation_samples"].append(
                                    f"Row {excel_row} [{col_name}]: text/corrupted duration '{val}' (type={raw_type or 'blank'})"
                                )

                    # 4. Numeric Payment / Discount Amount / Access ID
                    row_pay = 0.0
                    row_disc = 0.0
                    for col_i, col_name in num_cols:
                        cell = cells.get(col_i)
                        if cell is None:
                            continue
                        raw_type, raw_val = cell
                        val = raw_val.strip()
                        if not val or val == "-":
                            continue
                        num_val = _try_float(val) if raw_type in ("n", "") else None
                        if num_val is None:
                            res["violation_counts"]["numbers"] += 1
                            if len(res["violation_samples"]) < max_samples:
                                res["violation_samples"].append(
                                    f"Row {excel_row} [{col_name}]: non-numeric value '{val}' (type={raw_type or 'blank'})"
                                )
                        else:
                            if col_name == "Payment":
                                res["payment_sum"] += num_val
                                row_pay += num_val
                            elif col_name == "Discount Amount":
                                res["discount_sum"] += num_val
                                row_disc += num_val

                    # 5. Track 'N/A' vs null for Customer Name / User Full Name
                    for col_name, stats in na_cols.items():
                        ci = idx_map[col_name]
                        cell = cells.get(ci)
                        if cell is None or not cell[1].strip():
                            stats["null"] += 1
                        elif cell[1].strip() == "N/A":
                            stats["na"] += 1
                            res["na_count"] += 1
                        else:
                            stats["other"] += 1

                    # 6. Coverage date & timestamp checks + Insights aggregation
                    txn_dt = parsed_dates.get("Transaction Date/Time") or parsed_dates.get("Exit Date/Time")
                    if txn_dt is not None:
                        txn_d = txn_dt.date()
                        res["dates_seen"].add(txn_d.strftime("%Y-%m-%d"))
                        if tol_start and tol_end and (txn_d < tol_start or txn_d > tol_end):
                            if len(res["out_of_range_txn_dates"]) < 25:
                                res["out_of_range_txn_dates"].append(f"{acc_display} ({txn_d})")
                            else:
                                res["out_of_range_txn_dates"].append("__more__")

                    en_dt = parsed_dates.get("Entry Date/Time")
                    ex_dt = parsed_dates.get("Exit Date/Time")
                    if en_dt is not None and ex_dt is not None and ex_dt < en_dt:
                        if len(res["inverted_exit_lt_entry"]) < 25:
                            res["inverted_exit_lt_entry"].append(acc_display)
                        else:
                            res["inverted_exit_lt_entry"].append("__more__")

                    is_paid = 1 if row_pay > 0 else 0
                    if is_paid:
                        res["paid_rows"] += 1
                    else:
                        res["free_rows"] += 1

                    insight_dt = txn_dt or ex_dt or en_dt
                    if insight_dt is not None:
                        _accumulate_bucket(
                            res["by_date"],
                            insight_dt.strftime("%Y-%m-%d"),
                            parking_rows=1,
                            paid_rows=is_paid,
                            parking_payment=row_pay,
                            parking_discount=row_disc,
                        )
                        _accumulate_bucket(
                            res["by_hour"],
                            f"{insight_dt.hour:02d}",
                            parking_rows=1,
                            paid_rows=is_paid,
                            parking_payment=row_pay,
                        )

                    cp_idx = idx_map.get("Car Park")
                    cp_val = cells[cp_idx][1].strip() if (cp_idx is not None and cp_idx in cells) else ""
                    if cp_val and cp_val not in ("-", "N/A"):
                        _accumulate_bucket(
                            res["by_car_park"],
                            cp_val,
                            parking_rows=1,
                            paid_rows=is_paid,
                            parking_payment=row_pay,
                            parking_discount=row_disc,
                        )

                    if is_paid:
                        pt_idx = idx_map.get("Payment Type")
                        raw_ptype = cells[pt_idx][1] if (pt_idx is not None and pt_idx in cells) else ""
                        _accumulate_bucket(
                            res["payment_methods"],
                            _normalize_payment_method(raw_ptype),
                            count=1,
                            amount=row_pay,
                        )

                    # 7. Composite duplicate tracking
                    ser_idx = idx_map.get("Serial Number")
                    ser_val = cells[ser_idx][1].strip() if (ser_idx is not None and ser_idx in cells) else ""
                    td_idx = idx_map.get("Transaction Date/Time")
                    td_key = (
                        txn_dt.strftime("%Y-%m-%d %H:%M:%S")
                        if txn_dt is not None
                        else (cells[td_idx][1].strip() if (td_idx is not None and td_idx in cells) else "")
                    )
                    comp_key = (acc_display, td_key, ser_val)
                    composite_seen[comp_key] += 1

        # Finalize duplicate count
        dup_ids: list[str] = []
        dup_total = 0
        for (aid, _, _), cnt in composite_seen.items():
            if cnt > 1:
                dup_total += cnt - 1
                if len(dup_ids) < 20:
                    dup_ids.append(f"{aid} (x{cnt})")
        res["duplicate_count"] = dup_total
        res["duplicate_access_ids"] = dup_ids
        res["na_col_stats"] = na_cols

        # Evaluate N/A preservation if Customer Name / User Full Name exist and rows >= 20
        if res["rows"] >= 20 and na_cols:
            for col_name, stats in na_cols.items():
                if stats["na"] == 0 and stats["null"] > res["rows"] * 0.5:
                    res["violation_counts"]["na_stripped"] += stats["null"]
                    if len(res["violation_samples"]) < max_samples:
                        res["violation_samples"].append(
                            f"[{col_name}]: 0 'N/A' values found and {stats['null']} blank/null rows (likely stripped by pandas na_filter)"
                        )

        res["readable"] = True
        return res
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError, FileNotFoundError) as exc:
        res["error"] = f"Corrupt/unreadable xlsx: {exc}"
        return res


def _build_table_entry(
    key: str,
    prefix: str,
    cleaned_dir: str,
    expected_keys: list[str],
    manifest: dict[str, Any],
    clean_meta: dict[str, Any],
) -> tuple[str, str, Optional[str]]:
    display_name, file_base = TABLE_CONFIG[key]
    if key not in expected_keys:
        return display_name, "skipped", None
    target_file = os.path.join(cleaned_dir, f"{prefix}-{file_base}.xlsx")
    return display_name, file_base, target_file if os.path.exists(target_file) else None


def validate_backfill_run(
    spec: dict[str, Any],
    cleaned_dir: str,
    exports_dir: str,
    sheet_filter: str = "all",
    validate_enabled: bool = True,
    scrape_error: Optional[str] = None,
    manifest: Optional[dict[str, Any]] = None,
    clean_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Execute the full 4-tier Validation Gate over one backfill batch (`spec`).

    When `validate_enabled=False`, hard gating checks are bypassed so urgent
    extractions still upload, while producing a clear `'bypassed'` audit manifest.
    """
    start_d = _parse_iso_date(spec.get("start_date"))
    end_d = _parse_iso_date(spec.get("end_date"))
    prefix = spec.get("file_prefix", "")
    expected_dates = _date_range_list(start_d, end_d)
    expected_keys = expected_table_keys(sheet_filter)

    # Load disk manifests if not injected directly by tests/caller
    if manifest is None:
        m_path = os.path.join(exports_dir, "_scrape_manifest.json")
        if os.path.exists(m_path):
            try:
                with open(m_path, "r", encoding="utf-8") as fh:
                    manifest = json.load(fh)
            except Exception:
                manifest = {}
        else:
            manifest = {}

    if clean_meta is None:
        cm_path = os.path.join(cleaned_dir, "_clean_meta.json")
        if os.path.exists(cm_path):
            try:
                with open(cm_path, "r", encoding="utf-8") as fh:
                    clean_meta = json.load(fh)
            except Exception:
                clean_meta = {}
        else:
            clean_meta = {}

    hard_failures: list[dict[str, Any]] = []
    soft_warnings: list[dict[str, Any]] = []
    tables_report: dict[str, dict[str, Any]] = {}

    def add_hard(code: str, category: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
        hard_failures.append({
            "code": code,
            "severity": "hard",
            "category": category,
            "message": message,
            "details": details or {},
        })

    def add_soft(code: str, category: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
        soft_warnings.append({
            "code": code,
            "severity": "soft",
            "category": category,
            "message": message,
            "details": details or {},
        })

    if not validate_enabled:
        report = {
            "version": "1.0",
            "generated_at": _utc_now_iso(),
            "batch_name": spec.get("name", prefix),
            "file_prefix": prefix,
            "sharepoint_folder": spec.get("sharepoint_folder", ""),
            "start_date": start_d.strftime("%Y-%m-%d") if start_d else "",
            "end_date": end_d.strftime("%Y-%m-%d") if end_d else "",
            "sheet_filter": sheet_filter,
            "validation_enabled": False,
            "status": "bypassed",
            "upload_blocked": False,
            "upload_performed": False,
            "scrape_error": scrape_error,
            "summary": "Validation Gate bypassed by user toggle (time-constraint mode). Files staged directly for upload.",
            "tables": {
                TABLE_CONFIG[k][0]: {
                    "status": (
                        "done"
                        if os.path.exists(os.path.join(cleaned_dir, f"{prefix}-{TABLE_CONFIG[k][1]}.xlsx"))
                        else ("skipped" if k not in expected_keys else "failed")
                    ),
                    "file": f"{prefix}-{TABLE_CONFIG[k][1]}.xlsx" if k in expected_keys else None,
                    "rows": (
                        inspect_xlsx_summary(os.path.join(cleaned_dir, f"{prefix}-{TABLE_CONFIG[k][1]}.xlsx"))["row_count"]
                        if os.path.exists(os.path.join(cleaned_dir, f"{prefix}-{TABLE_CONFIG[k][1]}.xlsx"))
                        else 0
                    ),
                    "message": "Validation bypassed",
                }
                for k in ("revenue", "access", "events", "parking")
            },
            "checks": {
                "completeness": {"status": "bypassed", "expected_days": len(expected_dates), "covered_days": 0},
                "structure_and_types": {"status": "bypassed"},
                "reconciliation": {"status": "bypassed"},
                "anomalies": {"status": "bypassed"},
            },
            "hard_failures": [],
            "soft_warnings": [
                {
                    "code": "GATE_BYPASSED",
                    "severity": "soft",
                    "category": "policy",
                    "message": "Validation Gate was switched OFF by operator; schema, completeness, and financial checks were skipped.",
                    "details": {},
                }
            ],
        }
        return report

    # ---- Inspect caught scrape & clean exceptions ----
    if scrape_error:
        add_hard("SCRAPE_EXCEPTION_CAUGHT", "completeness", f"Scraper execution raised an exception: {scrape_error}")

    table_errors = manifest.get("table_errors") or {}
    for tbl_key, err_msg in table_errors.items():
        if tbl_key in expected_keys and err_msg:
            disp = TABLE_CONFIG.get(tbl_key, (tbl_key, tbl_key))[0]
            add_hard(
                "TABLE_SCRAPE_FAILED",
                "completeness",
                f"{disp} extraction failed: {err_msg}",
                {"table": disp, "error": err_msg},
            )

    clean_errors = clean_meta.get("clean_errors") or {}
    for fname, err_msg in clean_errors.items():
        add_hard(
            "CLEAN_STAGE_FAILED",
            "structure",
            f"Cleaning failed for {fname} (strict mode blocked raw file copy): {err_msg}",
            {"file": fname, "error": err_msg},
        )

    # ---- Inspect workbooks ----
    indepth_results: dict[str, dict[str, Any]] = {}
    for key in ("revenue", "access", "events"):
        disp, file_base = TABLE_CONFIG[key]
        if key not in expected_keys:
            tables_report[disp] = {"status": "skipped", "file": None, "rows": 0, "message": "Not requested in sheet filter"}
            continue
        fp = os.path.join(cleaned_dir, f"{prefix}-{file_base}.xlsx")
        if not os.path.exists(fp):
            tables_report[disp] = {"status": "failed", "file": f"{prefix}-{file_base}.xlsx", "rows": 0, "message": "Expected file missing"}
            add_hard("MISSING_EXPECTED_FILE", "completeness", f"Expected report '{os.path.basename(fp)}' is missing.")
            continue

        info = _validate_indepth_workbook(fp, file_base)
        indepth_results[key] = info
        if not info["readable"]:
            tables_report[disp] = {"status": "failed", "file": os.path.basename(fp), "rows": 0, "message": info["error"]}
            add_hard("UNREADABLE_XLSX", "completeness", f"{os.path.basename(fp)} cannot be read: {info['error']}")
            continue

        tbl_issues = []
        if not info["has_date_col"]:
            tbl_issues.append("Missing 'Date' column")
            add_hard("MISSING_DATE_COLUMN", "structure", f"{os.path.basename(fp)} is missing required 'Date' column (possible uncleaned header).")
        if info["totals_rows_remaining"] > 0:
            tbl_issues.append(f"{info['totals_rows_remaining']} 'Totals' row(s) left in file")
            add_hard("TOTALS_ROW_PRESENT", "structure", f"{os.path.basename(fp)} still contains {info['totals_rows_remaining']} 'Totals' row(s).")
        if info["rows"] == 0:
            tbl_issues.append("0 data rows")
            add_hard("EMPTY_TABLE_EXPORT", "completeness", f"{os.path.basename(fp)} contains 0 data rows.")

        tables_report[disp] = {
            "status": "failed" if tbl_issues else "done",
            "file": os.path.basename(fp),
            "rows": info["rows"],
            "totals_removed": info["totals_rows_remaining"] == 0,
            "message": "; ".join(tbl_issues) if tbl_issues else f"Verified ({info['rows']:,} rows)",
        }

    # ---- Parking Transactions ----
    pt_info: Optional[dict[str, Any]] = None
    pt_disp, pt_base = TABLE_CONFIG["parking"]
    if "parking" not in expected_keys:
        tables_report[pt_disp] = {"status": "skipped", "file": None, "rows": 0, "message": "Not requested in sheet filter"}
    else:
        pt_path = os.path.join(cleaned_dir, f"{prefix}-{pt_base}.xlsx")
        if not os.path.exists(pt_path):
            tables_report[pt_disp] = {"status": "failed", "file": f"{prefix}-{pt_base}.xlsx", "rows": 0, "message": "Expected file missing"}
            add_hard("MISSING_EXPECTED_FILE", "completeness", f"Expected report '{os.path.basename(pt_path)}' is missing.")
        else:
            pt_info = _validate_parking_workbook(pt_path, start_d, end_d)
            if not pt_info["readable"]:
                tables_report[pt_disp] = {"status": "failed", "file": os.path.basename(pt_path), "rows": 0, "message": pt_info["error"]}
                add_hard("UNREADABLE_XLSX", "completeness", f"{os.path.basename(pt_path)} cannot be read: {pt_info['error']}")
            else:
                if pt_info["missing_required_headers"]:
                    add_hard(
                        "MISSING_REQUIRED_COLUMNS",
                        "structure",
                        f"Parking-Transactions missing columns: {', '.join(pt_info['missing_required_headers'])}",
                        {"missing": pt_info["missing_required_headers"]},
                    )
                if pt_info["rows"] == 0:
                    add_hard("EMPTY_TABLE_EXPORT", "completeness", f"{os.path.basename(pt_path)} contains 0 rows.")

                vc = pt_info["violation_counts"]
                if vc["barcodes"] > 0:
                    add_hard(
                        "BARCODE_TEXT_CORRUPTION",
                        "structure",
                        f"{vc['barcodes']} barcode/ID cell(s) failed strict text integrity (numeric type, sci-notation, or .0).",
                        {"count": vc["barcodes"], "samples": pt_info["violation_samples"][:5]},
                    )
                if vc["dates"] > 0:
                    add_hard(
                        "DATE_NOT_NATIVE_EXCEL",
                        "structure",
                        f"{vc['dates']} date cell(s) stored as text instead of native Excel datetime.",
                        {"count": vc["dates"], "samples": pt_info["violation_samples"][:5]},
                    )
                if vc["durations"] > 0:
                    add_hard(
                        "DURATION_CORRUPTED_STRING",
                        "structure",
                        f"{vc['durations']} Duration cell(s) stored as text/1900-1899 string instead of numeric Excel duration.",
                        {"count": vc["durations"], "samples": pt_info["violation_samples"][:5]},
                    )
                if vc["numbers"] > 0:
                    add_hard(
                        "NUMERIC_FIELD_TEXT",
                        "structure",
                        f"{vc['numbers']} Payment/Discount/Access ID cell(s) are not valid Excel numbers.",
                        {"count": vc["numbers"], "samples": pt_info["violation_samples"][:5]},
                    )
                if vc["na_stripped"] > 0:
                    add_hard(
                        "NA_VALUES_STRIPPED",
                        "structure",
                        f"'N/A' values appear stripped to blanks ({vc['na_stripped']} null rows in Customer/User Full Name with 0 'N/A' strings).",
                        {"na_col_stats": pt_info["na_col_stats"]},
                    )

    # ---- Tier 1: Completeness & Qlik 100k Cap ----
    parking_manifest = manifest.get("parking") or {}
    portal_empty_days = sorted(set(parking_manifest.get("portal_empty_dates") or []))
    failed_days = sorted(set((parking_manifest.get("failed_dates") or {}).keys()))
    scraped_days = set(parking_manifest.get("scraped_dates") or [])
    chunk_rows = parking_manifest.get("chunk_row_counts") or {}
    dup_chunks = parking_manifest.get("duplicate_chunks_detected") or []

    max_export_rows = 0
    if parking_manifest.get("full_export_row_count"):
        max_export_rows = max(max_export_rows, int(parking_manifest["full_export_row_count"]))
    if chunk_rows:
        max_export_rows = max(max_export_rows, *(int(v) for v in chunk_rows.values()))
    if not chunk_rows and pt_info and parking_manifest.get("mode") != "daily_chunks":
        max_export_rows = max(max_export_rows, int(pt_info["rows"]))

    qlik_100k_risk = max_export_rows >= QLIK_ROW_LIMIT_WARNING
    if qlik_100k_risk:
        add_hard(
            "QLIK_100K_CAP_RISK",
            "completeness",
            f"Export row count ({max_export_rows:,}) reached Qlik Sense 100k calculation cap threshold ({QLIK_ROW_LIMIT_WARNING:,}). Data may be silently truncated.",
            {"max_export_rows": max_export_rows},
        )

    if dup_chunks:
        add_hard(
            "DUPLICATE_DAY_CHUNK",
            "completeness",
            f"Duplicate day chunk files detected during extraction: {', '.join(str(x) for x in dup_chunks)}",
            {"duplicates": dup_chunks},
        )

    missing_days: list[str] = []
    if "parking" in expected_keys and expected_dates:
        p_mode = parking_manifest.get("mode")
        if p_mode == "daily_chunks":
            missing_days = [
                d for d in expected_dates if d not in scraped_days and d not in portal_empty_days
            ]
        elif p_mode == "full_range" and pt_info and pt_info["readable"]:
            covered_from_data = pt_info["dates_seen"]
            rev_active = indepth_results.get("revenue", {}).get("active_dates", set())
            missing_days = sorted(
                d for d in expected_dates
                if d in failed_days or (d not in covered_from_data and d in rev_active)
            )
            for d in expected_dates:
                if d not in covered_from_data and d not in rev_active and d not in portal_empty_days:
                    portal_empty_days.append(d)
        else:
            covered_from_data = pt_info["dates_seen"] if (pt_info and pt_info["readable"]) else set()
            missing_days = sorted(
                d for d in expected_dates
                if d in failed_days or (d not in covered_from_data and d not in portal_empty_days)
            )

    if failed_days or missing_days:
        all_missing = sorted(set(failed_days) | set(missing_days))
        add_hard(
            "PARTIAL_DATA_MISSING_DAYS",
            "completeness",
            f"Partial backfill detected: missing Parking-Transactions data for {len(all_missing)} day(s): {', '.join(all_missing[:10])}",
            {"missing_days": all_missing, "failed_days": parking_manifest.get("failed_dates") or {}},
        )

    if portal_empty_days:
        add_soft(
            "PORTAL_ZERO_DATA_DAYS",
            "completeness",
            f"Qlik portal returned 0 records for {len(portal_empty_days)} calendar day(s) (downtime/no transactions): {', '.join(portal_empty_days[:10])}",
            {"portal_empty_days": portal_empty_days},
        )

    covered_days_count = (
        len(set(expected_dates) & scraped_days)
        if parking_manifest.get("mode") == "daily_chunks" and scraped_days
        else (len(set(expected_dates) & pt_info["dates_seen"]) if pt_info and pt_info["readable"] else 0)
    )

    # ---- Tier 3: Revenue vs Transactions Reconciliation ----
    rev_info = indepth_results.get("revenue")
    rev_gross = rev_info["revenue_totals"]["gross"] if (rev_info and rev_info["readable"]) else 0.0
    rev_theo = rev_info["revenue_totals"]["theoretical"] if (rev_info and rev_info["readable"]) else 0.0
    rev_qty = int(round(rev_info["revenue_totals"]["quantity"])) if (rev_info and rev_info["readable"]) else 0
    pt_payment = pt_info["payment_sum"] if (pt_info and pt_info["readable"]) else 0.0
    pt_discount = pt_info["discount_sum"] if (pt_info and pt_info["readable"]) else 0.0
    pt_rows = pt_info["rows"] if (pt_info and pt_info["readable"]) else 0

    pre_clean_totals_match = True
    pre_clean_map = (clean_meta.get("revenue_pre_clean_totals") or {}).get(f"{prefix}-Revenue-In-Depth.xlsx")
    if rev_info and rev_info["readable"] and pre_clean_map:
        raw_g = _try_float(pre_clean_map.get("Gross Revenue"))
        raw_q = _try_float(pre_clean_map.get("Quantity"))
        if raw_g is not None and abs(rev_gross - raw_g) > max(1.0, abs(raw_g) * 0.005):
            pre_clean_totals_match = False
            add_hard(
                "PRE_CLEAN_TOTALS_MISMATCH",
                "reconciliation",
                f"Cleaned Revenue-In-Depth Gross Revenue ({rev_gross:,.2f}) does not match pre-clean 'Totals' row ({raw_g:,.2f}).",
                {"cleaned_gross": rev_gross, "raw_totals_gross": raw_g},
            )
        if raw_q is not None and abs(rev_qty - raw_q) > max(1.0, abs(raw_q) * 0.005):
            pre_clean_totals_match = False
            add_hard(
                "PRE_CLEAN_QTY_MISMATCH",
                "reconciliation",
                f"Cleaned Revenue-In-Depth Quantity ({rev_qty:,}) does not match pre-clean 'Totals' row ({raw_q:,.0f}).",
                {"cleaned_qty": rev_qty, "raw_totals_qty": raw_q},
            )

    rev_diff_pct = 0.0
    qty_diff_pct = 0.0
    missing_in_parking_dates: list[str] = []
    recon_status = "skipped"

    if rev_info and rev_info["readable"] and pt_info and pt_info["readable"]:
        recon_status = "passed"
        denom_rev = max(abs(rev_gross), abs(pt_payment), 1.0)
        rev_diff_pct = round(abs(rev_gross - pt_payment) / denom_rev * 100.0, 2)
        denom_qty = max(rev_qty, pt_rows, 1)
        qty_diff_pct = round(abs(rev_qty - pt_rows) / denom_qty * 100.0, 2)

        if max(rev_gross, pt_payment) >= 100.0:
            if rev_diff_pct > RECON_HARD_FAIL_PCT:
                recon_status = "failed"
                add_hard(
                    "REVENUE_TXN_MISMATCH",
                    "reconciliation",
                    f"Revenue vs Parking Payment mismatch is {rev_diff_pct:.2f}% (Gross Revenue={rev_gross:,.2f} vs Parking Payment={pt_payment:,.2f}).",
                    {"revenue_gross": rev_gross, "parking_payment": pt_payment, "diff_pct": rev_diff_pct},
                )
            elif rev_diff_pct > RECON_SOFT_WARN_PCT:
                recon_status = "warning"
                add_soft(
                    "REVENUE_TXN_VARIANCE",
                    "reconciliation",
                    f"Revenue vs Parking Payment variance is {rev_diff_pct:.2f}% (Gross Revenue={rev_gross:,.2f} vs Parking Payment={pt_payment:,.2f}).",
                    {"revenue_gross": rev_gross, "parking_payment": pt_payment, "diff_pct": rev_diff_pct},
                )

        if max(rev_qty, pt_rows) >= 100:
            if qty_diff_pct > RECON_HARD_FAIL_PCT:
                recon_status = "failed"
                add_hard(
                    "REVENUE_QTY_MISMATCH",
                    "reconciliation",
                    f"Revenue Quantity ({rev_qty:,}) vs Parking Transactions row count ({pt_rows:,}) differs by {qty_diff_pct:.2f}%.",
                    {"revenue_quantity": rev_qty, "parking_rows": pt_rows, "diff_pct": qty_diff_pct},
                )
            elif qty_diff_pct > RECON_SOFT_WARN_PCT:
                if recon_status == "passed":
                    recon_status = "warning"
                add_soft(
                    "REVENUE_QTY_VARIANCE",
                    "reconciliation",
                    f"Revenue Quantity ({rev_qty:,}) vs Parking row count ({pt_rows:,}) differs by {qty_diff_pct:.2f}%.",
                    {"revenue_quantity": rev_qty, "parking_rows": pt_rows, "diff_pct": qty_diff_pct},
                )

        expected_set = set(expected_dates)
        rev_window_active = (rev_info["active_dates"] & expected_set) if expected_set else rev_info["active_dates"]
        missing_in_parking_dates = sorted(d for d in rev_window_active if d not in pt_info["dates_seen"])
        if missing_in_parking_dates:
            recon_status = "failed"
            add_hard(
                "MISSING_DAY_IN_PARKING_TXN",
                "reconciliation",
                f"Revenue-In-Depth has activity on {len(missing_in_parking_dates)} date(s) missing from Parking-Transactions: {', '.join(missing_in_parking_dates)}",
                {"dates": missing_in_parking_dates},
            )
        if not pre_clean_totals_match:
            recon_status = "failed"

    # ---- Tier 4: Duplicates & Timestamp Anomalies ----
    dup_count = pt_info["duplicate_count"] if (pt_info and pt_info["readable"]) else 0
    dup_sample = pt_info["duplicate_access_ids"] if (pt_info and pt_info["readable"]) else []
    inv_raw = pt_info["inverted_exit_lt_entry"] if (pt_info and pt_info["readable"]) else []
    inv_count = len(inv_raw)
    inv_sample = [x for x in inv_raw if x != "__more__"]
    oor_raw = pt_info["out_of_range_txn_dates"] if (pt_info and pt_info["readable"]) else []
    oor_count = len(oor_raw)
    oor_sample = [x for x in oor_raw if x != "__more__"]

    anom_status = "passed"
    if dup_count > DUPLICATE_HARD_FAIL_THRESHOLD:
        anom_status = "failed"
        add_hard(
            "EXCESSIVE_DUPLICATE_ROWS",
            "anomalies",
            f"Detected {dup_count:,} duplicate (Access ID, Transaction Date/Time, Serial Number) rows — indicates duplicate merge/chunk.",
            {"duplicate_count": dup_count, "sample_access_ids": dup_sample[:10]},
        )
    elif dup_count > 0:
        anom_status = "warning"
        add_soft(
            "DUPLICATE_COMPOSITE_ROWS",
            "anomalies",
            f"Detected {dup_count} duplicate (Access ID, Transaction Date/Time, Serial Number) row(s). Samples: {', '.join(dup_sample[:5])}",
            {"duplicate_count": dup_count, "sample_access_ids": dup_sample[:10]},
        )

    if inv_count > 0:
        if anom_status == "passed":
            anom_status = "warning"
        add_soft(
            "INVERTED_EXIT_LT_ENTRY",
            "anomalies",
            f"{inv_count:,} row(s) have Exit Date/Time < Entry Date/Time (source clock skew). Sample Access IDs: {', '.join(inv_sample[:8])}",
            {"count": inv_count, "sample_access_ids": inv_sample[:15]},
        )

    if oor_count > 0:
        if anom_status == "passed":
            anom_status = "warning"
        add_soft(
            "OUT_OF_RANGE_TXN_TIMESTAMPS",
            "anomalies",
            f"{oor_count:,} row(s) have Transaction Date/Time outside target range +/-1d tolerance. Samples: {', '.join(oor_sample[:8])}",
            {"count": oor_count, "sample_access_ids": oor_sample[:15]},
        )

    # Set Parking Transactions per-table badge status
    if "parking" in expected_keys and pt_disp in tables_report:
        pass
    elif "parking" in expected_keys and pt_info and pt_info["readable"]:
        pt_hard = [f for f in hard_failures if f["code"] in (
            "PARTIAL_DATA_MISSING_DAYS", "QLIK_100K_CAP_RISK", "MISSING_REQUIRED_COLUMNS",
            "EMPTY_TABLE_EXPORT", "BARCODE_TEXT_CORRUPTION", "DATE_NOT_NATIVE_EXCEL",
            "DURATION_CORRUPTED_STRING", "NUMERIC_FIELD_TEXT", "NA_VALUES_STRIPPED",
            "EXCESSIVE_DUPLICATE_ROWS", "DUPLICATE_DAY_CHUNK", "TABLE_SCRAPE_FAILED",
            "MISSING_DAY_IN_PARKING_TXN",
        )]
        if any(f["code"] == "PARTIAL_DATA_MISSING_DAYS" for f in pt_hard):
            p_status = "partial"
            p_msg = f"Partial data ({covered_days_count}/{len(expected_dates)} days, {pt_info['rows']:,} rows)"
        elif pt_hard:
            p_status = "failed"
            p_msg = pt_hard[0]["message"]
        elif soft_warnings:
            p_status = "warning"
            p_msg = f"Fetched with advisories ({pt_info['rows']:,} rows)"
        else:
            p_status = "done"
            p_msg = f"Verified ({pt_info['rows']:,} rows)"
        tables_report[pt_disp] = {
            "status": p_status,
            "file": f"{prefix}-{pt_base}.xlsx",
            "rows": pt_info["rows"],
            "na_count": pt_info["na_count"],
            "message": p_msg,
        }

    # Category statuses
    comp_hard = any(f["category"] == "completeness" for f in hard_failures)
    comp_soft = any(w["category"] == "completeness" for w in soft_warnings)
    comp_status = "failed" if comp_hard else ("warning" if comp_soft else "passed")

    struct_hard = any(f["category"] == "structure" for f in hard_failures)
    struct_status = "failed" if struct_hard else "passed"

    if hard_failures:
        overall_status = "failed"
        upload_blocked = True
        summary = (
            f"Validation Gate BLOCKED SharePoint upload ({len(hard_failures)} hard failure(s), "
            f"{len(soft_warnings)} soft warning(s)): {hard_failures[0]['message']}"
        )
    elif soft_warnings:
        overall_status = "warning"
        upload_blocked = False
        summary = f"Validation Gate PASSED with {len(soft_warnings)} soft warning(s). Data reconciled & uploaded."
    else:
        overall_status = "passed"
        upload_blocked = False
        summary = "Validation Gate PASSED: all completeness, schema, Excel type, and financial reconciliation checks succeeded."

    return {
        "version": "1.0",
        "generated_at": _utc_now_iso(),
        "batch_name": spec.get("name", prefix),
        "file_prefix": prefix,
        "sharepoint_folder": spec.get("sharepoint_folder", ""),
        "start_date": start_d.strftime("%Y-%m-%d") if start_d else "",
        "end_date": end_d.strftime("%Y-%m-%d") if end_d else "",
        "sheet_filter": sheet_filter,
        "validation_enabled": True,
        "status": overall_status,
        "upload_blocked": upload_blocked,
        "upload_performed": False,
        "scrape_error": scrape_error,
        "summary": summary,
        "tables": tables_report,
        "checks": {
            "completeness": {
                "status": comp_status,
                "expected_days": len(expected_dates),
                "covered_days": covered_days_count,
                "missing_days": sorted(set(failed_days) | set(missing_days)),
                "portal_empty_days": portal_empty_days,
                "failed_days": failed_days,
                "max_export_rows": max_export_rows,
                "qlik_100k_risk": qlik_100k_risk,
            },
            "structure_and_types": {
                "status": struct_status,
                "barcodes_text_ok": (pt_info["violation_counts"]["barcodes"] == 0) if pt_info else True,
                "dates_native_ok": (pt_info["violation_counts"]["dates"] == 0) if pt_info else True,
                "durations_numeric_ok": (pt_info["violation_counts"]["durations"] == 0) if pt_info else True,
                "numbers_ok": (pt_info["violation_counts"]["numbers"] == 0) if pt_info else True,
                "na_preserved_ok": (pt_info["violation_counts"]["na_stripped"] == 0) if pt_info else True,
                "na_count": pt_info["na_count"] if pt_info else 0,
                "violation_counts": {
                    **(pt_info["violation_counts"] if pt_info else {"barcodes": 0, "dates": 0, "durations": 0, "numbers": 0, "na_stripped": 0}),
                    "totals_left": sum(
                        r.get("totals_rows_remaining", 0) for r in indepth_results.values()
                    ),
                },
                "samples": pt_info["violation_samples"] if pt_info else [],
            },
            "reconciliation": {
                "status": recon_status,
                "revenue_gross": round(rev_gross, 2),
                "revenue_theoretical": round(rev_theo, 2),
                "revenue_quantity": rev_qty,
                "parking_payment": round(pt_payment, 2),
                "parking_discount": round(pt_discount, 2),
                "parking_rows": pt_rows,
                "revenue_diff_pct": rev_diff_pct,
                "quantity_diff_pct": qty_diff_pct,
                "pre_clean_totals_match": pre_clean_totals_match,
                "missing_in_parking_dates": missing_in_parking_dates,
            },
            "anomalies": {
                "status": anom_status,
                "duplicate_composite_rows": dup_count,
                "duplicate_sample_ids": dup_sample[:10],
                "inverted_exit_lt_entry_count": inv_count,
                "inverted_sample_ids": inv_sample[:15],
                "out_of_range_txn_dates_count": oor_count,
                "out_of_range_sample_ids": oor_sample[:15],
            },
        },
        "insights": _build_insights_payload(
            expected_dates=expected_dates,
            indepth_results=indepth_results,
            pt_info=pt_info,
            rev_theo=rev_theo,
            rev_gross=rev_gross,
            pt_payment=pt_payment,
            pt_discount=pt_discount,
        ),
        "hard_failures": hard_failures,
        "soft_warnings": soft_warnings,
    }


def _merged_check_status(reports: list[dict[str, Any]], check_key: str, any_bypassed: bool) -> str:
    """Safely roll up per-batch check category statuses into one unified status."""
    if any_bypassed and all(not r.get("validation_enabled", True) for r in reports):
        return "bypassed"
    statuses = [
        (r.get("checks") or {}).get(check_key, {}).get("status", "skipped")
        for r in reports
    ]
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses:
        return "warning"
    if "passed" in statuses:
        return "passed"
    return "bypassed" if any_bypassed else "skipped"


def _merge_check_bool(reports: list[dict[str, Any]], field: str) -> bool:
    return all(
        (r.get("checks") or {}).get("structure_and_types", {}).get(field, True)
        for r in reports
    )


def _compile_insights(
    by_date: dict[str, dict[str, float]],
    by_hour: dict[str, dict[str, float]],
    by_car_park: dict[str, dict[str, float]],
    payment_methods: dict[str, dict[str, float]],
    paid_rows: int,
    free_rows: int,
    total_discount: float,
    rev_theo: float,
    rev_gross: float,
    pt_payment: float,
    batches_series: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Compile pre-aggregated daily, monthly, hourly, car-park, and payment insights."""
    daily_series: list[dict[str, Any]] = []
    monthly_acc: dict[str, dict[str, float]] = {}
    access_entries_total = 0
    access_exits_total = 0

    for d_key in sorted(by_date.keys()):
        b = by_date[d_key]
        en = int(round(b.get("access_entries", 0.0)))
        ex = int(round(b.get("access_exits", 0.0)))
        access_entries_total += en
        access_exits_total += ex
        entry = {
            "date": d_key,
            "revenue_gross": round(b.get("revenue_gross", 0.0), 2),
            "revenue_theoretical": round(b.get("revenue_theoretical", 0.0), 2),
            "parking_payment": round(b.get("parking_payment", 0.0), 2),
            "parking_discount": round(b.get("parking_discount", 0.0), 2),
            "parking_rows": int(round(b.get("parking_rows", 0.0))),
            "paid_rows": int(round(b.get("paid_rows", 0.0))),
            "access_entries": en,
            "access_exits": ex,
        }
        daily_series.append(entry)
        _accumulate_bucket(
            monthly_acc,
            d_key[:7],
            revenue_gross=entry["revenue_gross"],
            revenue_theoretical=entry["revenue_theoretical"],
            parking_payment=entry["parking_payment"],
            parking_discount=entry["parking_discount"],
            parking_rows=entry["parking_rows"],
            paid_rows=entry["paid_rows"],
            access_entries=en,
            access_exits=ex,
        )

    monthly_series = [
        {
            "month": m_key,
            "revenue_gross": round(b.get("revenue_gross", 0.0), 2),
            "revenue_theoretical": round(b.get("revenue_theoretical", 0.0), 2),
            "parking_payment": round(b.get("parking_payment", 0.0), 2),
            "parking_discount": round(b.get("parking_discount", 0.0), 2),
            "parking_rows": int(round(b.get("parking_rows", 0.0))),
            "paid_rows": int(round(b.get("paid_rows", 0.0))),
            "access_entries": int(round(b.get("access_entries", 0.0))),
            "access_exits": int(round(b.get("access_exits", 0.0))),
        }
        for m_key in sorted(monthly_acc.keys())
        for b in (monthly_acc[m_key],)
    ]

    hourly_series = []
    for hr in range(24):
        h_key = f"{hr:02d}"
        hb = by_hour.get(h_key, {})
        hourly_series.append({
            "hour": f"{h_key}:00",
            "revenue_gross": round(hb.get("revenue_gross", 0.0), 2),
            "parking_payment": round(hb.get("parking_payment", 0.0), 2),
            "parking_rows": int(round(hb.get("parking_rows", 0.0))),
            "paid_rows": int(round(hb.get("paid_rows", 0.0))),
            "access_entries": int(round(hb.get("access_entries", 0.0))),
        })

    cp_list = [
        {
            "car_park": cp_name,
            "revenue_gross": round(cb.get("revenue_gross", 0.0), 2),
            "revenue_theoretical": round(cb.get("revenue_theoretical", 0.0), 2),
            "parking_payment": round(cb.get("parking_payment", 0.0), 2),
            "parking_discount": round(cb.get("parking_discount", 0.0), 2),
            "parking_rows": int(round(cb.get("parking_rows", 0.0))),
            "paid_rows": int(round(cb.get("paid_rows", 0.0))),
            "access_entries": int(round(cb.get("access_entries", 0.0))),
            "access_exits": int(round(cb.get("access_exits", 0.0))),
        }
        for cp_name, cb in by_car_park.items()
    ]
    cp_list.sort(key=lambda x: (x["revenue_gross"], x["parking_payment"], x["parking_rows"]), reverse=True)
    cp_list = cp_list[:25]

    pm_list = [
        {
            "method": pm_name,
            "count": int(round(pb.get("count", 0.0))),
            "amount": round(pb.get("amount", 0.0), 2),
        }
        for pm_name, pb in payment_methods.items()
    ]
    pm_list.sort(key=lambda x: (x["amount"], x["count"]), reverse=True)
    pm_list = pm_list[:12]

    total_rows = paid_rows + free_rows
    paid_pct = round((paid_rows / total_rows * 100.0), 2) if total_rows > 0 else 0.0

    def _pick_metric(item: dict[str, Any]) -> tuple[float, float, int]:
        return (
            float(item.get("revenue_gross", 0.0) or item.get("parking_payment", 0.0)),
            float(item.get("parking_payment", 0.0)),
            int(item.get("parking_rows", 0) or item.get("access_entries", 0)),
        )

    peak_day_item = max(daily_series, key=_pick_metric) if daily_series else None
    peak_hour_item = max(hourly_series, key=_pick_metric) if hourly_series else None
    top_cp_item = cp_list[0] if cp_list else None
    cp_total_basis = sum(max(c["revenue_gross"], c["parking_payment"]) for c in cp_list) or 1.0
    if top_cp_item:
        top_cp_val = max(top_cp_item["revenue_gross"], top_cp_item["parking_payment"])
        top_share = round(top_cp_val / cp_total_basis * 100.0, 1) if cp_total_basis > 0 else 0.0
    else:
        top_share = 0.0

    if batches_series and len(batches_series) > 1:
        default_view = "monthly"
    elif len(daily_series) > 35:
        default_view = "monthly"
    elif len(daily_series) <= 1:
        default_view = "hourly"
    else:
        default_view = "daily"

    return {
        "default_view": default_view,
        "kpis": {
            "days_count": len(daily_series),
            "months_count": len(monthly_series),
            "total_rows": total_rows,
            "paid_rows": paid_rows,
            "free_rows": free_rows,
            "paid_pct": paid_pct,
            "revenue_theoretical": round(rev_theo, 2),
            "revenue_gross": round(rev_gross, 2),
            "parking_payment": round(pt_payment, 2),
            "total_discount": round(total_discount, 2),
            "access_entries": access_entries_total,
            "access_exits": access_exits_total,
            "peak_day": {
                "date": peak_day_item["date"],
                "revenue_gross": peak_day_item["revenue_gross"],
                "parking_payment": peak_day_item["parking_payment"],
                "parking_rows": peak_day_item["parking_rows"],
            } if peak_day_item else None,
            "peak_hour": {
                "hour": peak_hour_item["hour"],
                "revenue_gross": peak_hour_item["revenue_gross"],
                "parking_payment": peak_hour_item["parking_payment"],
                "parking_rows": peak_hour_item["parking_rows"],
            } if peak_hour_item else None,
            "top_car_park": {
                "car_park": top_cp_item["car_park"],
                "revenue_gross": top_cp_item["revenue_gross"],
                "parking_payment": top_cp_item["parking_payment"],
                "parking_rows": top_cp_item["parking_rows"],
                "share_pct": top_share,
            } if top_cp_item else None,
        },
        "daily_series": daily_series,
        "monthly_series": monthly_series,
        "hourly_series": hourly_series,
        "by_car_park": cp_list,
        "payment_methods": pm_list,
        "batches_series": batches_series or [],
    }


def _build_insights_payload(
    expected_dates: list[str],
    indepth_results: dict[str, dict[str, Any]],
    pt_info: Optional[dict[str, Any]],
    rev_theo: float,
    rev_gross: float,
    pt_payment: float,
    pt_discount: float,
) -> dict[str, Any]:
    """Assemble insights from one batch's validated workbook summaries."""
    by_date: dict[str, dict[str, float]] = {d: {} for d in expected_dates}
    by_hour: dict[str, dict[str, float]] = {}
    by_car_park: dict[str, dict[str, float]] = {}

    for tbl_key in ("revenue", "access"):
        info = indepth_results.get(tbl_key)
        if info and info.get("readable"):
            _merge_bucket_map(by_date, info.get("by_date"))
            _merge_bucket_map(by_hour, info.get("by_hour"))
            _merge_bucket_map(by_car_park, info.get("by_car_park"))

    payment_methods: dict[str, dict[str, float]] = {}
    paid_rows = 0
    free_rows = 0
    if pt_info and pt_info.get("readable"):
        _merge_bucket_map(by_date, pt_info.get("by_date"))
        _merge_bucket_map(by_hour, pt_info.get("by_hour"))
        _merge_bucket_map(by_car_park, pt_info.get("by_car_park"))
        _merge_bucket_map(payment_methods, pt_info.get("payment_methods"))
        paid_rows = int(pt_info.get("paid_rows", 0))
        free_rows = int(pt_info.get("free_rows", 0))

    return _compile_insights(
        by_date=by_date,
        by_hour=by_hour,
        by_car_park=by_car_park,
        payment_methods=payment_methods,
        paid_rows=paid_rows,
        free_rows=free_rows,
        total_discount=pt_discount,
        rev_theo=rev_theo,
        rev_gross=rev_gross,
        pt_payment=pt_payment,
    )


def _merge_insights_payloads(reports: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Combine `insights` dictionaries across multi-batch / multi-month reports."""
    insight_reports = [r for r in reports if isinstance(r.get("insights"), dict)]
    if not insight_reports:
        return None

    by_date: dict[str, dict[str, float]] = {}
    by_hour: dict[str, dict[str, float]] = {}
    by_car_park: dict[str, dict[str, float]] = {}
    payment_methods: dict[str, dict[str, float]] = {}
    paid_rows = 0
    free_rows = 0
    total_discount = 0.0
    rev_theo = 0.0
    rev_gross = 0.0
    pt_payment = 0.0
    batches_series: list[dict[str, Any]] = []

    for r in insight_reports:
        ins = r["insights"]
        k = ins.get("kpis") or {}
        paid_rows += int(k.get("paid_rows", 0))
        free_rows += int(k.get("free_rows", 0))
        total_discount += float(k.get("total_discount", 0.0))
        rev_theo += float(k.get("revenue_theoretical", 0.0))
        rev_gross += float(k.get("revenue_gross", 0.0))
        pt_payment += float(k.get("parking_payment", 0.0))

        for item in ins.get("daily_series") or []:
            _accumulate_bucket(
                by_date,
                item.get("date"),
                revenue_gross=item.get("revenue_gross", 0.0),
                revenue_theoretical=item.get("revenue_theoretical", 0.0),
                parking_payment=item.get("parking_payment", 0.0),
                parking_discount=item.get("parking_discount", 0.0),
                parking_rows=item.get("parking_rows", 0),
                paid_rows=item.get("paid_rows", 0),
                access_entries=item.get("access_entries", 0),
                access_exits=item.get("access_exits", 0),
            )
        for item in ins.get("hourly_series") or []:
            h_raw = str(item.get("hour", ""))[:2]
            _accumulate_bucket(
                by_hour,
                h_raw,
                revenue_gross=item.get("revenue_gross", 0.0),
                parking_payment=item.get("parking_payment", 0.0),
                parking_rows=item.get("parking_rows", 0),
                paid_rows=item.get("paid_rows", 0),
                access_entries=item.get("access_entries", 0),
            )
        for item in ins.get("by_car_park") or []:
            _accumulate_bucket(
                by_car_park,
                item.get("car_park"),
                revenue_gross=item.get("revenue_gross", 0.0),
                revenue_theoretical=item.get("revenue_theoretical", 0.0),
                parking_payment=item.get("parking_payment", 0.0),
                parking_discount=item.get("parking_discount", 0.0),
                parking_rows=item.get("parking_rows", 0),
                paid_rows=item.get("paid_rows", 0),
                access_entries=item.get("access_entries", 0),
                access_exits=item.get("access_exits", 0),
            )
        for item in ins.get("payment_methods") or []:
            _accumulate_bucket(
                payment_methods,
                item.get("method"),
                count=item.get("count", 0),
                amount=item.get("amount", 0.0),
            )
        batches_series.append({
            "label": r.get("batch_name") or r.get("file_prefix") or "Batch",
            "start_date": r.get("start_date", ""),
            "end_date": r.get("end_date", ""),
            "revenue_gross": round(float(k.get("revenue_gross", 0.0)), 2),
            "revenue_theoretical": round(float(k.get("revenue_theoretical", 0.0)), 2),
            "parking_payment": round(float(k.get("parking_payment", 0.0)), 2),
            "parking_discount": round(float(k.get("total_discount", 0.0)), 2),
            "parking_rows": int(k.get("total_rows", 0)),
            "access_entries": int(k.get("access_entries", 0)),
        })

    return _compile_insights(
        by_date=by_date,
        by_hour=by_hour,
        by_car_park=by_car_park,
        payment_methods=payment_methods,
        paid_rows=paid_rows,
        free_rows=free_rows,
        total_discount=total_discount,
        rev_theo=rev_theo,
        rev_gross=rev_gross,
        pt_payment=pt_payment,
        batches_series=batches_series,
    )


def merge_validation_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one or more per-batch reports into a unified workflow report."""
    if not reports:
        return {
            "version": "1.0",
            "generated_at": _utc_now_iso(),
            "status": "failed",
            "validation_enabled": True,
            "upload_blocked": True,
            "upload_performed": False,
            "summary": "No batches were processed.",
            "tables": {},
            "checks": {},
            "hard_failures": [{"code": "NO_BATCHES", "severity": "hard", "category": "completeness", "message": "No backfill batches ran.", "details": {}}],
            "soft_warnings": [],
            "batches": [],
        }

    if len(reports) == 1:
        single = dict(reports[0])
        single["batches"] = [
            {
                "batch_name": single.get("batch_name"),
                "file_prefix": single.get("file_prefix"),
                "sharepoint_folder": single.get("sharepoint_folder"),
                "status": single.get("status"),
                "upload_blocked": single.get("upload_blocked"),
                "upload_performed": single.get("upload_performed"),
                "summary": single.get("summary"),
            }
        ]
        return single

    all_hard: list[dict[str, Any]] = []
    all_soft: list[dict[str, Any]] = []
    for r in reports:
        b = r.get("batch_name", "")
        for f in r.get("hard_failures", []):
            item = dict(f)
            item["message"] = f"[{b}] {item['message']}"
            all_hard.append(item)
        for w in r.get("soft_warnings", []):
            item = dict(w)
            item["message"] = f"[{b}] {item['message']}"
            all_soft.append(item)

    any_bypassed = any(not r.get("validation_enabled", True) for r in reports)
    if all_hard:
        status = "failed"
        blocked = True
        summary = f"Multi-batch Validation Gate BLOCKED upload on failed batch(es): {len(all_hard)} hard failure(s), {len(all_soft)} soft warning(s)."
    elif any_bypassed and all(r.get("status") == "bypassed" for r in reports):
        status = "bypassed"
        blocked = False
        summary = f"Validation Gate bypassed across {len(reports)} batch(es)."
    elif all_soft:
        status = "warning"
        blocked = False
        summary = f"All {len(reports)} batch(es) passed with {len(all_soft)} soft warning(s)."
    else:
        status = "passed"
        blocked = False
        summary = f"All {len(reports)} batch(es) passed full Validation Gate."

    # Roll up worst status per table across batches
    rank = {"failed": 4, "partial": 3, "warning": 2, "done": 1, "skipped": 0}
    merged_tables: dict[str, dict[str, Any]] = {}
    for r in reports:
        for tname, tinfo in (r.get("tables") or {}).items():
            prev = merged_tables.get(tname)
            cur_st = tinfo.get("status", "skipped")
            if prev is None:
                merged_tables[tname] = dict(tinfo)
            else:
                best_st = cur_st if rank.get(cur_st, 0) > rank.get(prev.get("status", "skipped"), 0) else prev.get("status")
                prev["status"] = best_st
                prev["rows"] = int(prev.get("rows", 0)) + int(tinfo.get("rows", 0))
                if cur_st in ("failed", "partial"):
                    prev["message"] = f"[{r.get('batch_name')}] {tinfo.get('message')}"

    # Roll up check KPIs
    exp_days = sum((r.get("checks", {}).get("completeness", {}).get("expected_days", 0)) for r in reports)
    cov_days = sum((r.get("checks", {}).get("completeness", {}).get("covered_days", 0)) for r in reports)
    rev_gross = sum((r.get("checks", {}).get("reconciliation", {}).get("revenue_gross", 0.0)) for r in reports)
    rev_qty = sum((r.get("checks", {}).get("reconciliation", {}).get("revenue_quantity", 0)) for r in reports)
    pt_pay = sum((r.get("checks", {}).get("reconciliation", {}).get("parking_payment", 0.0)) for r in reports)
    pt_rows = sum((r.get("checks", {}).get("reconciliation", {}).get("parking_rows", 0)) for r in reports)
    denom_r = max(abs(rev_gross), abs(pt_pay), 1.0)
    denom_q = max(rev_qty, pt_rows, 1)

    return {
        "version": "1.0",
        "generated_at": _utc_now_iso(),
        "batch_name": f"{len(reports)} Batches",
        "file_prefix": reports[-1].get("file_prefix", ""),
        "sharepoint_folder": ", ".join(r.get("sharepoint_folder", "") for r in reports),
        "start_date": reports[0].get("start_date", ""),
        "end_date": reports[-1].get("end_date", ""),
        "sheet_filter": reports[0].get("sheet_filter", "all"),
        "validation_enabled": not any_bypassed,
        "status": status,
        "upload_blocked": blocked,
        "upload_performed": any(r.get("upload_performed") for r in reports),
        "summary": summary,
        "tables": merged_tables,
        "checks": {
            "completeness": {
                "status": _merged_check_status(reports, "completeness", any_bypassed),
                "expected_days": exp_days,
                "covered_days": cov_days,
                "missing_days": sorted({d for r in reports for d in ((r.get("checks") or {}).get("completeness", {}).get("missing_days") or [])}),
                "portal_empty_days": sorted({d for r in reports for d in ((r.get("checks") or {}).get("completeness", {}).get("portal_empty_days") or [])}),
                "max_export_rows": max(((r.get("checks") or {}).get("completeness", {}).get("max_export_rows", 0)) for r in reports),
                "qlik_100k_risk": any((r.get("checks") or {}).get("completeness", {}).get("qlik_100k_risk") for r in reports),
            },
            "structure_and_types": {
                "status": _merged_check_status(reports, "structure_and_types", any_bypassed),
                "barcodes_text_ok": _merge_check_bool(reports, "barcodes_text_ok"),
                "dates_native_ok": _merge_check_bool(reports, "dates_native_ok"),
                "durations_numeric_ok": _merge_check_bool(reports, "durations_numeric_ok"),
                "numbers_ok": _merge_check_bool(reports, "numbers_ok"),
                "na_preserved_ok": _merge_check_bool(reports, "na_preserved_ok"),
                "na_count": sum((r.get("checks") or {}).get("structure_and_types", {}).get("na_count", 0) for r in reports),
            },
            "reconciliation": {
                "status": _merged_check_status(reports, "reconciliation", any_bypassed),
                "revenue_gross": round(rev_gross, 2),
                "revenue_quantity": rev_qty,
                "parking_payment": round(pt_pay, 2),
                "parking_rows": pt_rows,
                "revenue_diff_pct": round(abs(rev_gross - pt_pay) / denom_r * 100.0, 2) if max(rev_gross, pt_pay) > 0 else 0.0,
                "quantity_diff_pct": round(abs(rev_qty - pt_rows) / denom_q * 100.0, 2) if max(rev_qty, pt_rows) > 0 else 0.0,
            },
            "anomalies": {
                "status": _merged_check_status(reports, "anomalies", any_bypassed),
                "duplicate_composite_rows": sum((r.get("checks") or {}).get("anomalies", {}).get("duplicate_composite_rows", 0) for r in reports),
                "inverted_exit_lt_entry_count": sum((r.get("checks") or {}).get("anomalies", {}).get("inverted_exit_lt_entry_count", 0) for r in reports),
                "out_of_range_txn_dates_count": sum((r.get("checks") or {}).get("anomalies", {}).get("out_of_range_txn_dates_count", 0) for r in reports),
            },
        },
        "hard_failures": all_hard,
        "soft_warnings": all_soft,
        "insights": _merge_insights_payloads(reports),
        "batches": [
            {
                "batch_name": r.get("batch_name"),
                "file_prefix": r.get("file_prefix"),
                "sharepoint_folder": r.get("sharepoint_folder"),
                "status": r.get("status"),
                "upload_blocked": r.get("upload_blocked"),
                "upload_performed": r.get("upload_performed"),
                "summary": r.get("summary"),
                "checks": r.get("checks"),
                "hard_failures": r.get("hard_failures"),
                "soft_warnings": r.get("soft_warnings"),
            }
            for r in reports
        ],
    }


def write_validation_report(report: dict[str, Any], output_paths: list[str]) -> None:
    """Atomically persist `_validation_report.json` to one or more target paths."""
    payload = json.dumps(report, indent=2, default=str)
    for out_path in output_paths:
        if not out_path:
            continue
        parent = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(parent, exist_ok=True)
        tmp = f"{out_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, out_path)
