"""
SKIDATA Scraper Pipeline

Orchestrates the full workflow:
  1. Scraper  - Login to SKIDATA portal, download reports to exports/
  2. Transformer - Clean Excel files, move to Cleaned-Exports/
  3. odoo_export   - Build the Odoo import file and upload it to "SKIDATA - Odoo"
  4. To_Sharepoint - Upload cleaned files to Microsoft SharePoint
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

from scraper import login_and_open_portal
from Transformer import run as transform_exports
from To_Sharepoint import main as upload_to_sharepoint
from odoo_export import main as export_for_odoo


def main():
    """Run the full pipeline: scrape -> transform -> upload to SharePoint."""
    load_dotenv()

    print("=" * 60)
    print("Step 1/4: Scraping SKIDATA reports...")
    print("=" * 60)
    asyncio.run(login_and_open_portal())

    print("\n" + "=" * 60)
    print("Step 2/4: Transforming and cleaning exports...")
    print("=" * 60)
    transform_exports()

    # Odoo file first, from the local cleaned file; the raw upload below is what the Fabric
    # loader picks up and later removes. Each step is attempted even if the other fails.
    failures = []

    skip_odoo = os.getenv("SKIP_ODOO_EXPORT", "false").lower() in ("true", "1", "yes")
    odoo_folder = os.getenv("TARGET_FOLDER_PATH_ODOO") or "SKIDATA - Odoo"
    if not skip_odoo:
        print("\n" + "=" * 60)
        print(f"Step 3/4: Building and uploading the Odoo file ({odoo_folder})...")
        print("=" * 60)
        try:
            export_for_odoo()
        except (Exception, SystemExit) as exc:
            print(f"Odoo export failed: {exc!r}")
            failures.append("odoo export")
    else:
        print("\n" + "=" * 60)
        print("Step 3/4: Skipped Odoo export (SKIP_ODOO_EXPORT=true)...")
        print("=" * 60)

    target_folder = os.getenv("TARGET_FOLDER_PATH") or "Skidata_Daily_Files"
    print("\n" + "=" * 60)
    print(f"Step 4/4: Uploading to SharePoint ({target_folder})...")
    print("=" * 60)
    try:
        upload_to_sharepoint()
    except (Exception, SystemExit) as exc:
        print(f"Raw upload failed: {exc!r}")
        failures.append("raw upload")

    if failures:
        raise SystemExit("Pipeline step(s) failed: " + ", ".join(failures))

    print("\n" + "=" * 60)
    print("Pipeline complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
    sys.exit(0)
