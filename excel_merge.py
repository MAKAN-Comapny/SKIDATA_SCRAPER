"""
Streaming Excel merger for SKIDATA Parking-Transactions exports.

Combines daily Qlik Sense `.xlsx` exports using XML streaming + openpyxl
`write_only=True` so consolidated files inherit clean native Excel types
(dates, numeric durations without 1900-era text corruption, numeric payments
and Access IDs, preserved 'N/A' labels) while keeping 23-digit barcodes and
receipt/plate identifiers strictly as text (zero scientific notation).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

import openpyxl
from openpyxl.cell import WriteOnlyCell

_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_DATE_HEADERS = frozenset({
    "Entry Date/Time",
    "Exit Date/Time",
    "Transaction Date/Time",
})
_DURATION_HEADERS = frozenset({"Duration"})
_NUMERIC_HEADERS = frozenset({
    "Payment",
    "Discount Amount",
    "Access ID",
})
_TEXT_IDENTIFIER_HEADERS = frozenset({
    "Serial Number",
    "Receipt Number",
    "Credit Card Number",
    "Discount(Discount Number)",
    "Entry Plate Number",
    "Exit Plate Number",
})
_TEXT_IDENTIFIER_KEYWORDS = ("serial", "ticket", "receipt", "plate", "credit card")
_TRAILING_DOT_ZERO_RE = re.compile(r"^[+-]?\d+\.0+$")
_SCI_NOTATION_RE = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")
_DATE_PARSE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def _is_text_identifier(header: str) -> bool:
    """Return True if column must stay plain text to avoid scientific notation or .0."""
    if header in _TEXT_IDENTIFIER_HEADERS:
        return True
    lower = header.lower()
    if "amount" in lower or "payment" in lower:
        return False
    return any(term in lower for term in _TEXT_IDENTIFIER_KEYWORDS)


def _col_letters_to_idx(ref: str) -> Optional[int]:
    """Convert cell reference like 'AA12' to zero-based column index."""
    idx = 0
    found = False
    for ch in ref:
        if ch.isalpha():
            found = True
            idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return (idx - 1) if found else None


def _resolve_first_sheet_path(archive: zipfile.ZipFile) -> str:
    """Resolve the first worksheet XML path inside an .xlsx archive."""
    names = set(archive.namelist())
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        try:
            wb_root = ET.fromstring(archive.read("xl/workbook.xml"))
            sheet_el = wb_root.find(f".//{_SPREADSHEET_NS}sheet")
            if sheet_el is not None:
                r_id = sheet_el.attrib.get(_REL_ATTR)
                rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                for rel in rels_root.findall(f"{_PKG_REL_NS}Relationship"):
                    if rel.attrib.get("Id") == r_id:
                        target = rel.attrib.get("Target", "").lstrip("/")
                        candidate = target if target.startswith("xl/") else f"xl/{target}"
                        if candidate in names:
                            return candidate
        except ET.ParseError:
            pass

    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    for name in sorted(names):
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            return name
    raise FileNotFoundError("No worksheet XML found inside .xlsx archive.")


def _load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Stream shared string table if present in the .xlsx archive."""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings: list[str] = []
    with archive.open("xl/sharedStrings.xml") as handle:
        for _, elem in ET.iterparse(handle, events=("end",)):
            if elem.tag == f"{_SPREADSHEET_NS}si":
                strings.append("".join(elem.itertext()))
                elem.clear()
    return strings


def _extract_row_cells(row_elem: ET.Element, shared_strings: list[str]) -> dict[int, tuple[str, str]]:
    """Extract {col_idx: (raw_type, raw_value)} from a worksheet <row> element."""
    cells: dict[int, tuple[str, str]] = {}
    fallback_idx = 0
    for cell in row_elem.findall(f"{_SPREADSHEET_NS}c"):
        col_idx = _col_letters_to_idx(cell.attrib.get("r", ""))
        if col_idx is None:
            col_idx = fallback_idx
        fallback_idx = col_idx + 1

        c_type = cell.attrib.get("t", "n")
        inline_el = cell.find(f"{_SPREADSHEET_NS}is")
        if inline_el is not None:
            cells[col_idx] = ("s", "".join(inline_el.itertext()))
            continue

        val_el = cell.find(f"{_SPREADSHEET_NS}v")
        if val_el is None or val_el.text is None:
            continue
        if c_type == "s":
            cells[col_idx] = ("s", shared_strings[int(val_el.text)])
        else:
            cells[col_idx] = (c_type, val_el.text)
    return cells


def _format_identifier_text(raw_type: str, raw_val: str) -> str:
    """Keep identifiers/barcodes as clean exact text strings without .0 or scientific notation."""
    text = raw_val.strip()
    if not text:
        return ""
    if raw_type == "n" and _SCI_NOTATION_RE.fullmatch(text):
        try:
            dec = Decimal(text)
            if dec == dec.to_integral_value():
                return format(dec.to_integral_value(), "f")
            return format(dec, "f")
        except InvalidOperation:
            return text
    if _TRAILING_DOT_ZERO_RE.fullmatch(text):
        return text.split(".", 1)[0]
    return raw_val


def _parse_numeric_value(raw_val: str):
    """Parse numeric string to int when exact integer, float otherwise, or original str on failure."""
    stripped = raw_val.strip()
    if not stripped:
        return None
    try:
        num = float(stripped)
    except ValueError:
        return raw_val
    if num.is_integer() and "." not in stripped:
        try:
            return int(stripped)
        except ValueError:
            return int(num)
    return int(num) if num.is_integer() else num


def _build_output_cell(ws_out, header: str, raw_type: str, raw_val: Optional[str]):
    """Convert raw XML cell value into a properly typed openpyxl write-only cell or scalar."""
    if raw_val is None or raw_val == "":
        return None

    if _is_text_identifier(header):
        cleaned = _format_identifier_text(raw_type, raw_val)
        return cleaned if cleaned != "" else None

    if header in _DATE_HEADERS:
        if raw_type == "n":
            cell = WriteOnlyCell(ws_out, value=float(raw_val))
            cell.number_format = "yyyy-mm-dd h:mm:ss"
            return cell
        stripped = raw_val.strip()
        for fmt in _DATE_PARSE_FORMATS:
            try:
                parsed = datetime.strptime(stripped, fmt)
                cell = WriteOnlyCell(ws_out, value=parsed)
                cell.number_format = "yyyy-mm-dd h:mm:ss"
                return cell
            except ValueError:
                continue
        return raw_val

    if header in _DURATION_HEADERS:
        if raw_type == "n":
            cell = WriteOnlyCell(ws_out, value=float(raw_val))
            cell.number_format = "[h]:mm:ss"
            return cell
        return raw_val

    if header in _NUMERIC_HEADERS:
        return _parse_numeric_value(raw_val)

    return raw_val


def merge_parking_xlsx(input_files: Iterable[str], output_path: str) -> int:
    """
    Merge multiple Parking-Transactions .xlsx files into `output_path` atomically.
    Returns the total number of data rows written.
    """
    files = list(input_files)
    if not files:
        raise ValueError("No input Excel files provided for merge.")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    tmp_path = f"{output_path}.tmp.xlsx"

    wb_out = openpyxl.Workbook(write_only=True)
    ws_out = wb_out.create_sheet("Sheet1")
    canonical_headers: Optional[list[str]] = None
    total_rows = 0

    try:
        for file_path in files:
            with zipfile.ZipFile(file_path, "r") as archive:
                shared_strings = _load_shared_strings(archive)
                sheet_xml = _resolve_first_sheet_path(archive)

                file_headers: Optional[list[str]] = None
                col_mapping: list[tuple[int, int, str]] = []

                with archive.open(sheet_xml) as sheet_handle:
                    for _, elem in ET.iterparse(sheet_handle, events=("end",)):
                        if elem.tag != f"{_SPREADSHEET_NS}row":
                            continue

                        cells = _extract_row_cells(elem, shared_strings)
                        elem.clear()
                        if not cells:
                            continue

                        if file_headers is None:
                            max_idx = max(cells.keys())
                            file_headers = [
                                (cells[i][1] if i in cells else f"Column_{i}")
                                for i in range(max_idx + 1)
                            ]
                            if canonical_headers is None:
                                canonical_headers = file_headers
                                ws_out.append(canonical_headers)
                            src_index_by_name = {name: idx for idx, name in enumerate(file_headers)}
                            col_mapping = [
                                (out_idx, src_index_by_name[name], name)
                                for out_idx, name in enumerate(canonical_headers)
                                if name in src_index_by_name
                            ]
                            continue

                        out_row = [None] * len(canonical_headers)
                        for out_idx, src_idx, col_name in col_mapping:
                            entry = cells.get(src_idx)
                            if entry is not None:
                                out_row[out_idx] = _build_output_cell(ws_out, col_name, entry[0], entry[1])
                        ws_out.append(out_row)
                        total_rows += 1

        wb_out.save(tmp_path)
        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return total_rows
