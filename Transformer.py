"""Clean and move SKIDATA export files from exports/ to Cleaned-Exports/."""
import os
import glob
import shutil

import pandas as pd

EXPORTS_DIR = "exports"
CLEANED_EXPORTS_DIR = "Cleaned-Exports"


def _require_date(data, file):
    if "Date" not in data.columns:
        raise SystemExit(
            f"{os.path.basename(file)} has no 'Date' column (columns: {list(data.columns)[:8]}). "
            "The export is empty or its layout changed."
        )


def run():
    """Clean downloaded files and move them to Cleaned-Exports folder."""
    all_files = glob.glob(os.path.join(EXPORTS_DIR, "*.xlsx"))
    if not all_files:
        raise SystemExit(f"No .xlsx files found in '{EXPORTS_DIR}'. Run scraper first.")

    # Exclude Parking-Transactions from cleaning (they're still copied to Cleaned-Exports later)
    downloaded_files = [f for f in all_files if "Parking-Transactions" not in f]

    for file in downloaded_files:
        if "Access-In-Depth" in file:
            data = pd.read_excel(file)
            _require_date(data, file)
            data = data[data["Date"] != "Totals"]
            data = data.dropna(subset=["Date"])
            data.to_excel(file, index=False)

        if "Revenue-In-Depth" in file:
            data = pd.read_excel(file, header=1)
            _require_date(data, file)
            data = data[data["Date"] != "Totals"]
            data = data.dropna(subset=["Date"])
            data.to_excel(file, index=False)

        if "System-Event-In-Depth" in file:
            data = pd.read_excel(file)
            _require_date(data, file)
            data = data[data["Date"] != "Totals"]
            data = data.dropna(subset=["Date"])
            data.to_excel(file, index=False)

    os.makedirs(CLEANED_EXPORTS_DIR, exist_ok=True)
    for file in all_files:
        shutil.copy(file, CLEANED_EXPORTS_DIR)
    for file in all_files:
        os.remove(file)

    print(f"Transformed {len(all_files)} file(s) to {CLEANED_EXPORTS_DIR}/")


if __name__ == "__main__":
    run()