"""
Transformer Backfill
--------------------
Cleans downloaded historical backfill files from 'exports_backfill/'
and stages them to 'Cleaned-Exports-Backfill/' with pre-clean totals metadata.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
from typing import Any

import pandas as pd

EXPORTS_DIR = "exports_backfill"
CLEANED_EXPORTS_DIR = "Cleaned-Exports-Backfill"

_CLEAN_RULES: tuple[tuple[str, int], ...] = (
    ("Access-In-Depth", 0),
    ("Revenue-In-Depth", 1),
    ("System-Event-In-Depth", 0),
)


def _extract_pre_clean_revenue_totals(df: pd.DataFrame) -> dict[str, Any] | None:
    """Capture pre-clean 'Totals' row values from raw Revenue-In-Depth dataframe."""
    if "Date" not in df.columns:
        return None
    mask = df["Date"].astype(str).str.strip().str.lower() == "totals"
    if not mask.any():
        first_col = df.columns[0]
        mask = df[first_col].astype(str).str.strip().str.lower() == "totals"
    if not mask.any():
        return None
    row = df.loc[mask].iloc[0]
    totals: dict[str, Any] = {}
    for col in ("Quantity", "Theoretical Revenue", "Gross Revenue"):
        if col in df.columns:
            val = pd.to_numeric(row[col], errors="coerce")
            if pd.notna(val):
                totals[col] = float(val)
    return totals or None


def clean_backfill_directory(src_dir: str, dst_dir: str, strict: bool = True) -> dict[str, Any]:
    """
    Clean and stage downloaded `.xlsx` reports into `dst_dir` and write `_clean_meta.json`.

    In `strict=True` mode, cleaning failures are recorded in `clean_errors` rather
    than silently copying an uncleaned raw file into `dst_dir`.
    """
    os.makedirs(dst_dir, exist_ok=True)
    all_files = sorted(
        f for f in glob.glob(os.path.join(src_dir, "*.xlsx"))
        if os.path.isfile(f) and "daily_chunks" not in f
    )
    print(f"\nCleaning and staging {len(all_files)} file(s) from {src_dir} -> {dst_dir}...")

    meta: dict[str, Any] = {
        "staged_files": [],
        "clean_errors": {},
        "revenue_pre_clean_totals": {},
    }

    for src_path in all_files:
        fname = os.path.basename(src_path)
        dest_file = os.path.join(dst_dir, fname)
        matched_rule = next(((marker, hdr) for marker, hdr in _CLEAN_RULES if marker in fname), None)

        if matched_rule is None:
            # Parking-Transactions (and other unconfigured sheets) keep exact native types
            shutil.copy2(src_path, dest_file)
            meta["staged_files"].append(fname)
            print(f"  OK: {fname} (preserved native formatting)")
            continue

        marker, header_row = matched_rule
        try:
            df = pd.read_excel(src_path, header=header_row)
            if "Date" not in df.columns:
                raise KeyError(f"Missing expected 'Date' column in {fname} (header={header_row})")

            if marker == "Revenue-In-Depth":
                pre_totals = _extract_pre_clean_revenue_totals(df)
                if pre_totals:
                    meta["revenue_pre_clean_totals"][fname] = pre_totals

            date_norm = df["Date"].astype(str).str.strip().str.lower()
            first_col_norm = df[df.columns[0]].astype(str).str.strip().str.lower()
            cleaned_df = df[(date_norm != "totals") & (first_col_norm != "totals")].dropna(subset=["Date"])
            cleaned_df.to_excel(dest_file, index=False)
            meta["staged_files"].append(fname)
            print(f"  OK: {fname} ({len(cleaned_df):,} cleaned rows)")
        except Exception as exc:
            meta["clean_errors"][fname] = str(exc)
            if strict:
                print(f"  [ERROR] Cleaning failed for {fname} (strict mode blocked raw copy): {exc}")
                if os.path.exists(dest_file):
                    try:
                        os.remove(dest_file)
                    except OSError:
                        pass
            else:
                print(f"  [WARN] Copying without modification ({exc}): {fname}")
                shutil.copy2(src_path, dest_file)
                meta["staged_files"].append(fname)

    meta_path = os.path.join(dst_dir, "_clean_meta.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def run():
    all_files = glob.glob(os.path.join(EXPORTS_DIR, "*.xlsx"))
    if not all_files:
        print(f"No .xlsx files found in '{EXPORTS_DIR}'. Run scraper_backfill.py first.")
        return

    meta = clean_backfill_directory(EXPORTS_DIR, CLEANED_EXPORTS_DIR, strict=True)
    print(f"\nTransformed and staged {len(meta['staged_files'])} file(s) in '{CLEANED_EXPORTS_DIR}/'")


if __name__ == "__main__":
    run()
