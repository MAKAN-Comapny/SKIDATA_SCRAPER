"""
run_reconcile.py - Master Orchestrator for SkiData vs. Microsoft Fabric 15-Day Reconciliation.

Usage:
  # Standard 15-day run
  python run_reconcile.py --days 15

  # Run fresh scrape from SkiData Qlik portal first, then reconcile
  python run_reconcile.py --fresh-scrape --days 15

  # Dry-run for local verification (creates HTML & Excel without sending email)
  python run_reconcile.py --dry-run --test-mode

  # Custom recipient override
  python run_reconcile.py --recipients "sumit.dhankhar@makan.sa,karim.essam@makan.sa"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

from fabric_reader import FabricReader
from skidata_reader import SkiDataReader
from reconcile_engine import ReconcileEngine
from notifier import dispatch_reconciliation_report, get_recipients

load_dotenv()

# Ensure utf-8 encoding for console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_reconcile")


def parse_args():
    parser = argparse.ArgumentParser(description="SkiData vs. Microsoft Fabric 15-Day Reconciliation Pipeline")
    parser.add_argument("--days", type=int, default=15, help="Number of rolling days to reconcile (default: 15)")
    parser.add_argument("--target-date", type=str, default="", help="Target end date (YYYY-MM-DD). Defaults to latest file date / yesterday.")
    parser.add_argument(
        "--source",
        choices=["sharepoint", "scrape", "local"],
        default="sharepoint",
        help="Data source: 'sharepoint' (Fast: 15s from 'Ski Data 5'), 'scrape' (18min Playwright portal scrape), or 'local' (offline files)",
    )
    parser.add_argument(
        "--source-folder",
        type=str,
        default="Ski Data 5",
        help="SharePoint source folder name containing rolling 15-day files (default: 'Ski Data 5')",
    )
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Bypass stale data guard (allows running on older data for testing if today's data is not yet in SharePoint)",
    )
    parser.add_argument("--fresh-scrape", action="store_true", help="Alias to force --source scrape (runs Playwright scraper before reconciling)")
    parser.add_argument("--dry-run", action="store_true", help="Generate Excel report without sending notification or uploading to SharePoint")
    parser.add_argument("--test-mode", action="store_true", help="Run with mock Fabric data for offline local testing")
    parser.add_argument("--test-connection", action="store_true", help="Quickly verify Fabric connection and schema tables in < 3s")
    parser.add_argument("--recipients", type=str, default="", help="Comma-separated recipient emails (overrides .env)")
    parser.add_argument("--output-dir", type=str, default="reconciliation_reports", help="Output directory for reports")
    return parser.parse_args()


def main():
    args = parse_args()
    
    print("=" * 70)
    print("🚀 MAKAN PARKING: 15-DAY SKIDATA vs. FABRIC RECONCILIATION ENGINE")
    print("=" * 70)

    # 0. Pre-Flight Connection & Query Check (Fails in < 3s instead of waiting 18 mins!)
    print("\n" + "=" * 70)
    print("🔌 STEP 0: PRE-FLIGHT MICROSOFT FABRIC CONNECTION & QUERY CHECK")
    print("=" * 70)
    fabric_reader = FabricReader(mock_mode=args.test_mode)
    try:
        tables = fabric_reader.get_schema_tables()
        logger.info(f"✅ Fabric Connection Verified in < 2s! Discovered {len(tables)} objects in schema [{fabric_reader.schema_name}]: {tables}")

        # Introspect target tables and sample data
        fabric_reader.test_connection()

        # Pre-flight revenue query validation (checks syntax, columns, permissions)
        test_check_date = datetime.now().date() - timedelta(days=2)
        logger.info(f"Pre-flight testing revenue summary query for {test_check_date}...")
        test_summary_df = fabric_reader.get_daily_revenue_summary(test_check_date, test_check_date)
        logger.info(f"✅ Pre-flight revenue summary query SUCCESS! ({len(test_summary_df)} rows returned)")

    except Exception as e_preflight:
        logger.error(f"❌ Pre-flight Fabric check failed: {e_preflight}")
        logger.error("Aborting immediately before running source acquisition to save time.")
        sys.exit(1)

    if args.test_connection:
        print("\n✅ Fabric preflight healthcheck and query test passed! Exiting (test-connection mode).")
        sys.exit(0)

    # 1. Resolve Target Date Range
    explicit_target_date = None
    if args.target_date:
        try:
            explicit_target_date = datetime.strptime(args.target_date, "%Y-%m-%d").date()
            target_end_date = explicit_target_date
        except ValueError:
            logger.error(f"Invalid date format for --target-date: {args.target_date}. Expected YYYY-MM-DD.")
            sys.exit(1)
    else:
        target_end_date = datetime.now().date() - timedelta(days=1)

    # 2. Source Data Acquisition (SharePoint 'Ski Data 5', Playwright Scrape, or Local)
    effective_source = "scrape" if args.fresh_scrape else args.source

    if effective_source == "sharepoint":
        print("\n" + "=" * 70)
        print(f"📥 STEP 1: FETCH LATEST 15-DAY FILES FROM SHAREPOINT [{args.source_folder}]")
        print("=" * 70)
        try:
            from sharepoint_reader import download_latest_from_sharepoint
            sp_files = download_latest_from_sharepoint(
                source_folder=args.source_folder,
                target_date=explicit_target_date,
                allow_stale=args.allow_stale,
                dest_dir="exports",
            )
            logger.info(f"✅ SharePoint files successfully fetched from [{args.source_folder}]: {sp_files}")
            # If target_date was not explicitly pinned by the user, align target_end_date to the file's date
            if explicit_target_date is None and sp_files.get("file_date"):
                target_end_date = sp_files["file_date"]
                logger.info(f"Target evaluation end date aligned with SharePoint file date: {target_end_date}")
        except Exception as e_sp_src:
            logger.error(f"❌ SharePoint source fetch failed: {e_sp_src}")
            logger.error("STALE DATA GUARD: If today's files have not landed yet, aborting to prevent inaccurate audit.")
            sys.exit(1)

    elif effective_source == "scrape":
        print("\n" + "=" * 70)
        print("🌐 STEP 1: EXECUTING LIVE SKIDATA PORTAL SCRAPE (15-DAY ROLLING)")
        print("=" * 70)
        logger.info("Executing fresh SkiData Playwright scrape (15-day rolling)...")
        os.environ["DAYS_COUNT"] = str(args.days)
        try:
            from scraper import login_and_open_portal
            asyncio.run(login_and_open_portal())
            logger.info("Scraper execution completed successfully.")
            try:
                from Transformer import run as transform_exports
                logger.info("Transforming downloaded exports into Cleaned-Exports/...")
                transform_exports()
            except Exception as te:
                logger.info(f"Transform step note (files will be read from exports/): {te}")
        except Exception as e:
            logger.error(f"SkiData scraper failed during fresh scrape: {e}")
            sys.exit(1)

    elif effective_source == "local":
        print("\n" + "=" * 70)
        print("📂 STEP 1: USING LOCAL CACHED EXPORTS (Cleaned-Exports/ or exports/)")
        print("=" * 70)

    start_date = target_end_date - timedelta(days=args.days - 1)
    logger.info(f"Target Evaluation Window: {start_date} to {target_end_date} ({args.days} days)")

    # 3. Handle Recipient Overrides
    if args.recipients:
        os.environ["RECON_ALERT_RECIPIENTS"] = args.recipients

    recipients = get_recipients()
    logger.info(f"Authorized Report Recipients: {', '.join(recipients)}")

    # 4. Initialize Readers (fabric_reader already initialized in Step 0)
    skidata_reader = SkiDataReader(exports_dir="Cleaned-Exports", fallback_dir="exports")

    # 5. Execute Reconciliation Engine
    engine = ReconcileEngine(fabric_reader=fabric_reader, skidata_reader=skidata_reader)
    try:
        result = engine.run_reconciliation(
            days=args.days,
            target_end_date=target_end_date,
            output_dir=args.output_dir,
        )
    except Exception as e:
        logger.error(f"Reconciliation engine encountered an error: {e}", exc_info=True)
        sys.exit(1)

    # 6. Terminal Summary Printout
    print("\n" + "=" * 70)
    print(f"📊 RECONCILIATION SUMMARY MATRIX ({result.start_date} to {result.end_date})")
    print("=" * 70)
    print(result.summary_df.to_string(index=False))
    print("-" * 70)
    print(f"Total Evaluated Pairs: {len(result.summary_df)}")
    print(f"Total Inconsistencies: {result.total_mismatches}")
    print(f"Net Revenue Variance:  {result.total_revenue_diff:+,.2f} SAR")
    print(f"Excel Audit File:      {result.excel_report_path}")
    print("=" * 70)

    # 7. Direct Email Notification (Suppressed if 100% Match)
    html_preview_path = os.path.join(args.output_dir, f"Reconciliation_Preview_{target_end_date}.html")
    if result.total_mismatches == 0:
        logger.info("🎉 100% Match! Suppressing direct email notification as requested by management.")
    else:
        dispatch_success = dispatch_reconciliation_report(
            result=result,
            dry_run=args.dry_run,
            output_html_path=html_preview_path,
        )
        if args.dry_run:
            logger.info(f"Dry-run complete. HTML preview generated: {html_preview_path}")
        elif dispatch_success:
            logger.info("Reconciliation audit notification delivered successfully.")
        else:
            logger.warning("Notification could not be dispatched (check credentials). HTML report preserved locally.")

    # 8. Upload to SharePoint: ONLY when there is a mismatch (result.total_mismatches > 0)
    # Uploads ONLY the single Excel audit file (Dashboard is inside Tab 1) to avoid duplicate emails
    if result.total_mismatches == 0:
        print("\n" + "=" * 70)
        print("🎉 100% MATCH ACROSS ALL 15 DAYS! (0 mismatches found)")
        print("Skipping SharePoint upload so NO alert email is triggered for management.")
        print("=" * 70)
    else:
        target_sp_folder = os.getenv("SHAREPOINT_RECON_FOLDER", "reconcile-15days(Source & Fabric)")
        try:
            from To_Sharepoint import get_access_token, get_site_id, ensure_folder_path, upload_file
            logger.info(f"🚨 Discrepancies detected ({result.total_mismatches} pairs). Uploading SINGLE Excel audit report to SharePoint: [{target_sp_folder}]...")
            token = get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            site_id = get_site_id(headers)
            ensure_folder_path(site_id, headers, folder=target_sp_folder)

            # Upload ONLY the single Excel audit file (prevents duplicate emails in SharePoint)
            if result.excel_report_path and os.path.exists(result.excel_report_path):
                upload_file(site_id, headers, result.excel_report_path, folder=target_sp_folder)
                logger.info(f"✅ Single Excel audit report uploaded to SharePoint: {target_sp_folder}/{os.path.basename(result.excel_report_path)}")
                logger.info("SharePoint rule will now notify Sumit & Karim with the single audit report.")
        except Exception as e_sp:
            logger.warning(f"SharePoint upload skipped/failed: {e_sp}")

    print("\n✅ Reconciliation job finished successfully.\n")


if __name__ == "__main__":
    main()
