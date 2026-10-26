"""
SKIDATA Historical Backfill Pipeline Orchestrator
------------------------------------------------
Orchestrates downloading, cleaning, and uploading of historical monthly reports:
  - August 2026   -> SharePoint 'Validated_Docs/old/Aug'
  - September 2026 -> SharePoint 'Validated_Docs/old/Sep'

Each month contains all 4 core tables:
  1. Access In Depth Analysis
  2. Parking Transactions
  3. Revenue In Depth Analysis
  4. System Event In Depth Analysis

Usage:
  # Download and upload both August and September 2026
  python main_backfill.py --month both

  # August 2026 only
  python main_backfill.py --month August

  # September 2026 only
  python main_backfill.py --month September

  # Dry run locally without uploading to SharePoint
  python main_backfill.py --month both --no-upload
"""
import argparse
import asyncio
import os
import shutil
import sys
from datetime import date
from dotenv import load_dotenv

load_dotenv()

from scraper_backfill import scrape_month
from Transformer_backfill import run as transform_backfill
from To_Sharepoint_backfill import main as upload_backfill

MONTH_SPECS = {
    "august": {
        "name": "August 2026",
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 31),
        "file_prefix": "01-Aug-26-to-31-Aug-26",
        "sharepoint_folder": "Validated_Docs/old/Aug",
        "local_exports": "exports_backfill/Aug",
        "local_cleaned": "Cleaned-Exports-Backfill/Aug",
    },
    "september": {
        "name": "September 2026",
        "start_date": date(2026, 9, 1),
        "end_date": date(2026, 9, 30),
        "file_prefix": "01-Sep-26-to-30-Sep-26",
        "sharepoint_folder": "Validated_Docs/old/Sep",
        "local_exports": "exports_backfill/Sep",
        "local_cleaned": "Cleaned-Exports-Backfill/Sep",
    },
}


def clean_and_stage(src_dir: str, dst_dir: str):
    """Copies downloaded files and removes totals rows where applicable."""
    import pandas as pd
    import glob

    os.makedirs(dst_dir, exist_ok=True)
    all_files = glob.glob(os.path.join(src_dir, "*.xlsx"))
    print(f"\nCleaning and staging {len(all_files)} file(s) from {src_dir} -> {dst_dir}...")

    for f in all_files:
        dest_file = os.path.join(dst_dir, os.path.basename(f))
        try:
            if "Access-In-Depth" in f:
                df = pd.read_excel(f)
                df = df[df["Date"] != "Totals"].dropna(subset=["Date"])
                df.to_excel(dest_file, index=False)
            elif "Revenue-In-Depth" in f:
                df = pd.read_excel(f, header=1)
                df = df[df["Date"] != "Totals"].dropna(subset=["Date"])
                df.to_excel(dest_file, index=False)
            elif "System-Event-In-Depth" in f:
                df = pd.read_excel(f)
                df = df[df["Date"] != "Totals"].dropna(subset=["Date"])
                df.to_excel(dest_file, index=False)
            else:
                shutil.copy2(f, dest_file)
            print(f"  OK: {os.path.basename(dest_file)}")
        except Exception as e:
            print(f"  Copying without modification ({e}): {os.path.basename(f)}")
            shutil.copy2(f, dest_file)


async def execute_month_pipeline(spec: dict, sheet_filter: str, headless: bool, no_upload: bool):
    print("\n" + "#" * 65)
    print(f"# STARTING PIPELINE: {spec['name'].upper()}")
    print(f"# SharePoint Target: {spec['sharepoint_folder']}")
    print("#" * 65)

    os.makedirs(spec["local_exports"], exist_ok=True)
    os.makedirs(spec["local_cleaned"], exist_ok=True)

    # 1. Scrape from portal
    await scrape_month(
        month_name=spec["name"],
        start_date=spec["start_date"],
        end_date=spec["end_date"],
        file_prefix=spec["file_prefix"],
        download_dir=spec["local_exports"],
        sheet_filter=sheet_filter,
        headless=headless,
    )

    # 2. Clean and Stage
    clean_and_stage(spec["local_exports"], spec["local_cleaned"])

    # 3. Upload to SharePoint
    if not no_upload:
        print(f"\nUploading to SharePoint: {spec['sharepoint_folder']}...")
        upload_backfill(target_folder=spec["sharepoint_folder"], source_dir=spec["local_cleaned"])
    else:
        print(f"\nUpload skipped (--no-upload). Files saved locally in {spec['local_cleaned']}/")

    print("\n" + "=" * 65)
    print(f"COMPLETED PIPELINE FOR {spec['name'].upper()}")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="SKIDATA Historical Backfill Pipeline")
    parser.add_argument(
        "--month",
        type=str,
        default="both",
        choices=["august", "september", "both", "all", "aug", "sep"],
        help="Target month (default: both)",
    )
    parser.add_argument(
        "--sheet",
        type=str,
        default="all",
        choices=["all", "parking", "revenue", "access", "events"],
        help="Target sheets or all",
    )
    parser.add_argument("--headless", action="store_true", default=False, help="Run browser in headless mode")
    parser.add_argument("--no-upload", action="store_true", default=False, help="Skip SharePoint upload")
    args = parser.parse_args()

    # Normalize month selection
    target = args.month.lower()
    if target in ("aug", "august"):
        months_to_run = [MONTH_SPECS["august"]]
    elif target in ("sep", "september"):
        months_to_run = [MONTH_SPECS["september"]]
    else:
        months_to_run = [MONTH_SPECS["august"], MONTH_SPECS["september"]]

    for spec in months_to_run:
        asyncio.run(
            execute_month_pipeline(
                spec=spec,
                sheet_filter=args.sheet,
                headless=args.headless,
                no_upload=args.no_upload,
            )
        )

    print("\n" + "#" * 65)
    print("# ALL HISTORICAL BACKFILL TASKS FINISHED SUCCESSFULLY!")
    print("#" * 65)


if __name__ == "__main__":
    main()
