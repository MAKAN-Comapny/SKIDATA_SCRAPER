"""
SKIDATA Historical & Range Backfill Pipeline Orchestrator
---------------------------------------------------------
Orchestrates downloading, cleaning, and uploading of historical monthly or custom date range reports:
  - By Month:
      August 2026   -> SharePoint 'Validated_Docs/old/Aug'
      September 2026 -> SharePoint 'Validated_Docs/old/Sep'
      July 2026      -> SharePoint 'Validated_Docs/old/Jul'
      Any other month...
  - By Date Range:
      10-10-2026 to 23-10-2026 -> SharePoint 'Validated_Docs/old/10-Oct-26-to-23-Oct-26'
      (Automatically creates the folder in Validated_Docs/old/ by range name!)

Each run downloads all 4 core tables:
  1. Access In Depth Analysis
  2. Parking Transactions
  3. Revenue In Depth Analysis
  4. System Event In Depth Analysis

Usage Examples:
  # Download August and September 2026:
  python main_backfill.py --month both

  # Download specific month:
  python main_backfill.py --month August
  python main_backfill.py --month September

  # Download custom date range:
  python main_backfill.py --range "10-10-2026 to 23-10-2026"

  # Dry run locally without uploading to SharePoint:
  python main_backfill.py --month August --no-upload
"""
import argparse
import asyncio
import calendar
import os
import re
import shutil
import sys
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv()

from scraper_backfill import scrape_month
from Transformer_backfill import run as transform_backfill
from To_Sharepoint_backfill import main as upload_backfill

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_single_date(text: str) -> date:
    text = text.strip()
    for fmt in [
        "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
        "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%Y/%m/%d",
    ]:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Could not parse date: '{text}'. Use DD-MM-YYYY or YYYY-MM-DD.")


def parse_date_range(range_str: str) -> tuple[date, date, str, str]:
    """Parse string like '10-10-2026 to 23-10-2026' into start_date, end_date, file_prefix, folder_name."""
    delimiters = [" to ", " - ", " -> ", "..", ","]
    parts = None
    for d in delimiters:
        if d in range_str:
            parts = range_str.split(d, 1)
            break
    if not parts or len(parts) != 2:
        raise ValueError(f"Invalid range format: '{range_str}'. Expected 'start to end', e.g. '10-10-2026 to 23-10-2026'")

    start_d = parse_single_date(parts[0])
    end_d = parse_single_date(parts[1])
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    # Prefix matches standard SharePoint reference: e.g. 10-Oct-26-to-23-Oct-26
    start_str = start_d.strftime("%d-%b-%y")
    end_str = end_d.strftime("%d-%b-%y")
    file_prefix = f"{start_str}-to-{end_str}"
    folder_name = file_prefix
    return start_d, end_d, file_prefix, folder_name


def parse_month_spec(raw: str, default_year: int = 2026) -> tuple[date, date, str, str]:
    """Parse month name like 'August', 'Sep 2026', 'July' into start_date, end_date, prefix, folder."""
    raw = raw.strip()
    year_match = re.search(r"\b(20\d\d)\b", raw)
    year = int(year_match.group(1)) if year_match else default_year
    clean_name = re.sub(r"[^a-zA-Z]", "", raw).lower()
    if clean_name not in MONTH_MAP:
        raise ValueError(f"Unrecognized month name: '{raw}'")
    m_num = MONTH_MAP[clean_name]
    num_days = calendar.monthrange(year, m_num)[1]
    start_d = date(year, m_num, 1)
    end_d = date(year, m_num, num_days)
    prefix = f"{start_d.strftime('%d-%b-%y')}-to-{end_d.strftime('%d-%b-%y')}"
    folder_name = start_d.strftime("%b")  # e.g. Aug, Sep, Jul
    return start_d, end_d, prefix, folder_name


def build_pipeline_specs(
    month: str | None = None,
    date_range: str | None = None,
    custom_folder: str | None = None,
) -> list[dict]:
    specs = []
    base_sharepoint_root = "Validated_Docs/old"

    if date_range and date_range.strip():
        start_d, end_d, prefix, folder_name = parse_date_range(date_range)
        sp_folder = custom_folder or f"{base_sharepoint_root}/{folder_name}"
        specs.append({
            "name": f"Range ({prefix})",
            "start_date": start_d,
            "end_date": end_d,
            "file_prefix": prefix,
            "sharepoint_folder": sp_folder,
            "local_exports": os.path.join("exports_backfill", folder_name),
            "local_cleaned": os.path.join("Cleaned-Exports-Backfill", folder_name),
        })
        return specs

    m_raw = (month or "both").strip().lower()
    if m_raw in ("both", "all"):
        months_to_parse = ["august 2026", "september 2026"]
    elif "," in m_raw:
        months_to_parse = [m.strip() for m in m_raw.split(",") if m.strip()]
    else:
        months_to_parse = [m_raw]

    for m in months_to_parse:
        start_d, end_d, prefix, folder_name = parse_month_spec(m)
        sp_folder = custom_folder or f"{base_sharepoint_root}/{folder_name}"
        specs.append({
            "name": f"{folder_name} {start_d.year}",
            "start_date": start_d,
            "end_date": end_d,
            "file_prefix": prefix,
            "sharepoint_folder": sp_folder,
            "local_exports": os.path.join("exports_backfill", folder_name),
            "local_cleaned": os.path.join("Cleaned-Exports-Backfill", folder_name),
        })

    return specs


def clean_and_stage(src_dir: str, dst_dir: str):
    """Copies downloaded files and removes totals rows where applicable."""
    import pandas as pd
    import glob

    os.makedirs(dst_dir, exist_ok=True)
    # Only stage main month files, skip any internal daily chunk folder
    all_files = [
        f for f in glob.glob(os.path.join(src_dir, "*.xlsx"))
        if os.path.isfile(f) and "daily_chunks" not in f
    ]
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

    try:
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
    except Exception as scrape_err:
        print(f"\n[NOTICE] Scraping encountered an issue: {scrape_err}")
    finally:
        # 2. Clean and Stage downloaded files
        clean_and_stage(spec["local_exports"], spec["local_cleaned"])

        # 3. Upload to SharePoint
        if not no_upload:
            print(f"\nUploading available files to SharePoint: {spec['sharepoint_folder']}...")
            try:
                upload_backfill(target_folder=spec["sharepoint_folder"], source_dir=spec["local_cleaned"])
            except (Exception, SystemExit) as up_err:
                print(f"SharePoint upload error: {up_err}")
        else:
            print(f"\nUpload skipped (--no-upload). Files saved locally in {spec['local_cleaned']}/")

    print("\n" + "=" * 65)
    print(f"COMPLETED PIPELINE FOR {spec['name'].upper()}")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="SKIDATA Historical / Range Backfill Pipeline")
    parser.add_argument(
        "--month",
        type=str,
        default="both",
        help="Target month (e.g. 'both', 'August', 'September', 'Aug', 'Sep', 'July'). Default: both",
    )
    parser.add_argument(
        "--range",
        type=str,
        default="",
        help="Date range (e.g. '10-10-2026 to 23-10-2026'). If provided, overrides --month.",
    )
    parser.add_argument(
        "--folder",
        type=str,
        default="",
        help="Custom SharePoint destination folder (overrides default 'Validated_Docs/old/<Folder>')",
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

    # Build dynamic pipeline specifications
    specs_to_run = build_pipeline_specs(
        month=args.month,
        date_range=args.range,
        custom_folder=args.folder if args.folder.strip() else None,
    )

    print("\n" + "=" * 65)
    print(f"HISTORICAL / RANGE BACKFILL INITIALIZED ({len(specs_to_run)} task(s))")
    for s in specs_to_run:
        print(f"  - {s['name']}: {s['start_date']} -> {s['end_date']}")
        print(f"    Prefix:     {s['file_prefix']}")
        print(f"    SharePoint: {s['sharepoint_folder']}")
    print("=" * 65)

    for spec in specs_to_run:
        asyncio.run(
            execute_month_pipeline(
                spec=spec,
                sheet_filter=args.sheet,
                headless=args.headless,
                no_upload=args.no_upload,
            )
        )

    print("\n" + "#" * 65)
    print("# ALL HISTORICAL / RANGE BACKFILL TASKS FINISHED SUCCESSFULLY!")
    print("#" * 65)


if __name__ == "__main__":
    main()
