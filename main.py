"""
SKIDATA Scraper Pipeline

Orchestrates the full workflow:
  1. Scraper       - Login to SKIDATA portal, download reports to exports/
  2. Validator     - Ensure data integrity, correct dates, and uncorrupted containers
  3. Transformer   - Clean Excel files, move to Cleaned-Exports/
  4. To_Sharepoint - Upload cleaned files to Microsoft SharePoint
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

from scraper import login_and_open_portal
from validator import validate_exports
from Transformer import run as transform_exports
from To_Sharepoint import main as upload_to_sharepoint


def main():
    """Run the full pipeline: scrape -> validate -> transform -> upload to SharePoint."""
    load_dotenv()

    print("=" * 60)
    print("Step 1/4: Scraping SKIDATA reports...")
    print("=" * 60)
    asyncio.run(login_and_open_portal())

    skip_validation = os.getenv("SKIP_VALIDATION", "false").lower() in ("true", "1", "yes")
    if not skip_validation:
        print("\n" + "=" * 60)
        print("Step 2/4: Validating downloaded exports...")
        print("=" * 60)
        validate_exports(exports_dir="exports", strict=True)
    else:
        print("\n" + "=" * 60)
        print("Step 2/4: Skipped export validation (SKIP_VALIDATION=true)...")
        print("=" * 60)

    print("\n" + "=" * 60)
    print("Step 3/4: Transforming and cleaning exports...")
    print("=" * 60)
    transform_exports()

    target_folder = os.getenv("TARGET_FOLDER_PATH") or "Ski Data 5"
    if target_folder == "Skidata_Daily_Files":
        print("SAFETY GUARDRAIL: Blocked upload to 'Skidata_Daily_Files' on dev. Redirecting to 'Ski Data 5'.")
        target_folder = "Ski Data 5"
        os.environ["TARGET_FOLDER_PATH"] = "Ski Data 5"

    print("\n" + "=" * 60)
    print(f"Step 4/4: Uploading to SharePoint ({target_folder})...")
    print("=" * 60)
    upload_to_sharepoint()

    print("\n" + "=" * 60)
    print("Pipeline complete: All reports uploaded to SharePoint.")
    print("=" * 60)


if __name__ == "__main__":
    main()
    sys.exit(0)
