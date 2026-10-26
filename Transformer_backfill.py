"""
Transformer Backfill
--------------------
Cleans downloaded historical backfill files from 'exports_backfill/'
and moves them to 'Cleaned-Exports-Backfill/'.
"""
import glob
import os
import shutil
import pandas as pd

EXPORTS_DIR = "exports_backfill"
CLEANED_EXPORTS_DIR = "Cleaned-Exports-Backfill"


def run():
    all_files = glob.glob(os.path.join(EXPORTS_DIR, "*.xlsx"))
    if not all_files:
        print(f"No .xlsx files found in '{EXPORTS_DIR}'. Run scraper_backfill.py first.")
        return

    # Parking-Transactions are copied without filtering
    downloaded_files = [f for f in all_files if "Parking-Transactions" not in f]

    for file in downloaded_files:
        try:
            if "Access-In-Depth" in file:
                data = pd.read_excel(file)
                data = data[data["Date"] != "Totals"]
                data = data.dropna(subset=["Date"])
                data.to_excel(file, index=False)
                print(f"Cleaned Access-In-Depth: {os.path.basename(file)} ({len(data)} rows)")

            elif "Revenue-In-Depth" in file:
                data = pd.read_excel(file, header=1)
                data = data[data["Date"] != "Totals"]
                data = data.dropna(subset=["Date"])
                data.to_excel(file, index=False)
                print(f"Cleaned Revenue-In-Depth: {os.path.basename(file)} ({len(data)} rows)")

            elif "System-Event-In-Depth" in file:
                data = pd.read_excel(file)
                data = data[data["Date"] != "Totals"]
                data = data.dropna(subset=["Date"])
                data.to_excel(file, index=False)
                print(f"Cleaned System-Event-In-Depth: {os.path.basename(file)} ({len(data)} rows)")
        except Exception as e:
            print(f"Warning: Could not process {file}: {e}")

    os.makedirs(CLEANED_EXPORTS_DIR, exist_ok=True)
    for file in all_files:
        shutil.copy(file, CLEANED_EXPORTS_DIR)
        print(f"Staged in {CLEANED_EXPORTS_DIR}/: {os.path.basename(file)}")

    print(f"\nTransformed and staged {len(all_files)} file(s) in '{CLEANED_EXPORTS_DIR}/'")


if __name__ == "__main__":
    run()
